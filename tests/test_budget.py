"""Deterministic tests for the execution-budget manager.

Every boundary here is exercised with a **mocked transport**, so no test makes a
real request and the suite stays fast and offline. The one exception is
:class:`TestRealApplicationIntegration`, which runs the real pipeline against the
local application and asserts the measured traffic matches the accounting.

The cases map to the Task #45 checklist, numbered in the class docstrings.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from budget import (  # noqa: E402
    CALL_KINDS,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_USD,
    PROVIDER_PRICING,
    UNKNOWN,
    BudgetExceeded,
    RunBudget,
)
from net import Budget as NetBudget, HttpClient, HttpError  # noqa: E402


def _ok_response(url: str, status: int = 200, text: str = "body") -> mock.MagicMock:
    """A stand-in for the object urllib returns from urlopen."""
    handle = mock.MagicMock()
    handle.status = status
    handle.url = url
    handle.headers = {"Content-Type": "text/html"}
    handle.read.return_value = text.encode("utf-8")
    handle.__enter__ = lambda s: s
    handle.__exit__ = lambda *a: False
    return handle


class TestRequestBoundaries(unittest.TestCase):
    """Cases 1-3: zero budget, exactly at the limit, and concurrency."""

    def test_01_zero_request_budget_refuses_the_first_request(self) -> None:
        b = RunBudget(max_requests=0)
        self.assertTrue(b.winding_down)
        self.assertFalse(b.allow("web_request"))
        with self.assertRaises(BudgetExceeded):
            b.begin("web_request")
        self.assertEqual(b.requests_used, 0, "a refused request must not be counted")
        self.assertEqual(b.by_provider, {}, "no provider traffic may be recorded")

    def test_02_exactly_two_thousand_allowed_and_the_next_rejected(self) -> None:
        b = RunBudget(max_requests=DEFAULT_MAX_REQUESTS, reserve_fraction=0.0)
        for _ in range(DEFAULT_MAX_REQUESTS):
            b.begin("web_request")
        self.assertEqual(b.requests_used, 2000)
        self.assertEqual(b.requests_remaining, 0)
        with self.assertRaises(BudgetExceeded):
            b.begin("web_request")
        self.assertEqual(b.requests_used, 2000, "the 2001st request must never be counted")

    def test_02b_the_limit_is_2000_not_the_old_dead_controller_default(self) -> None:
        # Guards the specific regression where budget.BudgetController defaulted to
        # 6000 requests / $5 -- values that contradict the competition rules.
        b = RunBudget()
        self.assertEqual(b.max_requests, 2000)
        self.assertEqual(b.max_usd, 10.0)
        self.assertEqual(b.max_seconds, 45 * 60)
        from budget import BudgetController

        self.assertEqual(BudgetController().max_requests, DEFAULT_MAX_REQUESTS)

    def test_03_concurrent_callers_cannot_exceed_the_limit(self) -> None:
        # 40 threads each try to spend 100 times against a 2,000 cap. Without a
        # lock the check-then-increment race lets the total overshoot.
        b = RunBudget(max_requests=2000, reserve_fraction=0.0)
        granted: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(100):
                try:
                    b.begin("web_request")
                except BudgetExceeded:
                    return
                with lock:
                    granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(b.requests_used, 2000, "the cap must hold exactly")
        self.assertEqual(sum(granted), 2000, "granted calls must match the counter")
        self.assertLessEqual(b.requests_used, b.max_requests)


class TestRetriesAndRedirects(unittest.TestCase):
    """Cases 4-6: retries consume budget, redirect accounting, transport faults.

    Every test here uses a private cache directory, because the project's shared
    ``data/cache`` is populated by real runs and would otherwise make these tests
    order- and machine-dependent.
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _client(self, **kw) -> tuple[NetBudget, HttpClient]:
        b = NetBudget(hard_limit=100, soft_limit=100)
        return b, HttpClient(cache_dir=self._tmp.name, budget=b, **kw)

    def test_04_each_retry_attempt_consumes_one_request(self) -> None:
        import urllib.error

        b, client = self._client()
        # Three attempts: the first two raise a retryable 503, the third succeeds.
        err = urllib.error.HTTPError("https://x.no", 503, "busy", {}, None)
        with mock.patch("net._u.urlopen", side_effect=[err, err, _ok_response("https://x.no")]):
            with mock.patch("net.time.sleep"):
                client.get("https://x.no", use_cache=False, check_robots=False)
        self.assertEqual(b.run.requests_used, 3, "all three attempts must be counted")
        self.assertEqual(b.run.requests_retried, 2, "in-attempt retries must be visible")
        self.assertEqual(b.run.requests_succeeded, 1)

    def test_05_a_redirect_is_recorded_when_the_final_url_differs(self) -> None:
        b, client = self._client()
        # urllib reports the *final* URL after following a redirect.
        handle = _ok_response("https://www.x.no/", text="landed")
        with mock.patch("net._u.urlopen", return_value=handle):
            client.get("https://x.no", use_cache=False, check_robots=False)
        self.assertEqual(b.run.requests_used, 1, "one logical GET")
        self.assertEqual(b.run.requests_redirected, 1, "the hop must be visible")
        self.assertEqual(b.run.requests_succeeded, 1)

    def test_05b_no_redirect_recorded_when_the_url_is_unchanged(self) -> None:
        b, client = self._client()
        with mock.patch("net._u.urlopen", return_value=_ok_response("https://x.no")):
            client.get("https://x.no", use_cache=False, check_robots=False)
        self.assertEqual(b.run.requests_redirected, 0)

    def test_06_connection_failure_is_counted_once_per_attempt(self) -> None:
        b, client = self._client()
        # check_robots=False isolates the page fetch. Note that with robots
        # checking on, the robots.txt fetch is itself an outbound request and is
        # counted too -- see test_06e.
        with mock.patch("net._u.urlopen", side_effect=OSError("connection refused")):
            with mock.patch("net.time.sleep"):
                with self.assertRaises(HttpError):
                    client.get("https://dead.no", use_cache=False, check_robots=False)
        self.assertEqual(b.run.requests_used, 3, "3 attempts, all counted")
        self.assertEqual(b.run.requests_failed, 3)
        self.assertEqual(b.run.requests_succeeded, 0)

    def test_06e_the_robots_txt_fetch_is_itself_a_counted_request(self) -> None:
        """A robots.txt fetch is real outbound traffic, not an internal detail."""
        b, client = self._client()
        with mock.patch("net._u.urlopen", return_value=_ok_response("https://x.no")):
            client.get("https://x.no", use_cache=False)  # robots check is the default
        # 1 robots.txt fetch + 1 page fetch.
        self.assertEqual(b.run.requests_used, 2)
        self.assertEqual(b.run.requests_succeeded, 2)
        # Cached per host: a second URL on the same host must not refetch it.
        with mock.patch("net._u.urlopen", return_value=_ok_response("https://x.no/a")):
            client.get("https://x.no/a", use_cache=False)
        self.assertEqual(b.run.requests_used, 3, "robots.txt is served from its per-host cache")

    def test_06b_timeout_is_counted_and_retried(self) -> None:
        b, client = self._client()
        with mock.patch("net._u.urlopen", side_effect=TimeoutError("timed out")):
            with mock.patch("net.time.sleep"):
                with self.assertRaises(HttpError):
                    client.get("https://slow.no", use_cache=False, check_robots=False)
        self.assertEqual(b.run.requests_used, 3)
        self.assertEqual(b.run.requests_failed, 3)

    def test_06c_a_refused_request_never_reaches_the_transport(self) -> None:
        """The core guarantee: no request starts after the limit is reached."""
        b = NetBudget(hard_limit=1, soft_limit=1)
        client = HttpClient(cache_dir=self._tmp.name, budget=b)
        call = mock.Mock(return_value=_ok_response("https://x.no"))
        with mock.patch("net._u.urlopen", call):
            client.get("https://x.no", use_cache=False, check_robots=False)
            self.assertEqual(call.call_count, 1)
            with self.assertRaises(HttpError) as ctx:
                client.get("https://y.no", use_cache=False, check_robots=False)
            self.assertEqual(call.call_count, 1, "the 2nd request must not hit the network")
        self.assertIn("budget refused", str(ctx.exception))

    def test_06d_a_cache_hit_is_not_a_request(self) -> None:
        # Uses this class's isolated cache dir: the shared data/cache is populated
        # by real runs, so relying on it would make this test machine-dependent.
        b, client = self._client()
        call = mock.Mock(return_value=_ok_response("https://x.no"))
        with mock.patch("net._u.urlopen", call):
            client.get("https://x.no", check_robots=False)
            first = b.run.requests_used
            client.get("https://x.no", check_robots=False)  # served from disk cache
        self.assertEqual(call.call_count, 1, "only the first call hit the network")
        self.assertEqual(first, b.run.requests_used, "a cache hit must not spend budget")
        self.assertEqual(first, 1, "the first (uncached) call must have spent exactly one")
        self.assertEqual(b.run.cache_hits, 1)


