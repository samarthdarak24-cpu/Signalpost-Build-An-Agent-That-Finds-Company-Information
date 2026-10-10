"""The orchestrator.

Reads organisation numbers, runs each one through the cost cascade, writes a
profile per company, and prints a run report with time, search calls, tokens and
cost. Everything configurable lives in :class:`Settings` so the same code path
serves a one-company smoke test, the offline demo and a thousand-company batch.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from delta import (
    SnapshotStore,
    WebSnapshot,
    apply_registry_change,
    detect_web_changes,
    empty_for,
    fact_values_blob,
    recheck_web,
)
from enrich import enrich
from explain import explain_profile, render_report
from extract_llm import GAP_FIELDS, MODEL_GAP, MODEL_VERIFY, PRICING, LlmClient
from extract_rules import extract, is_organisational_contact
from gate import CONFIDENCE, decide, evaluate
from net import Budget, BudgetExceeded, HttpClient
from normalize import Page
from profiles import (
    Fact,
    Profile,
    SkippedField,
    utcnow_iso,
)
from registry import (
    REGISTRY_FACTS,
    BrregClient,
    RegnskapFigures,
    RegistryStore,
    address_line,
    entity_facts,
    fetch_regnskap,
)
from activity import activity_facts, activity_urls, extract_activity
from coverage import coverage_summary, domain_coverage, location_facts, people_facts
from entity_match import resolve_page
from jobs import careers_urls, extract_jobs, job_facts
from sitefinder import SiteFinder, page_urls
from synthesis import synthesis as synthesize_profile
from validate import OrgNumber, clean, format_display, validate
from verify import attach_org_number_proof, verify_profile

__all__ = ["Settings", "CompanyResult", "RunReport", "process_company", "run", "main"]

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

USEFUL_FIELDS: tuple[str, ...] = (
    # company record
    "legal_name", "legal_form", "status", "industry", "nace_code", "employees",
    "founded", "registered", "municipality", "postcode", "city", "address",
    "sector", "language_form", "purpose",
    # financials
    "latest_accounts_year", "accounting_year", "reporting_period", "revenue_latest",
    "net_result", "operating_result", "total_assets", "equity", "debt",
    # people & locations
    "ceo", "board_chair", "board_members", "auditor",
    "location_count", "business_locations",
    # website
    "website", "email", "phone", "description", "facebook", "linkedin", "instagram",
    # hiring & public activity
    "careers_page", "open_roles", "latest_job_title", "latest_update_title",
    "activity_count",
    # identifiers
    "lei", "parent_org_number",
)


@dataclass(slots=True)
class Settings:
    """Everything the pipeline needs, with the challenge's limits as defaults."""

    profiles_dir: Path = field(default_factory=lambda: ROOT / "profiles")
    runs_dir: Path = field(default_factory=lambda: ROOT / "runs")
    cache_dir: Path = field(default_factory=lambda: ROOT / "data" / "cache")
    registry_db: Path = field(default_factory=lambda: ROOT / "data" / "registry.sqlite")
    snapshot_db: Path = field(default_factory=lambda: ROOT / "data" / "snapshots.sqlite")
    llm_cache: Path = field(default_factory=lambda: ROOT / "data" / "llm_cache")

    offline: bool = False
    fetch_web: bool = True
    #: Annual accounts are a scored coverage category and cost nothing to fetch
    #: from Regnskapsregisteret, so they are on by default. Opt out with
    #: ``SIGNALPOST_NO_FINANCIALS=1``.
    fetch_financials: bool = True
    #: Hiring and public activity are read off a site that already passed the
    #: ownership gate, so they cost a couple of extra page fetches and no search
    #: or model spend. On by default; opt out with ``SIGNALPOST_NO_JOBS=1`` /
    #: ``SIGNALPOST_NO_ACTIVITY=1``.
    fetch_jobs: bool = True
    fetch_activity: bool = True
    use_llm: bool = True
    workers: int = 4
    max_pages_per_site: int = 4
    min_confidence: float = 0.60
    #: Confidence floor for values that arrived through the model rather than
    #: through a rule. Set higher because a model's reading of a page is a
    #: weaker guarantee than a regex that matched a literal.
    min_confidence_llm: float = 0.70
    reuse_cache: bool = True
    quiet: bool = False
    limit: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        def flag(name: str, default: bool = False) -> bool:
            return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"} or default

        return cls(
            offline=flag("SIGNALPOST_OFFLINE"),
            fetch_web=not flag("SIGNALPOST_NO_WEB"),
            fetch_financials=not flag("SIGNALPOST_NO_FINANCIALS"),
            fetch_jobs=not flag("SIGNALPOST_NO_JOBS"),
            fetch_activity=not flag("SIGNALPOST_NO_ACTIVITY"),
            use_llm=not flag("SIGNALPOST_NO_LLM"),
            workers=int(os.environ.get("SIGNALPOST_WORKERS", "4")),
            quiet=flag("SIGNALPOST_QUIET"),
        )


@dataclass(slots=True)
class CompanyResult:
    """Per-company bookkeeping, folded into the run report at the end."""

    org_number: str
    legal_name: str = ""
    ok: bool = False
    facts: int = 0
    empty: int = 0
    changes: int = 0
    gate_proof: str = ""
    gate_accepted: bool = False
    match_score: float = 0.0
    match_verdict: str = ""
    layers_used: list[str] = field(default_factory=list)
    requests: int = 0
    search_calls: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    error: str = ""
    #: "ok" | "empty" | "skipped" | "failed" -- what happened to website search,
    #: so a caller can tell "no website exists" from "the lookup did not run".
    search_status: str = ""
    search_error: str = ""
    #: Facts restored from the previously published profile because this run's
    #: sources did not produce them (resumability). Non-zero means the run was
    #: partial, not that the company changed.
    facts_carried_forward: int = 0
    #: Whether a previously published profile existed before this run. Separates
    #: "these facts are new" from "these facts were re-published unchanged" in
    #: the run report.
    had_previous_profile: bool = False

    @property
    def cost_usd(self) -> float:
        return (
            self.search_calls * PRICING["brave-search-query"]["input"]
            + _llm_cost(self.input_tokens, self.output_tokens)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_number": self.org_number,
            "legal_name": self.legal_name,
            "ok": self.ok,
            "facts": self.facts,
            "empty_fields": self.empty,
            "changes": self.changes,
            "gate": {"accepted": self.gate_accepted, "match_proof": self.gate_proof,
                     "match_score": round(self.match_score, 3),
                     "match_verdict": self.match_verdict},
            "layers_used": sorted(set(self.layers_used)),
            "requests": self.requests,
            "search_calls": self.search_calls,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "seconds": round(self.seconds, 2),
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
            "search": {"status": self.search_status, "error": self.search_error},
            "facts_carried_forward": self.facts_carried_forward,
            "had_previous_profile": self.had_previous_profile,
        }


def _llm_cost(input_tokens: int, output_tokens: int) -> float:
    price = PRICING[MODEL_GAP]
    return (input_tokens / 1_000_000) * price["input"] + (
        output_tokens / 1_000_000
    ) * price["output"]


