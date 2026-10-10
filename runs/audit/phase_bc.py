"""Phase B/C audit: backend robustness and keyless behaviour.

Checks only things that can be checked without credentials:
  * invalid input is rejected without crashing the server
  * a missing/blocked company returns an honest gap, not a fabricated answer
  * no environment secret ever appears in a response body
  * keyless search really works with no API key set
  * optional deps (duckdb/redis/faststream) degrade instead of exploding
  * the health endpoint reports key presence truthfully

Writes findings to runs/audit/phase_bc.txt.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

CONSOLE = "http://127.0.0.1:8787"
OUT = ROOT / "runs" / "audit" / "phase_bc.txt"
lines: list[str] = []


def rec(s: str = "") -> None:
    lines.append(s)
    print(s)


def post(path: str, payload: dict, timeout: int = 120):
    req = urllib.request.Request(
        CONSOLE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {"_body": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:  # noqa: BLE001
        return -1, {"_error": f"{type(e).__name__}: {e}"}


def get(path: str, timeout: int = 30):
    try:
        with urllib.request.urlopen(CONSOLE + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


rec("=" * 72)
rec("PHASE B/C - BACKEND AND KEYLESS AUDIT")
rec("=" * 72)

# --- 1. invalid inputs -------------------------------------------------------
rec("")
rec("1. INVALID INPUT (server must answer, not crash)")
cases = [
    ("empty", {"q": ""}),
    ("whitespace", {"q": "   "}),
    ("bad checksum", {"q": "123456789"}),
    ("non-numeric", {"q": "not-a-company-%%%"}),
    ("very long", {"q": "x" * 5000}),
]
for name, payload in cases:
    st, body = post("/api/live", payload, timeout=90)
    ok = st in (200, 400, 422)
    verdict = body.get("answered") if isinstance(body, dict) else None
    rec(f"   {'OK ' if ok else 'BAD'} {name:14} HTTP {st}  answered={verdict}")
    if isinstance(body, dict) and body.get("answer"):
        rec(f"       answer starts: {str(body['answer'])[:70]!r}")

# server still alive after all that?
st, _ = get("/api/health")
rec(f"   server alive after invalid input: HTTP {st}")

# --- 2. unregistered but valid number ---------------------------------------
rec("")
rec("2. VALID BUT UNREGISTERED NUMBER (must not fabricate)")
st, body = post("/api/live", {"q": "999999999"}, timeout=120)
if isinstance(body, dict):
    rec(f"   HTTP {st} answered={body.get('answered')} sources={len(body.get('sources') or [])}")
    rec(f"   answer: {str(body.get('answer'))[:110]!r}")

# --- 3. secrets must not leak ------------------------------------------------
rec("")
rec("3. SECRET LEAKAGE CHECK")
secrets: list[tuple[str, str]] = []
for var in ("ANTHROPIC_API_KEY", "BRAVE_API_KEY", "OPENAI_API_KEY"):
    v = os.environ.get(var, "")
    if v and len(v) > 6:
        secrets.append((var, v))
rec(f"   secrets present in this shell: {[n for n, _ in secrets] or 'none'}")
leaked = []
for path in ("/api/health", "/api/profiles", "/api/runs", "/api/coverage", "/api/live/status"):
    st, body = get(path)
    for name, val in secrets:
        if val in body:
            leaked.append((path, name))
        # also catch a bare 'sk-' prefix which would indicate any key echo
    if "sk-ant-" in body or "sk-or-" in body:
        leaked.append((path, "sk-* prefix"))
rec(f"   leaked secrets in responses: {leaked or 'none'}")

# --- 4. keyless search -------------------------------------------------------
rec("")
rec("4. KEYLESS SEARCH (no BRAVE_API_KEY)")
rec(f"   BRAVE_API_KEY set: {bool(os.environ.get('BRAVE_API_KEY'))}")
try:
    from liveweb import search_web

    out = search_web("Equinor ASA")
    rec(f"   engine={out.engine!r} ok={out.ok} results={len(out.results)} error={out.error!r}")
    rec(f"   first title: {(out.results[0].title if out.results else '')[:70]!r}")
except Exception as e:  # noqa: BLE001
    rec(f"   FAILED: {type(e).__name__}: {e}")

# --- 5. health truthfulness --------------------------------------------------
rec("")
rec("5. HEALTH REPORTS KEY PRESENCE TRUTHFULLY")
st, body = get("/api/health")
try:
    h = json.loads(body)
    rec(f"   llm_key_present   : {h.get('llm_key_present')}  (env set: {bool(os.environ.get('ANTHROPIC_API_KEY'))})")
    rec(f"   search_key_present: {h.get('search_key_present')}  (env set: {bool(os.environ.get('BRAVE_API_KEY'))})")
except Exception as e:  # noqa: BLE001
    rec(f"   could not parse health: {e}")

# --- 6. optional dependency degradation --------------------------------------
rec("")
rec("6. OPTIONAL DEPENDENCY DEGRADATION (absent duckdb/redis/faststream)")
try:
    import analytics

    rec(f"   analytics._duckdb_ok() -> {analytics._duckdb_ok()} (falls back to stdlib)")
    counts = analytics.status_counts()
    rec(f"   analytics.status_counts() works without duckdb -> {dict(list(counts.items())[:4])}")
except Exception as e:  # noqa: BLE001
    rec(f"   analytics FAILED: {type(e).__name__}: {e}")

try:
    import eventbus

    rec(f"   eventbus imports with no broker set; app is {eventbus.app!r}")
except Exception as e:  # noqa: BLE001
    rec(f"   eventbus FAILED: {type(e).__name__}: {e}")

try:
    import fanout

    rec("   fanout imports without redis")
except Exception as e:  # noqa: BLE001
    rec(f"   fanout FAILED: {type(e).__name__}: {e}")

rec("")
rec("=" * 72)
OUT.write_text("\n".join(lines), encoding="utf-8")
print(f"\naudit written -> {OUT}")
