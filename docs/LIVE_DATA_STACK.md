# Live Data Processing for Signalpost — Open-Source Stack Research

**Goal:** Identify actively-maintained, GitHub-available open-source projects that can be
integrated into the Signalpost evidence agent to keep company profiles *current* and to
push fresh evidence to the dashboard in (near) real time.

**Why this matters for the Builderr rubric (verified 50/30/12/8):**

- **Recall (50)** — a profile is only as good as its freshest fetch. A delta engine that
  re-validates on registry change keeps coverage from silently rotting.
- **Synthesis (12)** — the envelope already carries a `changes` list. A live change-stream
  is the natural source for that field and for the "Change Stream" UI screen.
- **Ease of use / UX (8)** — pushing updates over SSE/WebSocket means the dashboard updates
  without a manual refresh, which is exactly the "live" impression judges reward.

> **Constraint reminder (Oct 8 feedback):** the judged artifact is the *working agent*, not
> the UI. The live layer is a force-multiplier on recall + synthesis, not a replacement for a
> correct registry-first pipeline. Do **not** build chatbots, 3D, blockchain, recommendation
> engines, CRM, or multi-tenant SaaS. Keep the demo keyless.

---

## 1. What "live" means here — the three-layer loop

```
        INGEST                  PROCESS                  DELIVER
  ┌────────────────┐      ┌─────────────────┐      ┌──────────────────┐
  │ Detect that a  │      │ Re-fetch entity │      │ Push the new     │
  │ watched orgnr  │ ---> │ + roles + subs  │ ---> │ profile / change │
  │ changed        │      │ + regnskap      │      │ event to dashboard│
  │ (CDC cursor)   │      │ Diff vs stored  │      │ (SSE / WS)       │
  └────────────────┘      └─────────────────┘      └──────────────────┘
```

- **Ingest** = discover *which* organisasjonsnummer changed and *when*.
- **Process** = re-run the existing pipeline for just those numbers, then `deepdiff` the new
  envelope against the stored one.
- **Deliver** = stream the resulting `Change` to every connected dashboard session.

---

## 2. Verified ingestion source — Brreg change feed (no webhook exists)

Brreg is **pull/poll only**. There is **no webhook, SSE, or WebSocket** from Brreg. The
correct live-ingestion primitive is the *oppdateringer* (change-event) feed, which I confirmed
live against `https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter`:

```json
{
  "_links": { "enhet": { "href": "https://data.brreg.no/enhetsregisteret/api/enheter/913189892" } },
  "dato": "2018-04-23T06:03:29.443Z",
  "endringstype": "Ukjent",
  "oppdateringsid": 1,
  "organisasjonsnummer": "913189892"
}
```

- Response is **HAL**-wrapped: the list is at `_embedded.oppdaterteEnheter`; paging metadata at
  `page.{number,size,totalElements,totalPages}`.
- **`oppdateringsid` is a monotonic cursor.** `?oppdateringsid={LAST}&page={n}&size={m}` returns
  only events after that id. This is the CDC anchor — store the highest id seen, poll forward.
- `totalElements` across history ≈ **16,515,241** events, so you MUST use the cursor; never
  page from 0.
- `endringstype` ∈ `Ny | Endret | Slettet | Ukjent` tells you whether to upsert or mark
  `not_available`/`failed`.

**Ingestion algorithm (stdlib-friendly, no new dependency required):**

```
last_id = load("watermark.txt")          # highest oppdateringsid processed
loop every POLL_SECONDS:
    page = 0
    changed = set()
    while True:
        batch = GET(oppdateringer/enheter?oppdateringsid=last_id&page=page&size=500)
        for ev in batch._embedded.oppdaterteEnheter:
            changed.add(ev.organisasjonsnummer)
            last_id = max(last_id, ev.oppdateringsid)
        if page+1 >= batch.page.totalPages: break
        page += 1
    for org in changed: enqueue(org)      # -> PROCESS layer
    save("watermark.txt", last_id)
```

This already runs today inside `src/registry.py::BrregClient.changes()` — the research below
is about *hardening and extending* it.

---

## 3. Tooling catalog (all actively maintained, with docs)

Grouped by the layer they serve. Each entry: repo, what it does, how it supports live data,
maintenance signal, and integration approach for Signalpost.