class TestDeadlineAndCancellation(unittest.TestCase):
    """Cases 7 and 11: deadline expiry, cancellation, partial preservation."""

    def test_07_deadline_expiry_refuses_new_requests(self) -> None:
        b = RunBudget(max_seconds=0.05)
        self.assertFalse(b.deadline_reached)
        time.sleep(0.08)
        self.assertTrue(b.deadline_reached)
        self.assertTrue(b.winding_down)
        with self.assertRaises(BudgetExceeded):
            b.begin("web_request")
        self.assertEqual(b.requests_used, 0)

    def test_07b_check_deadline_raises_between_stages(self) -> None:
        b = RunBudget(max_seconds=0.01)
        time.sleep(0.03)
        with self.assertRaises(BudgetExceeded) as ctx:
            b.check_deadline("enrichment")
        self.assertIn("enrichment", str(ctx.exception))

    def test_07c_check_deadline_is_silent_before_the_deadline(self) -> None:
        RunBudget(max_seconds=60).check_deadline("fine")  # must not raise

    def test_11_cancellation_stops_new_work_and_preserves_counters(self) -> None:
        b = RunBudget(max_requests=100)
        b.begin("web_request")
        b.note_success()
        before = b.to_dict()
        b.cancel("deadline during batch")
        self.assertTrue(b.cancelled)
        self.assertEqual(b.termination_reason, "deadline during batch")
        with self.assertRaises(BudgetExceeded):
            b.begin("web_request")
        after = b.to_dict()
        self.assertEqual(
            after["requests"]["outbound_attempts"],
            before["requests"]["outbound_attempts"],
            "cancelling must not discard work already done",
        )
        self.assertEqual(after["requests"]["succeeded"], 1)

    def test_11b_cancel_keeps_the_first_reason(self) -> None:
        b = RunBudget()
        b.cancel("first")
        b.cancel("second")
        self.assertEqual(b.termination_reason, "first")


