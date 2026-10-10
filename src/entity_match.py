"""Entity resolution: score the signals, then let the gate decide.

The spec splits a web match into three stages -- *candidate discovery* (find
possible sites), *entity resolution* (how strongly does this page look like this
company?), and the *ownership decision* (publish or not). The gate already owns
the decision; this module owns the middle stage, because the gate's job is to be
strict, and a strict gate benefits from a numeric signal it can point at.

The scoring is additive and deterministic, so the same page always scores the
same and a reviewer can see exactly which signals fired:

    organisation number   +1.00   conclusive on its own
    exact legal name      +0.25
    normalised name       +0.15   (name matches once suffixes/punctuation fold)
    postcode              +0.15
    address line          +0.12
    city                  +0.10
    domain consistency    +0.10   the host looks like the company, not a directory
    phone                 +0.08

An organisation-number match short-circuits to ``publish``: a checksum-valid
number on the page is the strongest link the open web offers. Otherwise the
thresholds below apply, and they are chosen to reproduce the gate's own rule --
name alone never clears the bar, name plus postcode plus city does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from normalize import Page, extract_postcodes, fold_name
from validate import OrgNumber

__all__ = [
    "WEIGHTS",
    "PUBLISH_THRESHOLD",
    "REVIEW_THRESHOLD",
    "MatchSignals",
    "MatchScore",
    "resolve",
    "resolve_page",
    "best",
]

#: Signal weights. Named constants so the numbers live in one place and a change
#: to the rubric is a one-line edit, not a hunt through the codebase.
WEIGHTS: dict[str, float] = {
    "orgnr": 1.00,
    "exact_name": 0.25,
    "folded_name": 0.20,
    "postcode": 0.15,
    "address": 0.12,
    "city": 0.10,
    "domain": 0.10,
    "phone": 0.08,
}

#: Deterministic thresholds. ``publish`` clears the gate's own bar (a number, or
#: name plus postcode plus city); ``review`` is a candidate a human/verifier
#: should look at; everything else is rejected. The threshold is 0.45 so that a
#: *folded* name (0.20) with postcode (0.15) and city (0.10) reaches exactly
#: 0.45 -- matching the gate, which accepts a suffix-folded name plus place --
#: while a name with only one of the two still falls short.
PUBLISH_THRESHOLD = 0.45
REVIEW_THRESHOLD = 0.30

#: Hosts that are directories or aggregators, never a company's own site.
_DIRECTORY_HOSTS = (
    "proff.no", "1881.no", "gulesider.no", "regnskapstall.no", "purehelp.no",
    "wikipedia.org", "facebook.com", "linkedin.com", "instagram.com",
    "brreg.no", "data.brreg.no", "skatteetaten.no", "dnb.com", "finn.no",
)


@dataclass(slots=True)
class MatchSignals:
    """The raw boolean signals observed for one candidate page.

    Field names match the keys of :data:`WEIGHTS` one-to-one, so scoring is a
    direct lookup and there is no translation table to drift.
    """

    orgnr: bool = False
    exact_name: bool = False
    folded_name: bool = False
    postcode: bool = False
    address: bool = False
    city: bool = False
    domain: bool = False
    phone: bool = False

    def fired(self) -> list[str]:
        return [name for name in WEIGHTS if getattr(self, name, False)]


@dataclass(slots=True)
class MatchScore:
    """The resolved score for one candidate."""

    score: float
    verdict: str  #: "publish" | "review" | "reject"
    breakdown: dict[str, float] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def publishable(self) -> bool:
        return self.verdict == "publish"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "verdict": self.verdict,
            "breakdown": {k: round(v, 3) for k, v in self.breakdown.items()},
            "signals": self.signals,
            "reason": self.reason,
        }


def resolve(signals: MatchSignals) -> MatchScore:
    """Turn signals into a score and a deterministic verdict."""
    breakdown: dict[str, float] = {}
    for name, weight in WEIGHTS.items():
        if getattr(signals, name, False):
            breakdown[name] = weight
    # An exact name and a folded name are the same signal at two strengths, not
    # two signals: counting both would let a bare name clear the bar on its own.
    if "exact_name" in breakdown and "folded_name" in breakdown:
        breakdown.pop("folded_name")
    score = round(sum(breakdown.values()), 3)

    if signals.orgnr:
        return MatchScore(
            score=max(score, WEIGHTS["orgnr"]), verdict="publish",
            breakdown=breakdown, signals=signals.fired(),
            reason="checksum-valid organisation number present on the page",
        )

    if score >= PUBLISH_THRESHOLD:
        verdict, reason = "publish", "name plus address corroborate each other"
    elif score >= REVIEW_THRESHOLD:
        verdict, reason = "review", "partial corroboration; not enough to publish alone"
    else:
        verdict, reason = "reject", "only a name-level signal; name alone is never enough"
    return MatchScore(score=score, verdict=verdict, breakdown=breakdown,
                      signals=signals.fired(), reason=reason)


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.casefold().removeprefix("www.")


def _domain_consistent(url: str, name: str, legal_website: str = "") -> bool:
    host = _host(url)
    if not host:
        return False
    if any(host == d or host.endswith("." + d) for d in _DIRECTORY_HOSTS):
        return False
    if legal_website:
        reg_host = _host(legal_website)
        if reg_host and (host == reg_host or host.endswith("." + reg_host)
                         or reg_host.endswith("." + host)):
            return True
    # A host that contains the folded company name is a strong hint.
    tokens = [t for t in fold_name(name).split() if len(t) >= 4]
    squashed = re.sub(r"[^a-z0-9]", "", host)
    return any(t.replace(" ", "") in squashed for t in tokens)


def resolve_page(
    page: Page,
    org: OrgNumber,
    *,
    name: str,
    postcode: str | None = None,
    city: str | None = None,
    phone: str | None = None,
    address: str | None = None,
    legal_website: str = "",
) -> MatchScore:
    """Derive signals from a real page, then score it."""
    text = page.text
    folded = fold_name(text)
    company_key = fold_name(name)
    page_postcodes = extract_postcodes(text)

    def _has(value: str | None) -> bool:
        return bool(value) and str(value).casefold() in text.casefold()

    signals = MatchSignals(
        orgnr=org.value in page.org_numbers,
        exact_name=bool(name) and name.casefold() in text.casefold(),
        folded_name=bool(company_key) and company_key in folded,
        postcode=bool(postcode) and postcode in page_postcodes,
        address=bool(address) and _has(address),
        city=bool(city) and _has(city),
        domain=_domain_consistent(page.url, name, legal_website),
        phone=bool(phone) and re.sub(r"\D", "", str(phone)) in re.sub(r"\D", "", text),
    )
    return resolve(signals)


def best(scores: list[MatchScore]) -> MatchScore | None:
    """The strongest score, or ``None`` when the list is empty."""
    if not scores:
        return None
    return max(scores, key=lambda s: s.score)
