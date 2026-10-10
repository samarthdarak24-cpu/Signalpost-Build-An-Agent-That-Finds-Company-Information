"""The browser-based live web layer: search the open web, keylessly.

Everything above this module answers from *authoritative* sources -- the register,
the accounts register, an ownership-gated company site. That is the right default
for a *company profile*, where a wrong fact attributed to a wrong company is worse
than a missing one. But the same discipline cannot answer "what has Equinor said
about the energy transition this year?", because no single register holds that.

So this module adds the missing leg, and nothing else: it runs a **real search
query against a real search engine over HTTP** (the same request a browser makes
to the HTML/Lite endpoint), then hands the ranked results back as *candidates*.
It does not pretend a search snippet is proof. A snippet is a lead; :mod:`.liveagent`
is what decides whether a lead became an answer, and every answer it gives still
carries the URL and the quote that a reader can click through and check.

Two engines are tried, in order, both without an API key:

    DuckDuckGo  https://html.duckduckgo.com/html/   (the no-JS HTML endpoint)
    Bing        https://www.bing.com/search          (server-rendered results)

Both are rendered server-side, so a plain ``urllib`` GET returns the same list a
browser would show — no JavaScript engine, no headless browser, no key. When
``BRAVE_API_KEY`` is set, the caller may prefer Brave (see :mod:`.sitefinder`);
this module is the keyless floor, so the demo still searches on a judge's laptop.

The one rule this module never breaks: **it is a fetcher, not a knower.** It
returns whatever the engine returned, attributed to where it came from, and marks
"the engine returned nothing" as a distinct, honest outcome rather than a blank.
"""

from __future__ import annotations

import html as _html
import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from net import HttpClient, HttpError

__all__ = [
    "SearchResult",
    "SearchOutcome",
    "SEARCH_ENGINES",
    "WIKIPEDIA_ENGINE",
    "search_web",
    "wikipedia_search",
    "wikipedia_summary",
    "general_search",
    "fetch_page_text",
    "strip_html",
    "rank_results",
    "is_challenge_page",
]

#: Hosts that are directories, agencies or social pages rather than a company's
#: own site. The canonical list lives in :mod:`normalize` so the pre-filter, the
#: gate and this ranker cannot drift apart; a drift is how a directory page gets
#: promoted above the company it indexes.
from normalize import DIRECTORY_HOSTS as _CANONICAL_DIRECTORY_HOSTS

_DIRECTORY_HOSTS = set(_CANONICAL_DIRECTORY_HOSTS)

#: The browser User-Agent. A search engine serves the rendered result list to a
#: browser and refuses a bare bot string, so we present as a browser would. This
#: is not deception of a person -- it is asking for the same public page -- and we
#: still send no cookies and obey ``robots.txt`` on the result pages we then read.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Engines tried in order, keyless first so the demo works with no key at all.
#: Each entry: (name, url_template). Only the first engine that yields results is
#: used. When ``BRAVE_API_KEY`` is set the caller should pass the Brave engine
#: first (see :func:`brave_engine`) -- it is a real API, unthrottled and stable.
SEARCH_ENGINES: tuple[tuple[str, str], ...] = (
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={q}&kl=no-no"),
    ("mojeek", "https://www.mojeek.com/search?q={q}"),
    ("bing", "https://www.bing.com/search?q={q}&setlang=nb&cc=NO"),
)

#: Brave's web-search API, used as the primary engine when a key is present.
BRAVE_ENGINE: tuple[str, str] = ("brave", "https://api.search.brave.com/res/v1/web/search?q={q}&count=10&country=NO")

#: Wikipedia's open search API. No key, no throttle, no rate limit worth fearing;
#: it is the one *general-knowledge* source that is always available, so it backs
#: the live agent when every web engine is throttling. It answers "what is X" and
#: "who is Y" for anything with an article, and cites itself by construction.
WIKIPEDIA_ENGINE: tuple[str, str] = (
    "wikipedia",
    "https://{lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}"
    "&format=json&srlimit=7&srprop=snippet",
)

#: Wikipedia language editions tried, in order. Norwegian first for Norwegian
#: companies, then English for breadth.
WIKIPEDIA_LANGS: tuple[str, ...] = ("no", "en")