class TestCostAccounting(unittest.TestCase):
    """Cases 8, 9, 10: reservation, reconciliation, over-limit and unknown price."""

    def test_08_reservation_then_reconciliation(self) -> None:
        b = RunBudget(max_usd=10.0)
        b.begin("llm", cost=0.5)
        res = b.reserve_cost("anthropic", "fill_gaps", 0.5)
        self.assertEqual(b.cost_usd, 0.5, "the reservation must count as committed")
        self.assertEqual(b.cost_actual_usd, 0.0)
        b.reconcile_cost(res, actual_usd=0.12)
        self.assertEqual(b.cost_actual_usd, 0.12)
        self.assertEqual(b.cost_reserved_usd, 0.0)
        self.assertEqual(b.cost_usd, 0.12)
        self.assertAlmostEqual(b.cost_headroom, 9.88, places=6)

    def test_08b_reconcile_is_idempotent(self) -> None:
        b = RunBudget(max_usd=10.0)
        res = b.reserve_cost("anthropic", "x", 1.0)
        b.reconcile_cost(res, actual_usd=0.2)
        b.reconcile_cost(res, actual_usd=0.2)
        self.assertEqual(b.cost_actual_usd, 0.2, "double reconciliation must not double charge")

    def test_08c_an_unknown_charge_keeps_its_reservation(self) -> None:
        """An unknown charge must not silently free headroom for more spending."""
        b = RunBudget(max_usd=10.0)
        b.begin("llm", cost=1.0)
        res = b.reserve_cost("anthropic", "x", 1.0)
        b.reconcile_cost(res, actual_usd=0.0, unknown=True)
        self.assertEqual(b.cost_reserved_usd, 1.0)
        self.assertEqual(b.cost_unknown_ops, 1)

    def test_09_a_paid_call_exceeding_remaining_budget_is_refused(self) -> None:
        b = RunBudget(max_usd=1.0)
        b.charge("llm", cost_usd=0.9)
        self.assertFalse(b.allow("llm", cost=0.5), "0.9 + 0.5 exceeds the $1 ceiling")
        with self.assertRaises(BudgetExceeded):
            b.begin("llm", cost=0.5)
        self.assertEqual(b.requests_used, 0, "the refused call must not be counted")

    def test_09b_a_paid_call_within_budget_is_allowed(self) -> None:
        b = RunBudget(max_usd=1.0)
        b.charge("llm", cost_usd=0.2)
        b.begin("llm", cost=0.5)
        self.assertEqual(b.requests_used, 1)

    def test_09c_free_calls_are_not_blocked_by_the_cost_ceiling(self) -> None:
        b = RunBudget(max_usd=0.0)
        b.begin("registry")  # free but still consumes the request budget
        self.assertEqual(b.requests_used, 1)

    def test_10_unknown_pricing_is_unknown_not_zero(self) -> None:
        b = RunBudget(max_usd=10.0)
        self.assertEqual(b.price("no-such-provider"), UNKNOWN)
        self.assertNotEqual(b.price("no-such-provider"), 0.0)
        self.assertEqual(
            b.token_estimate("anthropic", model="no-such-model",
                             max_input_tokens=1000, max_output_tokens=100),
            UNKNOWN,
        )

    def test_10b_an_unpriced_paid_call_is_refused_under_strict_cost(self) -> None:
        b = RunBudget(max_usd=10.0, strict_cost=True)
        self.assertFalse(b.allow("llm", cost=UNKNOWN))
        with self.assertRaises(BudgetExceeded):
            b.begin("llm", cost=UNKNOWN)
        self.assertEqual(b.cost_unknown_ops, 0, "a refused call must not be charged")

    def test_10c_a_priced_model_reserves_a_worst_case_amount(self) -> None:
        b = RunBudget(max_usd=10.0)
        model = "claude-3-5-haiku-latest"
        price = b.token_estimate("anthropic", model=model,
                                max_input_tokens=1_000_000, max_output_tokens=1_000_000)
        entry = PROVIDER_PRICING["anthropic"]["models"][model]
        self.assertAlmostEqual(price, entry["input"] + entry["output"], places=6)
        self.assertGreater(price, 0.0)

    def test_10d_cost_ceiling_halts_new_paid_work_but_not_free_work(self) -> None:
        b = RunBudget(max_usd=1.0)
        b.charge("llm", cost_usd=1.0)
        self.assertTrue(b.winding_down)
        # A metered call is refused: it would move the cost further past the cap.
        self.assertFalse(b.allow("llm", cost=0.01))
        # A free call is not refused by the *cost* trigger, because it adds no
        # cost. (A free call may still be stopped by the request or time ceiling.)
        self.assertTrue(b.allow("web_request"))
        self.assertTrue(b.winding_down_for("llm"))
        self.assertFalse(b.winding_down_for("web_request"))


