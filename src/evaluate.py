"""Random-company evaluation harness.

Purpose: measure the agent on companies it has **not** been tuned on, against
ground truth that comes from the registry payload rather than from the agent's
own output. This avoids the trap of scoring a system against itself, where a
field is "covered" only because the agent decided it was.

What it measures, and why each one matters to the brief:

* **field coverage** -- how many registry-verifiable fields the agent actually
  fills. Coverage is the single largest scoring axis.
* **supported-fact precision** -- the share of published facts that carry a
  source URL, a verbatim quote and a match proof. An unsupported fact is a
  liability, not a gain.
* **company identity** -- the legal name the agent published versus the name in
  the registry payload. A mismatch is the disqualifying wrong-company error.
* **wrong-company contamination** -- any published fact whose ``org_number``
  belongs to a different company than the profile it sits in.
* **cost and budget** -- requests spent, so the 2,000-request ceiling is visible
  before it is breached.

Design rule, from the brief: *a missing search result must never be treated as
proof a fact is false*. So the harness reports **coverage** and separately
reports **unavailability**, and never counts an unavailable lookup as a miss.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

__all__ = ["Evaluation", "evaluate", "main"]

SEED = ROOT / "data" / "offline_seed.json"
GOLDEN = ROOT / "data" / "golden"
PROFILES = ROOT / "profiles"

#: Registry fields that are directly checkable against the Brreg payload. If the
#: payload has a value and the profile does not, that is a real coverage miss --
#: not a sourcing problem, because the register was already in hand.
CHECKABLE = (
    ("legal_name", lambda e: e.get("navn")),
    ("industry", lambda e: e.get("naeringskode1", {}).get("beskrivelse")),
    ("nace_code", lambda e: e.get("naeringskode1", {}).get("kode")),
    ("founded", lambda e: e.get("stiftelsesdato")),
    ("registered", lambda e: e.get("registreringsdatoEnhetsregisteret")),
    ("employees", lambda e: e.get("antallAnsatte")),
    ("municipality", lambda e: e.get("forretningsadresse", {}).get("kommune")),
    ("city", lambda e: e.get("forretningsadresse", {}).get("poststed")),
    ("postcode", lambda e: e.get("forretningsadresse", {}).get("postnummer")),
    ("email", lambda e: e.get("epostadresse")),
    ("phone", lambda e: e.get("telefon") or e.get("mobil")),
    ("website", lambda e: e.get("hjemmeside")),
    ("legal_form", lambda e: e.get("organisasjonsform", {}).get("beskrivelse")),
)


#: The field names in :data:`CHECKABLE`, in order. Exposed so tests and other
#: callers can enumerate the checkable set without reaching into the pair list.
CHECKABLE_FIELD_NAMES: tuple[str, ...] = tuple(name for name, _ in CHECKABLE)


@dataclass(slots=True)
class Evaluation:
    """The result of evaluating one batch of random companies."""

    n: int = 0
    slots_total: int = 0
    slots_filled: int = 0
    slots_unavailable: int = 0
    per_field: dict[str, dict[str, int]] = field(default_factory=dict)
    facts_total: int = 0
    facts_supported: int = 0
    identity_ok: int = 0
    #: How many companies we actually had register ground truth for. Identity
    #: accuracy is meaningless without it, so it is the only honest denominator.
    identity_checked: int = 0
    identity_mismatch: list[str] = field(default_factory=list)
    contamination: list[str] = field(default_factory=list)
    requests_used: int = 0
    search_failures: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    #: Coverage restricted to fields the register did *not* supply, i.e. the
    #: web layer's own contribution. Reported separately because a field copied
    #: from the register is not evidence that the hard part works.
    web_only_offered: int = 0
    web_only_filled: int = 0

    @property
    def coverage(self) -> float:
        return self.slots_filled / self.slots_total if self.slots_total else 0.0

    @property
    def supported_rate(self) -> float:
        return self.facts_supported / self.facts_total if self.facts_total else 0.0

    @property
    def identity_rate(self) -> float:
        """Share of *checkable* companies whose published name matches the register.

        Divided by ``identity_checked``, not by the batch size. A company with no
        ground-truth payload cannot be checked, so counting it as a mismatch would
        manufacture a disqualifying finding out of missing test data.
        """
        return self.identity_ok / self.identity_checked if self.identity_checked else 0.0

    @property
    def web_only_rate(self) -> float:
        """Coverage of fields the register left silent -- the web layer's own work."""
        return self.web_only_filled / self.web_only_offered if self.web_only_offered else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "companies": self.n,
            "coverage": round(self.coverage, 4),
            "coverage_detail": {
                "slots": self.slots_total,
                "filled": self.slots_filled,
                "unavailable_not_counted_as_miss": self.slots_unavailable,
            },
            "per_field": self.per_field,
            "supported_fact_rate": round(self.supported_rate, 4),
            "facts": self.facts_total,
            "identity_accuracy": round(self.identity_rate, 4),
            "identity_checked": self.identity_checked,
            "identity_uncheckable": self.n - self.identity_checked,
            "web_only_coverage": round(self.web_only_rate, 4),
            "web_only_detail": {
                "slots": self.web_only_offered,
                "filled": self.web_only_filled,
            },
            "identity_mismatches": self.identity_mismatch,
            "wrong_company_contamination": self.contamination,
            "requests_used": self.requests_used,
            "search_failures": self.search_failures,
            "seconds": round(self.seconds, 2),
            "notes": self.notes,
        }

    def report(self) -> str:
        lines = [
            "=" * 72,
            "  RANDOM-COMPANY EVALUATION",
            "=" * 72,
            f"  companies                   {self.n}",
            f"  field coverage              {self.coverage*100:5.1f}%   "
            f"({self.slots_filled}/{self.slots_total} checkable slots)",
            f"    unavailable (excluded)    {self.slots_unavailable}",
            f"  supported-fact precision    {self.supported_rate*100:5.1f}%   "
            f"({self.facts_supported}/{self.facts_total} facts)",
            f"  identity accuracy           {self.identity_rate*100:5.1f}%   "
            f"({self.identity_ok}/{self.identity_checked} names match the register; "
            f"{self.n - self.identity_checked} not checkable)",
            f"  wrong-company contamination {len(self.contamination)}",
            f"  web-only coverage           {self.web_only_rate*100:5.1f}%   "
            f"({self.web_only_filled}/{self.web_only_offered} contact/website slots "
            f"the register left silent)",
            f"  requests used               {self.requests_used}  (ceiling 2000)",
            f"  searches that failed        {self.search_failures}",
            f"  wall clock                  {self.seconds:.1f}s",
            "",
            "  per-field coverage",
        ]
        for name, tally in sorted(self.per_field.items(),
                                  key=lambda kv: -(kv[1]["present"] / max(kv[1]["offered"], 1))):
            offered = tally["offered"] or 1
            lines.append(
                f"    {name:<16} {tally['present']:>4}/{offered:<4} "
                f"{tally['present']/offered*100:5.1f}%"
            )
        if self.identity_mismatch:
            lines.append("")
            lines.append("  IDENTITY MISMATCHES (disqualifying class -- inspect every one)")
            lines.extend(f"    {m}" for m in self.identity_mismatch[:20])
        if self.contamination:
            lines.append("")
            lines.append("  WRONG-COMPANY CONTAMINATION")
            lines.extend(f"    {c}" for c in self.contamination[:20])
        for note in self.notes:
            lines.append(f"  note: {note}")
        lines.extend([
            "",
            "  HOW TO READ THIS",
            "    * 'field coverage' counts fields the *register* offered. A high",
            "      number here shows the register-copy path is faithful; it does not",
            "      show the web layer works. Read 'web-only coverage' for that.",
            "    * A slot is only excluded when the profile records that the lookup",
            "      did not complete. A lookup that ran and found nothing counts as a",
            "      real miss, because then the absence is evidence about the company.",
            "    * Identity accuracy compares the published legal name to the register",
            "      payload. Any mismatch is listed in full and is disqualifying.",
        ])
        return "\n".join(lines)


