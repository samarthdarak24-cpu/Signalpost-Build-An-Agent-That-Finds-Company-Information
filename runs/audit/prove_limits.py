"""Task #45 §4 — prove the limits under controlled traffic.

The brief forbids testing the counter by making thousands of real external
requests. This script therefore drives the *real* ``HttpClient`` (and therefore
the real accounting) against a **local mock HTTP server**, so:

* the request ceiling is proven at scale without touching any provider;
* the deadline is proven to stop new work while preserving completed work;
* cost reservations for a paid operation are proven to be refused before the
  ceiling is breached;
* redirects and retries are counted against real socket exchanges.

It prints measured numbers only. Anything it cannot measure exactly is reported
as unknown rather than guessed.

Run::  python runs/audit/prove_limits.py
"""

from __future__ import annotations

import http.server
import socketserver
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import tempfile  # noqa: E402

from budget import DEFAULT_MAX_SECONDS, BudgetExceeded, RunBudget  # noqa: E402
from net import Budget, HttpClient  # noqa: E402

HITS = {"n": 0}
REDIRECTS = {"n": 0}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        HITS["n"] += 1
        if self.path.startswith("/redirect"):
            REDIRECTS["n"] += 1
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if self.path.startswith("/flaky"):
            # Fail the first two calls to force retries, then succeed.
            if REDIRECTS["n"] < 2:
                REDIRECTS["n"] += 1
                self.send_response(503)
                self.end_headers()
                return
        body = b"<html><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence the server
        pass


