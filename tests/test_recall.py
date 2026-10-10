"""Tests for the recall matrix, freshness, and the terminal-output audit.

These are the three things the roadmap calls the score levers, so they are
tested for the property that makes them trustworthy: the matrix measures two
different things (companies reached vs facts found), freshness never reads an
absent date as a fresh one, and the audit catches every way the one-output-per-
input contract can break.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # so `import run` (the top-level runner) resolves

from coverage import (  # noqa: E402
    freshness_report,
    missing_high_value_fields,
    recall_matrix,
    render_recall_matrix,
)
from envelope import Envelope, envelope_audit, render_audit  # noqa: E402
from profiles import Fact, Profile, freshness_of  # noqa: E402


def _fact(name: str, value, *, retrieved_at: str = "2026-10-08T00:00:00Z") -> Fact:
    return Fact(field=name, value=value, source_url="u", retrieved_at=retrieved_at,
                evidence="e", match_proof="registry", confidence=0.95)


def _profile(orgnr: str, **facts) -> Profile:
    p = Profile(org_number=orgnr, legal_name="X", run_date="2026-10-08")
    for name, value in facts.items():
        p.facts.append(_fact(name, value))
    return p


class TestRecallMatrix(unittest.TestCase):
    def _profiles(self) -> list[Profile]:
        return [
            _profile("1", legal_name="A", ceo="X", website="w", revenue_latest=1),
            _profile("2", legal_name="B", board_chair="Y", website="w"),
            _profile("3", legal_name="C"),   # company record only
        ]

    def test_matrix_has_two_numbers_per_domain(self) -> None:
        matrix = recall_matrix(self._profiles())
        for domain in ("company", "financials", "people_locations", "website",
                       "hiring_activity"):
            self.assertIn(domain, matrix)
            self.assertIn("companies_pct", matrix[domain])
            self.assertIn("facts_pct", matrix[domain])

    def test_companies_pct_and_facts_pct_differ(self) -> None:
        matrix = recall_matrix(self._profiles())
        # all three companies have a legal_name -> company domain covers all
        self.assertEqual(matrix["company"]["companies_pct"], 1.0)
        # only one of three filed revenue -> financials facts are low
        self.assertLess(matrix["financials"]["facts_pct"], 1.0)
        # nobody has jobs -> zero coverage
        self.assertEqual(matrix["hiring_activity"]["companies_pct"], 0.0)

    def test_empty_input_is_safe(self) -> None:
        matrix = recall_matrix([])
        self.assertEqual(matrix["company"]["companies_pct"], 0.0)

    def test_missing_high_value_fields_ranks_by_headroom(self) -> None:
        missing = missing_high_value_fields(self._profiles(), limit=5)
        self.assertTrue(missing)
        # ranked by weight * absence; the first entry is never a fully-present field
        self.assertGreater(missing[0]["absent"], 0)
        self.assertTrue(all("headroom" in m for m in missing))

    def test_render_contains_the_header(self) -> None:
        text = render_recall_matrix(recall_matrix(self._profiles()),
                                    missing_high_value_fields(self._profiles()))
        self.assertIn("RECALL MATRIX", text)
        self.assertIn("Companies", text)
        self.assertIn("Facts", text)


class TestFreshness(unittest.TestCase):
    def test_absent_date_is_unknown_not_recent(self) -> None:
        self.assertEqual(freshness_of(""), "unknown")
        self.assertEqual(freshness_of("not-a-date"), "unknown")

    def test_now_is_recent(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.assertEqual(freshness_of(now), "recent")

    def test_old_date_is_stale(self) -> None:
        self.assertEqual(freshness_of("2000-01-01T00:00:00Z"), "stale")

    def test_week_old_is_this_week(self) -> None:
        when = (datetime.now(timezone.utc) - timedelta(days=3)).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
        self.assertEqual(freshness_of(when), "this_week")

    def test_fact_carries_freshness(self) -> None:
        f = _fact("legal_name", "X", retrieved_at="2000-01-01T00:00:00Z")
        self.assertEqual(f.freshness, "stale")
        self.assertEqual(f.to_dict()["freshness"], "stale")

    def test_freshness_report_is_per_domain(self) -> None:
        p = _profile("1", legal_name="A", ceo="X")
        report = freshness_report(p)
        self.assertIn("company", report)
        self.assertIn("freshness", report["company"])


class TestEnvelopeAudit(unittest.TestCase):
    def _env(self, orgnr: str, status: str = "available") -> Envelope:
        return Envelope(orgnr=orgnr, status=status, reason="r")

    def test_clean_run_is_ok(self) -> None:
        audit = envelope_audit(["1", "2"], [self._env("1"), self._env("2")])
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["inputs"], 2)
        self.assertEqual(audit["outputs"], 2)
        self.assertEqual(audit["missing_outputs"], [])

    def test_missing_output_is_caught(self) -> None:
        audit = envelope_audit(["1", "2"], [self._env("1")])
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["missing_outputs"], ["2"])

    def test_duplicate_output_is_caught(self) -> None:
        audit = envelope_audit(["1"], [self._env("1"), self._env("1")])
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["duplicate_outputs"], ["1"])

    def test_unknown_orgnr_is_caught(self) -> None:
        audit = envelope_audit(["1"], [self._env("1"), self._env("999")])
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["unknown_orgnrs"], ["999"])

    def test_inputs_are_normalised_before_comparison(self) -> None:
        # "986 587 098" and "986587098" are the same company
        audit = envelope_audit(["986 587 098"], [self._env("986587098")])
        self.assertTrue(audit["ok"])

    def test_status_counts_are_reported(self) -> None:
        audit = envelope_audit(
            ["1", "2", "3"],
            [self._env("1", "available"), self._env("2", "not_available"),
             self._env("3", "blocked")],
        )
        self.assertEqual(audit["matched"], 1)
        self.assertEqual(audit["not_available"], 1)
        self.assertEqual(audit["blocked"], 1)

    def test_render_has_the_headline_lines(self) -> None:
        text = render_audit(envelope_audit(["1"], [self._env("1")]))
        self.assertIn("INPUTS:", text)
        self.assertIn("Missing outputs:", text)


class TestEvaluatorContract(unittest.TestCase):
    """The whole point: consume the evaluator's file and return one row per input.

    This runs the real pipeline offline on a file that looks like the batch an
    evaluator would hand over -- valid numbers, invalid tokens, duplicate
    spellings and blank lines -- and asserts the execution contract holds.
    """

    def test_file_batch_yields_one_terminal_envelope_per_input(self) -> None:
        import tempfile

        from envelope import STATUSES  # noqa: PLC0415
        from pipeline import Settings, read_numbers, run  # noqa: PLC0415

        # ignore_cleanup_errors: the worker threads keep sqlite handles briefly on
        # Windows, so the temp dir may not delete instantly. Not a product issue.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmpdir = Path(tmp)
            numbers = ["986587098", "923609016", "974760673"]
            numbers += [f"{100000000 + i:09d}" for i in range(90)]
            lines = ["# evaluator batch", *numbers,
                     "NO 986 587 098 MVA", "986587098", "not-a-number", ""]
            src = tmpdir / "orgnrs.txt"
            src.write_text("\n".join(lines), encoding="utf-8")

            inputs = read_numbers(src)
            settings = Settings(
                profiles_dir=tmpdir / "profiles", runs_dir=tmpdir,
                registry_db=tmpdir / "reg.sqlite", snapshot_db=tmpdir / "snap.sqlite",
                offline=True, quiet=True,
            )
            run(inputs, settings=settings)
            envs = [json.loads(x) for x in
                    (tmpdir / "envelopes.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(envs), len(inputs))                     # N in -> N out
        self.assertEqual({e["orgnr"] for e in envs}, set(inputs))    # same set
        self.assertEqual(len({e["orgnr"] for e in envs}), len(envs))  # no duplicates
        for env in envs:
            self.assertIn(env["status"], STATUSES)                   # all terminal

    def test_read_numbers_normalises_and_dedupes(self) -> None:
        import tempfile

        from pipeline import read_numbers  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "n.txt"
            src.write_text("986 587 098\n986587098\nNO 986 587 098 MVA\n923609016\n",
                           encoding="utf-8")
            numbers = read_numbers(src)
        self.assertEqual(numbers, ["986587098", "923609016"])


class TestUniverseAdapter(unittest.TestCase):
    """The evaluator's public universe is the identity anchor; parse it exactly."""

    RECORDS = [
        {"organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS",
         "legal_form": "AS", "employees": 11, "bankrupt": False, "liquidating": False,
         "municipality": "SANDNES", "municipality_number": "1108",
         "industry_code": "43.210", "industry_label": "Elektrisk installasjonsarbeid",
         "website": "www.sandnes-elektriske.no", "latest_submitted_accounts": "2025"},
        {"organisation_number": "810059672", "name": "AASEN & FARSTAD AS",
         "legal_form": "AS", "employees": None, "bankrupt": False, "liquidating": True,
         "municipality": "MOLDE", "municipality_number": "1506",
         "industry_code": "68.200", "industry_label": "Utleie",
         "website": "", "latest_submitted_accounts": "2025"},
    ]

    def _file(self, tmp: str) -> Path:
        path = Path(tmp) / "uni.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in self.RECORDS), encoding="utf-8")
        return path

    def test_maps_universe_fields_to_the_register_shape(self) -> None:
        from universe import universe_to_entity  # noqa: PLC0415

        entity = universe_to_entity(self.RECORDS[0])
        self.assertEqual(entity["organisasjonsnummer"], "810034882")
        self.assertEqual(entity["navn"], "SANDNES ELEKTRISKE AS")
        self.assertEqual(entity["organisasjonsform"]["kode"], "AS")
        self.assertEqual(entity["naeringskode1"]["kode"], "43.210")
        self.assertEqual(entity["antallAnsatte"], 11)
        self.assertEqual(entity["hjemmeside"], "www.sandnes-elektriske.no")

    def test_null_employees_stays_absent_never_zero(self) -> None:
        from universe import universe_to_entity  # noqa: PLC0415

        entity = universe_to_entity(self.RECORDS[1])
        self.assertNotIn("antallAnsatte", entity)          # null -> absent
        self.assertTrue(entity.get("underAvvikling"))      # liquidating -> flag

    def test_orgnrs_are_normalised_and_deduped(self) -> None:
        import tempfile

        from universe import universe_orgnrs  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            numbers = universe_orgnrs(path)
        self.assertEqual(numbers, ["810034882", "810059672"])

    def test_read_numbers_accepts_the_universe_jsonl(self) -> None:
        import tempfile

        from pipeline import read_numbers  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            numbers = read_numbers(path)
        self.assertEqual(numbers, ["810034882", "810059672"])

    def test_find_in_universe(self) -> None:
        import tempfile

        from universe import find_in_universe  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            found = find_in_universe(path, "810034882")
            self.assertEqual(found["name"], "SANDNES ELEKTRISKE AS")
            self.assertIsNone(find_in_universe(path, "999999999"))

    def test_seed_mirror_populates_an_offline_registry(self) -> None:
        import tempfile

        from registry import RegistryStore  # noqa: PLC0415
        from universe import seed_mirror_from_universe  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            store = RegistryStore(Path(tmp) / "reg.sqlite")
            seeded = seed_mirror_from_universe(store, path)
            count = store.count()
            entity = store.lookup("810034882")
            store.close()
        self.assertEqual(seeded, 2)
        self.assertEqual(count, 2)
        self.assertEqual(entity["navn"], "SANDNES ELEKTRISKE AS")


