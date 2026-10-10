"""Regression tests for silent search-failure swallowing.

The defect these pin down: ``SiteFinder._search`` caught every exception and
returned an empty list, so "the search never ran", "the search was refused by
the API", and "the search ran and the web genuinely has nothing" were one
indistinguishable value. A profile built on top of that cannot tell a company
with no website from a company we simply failed to look up.

The brief forbids exactly this: *never treat a missing search result as proof
that a fact is false*. These tests fail if any of the three failure modes is
allowed to collapse back into "no results".
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

import sitefinder  # noqa: E402
from net import Budget, HttpClient  # noqa: E402
from sitefinder import SiteFinder, SearchAttempt  # noqa: E402


class _FlakyHttp:
    """An HTTP client whose every call fails the way a real outage does.

    Also stubs ``get`` (the keyless engine path) so a failing Brave call cannot
    silently fall through to the real network inside a unit test.
    """

    def __init__(self, message: str = "connection reset by peer") -> None:
        self.message = message
        self.budget = Budget()

    def get_json(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(self.message)

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(self.message)


class _WorkingHttp:
    """An HTTP client that returns a plausible Brave-shaped payload."""

    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.budget = Budget()
        self.calls = 0

    def get_json(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        return {"web": {"results": self.results}}

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("the keyless path must not run when Brave answered")


def _entity(name: str = "EQUINOR ASA") -> dict:
    return {
        "navn": name,
        "forretningsadresse": {"postnummer": "4035", "poststed": "STAVANGER"},
    }


class TestSearchAttemptIsTyped(unittest.TestCase):
    """Every search returns an attempt object that says what happened."""

    def test_ok_is_false_when_transport_fails(self) -> None:
        # `keyless=False` isolates the Brave path so the assertion is about the
        # paid engine, not about whether a fallback happened to answer.
        finder = SiteFinder(_FlakyHttp(), brave_key="k", keyless=False)
        attempt = finder.search_attempt("Equinor ASA")
        self.assertIsInstance(attempt, SearchAttempt)
        self.assertFalse(attempt.ok, "a transport failure must not read as success")
        self.assertEqual(attempt.results, [])
        self.assertTrue(attempt.error, "a failure must carry a reason")
        self.assertIn("connection reset", attempt.error)

    def test_ok_is_false_when_no_key_is_configured(self) -> None:
        # No key *and* no keyless fallback: the one configuration where nothing
        # can be attempted, so the attempt must say so rather than look empty.
        finder = SiteFinder(HttpClient(budget=Budget()), brave_key="", keyless=False)
        attempt = finder.search_attempt("Equinor ASA")
        self.assertFalse(attempt.ok)
        self.assertEqual(attempt.results, [])
        self.assertEqual(attempt.status, "skipped")
        self.assertTrue(attempt.error, "a skipped search must say it was skipped")
        self.assertIn("no", attempt.error.casefold())

    def test_no_key_still_searches_when_the_keyless_fallback_is_enabled(self) -> None:
        """The Task #47 fix: a missing Brave key no longer means 'we did not look'.

        With no key the finder must route to the keyless engine chain rather than
        giving up, so the pipeline can discover sites in the keyless submission.
        """
        finder = SiteFinder(_WorkingHttp([]), brave_key="")
        self.assertTrue(finder.keyless, "the keyless fallback is on by default")
        # The stub raises for `get`, so the keyless path is attempted and fails
        # rather than the whole lookup being skipped for want of a key.
        attempt = finder.search_attempt("Equinor ASA")
        self.assertNotEqual(
            attempt.status, "skipped",
            "with a keyless engine available the lookup must be attempted, not skipped",
        )

    def test_a_genuine_empty_result_is_not_an_error(self) -> None:
        """The one case that legitimately means 'the web has nothing'."""
        finder = SiteFinder(_WorkingHttp([]), brave_key="k", keyless=False)
        attempt = finder.search_attempt("Equinor ASA")
        self.assertFalse(attempt.ok)
        self.assertEqual(attempt.results, [])
        self.assertEqual(attempt.status, "empty")
        self.assertEqual(
            attempt.error, "",
            "an engine that answered with zero results is not a failure",
        )

    def test_a_failure_is_distinguishable_from_an_empty_result(self) -> None:
        failed = SiteFinder(_FlakyHttp(), brave_key="k", keyless=False).search_attempt("X ASA")
        empty = SiteFinder(_WorkingHttp([]), brave_key="k", keyless=False).search_attempt("X ASA")
        self.assertNotEqual(
            (failed.error, failed.results),
            (empty.error, empty.results),
            "a failed lookup and an empty lookup must not be identical",
        )


class TestCandidatesSurfaceSearchFailure(unittest.TestCase):
    """The failure has to survive the trip up to the pipeline."""

    def test_candidates_still_returns_the_registry_hit(self) -> None:
        entity = _entity()
        entity["hjemmeside"] = "https://www.equinor.com"
        finder = SiteFinder(_FlakyHttp(), brave_key="k", keyless=False)
        candidates = finder.candidates(entity, _org())
        self.assertTrue(candidates, "the registry candidate is unaffected")
        self.assertEqual(candidates[0].origin, "registry")

    def test_last_attempt_records_the_failure(self) -> None:
        finder = SiteFinder(_FlakyHttp(), brave_key="k", keyless=False)
        finder.candidates(_entity(), _org())
        self.assertIsNotNone(finder.last_search, "the finder must remember its search")
        self.assertFalse(finder.last_search.ok)
        self.assertIn("connection reset", finder.last_search.error)

    def test_successful_search_records_a_clean_attempt(self) -> None:
        results = [
            {
                "url": "https://www.equinor.com/",
                "title": "Equinor ASA",
                "description": "Org.nr. 923 609 016 Energi",
            }
        ]
        finder = SiteFinder(_WorkingHttp(results), brave_key="k")
        candidates = finder.candidates(_entity(), _org())
        self.assertIsNotNone(finder.last_search)
        self.assertTrue(finder.last_search.ok)
        self.assertEqual(finder.last_search.error, "")
        self.assertTrue(candidates, "a good result still yields a candidate")
        self.assertEqual(
            finder.last_search.engine, "brave",
            "an answered Brave search must be attributed to Brave",
        )


def _org():
    from validate import validate

    org = validate("923609016")
    assert org is not None
    return org


if __name__ == "__main__":
    unittest.main()