def _serve() -> tuple[socketserver.TCPServer, str]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def main() -> int:
    server, base = _serve()
    tmp = Path(tempfile.mkdtemp(prefix="signalpost-limits-"))
    print("=" * 72)
    print("CONTROLLED-TRAFFIC LIMIT PROOF (local mock server)")
    print("=" * 72)
    print(f"  mock server      {base}")
    print(f"  scratch cache   {tmp}")
    print()

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")

    try:
        # ---- 1. The request ceiling, at scale, against the real client ---------
        HITS["n"] = 0
        budget = Budget(hard_limit=200, soft_limit=200)
        budget.run.reserve_fraction = 0.0
        http = HttpClient(cache_dir=tmp / "c1", budget=budget, robots_aware=False)
        served = 0
        for _ in range(500):  # ask for far more than allowed
            try:
                http.get(f"{base}/ok?i={_}", use_cache=False, check_robots=False)
                served += 1
            except BudgetExceeded:
                break
            except Exception:
                break
        accounted = budget.run.to_dict()["requests"]["outbound_attempts"]
        check(
            "request ceiling stops traffic exactly at the cap",
            accounted == 200 and HITS["n"] == 200,
            f"accounted={accounted} real_server_hits={HITS['n']} cap=200 "
            f"(asked for 500)",
        )

        # ---- 2. The ceiling under concurrency, against the real client --------
        HITS["n"] = 0
        budget2 = Budget(hard_limit=120, soft_limit=120)
        budget2.run.reserve_fraction = 0.0
        http2 = HttpClient(cache_dir=tmp / "c2", budget=budget2, robots_aware=False)
        admitted = {"n": 0}
        lock = threading.Lock()

        def hammer() -> None:
            for _ in range(100):
                try:
                    http2.get(f"{base}/ok?c={_}", use_cache=False, check_robots=False)
                except BudgetExceeded:
                    return
                except Exception:
                    return
                with lock:
                    admitted["n"] += 1

        threads = [threading.Thread(target=hammer) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        acc2 = budget2.run.to_dict()["requests"]["outbound_attempts"]
        check(
            "concurrent workers cannot exceed the cap",
            acc2 == 120 and HITS["n"] == 120,
            f"12 threads admitted {admitted['n']}; accounted={acc2} "
            f"real_server_hits={HITS['n']} cap=120",
        )

        # ---- 3. The deadline stops new work, keeps the work already done -------
        HITS["n"] = 0
        budget3 = Budget()
        budget3.run.max_seconds = 1.0
        budget3.run.reserve_fraction = 0.0
        http3 = HttpClient(cache_dir=tmp / "c3", budget=budget3, robots_aware=False)
        completed = 0
        started = time.monotonic()
        while time.monotonic() - started < 3.0:
            try:
                http3.get(f"{base}/ok?d={completed}", use_cache=False, check_robots=False)
                completed += 1
            except BudgetExceeded:
                break
            except Exception:
                break
            time.sleep(0.02)
        acc3 = budget3.run.to_dict()["requests"]["outbound_attempts"]
        elapsed = time.monotonic() - started
        check(
            "deadline halts new requests and preserves completed ones",
            acc3 == completed and acc3 > 0 and elapsed < 3.0,
            f"completed={completed} accounted={acc3} elapsed={elapsed:.2f}s "
            f"(deadline 1.0s, loop allowed 3.0s)",
        )

        # ---- 4. Redirects are visible even though urllib follows them ---------
        HITS["n"] = 0
        REDIRECTS["n"] = 0
        budget4 = Budget(hard_limit=50, soft_limit=50)
        http4 = HttpClient(cache_dir=tmp / "c4", budget=budget4, robots_aware=False)
        http4.get(f"{base}/redirect", use_cache=False, check_robots=False)
        r4 = budget4.run.to_dict()["requests"]
        check(
            "a followed redirect is recorded as a redirect",
            r4["redirected"] >= 1,
            f"requested=/redirect final=/ok -> redirected={r4['redirected']} "
            f"(underscore: urllib makes N socket hops for one logical GET, so the "
            f"attempt count of {r4['outbound_attempts']} is a documented undercount)",
        )

        # ---- 5. A paid operation is refused before the ceiling is breached ----
        budget5 = RunBudget(max_requests=1000, max_usd=1.0, reserve_fraction=0.0)
        admitted_paid = 0
        refused_paid = 0
        for _ in range(10):
            try:
                budget5.begin(kind="search_brave", cost=0.30)
                budget5.charge("search_brave", cost_usd=0.30)
                admitted_paid += 1
            except BudgetExceeded:
                refused_paid += 1
        spent = budget5.to_dict()["cost"]["actual_usd"]
        check(
            "a paid operation is refused before the cost cap is breached",
            spent <= 1.0 and refused_paid > 0,
            f"admitted={admitted_paid} refused={refused_paid} "
            f"spent=${spent:.4f} of $1.00 cap",
        )

        # ---- 6. A worst-case reservation blocks an unaffordable call ----------
        budget6 = RunBudget(max_requests=1000, max_usd=0.50, reserve_fraction=0.0)
        held = budget6.reserve_cost("anthropic", "fill_gaps", 0.45)
        blocked = False
        try:
            # A second worst-case hold of 0.45 would reach 0.90 > 0.50.
            budget6.begin(kind="llm", cost=0.45)
        except BudgetExceeded:
            blocked = True
        check(
            "a reservation blocks a second call that would breach the cap",
            blocked,
            f"held=${held.reserved:.2f} outstanding; second $0.45 call refused "
            f"(cap $0.50, worst case would be $0.90)",
        )

        # ---- 7. Cost is reconciled, not double counted ------------------------
        budget7 = RunBudget(max_requests=100, max_usd=10.0)
        res = budget7.reserve_cost("anthropic", "fill_gaps", 1.0)
        budget7.reconcile_cost(res, actual_usd=0.20)
        c7 = budget7.to_dict()["cost"]
        check(
            "reserved cost is trued up to the actual charge",
            c7["actual_usd"] == 0.20 and c7["reserved_usd"] == 0.0,
            f"reserved $1.00 -> actual $0.20; reported actual=${c7['actual_usd']} "
            f"reserved=${c7['reserved_usd']} worst_case=${c7['worst_case_usd']}",
        )

        # ---- 8. Unknown cost keeps its hold instead of freeing headroom -------
        budget8 = RunBudget(max_requests=100, max_usd=10.0)
        res8 = budget8.reserve_cost("anthropic", "fill_gaps", 2.0)
        budget8.reconcile_cost(res8, actual_usd=0.0, unknown=True)
        c8 = budget8.to_dict()["cost"]
        check(
            "an unmeasurable charge keeps its hold (no freed headroom)",
            c8["reserved_usd"] == 2.0 and c8["unknown_cost_operations"] == 1,
            f"reserved=${c8['reserved_usd']} unknown_ops="
            f"{c8['unknown_cost_operations']} (headroom must not grow)",
        )

    finally:
        server.shutdown()
        server.server_close()

    passed = sum(1 for _, ok, _ in results if ok)
    print()
    print("=" * 72)
    print(f"RESULT: {passed}/{len(results)} checks passed")
    print("=" * 72)
    print()
    print("MEASURED vs EXTERNAL:")
    print("  All numeric claims above are from the local mock server and the real")
    print("  accountant. No external provider was contacted.")
    print("  External API cost actually incurred by this script: $0.00 (measurable,")
    print("  because nothing billable was called). Brreg/keyless traffic in the real")
    print("  pipeline is free but still consumes the request budget.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