### 3.1 Ingestion — HTTP client & conditional GET

| Project | Repo | How it helps live data | Status |
|---|---|---|---|
| **httpx** | https://github.com/encode/httpx | Async HTTP/2 client; send `If-None-Match`/`If-Modified-Since` to honor `ETag`/`Last-Modified` and turn unchanged re-fetches into cheap `304`s. `asyncio` fan-out across 1000 orgs. | Very active (encode org); docs python-httpx.org |
| **aiohttp** | https://github.com/aio-libs/aiohttp | Alternative async client; native SSE *client* consumer if any upstream ever streams. | Active (aio-libs) |
| **aiolimiter** | https://github.com/mjpieters/aiolimiter | Leaky-bucket rate limiter for asyncio — polite polling of Brreg without tripping their rate limits. | Active; 1.3.x, readthedocs docs |

**Integration:** Replace the stdlib `http.client` in `BrregClient` with `httpx.AsyncClient`,
cache `ETag` per orgnr, and wrap calls in `AsyncLimiter(max_rate=…)`. Keeps the demo keyless.

### 3.2 Process — diff / delta engine

| Project | Repo | How it helps live data | Status / Caveat |
|---|---|---|---|
| **deepdiff** | https://github.com/qlustered/deepdiff *(repo moved from `seperman/deepdiff`)* | Nested dict diff of old vs new envelope → structured `Change` (field, before, after). Drives the `changes` field + Change Stream UI. | ⚠️ **CVE-2025-58367 (RCE)** — pin to a patched release and never `eval`/`deepdiff` untrusted code; or use the stdlib fallback below. |
| **datasketch** | https://github.com/ekzhu/datasketch | MinHash/LSH for *probabilistic* change detection across very large entity sets (useful only if you scale past ~10k orgs). | Active; 2.x docs at ekzhu.com |
| **stdlib fallback** | `json` + `difflib` | For the competition scope (1000 orgs), a 15-line recursive dict compare is enough and has **zero** supply-chain risk. Recommended for the demo build. | Built-in |

**Integration:** `changes = DeepDiff(old_env, new_env, verbose_level=2)` → map to the existing
`Change` dataclass. If avoiding the CVE, write `delta.py::diff_envelopes(a, b)` with stdlib.

### 3.3 Scheduling — when to poll / re-validate

| Project | Repo | How it helps live data | Status |
|---|---|---|---|
| **APScheduler** | https://github.com/agronholm/apscheduler | Cron + interval triggers; `AsyncIOScheduler` runs inside the same event loop as httpx. Drives the poll loop and periodic full re-validation. v4.0 rewritten, async-native. | Active (author of anyio/uvicorn) |
| **schedule** | https://github.com/dbader/schedule | Tiny, readable in-process loop if you want zero config. | Active but single-threaded |
| **Dagster** | https://github.com/dagster-io/dagster | Asset-based orchestration: "materialize profile if source changed". Overkill for 1000 orgs but excellent if this becomes a product. | Very active |

**Integration:** `AsyncIOScheduler.add_job(watchdog_poll, "interval", seconds=60)`. Keep the
watermark in `runs/watermark.txt`.

### 3.4 Local store / instant query

| Project | Repo | How it helps live data | Status |
|---|---|---|---|
| **DuckDB** | https://github.com/duckdb/duckdb | Embedded columnar SQL; `SELECT * FROM envelopes.jsonl WHERE status='available'` is instant, no server. v1.5.x adds stronger JSON; v2.0 in dev. | Very active; duckdb.org |
| **SQLite** (stdlib) | — | Already on the box; WAL mode allows concurrent dashboard reads while the writer appends. | Built-in |
| **redis-py** | https://github.com/redis/redis-py | Cache + **pub/sub**: writer publishes `orgnr` on a channel, every dashboard bridge fans out. Also a TTL cache for `ETag`. | Active; needs a Redis server |

**Integration:** Phase 0 keeps `envelopes.jsonl` + SQLite. Phase 2 adds DuckDB for ad-hoc
analytics and Redis pub/sub for fan-out. Neither is required to score.

### 3.5 Delivery — push to the dashboard

