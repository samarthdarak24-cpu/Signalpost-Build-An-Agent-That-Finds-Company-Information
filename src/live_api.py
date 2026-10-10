"""Phase 1 — the real-time bridge.

A small FastAPI service that turns the Phase 0 watchdog into a *live* feed the
dashboard can subscribe to:

    INGEST   httpx + aiolimiter poll oppdateringer/enheter (rate-limited, async)
    PROCESS  asyncio.to_thread(process_orgnrs) reuses the exact Phase 0 pipeline
    DELIVER  sse-starlette streams every appended ChangeRecord to /api/stream/{orgnr}

It is an OPTIONAL companion process. The competition agent (``python run.py run``)
stays stdlib-only; this server is launched separately:

    uvicorn src.live_api:app --port 8799 --reload

Dependencies live in a dedicated venv (see requirements-live.txt) so the core
path never grows a third-party footprint.

Endpoints
    GET /api/health            process + feed status
    GET /api/live/status       watermark, last poll, events seen, interval
    GET /api/envelopes         every envelope (one line per input)
    GET /api/envelope/{orgnr}  a single envelope
    GET /api/changes?orgnr=&limit=   recent change records
    GET /api/stream/{orgnr}    Server-Sent Events of new changes (orgnr="*" = all)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from fastapi import FastAPI, Request  # noqa: E402
from sse_starlette.sse import EventSourceResponse  # noqa: E402
import httpx  # noqa: E402

from live import (  # noqa: E402
    CHANGES_PATH,
    DEFAULT_INTERVAL,
    DEFAULT_PER_TICK,
    ENVELOPES_PATH,
    WATERMARK_PATH,
    changed_orgnrs,
    load_watermark,
    process_orgnrs,
    save_watermark,
    seek_tail,
)
from net import USER_AGENT  # noqa: E402
from budget import RunBudget, BudgetExceeded  # noqa: E402
from profiles import utcnow_iso  # noqa: E402
from registry import BASE, BrregClient  # noqa: E402

#: The async feed path must answer to the *same* accountant as the sync pipeline.
#: Before this existed, ``collect_changes_async`` made up to 20 httpx requests per
#: 60-second tick with no budget calls at all -- traffic that counted toward the
#: provider's limits while being invisible to the 2,000-request ceiling. It gets
#: its own :class:`RunBudget` because it is a long-lived background poller rather
#: than a single evaluation run, but it is now bounded and reportable.
FEED_BUDGET = RunBudget(run_id="live-feed", max_seconds=float("inf"))

__all__ = ["app"]

#: Process-wide live state, mutated by the poll loop and read by the API.
STATE: dict[str, Any] = {
    "watermark": 0,
    "last_poll": "",
    "events_seen": 0,
    "interval": DEFAULT_INTERVAL,
    "per_tick": DEFAULT_PER_TICK,
    "no_web": True,
    "no_llm": True,
}

HTTP_CLIENT = None
LIMITER = None


# --------------------------------------------------------------------- async ingest


async def collect_changes_async(
    client: "Any",
    limiter: "Any",
    last_cursor: int,
    page_size: int = 500,
    max_pages: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """Async equivalent of ``live.collect_changes`` using httpx + aiolimiter.

    ``aiolimiter`` enforces a polite request rate so a tight poll loop never
    hammers Brreg; the offset ceiling (``page * size < 10000``) is honoured to
    avoid HTTP 400, and we stop on the first short page.
    """
    events: list[dict[str, Any]] = []
    cursor = last_cursor
    budget = FEED_BUDGET
    for page in range(max_pages):
        if page * page_size >= 10000:
            break
        # Respect the feed's own budget before every page, exactly as the sync
        # client does. A refusal ends the tick cleanly; the cursor is not advanced
        # past data we did not read, so the next tick resumes correctly.
        try:
            budget.begin(kind="registry")
        except BudgetExceeded as exc:
            STATE["last_feed_refusal"] = str(exc)
            break
        async with limiter:
            try:
                resp = await client.get(
                    f"{BASE}/oppdateringer/enheter",
                    params={"oppdateringsid": last_cursor, "page": page, "size": page_size},
                )
            except Exception as exc:
                budget.note_failure()
                STATE["last_feed_refusal"] = f"feed transport error: {exc}"
                break
        if resp.status_code != 200:
            budget.note_failure()
            break
        budget.note_success()
        payload = resp.json()
        batch = (payload.get("_embedded") or {}).get("oppdaterteEnheter", [])
        if not batch:
            break
        for ev in batch:
            events.append(ev)
            cid = ev.get("oppdateringsid")
            if isinstance(cid, int) and cid > cursor:
                cursor = cid
        if len(batch) < page_size:
            break
        if len(events) >= 10000:
            # Safety net: never replay more than 10k events in a single tick even
            # if the feed returns oversized pages; the next tick continues from cursor.
            break
    return events, cursor


async def poll_once() -> None:
    """One scheduled tick: poll the feed, re-validate, advance the watermark."""
    global HTTP_CLIENT, LIMITER
    cursor = STATE["watermark"]
    events, new_cursor = await collect_changes_async(HTTP_CLIENT, LIMITER, cursor)
    orgnrs = changed_orgnrs(events)
    if orgnrs:
        # pipeline.run is sync and I/O bound; run it off the event loop.
        await asyncio.to_thread(
            process_orgnrs,
            orgnrs,
            no_web=STATE["no_web"],
            no_llm=STATE["no_llm"],
            limit=STATE["per_tick"],
            verbose=True,
        )
    STATE["watermark"] = new_cursor
    STATE["last_poll"] = utcnow_iso()
    STATE["events_seen"] += len(events)
    save_watermark(new_cursor)


# --------------------------------------------------------------------- SSE delivery


async def stream_changes(orgnr: str) -> AsyncIterator[dict[str, Any]]:
    """Yield change records appended to changes.jsonl after subscription time.

    Tailing the file (rather than an in-process queue) keeps the bridge decoupled
    from the sync pipeline: anything that appends to changes.jsonl — Phase 0's
    daemon or this server — is streamed to subscribers for free.
    """
    seen = 0
    last_beat = time.monotonic()
    try:
        while True:
            if CHANGES_PATH.exists():
                lines = CHANGES_PATH.read_text(encoding="utf-8").splitlines()
                for line in lines[seen:]:
                    seen += 1
                    if not line.strip():
                        continue
                    try:
                        change = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if orgnr == "*" or change.get("orgnr") == orgnr:
                        yield {"event": "change", "data": json.dumps(change, ensure_ascii=False)}
            if time.monotonic() - last_beat > 15:
                yield ": ping"  # SSE comment keeps the connection alive
                last_beat = time.monotonic()
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return


# --------------------------------------------------------------------- app + routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    global HTTP_CLIENT, LIMITER
    from aiolimiter import AsyncLimiter  # local import keeps core stdlib-clean

    STATE["watermark"] = load_watermark() or seek_tail(BrregClient())
    HTTP_CLIENT = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT})
    LIMITER = AsyncLimiter(max_rate=10, time_period=1)
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_once, "interval", seconds=STATE["interval"])
    scheduler.start()
    app.state.scheduler = scheduler
    print(f"[live-api] watching feed from oppdateringsid={STATE['watermark']}; polling every {STATE['interval']}s")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await HTTP_CLIENT.aclose()


app = FastAPI(title="Signalpost Live", version="1.0", lifespan=lifespan)


# The console is served by the stdlib server on :8787 while this bridge runs on
# :8799, so the browser treats the change stream as a cross-origin request. Only
# the read-only live endpoints are exposed, and only to local origins: this is a
# local demo bridge, not a public service, and the core agent is unaffected.
_ALLOWED_ORIGINS = {
    "http://127.0.0.1:8787", "http://localhost:8787",
    "http://127.0.0.1:8799", "http://localhost:8799",
}


@app.middleware("http")
async def allow_local_console(request: Request, call_next):
    """Permit the locally served console to read the live stream."""
    response = await call_next(request)
    origin = request.headers.get("origin", "")
    if origin in _ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    if request.method == "OPTIONS":
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "signalpost-live",
        "watermark": STATE["watermark"],
        "last_poll": STATE["last_poll"],
        "events_seen": STATE["events_seen"],
        "interval": STATE["interval"],
        # Feed traffic is now budgeted, so report it rather than leaving it
        # invisible: these are the requests the async path actually made.
        "feed_budget": FEED_BUDGET.to_dict(),
        "last_feed_refusal": STATE.get("last_feed_refusal", ""),
    }


@app.get("/api/live/status")
async def live_status() -> dict[str, Any]:
    return dict(STATE)


@app.get("/api/envelopes")
async def envelopes(limit: int = 1000) -> list[dict[str, Any]]:
    if not ENVELOPES_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in ENVELOPES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
        if len(out) >= limit:
            break
    return out


@app.get("/api/envelope/{orgnr}")
async def envelope(orgnr: str) -> dict[str, Any] | dict[str, str]:
    if not ENVELOPES_PATH.exists():
        return {"error": "no envelopes yet"}
    for line in ENVELOPES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        env = json.loads(line)
        if env.get("orgnr") == orgnr:
            return env
    return {"error": "not found"}


@app.get("/api/changes")
async def changes(orgnr: str = "", limit: int = 100) -> list[dict[str, Any]]:
    if not CHANGES_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(CHANGES_PATH.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            change = json.loads(line)
        except json.JSONDecodeError:
            continue
        if orgnr and change.get("orgnr") != orgnr:
            continue
        out.append(change)
        if len(out) >= limit:
            break
    return out


@app.post("/api/poll")
async def trigger_poll(limit: int = DEFAULT_PER_TICK) -> dict[str, Any]:
    """Force a tick now (otherwise the scheduler fires every ``STATE['interval']`` s)."""
    STATE["per_tick"] = limit
    before = STATE["events_seen"]
    await poll_once()
    return {"ok": True, "events_seen_before": before, "events_seen_after": STATE["events_seen"],
            "watermark": STATE["watermark"], "last_poll": STATE["last_poll"]}


@app.get("/api/analytics")
async def analytics() -> dict[str, Any]:
    """Phase 2 — coverage / status / field-frequency aggregates (DuckDB)."""
    from analytics import analytics_bundle

    return analytics_bundle()


@app.get("/api/stream/{orgnr}")
async def stream(request: Request, orgnr: str) -> EventSourceResponse:
    """Server-Sent Events of change records for ``orgnr`` (``*`` = all)."""
    return EventSourceResponse(stream_changes(orgnr))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("src.live_api:app", host="127.0.0.1", port=8799, log_level="info")