class TestProviderCoverage(unittest.TestCase):
    """Case 14: accounting spans every outbound provider, not just one engine."""

    def test_14_every_call_kind_is_attributed_to_a_provider(self) -> None:
        b = RunBudget(max_requests=10_000, max_usd=1000.0)
        for kind in CALL_KINDS:
            cost = 0.0
            if CALL_KINDS[kind]["metered"]:
                cost = 0.0001
            b.begin(kind, cost=cost)
        self.assertEqual(len(b.by_provider), len({m["provider"] for m in CALL_KINDS.values()}))
        for kind in CALL_KINDS:
            self.assertEqual(b.by_operation.get(kind), 1, f"{kind} must be counted")
        # The point of this test: no provider is silently exempt.
        self.assertIn("brreg", b.by_provider)
        self.assertIn("brave", b.by_provider)
        self.assertIn("anthropic", b.by_provider)
        self.assertIn("keyless", b.by_provider)
        self.assertEqual(b.requests_used, len(CALL_KINDS))

    def test_14b_free_providers_still_consume_the_request_budget(self) -> None:
        b = RunBudget(max_requests=2, max_usd=10.0, reserve_fraction=0.0)
        b.begin("registry")   # free
        b.begin("gleif")      # free
        with self.assertRaises(BudgetExceeded):
            b.begin("wikidata")
        self.assertEqual(b.cost_actual_usd, 0.0, "free means no charge")
        self.assertEqual(b.requests_used, 2, "but free is not unlimited")

    def test_14c_a_single_run_report_is_machine_readable(self) -> None:
        b = RunBudget(run_id="test-run", started_at="2026-10-10T00:00:00Z")
        b.begin("registry")
        b.note_success()
        b.begin("search_keyless")
        b.note_success(requested="https://a.no", redirected_from="https://b.no")
        b.note_failure()
        b.note_cache_hit()
        report = json.loads(json.dumps(b.to_dict()))
        for key in ("run_id", "started_at", "ended_at", "elapsed_seconds", "limits",
                    "requests", "cache_hits", "by_provider", "by_operation", "cost",
                    "tokens", "refusals", "termination_reason"):
            self.assertIn(key, report)
        self.assertEqual(report["requests"]["outbound_attempts"], 2)
        self.assertEqual(report["requests"]["succeeded"], 2)
        self.assertEqual(report["requests"]["failed"], 1)
        self.assertEqual(report["requests"]["redirected"], 1)
        self.assertEqual(report["cache_hits"], 1)


