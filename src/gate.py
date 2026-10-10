"""The proof-of-ownership gate.

A Norwegian business is legally required to display its name and organisation
number. That requirement is the cheapest reliable link between a company and a
website, so it is the gate: a page counts as belonging to a company only when a
*checksum-valid* organisation number appears on it.

A gate that demanded the number would miss the many small sites that show a
name and an address but no number. So there is one documented fallback — name
plus postcode plus city together — and it is charged a lower confidence and a
weaker proof type, which the verifier then treats accordingly.

Anything else is rejected. Name-only matches are never accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from normalize import Page, fold_name, extract_postcodes, is_directory_host
from validate import OrgNumber, clean

__all__ = ["GateVerdict", "GateDecision", "evaluate", "confidence_for"]


#: Confidence assigned per proof type. A register-sourced fact is 1.0; a number
#: on the page is as close to certainty as the open web allows; name+place is a
#: real but weaker signal; name-only never clears any threshold.
CONFIDENCE: dict[str, float] = {
    "registry": 1.00,
    "orgnr_on_page": 0.96,
    "identifier_join": 0.90,
    "name_postcode": 0.74,
    "name_only": 0.35,
    "rejected": 0.0,
}

Verdict = Literal[
    "accepted_orgnr",
    "accepted_name_postcode",
    "rejected_no_proof",
    "rejected_wrong_orgnr",
    "rejected_name_only",
    "rejected_text_too_short",
    "rejected_directory_host",
]


@dataclass(slots=True)
class GateVerdict:
    """The gate's answer for one page.

    ``accepted`` is the only thing callers need to branch on, but everything
    else is kept so the verdict can be shown in the UI and audited later —
    including the other organisations mentioned on the page, which is how a
    parent/group site is distinguished from a genuinely wrong match.
    """

    url: str
    verdict: Verdict
    match_proof: str
    confidence: float
    quote: str = ""
    other_org_numbers: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict in ("accepted_orgnr", "accepted_name_postcode")

    @property
    def names_this_company(self) -> bool:
        return self.verdict == "accepted_orgnr"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "verdict": self.verdict,
            "match_proof": self.match_proof,
            "confidence": round(self.confidence, 3),
            "quote": self.quote,
            "other_org_numbers": self.other_org_numbers,
            "detail": self.detail,
        }


@dataclass(slots=True)
class GateDecision:
    """The best verdict across all candidate pages of one website."""

    accepted: bool
    match_proof: str
    confidence: float
    verdict: GateVerdict | None = None
    considered: list[GateVerdict] = field(default_factory=list)

    @property
    def url(self) -> str:
        return self.verdict.url if self.verdict else ""

    @property
    def quote(self) -> str:
        return self.verdict.quote if self.verdict else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "match_proof": self.match_proof,
            "confidence": round(self.confidence, 3),
            "winning_url": self.url,
            "considered": [v.to_dict() for v in self.considered],
        }


def _quote_around(text: str, needle: str, span: int = 70) -> str:
    """A short window of page text around ``needle``, for the evidence field."""
    flat = re.sub(r"\s+", " ", text)
    idx = flat.casefold().find(needle.casefold())
    if idx < 0:
        flat = re.sub(r"\s+", " ", re.sub(r"[.\-\s]", "", text))
        idx = flat.casefold().find(re.sub(r"[.\-\s]", "", needle).casefold())
        if idx < 0:
            return ""
    start = max(0, idx - span)
    end = min(len(flat), idx + len(needle) + span)
    snippet = flat[start:end].strip()
    return f"...{snippet}..." if start else snippet


#: A page with less visible text than this is almost certainly a JS shell, a
#: cookie wall or a block message rather than content worth reading. It is a
#: floor for *is this a real page*, not a quality bar for the company's site.
MIN_TEXT_LENGTH = 60


def evaluate(
    page: Page,
    org: OrgNumber,
    *,
    name: str,
    postcode: str | None = None,
    city: str | None = None,
    min_text_length: int = MIN_TEXT_LENGTH,
) -> GateVerdict:
    """Decide whether ``page`` belongs to ``org``.

    Order matters and mirrors the cost cascade: the checksummed number is tried
    first because it is conclusive; the name/place fallback is tried only if the
    number is absent, and never if the page shows a *different* number (which is
    the classic wrong-company trap on a shared group website).
    """
    url = page.url

    if page.status != 200:
        return GateVerdict(url, "rejected_no_proof", "rejected", 0.0,
                           detail=f"page returned HTTP {page.status}")

    # A directory page is not the company's own site, however many company facts
    # it quotes. This check runs *before* the number-on-page rule because that
    # rule cannot tell "the company published its number" from "a directory
    # quoted the company's number" -- and the second case is the modal top hit
    # for a keyless search. Accepting it would attribute a directory's content
    # to the company, so the host decides first.
    if is_directory_host(url):
        return GateVerdict(
            url, "rejected_directory_host", "rejected", 0.0,
            detail="host is a directory, register or aggregator, not the company's own site",
        )

    text = page.text
    if len(text) < min_text_length:
        return GateVerdict(
            url, "rejected_text_too_short", "rejected", 0.0,
            detail=f"only {len(text)} characters of visible text; likely a block page",
        )

    found = page.org_numbers
    others = [n for n in found if n != org.value]

    if org.value in found:
        return GateVerdict(
            url=url,
            verdict="accepted_orgnr",
            match_proof="orgnr_on_page",
            confidence=CONFIDENCE["orgnr_on_page"],
            quote=_quote_around(text, org.display) or _quote_around(text, org.value),
            other_org_numbers=others,
            detail="checksum-valid organisation number present on the page",
        )

    if others:
        return GateVerdict(
            url=url,
            verdict="rejected_wrong_orgnr",
            match_proof="rejected",
            confidence=0.0,
            other_org_numbers=others,
            detail=(
                "page shows a different organisation number "
                f"({', '.join(others[:3])}) and not {org.display}"
            ),
        )

    folded_page = fold_name(text)
    company_key = fold_name(name)
    name_present = bool(company_key) and company_key in folded_page

    if not name_present:
        return GateVerdict(
            url, "rejected_no_proof", "rejected", 0.0,
            detail="no organisation number and no matching name",
        )

    # Name is present but nothing is checksummed. Demand corroboration from the
    # postal address before trusting it.
    page_postcodes = extract_postcodes(text)
    postcode_ok = bool(postcode) and postcode in page_postcodes
    city_ok = bool(city) and city.casefold() in text.casefold()

    if postcode_ok and city_ok:
        anchor = f"{postcode} {city}"
        return GateVerdict(
            url=url,
            verdict="accepted_name_postcode",
            match_proof="name_postcode",
            confidence=CONFIDENCE["name_postcode"],
            quote=_quote_around(text, city) or _quote_around(text, postcode or ""),
            detail=f"name plus address match ({anchor}) but no organisation number on page",
        )

    missing = []
    if not postcode_ok:
        missing.append(f"postcode {postcode or '(not in register)'}")
    if not city_ok:
        missing.append(f"city {city or '(not in register)'}")
    return GateVerdict(
        url=url,
        verdict="rejected_name_only",
        match_proof="name_only",
        confidence=CONFIDENCE["name_only"],
        quote=_quote_around(text, company_key) or "",
        detail=f"name matched but {', '.join(missing)} did not; name alone is not accepted",
    )


def decide(verdicts: Iterable[GateVerdict]) -> GateDecision:
    """Pick the strongest verdict from a set of candidate pages.

    A number-proven page always beats a name/place page. Ties break on
    confidence then on page order, so the result is deterministic.
    """
    considered = list(verdicts)
    ranked = sorted(
        considered,
        key=lambda v: (
            {"orgnr_on_page": 2, "name_postcode": 1}.get(v.match_proof, 0),
            v.confidence,
        ),
        reverse=True,
    )
    winner = next((v for v in ranked if v.accepted), None)
    if winner is None:
        return GateDecision(False, "rejected", 0.0, None, considered)
    return GateDecision(True, winner.match_proof, winner.confidence, winner, considered)


def confidence_for(proof: str) -> float:
    """Public accessor so the pipeline never hardcodes a number."""
    return CONFIDENCE.get(proof, 0.0)


def group_site_rule(page: Page, org: OrgNumber) -> str:
    """Extra detail for the profile when a page is a shared group website.

    A subsidiary that reuses its parent's site is only given facts when a page
    actually names the subsidiary or shows its number — which the gate has
    already enforced. This function just makes that visible in the output.
    """
    others = [n for n in page.org_numbers if n != org.value]
    if not others:
        return ""
    return (
        f"page also carries {len(others)} other organisation number(s); "
        "only facts naming this company or its number were taken"
    )