| Project | Repo | How it helps live data | Status |
|---|---|---|---|
| **sse-starlette** | https://github.com/sysid/sse-starlette | Production-ready Server-Sent Events for Starlette/FastAPI. One `/stream/{orgnr}` endpoint pushes `Change` events; the Next.js dashboard consumes via native `EventSource`. No extra client lib. | Active; PyPI sse-starlette |
| **websockets** | https://github.com/python-websocket/websockets | Bidirectional alternative if the UI needs to *send* watch/unwatch commands. | Active |
| **FastAPI** | https://github.com/fastapi/fastapi | ASGI app that hosts the SSE/WS endpoints and wraps the existing agent as `/run` and `/stream`. | Very active |
| **Uvicorn** | https://github.com/encode/uvicorn | ASGI server to run FastAPI. | Active (encode) |
| **anyio** | https://github.com/agronholm/anyio | Unified asyncio/trio backend if you adopt structured concurrency. | Active |

**Integration:** The current `src/serve.py` is a stdlib `http.server`. Upgrading to
FastAPI + Uvicorn + sse-starlette is the single highest-leverage change for "live UX" — it
lets the dashboard subscribe to `Change` events instead of polling.

### 3.6 Event backbone (only if scaling past the demo)

| Project | Repo | How it helps live data | Status |
|---|---|---|---|
| **FastStream** | https://github.com/ag2ai/faststream *(moved from airtai)* | Typed Kafka/RabbitMQ/NATS/Redis-Streams consumers; durable ingestion if Brreg volume grows. | Active; faststream.ag2.ai |
| **RisingWave** | https://github.com/risingwavelabs/risingwave | Postgres-compatible streaming DB; SQL materialized views over change streams. Emerging, Apache-2.0. | Very active; risingwave.com |
| **Temporal** | https://github.com/temporalio/sdk-python | Durable workflows with automatic retries — guarantees every queued orgnr is eventually re-validated even across crashes. | Active; python.temporal.io |

**Integration:** None of these are needed for the 1000-org demo. Listed because they are the
natural "emerging tech" path if Signalpost productizes.

### 3.7 Validation (already in use)

| Project | Repo | Note |
|---|---|---|
| **pydantic** | https://github.com/pydantic/pydantic | v2 is fast; validate envelope + change schemas before emit. Likely already a transitive dep. |

---

## 4. Security caveat — DeepDiff CVE-2025-58367

Research surfaced **CVE-2025-58367 (RCE in DeepDiff)**. If you adopt DeepDiff for the delta
engine:

1. **Pin to a patched version** and track it in `requirements` / `pyproject`.
2. **Never** let it diff attacker-controlled Python objects — only the agent's own envelope
   dicts, so exposure is limited, but the CVE is real.
3. **Preferred for the demo:** a stdlib `diff_envelopes(a, b)` (recursive key/value compare).
   Zero dependencies, zero CVE surface, and plenty for 1000 orgs.

---

## 5. Recommended pragmatic stack (phased)

Keep the competition constraint front of mind: **the demo must stay keyless and the agent
must keep scoring.** So phase it.

### Phase 0 — stdlib-only live watchdog (ships now, no new deps)
- `src/live.py`: the watermark loop from §2 using `http.client` + `sched`.
- On changed orgnr → call existing `pipeline.run([org])` → `diff_envelopes` (stdlib) → append
  to `envelopes.jsonl` and a `changes.jsonl`.
- Dashboard reads `changes.jsonl` on load (already has the Change Stream mock).

### Phase 1 — real-time bridge (FastAPI + SSE)
- `FastAPI` + `Uvicorn` + `sse-starlette` wrapping the agent.
- `GET /stream/{orgnr}` → `EventSource` in Next.js dashboard updates the evidence card live.
- `httpx.AsyncClient` + `aiolimiter` for polite, cache-aware polling.
- `APScheduler.AsyncIOScheduler` drives the poll loop inside the event loop.

### Phase 2 — scale-out (only if productized)
- `DuckDB` for instant analytics over `envelopes.jsonl`.
- `Redis` pub/sub for multi-session fan-out.
- `FastStream` / `RisingWave` / `Temporal` if ingestion volume or durability needs grow.

---

## 6. Reference prototype — `src/live.py` (Phase 0 sketch)