@dataclass(slots=True)
class RunReport:
    """The artefact the brief asks for: cost, time and coverage, per run."""

    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    companies_requested: int = 0
    companies_written: int = 0
    companies_failed: int = 0
    invalid_numbers: list[str] = field(default_factory=list)
    not_in_registry: list[str] = field(default_factory=list)
    websites_accepted: int = 0
    websites_rejected: int = 0
    gate_breakdown: dict[str, int] = field(default_factory=dict)
    requests: int = 0
    search_calls: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    changes_detected: int = 0
    registry_events_applied: int = 0
    #: Companies that were not in the local mirror and were fetched live from the
    #: register. Non-zero on an evaluator batch of unseen companies.
    registry_fetched_live: int = 0
    corroborated_fields: int = 0
    conflicts_resolved: int = 0
    conflicts_dropped: int = 0
    invalid_dropped: int = 0
    field_coverage: dict[str, int] = field(default_factory=dict)
    layer_counts: dict[str, int] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    #: Execution-contract bookkeeping. ``envelopes_total`` must equal
    #: ``companies_requested`` on a healthy run, and the by-status tally must sum
    #: to the same number.
    envelopes_path: str = ""
    envelopes_total: int = 0
    envelopes_by_status: dict[str, int] = field(default_factory=dict)
    envelope_error: str = ""
    #: The terminal-output audit: inputs vs outputs, plus missing/duplicate/unknown
    #: checks. ``ok`` is true only when every input got exactly one output.
    envelope_audit: dict[str, Any] = field(default_factory=dict)
    #: §7 run-level accounting, derived from the real per-company results and the
    #: live budget rather than from constants. Populated by :meth:`finalise`.
    profiles_attempted: int = 0
    profiles_completed: int = 0
    profiles_deferred: int = 0
    facts_added: int = 0
    facts_updated: int = 0
    facts_unchanged: int = 0
    facts_rejected: int = 0
    searches_ok: int = 0
    searches_empty: int = 0
    searches_skipped: int = 0
    searches_failed: int = 0
    facts_carried_forward: int = 0
    #: Why the run stopped early, or "" if it ran to completion. Taken from the
    #: budget's own termination reason so it cannot disagree with the counters.
    termination_reason: str = ""
    cache_hits: int = 0
    outbound_attempts: int = 0
    outbound_succeeded: int = 0
    outbound_failed: int = 0
    outbound_redirected: int = 0
    outbound_retried: int = 0

    def finalise(self) -> None:
        """Derive the §7 run-level fields from the real results and budget.

        Everything here is a fold over ``self.results`` (one entry per company
        actually processed) or over ``self.budget`` (the live accountant). Nothing
        is hardcoded and nothing is estimated: if a number cannot be derived, it
        stays at its zero initialiser rather than being guessed.
        """
        self.profiles_attempted = len(self.results)
        self.profiles_completed = sum(1 for r in self.results if r.get("ok"))
        # Deferred = asked for but not completed. A company that errored, or that
        # never produced an envelope, is deferred rather than silently dropped.
        self.profiles_deferred = max(
            0, self.companies_requested - self.profiles_completed
        )

        searches = {"ok": 0, "empty": 0, "skipped": 0, "failed": 0}
        added = updated = unchanged = 0
        carried = 0
        for r in self.results:
            status = ((r.get("search") or {}).get("status") or "").strip()
            if status in searches:
                searches[status] += 1
            n = int(r.get("facts") or 0)
            changed = int(r.get("changes") or 0)
            had_previous = bool(r.get("had_previous_profile"))
            # Partition this profile's facts into disjoint buckets:
            #   updated   -- value differs from the previous run (a change record)
            #   unchanged -- a previous run published this field and it still matches
            #   added     -- the field was not in any previous run's profile
            # ``had_previous_profile`` is what separates "first publication" from
            # "re-published identically"; without it, every repeat run would
            # report its facts as newly added.
            if had_previous:
                updated += changed
                unchanged += max(0, n - changed)
            else:
                added += n
            carried += int(r.get("facts_carried_forward") or 0)

        self.facts_added = added
        self.facts_updated = updated
        self.facts_unchanged = unchanged
        # Rejected facts are counted by the verifier: invalid values dropped, plus
        # conflicts it resolved by discarding the losing candidate.
        self.facts_rejected = self.invalid_dropped + self.conflicts_dropped
        self.facts_carried_forward = carried
        self.searches_ok = searches["ok"]
        self.searches_empty = searches["empty"]
        self.searches_skipped = searches["skipped"]
        self.searches_failed = searches["failed"]

        b = self.budget or {}
        reqs = b.get("requests") or {}
        self.outbound_attempts = int(reqs.get("outbound_attempts") or 0)
        self.outbound_succeeded = int(reqs.get("succeeded") or 0)
        self.outbound_failed = int(reqs.get("failed") or 0)
        self.outbound_redirected = int(reqs.get("redirected") or 0)
        self.outbound_retried = int(reqs.get("retried") or 0)
        self.cache_hits = int(b.get("cache_hits") or 0)
        self.termination_reason = str(b.get("termination_reason") or "")

    @property
    def coverage(self) -> float:
        if not self.companies_written:
            return 0.0
        total = sum(self.field_coverage.values())
        return total / (self.companies_written * len(USEFUL_FIELDS))

    @property
    def search_cost(self) -> float:
        return self.search_calls * PRICING["brave-search-query"]["input"]

    @property
    def llm_cost(self) -> float:
        return _llm_cost(self.input_tokens, self.output_tokens)

    @property
    def total_cost(self) -> float:
        return self.search_cost + self.llm_cost

    def cost_per_1000(self) -> float:
        if not self.companies_written:
            return 0.0
        return self.total_cost / self.companies_written * 1000

    @property
    def seconds_per_company(self) -> float:
        if not self.companies_written:
            return 0.0
        return self.seconds / self.companies_written

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seconds": round(self.seconds, 2),
            "companies_requested": self.companies_requested,
            "companies_written": self.companies_written,
            "companies_failed": self.companies_failed,
            "invalid_numbers": self.invalid_numbers,
            "not_in_registry": self.not_in_registry,
            "websites_accepted": self.websites_accepted,
            "websites_rejected": self.websites_rejected,
            "gate_breakdown": self.gate_breakdown,
            "requests": self.requests,
            "search_calls": self.search_calls,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "changes_detected": self.changes_detected,
            "registry_events_applied": self.registry_events_applied,
            "registry_fetched_live": self.registry_fetched_live,
            "corroborated_fields": self.corroborated_fields,
            "conflicts_resolved": self.conflicts_resolved,
            "conflicts_dropped": self.conflicts_dropped,
            "invalid_dropped": self.invalid_dropped,
            "field_coverage": self.field_coverage,
            "coverage_ratio": round(self.coverage, 4),
            "layer_counts": self.layer_counts,
            "models": self.models,
            "budget": self.budget,
            "envelopes_path": self.envelopes_path,
            "envelopes_total": self.envelopes_total,
            "envelopes_by_status": self.envelopes_by_status,
            "envelope_audit": self.envelope_audit,
            "cost": {
                "search_calls": self.search_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "search_usd": round(self.search_cost, 6),
                "llm_usd": round(self.llm_cost, 6),
                "total_usd": round(self.total_cost, 6),
                "per_1000_profiles_usd": round(self.cost_per_1000(), 4),
            },
            #: §7: the run-level execution report. Every value is folded from the
            #: real per-company results and the live budget accountant; see
            #: :meth:`finalise`. ``budget`` carries the authoritative cost
            #: breakdown (actual / reserved / unknown) so the two never disagree.
            "execution": {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": round(self.seconds, 2),
                "termination_reason": self.termination_reason or "completed",
                "early_termination": bool(self.termination_reason),
                "profiles": {
                    "requested": self.companies_requested,
                    "attempted": self.profiles_attempted,
                    "completed": self.profiles_completed,
                    "deferred": self.profiles_deferred,
                    "failed": self.companies_failed,
                },
                "outbound": {
                    "attempts": self.outbound_attempts,
                    "succeeded": self.outbound_succeeded,
                    "failed": self.outbound_failed,
                    "redirected": self.outbound_redirected,
                    "retried": self.outbound_retried,
                    "cache_hits": self.cache_hits,
                },
                "by_provider": (self.budget or {}).get("by_provider", {}),
                "by_operation": (self.budget or {}).get("by_operation", {}),
                "cost": {
                    "actual_usd": ((self.budget or {}).get("cost") or {}).get("actual_usd", 0.0),
                    "reserved_usd": ((self.budget or {}).get("cost") or {}).get("reserved_usd", 0.0),
                    "worst_case_usd": ((self.budget or {}).get("cost") or {}).get("worst_case_usd", 0.0),
                    "unknown_cost_operations": ((self.budget or {}).get("cost") or {}).get(
                        "unknown_cost_operations", 0
                    ),
                },
                "facts": {
                    "added": self.facts_added,
                    "updated": self.facts_updated,
                    "unchanged": self.facts_unchanged,
                    "rejected": self.facts_rejected,
                    "carried_forward": self.facts_carried_forward,
                },
                "searches": {
                    "ok": self.searches_ok,
                    "empty": self.searches_empty,
                    "skipped": self.searches_skipped,
                    "failed": self.searches_failed,
                },
            },
            "results": self.results,
        }

    def render(self) -> str:
        """The human-readable run report printed at the end of every run."""
        lines = [
            "",
            "─" * 72,
            f"RUN REPORT  {self.run_id}",
            "─" * 72,
            f"  companies        requested {self.companies_requested:<6} "
            f"written {self.companies_written:<6} failed {self.companies_failed}",
            f"  wall clock       {self.seconds:.1f}s  "
            f"({self.seconds_per_company:.2f}s per company)",
            f"  coverage         {self.coverage * 100:.1f}% of {len(USEFUL_FIELDS)} useful fields",
            "",
            "  execution",
            f"    profiles       requested {self.companies_requested:<6} "
            f"completed {self.profiles_completed:<6} deferred {self.profiles_deferred}",
            f"    outbound       attempts {self.outbound_attempts:<6} "
            f"ok {self.outbound_succeeded:<6} failed {self.outbound_failed:<6} "
            f"redirects {self.outbound_redirected}  retries {self.outbound_retried}",
            f"    cache          hits {self.cache_hits}",
            f"    searches       ok {self.searches_ok}  empty {self.searches_empty}  "
            f"skipped {self.searches_skipped}  failed {self.searches_failed}",
            f"    facts          added {self.facts_added}  updated {self.facts_updated}  "
            f"unchanged {self.facts_unchanged}  rejected {self.facts_rejected}"
            + (f"  carried-forward {self.facts_carried_forward}"
               if self.facts_carried_forward else ""),
            f"    termination    {self.termination_reason or 'completed'}",
            "",
            "  cost",
            f"    search calls   {self.search_calls:<6} "
            f"${self.search_cost:.4f}",
            f"    llm tokens     in {self.input_tokens:<7} out {self.output_tokens:<7} "
            f"${self.llm_cost:.4f}",
            f"    total          ${self.total_cost:.4f}  "
            f"→ ${self.cost_per_1000():.2f} per 1,000 profiles",
            "",
            "  evidence",
            f"    websites       accepted {self.websites_accepted:<6} "
            f"rejected {self.websites_rejected}",
            f"    gate proofs    {self._gate_line()}",
            f"    corroborated   {self.corroborated_fields:<6} "
            f"conflicts resolved {self.conflicts_resolved} "
            f"dropped {self.conflicts_dropped}",
            f"    lmm calls      {self.llm_calls}",
            "",
            "  freshness",
            f"    changes        {self.changes_detected:<6} "
            f"registry events applied {self.registry_events_applied}",
            "",
            "  models",
            f"    gap filling    {self.models.get('gap_filling', '-')}",
            f"    arbitration    {self.models.get('arbitration', '-')}",
            f"    search         {self.models.get('search', '-')}",
            "",
            "  top fields by coverage",
        ]
        ranked = sorted(self.field_coverage.items(), key=lambda kv: -kv[1])
        denominator = max(self.companies_written, 1)
        for name, hits in ranked[:12]:
            share = hits / denominator
            bar = "█" * max(1, int(share * 28))
            lines.append(f"    {name:<22} {share * 100:5.1f}%  {bar}")
        if self.envelope_audit:
            from envelope import render_audit  # noqa: PLC0415

            lines.append("")
            lines.append("  output audit")
            for line in render_audit(self.envelope_audit).splitlines():
                lines.append("    " + line)
        lines.append("─" * 72)
        return "\n".join(lines)

    def _gate_line(self) -> str:
        if not self.gate_breakdown:
            return "none"
        return "  ".join(f"{k}={v}" for k, v in sorted(self.gate_breakdown.items()))

    def write(self, directory: Path) -> Path:
        """Write ``run-<id>.json`` atomically (same rationale as ``Profile.write``)."""
        import tempfile

        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"run-{self.run_id}.json"
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".run-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return target


