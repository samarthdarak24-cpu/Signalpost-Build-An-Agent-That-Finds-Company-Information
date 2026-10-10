"""Hiring & jobs: read open roles off a site that already proved ownership.

Jobs are explicitly part of the brief, and "Hiring & public activity" is one of
the five domains in the reference profile. The rule this module lives by is the
same one the rest of the web layer lives by, only sharper here:

    A job listing is only ever read from a page that passed the ownership gate.

A careers page on a host we could not tie to the company is a wrong-company
risk, and a job title attributed to the wrong employer is exactly the kind of
error the challenge treats as fatal. So this module never fetches: the caller
passes in the *accepted* pages, and everything here is a pure parse over them.

Two extraction paths, strongest first:

1. **JSON-LD ``JobPosting``** -- schema.org structured data, which is machine
   written and therefore carries an exact title, location and posting date.
2. **Anchor heuristics** -- links whose target or text look like a job listing,
   used only when there is no structured data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from normalize import Page, jsonld_objects, visible_text
from profiles import Fact

__all__ = ["CAREERS_PATHS", "JobPosting", "careers_urls", "extract_jobs", "job_facts"]

#: Paths a company's careers page most often lives at, English and Norwegian,
#: tried in order. Deliberately broad: a careers page is worth a cheap fetch and
#: the gate already guarantees we only read the company's own verified site.
CAREERS_PATHS: tuple[str, ...] = (
    "/karriere", "/jobs", "/jobber", "/careers", "/ledige-stillinger",
    "/stillinger", "/vacancies", "/work-with-us", "/work-with-us/jobs",
    "/om-oss/karriere", "/om-oss/jobb", "/om-oss/jobb-og-karriere",
    "/karriere/ledige-stillinger", "/careers/jobs", "/en/careers",
)

_JOB_HREF = re.compile(
    r"(?i)/(jobb|jobs|stilling|stillinger|career|careers|vacanc|ledige|apply|position)"
)
_JOB_WORDS = re.compile(
    r"(?i)\b(stilling|stillinger|jobb|jobber|ledig|ledige|søk|søknadsfrist|"
    r"vacancy|position|we are hiring|apply now)\b"
)
_ANCHOR = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>')


@dataclass(slots=True)
class JobPosting:
    """One open role, tied back to the page it was read from."""

    title: str
    url: str = ""
    location: str = ""
    posted: str = ""
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "url": self.url, "location": self.location,
            "posted": self.posted, "source_url": self.source_url,
        }


def careers_urls(base: str, paths: Iterable[str] = CAREERS_PATHS) -> list[str]:
    """Expand a verified site root into the careers paths worth fetching."""
    root = (base or "").rstrip("/")
    if not root:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        url = root + path
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            return obj[key]
    return None


def _job_location(obj: dict[str, Any]) -> str:
    loc = obj.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return ""
    address = loc.get("address") or {}
    if isinstance(address, list):
        address = address[0] if address else {}
    if isinstance(address, dict):
        parts = [address.get("addressLocality"), address.get("addressRegion"),
                 address.get("addressCountry")]
        return ", ".join(str(p) for p in parts if p)
    return str(loc.get("name") or "")


def _from_jsonld(markup: str, page_url: str) -> list[JobPosting]:
    out: list[JobPosting] = []
    for obj in jsonld_objects(markup):
        types = obj.get("@type")
        type_set = {types} if isinstance(types, str) else set(types or [])
        if "JobPosting" not in type_set:
            continue
        title = str(obj.get("title") or "").strip()
        if not title:
            continue
        out.append(
            JobPosting(
                title=title,
                url=str(obj.get("url") or page_url),
                location=_job_location(obj),
                posted=str(obj.get("datePosted") or "")[:10],
                source_url=page_url,
            )
        )
    return out


def _from_anchors(markup: str, page_url: str) -> list[JobPosting]:
    out: list[JobPosting] = []
    for match in _ANCHOR.finditer(markup):
        href = match.group(1).strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        text = visible_text(match.group(2)).strip()
        if not text or len(text) > 120:
            continue
        from urllib.parse import urljoin

        absolute = urljoin(page_url, href)
        if _JOB_HREF.search(href) or _JOB_WORDS.search(text):
            out.append(JobPosting(title=text, url=absolute, source_url=page_url))
    return out


def extract_jobs(pages: Iterable[Page], *, base_url: str = "") -> list[JobPosting]:
    """Read job postings from already-verified pages.

    JSON-LD wins when present; anchors fill in otherwise. Postings are deduped
    on (title, url) so a listing linked from several pages counts once.
    """
    postings: list[JobPosting] = []
    seen: set[tuple[str, str]] = set()

    def _add(items: list[JobPosting]) -> None:
        for item in items:
            key = (item.title.casefold(), item.url)
            if key in seen:
                continue
            seen.add(key)
            postings.append(item)

    for page in pages:
        markup = getattr(page, "markup", "") or ""
        page_url = getattr(page, "url", "") or base_url
        structured = _from_jsonld(markup, page_url)
        _add(structured)
        if not structured:
            _add(_from_anchors(markup, page_url))
    return postings


def job_facts(
    postings: Iterable[JobPosting],
    *,
    number: str,
    retrieved_at: str,
    source_url: str,
    careers_url: str = "",
    match_proof: str = "orgnr_on_page",
    layer: str = "web_rules",
) -> list[Fact]:
    """Turn postings into facts: careers page, open-role count, latest role.

    Nothing is published when there is nothing to publish: no careers page means
    no ``careers_page`` fact, and no postings means no ``open_roles`` fact. A
    zero would be a guess about the world, not a reading of the source.
    """
    postings = list(postings)
    facts: list[Fact] = []

    if careers_url:
        facts.append(
            Fact(
                field="careers_page", value=careers_url, source_url=source_url,
                retrieved_at=retrieved_at,
                evidence=f'careers page reached at "{careers_url}" on the verified site',
                match_proof=match_proof, confidence=0.9, layer=layer,
                method="careers path discovery on a verified site", org_number=number,
            )
        )

    if postings:
        facts.append(
            Fact(
                field="open_roles", value=len(postings), source_url=source_url,
                retrieved_at=retrieved_at,
                evidence=f"{len(postings)} open role(s) listed: "
                         + "; ".join(p.title for p in postings[:3]),
                match_proof=match_proof, confidence=0.88, layer=layer,
                method="job listings on the verified careers page", org_number=number,
            )
        )
        latest = max(postings, key=lambda p: p.posted or "")
        facts.append(
            Fact(
                field="latest_job_title", value=latest.title, source_url=latest.source_url or source_url,
                retrieved_at=retrieved_at, evidence=f'latest listing: "{latest.title}"',
                match_proof=match_proof, confidence=0.87, layer=layer,
                method="job listing title", org_number=number,
            )
        )
        if latest.posted:
            facts.append(
                Fact(
                    field="latest_job_date", value=latest.posted,
                    source_url=latest.source_url or source_url, retrieved_at=retrieved_at,
                    evidence=f'datePosted "{latest.posted}" for "{latest.title}"',
                    match_proof=match_proof, confidence=0.86, layer=layer,
                    method="job posting datePosted", org_number=number,
                )
            )
    return facts