def _sourced(fact: dict[str, Any]) -> bool:
    """A fact is supported when it names where it came from and proves the match."""
    return bool(fact.get("source_url")) and bool(fact.get("evidence")) \
        and bool(fact.get("match_proof"))


def evaluate(numbers: Iterable[str], *, seed_payload: dict[str, dict],
             verbose: bool = False) -> Evaluation:
    """Score already-published profiles against the registry payloads."""
    ev = Evaluation()
    started = time.monotonic()

    for number in numbers:
        profile_path = PROFILES / f"{number}.json"
        if not profile_path.exists():
            ev.notes.append(f"{number}: no profile on disk, skipped")
            continue
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        entity = seed_payload.get(number)
        ev.n += 1

        published = {f["field"]: f for f in profile.get("facts", [])}

        # -- identity: did we identify the right company?
        if entity:
            ev.identity_checked += 1
            want = (entity.get("navn") or "").strip().casefold()
            got = (profile.get("legal_name") or "").strip().casefold()
            if want and got and (want == got or want in got or got in want):
                ev.identity_ok += 1
            elif want:
                ev.identity_mismatch.append(
                    f"{number}: register={entity.get('navn')!r} published={profile.get('legal_name')!r}"
                )

        # -- contamination: any fact carrying a foreign org number?
        for fact in profile.get("facts", []):
            foreign = fact.get("org_number")
            if foreign and str(foreign) != str(number):
                ev.contamination.append(
                    f"{number}: field {fact.get('field')!r} carries org_number {foreign}"
                )

        # -- support: does every fact cite itself?
        for fact in profile.get("facts", []):
            ev.facts_total += 1
            if _sourced(fact):
                ev.facts_supported += 1

        # -- coverage against what the register actually offered
        if entity:
            skip_reasons = {s["field"]: s["reason"]
                            for s in profile.get("empty_fields", [])}
            for field_name, extractor in CHECKABLE:
                try:
                    offered = extractor(entity)
                except Exception:
                    offered = None
                if offered in (None, "", [], 0):
                    continue
                tally = ev.per_field.setdefault(field_name, {"offered": 0, "present": 0})
                tally["offered"] += 1
                ev.slots_total += 1
                if field_name in published:
                    tally["present"] += 1
                    ev.slots_filled += 1
                else:
                    reason = skip_reasons.get(field_name, "")
                    # An unavailable lookup is not a miss. The register offered
                    # the value, but if the agent recorded that it could not
                    # check, we exclude it rather than score it as absent.
                    if "could not" in reason.casefold() or "did not complete" in reason.casefold():
                        ev.slots_unavailable += 1
                        tally["offered"] -= 1
                        ev.slots_total -= 1
                        # Drop the entry entirely once nothing is left to offer,
                        # so the per-field table never shows a meaningless 0/0 row.
                        if tally["offered"] <= 0 and tally["present"] <= 0:
                            ev.per_field.pop(field_name, None)
                        continue

        status = (profile.get("errors") or [])
        ev.search_failures += sum(1 for e in status if "lookup" in e.casefold()
                                  or "search" in e.casefold())

        # -- the web layer's own contribution: fields the register left silent.
        # Measured separately because a value copied from the register proves the
        # register was reachable, not that discovery works.
        if entity:
            for field_name, extractor in CHECKABLE:
                if field_name not in ("website", "email", "phone"):
                    continue
                try:
                    offered = extractor(entity)
                except Exception:
                    offered = None
                if offered in (None, "", [], 0):
                    ev.web_only_offered += 1
                    if field_name in published:
                        ev.web_only_filled += 1

        if verbose:
            print(f"  {number}  {profile.get('legal_name','?'):<34} "
                  f"{len(published)} facts")

    ev.seconds = time.monotonic() - started
    return ev


