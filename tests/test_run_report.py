"""Task #45 §7 — the run-level execution report.

The brief requires the batch to emit a machine-readable report with run ID and
timestamps, elapsed time, outbound attempts and outcomes, retries and redirects,
per-provider and per-operation counts, actual/reserved/unknown cost, cache hits,
profiles attempted/completed/deferred, facts added/updated/unchanged/rejected,
search outcomes and an early-termination reason.

The critical rule these tests enforce: **no number may be fabricated or
hardcoded**. Every value must be a fold over the real per-company results or the
live budget accountant, so a change in the run is visible in the report.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from budget import RunBudget  # noqa: E402
from pipeline import RunReport, Settings, run  # noqa: E402

#: Three real, stable entities. Small enough to stay fast; real enough that the
#: report is produced from genuine register reads rather than fixtures.
SAMPLE = ["923609016", "914778271", "984851006"]


class _RunReportBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _settings(self) -> Settings:
        return Settings(
            profiles_dir=self.tmp / "profiles",
            runs_dir=self.tmp / "runs",
            cache_dir=ROOT / "data" / "cache",
            registry_db=ROOT / "data" / "registry.sqlite",
            snapshot_db=self.tmp / "snapshots.sqlite",
            llm_cache=ROOT / "data" / "llm_cache",
            offline=False,
            fetch_web=False,
            fetch_financials=False,
            fetch_jobs=False,
            fetch_activity=False,
            use_llm=False,
            quiet=True,
        )

    def _run(self) -> dict:
        return run(SAMPLE, settings=self._settings()).to_dict()["execution"]


class TestReportHasEveryRequiredField(_RunReportBase):
    """§7's field list, asserted one by one against a real run."""

    def test_the_report_is_machine_readable_and_complete(self) -> None:
        e = self._run()
        for key in ("run_id", "started_at", "finished_at", "elapsed_seconds"):
            self.assertIn(key, e, f"missing §7 field: {key}")
            self.assertTrue(e[key] or e[key] == 0.0, f"{key} is empty on a real run")

    def test_profiles_attempted_completed_deferred_are_reported(self) -> None:
        e = self._run()
        p = e["profiles"]
        self.assertEqual(p["requested"], len(SAMPLE))
        self.assertEqual(p["attempted"], len(SAMPLE))
        self.assertEqual(p["completed"], len(SAMPLE))
        self.assertEqual(p["deferred"], 0, "a clean run defers nothing")

    def test_outbound_accounting_is_present_and_internally_consistent(self) -> None:
        e = self._run()
        o = e["outbound"]
        for key in ("attempts", "succeeded", "failed", "redirected", "retried", "cache_hits"):
            self.assertIn(key, o, f"missing outbound field: {key}")
        # Succeeded + failed cannot exceed attempts: every outcome belongs to a
        # counted attempt.
        self.assertLessEqual(
            o["succeeded"] + o["failed"], o["attempts"],
            "outcomes exceed attempts; an outcome was recorded without an attempt",
        )

    def test_by_provider_and_by_operation_sum_to_the_total(self) -> None:
        e = self._run()
        attempts = e["outbound"]["attempts"]
        if attempts == 0:
            self.skipTest("this run made no outbound requests")
        self.assertEqual(
            sum(e["by_provider"].values()), attempts,
            "per-provider counts must sum to the total: no request off the books",
        )
        self.assertEqual(
            sum(e["by_operation"].values()), attempts,
            "per-operation counts must sum to the total",
        )

    def test_cost_reports_actual_reserved_and_unknown_separately(self) -> None:
        e = self._run()
        c = e["cost"]
        for key in ("actual_usd", "reserved_usd", "worst_case_usd", "unknown_cost_operations"):
            self.assertIn(key, c, f"missing cost field: {key}")
        self.assertEqual(c["actual_usd"], 0.0, "a keyless run costs nothing")
        self.assertEqual(c["unknown_cost_operations"], 0, "nothing unknown was attempted")

    def test_search_outcomes_are_all_four_reported(self) -> None:
        e = self._run()
        for key in ("ok", "empty", "skipped", "failed"):
            self.assertIn(key, e["searches"], f"missing search outcome: {key}")

    def test_termination_reason_is_present_and_means_completed(self) -> None:
        e = self._run()
        self.assertEqual(e["termination_reason"], "completed")
        self.assertFalse(e["early_termination"])


