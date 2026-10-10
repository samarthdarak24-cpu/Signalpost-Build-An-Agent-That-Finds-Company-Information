"""Tests for the random-company evaluation harness.

The harness exists to catch problems, so it is tested by handing it known-bad
profiles and asserting it *notices*. A checker that always returns 100% would
pass every "the score is high" assertion while being useless, so these tests
deliberately construct the failures it is supposed to find.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

import evaluate as E  # noqa: E402


def _payload(number: str = "923609016", **over) -> dict:
    entity = {
        "organisasjonsnummer": number,
        "navn": "EQUINOR ASA",
        "naeringskode1": {"kode": "06.100", "beskrivelse": "Utvinning av råolje"},
        "stiftelsesdato": "1972-06-14",
        "registreringsdatoEnhetsregisteret": "1995-03-01",
        "antallAnsatte": 21000,
        "forretningsadresse": {"kommune": "STAVANGER", "poststed": "STAVANGER",
                               "postnummer": "4035"},
        "epostadresse": "post@equinor.com",
        "telefon": "51990000",
        "hjemmeside": "equinor.com",
        "organisasjonsform": {"beskrivelse": "Allmennaksjeselskap"},
    }
    entity.update(over)
    return entity


def _profile(number: str = "923609016", *, name: str = "EQUINOR ASA",
             fields: list[str] | None = None, bad_fact: dict | None = None) -> dict:
    fields = fields if fields is not None else list(E.CHECKABLE_FIELD_NAMES)
    facts = []
    for f in fields:
        value: object = "x"
        if f == "legal_name":
            value = name
        elif f == "employees":
            value = 21000
        facts.append({
            "field": f, "value": value,
            "source_url": "https://data.brreg.no/x",
            "retrieved_at": "2026-10-10T00:00:00Z",
            "evidence": f"{f} from the register",
            "match_proof": "orgnr_exact",
            "confidence": 0.95,
            "org_number": number,
        })
    if bad_fact:
        facts.append(bad_fact)
    return {
        "org_number": number, "legal_name": name, "run_date": "2026-10-10",
        "facts": facts, "empty_fields": [], "errors": [],
    }


class _TempProfiles:
    """Point the harness at a scratch directory for one test."""

    def __init__(self, profiles: dict[str, dict]) -> None:
        self.profiles = profiles
        self._saved = E.PROFILES

    def __enter__(self):
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="eval-test-"))
        for number, prof in self.profiles.items():
            (self.dir / f"{number}.json").write_text(
                json.dumps(prof, ensure_ascii=False), encoding="utf-8"
            )
        E.PROFILES = self.dir
        return self.dir

    def __exit__(self, *exc):
        E.PROFILES = self._saved
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class TestHarnessDetectsProblems(unittest.TestCase):
    """Every assertion here is that the harness reports a *failure*."""

    def test_clean_profile_scores_full(self) -> None:
        with _TempProfiles({"923609016": _profile()}):
            ev = E.evaluate(["923609016"], seed_payload={"923609016": _payload()})
        self.assertEqual(ev.identity_rate, 1.0)
        self.assertEqual(ev.supported_rate, 1.0)
        self.assertEqual(ev.contamination, [])
        self.assertEqual(ev.coverage, 1.0)

    def test_wrong_company_name_is_caught(self) -> None:
        prof = _profile(name="NORSK HYDRO ASA")
        with _TempProfiles({"923609016": prof}):
            ev = E.evaluate(["923609016"], seed_payload={"923609016": _payload()})
        self.assertEqual(ev.identity_ok, 0)
        self.assertEqual(len(ev.identity_mismatch), 1)
        self.assertIn("EQUINOR ASA", ev.identity_mismatch[0])
        self.assertIn("NORSK HYDRO ASA", ev.identity_mismatch[0])

    def test_foreign_org_number_on_a_fact_is_caught(self) -> None:
        foreign = {
            "field": "city", "value": "OSLO",
            "source_url": "https://example.no", "retrieved_at": "2026-10-10T00:00:00Z",
            "evidence": "Oslo", "match_proof": "orgnr_exact", "confidence": 0.9,
            "org_number": "999888777",
        }
        with _TempProfiles({"923609016": _profile(bad_fact=foreign)}):
            ev = E.evaluate(["923609016"], seed_payload={"923609016": _payload()})
        self.assertEqual(len(ev.contamination), 1)
        self.assertIn("999888777", ev.contamination[0])

    def test_unsupported_fact_lowers_precision(self) -> None:
        bare = {
            "field": "city", "value": "STAVANGER",
            "source_url": "", "retrieved_at": "", "evidence": "",
            "match_proof": "", "confidence": 0.5, "org_number": "923609016",
        }
        with _TempProfiles({"923609016": _profile(bad_fact=bare)}):
            ev = E.evaluate(["923609016"], seed_payload={"923609016": _payload()})
        self.assertLess(ev.supported_rate, 1.0)
        self.assertEqual(ev.facts_supported, ev.facts_total - 1)

    def test_missing_field_lowers_coverage(self) -> None:
        fields = [f for f in E.CHECKABLE_FIELD_NAMES if f != "industry"]
        with _TempProfiles({"923609016": _profile(fields=fields)}):
            ev = E.evaluate(["923609016"], seed_payload={"923609016": _payload()})
        self.assertLess(ev.coverage, 1.0)
        self.assertEqual(ev.per_field["industry"]["present"], 0)
        self.assertEqual(ev.per_field["industry"]["offered"], 1)

    def test_a_failed_lookup_is_not_counted_as_a_miss(self) -> None:
        """The brief's rule, enforced by the harness itself."""
        prof = _profile(fields=[f for f in E.CHECKABLE_FIELD_NAMES if f != "website"])
        prof["empty_fields"] = [{
            "field": "website",
            "reason": "website not determined: the lookup did not complete (no key)",
            "candidates_considered": 0,
        }]
        with _TempProfiles({"923609016": prof}):
            ev = E.evaluate(["923609016"], seed_payload={"923609016": _payload()})
        self.assertNotIn("website", ev.per_field,
                         "an unavailable lookup must leave the denominator alone")
        self.assertEqual(ev.slots_unavailable, 1)

    def test_an_empty_result_is_counted_as_a_real_miss(self) -> None:
        """The other half: 'we looked and found nothing' *is* a miss."""
        prof = _profile(fields=[f for f in E.CHECKABLE_FIELD_NAMES if f != "website"])
        prof["empty_fields"] = [{
            "field": "website",
            "reason": "ownership gate found no page carrying the organisation number",
            "candidates_considered": 3,
        }]
        with _TempProfiles({"923609016": prof}):
            ev = E.evaluate(["923609016"], seed_payload={"923609016": _payload()})
        self.assertIn("website", ev.per_field)
        self.assertEqual(ev.per_field["website"]["present"], 0)
        self.assertEqual(ev.slots_unavailable, 0)


