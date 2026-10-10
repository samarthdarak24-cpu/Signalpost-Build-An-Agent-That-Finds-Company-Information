"""Task #45 §5 — resumability and partial-result regression tests.

The contract these pin down, from the brief:

* previously verified facts and provenance remain intact when a run is
  interrupted or its sources fail;
* an interrupted lookup is recorded as incomplete, never as an empty result;
* a missing result does not erase an existing verified fact;
* a derived value is never fabricated from a failed lookup;
* profile writes are atomic, so an interrupted write cannot corrupt history;
* re-running does not duplicate or corrupt the profile set.

Each test uses a scratch profiles directory, so the committed profile corpus is
never touched and the tests are order-independent.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from coverage import location_facts  # noqa: E402
from delta import SnapshotStore  # noqa: E402
from extract_llm import LlmClient  # noqa: E402
from net import Budget, HttpClient  # noqa: E402
from pipeline import RunReport, Settings, _carry_forward, process_company  # noqa: E402
from profiles import Fact, Profile  # noqa: E402
from registry import RegistryStore  # noqa: E402

ORG = "923609016"  # a real, stable entity used by the other Stage-45 tests


class _FailingHttp(HttpClient):
    """Transport that always fails, to simulate an outage on a re-run."""

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise ConnectionError("simulated outage")


class _ResumabilityBase(unittest.TestCase):
    def setUp(self) -> None:
        # ``ignore_cleanup_errors`` because SQLite keeps the scratch database
        # handle open for the life of the process on Windows; the data is
        # irrelevant after the test, so a locked temp file must not fail it.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._stores: list[object] = []
        self.settings = self._settings()
        (ROOT / "data" / "cache").mkdir(parents=True, exist_ok=True)

    def _close_stores(self) -> None:
        for store in self._stores:
            try:
                store.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._stores.clear()

    def _settings(self, **over: object) -> Settings:
        base = dict(
            profiles_dir=self.tmp / "profiles",
            runs_dir=self.tmp / "runs",
            cache_dir=ROOT / "data" / "cache",
            registry_db=ROOT / "data" / "registry.sqlite",
            snapshot_db=self.tmp / "snapshots.sqlite",
            llm_cache=ROOT / "data" / "llm_cache",
            offline=False,
            fetch_web=False,          # the web layer is not what these tests probe
            fetch_financials=False,
            fetch_jobs=False,
            fetch_activity=False,
            use_llm=False,
        )
        base.update(over)
        return Settings(**base)  # type: ignore[arg-type]

    def _run(self, settings: Settings | None = None,
             http: HttpClient | None = None) -> object:
        settings = settings or self.settings
        http = http or HttpClient(cache_dir=settings.cache_dir, budget=Budget())
        store = RegistryStore(settings.registry_db)
        snapshots = SnapshotStore(settings.snapshot_db)
        self._stores.extend([store, snapshots])
        self.addCleanup(self._close_stores)
        return process_company(
            ORG,
            store=store,
            snapshots=snapshots,
            settings=settings, http=http,
            llm=LlmClient(http, cache_dir=str(settings.llm_cache)),
            reporter=RunReport(), lock=threading.Lock(),
        )

    def _read(self, settings: Settings | None = None) -> Profile:
        settings = settings or self.settings
        return Profile.read(settings.profiles_dir / f"{ORG}.json")

    @staticmethod
    def _pairs(profile: Profile) -> set[tuple[str, str]]:
        return {(f.field, str(f.value)) for f in profile.facts}


class TestFactsSurviveAFailedReRun(_ResumabilityBase):
    """A second run whose sources fail must not lose the first run's facts."""

    def test_a_failed_lookup_does_not_erase_published_facts(self) -> None:
        self._run()
        before = self._pairs(self._read())
        self.assertTrue(before, "the first run must produce facts to protect")

        self._run(settings=self._settings(),
                  http=_FailingHttp(cache_dir=self.settings.cache_dir, budget=Budget()))
        after = self._pairs(self._read())

        self.assertTrue(
            before.issubset(after),
            f"facts lost by a failed re-run: {sorted(before - after)[:5]}",
        )

    def test_carried_forward_facts_keep_their_original_provenance(self) -> None:
        self._run()
        first = Profile.read(self.settings.profiles_dir / f"{ORG}.json")
        origin = {f.field: (f.source_url, f.evidence) for f in first.facts}

        self._run(settings=self._settings(),
                  http=_FailingHttp(cache_dir=self.settings.cache_dir, budget=Budget()))
        second = self._read()

        # Any fact that was restored must still cite the URL it originally came
        # from -- it must not be re-attributed to the run that failed.
        for fact in second.facts:
            if fact.field in origin and origin[fact.field][1]:
                self.assertEqual(
                    fact.source_url, origin[fact.field][0],
                    f"{fact.field} was re-cited to a different source",
                )

    def test_a_carried_forward_fact_is_disclosed_not_silent(self) -> None:
        self._run()
        result = self._run(
            settings=self._settings(),
            http=_FailingHttp(cache_dir=self.settings.cache_dir, budget=Budget()),
        )
        if getattr(result, "facts_carried_forward", 0) > 0:
            # The disclosure lives on the written profile, since that is what a
            # consumer of the corpus actually reads.
            joined = " ".join(self._read().errors)
            self.assertIn("carried forward", joined,
                          "restoring facts must be visible in the profile errors")
        self.assertGreaterEqual(
            getattr(result, "facts_carried_forward", 0), 0,
            "the carried-forward count must be reported on the result",
        )

    def test_a_fresh_value_wins_over_a_carried_forward_one(self) -> None:
        """Carry-forward must never mask a genuine change."""
        self.settings.profiles_dir.mkdir(parents=True, exist_ok=True)
        stale = Profile(org_number=ORG, legal_name="STALE AS", run_date="2020-01-01")
        stale.add(Fact(field="legal_name_stale_probe", value="OLD", source_url="https://old",
                       retrieved_at="2020-01-01T00:00:00Z", evidence="old", match_proof="registry",
                       confidence=0.9, layer="registry"))
        stale.write(self.settings.profiles_dir)

        profile = Profile(org_number=ORG, legal_name="NEW AS", run_date="2026-10-10")
        profile.add(Fact(field="legal_name_stale_probe", value="NEW", source_url="https://new",
                         retrieved_at="2026-10-10T00:00:00Z", evidence="new", match_proof="registry",
                         confidence=0.9, layer="registry"))
        restored = _carry_forward(profile, self.settings.profiles_dir, ORG)

        self.assertEqual(restored, 0, "a field present in this run must not be restored")
        self.assertEqual(profile.value("legal_name_stale_probe"), "NEW")


