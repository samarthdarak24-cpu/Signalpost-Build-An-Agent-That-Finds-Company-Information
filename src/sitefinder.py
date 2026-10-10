"""Website discovery: registry first, search only when the registry is silent.

The register carries a ``hjemmeside`` field for roughly a third of active
limited companies. When it is present it is free and authoritative, so it is
always tried first. Only when it is absent do we pay for a search query — and
that query is built to be as discriminating as possible by including the
organisation number, which turns a fuzzy name search into an exact one.

Two engines are available, and the difference between them is the whole reason
web-only coverage was once 0.0% with no key configured:

* **Brave** (``search_brave``) — a real, unthrottled, *paid* API. The single
  ``search_attempt`` path used to be Brave-only, so with no ``BRAVE_API_KEY``
  every website lookup returned ``skipped`` and the batch pipeline discovered
  nothing outside the register.
* **The keyless stack** (``search_keyless``) — ``liveweb.search_web``'s
  DuckDuckGo / Mojeek / Bing chain, which needs no key at all and already
  distinguishes ``ok`` from ``empty`` from ``throttled``.

The fixed ``search_attempt`` tries Brave when a key is present and falls back to
the keyless chain otherwise, so the keyless demo can discover websites *and* the
paid path remains the fast one. Search snippets are still only *candidates*:
``candidates()`` output must survive the ownership gate before anything becomes
a fact, so a weak engine yields no fact rather than a misattributed one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

import liveweb
from net import HttpClient, Budget
from normalize import fold_name, extract_postcodes, DIRECTORY_HOSTS
from validate import OrgNumber

__all__ = ["Candidate", "SearchAttempt", "SiteFinder", "PAGE_PATHS", "normalize_url"]

BASE = "https://api.search.brave.com/res/v1/web/search"

#: Pages worth fetching to establish ownership and lift contact data. Home is
#: first because it is the page most likely to carry the organisation number,
#: and the footer of a Norcontact block is usually repeated everywhere.
PAGE_PATHS: tuple[str, ...] = ("", "/kontakt", "/kontakt-oss", "/om-oss", "/contact", "/about")

#: Hosts that appear in search results but are never the company's own site.
#: The list itself lives in :mod:`normalize` so the pre-filter, the gate and the
#: live-web ranker cannot disagree about what a directory is.
_DENY_HOSTS = set(DIRECTORY_HOSTS)

#: Hosts that are almost certainly a company's own site even without the number.
_WEAK_ACCEPT_TLDS = (".no", ".com", ".org", ".net", ".io", ".co", ".eu")


@dataclass(slots=True)
class Candidate:
    """A possible company website and where it came from."""

    url: str
    origin: str  #: "registry" | "brave" | "duckduckgo" | "mojeek" | "bing" | "keyless"
    title: str = ""
    snippet: str = ""
    rank: int = 0
    score: float = 0.0
    rejected_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "origin": self.origin, "title": self.title,
            "snippet": self.snippet, "rank": self.rank,
            "score": round(self.score, 3), "rejected_reason": self.rejected_reason,
        }


@dataclass(slots=True)
class SearchAttempt:
    """One search attempt and what actually happened to it.

    The whole point of this type is that an *empty* result set and a *failed*
    lookup are different facts that must not share a representation. A search
    that never ran because no key was configured, a search the API refused, and
    a search that ran and found nothing all previously returned ``[]`` -- so a
    profile could not tell "this company has no website" from "we could not
    look". The brief forbids treating a missing result as proof a fact is
    false, and this type is what makes that rule enforceable downstream.

    ``ok`` is deliberately *not* "no error": it means results were returned. A
    clean zero-result answer is ``ok=False, error=""``.
    """

    query: str
    results: list[dict[str, Any]] = field(default_factory=list)
    engine: str = "brave"
    error: str = ""
    #: "ok" | "empty" | "skipped" | "failed" -- the single word a caller can
    #: branch on without string-matching the error text.
    status: str = "empty"

    @property
    def ok(self) -> bool:
        return bool(self.results)

    @property
    def failed(self) -> bool:
        """True when the search did not complete, as opposed to completing empty."""
        return self.status in ("failed", "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "engine": self.engine,
            "status": self.status,
            "count": len(self.results),
            "error": self.error,
        }


def normalize_url(raw: str) -> str:
    """Add a scheme, drop a trailing slash, keep the registrable part.

    The register stores bare hosts (``havas.no``); search results come back as
    full URLs. Both must land on the same normal form or the dedupe fails.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.netloc.casefold().removeprefix("www.")
    if "@" in host or not host:
        return ""
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", parsed.query, ""))