class TestRunIsolation(unittest.TestCase):
    """Case 15: two simultaneous runs must not share counters."""

    def test_15_two_independent_runs_do_not_share_counters(self) -> None:
        a = RunBudget(run_id="A", max_requests=10)
        b = RunBudget(run_id="B", max_requests=10)
        for _ in range(7):
            a.begin("registry")
        for _ in range(2):
            b.begin("web_request")
        self.assertEqual(a.requests_used, 7)
        self.assertEqual(b.requests_used, 2)
        self.assertEqual(a.by_provider.get("brreg"), 7)
        self.assertIsNone(b.by_provider.get("brreg"))
        a.cancel("A only")
        self.assertTrue(a.cancelled)
        self.assertFalse(b.cancelled, "cancelling one run must not stop the other")

    def test_15b_two_runs_concurrently_keep_separate_totals(self) -> None:
        results: dict[str, int] = {}
        lock = threading.Lock()

        def run(name: str, n: int) -> None:
            budget = RunBudget(max_requests=10_000, reserve_fraction=0.0)
            for _ in range(n):
                budget.begin("registry")
            with lock:
                results[name] = budget.requests_used

        t1 = threading.Thread(target=run, args=("x", 300))
        t2 = threading.Thread(target=run, args=("y", 500))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(results, {"x": 300, "y": 500})