class TestReportIsDerivedNotHardcoded(_RunReportBase):
    """The values must move when the run moves."""

    def test_facts_added_then_unchanged_across_two_runs(self) -> None:
        s = self._settings()
        first = run(SAMPLE, settings=s).to_dict()["execution"]["facts"]
        second = run(SAMPLE, settings=s).to_dict()["execution"]["facts"]

        self.assertGreater(first["added"], 0, "a fresh run adds facts")
        self.assertEqual(first["updated"], 0, "nothing to update on a first run")
        # A repeat run over an unchanged register re-publishes identically.
        self.assertEqual(second["added"], 0, "a repeat run adds nothing new")
        self.assertGreater(second["unchanged"], 0, "a repeat run reports unchanged facts")

    def test_fact_partition_is_disjoint_and_sums_to_the_published_total(self) -> None:
        s = self._settings()
        rep = run(SAMPLE, settings=s)
        e = rep.to_dict()["execution"]["facts"]
        published = sum(int(r.get("facts") or 0) for r in rep.to_dict()["results"])
        total = e["added"] + e["updated"] + e["unchanged"]
        self.assertEqual(
            total, published,
            f"added+updated+unchanged ({total}) must equal the published facts ({published})",
        )

    def test_a_smaller_batch_produces_a_smaller_report(self) -> None:
        one = run(SAMPLE[:1], settings=self._settings()).to_dict()["execution"]
        many = run(SAMPLE, settings=self._settings()).to_dict()["execution"]
        self.assertEqual(one["profiles"]["requested"], 1)
        self.assertLess(one["profiles"]["requested"], many["profiles"]["requested"])


class TestReportReflectsEarlyTermination(_RunReportBase):
    """An interrupted run must report why, not report success."""

    def test_finalise_uses_the_budget_termination_reason(self) -> None:
        # Build a reporter by hand with a budget that recorded a deadline stop.
        b = RunBudget(max_requests=10)
        b._refuse("deadline", "registry") if hasattr(b, "_refuse") else None
        b.cancel("deadline")
        rep = RunReport(run_id="probe", companies_requested=3)
        rep.results = [{"ok": True, "facts": 5, "changes": 0, "search": {"status": "ok"}}]
        rep.budget = b.to_dict()
        rep.finalise()

        self.assertEqual(rep.termination_reason, "deadline")
        self.assertEqual(rep.profiles_attempted, 1)
        self.assertEqual(rep.profiles_completed, 1)
        # Requested 3, completed 1 -> 2 deferred, and that must be explicit.
        self.assertEqual(rep.profiles_deferred, 2)

    def test_deferred_counts_requests_that_did_not_complete(self) -> None:
        rep = RunReport(run_id="probe", companies_requested=5)
        rep.results = [
            {"ok": True, "facts": 1, "changes": 0, "search": {"status": "ok"}},
            {"ok": False, "facts": 0, "changes": 0, "search": {"status": "failed"}},
        ]
        rep.finalise()
        self.assertEqual(rep.profiles_completed, 1)
        self.assertEqual(rep.profiles_deferred, 4, "5 requested, 1 completed -> 4 deferred")
        self.assertEqual(rep.searches_failed, 1)


class TestReportIsWrittenAtomically(_RunReportBase):
    def test_the_report_file_is_written_and_parses(self) -> None:
        import json

        s = self._settings()
        rep = run(SAMPLE, settings=s)
        path = rep.write(s.runs_dir)
        self.assertTrue(path.exists())
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["run_id"], rep.run_id)
        self.assertIn("execution", raw, "the §7 block must be in the written artefact")
        # No temp files may survive the atomic write.
        leftovers = [p.name for p in s.runs_dir.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


if __name__ == "__main__":
    unittest.main()