class TestEnvelopeStatusMapping(unittest.TestCase):
    """A source we could not reach is 'blocked' -- never confused with a 404."""

    def _status(self, error: str) -> str:
        from pipeline import CompanyResult, _envelope_status  # noqa: PLC0415

        return _envelope_status(CompanyResult(org_number="1", error=error), [])[0]

    def test_unreachable_register_is_blocked(self) -> None:
        self.assertEqual(self._status("register unreachable: HttpError('HTTP 0')"), "blocked")

    def test_absent_company_is_not_available(self) -> None:
        self.assertEqual(
            self._status("not present in the registry (checked the mirror and live)"),
            "not_available",
        )

    def test_bad_checksum_is_not_applicable(self) -> None:
        self.assertEqual(self._status("rejected by checksum: '123'"), "not_applicable")

    def test_unknown_error_is_failed(self) -> None:
        self.assertEqual(self._status("something unexpected"), "failed")


class TestLiveLookup(unittest.TestCase):
    """The one-number live path: reject bad input cleanly, never hit the network."""

    def test_cli_lookup_rejects_an_invalid_number(self) -> None:
        import cli  # noqa: PLC0415

        self.assertEqual(cli.main(["lookup", "123"]), 2)
        self.assertEqual(cli.main(["lookup", "not-a-number"]), 2)

    def test_server_lookup_rejects_an_invalid_number(self) -> None:
        import serve  # noqa: PLC0415

        self.assertEqual(serve._lookup("123").get("error"), "invalid organisation number")

    def test_run_py_exposes_the_lookup_task(self) -> None:
        import run  # noqa: PLC0415

        self.assertIn("lookup", run.TASKS)
        self.assertIn("seed-universe", run.TASKS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