#: Substrings that mark an anti-bot / challenge interstitial rather than results.
#: A live agent must never present a captcha page as "no results", or a throttled
#: moment looks like a fact about the world.
_CHALLENGE_MARKERS = (
    "<title>captcha", "captcha - ", "enable javascript and cookies",
    "unusual traffic", "are you a robot", "verify you are human",
    "detected unusual", "checking your browser", "cf-browser-verification",
)


def is_challenge_page(status: int, markup: str) -> bool:
    """True when a response is an anti-bot interstitial, not a result page.

    Engines answer a burst of scripted queries with HTTP 202 or a "Captcha"
    title. Treating that as a real empty result set would silently misreport a
    throttle as a fact, so it is detected explicitly and surfaced as its own
    outcome.
    """
    if status in (202, 429, 403):
        return True
    head = markup[:4000].casefold()
    return any(marker in head for marker in _CHALLENGE_MARKERS)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(fragment: str) -> str:
    """Turn a snippet of markup into readable text, entities decoded.

    Search result titles and snippets come back with the engine's highlighting
    markup (``<b>``) and HTML entities. A reader wants the text, so we drop the
    tags and decode the entities -- but the *link* is kept verbatim elsewhere, so
    attribution never depends on this lossy step.
    """
    if not fragment:
        return ""
    text = _TAG.sub(" ", fragment)
    text = _html.unescape(text)
    return _WS.sub(" ", text).strip()


@dataclass(slots=True)
class SearchResult:
    """One ranked result: where it points, what it said, and how it was found."""

    title: str
    url: str
    snippet: str = ""
    rank: int = 0
    engine: str = ""
    #: The raw href as the engine returned it, before redirect unwrapping. Kept so
    #: an auditor can see the link was not invented.
    raw_href: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "url": self.url, "snippet": self.snippet,
            "rank": self.rank, "engine": self.engine,
        }


@dataclass(slots=True)
class SearchOutcome:
    """The result of one search attempt, including the honest empty case."""

    query: str
    results: list[SearchResult] = field(default_factory=list)
    engine: str = ""
    engines_tried: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "engine": self.engine,
            "engines_tried": self.engines_tried,
            "count": len(self.results),
            "results": [r.to_dict() for r in self.results],
            "error": self.error,
        }


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.casefold().removeprefix("www.")
    except Exception:
        return ""


def _is_directory(host: str) -> bool:
    host = host.removeprefix("www.")
    return any(
        host == h.removeprefix("www.") or host.endswith("." + h.removeprefix("www."))
        for h in _DIRECTORY_HOSTS
    )


def rank_results(
    results: list[SearchResult],
    *,
    prefer_host: str = "",
    company_name: str = "",
) -> list[SearchResult]:
    """Reorder results so an owned page outranks a directory listing.

    A search for a company name returns the register, Proff, orgnummer.no and
    Wikipedia before it returns the company itself, because those pages are
    optimised for exactly that query. For *this* task that ordering is wrong: a
    directory page tells us nothing the register does not, while the company's
    own site does. So a non-directory host is promoted, a host whose label
    matches the company name is promoted further, and ``rank`` is rewritten to
    reflect the new order so downstream confidence arithmetic stays consistent.
    """
    name_key = re.sub(r"[^a-z0-9]", "", (company_name or "").casefold())

    def score(result: SearchResult) -> tuple[int, int]:
        host = _host_of(result.url)
        penalty = 1 if _is_directory(host) else 0
        owned = 0
        if name_key:
            label = host.split(".")[0]
            if label and (label in name_key or name_key in label):
                owned = -1
        return (penalty + owned, result.rank)

    ordered = sorted(results, key=score)
    for new_rank, result in enumerate(ordered):
        result.rank = new_rank
    return ordered


def _unwrap_duckduckgo(href: str) -> str:
    """``//duckduckgo.com/l/?uddg=<enc>`` -> the real destination.

    DuckDuckGo wraps every result in a redirector. The destination is the
    percent-encoded ``uddg`` parameter; unwrapping it gives the URL a reader can
    click and an auditor can re-fetch.
    """
    if "uddg=" not in href:
        return href
    query = urllib.parse.urlparse(href).query
    target = urllib.parse.parse_qs(query).get("uddg", [""])[0]
    return target or href


