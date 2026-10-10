"""Tests for the retrieval-depth modules added in the P0 pass.

entity_match, budget, cache, synthesis, ask and the snapshot/idempotency upgrade.
Each is tested for the property that makes it safe: entity_match never lets a
name-only match publish, budget refuses to overspend, cache misses when the
question changes, synthesis never invents a line, ask never answers from nothing,
and a duplicate refresh produces no change.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from ask import ask  # noqa: E402
from budget import BudgetController, BudgetExceeded  # noqa: E402
from cache import ResearchCache, cache_key  # noqa: E402
from delta import (  # noqa: E402
    SnapshotStore,
    WebSnapshot,
    detect_web_changes,
    refresh_noop,
    snapshot_id_for,
)
from entity_match import (  # noqa: E402
    MatchSignals,
    best,
    resolve,
    resolve_page,
)
from normalize import Page  # noqa: E402
from profiles import Fact, Profile  # noqa: E402
from synthesis import render_text, synthesis  # noqa: E402
from validate import validate  # noqa: E402


def _fact(name: str, value, *, layer: str = "registry", proof: str = "registry") -> Fact:
    return Fact(field=name, value=value, source_url="https://src", retrieved_at="t",
                evidence=f"{name} evidence", match_proof=proof, confidence=0.95, layer=layer)


def _profile(**facts) -> Profile:
    p = Profile(org_number="986587098", legal_name="2CARE BPA AS", run_date="2026-10-08")
    for name, value in facts.items():
        p.facts.append(_fact(name, value))
    return p


class TestEntityMatch(unittest.TestCase):
    def test_orgnr_alone_publishes(self) -> None:
        score = resolve(MatchSignals(orgnr=True))
        self.assertTrue(score.publishable)
        self.assertGreaterEqual(score.score, 1.0)

    def test_name_alone_is_rejected(self) -> None:
        score = resolve(MatchSignals(folded_name=True, exact_name=True))
        self.assertEqual(score.verdict, "reject")

    def test_name_plus_place_publishes(self) -> None:
        score = resolve(MatchSignals(exact_name=True, postcode=True, city=True))
        self.assertEqual(score.verdict, "publish")

    def test_name_plus_postcode_only_is_review(self) -> None:
        score = resolve(MatchSignals(exact_name=True, postcode=True))
        self.assertEqual(score.verdict, "review")

    def test_breakdown_lists_the_signals_that_fired(self) -> None:
        score = resolve(MatchSignals(orgnr=True, city=True))
        self.assertIn("orgnr", score.breakdown)
        self.assertIn("city", score.signals)

    def test_resolve_page_reads_a_real_page(self) -> None:
        org = validate("986587098")
        page = Page(url="https://x.no/", markup=(
            "<html><body><h1>2Care BPA</h1><p>Org.nr 986 587 098</p>"
            "<p>Vi tilbyr tjenester i Oslo.</p></body></html>"))
        score = resolve_page(page, org, name="2CARE BPA AS")
        self.assertTrue(score.publishable)
        self.assertIn("orgnr", score.signals)

    def test_best_picks_the_strongest(self) -> None:
        weak = resolve(MatchSignals(exact_name=True))
        strong = resolve(MatchSignals(orgnr=True))
        self.assertIs(best([weak, strong]), strong)


class TestBudget(unittest.TestCase):
    def test_allow_and_spend(self) -> None:
        b = BudgetController(max_requests=3)
        self.assertTrue(b.allow("registry"))
        b.spend("registry")
        self.assertEqual(b.requests_used, 1)

    def test_search_cap_is_enforced(self) -> None:
        b = BudgetController(max_search=1)
        self.assertTrue(b.allow("brave_search"))
        b.spend("brave_search")
        self.assertFalse(b.allow("brave_search"))
        self.assertEqual(b.refusals.get("brave_search"), 1)

    def test_llm_cap_is_enforced(self) -> None:
        b = BudgetController(max_llm=1)
        b.spend("llm")
        self.assertFalse(b.allow("llm"))

    def test_winding_down_near_the_request_cap(self) -> None:
        b = BudgetController(max_requests=10, reserve_fraction=0.2)
        for _ in range(9):
            b.spend("registry")
        self.assertTrue(b.winding_down)
        self.assertFalse(b.allow("registry"))

    def test_enforce_raises_when_refused(self) -> None:
        b = BudgetController(max_search=0)
        with self.assertRaises(BudgetExceeded):
            b.spend("brave_search", enforce=True)

    def test_to_dict_reports_everything(self) -> None:
        b = BudgetController()
        b.spend("registry", cost_usd=0.001)
        data = b.to_dict()
        for key in ("requests_used", "search_used", "llm_used", "cost_usd", "winding_down"):
            self.assertIn(key, data)


class TestResearchCache(unittest.TestCase):
    def test_remember_computes_once_then_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp) / "c.sqlite")
            calls = {"n": 0}

            def producer() -> dict:
                calls["n"] += 1
                return {"value": 42}

            first = cache.remember("field", orgnr="1", url="u", field="revenue", producer=producer)
            second = cache.remember("field", orgnr="1", url="u", field="revenue", producer=producer)
            self.assertEqual(first, second)
            self.assertEqual(calls["n"], 1)   # producer ran only once
            cache.close()

    def test_different_field_is_a_different_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp) / "c.sqlite")
            cache.remember("field", url="u", field="email", producer=lambda: "a@b.no")
            self.assertIsNone(cache.get(cache_key("field", url="u", field="phone")))
            cache.close()

    def test_content_hash_change_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp) / "c.sqlite")
            cache.remember("html", url="u", content_hash="h1", producer=lambda: "old")
            self.assertIsNone(cache.get(cache_key("html", url="u", content_hash="h2")))
            cache.close()

    def test_model_change_misses_llm_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp) / "c.sqlite")
            cache.remember("llm", url="u", field="revenue", model="m1", producer=lambda: "1")
            self.assertIsNone(cache.get(cache_key("llm", url="u", field="revenue", model="m2")))
            cache.close()

    def test_stats_track_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp) / "c.sqlite")
            cache.remember("registry", orgnr="1", producer=lambda: {"x": 1})
            self.assertEqual(cache.stats()["entries"], 1)
            cache.close()


class TestSynthesis(unittest.TestCase):
    def test_views_are_built_from_facts(self) -> None:
        p = _profile(ceo="Anders Opedal", revenue_latest=36177737,
                     open_roles=3, latest_update_title="Ny fabrikk")
        synth = synthesis(p)
        self.assertEqual(synth["views"]["who_runs_it"]["lines"][0]["value"], "Anders Opedal")
        self.assertEqual(synth["views"]["latest_financials"]["lines"][0]["value"], 36177737)
        self.assertEqual(synth["views"]["recent_activity"]["lines"][0]["value"], "Ny fabrikk")

    def test_empty_view_is_marked_empty_not_invented(self) -> None:
        synth = synthesis(_profile(ceo="X"))
        self.assertTrue(synth["views"]["recent_activity"]["empty"])
        self.assertEqual(synth["views"]["recent_activity"]["lines"], [])

    def test_missing_panel_lists_gaps_with_a_reason(self) -> None:
        synth = synthesis(_profile(ceo="X"))
        missing = synth["missing"]
        self.assertTrue(any(m["field"] == "revenue_latest" for m in missing))
        self.assertTrue(all(m["reason"] for m in missing))

    def test_render_text_names_the_sections(self) -> None:
        text = render_text(synthesis(_profile(ceo="X")))
        self.assertIn("WHO RUNS IT?", text)
        self.assertIn("NOT AVAILABLE", text)


class TestAsk(unittest.TestCase):
    def test_leadership_question_routes_and_answers(self) -> None:
        answer = ask(_profile(ceo="Anders Opedal"), "Who runs this company?")
        self.assertEqual(answer.intent, "leadership")
        self.assertTrue(answer.answered)
        self.assertIn("Anders Opedal", answer.text)

    def test_hiring_question_routes(self) -> None:
        answer = ask(_profile(open_roles=2, latest_job_title="Utvikler"), "Is it hiring?")
        self.assertEqual(answer.intent, "hiring")
        self.assertTrue(answer.answered)

    def test_unanswered_question_is_honest(self) -> None:
        answer = ask(_profile(ceo="X"), "What are the latest financial results?")
        self.assertFalse(answer.answered)
        self.assertIn("not available", answer.text.casefold())

    def test_answer_only_cites_verified_facts(self) -> None:
        answer = ask(_profile(ceo="X", board_chair="Y"), "Who runs it?")
        self.assertTrue(answer.evidence)
        for line in answer.evidence:
            self.assertTrue(line["source_url"])
            self.assertTrue(line["evidence"])


class TestSnapshotIdempotency(unittest.TestCase):
    def test_snapshot_id_is_content_addressed(self) -> None:
        self.assertEqual(snapshot_id_for("u", "h"), snapshot_id_for("u", "h"))
        self.assertNotEqual(snapshot_id_for("u", "h1"), snapshot_id_for("u", "h2"))

    def test_refresh_noop_on_same_hash(self) -> None:
        previous = WebSnapshot(url="u", content_hash="h1")
        self.assertTrue(refresh_noop(previous, "h1"))
        self.assertFalse(refresh_noop(previous, "h2"))
        self.assertFalse(refresh_noop(None, "h1"))

    def test_snapshot_store_round_trips_snapshot_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "s.sqlite")
            snap = WebSnapshot(url="https://x.no/", content_hash="abc", etag="W/1")
            store.put(snap)
            loaded = store.get("https://x.no/")
            self.assertEqual(loaded.snapshot_id, snap.snapshot_id)
            self.assertEqual(loaded.content_hash, "abc")
            store.close()

    def test_duplicate_refresh_produces_no_change(self) -> None:
        profile = Profile(org_number="986587098", legal_name="X", run_date="d")
        profile.facts = [_fact("phone", "45 22 22 88")]
        previous = {"phone": "45 22 22 88"}
        self.assertEqual(
            detect_web_changes(profile, org_number="986587098",
                               previous=previous, source_url="u"), [])
        # and a second identical refresh is equally silent
        self.assertEqual(
            detect_web_changes(profile, org_number="986587098",
                               previous=previous, source_url="u"), [])


class TestAdversarialEntityMatch(unittest.TestCase):
    """Hard negatives. A wrong-company publication is qualification-threatening,
    so these assert the boundaries rather than the happy path."""

    ORG = validate("986587098")

    def _page(self, markup: str, url: str = "https://x.no/") -> Page:
        return Page(url=url, markup=markup)

    def test_decision_table(self) -> None:
        # orgnr -> strongest; name only -> never; name+city -> weak;
        # name+postcode+city -> reduced; orgnr+corroboration -> strong
        self.assertTrue(resolve(MatchSignals(orgnr=True)).publishable)
        self.assertFalse(resolve(MatchSignals(exact_name=True)).publishable)
        self.assertFalse(resolve(MatchSignals(exact_name=True, city=True)).publishable)
        self.assertTrue(resolve(MatchSignals(exact_name=True, postcode=True, city=True)).publishable)
        strong = resolve(MatchSignals(orgnr=True, exact_name=True, postcode=True, city=True))
        self.assertGreater(strong.score, resolve(MatchSignals(orgnr=True)).score)

    def test_orgnr_beats_a_rebranded_name(self) -> None:
        page = self._page("<html><body><p>NewCo AS</p><p>Org.nr 986 587 098</p>"
                          "<p>Vi jobber med tjenester for kunder i hele Norge.</p></body></html>")
        score = resolve_page(page, self.ORG, name="OldCo AS")
        self.assertTrue(score.publishable)
        self.assertIn("orgnr", score.signals)

    def test_name_collision_in_another_city_never_publishes(self) -> None:
        page = self._page("<html><body><h1>2CARE BPA AS</h1>"
                          "<p>Vi er en helt annen bedrift i Bergen med samme navn.</p></body></html>")
        self.assertFalse(resolve_page(page, self.ORG, name="2CARE BPA AS").publishable)

    def test_parent_number_on_shared_site_is_not_this_company(self) -> None:
        page = self._page("<html><body><h1>2CARE BPA</h1><p>Org.nr 923 609 016</p>"
                          "<p>Konsernets hovedkontor for alle selskaper i gruppen.</p></body></html>")
        score = resolve_page(page, self.ORG, name="2CARE BPA AS", postcode="0582", city="OSLO")
        self.assertFalse(score.publishable)

    def test_checksum_invalid_near_miss_is_not_a_number_match(self) -> None:
        page = self._page("<html><body><h1>2CARE BPA</h1><p>986 587 099</p>"
                          "<p>0582 OSLO. Vi tilbyr tjenester til kommuner.</p></body></html>")
        self.assertNotIn("986587099", page.org_numbers)      # fails modulus-11
        score = resolve_page(page, self.ORG, name="2CARE BPA AS", postcode="0582", city="OSLO")
        self.assertNotIn("orgnr", score.signals)

    def test_directory_host_earns_no_domain_signal(self) -> None:
        from entity_match import _domain_consistent  # noqa: PLC0415
        self.assertFalse(_domain_consistent("https://www.proff.no/x", "2CARE BPA AS"))
        self.assertFalse(_domain_consistent("https://www.linkedin.com/company/x", "2CARE BPA AS"))
        self.assertTrue(_domain_consistent("https://2carebpa.no/", "2CARE BPA AS"))

    def test_name_postcode_city_scores_below_a_number_match(self) -> None:
        weak = resolve_page(
            self._page("<html><body><h1>2CARE BPA</h1>"
                       "<p>Brobekkveien 80, 0582 OSLO. Vi tilbyr tjenester.</p></body></html>"),
            self.ORG, name="2CARE BPA AS", postcode="0582", city="OSLO")
        strong = resolve_page(
            self._page("<html><body><h1>2CARE BPA</h1><p>Org.nr 986 587 098</p></body></html>"),
            self.ORG, name="2CARE BPA AS", postcode="0582", city="OSLO")
        self.assertTrue(weak.publishable)
        self.assertLess(weak.score, strong.score)


if __name__ == "__main__":
    unittest.main(verbosity=2)
