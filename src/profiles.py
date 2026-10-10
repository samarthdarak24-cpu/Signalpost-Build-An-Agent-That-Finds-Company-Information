"""Evidence-backed profile schema and the fact record.

Every published value in a Signalpost profile is a :class:`Fact`. A fact that
cannot name its source, its retrieval date, a verbatim quote and a matching
proof is not written at all — it becomes a :class:`SkippedField` instead.

This module is pure data. It has no network dependency so the whole schema can
be unit tested and round-tripped.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

__all__ = [
    "MatchProof",
    "Freshness",
    "SkippedField",
    "Fact",
    "ChangeRecord",
    "Profile",
    "utcnow_iso",
    "freshness_of",
    "whitespace_contains",
]

#: How a value was tied to the company. Ordered from strongest to weakest.
MatchProof = Literal[
    "registry",          # read straight out of the official register
    "orgnr_on_page",     # checksum-valid org number found on the page itself
    "name_postcode",     # name + postcode + city all agree on the page
    "identifier_join",   # LEI / Wikidata join keyed on a register identifier
    "name_only",         # weak: name matched but nothing corroborated it
]


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


#: How old a retrieval is, in coarse buckets. Freshness is a first-class property
#: because "the register said this" and "the register said this two years ago"
#: are different claims, and the delta engine needs to tell them apart.
Freshness = Literal["recent", "this_week", "this_month", "this_year", "stale", "unknown"]

_FRESHNESS_BUCKETS: tuple[tuple[float, Freshness], ...] = (
    (1.0, "recent"),
    (7.0, "this_week"),
    (31.0, "this_month"),
    (366.0, "this_year"),
)


def freshness_of(retrieved_at: str) -> Freshness:
    """Bucket a retrieval timestamp by age. Unparseable or empty -> ``unknown``.

    Never guesses: an empty ``retrieved_at`` is ``unknown``, not ``recent``. The
    point of the property is to be trustworthy, so an absent date must not read
    as a fresh one.
    """
    if not retrieved_at:
        return "unknown"
    text = retrieved_at.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return "unknown"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - when).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    for limit, label in _FRESHNESS_BUCKETS:
        if age_days < limit:
            return label
    return "stale"


_WS = re.compile(r"\s+")


def _fold(text: str) -> str:
    return _WS.sub(" ", text).strip()


def whitespace_contains(haystack: str, needle: str) -> bool:
    """True when ``needle`` appears in ``haystack`` ignoring whitespace runs.

    Quotes lifted off a rendered page almost never survive byte-identical
    copying: the browser collapses runs of spaces and newlines. Comparing on a
    whitespace-folded normal form keeps the "the quote must exist on the page"
    guarantee honest without being brittle about layout.
    """
    if not needle or not haystack:
        return False
    return _fold(needle).casefold() in _fold(haystack).casefold()


@dataclass(slots=True)
class SkippedField:
    """A field the agent deliberately left empty, and why."""

    field: str
    reason: str
    candidates_considered: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "reason": self.reason,
                "candidates_considered": self.candidates_considered}


@dataclass(slots=True)
class Fact:
    """One published value plus everything needed to audit it."""

    field: str
    value: Any
    source_url: str
    retrieved_at: str
    evidence: str
    match_proof: MatchProof
    confidence: float
    layer: Literal["registry", "identifier", "web_rules", "web_llm", "derived"] = "registry"
    method: str = ""
    changed_from: Any = None
    explanation: str = ""
    org_number: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range for {self.field}: {self.confidence}")
        if self.value is None:
            raise ValueError(f"fact {self.field!r} may not carry a null value; use SkippedField")

    @property
    def is_weak(self) -> bool:
        """True when the match rests on a name alone — never treated as high confidence."""
        return self.match_proof == "name_only"

    @property
    def freshness(self) -> Freshness:
        """How old this fact's retrieval is, in coarse buckets."""
        return freshness_of(self.retrieved_at)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        # ``sources`` is only interesting when a fact was corroborated.
        if not out["sources"]:
            out.pop("sources")
        out["freshness"] = self.freshness
        return out

    def compact(self) -> dict[str, Any]:
        """The six fields the brief requires, nothing else."""
        return {
            "field": self.field,
            "value": self.value,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "evidence": self.evidence,
            "match_proof": self.match_proof,
            "confidence": round(self.confidence, 3),
            "changed_from": self.changed_from,
        }