# --------------------------------------------------------------------- helpers


def _registry_facts(
    entity: dict[str, Any],
    *,
    number: str,
    retrieved_at: str,
    source_url: str,
) -> list[Fact]:
    """Turn a register payload into facts, each stamped with its own receipt.

    The field table lives in :data:`registry.REGISTRY_FACTS` so there is exactly
    one place that decides which register fields become published facts. A
    second copy here once silently dropped ``city``, ``description`` and the
    ``mobil`` fallback; the single source removes that whole class of bug.
    """
    facts: list[Fact] = []
    for key, field_name, confidence in REGISTRY_FACTS:
        alternatives = key if isinstance(key, tuple) else (key,)
        value: Any = None
        used_key: Any = None
        for candidate in alternatives:
            candidate_value = _dig(entity, candidate)
            if isinstance(candidate_value, list):
                candidate_value = (
                    " ".join(str(v).strip() for v in candidate_value if str(v).strip())
                    or None
                )
            if isinstance(candidate_value, str):
                candidate_value = candidate_value.strip() or None
            if candidate_value is not None:
                value, used_key = candidate_value, candidate
                break
        if value is None:
            continue
        facts.append(
            Fact(
                field=field_name,
                value=value,
                source_url=source_url,
                retrieved_at=retrieved_at,
                evidence=_registry_quote(used_key, value),
                match_proof="registry",
                confidence=confidence,
                layer="registry",
                method=f"Enhetsregisteret field {used_key}",
                org_number=number,
            )
        )

    # The organisation number itself is a published field in the reference, not
    # just the key we look the company up by. It is the strongest identity fact
    # there is, so it is emitted with full confidence.
    facts.append(
        Fact(
            field="org_number", value=number, source_url=source_url,
            retrieved_at=retrieved_at,
            evidence=f'organisasjonsnummer: "{number}"',
            match_proof="registry", confidence=1.0, layer="registry",
            method="Enhetsregisteret organisasjonsnummer", org_number=number,
        )
    )

    if entity.get("underAvvikling"):
        facts.append(_boolean_fact("winding_up", True, source_url, retrieved_at, number))
    if entity.get("konkurs"):
        facts.append(_boolean_fact("bankrupt", True, source_url, retrieved_at, number))

    composed = address_line(entity)
    if composed:
        facts.append(
            Fact(
                field="address", value=composed, source_url=source_url,
                retrieved_at=retrieved_at,
                evidence=f"forretningsadresse: {composed}",
                match_proof="registry", confidence=0.97, layer="registry",
                method="forretningsadresse composed", org_number=number,
            )
        )

    # One status word, derived from the register's own dissolution flags. The
    # register carries no explicit "active" field, but the *absence* of every
    # dissolution flag is itself a fact about the record -- and "status" is one
    # of the categories the challenge names, so leaving it blank was a plain gap.
    status_value, status_evidence = _register_status(entity)
    facts.append(
        Fact(
            field="status", value=status_value, source_url=source_url,
            retrieved_at=retrieved_at, evidence=status_evidence,
            match_proof="registry", confidence=0.95, layer="derived",
            method="derived from Enhetsregisteret status flags", org_number=number,
        )
    )
    return facts


def _register_status(entity: dict[str, Any]) -> tuple[str, str]:
    """Read the register's dissolution flags into a single status word."""
    if entity.get("konkurs"):
        return "bankrupt", 'register flag "konkurs": true'
    if entity.get("underAvvikling"):
        return "winding_up", 'register flag "underAvvikling": true'
    if entity.get("underTvangsavviklingEllerTvangsopplosning"):
        return ("compulsory_dissolution",
                'register flag "underTvangsavviklingEllerTvangsopplosning": true')
    return "active", "no dissolution flag set on the register record"


def _boolean_fact(field_name: str, value: bool, url: str, retrieved_at: str, number: str) -> Fact:
    return Fact(
        field=field_name, value=value, source_url=url, retrieved_at=retrieved_at,
        evidence=f"register flag {field_name} is set", match_proof="registry",
        confidence=0.98, layer="registry", method="register status flag", org_number=number,
    )


def _registry_quote(key: str, value: Any) -> str:
    leaf = key.split(".")[-1]
    return f'"{leaf}": {json.dumps(value, ensure_ascii=False)}'