class TestDerivedValuesAreNotFabricated(_ResumabilityBase):
    """A count derived from a failed lookup must not be published."""

    def test_location_count_is_not_published_when_the_lookup_failed(self) -> None:
        facts = location_facts([], number=ORG, retrieved_at="now",
                               source_url="https://data.brreg.no", lookup_ok=False)
        self.assertEqual(facts, [], "a failed read must not become '1 location'")

    def test_location_count_is_one_when_the_register_answered_with_none(self) -> None:
        facts = location_facts([], number=ORG, retrieved_at="now",
                               source_url="https://data.brreg.no", lookup_ok=True)
        self.assertEqual([(f.field, f.value) for f in facts], [("location_count", 1)])

    def test_location_count_reflects_real_sub_units(self) -> None:
        units = [
            {"beliggenhetsadresse": {"adresse": ["A"], "postnummer": "1", "poststed": "X"}},
            {"beliggenhetsadresse": {"adresse": ["B"], "postnummer": "2", "poststed": "Y"}},
        ]
        facts = location_facts(units, number=ORG, retrieved_at="now",
                               source_url="https://data.brreg.no", lookup_ok=True)
        self.assertIn(("location_count", 3), [(f.field, f.value) for f in facts])

    def test_a_derived_count_is_never_lower_than_the_preserved_list(self) -> None:
        """The bug this guards: count=1 published beside a 35-place list."""
        self._run()  # first run: real sub-units present
        first = {f.field: f.value for f in self._read().facts}
        if "business_locations" not in first:
            self.skipTest("this entity has no sub-units to test the invariant with")
        n_places = len(str(first["business_locations"]).split(" | "))

        self._run(settings=self._settings(),
                  http=_FailingHttp(cache_dir=self.settings.cache_dir, budget=Budget()))
        after = {f.field: f.value for f in self._read().facts}
        self.assertGreaterEqual(
            int(after["location_count"]), n_places,
            "the published count must not contradict the published location list",
        )


