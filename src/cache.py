"""A research cache keyed on the whole question, not just the URL.

The existing HTTP cache is keyed on a URL, which is right for a *page* but wrong
for a *research answer*. The same page can answer "what is the email" and "what
is the phone" differently, and the same question can be answered by two models
with two different results. So the cache key here is the full tuple the spec
names:

    namespace + orgnr + source_url + content_hash + field + model

which has two consequences worth stating plainly:

*   **A changed page invalidates only what it should.** When the content hash
    moves, every answer derived from the old hash misses and is recomputed; the
    answers derived from other pages are untouched.
*   **A model change invalidates model answers only.** Bumping the extraction
    model misses the ``llm`` entries and leaves the deterministic parses intact.

It is deliberately the same shape for every namespace -- registry, GLEIF,
Wikidata, search, HTML, parsed fields, LLM extraction -- so there is one cache,
one key function and one eviction story rather than seven.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

__all__ = ["ResearchCache", "cache_key", "NAMESPACES"]

#: The answer families the cache holds. Kept explicit so a typo in a namespace
#: is caught here rather than silently creating a parallel cache.
NAMESPACES = (
    "registry", "gleif", "wikidata", "search", "html", "field", "llm",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_cache (
    key TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    payload TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_ns ON research_cache(namespace);
"""


def cache_key(
    namespace: str,
    *,
    orgnr: str = "",
    url: str = "",
    content_hash: str = "",
    field: str = "",
    model: str = "",
) -> str:
    """A stable key over the full question tuple.

    Any component that is not supplied is simply absent from the key, so a
    registry lookup keyed only on the organisation number never collides with a
    page extraction keyed on the URL *and* the content hash.
    """
    parts = [namespace, orgnr, url, content_hash, field, model]
    blob = "\x1f".join(parts)
    return f"{namespace}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


class ResearchCache:
    """SQLite-backed get-or-compute cache with per-namespace stats."""

    def __init__(self, path: Path | str = "data/research_cache.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.hits: dict[str, int] = {}
        self.misses: dict[str, int] = {}

    # --------------------------------------------------------------- basics
    def get(self, key: str) -> Any | None:
        row = self._conn.execute(
            "SELECT payload FROM research_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            return None

    def put(self, key: str, value: Any, *, namespace: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO research_cache (key, namespace, payload, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "payload = excluded.payload, created_at = excluded.created_at",
                (key, namespace or key.split(":", 1)[0],
                 json.dumps(value, ensure_ascii=False), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
            self._conn.commit()

    # ------------------------------------------------------ get-or-compute
    def remember(
        self,
        namespace: str,
        *,
        producer: Callable[[], Any],
        orgnr: str = "",
        url: str = "",
        content_hash: str = "",
        field: str = "",
        model: str = "",
        use: bool = True,
    ) -> Any:
        """Return the cached answer, or compute, store and return it.

        ``use=False`` bypasses the read (always recompute) but still writes, which
        is what a forced refresh wants.
        """
        key = cache_key(namespace, orgnr=orgnr, url=url,
                        content_hash=content_hash, field=field, model=model)
        if use:
            cached = self.get(key)
            if cached is not None:
                self.hits[namespace] = self.hits.get(namespace, 0) + 1
                return cached
        self.misses[namespace] = self.misses.get(namespace, 0) + 1
        value = producer()
        if value is not None:
            self.put(key, value, namespace=namespace)
        return value

    # ---------------------------------------------------------------- stats
    def count(self, namespace: str | None = None) -> int:
        if namespace:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM research_cache WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM research_cache").fetchone()
        return int(row["n"])

    def stats(self) -> dict[str, Any]:
        return {
            "entries": self.count(),
            "hits": dict(self.hits),
            "misses": dict(self.misses),
            "namespaces": {ns: self.count(ns) for ns in NAMESPACES},
        }

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()
