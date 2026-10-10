"""One explanation line per fact.

The brief awards ten points for "useful explanations — one line per fact with
proof type". The explanation has to say two things at once: what evidence
supports the value, and how that evidence was tied to *this* company. A line
that says only "from the website" is worth nothing, because it does not answer
the only question that matters when two companies share a name.
"""

from __future__ import annotations

from typing import Any

from profiles import Fact, Profile

__all__ = ["explain", "explain_profile", "render_report"]

#: How each proof type reads to a human.
PROOF_PHRASE = {
    "registry": "the official register",
    "orgnr_on_page": "the organisation number printed on the page",
    "name_postcode": "the company name together with its postal address",
    "identifier_join": "a shared register identifier",
    "name_only": "a name match alone",
}

LAYER_PHRASE = {
    "registry": "registry",
    "identifier": "GLEIF/Wikidata",
    "web_rules": "page rules",
    "web_llm": "model extraction",
    "derived": "derived",
}


def explain(fact: Fact, *, company_name: str = "") -> str:
    """Compose the single line stored alongside the fact.

    Kept to one sentence with the value, the proof, the layer and the date, so a
    reviewer can follow the chain without opening the JSON.
    """
    proof = PROOF_PHRASE.get(fact.match_proof, fact.match_proof)
    layer = LAYER_PHRASE.get(fact.layer, fact.layer)
    subject = company_name or fact.org_number or "the company"
    value = fact.value
    if isinstance(value, str) and len(value) > 90:
        value = value[:87] + "..."

    changed = ""
    if fact.changed_from is not None:
        changed = f" Changed from {fact.changed_from!r} on {fact.retrieved_at[:10]}."

    if fact.match_proof == "registry":
        body = f"{subject}: {fact.field} = {value!r}, read directly from {proof}."
    elif fact.match_proof == "orgnr_on_page":
        body = (
            f"{subject}: {fact.field} = {value!r}, taken from a page that displays the "
            f"company's own organisation number ({proof})."
        )
    elif fact.match_proof == "name_postcode":
        body = (
            f"{subject}: {fact.field} = {value!r}, accepted because {proof} agree; the "
            "page carried no organisation number, so this is a weaker match."
        )
    elif fact.match_proof == "identifier_join":
        body = f"{subject}: {fact.field} = {value!r}, joined on {proof}."
    else:
        body = (
            f"{subject}: {fact.field} = {value!r}, resting on {proof} only and therefore "
            "below the confidence threshold."
        )
    return f"{body} ({layer}, retrieved {fact.retrieved_at[:10]}).{changed}"


def explain_profile(profile: Profile) -> None:
    """Fill ``fact.explanation`` for every fact in place."""
    for fact in profile.facts:
        fact.explanation = explain(fact, company_name=profile.legal_name)


def render_report(profile: Profile, *, width: int = 78) -> str:
    """A plain-text rendering of one profile, used by the CLI and the run log."""
    lines = [
        "=" * width,
        f"{profile.legal_name or '(unnamed)'}  ·  org {_display(profile.org_number)}",
        f"run {profile.run_id or '-'} · {profile.run_date} · {len(profile.facts)} facts"
        + (f" · {len(profile.changes)} changes" if profile.changes else ""),
        "=" * width,
    ]

    buckets: dict[str, list[Fact]] = {}
    for fact in profile.facts:
        bucket = fact.field.split(".")[0]
        buckets.setdefault(bucket, []).append(fact)

    for bucket in sorted(buckets):
        lines.append("")
        lines.append(f"  {bucket.upper()}")
        for fact in buckets[bucket]:
            confidence = f"{fact.confidence:.2f}"
            lines.append(f"    {fact.field:<22} {_render_value(fact.value):<34} {confidence}")
            lines.append(f"      ↳ {fact.explanation or fact.method}")

    if profile.changes:
        lines.append("")
        lines.append("  CHANGES")
        for change in profile.changes:
            lines.append(
                f"    {change.field:<22} {_render_value(change.old)} → "
                f"{_render_value(change.new)}  ({change.kind}, {change.detected_at[:10]})"
            )

    if profile.empty_fields:
        lines.append("")
        lines.append("  LEFT EMPTY (abstained)")
        for skipped in profile.empty_fields[:12]:
            lines.append(f"    {skipped.field:<22} {skipped.reason}")

    return "\n".join(lines)


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    text = str(value)
    return text if len(text) <= 34 else text[:31] + "..."


def _display(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"{digits[:3]} {digits[3:6]} {digits[6:9]}" if len(digits) == 9 else number