def _seed_payload() -> dict[str, dict]:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    return {e.get("organisasjonsnummer"): e
            for e in data.get("entities", [])
            if e.get("organisasjonsnummer")}


def _random_numbers(n: int, *, rng: random.Random) -> list[str]:
    """Random org numbers from the seed plus the golden set, never re-used."""
    pool = list(_seed_payload().keys())
    golden = [p.stem for p in GOLDEN.glob("*.json")]
    combined = sorted(set(pool) | set(golden))
    if n >= len(combined):
        return combined
    return sorted(rng.sample(combined, n))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluate", description=__doc__)
    parser.add_argument("--n", type=int, default=40, help="how many companies")
    parser.add_argument("--seed", type=int, default=20261010,
                        help="RNG seed, so a run is reproducible")
    parser.add_argument("--all", action="store_true", help="use every known number")
    parser.add_argument("--out", type=Path, default=None, help="write JSON here")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    payload = _seed_payload()
    # When --all, every profile on disk is in scope and payload lookup is optional.
    if args.all:
        numbers = sorted(p.stem for p in PROFILES.glob("*.json"))
    else:
        numbers = _random_numbers(args.n, rng=rng)

    print(f"evaluating {len(numbers)} companies (seed={args.seed})")
    ev = evaluate(numbers, seed_payload=payload, verbose=args.verbose)
    print(ev.report())

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(ev.to_dict(), ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