def _registrable(url: str) -> str:
    host = urlparse(url).netloc.casefold().removeprefix("www.")
    return host


class SiteFinder:
    """Finds candidate websites for a company, cheapest source first."""

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        brave_key: str | None = None,
        keyless: bool = True,
        max_queries: int = 4,
    ) -> None:
        self.http = client or HttpClient()
        self.brave_key = brave_key if brave_key is not None else os.environ.get("BRAVE_API_KEY", "")
        #: Whether to fall back to the keyless engine chain when Brave is not
        #: configured or does not answer. Default on, so the keyless demo can
        #: actually discover websites; set False to test the paid path alone.
        self.keyless = keyless
        #: Upper bound on how many distinct queries we will issue for one company.
        #: A strict, documented ceiling so query expansion can never blow the
        #: 2,000-request budget. Set to 0 to disable search entirely.
        self.max_queries = max(0, int(max_queries))
        #: The most recent search, kept so the pipeline can record *why* a
        #: website was not found rather than silently reporting absence.
        self.last_search: SearchAttempt | None = None

    # ------------------------------------------------------------ registry

    @staticmethod
    def from_registry(entity: dict[str, Any]) -> Candidate | None:
        raw = (entity.get("hjemmeside") or "").strip()
        if not raw:
            return None
        url = normalize_url(raw)
        if not url:
            return None
        return Candidate(url=url, origin="registry", title=entity.get("navn", ""), score=1.0)

    # -------------------------------------------------------------- search

    def search_attempt(self, query: str, *, count: int = 10, country: str = "NO") -> SearchAttempt:
        """Run one search and report *what happened*, not just what came back.

        Brave is tried when a key is configured; the keyless engine chain is
        tried otherwise, and also as a fallback when Brave was configured but
        could not answer. Every exit path is classified, so a caller can never
        mistake "we did not look" for "we looked and there was nothing":

        * no key and no keyless fallback -> ``status="skipped"``
        * budget winding down            -> ``status="skipped"``
        * transport/API error            -> ``status="failed"``, with the reason
        * answered, no hits              -> ``status="empty"``, ``error=""``
        * answered with hits             -> ``status="ok"``
        """
        if not self.brave_key and not self.keyless:
            return SearchAttempt(
                query=query, status="skipped",
                error="no BRAVE_API_KEY configured, so no search was performed",
            )

        if self.brave_key:
            attempt = self._search_brave(query, count=count, country=country)
            # A refusal or a transport failure is exactly the case the keyless
            # chain exists to cover. An *empty* Brave answer is a real answer
            # and is returned as-is rather than second-guessed.
            if attempt.status in ("ok", "empty"):
                return attempt
            if not self.keyless:
                return attempt
            fallback_reason = attempt.error
        else:
            fallback_reason = "no BRAVE_API_KEY configured"

        keyless = self._search_keyless(query, count=count)
        if keyless.status == "skipped" and fallback_reason:
            # Both routes were unavailable: report the *reason the first one was
            # skipped*, since that is the actionable one (i.e. no key).
            keyless.error = f"{fallback_reason}; {keyless.error}" if keyless.error else fallback_reason
        return keyless

    def _search_brave(self, query: str, *, count: int, country: str) -> SearchAttempt:
        """The paid Brave API path, with its own cost reservation."""
        if self.http.budget.winding_down:
            return SearchAttempt(
                query=query, status="skipped",
                error="request budget is winding down, so the search was not performed",
            )
        # Brave is a *paid* provider. Charge it at the documented per-query price
        # and refuse the call if the listing price is unknown, so the $10 ceiling
        # cannot be quietly exceeded by an unpriced search.
        run = self.http.budget.run
        unit = run.price("brave")
        if unit < 0 and run.strict_cost:
            return SearchAttempt(
                query=query, status="skipped",
                error="refusing paid search: no documented price for the brave provider",
            )
        try:
            run.begin(kind="search_brave", cost=max(0.0, unit))
        except Exception as exc:  # BudgetExceeded from any layer
            return SearchAttempt(
                query=query, status="skipped",
                error=f"budget refused the search: {exc}",
            )
        try:
            payload = self.http.get_json(
                f"{BASE}?q={_urlencode(query)}&count={count}&country={country}&search_lang=no",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.brave_key,
                },
            )
        except Exception as exc:  # noqa: BLE001 - the reason is the whole point
            run.note_failure()
            return SearchAttempt(
                query=query, status="failed",
                error=f"brave search failed: {type(exc).__name__}: {exc}",
            )
        run.note_success()
        if unit > 0:
            run.charge("search_brave", cost_usd=unit)
        results = ((payload.get("web") or {}).get("results")) or []
        return SearchAttempt(
            query=query, results=results,
            status="ok" if results else "empty", error="",
        )

    def _search_keyless(self, query: str, *, count: int) -> SearchAttempt:
        """The free DuckDuckGo/Mojeek/Bing chain from :mod:`liveweb`.

        Free to the wallet but still real outbound requests. Those requests are
        accounted for *inside* :func:`liveweb.search_web`, which goes through
        ``HttpClient.get`` -- each engine attempt is one counted request, and
        each is attributed to ``search_keyless`` by host.

        So this method must **not** call ``run.begin`` itself: that would count
        the same engine attempt twice (once as a pre-admission here, once as the
        real ``client.get``), which is exactly the multi-layer double-counting
        the brief forbids. It uses :meth:`allow` instead -- a pure check that
        records a refusal without spending -- so the lookup still fails fast
        when the budget is exhausted, without inflating the request count.
        """
        if self.http.budget.winding_down:
            return SearchAttempt(
                query=query, engine="keyless", status="skipped",
                error="request budget is winding down, so the search was not performed",
            )
        run = self.http.budget.run
        if not run.allow("search_keyless"):
            return SearchAttempt(
                query=query, engine="keyless", status="skipped",
                error="budget refused the search: no request budget left",
            )
        try:
            outcome = liveweb.search_web(
                query, client=self.http, limit=count, cache=True, settle_seconds=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - the reason is the whole point
            return SearchAttempt(
                query=query, engine="keyless", status="failed",
                error=f"keyless search failed: {type(exc).__name__}: {exc}",
            )
        # liveweb already noted success/failure on each engine request via
        # HttpClient.get; a throttled attempt is recorded there as a failure and
        # an answered engine as a success. Only the *aggregate* classification is
        # left to do here.
        engine = outcome.engine or "keyless"
        results = [r.to_dict() for r in outcome.results]
        if results:
            return SearchAttempt(query=query, results=results, engine=engine,
                                 status="ok", error="")
        # No results came back. Distinguish "the engines answered and had
        # nothing" (a genuine empty web -- status "empty") from "we could not
        # look" (status "failed"). Only the latter is a failure. ``search_web``
        # emits the sentinel "no engine returned results" when every tried engine
        # responded cleanly with zero hits, so that is the honest empty case.
        error = outcome.error or ""
        if "throttled" in error or error.endswith("HTTP 429"):
            return SearchAttempt(query=query, engine=engine, status="failed", error=error)
        if error and error != "no engine returned results":
            # Any other engine error (timeout, 5xx, DNS, connection refused)
            # means the lookups did not complete, so the search failed rather
            # than came back empty.
            return SearchAttempt(query=query, engine=engine, status="failed", error=error)
        return SearchAttempt(query=query, engine=engine, status="empty", error="")

    def _search(self, query: str, *, count: int = 10, country: str = "NO") -> list[dict[str, Any]]:
        """Backwards-compatible view of :meth:`search_attempt`.

        Kept so existing callers keep working; new callers should use
        ``search_attempt`` when the *reason* for an empty list matters.
        """
        return self.search_attempt(query, count=count, country=country).results

    def from_search(
        self,
        entity: dict[str, Any],
        org: OrgNumber,
    ) -> list[Candidate]:
        """Try several queries and return the first one that yields results.

        Query shape is the single biggest lever on web-only coverage
        (Task #48). The historical query included the organisation number
        (``"Name" "orgno"`` / ``Name organisasjonsnummer orgno``); that query is
        *optimised to return directory pages*, because directories are exactly
        the sites that index a company by its number, and the ownership gate
        correctly rejects every one of them. So including the number produced a
        clean zero official-site hits.

        The queries below lead with the company **name**, which is what surfaces
        the company's own homepage as the top hit. The number-bearing query is
        retained as a last-resort fallback (it is the only one that can still
        disambiguate a name that is also a common word), but it is never tried
        first. Every improvement here is purely about *which pages get fetched*;
        the gate, the directory deny-list and the candidate scoring are left
        exactly as they were, so a directory page can still never become a
        company website.
        """
        name = (entity.get("navn") or "").strip()
        if not name:
            self.last_search = SearchAttempt(
                query="", status="skipped",
                error="the entity has no name, so there was nothing to search for",
            )
            return []

        addr = entity.get("forretningsadresse") or {}
        city = (addr.get("poststed") or "").strip()
        postcode = (addr.get("postnummer") or "").strip()

        #: Candidate queries, in priority order. Deduped and capped by
        #: ``max_queries`` so query expansion cannot exhaust the request budget.
        raw_queries = [
            name,                                      # bare name -> official site is top hit
            f"{name} hjemmeside",                      # explicit "website" intent
            (f"{name} {city}" if city else None),       # name + locality disambiguates
            f'"{name}" "{org.display}"',                # identifier query (last resort)
            f"{name} organisasjonsnummer {org.value}",  # number query (last resort)
        ]
        seen: set[str] = set()
        queries: list[str] = []
        for q in raw_queries:
            q = (q or "").strip()
            if not q or q in seen:
                continue
            seen.add(q)
            queries.append(q)
        if self.max_queries:
            queries = queries[: self.max_queries]

        results: list[dict[str, Any]] = []
        self.last_search = None
        for query in queries:
            attempt = self.search_attempt(query)
            # The last attempt wins, so a run that tries several queries and
            # fails them all reports the most recent failure rather than a stale
            # success.
            self.last_search = attempt
            if attempt.results:
                results = attempt.results
                break
        origin = self.last_search.engine if self.last_search else "search"

        addr = entity.get("forretningsadresse") or {}
        city = (addr.get("poststed") or "").casefold()
        postcode = addr.get("postnummer") or ""
        name_key = fold_name(name)

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for rank, item in enumerate(results):
            url = normalize_url(item.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            host = _registrable(url)
            title = item.get("title") or ""
            description = item.get("description") or ""

            candidate = Candidate(
                url=url, origin=origin, title=title, snippet=description, rank=rank
            )

            if any(host == d or host.endswith("." + d) for d in _DENY_HOSTS):
                candidate.rejected_reason = "directory or social host, not a company site"
                candidates.append(candidate)
                continue
            if not host.endswith(_WEAK_ACCEPT_TLDS):
                candidate.rejected_reason = f"host suffix not recognised: {host}"
                candidates.append(candidate)
                continue

            blob = f"{title} {description} {url}".casefold()
            score = 0.0
            if org.value in re.sub(r"\D", "", blob):
                score += 0.6
            if name_key and name_key in fold_name(f"{title} {description}"):
                score += 0.25
            if city and city in blob:
                score += 0.1
            if postcode and postcode in blob:
                score += 0.1
            score -= rank * 0.02
            candidate.score = max(score, 0.0)

            # A search snippet is not evidence, so this is a *candidate* filter
            # only. The gate still has to pass on the real page.
            if candidate.score < 0.2:
                candidate.rejected_reason = "search snippet did not corroborate name or number"
            candidates.append(candidate)

        candidates.sort(key=lambda c: (-c.score, c.rank))
        return candidates

    # ------------------------------------------------------------- combined

    def candidates(self, entity: dict[str, Any], org: OrgNumber) -> list[Candidate]:
        """Registry candidate (if any) followed by surviving search candidates."""
        out: list[Candidate] = []
        registry = self.from_registry(entity)
        if registry:
            out.append(registry)
        for candidate in self.from_search(entity, org):
            if not candidate.rejected_reason:
                out.append(candidate)
        return out


def _urlencode(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def page_urls(base: str, paths: Iterable[str] = PAGE_PATHS) -> list[str]:
    """Expand a site root into the small set of pages worth fetching."""
    root = base.rstrip("/")
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        url = root + path
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