class TestMissingGroundTruthIsNotAFailure(unittest.TestCase):
    """A company we cannot check must not be scored as a mismatch.

    This pins a mistake the harness actually made: with ground truth for only
    some companies, identity accuracy was divided by the whole batch, which
    turned 1,001 unchecked companies into 1,001 apparent identity failures -- a
    fabricated disqualifying finding produced entirely by missing test data.
    """

    def test_unchecked_companies_do_not_lower_identity_accuracy(self) -> None:
        profiles = {"923609016": _profile()}
        with _TempProfiles(profiles):
            ev = E.evaluate(["923609016", "111111111", "222222222"],
                            seed_payload={"923609016": _payload()})
        self.assertEqual(ev.n, 1, "only the company with a profile is evaluated")
        self.assertEqual(ev.identity_checked, 1)
        self.assertEqual(ev.identity_rate, 1.0)
        self.assertEqual(ev.identity_mismatch, [])

    def test_identity_accuracy_denominator_is_the_checkable_set(self) -> None:
        profiles = {"923609016": _profile(), "974760673": _profile("974760673")}
        payload = {"923609016": _payload(), "974760673": None}
        del payload["974760673"]
        with _TempProfiles(profiles):
            ev = E.evaluate(list(profiles), seed_payload=payload)
        self.assertEqual(ev.identity_checked, 1,
                         "a company with no payload is not checkable")
        self.assertEqual(ev.identity_rate, 1.0)

    def test_report_discloses_the_uncheckable_count(self) -> None:
        profiles = {"923609016": _profile(), "111111111": _profile("111111111")}
        with _TempProfiles(profiles):
            ev = E.evaluate(list(profiles), seed_payload={"923609016": _payload()})
        self.assertIn("not checkable", ev.report())
        self.assertEqual(ev.to_dict()["identity_uncheckable"], 1)


class TestHarnessMechanics(unittest.TestCase):
    def test_reproducible_with_the_same_seed(self) -> None:
        import random

        a = E._random_numbers(10, rng=random.Random(7))
        b = E._random_numbers(10, rng=random.Random(7))
        self.assertEqual(a, b)

    def test_different_seeds_generally_differ(self) -> None:
        import random

        a = E._random_numbers(15, rng=random.Random(1))
        b = E._random_numbers(15, rng=random.Random(2))
        self.assertNotEqual(a, b)

    def test_hashable_report(self) -> None:
        ev = E.Evaluation(n=2, slots_total=4, slots_filled=3)
        payload = json.dumps(ev.to_dict())
        self.assertIn("coverage", payload)


if __name__ == "__main__":
    unittest.main()