```python
"""Stdlib-only live watchdog: poll Brreg change feed, re-validate, emit Change."""
import json, time, sched, threading
from pathlib import Path
from registry import BrregClient
from pipeline import run as run_pipeline
from envelope import envelopes_to_jsonl

WATERMARK = Path("runs/watermark.txt")
POLL_SECONDS = 60

def load_watermark() -> int:
    return int(WATERMARK.read_text()) if WATERMARK.exists() else 0

def save_watermark(n: int) -> None:
    WATERMARK.write_text(str(n))

def changed_orgnrs_since(last_id: int) -> tuple[set[str], int]:
    client = BrregClient()
    seen, new_id = set(), last_id
    page = 0
    while True:
        batch = client.changes(last_id, page=page, size=500)
        evs = batch.get("_embedded", {}).get("oppdaterteEnheter", [])
        if not evs:
            break
        for ev in evs:
            seen.add(ev["organisasjonsnummer"])
            new_id = max(new_id, ev["oppdateringsid"])
        if page + 1 >= batch["page"]["totalPages"]:
            break
        page += 1
    return seen, new_id

def tick() -> None:
    last = load_watermark()
    orgs, new_id = changed_orgnrs_since(last)
    if orgs:
        run_pipeline(list(orgs), no_web=True, no_llm=True)   # reuses existing pipeline
    save_watermark(new_id)

def main() -> None:
    s = sched.scheduler(time.time, time.sleep)
    def loop():
        tick(); s.enter(POLL_SECONDS, 1, loop)
    s.enter(0, 1, loop)
    s.run()
```

> This is intentionally thin: it reuses `BrregClient.changes()`, `pipeline.run()`, and the
> envelope writer that already exist. The "live" value is the *watermark cursor*, not new code.

---

## 7. Dashboard consumption

The served console (`web-next/index.html` + `app.js` + `styles.css`) is plain HTML/CSS/JS with
no build step and no framework, so consuming the stream needs nothing installed. It is already
implemented, in `openChangeStream()` in `web-next/app.js`: the "Live registry changes" panel
opens an `EventSource` against the bridge and renders each event as a row, newest first.