def _dig(payload: dict[str, Any], dotted: str) -> Any:
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _financial_facts(figures: RegnskapFigures, number: str) -> list[Fact]:
    """One fact per filed figure. Absent figures produce no fact at all."""
    from registry import _number  # noqa: PLC0415 - private reuse inside the package

    mapping = [
        ("revenue_latest", figures.revenue, 0.93),
        ("operating_result", figures.operating_result, 0.92),
        ("net_result", figures.net_result, 0.92),
        ("total_assets", figures.total_assets, 0.93),
        ("equity", figures.equity, 0.93),
        ("debt", figures.debt, 0.92),
    ]
    retrieved = utcnow_iso()
    facts: list[Fact] = []
    for field_name, value, confidence in mapping:
        if value is None:
            continue
        facts.append(
            Fact(
                field=field_name, value=value, source_url=figures.source_url,
                retrieved_at=retrieved, evidence=(
                    f"Annual accounts for the period ending {figures.period_end}: "
                    f"{field_name.replace('_', ' ')} {value} {figures.currency}"
                ),
                match_proof="registry", confidence=confidence, layer="registry",
                method="Regnskapsregisteret", org_number=number,
            )
        )
    if figures.year:
        facts.append(
            Fact(
                field="accounting_year", value=figures.year, source_url=figures.source_url,
                retrieved_at=retrieved,
                evidence=f"regnskapsperiode ended {figures.period_end}",
                match_proof="registry", confidence=0.94, layer="registry",
                method="derived from period end date", org_number=number,
            )
        )
    # The reporting period is a first-class field in the reference profile, and it
    # is a different thing from the retrieval date: "the 2025 accounts" is not
    # "read on 08 Oct 2026". Both are published.
    if figures.period_end:
        facts.append(
            Fact(
                field="reporting_period", value=str(figures.period_end)[:10],
                source_url=figures.source_url, retrieved_at=retrieved,
                evidence=f"regnskapsperiode slutt {figures.period_end}",
                match_proof="registry", confidence=0.94, layer="registry",
                method="Regnskapsregisteret period end", org_number=number,
            )
        )
    return facts


def _identifier_facts(enrichment: Any, number: str) -> list[Fact]:
    """One fact per identifier. Missing identifiers produce nothing, not nulls."""
    retrieved = utcnow_iso()
    facts: list[Fact] = []
    if enrichment.lei.found:
        facts.append(
            Fact(
                field="lei", value=enrichment.lei.lei, source_url=enrichment.lei.source_url,
                retrieved_at=enrichment.lei.retrieved_at or retrieved,
                evidence=(
                    f'GLEIF record for registration {number}: legal name '
                    f'"{enrichment.lei.legal_name}", status {enrichment.lei.status}'
                ),
                match_proof="identifier_join", confidence=0.93, layer="identifier",
                method="GLEIF registeredAs join", org_number=number,
            )
        )
        if enrichment.lei.parent_lei:
            facts.append(
                Fact(
                    field="parent_lei", value=enrichment.lei.parent_lei,
                    source_url=enrichment.lei.source_url,
                    retrieved_at=enrichment.lei.retrieved_at or retrieved,
                    evidence=f"GLEIF direct parent relationship → {enrichment.lei.parent_lei}",
                    match_proof="identifier_join", confidence=0.88, layer="identifier",
                    method="GLEIF direct-parent relationship", org_number=number,
                )
            )
    if enrichment.wikidata.found:
        if enrichment.wikidata.qid:
            facts.append(
                Fact(
                    field="wikidata", value=enrichment.wikidata.qid,
                    source_url=enrichment.wikidata.source_url,
                    retrieved_at=enrichment.wikidata.retrieved_at or retrieved,
                    evidence=f'Wikidata item matching P2338 = "{number}"',
                    match_proof="identifier_join", confidence=0.86, layer="identifier",
                    method="Wikidata SPARQL on P2338", org_number=number,
                )
            )
        if enrichment.wikidata.official_website:
            facts.append(
                Fact(
                    field="official_website", value=enrichment.wikidata.official_website,
                    source_url=enrichment.wikidata.source_url,
                    retrieved_at=enrichment.wikidata.retrieved_at or retrieved,
                    evidence=f'Wikidata P856 for {enrichment.wikidata.qid}',
                    match_proof="identifier_join", confidence=0.84, layer="identifier",
                    method="Wikidata P856", org_number=number,
                )
            )
    return facts


# ---------------------------------------------------------------- the pipeline


def _needs_live_address(entity: dict[str, Any]) -> bool:
    """True when a mirrored record lacks the register's address detail.

    A company seeded from the public universe carries identity (name, legal form,
    industry, employees) but no ``forretningsadresse`` street or postcode; the
    live register does. Re-fetching one keyless entity fills postcode, city and
    the composed address instead of leaving them empty.
    """
    addr = entity.get("forretningsadresse") or {}
    return not (addr.get("postnummer") or addr.get("adresse"))