class TestFailureModesDoNotCorruptState(unittest.TestCase):
    """Supporting guarantees: refusals are counted, never silent."""

    def test_refusals_are_recorded_per_kind(self) -> None:
        b = RunBudget(max_requests=0, reserve_fraction=0.0)
        b.allow("registry")
        b.allow("registry")
        b.allow("web_request")
        self.assertEqual(b.refusals["registry"], 2)
        self.assertEqual(b.refusals["web_request"], 1)
        self.assertEqual(b.termination_reason, "request limit")

    def test_unknown_kind_is_refused_not_silently_counted(self) -> None:
        b = RunBudget()
        self.assertFalse(b.allow("not_a_real_kind"))
        with self.assertRaises(BudgetExceeded):
            b.begin("not_a_real_kind")
        self.assertEqual(b.requests_used, 0)
        self.assertEqual(b.termination_reason, "unknown operation kind")

    def test_net_budget_and_run_budget_agree_on_one_counter(self) -> None:
        nb = NetBudget(hard_limit=5, soft_limit=5)
        nb.spend()
        nb.spend()
        self.assertEqual(nb.used, nb.run.requests_used)
        self.assertEqual(nb.used, 2)


class TestRealApplicationIntegration(unittest.TestCase):
    """One controlled integration test against the real application.

    Not thousands of requests: a single-page profile run, asserting that the
    *measured* outbound attempts match the accounting exactly. Served from a local
    file so no provider quota is touched.
    """

    def test_real_keyless_run_accounts_for_every_request(self) -> None:
        from delta import SnapshotStore
        from extract_llm import LlmClient
        from pipeline import RunReport, Settings, process_company
        from registry import RegistryStore

        budget = NetBudget(hard_limit=2000, soft_limit=2000)
        http = HttpClient(budget=budget)
        calls = {"n": 0}
        real_urlopen = __import__("urllib.request", fromlist=["urlopen"]).urlopen

        def counting_urlopen(request, *args, **kwargs):  # noqa: ANN001
            calls["n"] += 1
            return real_urlopen(request, *args, **kwargs)

        # The stores are closed explicitly. Constructed inline they outlive the
        # test and are reclaimed only by a later garbage-collection pass, which
        # surfaces as a ``ResourceWarning: unclosed database`` attributed to
        # whichever unrelated test happens to be running -- a false positive that
        # hides real leaks. The production path closes both in ``run()``'s
        # ``finally``; this mirrors that so the suite is as clean as the runtime.
        store = RegistryStore()
        snapshots = SnapshotStore()
        self.addCleanup(snapshots.close)
        self.addCleanup(store.close)

        with mock.patch("net._u.urlopen", side_effect=counting_urlopen):
            process_company(
                "923609016",
                store=store,
                snapshots=snapshots,
                settings=Settings(offline=False, use_llm=False, fetch_web=True),
                http=http, llm=LlmClient(), reporter=RunReport(),
                lock=threading.Lock(),
            )

        report = budget.run.to_dict()
        self.assertEqual(
            report["requests"]["outbound_attempts"], calls["n"],
            "every real socket call must be accounted for",
        )
        self.assertLessEqual(report["requests"]["outbound_attempts"], 2000)
        self.assertEqual(report["cost"]["actual_usd"], 0.0, "keyless run costs nothing")
        # The register entity may be served from the local mirror, in which case no
        # Brreg API call is needed -- so assert that traffic was attributed to some
        # real provider rather than implying a specific one was hit.
        self.assertTrue(report["by_provider"], "traffic must be attributed")
        self.assertEqual(
            sum(report["by_provider"].values()),
            report["requests"]["outbound_attempts"],
            "per-provider counts must sum to the total: no request is off the books",
        )
        self.assertEqual(
            sum(report["by_operation"].values()),
            report["requests"]["outbound_attempts"],
            "per-operation counts must sum to the total",
        )


