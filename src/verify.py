"""Cross-source verification and abstention.

Three jobs, in this order:

1. **Corroborate** — when two independent sources agree, raise confidence and
   attach both to the fact, so the profile shows a fact with two receipts.
2. **Arbitrate** — when they disagree, ask the larger model *once*. If it cannot
   pick a winner, the field is dropped. A coin flip between two plausible values
   is exactly the wrong-company error the brief disqualifies for.
3. **Constrain** — enforce the field-specific validity rules that a model cannot
   be trusted to hold: a phone number must be a Norwegian number, an email must
   be role-based, an address must carry a real postcode.

The verifier never invents a value. It can only lower confidence, delete, or
promote an existing one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from extract_llm import LlmClient, resolve_conflict
from profiles import Fact, MatchProof, Profile, SkippedField

__all__ = ["VerificationOutcome", "verify_profile", "validate_value", "VALIDATORS"]

#: Tolerance when comparing two sources on one field. Values closer than this
#: are treated as agreement rather than a conflict, because the web renders
#: "45 22 22 88" and "45222288" for the same switchboard.
_PHONE_DIGITS = re.compile(r"\D")
_EMAIL_ANY = re.compile(r"(?i)^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
_POSTCODE_ANY = re.compile(r"^\d{4}$")


def _norm_phone(value: str) -> str:
    digits = _PHONE_DIGITS.sub("", str(value))
    if digits.startswith("0047"):
        digits = digits[4:]
    elif digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits


def _norm_email(value: str) -> str:
    return str(value).strip().casefold().removeprefix("mailto:")


def _norm_url(value: str) -> str:
    text = str(value).strip().casefold()
    for prefix in ("https://", "http://"):
        text = text.removeprefix(prefix)
    return text.removeprefix("www.").rstrip("/")


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _norm_year(value: str) -> str:
    match = re.search(r"(1[89]\d{2}|20\d{2})", str(value))
    return match.group(1) if match else _norm_text(value)


VALIDATORS: dict[str, Any] = {
    "email": (lambda v: bool(_EMAIL_ANY.match(str(v).strip())) and "@" in str(v),
              "not a well-formed email address"),
    "phone": (lambda v: len(_norm_phone(v)) == 8, "not an 8-digit Norwegian number"),
    "postcode": (lambda v: bool(_POSTCODE_ANY.match(str(v).strip())),
                 "not a 4-digit Norwegian postcode"),
    "website": (lambda v: "." in str(v) and " " not in str(v).strip(),
                "not a plausible hostname"),
    "employees": (lambda v: str(v).strip().isdigit() and int(v) >= 0,
                  "not a non-negative integer"),
}

NORMALISERS = {
    "phone": _norm_phone,
    "email": _norm_email,
    "website": _norm_url,
    "postcode": lambda v: str(v).strip(),
    "address": _norm_text,
    "description": _norm_text,
    "employees": lambda v: str(v).strip(),
    "founding_year": _norm_year,
    "city": _norm_text,
}


def validate_value(field_name: str, value: Any) -> tuple[bool, str]:
    """Run the field-specific constraint, if there is one."""
    validator = VALIDATORS.get(field_name)
    if validator is None:
        return True, ""
    check, message = validator
    try:
        return (True, "") if check(value) else (False, message)
    except Exception:
        return False, message


def _equivalent(field_name: str, left: Any, right: Any) -> bool:
    normalise = NORMALISERS.get(field_name, _norm_text)
    try:
        return normalise(left) == normalise(right)
    except Exception:
        return str(left).strip() == str(right).strip()


@dataclass(slots=True)
class VerificationOutcome:
    """What the verifier did to a profile, in numbers, for the run report."""

    corroborated: int = 0
    conflicts_resolved: int = 0
    conflicts_dropped: int = 0
    invalid_dropped: int = 0
    llm_calls: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corroborated": self.corroborated,
            "conflicts_resolved": self.conflicts_resolved,
            "conflicts_dropped": self.conflicts_dropped,
            "invalid_dropped": self.invalid_dropped,
            "llm_calls": self.llm_calls,
            "notes": self.notes,
        }


def _proof_rank(proof: str) -> int:
    return {
        "registry": 5,
        "orgnr_on_page": 4,
        "identifier_join": 3,
        "name_postcode": 2,
        "name_only": 1,
    }.get(proof, 0)


def verify_profile(
    profile: Profile,
    *,
    llm: LlmClient | None = None,
    minimum_confidence: float,
    max_conflicts_per_profile: int = 4,
) -> VerificationOutcome:
    """Verify every fact in ``profile`` in place.

    Returns a :class:`VerificationOutcome` so the run report can say "12 fields
    corroborated, 2 conflicts resolved, 1 dropped" rather than just "done".
    """
    outcome = VerificationOutcome()
    if not profile.facts:
        return outcome

    by_field: dict[str, list[int]] = {}
    for index, fact in enumerate(profile.facts):
        by_field.setdefault(fact.field, []).append(index)

    drop: set[int] = set()
    conflicts_handled = 0

    for field_name, indices in by_field.items():
        # --- constraint check first: cheapest, and it can delete outright -----
        for index in indices:
            ok, reason = validate_value(field_name, profile.facts[index].value)
            if not ok:
                drop.add(index)
                profile.empty_fields.append(
                    SkippedField(
                        field=field_name,
                        reason=f"{reason}: {profile.facts[index].value!r}",
                        candidates_considered=1,
                    )
                )
                outcome.invalid_dropped += 1
                outcome.notes.append(f"{field_name}: dropped invalid value ({reason})")

        survivors = [i for i in indices if i not in drop]
        if len(survivors) <= 1:
            continue

        # --- corroborate or arbitrate ----------------------------------------
        grouped: list[list[int]] = []
        for index in survivors:
            for group in grouped:
                if _equivalent(field_name, profile.facts[group[0]].value,
                               profile.facts[index].value):
                    group.append(index)
                    break
            else:
                grouped.append([index])

        if len(grouped) == 1:
            # Every source says the same thing: promote confidence, keep receipts.
            group = grouped[0]
            best = max(group, key=lambda i: (
                _proof_rank(profile.facts[i].match_proof), profile.facts[i].confidence
            ))
            keeper = profile.facts[best]
            distinct_sources = {profile.facts[i].source_url for i in group}
            keeper.confidence = min(
                1.0,
                max(profile.facts[i].confidence for i in group)
                + (0.03 if len(distinct_sources) > 1 else 0.0),
            )
            keeper.sources = [
                {
                    "url": profile.facts[i].source_url,
                    "match_proof": profile.facts[i].match_proof,
                    "evidence": profile.facts[i].evidence[:240],
                    "retrieved_at": profile.facts[i].retrieved_at,
                }
                for i in group if i != best
            ]
            for index in group:
                if index != best:
                    drop.add(index)
            if len(distinct_sources) > 1:
                outcome.corroborated += 1
                outcome.notes.append(
                    f"{field_name}: corroborated across {len(distinct_sources)} sources"
                )
            continue

        # Genuine disagreement.
        ranked = sorted(
            survivors,
            key=lambda i: (_proof_rank(profile.facts[i].match_proof),
                           profile.facts[i].confidence),
            reverse=True,
        )
        best_index, second_index = ranked[0], ranked[1]

        # A conclusive proof beats a weak one without spending a model call:
        # an on-page organisation number is not something a model should
        # second-guess.
        if _proof_rank(profile.facts[best_index].match_proof) > _proof_rank(
            profile.facts[second_index].match_proof
        ) + 1:
            for index in survivors:
                if index != best_index:
                    drop.add(index)
            outcome.notes.append(
                f"{field_name}: kept {profile.facts[best_index].match_proof} source over "
                f"{profile.facts[second_index].match_proof}"
            )
            continue

        if conflicts_handled >= max_conflicts_per_profile:
            for index in survivors:
                drop.add(index)
            profile.empty_fields.append(
                SkippedField(field_name, "per-profile conflict arbitration cap reached",
                             len(survivors))
            )
            outcome.conflicts_dropped += 1
            continue

        conflicts_handled += 1
        first, second = profile.facts[best_index], profile.facts[second_index]
        choice, rationale = (
            resolve_conflict(
                llm,
                field_name=field_name,
                first={"value": first.value, "quote": first.evidence,
                       "url": first.source_url, "match_proof": first.match_proof,
                       "confidence": first.confidence},
                second={"value": second.value, "quote": second.evidence,
                        "url": second.source_url, "match_proof": second.match_proof,
                        "confidence": second.confidence},
            )
            if llm is not None
            else ("unresolved", "verifier ran without a model; unresolved conflicts are dropped")
        )
        if llm is not None:
            outcome.llm_calls += 1

        if choice == "first":
            for index in survivors:
                if index != best_index:
                    drop.add(index)
            outcome.conflicts_resolved += 1
            outcome.notes.append(f"{field_name}: resolved in favour of {first.source_url} — {rationale}")
        elif choice == "second":
            for index in survivors:
                if index != second_index:
                    drop.add(index)
            outcome.conflicts_resolved += 1
            outcome.notes.append(f"{field_name}: resolved in favour of {second.source_url} — {rationale}")
        else:
            for index in survivors:
                drop.add(index)
            profile.empty_fields.append(
                SkippedField(
                    field=field_name,
                    reason=(
                        f"unresolved conflict between {first.source_url} and "
                        f"{second.source_url}; dropped rather than guessed"
                    ),
                    candidates_considered=len(survivors),
                )
            )
            outcome.conflicts_dropped += 1
            outcome.notes.append(f"{field_name}: unresolved, field dropped")

    if drop:
        profile.facts = [f for i, f in enumerate(profile.facts) if i not in drop]
    return outcome


def attach_org_number_proof(profile: Profile, org_number: str) -> None:
    """Assert the invariant the brief cares about most.

    Every fact in a profile must have been retrieved under the same organisation
    number that names the profile. A mismatch is a wrong-company publication,
    and it is caught here rather than shipped.
    """
    for fact in profile.facts:
        if fact.org_number and fact.org_number != org_number:
            raise ValueError(
                f"fact {fact.field!r} was retrieved under {fact.org_number} "
                f"but is being published under {org_number}"
            )
        fact.org_number = org_number


def strongest_proof(facts: Iterable[Fact]) -> MatchProof:
    """The best proof type among a set of facts — used as the profile's headline."""
    return max(
        (f.match_proof for f in facts),
        key=_proof_rank,
        default="registry",
    )