def _unwrap_bing(href: str) -> str:
    """``bing.com/ck/a?...&u=a1<base64url>`` -> the real destination.

    Bing's redirector base64url-encodes the destination after a two-character
    prefix (``a1``). If the decode fails we keep the original href rather than
    invent one -- a broken link we report beats a link we fabricated.
    """
    if "bing.com/ck/a" not in href:
        return href
    raw = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
    if not raw:
        return href
    import base64

    payload = raw[2:] if raw.startswith("a1") else raw
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8", "replace")
    except Exception:
        return href
    return decoded if decoded.startswith("http") else href


# ------------------------------------------------------------------- parsing


def _parse_duckduckgo(markup: str) -> list[SearchResult]:
    """Parse the DDG HTML endpoint's result list."""
    out: list[SearchResult] = []
    # Each result: an <a class="result__a" href="...">Title</a>, then a snippet.
    anchors = re.findall(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        markup, re.S,
    )
    snippets = re.findall(
        r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', markup, re.S,
    )
    for rank, (href, title) in enumerate(anchors):
        url = _unwrap_duckduckgo(_html.unescape(href))
        snippet = strip_html(snippets[rank]) if rank < len(snippets) else ""
        out.append(SearchResult(
            title=strip_html(title), url=url, snippet=snippet,
            rank=rank, engine="duckduckgo", raw_href=href,
        ))
    return out