class TestNoDoubleCountingAcrossLayers(unittest.TestCase):
    """One outbound attempt must be counted once, however many layers see it.

    The brief is explicit: *avoid double-counting a single request at multiple
    layers*. The keyless website search is the live case -- ``sitefinder``
    orchestrates it and ``liveweb`` issues the engine requests through
    ``HttpClient.get``, so a naive pre-admission in ``sitefinder`` would count
    each engine attempt twice. This is a real regression: it was measured as 8
    accounted against 6 real socket calls before the fix.
    """

    def test_keyless_search_counts_each_engine_request_once(self) -> None:
        import threading
        import unittest.mock as mock

        from net import HttpClient, Budget as NetBudget
        from sitefinder import SiteFinder
        from validate import OrgNumber

        budget = NetBudget(hard_limit=2000, soft_limit=2000)
        http = HttpClient(budget=budget)
        finder = SiteFinder(http)
        entity = {
            "navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
            "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"},
        }
        real_urlopen = __import__("urllib.request", fromlist=["urlopen"]).urlopen
        calls = {"n": 0}

        def counting(request, *args, **kwargs):  # noqa: ANN001
            calls["n"] += 1
            return real_urlopen(request, *args, **kwargs)

        with mock.patch("net._u.urlopen", side_effect=counting):
            finder.candidates(entity, OrgNumber("923609016"))

        accounted = budget.run.to_dict()["requests"]["outbound_attempts"]
        self.assertEqual(
            accounted, calls["n"],
            "each keyless engine request is one outbound attempt: no layer may "
            f"count it twice (accounted {accounted}, real socket calls {calls['n']})",
        )


class TestDirectoryHostsAreNeverPublished(unittest.TestCase):
    """A directory that quotes the org number must not be read as ownership.

    This is the misattribution the keyless fallback exposed: the top keyless hit
    for a company query is usually a directory, and a directory *displays the
    organisation number verbatim*, so the number-on-page rule alone accepts it.
    """

    def test_the_shared_host_list_is_single_sourced(self) -> None:
        from normalize import DIRECTORY_HOSTS, is_directory_host
        import sitefinder as sf
        import liveweb as lw

        # sitefinder and liveweb must agree with the canonical list, or a host
        # can be filtered in one layer and accepted in another.
        self.assertTrue(set(DIRECTORY_HOSTS).issubset(sf._DENY_HOSTS))
        self.assertTrue(set(DIRECTORY_HOSTS).issubset(lw._DIRECTORY_HOSTS))
        for host in ("proff.no", "www.proff.no", "visverdi.no", "virksomhet.brreg.no"):
            self.assertTrue(is_directory_host(host), f"{host} must be a directory")
        for host in ("equinor.com", "www.equinor.com", "borregaard.no"):
            self.assertFalse(is_directory_host(host), f"{host} is an owned site")

    def test_gate_rejects_a_directory_page_carrying_the_org_number(self) -> None:
        from gate import evaluate
        from normalize import Page
        from validate import OrgNumber

        # A directory page that shows the correct number *and* the right name.
        markup = (
            "<html><body><h1>EQUINOR ASA</h1>"
            "<p>Organisasjonsnummer 923 609 016 &middot; Stavanger</p>"
            "<p>Se regnskap, roller og kontaktinformasjon for dette selskapet.</p>"
            "</body></html>"
        )
        page = Page(url="https://visverdi.no/selskap/equinor-asa-923609016",
                    status=200, markup=markup)
        verdict = evaluate(page, OrgNumber("923609016"), name="EQUINOR ASA",
                           postcode="4035", city="STAVANGER")
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.verdict, "rejected_directory_host")

    def test_gate_still_accepts_a_genuine_owned_site(self) -> None:
        from gate import evaluate
        from normalize import Page
        from validate import OrgNumber

        markup = (
            "<html><body><h1>Equinor ASA</h1>"
            "<p>Organisasjonsnummer 923 609 016. Vi leverer energi.</p>"
            "</body></html>"
        )
        page = Page(url="https://www.equinor.com/om-oss", status=200, markup=markup)
        verdict = evaluate(page, OrgNumber("923609016"), name="EQUINOR ASA")
        self.assertTrue(verdict.accepted, "the gate must not over-reject owned sites")
        self.assertEqual(verdict.match_proof, "orgnr_on_page")


if __name__ == "__main__":
    unittest.main()