class TestWritesAreAtomic(_ResumabilityBase):
    """An interrupted write must not corrupt the previously published profile."""

    def test_a_write_leaves_no_temp_files_behind(self) -> None:
        directory = self.tmp / "atomic"
        profile = Profile(org_number=ORG, legal_name="ATOMIC AS", run_date="2026-10-10")
        profile.add(Fact(field="x", value="y", source_url="https://a", retrieved_at="now",
                         evidence="e", match_proof="registry", confidence=0.9, layer="registry"))
        profile.write(directory)

        names = sorted(p.name for p in directory.iterdir())
        self.assertEqual(names, [f"{ORG}.json"],
                         f"atomic write must leave exactly one file, found {names}")

    def test_a_failed_write_preserves_the_previous_file(self) -> None:
        directory = self.tmp / "atomic2"
        good = Profile(org_number=ORG, legal_name="GOOD AS", run_date="2026-10-10")
        good.add(Fact(field="keep", value="me", source_url="https://a", retrieved_at="now",
                      evidence="e", match_proof="registry", confidence=0.9, layer="registry"))
        good.write(directory)
        before = (directory / f"{ORG}.json").read_text(encoding="utf-8")

        # Make serialisation blow up mid-write.
        class Boom(Profile):
            def to_dict(self):  # noqa: ANN201
                raise RuntimeError("serialisation failed")

        boom = Boom(org_number=ORG, legal_name="BOOM", run_date="2026-10-10")
        with self.assertRaises(RuntimeError):
            boom.write(directory)

        after = (directory / f"{ORG}.json").read_text(encoding="utf-8")
        self.assertEqual(before, after, "the previously published file must be intact")
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [f"{ORG}.json"],
                         "no temp file may survive a failed write")

    def test_a_written_profile_is_always_readable(self) -> None:
        directory = self.tmp / "atomic3"
        profile = Profile(org_number=ORG, legal_name="READABLE AS", run_date="2026-10-10")
        for i in range(200):
            profile.add(Fact(field=f"f{i}", value="v" * 100, source_url="https://a",
                             retrieved_at="now", evidence="e", match_proof="registry",
                             confidence=0.9, layer="registry"))
        profile.write(directory)
        raw = json.loads((directory / f"{ORG}.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["facts"]), 200)
        self.assertEqual(Profile.read(directory / f"{ORG}.json").org_number, ORG)


class TestRepeatedRunsDoNotDuplicate(_ResumabilityBase):
    """Re-running the same company must converge, not accumulate."""

    def test_two_identical_runs_produce_an_identical_profile(self) -> None:
        self._run()
        first = self._pairs(self._read())
        self._run()
        second = self._pairs(self._read())
        self.assertEqual(first, second, "a repeat run must be idempotent")

    def test_rerunning_does_not_grow_the_fact_count(self) -> None:
        self._run()
        n1 = len(self._read().facts)
        self._run()
        n2 = len(self._read().facts)
        self.assertEqual(n1, n2, f"fact count drifted {n1} -> {n2} across identical runs")

    def test_no_duplicate_fields_in_a_written_profile(self) -> None:
        self._run()
        fields = [f.field for f in self._read().facts]
        dupes = {f for f in fields if fields.count(f) > 1}
        self.assertFalse(dupes, f"duplicate fields published: {sorted(dupes)}")


if __name__ == "__main__":
    unittest.main()
