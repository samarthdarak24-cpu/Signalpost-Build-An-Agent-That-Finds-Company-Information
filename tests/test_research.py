"""Tests for the five-domain research engine.

The engine is the layer the spec puts directly under the validator, so it is
tested for the two things that decide whether it helps or hurts: does the
planner refuse the web for a sole trader, and does one broken domain ever cost
the company its other four? Both are asserted here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from activity import activity_facts, activity_urls, extract_activity  # noqa: E402
from coverage import coverage_summary, domain_coverage  # noqa: E402
from jobs import careers_urls, extract_jobs, job_facts  # noqa: E402
from normalize import Page  # noqa: E402
from profiles import Fact, Profile, SkippedField  # noqa: E402
from research_fields import (  # noqa: E402
    DOMAINS,
    SOURCE_PRIORITY,
    SOURCE_REGISTRY,
    SOURCE_REGISTRY_SITE,
    catalog_matrix,
    domain_fields,
    field_domain,
    primary_source_for,
)
from research_plan import DEFERRED, READY, SKIP, DomainPlan, ResearchPlan, plan  # noqa: E402
from research_runner import (  # noqa: E402
    STATUS_DEFERRED,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    run_domains,
    summarize,
)


def _fact(name: str, value="x", *, layer: str = "registry") -> Fact:
    return Fact(
        field=name, value=value, source_url="u", retrieved_at="t", evidence="e",
        match_proof="registry", confidence=0.95, layer=layer,
    )


class TestResearchFields(unittest.TestCase):
    def test_five_domains_in_reference_order(self) -> None:
        self.assertEqual(
            DOMAINS,
            ("company", "financials", "people_locations", "website", "hiring_activity"),
        )

    def test_every_field_maps_to_a_domain(self) -> None:
        for domain in DOMAINS:
            self.assertTrue(domain_fields(domain), f"{domain} has no fields")

    def test_field_domain_lookup(self) -> None:
        self.assertEqual(field_domain("ceo"), "people_locations")
        self.assertEqual(field_domain("revenue_latest"), "financials")
        self.assertEqual(field_domain("website"), "website")
        self.assertEqual(field_domain("open_roles"), "hiring_activity")
        self.assertEqual(field_domain("legal_name"), "company")
        self.assertEqual(field_domain("not_a_field"), "")

    def test_registry_is_the_primary_source_for_identity(self) -> None:
        self.assertEqual(primary_source_for("legal_name"), SOURCE_REGISTRY)
        # website's first source is the register's own URL, search is only fallback
        self.assertEqual(primary_source_for("website"), SOURCE_REGISTRY_SITE)

    def test_source_priority_matrix_is_complete(self) -> None:
        information = [row[0] for row in SOURCE_PRIORITY]
        for required in ("Identity", "Roles", "Locations", "Financials", "Website",
                         "Jobs", "Activity", "LEI", "Identifiers"):
            self.assertIn(required, information)

    def test_catalog_matrix_has_all_domains(self) -> None:
        matrix = catalog_matrix()
        self.assertEqual(set(matrix), set(DOMAINS))
        for rows in matrix.values():
            for row in rows:
                self.assertIn("primary", row)
                self.assertIn("fallback", row)


class TestResearchPlan(unittest.TestCase):
    ENTITY = {"organisasjonsnummer": "986587098", "navn": "2CARE BPA AS",
              "organisasjonsform": {"kode": "AS"}}

    def test_online_plan_attempts_all_five(self) -> None:
        p = plan(self.ENTITY, offline=False, web_allowed=True, search_available=True)
        self.assertEqual(
            sorted(p.ready_domains()),
            sorted(DOMAINS),
        )

    def test_offline_plan_touches_no_network(self) -> None:
        p = plan(self.ENTITY, offline=True)
        self.assertEqual(p.ready_domains(), [])          # nothing is attempted
        self.assertEqual(p.domain("company").status, SKIP)  # mirror only, explicitly
        for key in ("financials", "people_locations", "website", "hiring_activity"):
            self.assertEqual(p.domain(key).status, DEFERRED)

    def test_sole_trader_skips_the_web_entirely(self) -> None:
        p = plan(self.ENTITY, offline=False, web_allowed=False)
        self.assertEqual(p.domain("website").status, SKIP)
        self.assertIn("sole trader", p.domain("website").reason)
        # hiring is read off a verified site, so it is deferred, never attempted
        self.assertEqual(p.domain("hiring_activity").status, DEFERRED)

    def test_no_web_defers_website_and_hiring(self) -> None:
        p = plan(self.ENTITY, offline=False, fetch_web=False, web_allowed=True)
        self.assertEqual(p.domain("website").status, DEFERRED)
        self.assertEqual(p.domain("hiring_activity").status, DEFERRED)

    def test_financials_can_be_disabled(self) -> None:
        p = plan(self.ENTITY, offline=False, fetch_financials=False, web_allowed=True)
        self.assertEqual(p.domain("financials").status, DEFERRED)

    def test_attempted_fields_only_from_ready_domains(self) -> None:
        p = plan(self.ENTITY, offline=False, web_allowed=True)
        fields = p.attempted_fields()
        self.assertIn("legal_name", fields)
        self.assertIn("revenue_latest", fields)
        self.assertIn("open_roles", fields)

    def test_plan_round_trips_to_dict(self) -> None:
        p = plan(self.ENTITY, offline=False, web_allowed=True)
        data = p.to_dict()
        self.assertEqual(data["orgnr"], "986587098")
        self.assertEqual(len(data["domains"]), 5)
        self.assertIn("ready_domains", data)


class TestResearchRunner(unittest.TestCase):
    def _all_ready_plan(self) -> ResearchPlan:
        return ResearchPlan(
            orgnr="986587098",
            domains=[
                DomainPlan("company", ("legal_name", "legal_form"), SOURCE_REGISTRY),
                DomainPlan("financials", ("revenue_latest",), "regnskapsregisteret"),
                DomainPlan("people_locations", ("ceo",), "registry_roles"),
                DomainPlan("website", ("website",), SOURCE_REGISTRY_SITE),
                DomainPlan("hiring_activity", ("open_roles",), "careers_page"),
            ],
        )

    def test_ready_domain_collects_facts(self) -> None:
        plan_obj = self._all_ready_plan()
        retrievers = {
            "company": lambda: [_fact("legal_name", "X"), _fact("legal_form", "AS")],
            "financials": lambda: [],
            "people_locations": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            "website": lambda: [_fact("website", "https://x.no", layer="web_rules")],
            "hiring_activity": lambda: [_fact("open_roles", 3, layer="web_rules")],
        }
        outcome = run_domains(plan_obj, retrievers)
        self.assertEqual(outcome.domain("company").status, STATUS_OK)
        self.assertEqual(outcome.domain("company").fact_count, 2)
        self.assertEqual(outcome.domain("financials").status, STATUS_EMPTY)
        self.assertEqual(outcome.domain("people_locations").status, STATUS_FAILED)
        self.assertIn("boom", outcome.domain("people_locations").error)
        # a failed domain does not stop the others
        self.assertEqual(outcome.domain("website").status, STATUS_OK)
        self.assertEqual(outcome.domain("hiring_activity").status, STATUS_OK)

    def test_deferred_and_skipped_domains_are_not_attempted(self) -> None:
        plan_obj = ResearchPlan(orgnr="1", domains=[
            DomainPlan("company", ("legal_name",), SOURCE_REGISTRY, status=SKIP, reason="offline"),
            DomainPlan("website", ("website",), SOURCE_REGISTRY_SITE, status=DEFERRED, reason="no web"),
        ])
        called = []

        def boom():
            called.append(1)
            return []

        outcome = run_domains(plan_obj, {"company": boom, "website": boom})
        self.assertEqual(outcome.domain("company").status, STATUS_SKIPPED)
        self.assertEqual(outcome.domain("website").status, STATUS_DEFERRED)
        self.assertEqual(called, [])  # neither retriever ran

    def test_missing_retriever_is_reported_not_ignored(self) -> None:
        plan_obj = ResearchPlan(orgnr="1", domains=[
            DomainPlan("company", ("legal_name",), SOURCE_REGISTRY, status=READY),
        ])
        outcome = run_domains(plan_obj, {})
        self.assertEqual(outcome.domain("company").status, STATUS_SKIPPED)
        self.assertIn("no retriever", outcome.domain("company").detail)

    def test_coverage_matrix_is_field_and_source_aware(self) -> None:
        plan_obj = ResearchPlan(orgnr="986587098", domains=[
            DomainPlan("company", ("legal_name", "legal_form"), SOURCE_REGISTRY),
        ])
        outcome = run_domains(plan_obj, {
            "company": lambda: [_fact("legal_name", "X", layer="registry")],
        })
        cov = outcome.coverage()["company"]
        self.assertIn("legal_name", cov["present"])
        self.assertIn("legal_form", cov["missing"])
        self.assertEqual(cov["sources"]["legal_name"], "registry")

    def test_summarize_is_readable(self) -> None:
        plan_obj = self._all_ready_plan()
        outcome = run_domains(plan_obj, {"company": lambda: [_fact("legal_name", "X")]})
        text = summarize(outcome)
        self.assertIn("986587098", text)
        self.assertIn("facts", text)


class TestCoverageMatrix(unittest.TestCase):
    """The coverage module must be field-aware and source-aware, not a score."""

    def _profile(self) -> Profile:
        p = Profile(org_number="986587098", legal_name="2CARE BPA AS", run_date="2026-10-08")
        p.facts = [
            _fact("legal_name", "2CARE BPA AS", layer="registry"),
            _fact("revenue_latest", 36177737, layer="registry"),
            _fact("ceo", "Anders Opedal", layer="registry"),
            _fact("website", "https://x.no", layer="web_rules"),
            _fact("open_roles", 3, layer="web_rules"),
        ]
        p.empty_fields = [SkippedField("lei", "not in GLEIF")]
        return p

    def test_matrix_covers_all_five_domains(self) -> None:
        matrix = domain_coverage(self._profile())
        self.assertEqual(
            set(matrix),
            {"company", "financials", "people_locations", "website", "hiring_activity"},
        )

    def test_matrix_is_field_aware(self) -> None:
        matrix = domain_coverage(self._profile())
        self.assertIn("legal_name", matrix["company"]["present"])
        self.assertIn("legal_form", matrix["company"]["missing"])
        self.assertIn("revenue_latest", matrix["financials"]["present"])
        self.assertIn("open_roles", matrix["hiring_activity"]["present"])

    def test_matrix_is_source_aware(self) -> None:
        matrix = domain_coverage(self._profile())
        self.assertEqual(matrix["company"]["sources"]["legal_name"], "registry")
        self.assertEqual(matrix["website"]["sources"]["website"], "web_rules")
        self.assertEqual(matrix["hiring_activity"]["sources"]["open_roles"], "web_rules")

    def test_matrix_accepts_a_bare_fact_list(self) -> None:
        matrix = domain_coverage([_fact("legal_name", "X")])
        self.assertIn("legal_name", matrix["company"]["present"])

    def test_summary_counts_evidence_and_gaps(self) -> None:
        summary = coverage_summary(self._profile())
        self.assertEqual(summary["facts_found"], 5)
        self.assertEqual(summary["evidence_backed"], 5)   # all carry url + quote
        self.assertEqual(summary["unavailable"], 1)       # the skipped lei
        self.assertEqual(summary["ambiguous"], 0)

    def test_summary_flags_name_only_as_ambiguous(self) -> None:
        p = Profile(org_number="1", legal_name="X", run_date="d")
        p.facts = [Fact(field="email", value="a@b.no", source_url="u", retrieved_at="t",
                        evidence="e", match_proof="name_only", confidence=0.4)]
        summary = coverage_summary(p)
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(summary["evidence_backed"], 1)


class TestJobs(unittest.TestCase):
    def _page(self, url: str, markup: str) -> Page:
        return Page(url=url, markup=markup, retrieved_at="2026-10-08T00:00:00Z")

    def test_careers_urls_expand_a_root(self) -> None:
        urls = careers_urls("https://x.no")
        self.assertIn("https://x.no/karriere", urls)
        self.assertIn("https://x.no/jobs", urls)

    def test_jsonld_jobposting_is_read(self) -> None:
        markup = (
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"Seniorutvikler",'
            '"datePosted":"2026-09-30",'
            '"jobLocation":{"address":{"addressLocality":"Oslo"}},'
            '"url":"https://x.no/jobb/1"}</script>'
        )
        postings = extract_jobs([self._page("https://x.no/karriere", markup)])
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0].title, "Seniorutvikler")
        self.assertEqual(postings[0].location, "Oslo")
        self.assertEqual(postings[0].posted, "2026-09-30")

    def test_anchor_fallback_finds_job_links(self) -> None:
        markup = '<a href="/jobb/okonom">Vi søker økonom</a>'
        postings = extract_jobs([self._page("https://x.no/karriere", markup)])
        self.assertEqual(len(postings), 1)
        self.assertIn("økonom", postings[0].title)

    def test_job_facts_publish_count_and_latest(self) -> None:
        postings = extract_jobs([self._page(
            "https://x.no/karriere",
            '<script type="application/ld+json">['
            '{"@type":"JobPosting","title":"A","datePosted":"2026-01-01"},'
            '{"@type":"JobPosting","title":"B","datePosted":"2026-05-01"}]</script>',
        )])
        facts = job_facts(postings, number="986587098", retrieved_at="t",
                          source_url="https://x.no/karriere",
                          careers_url="https://x.no/karriere")
        by_field = {f.field: f.value for f in facts}
        self.assertEqual(by_field["careers_page"], "https://x.no/karriere")
        self.assertEqual(by_field["open_roles"], 2)
        self.assertEqual(by_field["latest_job_title"], "B")
        self.assertEqual(by_field["latest_job_date"], "2026-05-01")

    def test_jobs_never_fabricate_a_zero(self) -> None:
        # No postings and no careers page -> no facts at all, not a zero count.
        self.assertEqual(job_facts([], number="1", retrieved_at="t", source_url="u"), [])


class TestActivity(unittest.TestCase):
    def _page(self, url: str, markup: str) -> Page:
        return Page(url=url, markup=markup, retrieved_at="2026-10-08T00:00:00Z")

    def test_activity_urls_expand_a_root(self) -> None:
        urls = activity_urls("https://x.no")
        self.assertIn("https://x.no/nyheter", urls)
        self.assertIn("https://x.no/press", urls)

    def test_jsonld_news_article_is_read(self) -> None:
        markup = (
            '<script type="application/ld+json">'
            '{"@type":"NewsArticle","headline":"Ny fabrikk i Bergen",'
            '"datePublished":"2026-08-15T09:00:00Z"}</script>'
        )
        items = extract_activity([self._page("https://x.no/nyheter", markup)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Ny fabrikk i Bergen")
        self.assertEqual(items[0].date, "2026-08-15")

    def test_time_element_fallback(self) -> None:
        markup = '<time datetime="2026-07-01">Vi åpner ny avdeling</time>'
        items = extract_activity([self._page("https://x.no/aktuelt", markup)])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].date, "2026-07-01")

    def test_activity_facts_publish_count_and_latest(self) -> None:
        items = extract_activity([self._page(
            "https://x.no/nyheter",
            '<script type="application/ld+json">['
            '{"@type":"NewsArticle","headline":"Old","datePublished":"2026-01-01"},'
            '{"@type":"NewsArticle","headline":"New","datePublished":"2026-09-01"}]</script>',
        )])
        facts = activity_facts(items, number="986587098", retrieved_at="t",
                               source_url="https://x.no/nyheter")
        by_field = {f.field: f.value for f in facts}
        self.assertEqual(by_field["activity_count"], 2)
        self.assertEqual(by_field["latest_update_title"], "New")
        self.assertEqual(by_field["latest_update_date"], "2026-09-01")

    def test_activity_never_fabricates(self) -> None:
        self.assertEqual(activity_facts([], number="1", retrieved_at="t", source_url="u"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
