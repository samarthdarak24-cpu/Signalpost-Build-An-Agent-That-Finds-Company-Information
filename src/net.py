"""HTTP plumbing shared by every source client.

One place for the rules the brief cares about: an identifying User-Agent, a disk
cache keyed by URL hash so development re-runs are free, jittered backoff on the
retryable status codes, and a request budget that winds down rather than dying
mid-run.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error as _ue
import urllib.request as _u
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from budget import (
    CALL_KINDS,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_USD,
    BudgetExceeded,
    RunBudget,
)

if TYPE_CHECKING:
    pass
from urllib.parse import urlparse

__all__ = ["Budget", "HttpError", "HttpResponse", "HttpClient"]

RETRYABLE = {429, 500, 502, 503, 504}

USER_AGENT = os.environ.get(
    "SIGNALPOST_UA",
    "SignalpostEvidenceEngine/1.0 (+https://github.com/signalpost; contact: submit@builderr.ai)",
)


class HttpError(RuntimeError):
    """Non-retryable transport failure.

    The reason is included in the message, not just stored on the attribute: a
    budget refusal, a robots.txt disallow and a 404 are very different problems,
    and a caller reading only ``str(exc)`` must be able to tell them apart.
    """

    def __init__(self, status: int, url: str, body: str = "") -> None:
        detail = f": {body[:200]}" if body else ""
        super().__init__(f"HTTP {status} for {url}{detail}")
        self.status = status
        self.url = url
        self.body = body[:400]


# ``BudgetExceeded`` is imported from ``budget`` rather than redefined here. Two
# distinct classes with the same name meant a refusal raised by the accountant was
# not caught by the transport's own handler, so a refused request escaped as an
# unhandled exception instead of being reported as a failed fetch.


def _budget_exceeded(exc: Exception, url: str) -> "HttpError":
    """Re-raise a budget refusal as a transport-level error.

    The pipeline already treats :class:`HttpError` as "this fetch did not happen",
    which is precisely what a refused request is. Mapping it here keeps a budget
    refusal from being mistaken for a network fault in the reports.
    """
    return HttpError(0, url, f"budget refused: {exc}")


#: Host fragments mapped to the budget operation they belong to. Ordered longest
#: suffix first so ``api.search.brave.com`` wins over a bare ``brave.com`` rule.
#: Without this, every fetch is reported as generic ``web_request`` traffic and
#: the per-provider report cannot show which service the requests went to.
_HOST_KINDS: tuple[tuple[str, str], ...] = (
    ("data.brreg.no", "registry"),
    ("brreg.no", "registry"),
    ("api.search.brave.com", "search_brave"),
    ("api.gleif.org", "gleif"),
    ("query.wikidata.org", "wikidata"),
    ("wikipedia.org", "wikipedia"),
    ("api.anthropic.com", "llm"),
    ("html.duckduckgo.com", "search_keyless"),
    ("mojeek.com", "search_keyless"),
    ("bing.com", "search_keyless"),
)


def _infer_kind(url: str) -> str:
    """The budget operation a URL belongs to, by host."""
    host = (urlparse(url).netloc or "").casefold()
    for fragment, kind in _HOST_KINDS:
        if host == fragment or host.endswith("." + fragment):
            return kind
    return "web_request"


#: Host fragment -> the provider label used when a call is attributed by host
#: rather than by operation. Keeps robots.txt traffic counted against the service
#: it was actually sent to, instead of a vague "generic" bucket.
def _provider_for(url: str) -> str:
    return CALL_KINDS.get(_infer_kind(url), {}).get("provider", "generic")


@dataclass
class Budget:
    """The run's request/cost budget, as seen by :class:`HttpClient`.

    This is a thin, backwards-compatible facade over :class:`budget.RunBudget`,
    which owns the counters, the lock, the deadline and the cost accounting.
    Keeping one accountant matters: the previous implementation kept a bare
    ``int`` here and a *separate* dead controller in ``budget.py`` whose default
    caps contradicted the competition rules.

    ``max_requests`` is the enforceable ceiling; ``soft_limit`` is the point at
    which the pipeline winds down gracefully so it can still finish in-flight
    work rather than dying mid-company.
    """

    soft_limit: int = int(os.environ.get("SIGNALPOST_SOFT_LIMIT", "1700"))
    hard_limit: int = int(os.environ.get("SIGNALPOST_HARD_LIMIT", "2000"))
    max_seconds: float = float(
        os.environ.get("SIGNALPOST_MAX_SECONDS", str(DEFAULT_MAX_SECONDS))
    )
    max_usd: float = float(os.environ.get("SIGNALPOST_MAX_USD", str(DEFAULT_MAX_USD)))
    _budget: "RunBudget" = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._budget is None:
            # The soft limit is expressed as RunBudget's reserve: winding down
            # begins once fewer than (hard - soft) requests remain.
            reserve = max(0, self.hard_limit - self.soft_limit)
            fraction = (reserve / self.hard_limit) if self.hard_limit else 0.0
            self._budget = RunBudget(
                max_requests=self.hard_limit,
                max_seconds=self.max_seconds,
                max_usd=self.max_usd,
                reserve_fraction=fraction,
            )

    # -- counters, under the names the rest of the codebase already uses
    @property
    def used(self) -> int:
        return self._budget.requests_used

    @property
    def search_calls(self) -> int:
        return self._budget.by_operation.get("search_brave", 0) + self._budget.by_operation.get(
            "search_keyless", 0
        )

    @search_calls.setter
    def search_calls(self, value: int) -> None:
        # Legacy code increments this directly; keep it meaningful rather than
        # letting a shadow counter drift away from the real one.
        if value > self.search_calls:
            self._budget.note_internal("search_legacy_increment")

    @property
    def llm_input_tokens(self) -> int:
        return self._budget.input_tokens

    @llm_input_tokens.setter
    def llm_input_tokens(self, value: int) -> None:
        if value > self._budget.input_tokens:
            self._budget.input_tokens = value

    @property
    def llm_output_tokens(self) -> int:
        return self._budget.output_tokens

    @llm_output_tokens.setter
    def llm_output_tokens(self, value: int) -> None:
        if value > self._budget.output_tokens:
            self._budget.output_tokens = value

    @property
    def cost_usd(self) -> float:
        return self._budget.cost_usd

    def spend(self, n: int = 1, kind: str = "web_request") -> None:
        """Account for ``n`` outbound attempts, refusing past the hard limit."""
        for _ in range(n):
            if not self._budget.allow(kind) or self._budget.requests_used >= self.hard_limit:
                raise BudgetExceeded(
                    f"hard request limit reached ({self.hard_limit}); "
                    f"{self._budget.requests_used} already spent"
                )
            self._budget.spend(kind)

    @property
    def winding_down(self) -> bool:
        return self._budget.winding_down

    @property
    def run(self) -> "RunBudget":
        """The underlying accountant, for callers that want the full report."""
        return self._budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests_used": self._budget.requests_used,
            "requests_soft_limit": self.soft_limit,
            "requests_hard_limit": self.hard_limit,
            "search_calls": self.search_calls,
            "llm_input_tokens": self._budget.input_tokens,
            "llm_output_tokens": self._budget.output_tokens,
            "cost_usd": self.cost_usd,
            "elapsed_seconds": round(self._budget.elapsed, 2),
            "max_seconds": self.max_seconds,
            "max_usd": self.max_usd,
        }


@dataclass(slots=True)
class HttpResponse:
    url: str
    status: int
    text: str
    headers: dict[str, str]
    from_cache: bool = False
    elapsed_ms: int = 0

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "ignore")).hexdigest()[:32]


class HttpClient:
    """A small, polite, cached HTTP client built on the standard library.

    Using ``urllib`` rather than ``requests`` keeps ``make setup`` to zero
    third-party packages for the core path, which matters when the judge has one
    command and no network patience.
    """

    def __init__(
        self,
        cache_dir: Path | str = "data/cache",
        budget: Budget | None = None,
        *,
        timeout: float = 20.0,
        spacing_ms: int = 120,
        user_agent: str = USER_AGENT,
        robots_aware: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.budget = budget or Budget()
        self.timeout = timeout
        self.spacing_ms = spacing_ms
        self.user_agent = user_agent
        self.robots_aware = robots_aware
        self._last_call = 0.0
        self._robots: dict[str, bool] = {}

    # ---------------------------------------------------------------- cache

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"

    def _read_cache(self, key: str) -> HttpResponse | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return HttpResponse(
            url=blob["url"], status=blob["status"], text=blob["text"],
            headers=blob.get("headers", {}), from_cache=True,
        )

    def _write_cache(self, key: str, response: HttpResponse) -> None:
        if response.status >= 400:
            return
        self._cache_path(key).write_text(
            json.dumps({"url": response.url, "status": response.status,
                        "text": response.text, "headers": response.headers}),
            encoding="utf-8",
        )

    # -------------------------------------------------------------- robots

    def allowed_by_robots(self, url: str) -> bool:
        """Best-effort robots.txt check, cached per host.

        We fetch the file once per host and look for a blanket ``Disallow: /``.
        Full directive matching is more machinery than it is worth here; the
        conservative reading is that a blanket disallow means we skip the site
        entirely and fall back to the register and search snippets.
        """
        if not self.robots_aware:
            return True
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return True
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host in self._robots:
            return self._robots[host]
        verdict = True
        try:
            # Attributed as its own operation: a robots.txt fetch is real outbound
            # traffic and must be visible in the report, not hidden inside the
            # page fetch that triggered it.
            response = self.get(
                f"{host}/robots.txt", use_cache=True, check_robots=False, kind="robots"
            )
            text = response.text.lower()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("user-agent:") and line.split(":", 1)[1].strip() == "*":
                    continue
                if line.startswith("disallow:") and line.split(":", 1)[1].strip() == "/":
                    verdict = False
                    break
        except Exception:
            verdict = True  # absence or failure == allowed
        self._robots[host] = verdict
        return verdict

    # ------------------------------------------------------------ requests

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        use_cache: bool = True,
        check_robots: bool = True,
        max_retries: int = 3,
        kind: str | None = None,
    ) -> HttpResponse:
        """GET ``url`` with caching, spacing, retries and budget accounting.

        A ``304 Not Modified`` is returned as-is (with empty text) rather than
        raised, because the delta engine needs to distinguish "unchanged" from
        "failed".

        ``kind`` names the budget operation so traffic is attributed to its real
        provider rather than a generic bucket. Callers that know what they are
        fetching should pass it; the default is inferred from the host, so a Brreg
        call is never silently reported as generic web traffic.
        """
        if check_robots and not self.allowed_by_robots(url):
            raise HttpError(451, url, "robots.txt disallows this path")

        operation = kind or _infer_kind(url)

        cache_key = json.dumps(
            {"url": url, "headers": {k.lower(): v for k, v in (headers or {}).items()}},
            sort_keys=True,
        )
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                # A cache hit is not a network request: it must not consume the
                # outbound budget, but it must be visible in the report so the two
                # are never conflated.
                self.budget.run.note_cache_hit()
                return cached

        last_error: Exception | None = None
        for attempt in range(max_retries):
            # Reserve *before* the socket. Accounting after the call would let an
            # over-limit request reach the network and only then be counted, which
            # is exactly the guarantee the 2,000 ceiling is supposed to give.
            try:
                self.budget.run.begin(
                    kind=operation,
                    provider_override=_provider_for(url) if operation != _infer_kind(url) else "",
                )
            except BudgetExceeded as exc:
                raise _budget_exceeded(exc, url) from exc
            if attempt:
                self.budget.run.note_retry()
            delay = self.spacing_ms / 1000.0
            elapsed = time.monotonic() - self._last_call
            if elapsed < delay:
                time.sleep(delay - elapsed)
            started = time.monotonic()
            try:
                request_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
                request_headers.update({k: v for k, v in (headers or {}).items()})
                request = _u.Request(url, headers=request_headers)
                with _u.urlopen(request, timeout=self.timeout) as handle:
                    raw = handle.read().decode("utf-8", "replace")
                    final_url = getattr(handle, "url", url) or url
                    response = HttpResponse(
                        url=url,
                        status=int(getattr(handle, "status", 200)),
                        text=raw,
                        headers={k.lower(): v for k, v in handle.headers.items()},
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                self._last_call = time.monotonic()
                # urllib follows redirects internally, so one logical GET may have
                # been several socket exchanges. We cannot count each hop, but we
                # can make the undercount visible instead of silent.
                self.budget.run.note_success(requested=url, redirected_from=final_url)
                if use_cache:
                    self._write_cache(cache_key, response)
                return response
            except _ue.HTTPError as exc:
                self._last_call = time.monotonic()
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if exc.code == 304:
                    self.budget.run.note_success()
                    return HttpResponse(url=url, status=304, text="",
                                        headers={k.lower(): v for k, v in (exc.headers or {}).items()})
                if exc.code == 404:
                    self.budget.run.note_failure()
                    raise HttpError(404, url, body) from exc
                if exc.code in RETRYABLE and attempt < max_retries - 1:
                    self.budget.run.note_failure()
                    last_error = HttpError(exc.code, url, body)
                    time.sleep(min(8.0, (2 ** attempt) * 0.5) * (0.5 + random.random()))
                    continue
                self.budget.run.note_failure()
                raise HttpError(exc.code, url, body) from exc
            except Exception as exc:  # network-level failure, worth one more try
                self._last_call = time.monotonic()
                last_error = exc
                self.budget.run.note_failure()
                if attempt < max_retries - 1:
                    time.sleep(min(8.0, (2 ** attempt) * 0.5) * (0.5 + random.random()))
                    continue
                raise HttpError(0, url, str(exc)) from exc
        raise HttpError(0, url, str(last_error))

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return json.loads(self.get(url, **kwargs).text)

    def probe(self, url: str, *, etag: str | None, last_modified: str | None) -> HttpResponse:
        """Conditional GET used by the delta engine.

        304 comes back in milliseconds and costs no content transfer, which is
        how the second and later runs stay cheap on unchanged pages.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return self.get(url, headers=headers, use_cache=False)