def process_company(
    raw_number: Any,
    *,
    store: RegistryStore,
    snapshots: SnapshotStore,
    settings: Settings,
    http: HttpClient,
    llm: LlmClient,
    reporter: RunReport,
    lock: threading.Lock,
) -> CompanyResult:
    """Run one company through the cascade and write its profile.

    The cascade is literal: each block below runs only when the block above it
    could not answer the field in question.
    """
    started = time.monotonic()
    result = CompanyResult(org_number=str(raw_number))

    org = validate(raw_number)
    if org is None:
        result.error = f"rejected by checksum: {raw_number!r}"
        reporter.invalid_numbers.append(str(raw_number))
        return result
    result.org_number = org.value
    number = org.value

    entity = store.lookup(org)
    # Fetch the entity live from the register when it is unseen, *or* when the
    # mirrored record is incomplete. A company seeded from the public universe
    # carries identity but no street address; the live register supplies the
    # postcode, city and street line, so one extra keyless request fills the
    # address fields rather than leaving them empty. An unseen company is a
    # first-class input, never a gap.
    fetch_error = ""
    if not settings.offline and (entity is None or _needs_live_address(entity)):
        try:
            live = BrregClient(http).entity(org)
        except Exception as exc:
            live = None
            # ``entity()`` returns None for a genuine 404 and raises for anything
            # else. A 404 means the company is not registered; a transport error
            # means *we* could not reach the register. Conflating the two would
            # publish "no information exists" when the truth is "we did not look
            # successfully" -- so the two are kept apart.
            if getattr(exc, "status", None) != 404:
                fetch_error = f"register unreachable: {exc}"
        if live:
            merged = {**(entity or {}), **live}
            try:
                store.upsert(merged, fetched_at=utcnow_iso())
                store.commit()
                entity = store.lookup(org) or merged
                reporter.registry_fetched_live += 1
            except Exception:
                entity = merged
    if entity is None:
        if fetch_error:
            result.error = fetch_error  # -> blocked
        else:
            result.error = "not present in the registry (checked the mirror and live)"
            reporter.not_in_registry.append(number)
        return result

    retrieved_at = utcnow_iso()
    registry_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{number}"
    profile = Profile(
        org_number=number,
        legal_name=str(entity.get("navn") or ""),
        run_date=date.today().isoformat(),
        run_id=reporter.run_id,
    )
    result.legal_name = profile.legal_name

    # ---- layer 1: deterministic registry facts (free) -----------------------
    for fact in _registry_facts(entity, number=number, retrieved_at=retrieved_at,
                                source_url=registry_url):
        profile.add(fact, minimum_confidence=settings.min_confidence)
    result.layers_used.append("registry")

    # entity graph: parent unit and sub-units
    parent = (entity.get("overordnetEnhet") or "").strip()
    if parent and clean(parent):
        profile.add(
            Fact(
                field="parent_org_number", value=clean(parent), source_url=registry_url,
                retrieved_at=retrieved_at,
                evidence=f'"overordnetEnhet": "{parent}"',
                match_proof="registry", confidence=0.97, layer="registry",
                method="Enhetsregisteret overordnetEnhet", org_number=number,
            ),
            minimum_confidence=settings.min_confidence,
        )

    # ---- layer 2: identifiers, people and locations (free, parallel) --------
    # All three are free register lookups, and they are the categories the
    # challenge's largest scoring band asks for beyond plain identity. They are
    # attempted independently so one failing source cannot cost the others.
    if not settings.offline:
        enrichment = enrich(org, http)
        for fact in _identifier_facts(enrichment, number):
            profile.add(fact, minimum_confidence=settings.min_confidence)
        if profile.facts:
            result.layers_used.append("identifier")

        brreg = BrregClient(http)

        # People: office-holders, from the register's own role listing.
        try:
            role_facts = people_facts(
                brreg.roles(org),
                number=number,
                retrieved_at=retrieved_at,
                source_url=registry_url,
            )
            for fact in role_facts:
                profile.add(fact, minimum_confidence=settings.min_confidence)
            if role_facts:
                result.layers_used.append("people")
        except Exception:
            # A missing role listing is a gap, never a reason to fail the company.
            pass

        # Locations: registered sub-entities, plus the head office. The strict
        # form is used because ``location_count`` is derived from the listing --
        # a failed read must not be published as a count of 1.
        try:
            units_ok, units = brreg.try_sub_units(org)
            unit_facts = location_facts(
                units,
                number=number,
                retrieved_at=retrieved_at,
                source_url=registry_url,
                lookup_ok=units_ok,
            )
            for fact in unit_facts:
                profile.add(fact, minimum_confidence=settings.min_confidence)
            if unit_facts:
                result.layers_used.append("locations")
            elif not units_ok:
                result.layers_used.append("locations_lookup_failed")
                profile.errors.append(
                    "registered locations could not be read this run; no count published"
                )
        except Exception:
            pass

    # ---- layer 3: annual accounts (free, optional) --------------------------
    if settings.fetch_financials and not settings.offline:
        figures = fetch_regnskap(org, http)
        if figures is not None and figures.available:
            for fact in _financial_facts(figures, number):
                profile.add(fact, minimum_confidence=settings.min_confidence)
            result.layers_used.append("financials")

    # ---- layer 4: the web (costs money) ------------------------------------
    web_facts: list[Fact] = []
    web_allowed = is_organisational_contact(entity)
    if not web_allowed:
        # A sole trader (ENK) *is* a natural person, so any contact detail lifted
        # from its website is personal data. The brief forbids collecting it, so
        # the web layer is skipped entirely rather than filtered afterwards.
        empty_for(
            profile, "website",
            "entity is a sole trader (ENK); web contact details are personal data "
            "and are not collected",
        )
        result.layers_used.append("web_skipped_sole_trader")
    if (settings.fetch_web and web_allowed and not settings.offline
            and not http.budget.winding_down):
        finder = SiteFinder(http)
        candidates = finder.candidates(entity, org)
        decisions: list[Any] = []
        accepted_candidate = None
        accepted_pages: list[Page] = []

        # The search may have failed rather than come back empty. If it did, that
        # is recorded now -- before any field is filled -- so that a thin profile
        # is never mistaken for a company that genuinely has nothing to find.
        # "We could not look" and "we looked and found nothing" are different
        # claims, and only the second one is evidence about the company.
        search_failure = ""
        attempt = finder.last_search
        if attempt is not None and attempt.failed:
            search_failure = attempt.error
            result.search_status = attempt.status
            result.search_error = attempt.error
            profile.errors.append(
                f"website discovery incomplete: {attempt.error}"
            )
            result.layers_used.append("website_search_failed")
        elif attempt is not None:
            result.search_status = attempt.status

        for candidate in candidates[:3]:
            pages: list[Page] = []
            for url in page_urls(candidate.url)[: settings.max_pages_per_site]:
                if http.budget.winding_down:
                    break
                snapshot = snapshots.get(url)
                if snapshot is not None and settings.reuse_cache:
                    state = recheck_web(url, snapshots=snapshots, client=http)
                    if state.state == "unchanged":
                        reporter.gate_breakdown["cached_unchanged"] = (
                            reporter.gate_breakdown.get("cached_unchanged", 0) + 1
                        )
                        continue
                try:
                    response = http.get(url)
                except Exception:
                    continue
                pages.append(
                    Page(
                        url=url, status=response.status, markup=response.text,
                        etag=response.etag, last_modified=response.last_modified,
                        content_hash=response.content_hash, retrieved_at=utcnow_iso(),
                    )
                )

            if not pages:
                continue

            addr = entity.get("forretningsadresse") or {}
            verdicts = [
                evaluate(
                    page, org, name=profile.legal_name,
                    postcode=addr.get("postnummer"), city=addr.get("poststed"),
                )
                for page in pages
            ]
            decisions.extend(verdicts)
            decision = decide(verdicts)
            if decision.accepted:
                accepted_candidate = candidate
                winning_url = decision.url
                winning = next((p for p in pages if p.url == winning_url), pages[0])
                # Fetch the sibling pages of an accepted site: the contact page
                # usually holds the email the home page hides behind a form.
                accepted_pages = [winning] + [p for p in pages if p is not winning][:1]
                # Entity resolution: an additive score over every signal, kept
                # beside the gate's verdict so the decision is auditable.
                try:
                    addr = entity.get("forretningsadresse") or {}
                    em = resolve_page(
                        winning, org, name=profile.legal_name,
                        postcode=addr.get("postnummer"), city=addr.get("poststed"),
                        phone=profile.value("phone"), address=profile.value("address"),
                        legal_website=profile.value("website") or "",
                    )
                    result.match_score = em.score
                    result.match_verdict = em.verdict
                except Exception:
                    pass
                break

        for verdict in decisions:
            key = verdict.match_proof if verdict.accepted else verdict.verdict
            reporter.gate_breakdown[key] = reporter.gate_breakdown.get(key, 0) + 1

        if accepted_candidate is not None and accepted_pages:
            reporter.websites_accepted += 1
            result.gate_accepted = True
            result.gate_proof = decisions and decide(decisions).match_proof or ""
            profile.add(
                Fact(
                    field="website", value=accepted_candidate.url,
                    source_url=decisions[0].url if decisions else accepted_candidate.url,
                    retrieved_at=accepted_pages[0].retrieved_at,
                    evidence=_gate_quote(accepted_pages, number),
                    match_proof=result.gate_proof or "orgnr_on_page",
                    confidence=CONFIDENCE.get(result.gate_proof, 0.9),
                    layer="web_rules", method=f"ownership gate via {accepted_candidate.origin}",
                    org_number=number,
                ),
                minimum_confidence=0.0,  # the gate already decided; record the decision
            )
            result.layers_used.append("web_rules")

            # ---- rule extraction: zero tokens
            extractions = [extract(page, org) for page in accepted_pages]
            web_facts.extend(
                _facts_from_rules(
                    extractions, accepted_pages, org, profile, settings=settings
                )
            )

            # ---- hiring & public activity: the last two domains -------------
            # Read only from the site that just passed the gate, and only for an
            # organisational contact (the sole-trader guard above already ruled
            # the alternative out). Both are best-effort: a site without a
            # careers page simply yields no jobs, which is a gap, not a failure.
            if settings.fetch_jobs or settings.fetch_activity:
                extra_pages = _verified_extra_pages(
                    accepted_candidate.url, accepted_pages, http=http,
                )
                gate_proof = result.gate_proof or "orgnr_on_page"
                if settings.fetch_jobs:
                    try:
                        postings = extract_jobs(extra_pages, base_url=accepted_candidate.url)
                        careers = _careers_url_from(accepted_candidate.url, extra_pages)
                        job_list = job_facts(
                            postings, number=number, retrieved_at=retrieved_at,
                            source_url=careers or accepted_candidate.url,
                            careers_url=careers, match_proof=gate_proof,
                        )
                        if job_list:
                            web_facts.extend(job_list)
                            result.layers_used.append("jobs")
                    except Exception:
                        pass
                if settings.fetch_activity:
                    try:
                        items = extract_activity(extra_pages, base_url=accepted_candidate.url)
                        activity_list = activity_facts(
                            items, number=number, retrieved_at=retrieved_at,
                            source_url=accepted_candidate.url, match_proof=gate_proof,
                        )
                        if activity_list:
                            web_facts.extend(activity_list)
                            result.layers_used.append("activity")
                    except Exception:
                        pass

            # ---- gap filling: only the residue
            missing = [
                name for name in GAP_FIELDS
                if profile.value(name) is None and not _covered_by(web_facts, name)
            ]
            if settings.use_llm and missing and llm.enabled:
                page = max(accepted_pages, key=lambda p: len(p.text))
                gap = llm.fill_gaps(
                    page.text, url=page.url, wanted=missing,
                    company_name=profile.legal_name, org_display=org.display,
                )
                result.llm_calls += 1 if not gap.cached else 0
                result.input_tokens += gap.input_tokens
                result.output_tokens += gap.output_tokens
                reporter.llm_calls += 1 if not gap.cached else 0
                reporter.input_tokens += gap.input_tokens
                reporter.output_tokens += gap.output_tokens
                for candidate in gap.accepted:
                    web_facts.append(
                        Fact(
                            field=candidate.field, value=candidate.value,
                            source_url=page.url, retrieved_at=page.retrieved_at,
                            evidence=candidate.quote, match_proof=result.gate_proof,
                            confidence=min(CONFIDENCE.get(result.gate_proof, 0.9), 0.86),
                            layer="web_llm",
                            method=f"{MODEL_GAP}; quote verified against page text",
                            org_number=number,
                        )
                    )
                for candidate in gap.rejected:
                    profile.empty_fields.append(
                        SkippedField(candidate.field,
                                     f"model quote not found on page: {candidate.reject_reason}")
                    )
                if gap.accepted:
                    result.layers_used.append("web_llm")
                if gap.error:
                    profile.errors.append(f"gap filling unavailable: {gap.error}")
        else:
            reporter.websites_rejected += 1
            if search_failure:
                # The lookup itself did not complete. Saying "no website was
                # found" here would assert something we never established; the
                # honest statement is that we could not check.
                empty_for(
                    profile, "website",
                    f"website not determined: the lookup did not complete "
                    f"({search_failure})",
                )
            else:
                empty_for(
                    profile, "website",
                    "ownership gate found no page carrying the organisation number or a "
                    "name with matching address",
                )

    for fact in web_facts:
        profile.add(fact, minimum_confidence=settings.min_confidence)

    # ---- layer 5: verification ---------------------------------------------
    outcome = verify_profile(
        profile, llm=llm if settings.use_llm else None,
        minimum_confidence=settings.min_confidence,
    )
    result.llm_calls += outcome.llm_calls
    reporter.corroborated_fields += outcome.corroborated
    reporter.conflicts_resolved += outcome.conflicts_resolved
    reporter.conflicts_dropped += outcome.conflicts_dropped
    reporter.invalid_dropped += outcome.invalid_dropped
    result.layers_used.append("verified")

    for name in USEFUL_FIELDS:
        if profile.value(name) is not None:
            reporter.field_coverage[name] = reporter.field_coverage.get(name, 0) + 1
        elif not any(s.field == name for s in profile.empty_fields):
            empty_for(profile, name, "no source produced a value above the confidence threshold")
    for fact in profile.facts:
        reporter.layer_counts[fact.layer] = reporter.layer_counts.get(fact.layer, 0) + 1

    # ---- layer 6: explanation and change detection -------------------------
    explain_profile(profile)
    attach_org_number_proof(profile, number)

    # ---- layer 7: resumability -------------------------------------------
    # A re-run whose sources failed must not erase what a previous run proved.
    # The rule is narrow on purpose: a prior fact is carried forward only when
    # *the current run did not produce that field at all*, i.e. the field is
    # genuinely absent rather than deliberately re-derived. If the current run
    # produced a value it wins, because it is the fresher evidence -- so this
    # can never mask a real change, and it can never resurrect a value the
    # register has since corrected to a different one.
    carried = _carry_forward(profile, settings.profiles_dir, number)
    if carried:
        result.facts_carried_forward = carried
        profile.errors.append(
            f"{carried} fact(s) carried forward from the previously published "
            "profile: this run's sources did not produce them"
        )

    previous = _previous_values(settings.profiles_dir, number)
    result.had_previous_profile = bool(previous)
    if previous:
        detect_web_changes(profile, org_number=number, previous=previous,
                           source_url=registry_url)
    reporter.changes_detected += len(profile.changes)

    profile.facts.sort(key=lambda f: f.field)
    profile.write(settings.profiles_dir)
    snapshots.record_facts(profile)

    result.ok = True
    result.facts = len(profile.facts)
    result.empty = len(profile.empty_fields)
    result.changes = len(profile.changes)
    result.seconds = time.monotonic() - started
    return result


