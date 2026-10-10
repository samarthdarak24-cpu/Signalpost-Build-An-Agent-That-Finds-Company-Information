"""The research runner: execute the plan, one domain at a time, and report.

The planner (:mod:`research_plan`) decides *what* to do; this module *does* it
and records what happened. It is deliberately an orchestrator over injected
retrievers rather than a fetcher in its own right, for three reasons:

*   **Testability.** A domain retriever is just ``() -> list[Fact]``, so the
    whole five-domain flow can be exercised offline with synthetic facts.
*   **One source per field.** The actual field construction stays in the modules
    that own it (``pipeline._registry_facts``, ``coverage.people_facts``,
    ``jobs``/``activity``). The runner never re-invents a field.
*   **Fault isolation.** A domain that raises is recorded as ``failed`` and the
    other four still run. One broken source never costs a company its profile --
    the same instinct as the result envelope, one level down.

Each domain resolves to one :class:`DomainResult`, and the whole run folds into
a :class:`ResearchOutcome` whose :meth:`ResearchOutcome.coverage` is the
field-aware, source-aware matrix the console and the envelope both read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from research_fields import DOMAIN_LABELS, DOMAINS, domain_fields
from research_plan import DEFERRED, READY, SKIP, ResearchPlan

__all__ = [
    "Retriever",
    "DomainResult",
    "ResearchOutcome",
    "run_domains",
    "register_retrievers",
    "summarize",
]

#: A domain retriever collects the facts it can for one domain. It may raise;
#: the runner treats a raised exception as a ``failed`` domain, not a crash.
Retriever = Callable[[], list[Any]]

#: Domain status vocabulary. ``ok`` = facts found; ``empty`` = source answered
#: but held nothing; ``skipped`` = not attempted by design (see the plan);
#: ``deferred`` = not attempted because a precondition was missing; ``failed`` =
#: attempted and errored.
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_SKIPPED = "skipped"
STATUS_DEFERRED = "deferred"
STATUS_FAILED = "failed"


@dataclass(slots=True)
class DomainResult:
    """What one domain produced, and why."""

    domain: str
    status: str
    facts: list[Any] = field(default_factory=list)
    source: str = ""
    detail: str = ""
    error: str = ""

    @property
    def label(self) -> str:
        return DOMAIN_LABELS.get(self.domain, self.domain)

    @property
    def fact_count(self) -> int:
        return len(self.facts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "label": self.label,
            "status": self.status,
            "source": self.source,
            "fact_count": self.fact_count,
            "fields": sorted({getattr(f, "field", "") for f in self.facts} - {""}),
            "detail": self.detail,
            "error": self.error,
        }


@dataclass(slots=True)
class ResearchOutcome:
    """The five-domain result for one company."""

    orgnr: str
    results: list[DomainResult] = field(default_factory=list)

    def domain(self, key: str) -> DomainResult | None:
        for entry in self.results:
            if entry.domain == key:
                return entry
        return None

    @property
    def facts(self) -> list[Any]:
        out: list[Any] = []
        for entry in self.results:
            out.extend(entry.facts)
        return out

    @property
    def fact_count(self) -> int:
        return sum(r.fact_count for r in self.results)

    def coverage(self) -> dict[str, dict[str, Any]]:
        """Field-aware, source-aware coverage for each of the five domains.

        For every domain: which catalog fields were filled, which are still
        missing, the ratio, and -- for the filled ones -- the layer (source) the
        fact came in on. This is the matrix, not a single score.
        """
        matrix: dict[str, dict[str, Any]] = {}
        for entry in self.results:
            catalog = [f.field for f in domain_fields(entry.domain)]
            present_map: dict[str, str] = {}
            extra: list[str] = []
            for fact in entry.facts:
                name = getattr(fact, "field", "")
                if not name:
                    continue
                if name in catalog:
                    present_map[name] = getattr(fact, "layer", "") or entry.source
                elif name not in extra:
                    extra.append(name)
            present = [f for f in catalog if f in present_map]
            missing = [f for f in catalog if f not in present_map]
            matrix[entry.domain] = {
                "label": entry.label,
                "status": entry.status,
                "fields_total": len(catalog),
                "fields_present": len(present),
                "ratio": round(len(present) / len(catalog), 3) if catalog else 0.0,
                "present": present,
                "missing": missing,
                "sources": present_map,
                "extra_fields": sorted(extra),
            }
        return matrix

    def to_dict(self) -> dict[str, Any]:
        return {
            "orgnr": self.orgnr,
            "fact_count": self.fact_count,
            "domains": [r.to_dict() for r in self.results],
            "coverage": self.coverage(),
        }


def run_domains(plan: ResearchPlan, retrievers: Mapping[str, Retriever]) -> ResearchOutcome:
    """Execute every ready domain in ``plan`` against ``retrievers``.

    Domains the plan marks ``deferred`` or ``skip`` are recorded with that status
    and are *not* attempted. A ready domain with no wired retriever is recorded
    as ``skipped`` rather than silently ignored, so a gap in the wiring is
    visible instead of looking like a source that held nothing.
    """
    outcome = ResearchOutcome(orgnr=plan.orgnr)
    for domain_plan in plan.domains:
        if domain_plan.status != READY:
            outcome.results.append(
                DomainResult(
                    domain=domain_plan.domain,
                    status=STATUS_SKIPPED if domain_plan.status == SKIP else STATUS_DEFERRED,
                    source=domain_plan.primary,
                    detail=domain_plan.reason,
                )
            )
            continue

        retriever = retrievers.get(domain_plan.domain)
        if retriever is None:
            outcome.results.append(
                DomainResult(
                    domain=domain_plan.domain,
                    status=STATUS_SKIPPED,
                    source=domain_plan.primary,
                    detail="no retriever wired for this domain",
                )
            )
            continue

        try:
            facts = list(retriever() or [])
        except Exception as exc:  # one domain must never sink the company
            outcome.results.append(
                DomainResult(
                    domain=domain_plan.domain,
                    status=STATUS_FAILED,
                    source=domain_plan.primary,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        outcome.results.append(
            DomainResult(
                domain=domain_plan.domain,
                status=STATUS_OK if facts else STATUS_EMPTY,
                source=domain_plan.primary,
                facts=facts,
            )
        )
    return outcome


def register_retrievers(
    entity: dict[str, Any],
    org: Any,
    http: Any,
    *,
    number: str,
    retrieved_at: str,
    registry_url: str,
    fetch_financials: bool = True,
    include_identifiers: bool = True,
) -> dict[str, Retriever]:
    """Wire the free, register-side domains: company, financials, people/locations.

    These three cost nothing and need no gate, so they can run standalone. The
    ``website`` and ``hiring_activity`` retrievers are *not* built here: they
    depend on a page that passed the ownership gate, and the pipeline owns that
    flow, so it supplies those two.
    """
    # Imported lazily: ``pipeline`` imports this module, so importing it back at
    # module load time would be circular. By call time ``pipeline`` is loaded.
    from coverage import location_facts, people_facts  # noqa: PLC0415
    from pipeline import _financial_facts, _identifier_facts, _registry_facts  # noqa: PLC0415
    from registry import BrregClient, fetch_regnskap  # noqa: PLC0415

    brreg = BrregClient(http)

    def company() -> list[Any]:
        facts = list(_registry_facts(
            entity, number=number, retrieved_at=retrieved_at, source_url=registry_url,
        ))
        if include_identifiers:
            try:
                from enrich import enrich  # noqa: PLC0415

                facts.extend(_identifier_facts(enrich(org, http), number))
            except Exception:
                # Identifiers are a bonus layer; their absence is a gap, not an error.
                pass
        return facts

    def financials() -> list[Any]:
        figures = fetch_regnskap(org, http)
        if figures is None or not figures.available:
            return []
        return list(_financial_facts(figures, number))

    def people_locations() -> list[Any]:
        facts: list[Any] = []
        try:
            facts.extend(people_facts(
                brreg.roles(org), number=number, retrieved_at=retrieved_at,
                source_url=registry_url,
            ))
        except Exception:
            pass
        try:
            facts.extend(location_facts(
                brreg.sub_units(org), number=number, retrieved_at=retrieved_at,
                source_url=registry_url,
            ))
        except Exception:
            pass
        return facts

    retriever_map: dict[str, Retriever] = {
        "company": company,
        "people_locations": people_locations,
    }
    if fetch_financials:
        retriever_map["financials"] = financials
    return retriever_map


def summarize(outcome: ResearchOutcome) -> str:
    """A one-line, human-readable digest of a research outcome."""
    bits = [f"{r.label}: {r.status}({r.fact_count})" for r in outcome.results]
    return f"{outcome.orgnr or '-'} [{outcome.fact_count} facts] " + "; ".join(bits)
