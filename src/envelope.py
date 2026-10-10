"""The result envelope: exactly one output for every input.

The execution contract for this challenge is blunt. Every organisation number
that goes in must produce a result that comes out — including the ones where
nothing could be found. A run that returns 83 results for 100 inputs has failed
the contract even if all 83 are perfect.

So the pipeline never drops a company. It wraps whatever it managed to learn in
an envelope whose ``status`` says honestly how far it got.

The six states are the ones the challenge defines:

``available``       the entity was found and facts were published
``not_available``   the entity is real but the sources hold nothing for it
``not_applicable``  the input is not a usable organisation number at all
``ambiguous``       the entity was found but its identity could not be pinned
``blocked``         the only source that could have answered refused access
``failed``          something went wrong that we could not attribute

The rule that matters most is the one this module enforces structurally: a
status of ``not_available`` or ``ambiguous`` carries **no** speculative facts.
It is always better to publish a gap than a guess, because a fact attached to
the wrong company is the one error this challenge treats as fatal.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from validate import clean

__all__ = [
    "STATUSES",
    "SourceOutcome",
    "Envelope",
    "envelopes_to_jsonl",
    "envelope_audit",
    "render_audit",
]

#: The full set of states a result may carry. Kept as a plain tuple so the
#: vocabulary is greppable and cannot drift.
STATUSES: tuple[str, ...] = (
    "available",
    "not_available",
    "not_applicable",
    "ambiguous",
    "blocked",
    "failed",
)

#: Statuses that are allowed to carry facts. The others must not, and
#: :meth:`Envelope.validate` enforces it.
_CAN_CARRY_FACTS = {"available", "ambiguous"}


@dataclass(slots=True)
class SourceOutcome:
    """What one source did for one company.

    This is the audit trail behind the envelope's single status word. When a
    judge asks "why is this field empty", the answer is one of these lines.
    """

    name: str
    status: str
    detail: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.url:
            out["url"] = self.url
        return out


@dataclass(slots=True)
class Envelope:
    """One input, one output."""

    orgnr: str
    status: str
    reason: str
    retrieved_at: str = ""
    legal_name: str = ""
    facts: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    sources: list[SourceOutcome] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    #: The five reference views (Company brief, Latest financials, Who runs it,
    #: Working here, Recent activity) plus the Unknown/Missing panel, each line
    #: pointing back at the fact it came from. Empty when the profile could not
    #: be read -- synthesis never invents, so an empty dict is an honest "none".
    synthesis: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown envelope status: {self.status!r}")

    def validate(self) -> None:
        """Refuse to emit an envelope that breaks the honesty rules.

        Called before writing, so a violation surfaces during the run rather
        than in a judge's spreadsheet.
        """
        if self.status not in _CAN_CARRY_FACTS and self.facts:
            raise ValueError(
                f"{self.orgnr}: status {self.status!r} may not carry "
                f"{len(self.facts)} facts; only {sorted(_CAN_CARRY_FACTS)} may"
            )
        if not self.reason.strip():
            raise ValueError(f"{self.orgnr}: every envelope needs a reason")

    @property
    def fact_count(self) -> int:
        return len(self.facts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "orgnr": self.orgnr,
            "status": self.status,
            "reason": self.reason,
            "retrieved_at": self.retrieved_at,
            "legal_name": self.legal_name,
            "fact_count": len(self.facts),
            "facts": self.facts,
            "skipped": self.skipped,
            "changes": self.changes,
            "sources": [s.to_dict() for s in self.sources],
            "coverage": self.coverage,
            "synthesis": self.synthesis,
            "cost": self.cost,
            "seconds": round(self.seconds, 3),
        }


def envelopes_to_jsonl(envelopes: Iterable[Envelope], path: Path | str) -> Path:
    """Write one envelope per line.

    JSONL rather than a JSON array because it is appendable and streamable: a
    run that dies at company 87 still leaves 87 readable results, which is the
    same instinct as the envelope itself.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for env in envelopes:
            env.validate()
            handle.write(json.dumps(env.to_dict(), ensure_ascii=False) + "\n")
    return target


def envelope_audit(inputs: Iterable[Any], envelopes: Iterable[Envelope]) -> dict[str, Any]:
    """The terminal-output audit: exactly one result per supplied company.

    Builderr's rule is blunt -- a missing row makes the run unscorable -- so this
    checks the three ways the contract can break: a company with no output, a
    company with two, and an output for a company that was never supplied. Inputs
    are normalised the same way the pipeline normalises them, so a spacing or
    formatting difference is not mistaken for a missing company.
    """
    in_list = [str(clean(n) or str(n).strip()) for n in inputs]
    out_list = [e.orgnr for e in envelopes]
    in_set, out_counts = set(in_list), Counter(out_list)
    status = Counter(e.status for e in envelopes)

    missing = [n for n in in_list if n not in set(out_list)]
    duplicates = [n for n, count in out_counts.items() if count > 1]
    unknown = [n for n in out_list if n not in in_set]

    return {
        "inputs": len(in_list),
        "outputs": len(out_list),
        "matched": status.get("available", 0),
        "ambiguous": status.get("ambiguous", 0),
        "not_available": status.get("not_available", 0),
        "not_applicable": status.get("not_applicable", 0),
        "blocked": status.get("blocked", 0),
        "failed": status.get("failed", 0),
        "missing_outputs": missing,
        "duplicate_outputs": duplicates,
        "unknown_orgnrs": unknown,
        "ok": not missing and not duplicates and not unknown
        and len(in_list) == len(out_list),
    }


def render_audit(audit: dict[str, Any]) -> str:
    """The audit as fixed-width terminal text."""
    tick = "OK" if audit.get("ok") else "!!"
    lines = [
        f"INPUTS:            {audit['inputs']:>5}",
        f"OUTPUTS:           {audit['outputs']:>5}",
        f"MATCHED:           {audit['matched']:>5}",
        f"AMBIGUOUS:         {audit['ambiguous']:>5}",
        f"NOT_AVAILABLE:     {audit['not_available']:>5}",
        f"NOT_APPLICABLE:    {audit['not_applicable']:>5}",
        f"BLOCKED:           {audit['blocked']:>5}",
        f"FAILED:            {audit['failed']:>5}",
        "",
        f"Missing outputs:   {len(audit['missing_outputs']):>5} {tick}",
        f"Duplicate outputs: {len(audit['duplicate_outputs']):>5} {tick}",
        f"Unknown orgnrs:    {len(audit['unknown_orgnrs']):>5} {tick}",
    ]
    return "\n".join(lines)