```js
// web-next/app.js — the shape of it
const source = new EventSource(`http://${LIVE_BRIDGE}/api/stream/*`);
source.addEventListener("change", (ev) => {
  const change = JSON.parse(ev.data);
  render(change);            // live update, no refresh
});
```

Two details worth keeping if this is ever reworked:

- The console and the bridge are **different origins** (`:8787` and `:8799`), so the bridge
  sends `Access-Control-Allow-Origin` restricted to local origins. Without it the browser
  silently blocks the read and the panel looks empty rather than broken.
- When the bridge is not running the panel says **"bridge offline"** and explains why. It does
  not fall back to an empty list, which would read as "nothing has changed" — a claim about the
  world the console is not in a position to make.

`EventSource` is native in every browser, so there is no dependency to add.

---

## 8. What NOT to build (per Oct 8 feedback)

- No AI chatbot, no 3D animations, no blockchain, no recommendation engine, no CRM, no
  multi-tenant SaaS, no fabricated metrics.
- Do not add RisingWave/Temporal/Redis *for the demo* — they add infra the judges can't run
  keyless and they don't move the recall/precision needle.
- The live layer is a **recall + synthesis amplifier**, not a substitute for correct facts.

---

## 9. Repo quick-reference

| Layer | Project | URL |
|---|---|---|
| HTTP | httpx | https://github.com/encode/httpx |
| HTTP | aiohttp | https://github.com/aio-libs/aiohttp |
| Rate limit | aiolimiter | https://github.com/mjpieters/aiolimiter |
| Diff | deepdiff ⚠️CVE | https://github.com/qlustered/deepdiff |
| Diff | datasketch | https://github.com/ekzhu/datasketch |
| Scheduler | APScheduler | https://github.com/agronholm/apscheduler |
| Scheduler | schedule | https://github.com/dbader/schedule |
| Store | DuckDB | https://github.com/duckdb/duckdb |
| Cache/fan-out | redis-py | https://github.com/redis/redis-py |
| SSE | sse-starlette | https://github.com/sysid/sse-starlette |
| WS | websockets | https://github.com/python-websocket/websockets |
| Server | FastAPI | https://github.com/fastapi/fastapi |
| Server | Uvicorn | https://github.com/encode/uvicorn |
| Backend | anyio | https://github.com/agronholm/anyio |
| Event bus | FastStream | https://github.com/ag2ai/faststream |
| Stream DB | RisingWave | https://github.com/risingwavelabs/risingwave |
| Durable WF | Temporal | https://github.com/temporalio/sdk-python |
| Validate | pydantic | https://github.com/pydantic/pydantic |

*Researched 2026-10; all repos above showed recent activity and maintained documentation at
time of writing. Brreg change-feed schema verified live against the production API.*

---

## 10. Implementation status (built 2026-10-08)

All three phases are implemented and verified against the live Brreg API. The
competition agent (`python run.py run`) stays stdlib-only; the live layer is an
optional companion process installed in a dedicated venv (`requirements-live.txt`).

### Phase 0 — stdlib watchdog ✅
`src/live.py` (no third-party deps):
- `collect_changes()` polls `oppdateringer/enheter` with the `oppdateringsid`
  cursor (exclusive lower bound, verified live). Stops on short page + honours
  Brreg's `page*size < 10000` ceiling.
- `seek_tail()` binary-searches the cursor to seed "now" (the feed cannot be
  paged to its tail and `dato` filtering is rejected by the API — both latent
  bugs in the pre-existing `changes()`/`sync_changes` `since` path, now bypassed).
- `process_orgnrs()` reuses `pipeline.run` in an isolated `runs/.live` dir, then
  `merge_envelopes()` merges back into `runs/envelopes.jsonl` (never clobbers the
  batch) and `append_changes()` writes detected `ChangeRecord`s to `runs/changes.jsonl`.
- Verified: a bounded tick re-validated companies and grew `envelopes.jsonl`
  100 → 103 lines, other 100 preserved; 0 spurious changes (honest diff).

### Phase 1 — real-time FastAPI + SSE bridge ✅
`src/live_api.py` (deps in `requirements-live.txt`):
- `httpx.AsyncClient` + `aiolimiter.AsyncLimiter` poll the change feed (rate-limited).
- `APScheduler.AsyncIOScheduler` drives `poll_once` every `interval` seconds.
- `asyncio.to_thread(process_orgnrs)` reuses the exact Phase 0 pipeline off-loop.
- `sse-starlette.EventSourceResponse` streams every appended change to
  `/api/stream/{orgnr}` (tails `changes.jsonl`; `*` = all). Heartbeat every 15s.
- Endpoints: `/api/health`, `/api/live/status`, `/api/envelopes`,
  `/api/envelope/{orgnr}`, `/api/changes`, `/api/analytics`, `/api/poll`, `/api/stream/{orgnr}`.
- Verified: server boots; `POST /api/poll` fetched 20k events via httpx+aiolimiter,
  re-validated 2 companies, advanced the watermark; SSE pushed a change to a
  live client in real time.

### Phase 2 — scale-out (DuckDB + Redis/FastStream scaffold) ✅
- `src/analytics.py` — DuckDB queries over `envelopes.jsonl`/`changes.jsonl`
  (status mix, fact stats, field frequency via `UNNEST`, coverage-by-field,
  recent changes). Pure-Python fallback if DuckDB absent. Verified: `engine: duckdb`,
  105 envelopes, 2611 facts, 36 fields.
- `src/fanout.py` — Redis pub/sub; silent no-op when `REDIS_URL` unset. Verified
  `available: False`, `publish_change -> False` (no broker, no error).
- `src/eventbus.py` — FastStream scaffold; active only when `FASTSTREAM_BROKER`
  set. Inert otherwise. Verified `active: False`.

### Run it
```bash
# Phase 0 (stdlib, keyless)
python src/live.py --once --limit 5          # one tick
python src/live.py --interval 60              # daemon

# Phase 1 + 2 (optional, separate venv)
.venv-live/Scripts/python.exe -m pip install -r requirements-live.txt
.venv-live/Scripts/python.exe -m uvicorn src.live_api:app --port 8799
# dashboard subscribes: new EventSource("/api/stream/984851006")
```