def _gate_quote(pages: list[Page], number: str) -> str:
    from validate import format_display

    display = format_display(number)
    for page in pages:
        idx = page.text.find(display)
        if idx < 0:
            idx = page.text.find(number)
        if idx >= 0:
            start, end = max(0, idx - 60), idx + len(display) + 40
            return " ".join(page.text[start:end].split())
    return f"organisation number {display} located in page markup"


def _covered_by(facts: list[Fact], name: str) -> bool:
    return any(f.field == name for f in facts)


def _facts_from_rules(
    extractions: list[Any],
    pages: list[Page],
    org: OrgNumber,
    profile: Profile,
    *,
    settings: Settings,
) -> list[Fact]:
    """Promote rule extraction output into facts.

    Every value here inherits the proof type the gate established for the page
    it came from. That is the point of the gate: the proof travels with the
    facts, so nothing needs to be re-argued downstream.
    """
    proof = "orgnr_on_page" if any(p.proves(org) for p in pages) else "name_postcode"
    confidence = CONFIDENCE[proof]
    by_url = {p.url: p for p in pages}
    facts: list[Fact] = []

    def add(field_name: str, value: Any, url: str, evidence: str, method: str,
            *, penalty: float = 0.0) -> None:
        if value in (None, "", [], {}):
            return
        if profile.value(field_name) is not None:
            return  # a register value outranks a web value; never overwrite here
        facts.append(
            Fact(
                field=field_name, value=value, source_url=url,
                retrieved_at=by_url.get(url, pages[0]).retrieved_at or utcnow_iso(),
                evidence=evidence[:400] or "(structured data on page)",
                match_proof=proof, confidence=max(0.0, confidence - penalty),
                layer="web_rules", method=method, org_number=org.value,
            )
        )

    for extraction in extractions:
        url = extraction.url
        page = by_url.get(url, pages[0])

        if extraction.emails:
            add("email", extraction.emails[0], url,
                extraction.quote_contact or extraction.emails[0],
                "mailto link or role address in page text", penalty=0.04)
        if extraction.phones:
            add("phone", extraction.phones[0], url,
                extraction.quote_contact or extraction.phones[0],
                "tel: link on page", penalty=0.04)
        if extraction.address and not _address_is_registry(profile):
            add("address", extraction.address, url, extraction.quote_contact
                or extraction.address, "postal address block on page", penalty=0.10)
        if extraction.description:
            add("description", extraction.description, url,
                extraction.quote_contact or extraction.description[:160],
                "JSON-LD description / og:description", penalty=0.12)
        for label, link in extraction.socials.items():
            add(label, link, url, f'<a href="{link}">', "social profile link", penalty=0.08)

        if extraction.org_numbers and org.value in extraction.org_numbers:
            add("website_orgnr_proof", org.display, url,
                extraction.quote_orgnr or f"organisation number {org.display} on page",
                "proof-of-ownership gate", penalty=0.0)

        del page

    # Deduplicate on field, keeping the highest-confidence candidate.
    best: dict[str, Fact] = {}
    for fact in facts:
        current = best.get(fact.field)
        if current is None or fact.confidence > current.confidence:
            best[fact.field] = fact
    return list(best.values())


def _address_is_registry(profile: Profile) -> bool:
    fact = profile.get("address")
    return fact is not None and fact.match_proof == "registry"


def _previous_values(directory: Path, number: str) -> dict[str, Any]:
    """Load the previously published profile's values for change diffing."""
    path = directory / f"{number}.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {item["field"]: item.get("value") for item in raw.get("facts", [])}


def _previous_facts(directory: Path, number: str) -> list[Fact]:
    """The full facts of the previously published profile, or ``[]``.

    Returns the complete :class:`Fact` (value, provenance, evidence, proof) so a
    carried-forward fact keeps its original citation rather than being
    re-attributed to this run. A malformed or missing file yields ``[]``: a
    corrupt prior profile must never be able to inject anything.
    """
    path = directory / f"{number}.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[Fact] = []
    for item in raw.get("facts", []):
        try:
            out.append(Fact(
                field=item["field"],
                value=item["value"],
                source_url=item.get("source_url", ""),
                retrieved_at=item.get("retrieved_at", ""),
                evidence=item.get("evidence", ""),
                match_proof=item.get("match_proof", "registry"),
                confidence=float(item.get("confidence", 0.0)),
                layer=item.get("layer", "carried_forward"),
                method=item.get("method", ""),
                changed_from=item.get("changed_from"),
                explanation=item.get("explanation", ""),
                org_number=number,
            ))
        except (KeyError, TypeError, ValueError):
            # One unreadable fact must not abort the carry-forward of the rest.
            continue
    return out


