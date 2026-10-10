"""Phase 2 — Redis pub/sub fan-out (optional, graceful).

When ``REDIS_URL`` is set, every detected ``ChangeRecord`` is published on a
channel so multiple dashboard sessions or worker processes can subscribe without
each tailing ``changes.jsonl``. When it is unset — the keyless demo — every
function here is a silent no-op, so the live layer degrades cleanly to the
file-tail SSE used in Phase 1 and the core agent stays dependency-free.

This is the only place that touches Redis; nothing in the ingest or process path
imports it, so a missing broker can never break a run.
"""

from __future__ import annotations

import json
import os
from typing import Any

CHANNEL = "signalpost:changes"

_CLIENT: Any = None  #: redis client, False once we know it is unavailable


def _client() -> Any:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    url = os.environ.get("REDIS_URL")
    if not url:
        _CLIENT = False
        return None
    try:
        import redis

        _CLIENT = redis.Redis.from_url(url, socket_connect_timeout=2)
        _CLIENT.ping()
    except Exception:
        _CLIENT = False
    return _CLIENT


def publish_change(change: dict[str, Any]) -> bool:
    """Publish a change record. Returns True if it actually went to Redis."""
    client = _client()
    if client is None:
        return False
    try:
        client.publish(CHANNEL, json.dumps(change, ensure_ascii=False))
        return True
    except Exception:
        return False


def subscribe() -> Any:
    """Return a Redis pubsub object subscribed to the changes channel, or None."""
    client = _client()
    if client is None:
        return None
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    return pubsub


def available() -> bool:
    return _client() is not None
