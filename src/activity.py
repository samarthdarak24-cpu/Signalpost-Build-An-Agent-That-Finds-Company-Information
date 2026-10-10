"""Public activity: dated company updates read off a verified site.

The reference profile lists "recent company updates" under Hiring & public
activity, and the brief scores useful company research -- so a dated, sourced
update is worth publishing. This module reads them from the same kind of
verified pages as :mod:`jobs`, and under the same rule: only a page that passed
the ownership gate, never an unverified host.

What counts as public activity here is deliberately narrow and factual:

*   a newsroom / press / blog post (``NewsArticle``, ``BlogPosting``, ...), and
*   a dated entry -- a ``<time datetime=...>`` element or a news-style link.

Each item keeps its publication date, so the profile can say *when* the update
happened rather than merely that one exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

from normalize import Page, jsonld_objects, visible_text
from profiles import Fact

__all__ = ["ACTIVITY_PATHS", "ActivityItem", "activity_urls", "extract_activity", "activity_facts"]

#: Where a company's news / press / blog listings usually live, English and
#: Norwegian, tried in order.
ACTIVITY_PATHS: tuple[str, ...] = (
    "/nyheter", "/news", "/aktuelt", "/press", "/presserom", "/presse",
    "/pressemeldinger", "/blogg", "/blog", "/nyhetsarkiv", "/media",
    "/om-oss/nyheter", "/aktuelt/nyheter", "/aktuelt/pressemeldinger",
    "/newsroom", "/insights",
)

#: schema.org types that represent a dated public update.
_ARTICLE_TYPES = {"NewsArticle", "BlogPosting", "Article", "PressRelease", "Report"}

_NEWS_HREF = re.compile(
    r"(?i)/(nyhet|nyheter|news|aktuelt|press|blogg|blog|innlegg|artikkel|article|story|"
    r"update|announcement)"
)
_TIME = re.compile(r'(?is)<time\b[^>]*datetime=["\']([^"\']+)["\'][^>]*>(.*?)</time>')
_ANCHOR = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>')


@dataclass(slots=True)
class ActivityItem:
    """One dated public update, tied to the page it was read from."""

    title: str
    date: str = ""
    url: str = ""
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "date": self.date, "url": self.url,
                "source_url": self.source_url}


def activity_urls(base: str, paths: Iterable[str] = ACTIVITY_PATHS) -> list[str]:
    """Expand a verified site root into the news/press paths worth fetching."""
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


def _from_jsonld(markup: str, page_url: str) -> list[ActivityItem]:
    out: list[ActivityItem] = []
    for obj in jsonld_objects(markup):
        types = obj.get("@type")
        type_set = {types} if isinstance(types, str) else set(types or [])
        if not (type_set & _ARTICLE_TYPES):
            continue
        title = str(obj.get("headline") or obj.get("name") or "").strip()
        if not title:
            continue
        date = str(obj.get("datePublished") or obj.get("dateModified") or "")[:10]
        out.append(ActivityItem(title=title, date=date,
                                url=str(obj.get("url") or page_url), source_url=page_url))
    return out


def _from_time_and_anchors(markup: str, page_url: str) -> list[ActivityItem]:
    out: list[ActivityItem] = []
    # Dated entries: a <time> element carries the date; its text is the title.
    for match in _TIME.finditer(markup):
        date = match.group(1).strip()[:10]
        title = visible_text(match.group(2)).strip()
        if title and len(title) <= 160:
            out.append(ActivityItem(title=title, date=date, url=page_url, source_url=page_url))
    # News-style links: a target that reads like a newsroom entry.
    for match in _ANCHOR.finditer(markup):
        href = match.group(1).strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        if not _NEWS_HREF.search(href):
            continue
        title = visible_text(match.group(2)).strip()
        if not title or len(title) > 160:
            continue
        out.append(ActivityItem(title=title, url=urljoin(page_url, href), source_url=page_url))
    return out


def extract_activity(pages: Iterable[Page], *, base_url: str = "") -> list[ActivityItem]:
    """Read dated public updates from already-verified pages."""
    items: list[ActivityItem] = []
    seen: set[tuple[str, str]] = set()

    def _add(batch: list[ActivityItem]) -> None:
        for item in batch:
            key = (item.title.casefold(), item.url)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

    for page in pages:
        markup = getattr(page, "markup", "") or ""
        page_url = getattr(page, "url", "") or base_url
        structured = _from_jsonld(markup, page_url)
        _add(structured)
        if not structured:
            _add(_from_time_and_anchors(markup, page_url))
    return items


def activity_facts(
    items: Iterable[ActivityItem],
    *,
    number: str,
    retrieved_at: str,
    source_url: str,
    match_proof: str = "orgnr_on_page",
    layer: str = "web_rules",
) -> list[Fact]:
    """Turn updates into facts: a count, and the most recent dated update."""
    items = list(items)
    if not items:
        return []

    facts: list[Fact] = [
        Fact(
            field="activity_count", value=len(items), source_url=source_url,
            retrieved_at=retrieved_at,
            evidence=f"{len(items)} dated public update(s) listed",
            match_proof=match_proof, confidence=0.86, layer=layer,
            method="public updates on the verified site", org_number=number,
        )
    ]
    latest = max(items, key=lambda i: i.date or "")
    facts.append(
        Fact(
            field="latest_update_title", value=latest.title,
            source_url=latest.source_url or source_url, retrieved_at=retrieved_at,
            evidence=f'latest public update: "{latest.title}"',
            match_proof=match_proof, confidence=0.85, layer=layer,
            method="most recent public update title", org_number=number,
        )
    )
    if latest.date:
        facts.append(
            Fact(
                field="latest_update_date", value=latest.date,
                source_url=latest.source_url or source_url, retrieved_at=retrieved_at,
                evidence=f'published "{latest.date}" for "{latest.title}"',
                match_proof=match_proof, confidence=0.85, layer=layer,
                method="publication date of the latest update", org_number=number,
            )
        )
    return facts