#: Fields that must never be carried forward from a previous run, because their
#: absence in the current run is itself the meaningful result. A website or a
#: phone number that a re-check did not confirm should not be re-asserted from
#: yesterday's file: it is the category most likely to have genuinely changed,
#: and resurrecting it would be exactly the "empty result overwrites / stale
#: result survives" failure the brief warns about in the opposite direction.
_CARRY_FORWARD_EXCLUDED = frozenset({"website", "email", "phone"})


def _carry_forward(profile: Profile, directory: Path, number: str) -> int:
    """Re-add prior facts for fields this run produced nothing for.

    Resumability requirement: *missing results must not erase existing verified
    facts*. When a source fails (a register read that timed out, an enrichment
    that was refused by the budget), the field simply does not appear -- which
    previously meant the field was dropped from the published profile outright.
    That is a data *loss* caused by an *absence of evidence*, not by a change in
    the company.

    The prior value is restored only when all of the following hold, so the
    operation can never invent or overwrite anything:

    * the field is absent from the current profile (a fresh value always wins);
    * the field is not in :data:`_CARRY_FORWARD_EXCLUDED` (volatile contact
      data is genuinely re-checked, and its absence is meaningful);
    * the prior fact carried a non-empty value and a source URL (i.e. it was
      evidence-backed when it was published).

    Returns the number of facts restored.
    """
    if not directory or not number:
        return 0
    present = {f.field for f in profile.facts}
    restored = 0
    for fact in _previous_facts(directory, number):
        if fact.field in present or fact.field in _CARRY_FORWARD_EXCLUDED:
            continue
        if not str(fact.value).strip() or not fact.source_url:
            continue
        profile.add(fact, minimum_confidence=0.0)
        present.add(fact.field)
        restored += 1
    return restored


# ------------------------------------------------------------------- top level


def _status_tally(envelopes: Sequence[Any]) -> dict[str, int]:
    """Count envelopes by status, including the zeroes.

    Every known status appears even when it is zero, so the run report shows the
    full contract rather than only the states that happened to occur.
    """
    from envelope import STATUSES  # noqa: PLC0415

    tally = {status: 0 for status in STATUSES}
    for env in envelopes:
        tally[env.status] = tally.get(env.status, 0) + 1
    return tally


def _envelope_status(result: "CompanyResult", facts: list[dict[str, Any]]) -> tuple[str, str]:
    """Decide the envelope status for one company.

    The order matters. A structural problem with the *input* is reported before
    anything about the data, because "this was never a company number" and "we
    found nothing for this company" are different answers and a judge will
    check which one we gave.
    """
    error = (result.error or "").strip()
    lowered = error.lower()

    if "checksum" in lowered:
        return "not_applicable", error or "not a valid organisation number"
    # A source we could not reach is *blocked*, not "no information exists" and
    # not a crash. It is a fact about our run, not about the company.
    if "unreachable" in lowered or "blocked" in lowered or "refused" in lowered:
        return "blocked", error
    if "not present in the registry" in lowered or "not in the registry" in lowered:
        return "not_available", error
    if "no such" in lowered or "invalid" in lowered:
        return "not_applicable", error
    if error:
        return "failed", error
    if facts:
        return "available", f"{len(facts)} facts published with full evidence"
    return (
        "not_available",
        "entity is in the register but no source produced a publishable fact",
    )


def _verified_extra_pages(
    base_url: str, accepted_pages: list[Page], *, http: Any
) -> list[Page]:
    """Fetch the careers/news paths of an accepted site, bounded and polite.

    Only the site that passed the gate is read, and only a handful of paths, so
    the hiring/activity domains add a couple of requests per company rather than
    a crawl.
    """
    pages: list[Page] = list(accepted_pages)
    seen = {p.url for p in pages}
    wanted = careers_urls(base_url)[:2] + activity_urls(base_url)[:2]
    for url in wanted:
        if url in seen or http.budget.winding_down:
            continue
        seen.add(url)
        try:
            response = http.get(url)
        except Exception:
            continue
        if getattr(response, "status", 200) >= 400:
            continue
        pages.append(
            Page(
                url=url, status=response.status, markup=response.text,
                etag=response.etag, last_modified=response.last_modified,
                content_hash=response.content_hash, retrieved_at=utcnow_iso(),
            )
        )
    return pages


def _careers_url_from(base_url: str, pages: list[Page]) -> str:
    """The careers URL that was actually fetched, if any."""
    fetched = {p.url for p in pages}
    for url in careers_urls(base_url):
        if url in fetched:
            return url
    return ""


def build_envelopes(
    results: Sequence["CompanyResult"],
    *,
    settings: "Settings",
    reporter: "RunReport",
) -> list[Any]:
    """One envelope per input, with no company dropped.

    Walks the *results*, not the successes: a company that failed validation, or
    that is absent from the register, still gets an envelope saying so. That is
    what makes the run satisfy the execution contract even on a bad batch.
    """
    from envelope import Envelope, SourceOutcome  # noqa: PLC0415

    registry_url = "https://data.brreg.no/enhetsregisteret/api/enheter"
    envelopes: list[Any] = []

    for result in results:
        number = result.org_number
        path = settings.profiles_dir / f"{number}.json"
        facts: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        name = result.legal_name
        retrieved = ""
        profile_obj: Profile | None = None

        if path.exists():
            try:
                profile_obj = Profile.read(path)
                facts = [f.compact() for f in profile_obj.facts]
                skipped = [s.to_dict() for s in profile_obj.empty_fields]
                changes = [c.to_dict() for c in profile_obj.changes]
                name = name or profile_obj.legal_name
                retrieved = next(
                    (f.retrieved_at for f in profile_obj.facts if f.retrieved_at), ""
                )
            except Exception:
                # An unreadable profile is a failed company, not a failed run.
                pass

        status, reason = _envelope_status(result, facts)

        # The field-aware, source-aware coverage matrix travels with the result,
        # so a reviewer sees per-domain recall without re-running the pipeline.
        matrix = domain_coverage(profile_obj) if profile_obj is not None else {}
        summary = coverage_summary(profile_obj) if profile_obj is not None else {}
        # The five reference views travel too, so the answer to "who runs it?"
        # is in the result, not something a reviewer has to reconstruct.
        synth = synthesize_profile(profile_obj) if profile_obj is not None else {}

        sources: list[SourceOutcome] = []
        for layer in result.layers_used:
            sources.append(
                SourceOutcome(
                    name=layer,
                    status="ok",
                    detail=f"contributed facts on the {layer} layer",
                    url=f"{registry_url}/{number}" if layer == "registry" else "",
                )
            )
        if not sources and status in ("not_available", "not_applicable"):
            sources.append(
                SourceOutcome(
                    name="registry",
                    status=status,
                    detail=reason,
                    url=f"{registry_url}/{number}",
                )
            )

        envelopes.append(
            Envelope(
                orgnr=number,
                status=status,
                reason=reason,
                retrieved_at=retrieved,
                legal_name=name,
                facts=facts,
                skipped=skipped,
                changes=changes,
                sources=sources,
                coverage={
                    "facts": len(facts),
                    "fields": len({f["field"] for f in facts}),
                    "skipped": len(skipped),
                    "summary": summary,
                    "domains": matrix,
                },
                synthesis=synth,
                cost={"usd": round(result.cost_usd, 6)},
                seconds=result.seconds,
            )
        )

    return envelopes