def _parse_bing(markup: str) -> list[SearchResult]:
    """Parse Bing's server-rendered result list (``li.b_algo`` blocks)."""
    out: list[SearchResult] = []
    blocks = re.findall(r'<li class="b_algo".*?</li>', markup, re.S)
    for rank, block in enumerate(blocks):
        m = re.search(r'<h2>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href, title = m.group(1), m.group(2)
        snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        out.append(SearchResult(
            title=strip_html(title),
            url=_unwrap_bing(_html.unescape(href)),
            snippet=strip_html(snip.group(1)) if snip else "",
            rank=rank, engine="bing", raw_href=href,
        ))
    return out


_PARSERS = {"duckduckgo": _parse_duckduckgo, "bing": _parse_bing}


def _parse_mojeek(markup: str) -> list[SearchResult]:
    """Parse Mojeek's organic results (an independent, keyless index).

    Mojeek renders each result as ``<li><h2><a href="...">Title</a></h2> ...
    <p class="s">Snippet</p>``. It is a smaller index than DuckDuckGo's but it is
    an entirely separate crawler, so it is a genuine fallback rather than a
    reskin of the same upstream.
    """
    out: list[SearchResult] = []
    blocks = re.findall(r'<li>\s*<h2>.*?</li>', markup, re.S)
    for rank, block in enumerate(blocks):
        m = re.search(r'<h2>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href, title = m.group(1), m.group(2)
        if href.startswith(("/", "#")):
            continue
        snip = re.search(r'<p[^>]*class="s"[^>]*>(.*?)</p>', block, re.S)
        out.append(SearchResult(
            title=strip_html(title), url=_html.unescape(href),
            snippet=strip_html(snip.group(1)) if snip else "",
            rank=rank, engine="mojeek", raw_href=href,
        ))
    return out


def _parse_brave(payload_text: str) -> list[SearchResult]:
    """Parse the Brave web-search JSON API response."""
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return []
    out: list[SearchResult] = []
    for rank, item in enumerate(((payload.get("web") or {}).get("results")) or []):
        out.append(SearchResult(
            title=strip_html(item.get("title", "")),
            url=item.get("url", ""),
            snippet=strip_html(item.get("description", "")),
            rank=rank, engine="brave", raw_href=item.get("url", ""),
        ))
    return out


_PARSERS.update({"mojeek": _parse_mojeek, "brave": _parse_brave})


# --------------------------------------------------------------------- search


_CACHE_DIR = Path("data") / "search_cache"


def _cache_path(query: str, engine: str) -> Path:
    key = hashlib.sha256(f"{engine}\x00{query}".encode()).hexdigest()[:24]
    return _CACHE_DIR / f"{key}.json"


def _cache_get(query: str, engine: str) -> list[SearchResult] | None:
    path = _cache_path(query, engine)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return [SearchResult(**item) for item in blob.get("results", [])]


def _cache_put(query: str, engine: str, results: list[SearchResult]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(query, engine).write_text(
            json.dumps({"query": query, "engine": engine,
                        "results": [r.to_dict() for r in results]}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _brave_headers() -> dict[str, str] | None:
    """Brave API headers when ``BRAVE_API_KEY`` is set, else None."""
    import os

    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not key:
        return None
    return {"Accept": "application/json", "X-Subscription-Token": key}


def search_web(
    query: str,
    *,
    client: HttpClient | None = None,
    engines: tuple[tuple[str, str], ...] | None = None,
    limit: int = 10,
    company_name: str = "",
    cache: bool = True,
    settle_seconds: float = 0.0,
) -> SearchOutcome:
    """Search the open web for ``query`` and return ranked, attributed results.

    Engine order: Brave first *when a key is set* (a real, unthrottled API), then
    the keyless engines in turn. The first engine that yields parsed results wins,
    so a healthy primary costs one request.

    Three behaviours separate this from a naive scraper, and they matter for a
    *live* agent:

    * **Results are cached on disk.** A repeated question in a live session does
      not re-hit an engine, which both speeds the follow-up and keeps us polite.
    * **A challenge page is never an empty result set.** An HTTP 202/429 or a
      "Captcha" interstitial is recorded as ``throttled`` and the next engine is
      tried, so a throttled moment is never misreported as "the web has nothing".
    * **Every attempt is named.** ``engines_tried`` lists what was tried and
      ``error`` carries the last reason, so the outcome is auditable.
    """
    outcome = SearchOutcome(query=query)
    query = (query or "").strip()
    if not query:
        outcome.error = "empty query"
        return outcome

    chain: list[tuple[str, str]] = list(engines) if engines is not None else list(SEARCH_ENGINES)
    brave = _brave_headers()
    if brave and not any(name == "brave" for name, _ in chain):
        chain.insert(0, BRAVE_ENGINE)

    client = client or HttpClient()
    throttled: list[str] = []
    for name, template in chain:
        if cache:
            cached = _cache_get(query, name)
            if cached:
                outcome.results = rank_results(cached, company_name=company_name)[:limit]
                outcome.engine = name
                outcome.engines_tried.append(name)
                return outcome

        url = template.format(q=urllib.parse.quote_plus(query))
        outcome.engines_tried.append(name)
        headers = dict(brave) if (name == "brave" and brave) else \
            {"User-Agent": BROWSER_UA, "Accept": "text/html"}
        try:
            response = client.get(
                url, use_cache=False, check_robots=False, headers=headers,
            )
        except HttpError as exc:
            outcome.error = f"{name}: HTTP {exc.status}"
            continue
        except Exception as exc:  # network-level; try the next engine
            outcome.error = f"{name}: {type(exc).__name__}"
            continue

        if is_challenge_page(response.status, response.text):
            throttled.append(name)
            outcome.error = f"{name}: throttled (anti-bot challenge)"
            continue

        parser = _PARSERS.get(name)
        results = parser(response.text) if parser else []
        if results:
            if cache:
                _cache_put(query, name, results)
            outcome.results = rank_results(
                results[:limit], company_name=company_name)[:limit]
            outcome.engine = name
            outcome.error = ""
            break

    if not outcome.results:
        if throttled and not outcome.error:
            outcome.error = "throttled: " + ", ".join(throttled)
        elif not outcome.error:
            outcome.error = "no engine returned results"
    return outcome


def fetch_page_text(url: str, *, client: HttpClient | None = None, limit: int = 8000) -> str:
    """Fetch ``url`` and return readable text (tags stripped), up to ``limit``.

    Used to promote a *result* into *evidence*: a snippet is a lead, but the page
    it points to is what a quote can be verified against. Returns ``""`` on any
    failure -- an unreadable page is a gap, never a guess.
    """
    client = client or HttpClient()
    try:
        response = client.get(url, headers={"User-Agent": BROWSER_UA})
    except Exception:
        return ""
    markup = response.text
    # Strip script/style bodies first, then tags.
    markup = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    text = strip_html(markup)
    return text[:limit]


# ------------------------------------------------------------------- wikipedia


def wikipedia_summary(title: str, *, client: HttpClient | None = None, lang: str = "no") -> str:
    """The plain-text extract of a Wikipedia article, via the REST summary API.

    The article HTML is heavy and contains nav chrome; the REST summary endpoint
    returns the lead paragraph as clean text, which is exactly what a quote needs.
    Keyless and stable.
    """
    client = client or HttpClient()
    url = (f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
           f"{urllib.parse.quote(title.replace(' ', '_'))}")
    try:
        response = client.get(url, use_cache=False, check_robots=False,
                              headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
        payload = json.loads(response.text)
    except Exception:
        return ""
    return strip_html(payload.get("extract", ""))[:1500]


def wikipedia_search(
    query: str,
    *,
    client: HttpClient | None = None,
    langs: tuple[str, ...] = WIKIPEDIA_LANGS,
    limit: int = 7,
) -> SearchOutcome:
    """Search Wikipedia's open API. Keyless, unthrottled, and always available.

    This is the live agent's dependable general-knowledge leg. It cannot answer a
    question about a company that has no article, but for anything notable it
    gives an attributed, quotable source with no key and no rate limiting -- the
    exact fallback the keyless engines cannot promise. The article *title* and the
    *snippet* come back together, so a result is its own citation.
    """
    outcome = SearchOutcome(query=query)
    query = (query or "").strip()
    if not query:
        outcome.error = "empty query"
        return outcome

    client = client or HttpClient()
    for lang in langs:
        url = WIKIPEDIA_ENGINE[1].format(lang=lang, q=urllib.parse.quote_plus(query))
        outcome.engines_tried.append(f"wikipedia:{lang}")
        try:
            response = client.get(
                url, use_cache=False, check_robots=False,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            )
            payload = json.loads(response.text)
        except Exception as exc:
            outcome.error = f"wikipedia:{lang}: {type(exc).__name__}"
            continue
        hits = ((payload.get("query") or {}).get("search")) or []
        for rank, hit in enumerate(hits):
            title = hit.get("title", "")
            page = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            outcome.results.append(SearchResult(
                title=title,
                url=page,
                snippet=strip_html(hit.get("snippet", "")),
                rank=rank, engine=f"wikipedia:{lang}", raw_href=page,
            ))
        if outcome.results:
            outcome.engine = f"wikipedia:{lang}"
            outcome.error = ""
            outcome.results = outcome.results[:limit]
            break
    if not outcome.results and not outcome.error:
        outcome.error = "wikipedia: no article matched"
    return outcome


def general_search(
    query: str,
    *,
    client: HttpClient | None = None,
    limit: int = 10,
    company_name: str = "",
    prefer_wikipedia: bool = False,
) -> SearchOutcome:
    """Search the web engines, then Wikipedia, so a question always has a source.

    The web engines give the broadest coverage but throttle and are flaky; the
    Wikipedia API is narrower but never unavailable. Running them as one chain --
    engines first (better answers when they work), Wikipedia last (guaranteed) --
    means the live agent degrades from *best available* to *always available*
    instead of failing outright. The chosen engine is reported either way, so the
    reader always knows which source spoke.

    With ``prefer_wikipedia`` the order flips for definitional questions ("what is
    X?"), where an encyclopedia's lead paragraph is a better answer than a
    company's own homepage navigation.
    """
    if prefer_wikipedia:
        wiki = wikipedia_search(query, client=client, limit=min(limit, 7))
        if wiki.results:
            return wiki

    out = search_web(query, client=client, limit=limit, company_name=company_name)
    if out.results:
        return out
    wiki = wikipedia_search(query, client=client, limit=min(limit, 7))
    if wiki.results:
        wiki.engines_tried = out.engines_tried + wiki.engines_tried
        return wiki
    # Neither worked: return the web attempt with the Wikipedia reason appended.
    out.engines_tried = out.engines_tried + wiki.engines_tried
    out.error = (out.error or "no results") + f"; {wiki.error}"
    return out