@dataclass(slots=True)
class ChangeRecord:
    """A detected difference against the previous run."""

    field: str
    old: Any
    new: Any
    detected_at: str
    source_url: str = ""
    kind: Literal["registry", "web", "identifier"] = "registry"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Profile:
    """The output document for one company."""

    org_number: str
    legal_name: str
    run_date: str
    facts: list[Fact] = field(default_factory=list)
    changes: list[ChangeRecord] = field(default_factory=list)
    empty_fields: list[SkippedField] = field(default_factory=list)
    run_id: str = ""
    schema_version: str = "1.0"
    errors: list[str] = field(default_factory=list)

    def add(self, fact: Fact, *, minimum_confidence: float = 0.0) -> bool:
        """Attach ``fact``, or abstain.

        Returns ``True`` when the fact was kept. When ``fact.confidence`` is
        below ``minimum_confidence`` the value is dropped and a reason is
        recorded instead — this is the abstention rule from the brief.
        """
        if fact.confidence < minimum_confidence:
            self.empty_fields.append(
                SkippedField(
                    field=fact.field,
                    reason=(
                        f"confidence {fact.confidence:.2f} below threshold "
                        f"{minimum_confidence:.2f}; best proof was {fact.match_proof}"
                    ),
                    candidates_considered=1,
                )
            )
            return False
        fact.org_number = fact.org_number or self.org_number
        self.facts.append(fact)
        return True

    def get(self, name: str) -> Fact | None:
        for fact in self.facts:
            if fact.field == name:
                return fact
        return None

    def value(self, name: str) -> Any:
        fact = self.get(name)
        return fact.value if fact else None

    def replace(self, fact: Fact) -> None:
        """Overwrite an existing field, recording the old value on the new fact."""
        previous = self.get(fact.field)
        if previous is not None:
            fact.changed_from = previous.value
            self.facts = [f for f in self.facts if f.field != fact.field]
        self.facts.append(fact)

    @property
    def field_names(self) -> list[str]:
        return [f.field for f in self.facts]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "org_number": self.org_number,
            "legal_name": self.legal_name,
            "run_date": self.run_date,
            "run_id": self.run_id,
            "fact_count": len(self.facts),
            "facts": [f.to_dict() for f in self.facts],
            "changes": [c.to_dict() for c in self.changes],
            "empty_fields": [s.to_dict() for s in self.empty_fields],
            "errors": self.errors,
        }

    def write(self, directory: Path) -> Path:
        """Write ``<directory>/<orgnr>.json`` atomically and return the path.

        Atomicity is a resumability requirement, not a nicety. A budget or
        deadline termination kills the process *between* statements, and a plain
        ``write_text`` truncates the target first -- so an interruption could
        leave a profile that is half old, half new, or empty. Because the batch
        re-runs over the same directory, that corruption would then be read back
        and treated as the company's real history.

        The sequence is therefore: write a sibling temp file, flush and fsync it,
        then ``os.replace`` it onto the target. ``os.replace`` is atomic on
        POSIX and on Windows, so a reader sees either the complete old file or
        the complete new one -- never a partial one. The temp file is removed if
        anything fails, so no stray artifacts accumulate in ``profiles/``.
        """
        import os
        import tempfile

        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{self.org_number}.json"
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=f".{self.org_number}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            # Leave the previous published file intact rather than a partial one.
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return target

    @classmethod
    def read(cls, path: Path) -> "Profile":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        profile = cls(
            org_number=raw["org_number"],
            legal_name=raw.get("legal_name", ""),
            run_date=raw.get("run_date", ""),
            run_id=raw.get("run_id", ""),
            schema_version=raw.get("schema_version", "1.0"),
            errors=list(raw.get("errors", [])),
        )
        for item in raw.get("facts", []):
            item.pop("org_number", None) or None
            profile.facts.append(
                Fact(
                    field=item["field"],
                    value=item["value"],
                    source_url=item.get("source_url", ""),
                    retrieved_at=item.get("retrieved_at", ""),
                    evidence=item.get("evidence", ""),
                    match_proof=item.get("match_proof", "registry"),
                    confidence=float(item.get("confidence", 0.0)),
                    layer=item.get("layer", "registry"),
                    method=item.get("method", ""),
                    changed_from=item.get("changed_from"),
                    explanation=item.get("explanation", ""),
                    org_number=raw["org_number"],
                    sources=list(item.get("sources", [])),
                )
            )
        for item in raw.get("changes", []):
            profile.changes.append(ChangeRecord(**item))
        for item in raw.get("empty_fields", []):
            profile.empty_fields.append(SkippedField(**item))
        return profile


def coverage_ratio(profiles: Iterable[Profile], useful_fields: Iterable[str]) -> float:
    """Share of ``useful_fields`` actually published across ``profiles``."""
    fields = list(useful_fields)
    if not fields:
        return 0.0
    profiles = list(profiles)
    if not profiles:
        return 0.0
    hits = sum(1 for p in profiles for name in fields if p.value(name) is not None)
    return hits / (len(profiles) * len(fields))


def today() -> str:
    return date.today().isoformat()