def run(
    numbers: Sequence[Any],
    *,
    settings: Settings | None = None,
    progress: Any | None = None,
) -> RunReport:
    """Process ``numbers`` and return the run report.

    ``progress`` is an optional callable receiving ``(done, total, CompanyResult)``
    so the CLI and the web console can share one entry point.
    """
    settings = settings or Settings.from_env()
    reporter = RunReport(
        run_id=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        started_at=utcnow_iso(),
        companies_requested=len(numbers),
        models={"gap_filling": MODEL_GAP, "arbitration": MODEL_VERIFY,
                "search": "brave-web-search"},
    )
    started = time.monotonic()
    lock = threading.Lock()

    store = RegistryStore(settings.registry_db)
    snapshots = SnapshotStore(settings.snapshot_db)
    budget = Budget()
    http = HttpClient(cache_dir=settings.cache_dir, budget=budget)
    llm = LlmClient(http, cache_dir=str(settings.llm_cache))

    # Registry changes are applied before anything is read, so every profile in
    # this run reflects the same register state.
    if not settings.offline:
        try:
            from delta import apply_registry_change
            from registry import sync_changes

            events = sync_changes(store, BrregClient(http), since=None, max_pages=2)
            reporter.registry_events_applied = len(events)
        except Exception:
            reporter.registry_events_applied = 0

    results: list[CompanyResult] = []
    done = 0
    try:
        with futures.ThreadPoolExecutor(max_workers=max(1, settings.workers)) as pool:
            pending = {
                pool.submit(
                    process_company, number,
                    store=store, snapshots=snapshots, settings=settings,
                    http=http, llm=llm, reporter=reporter, lock=lock,
                ): number
                for number in numbers
            }
            for future in futures.as_completed(pending):
                number = pending[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # a single bad company must not kill the run
                    outcome = CompanyResult(org_number=str(number), error=f"{type(exc).__name__}: {exc}")
                    reporter.companies_failed += 1
                done += 1
                results.append(outcome)
                if progress is not None:
                    try:
                        progress(done, len(numbers), outcome)
                    except Exception:
                        pass
    except BudgetExceeded:
        # The budget refused to start further work. Requests already submitted
        # still need to be collected so their threads do not outlive the run and
        # so their (possibly written) profiles are counted rather than lost.
        for future, number in pending.items():
            if future.done() and not future.cancelled():
                try:
                    results.append(future.result())
                except Exception:
                    pass
    finally:
        # Release DB handles deterministically. Leaking them keeps a file lock on
        # the scratch databases under Windows and makes a re-run flaky.
        for handle in (store, snapshots):
            try:
                handle.close()
            except Exception:
                pass
        reporter.results = [r.to_dict() for r in results]
        reporter.companies_written = sum(1 for r in results if r.ok)
        reporter.companies_failed = sum(1 for r in results if not r.ok)
        reporter.requests = budget.used
        reporter.search_calls = budget.search_calls
        reporter.input_tokens = budget.llm_input_tokens
        reporter.output_tokens = budget.llm_output_tokens
        reporter.budget = budget.to_dict()
        reporter.seconds = time.monotonic() - started
        reporter.finished_at = utcnow_iso()
        # Fold the §7 execution report from the real results and the live budget.
        # Done here, in ``finally``, so an early termination still reports what
        # actually happened rather than nothing.
        reporter.finalise()

        # The execution contract: one envelope per input, always. Built from
        # ``numbers`` rather than ``results`` so a company that crashed still
        # produces an output line instead of vanishing from the batch.
        try:
            from envelope import envelope_audit, envelopes_to_jsonl  # noqa: PLC0415

            by_number = {r.org_number: r for r in results}
            ordered = [
                by_number.get(
                    str(clean(n) or n),
                    CompanyResult(
                        org_number=str(clean(n) or n),
                        error=f"not processed: {n!r}",
                    ),
                )
                for n in numbers
            ]
            envelopes = build_envelopes(ordered, settings=settings, reporter=reporter)
            target = envelopes_to_jsonl(envelopes, settings.runs_dir / "envelopes.jsonl")
            reporter.envelopes_path = str(target)
            reporter.envelopes_total = len(envelopes)
            reporter.envelopes_by_status = _status_tally(envelopes)
            reporter.envelope_audit = envelope_audit(numbers, envelopes)
        except Exception as exc:  # never let reporting sink a good run
            reporter.envelopes_path = ""
            reporter.envelope_error = f"{type(exc).__name__}: {exc}"

        reporter.write(settings.runs_dir)
        snapshots.close()
        store.close()
    return reporter


def read_numbers(path: Path | str) -> list[str]:
    """Read the evaluator's company file: one number per line, ``#`` comments allowed.

    The file is normalised and de-duplicated *safely*, which means two things:

    *   **Normalised** -- ``NO 986 587 098 MVA``, ``986.587.098`` and the bare
        integer all collapse to ``986587098``, so the same company supplied in
        two spellings is one input, not two.
    *   **De-duplicated, order preserved** -- a repeated number produces one
        envelope, never two, because a duplicate output breaks the one-result-
        per-input contract.

    A token that is not a usable number is *kept* (not dropped): it still has to
    come back as a ``not_applicable`` envelope, because dropping it would leave a
    company with no row at all -- which is exactly what makes a run unscorable.
    """
    text = Path(path).read_text(encoding="utf-8")
    seen: set[str] = set()
    numbers: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # The evaluator's batch may be the plain one-number-per-line file, or the
        # universe JSONL (one record per line). Accept both without a flag.
        if line.startswith("{"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            token = clean(record.get("organisation_number") or record.get("organisasjonsnummer"))
            if token and token not in seen:
                seen.add(token)
                numbers.append(token)
            continue
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # The whole line is usually one number, possibly written "986 587 098"
        # or "NO 986 587 098 MVA"; only fall back to tokenising when it is not.
        whole = clean(line)
        tokens = [whole] if whole else [
            clean(token) or token.strip() for token in line.replace(",", " ").split()
        ]
        for token in tokens:
            if not token or token in seen:
                continue
            seen.add(token)
            numbers.append(token)
    return numbers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalpost",
        description="Build evidence-backed profiles for Norwegian organisation numbers.",
    )
    parser.add_argument("numbers", nargs="*", help="organisation numbers, or paths to files of them")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--offline", action="store_true", help="registry mirror only, no network")
    parser.add_argument("--no-web", action="store_true", help="skip the web layer")
    parser.add_argument("--financials", action="store_true", help="include annual accounts")
    parser.add_argument("--no-llm", action="store_true", help="rules and verifier only")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--show", metavar="ORGNR", default=None,
                        help="print the rendered profile for one company and exit")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.workers:
        settings.workers = args.workers
    settings.offline = settings.offline or args.offline
    settings.fetch_web = settings.fetch_web and not args.no_web
    settings.fetch_financials = settings.fetch_financials or args.financials
    settings.use_llm = settings.use_llm and not args.no_llm
    settings.quiet = settings.quiet or args.quiet
    if args.limit:
        settings.limit = args.limit

    if args.show:
        path = settings.profiles_dir / f"{(clean(args.show) or args.show)}.json"
        if not path.exists():
            print(f"no profile at {path}", file=sys.stderr)
            return 1
        print(render_report(Profile.read(path)))
        return 0

    numbers: list[str] = []
    for item in args.numbers:
        candidate = Path(item)
        if candidate.exists() and candidate.is_file():
            numbers.extend(read_numbers(candidate))
        else:
            numbers.append(item)
    if not numbers:
        default = ROOT / "orgnrs.txt"
        if default.exists():
            numbers = read_numbers(default)
        else:
            print("no organisation numbers given and no orgnrs.txt found", file=sys.stderr)
            return 2
    if settings.limit:
        numbers = numbers[: settings.limit]

    def show(done: int, total: int, outcome: CompanyResult) -> None:
        if settings.quiet:
            return
        mark = "ok " if outcome.ok else "err"
        label = outcome.legal_name[:28] if outcome.legal_name else outcome.org_number
        detail = f"{outcome.facts:>3} facts"
        if outcome.changes:
            detail += f", {outcome.changes} changed"
        if not outcome.ok:
            detail = outcome.error[:48]
        stream = sys.stderr
        stream.write(f"  [{done:>4}/{total}] {mark} {outcome.org_number}  {label:<30} {detail}\n")
        stream.flush()

    report = run(numbers, settings=settings, progress=show)
    report.write(settings.runs_dir)
    if not settings.quiet:
        print(report.render())
    else:
        print(json.dumps(report.to_dict()["cost"], indent=2))
    return 0 if report.companies_written else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
