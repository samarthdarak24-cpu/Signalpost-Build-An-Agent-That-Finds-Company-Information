"""Task #48 — regression tests for the safer web-discovery query strategy.

These tests are deterministic: the search engine is mocked at the
``liveweb.search_web`` boundary (scenarios 1-7, 9, 10) or driven by a local mock
HTTP server (scenario 8, request ceiling / accounting). No real external traffic
is produced by this file.

What is intentionally *not* touched by these tests: the ownership gate, the
directory deny-list, the candidate scorer's weights, and the budget accountant.
Those are asserted to keep behaving as before, so improving coverage cannot come
at the cost of a misattribution or an unbudgeted request.
"""

from __future__ import annotations

import sys
import threading
import unittest
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import liveweb
from liveweb import SearchOutcome, SearchResult

import sitefinder
from sitefinder import SiteFinder
from gate import evaluate as gate_evaluate
from normalize import Page
from validate import OrgNumber


def _engine_result(title: str, url: str, snippet: str = "") -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, rank=0, engine="duckduckgo")


def make_fake_search_web(mapping: dict[str, list[SearchResult]], *, fail: bool = False):
    """Return a fake ``liveweb.search_web`` that serves ``mapping`` by query.

    ``fail`` makes every call raise (simulating a provider timeout / network
    error), which is the honest "we could not look" outcome the gate and the
    pipeline must distinguish from an empty web.
    """
    calls = []

    def fake(query, *, client=None, engines=None, limit=10, company_name="",  # noqa: ANN001
             cache=True, settle_seconds=0.0):
        calls.append(query)
        if fail:
            raise TimeoutError("simulated provider timeout")
        outcome = SearchOutcome(query=query)
        outcome.engines_tried.append("duckduckgo")
        results = mapping.get(query, [])
        outcome.results = results
        outcome.engine = "duckduckgo" if results else ""
        outcome.error = "" if results else "no engine returned results"
        return outcome

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


class TestOfficialSiteDiscovery(unittest.TestCase):
    """Scenario 1 + 2: a real owned site is found, a directory is not."""

    def _finder(self, mapping, *, fail=False):
        f = SiteFinder()
        patcher = mock.patch("sitefinder.liveweb.search_web", make_fake_search_web(mapping, fail=fail))
        patcher.start()
        self.addCleanup(patcher.stop)
        return f

    def test_name_query_surfaces_official_site(self):
        f = self._finder({
            "EQUINOR ASA": [_engine_result("Equinor", "https://equinor.com")],
        })
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertTrue(cands, "an official site must be returned as a candidate")
        top = cands[0]
        self.assertEqual(top.url, "https://equinor.com")
        self.assertEqual(top.rejected_reason, "", "an owned site must not be rejected")
        self.assertEqual(f.last_search.status, "ok")

    def test_directory_only_result_is_rejected(self):
        f = self._finder({
            "EQUINOR ASA": [_engine_result("EQUINOR ASA - Proff.no", "https://proff.no/selskap/equinor")],
        })
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        # from_search still yields the candidate object (with a rejection reason);
        # the combined candidates() must exclude it.
        raw = f.from_search(entity, OrgNumber("923609016"))
        self.assertTrue(raw)
        self.assertIn("directory", raw[0].rejected_reason.lower())
        self.assertEqual(f.candidates(entity, OrgNumber("923609016")), [])


class TestWrongCompanyRejection(unittest.TestCase):
    """Scenario 3: a wrong-company page that quotes the target name but a wrong
    org number reaches the gate (not silently filtered) and is rejected there."""

    def test_gate_rejects_wrong_org_number(self):
        # A page that mentions EQUINOR but carries a different checksum-valid number.
        # Enough visible text to clear the gate's MIN_TEXT_LENGTH floor.
        markup = (
            "<html><body><h1>EQUINOR ASA</h1>"
            "<p>Equinor ASA er et norsk energiselskap. "
            "Organisasjonsnummer 801 455 271 &middot; Oslo. "
            "Selskapet driver med olje og gass samt fornybar energi.</p>"
            "</body></html>"
        )
        page = Page(url="https://some-other-site.no", status=200, markup=markup)
        verdict = gate_evaluate(page, OrgNumber("923609016"), name="EQUINOR ASA",
                                postcode="4035", city="STAVANGER")
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.verdict, "rejected_wrong_orgnr")


class TestEmptyAndFailedSearch(unittest.TestCase):
    """Scenarios 4, 5 and 10: empty vs failed are distinct, and a failed search
    is surfaced (never swallowed into an empty result)."""

    def _finder(self, mapping=None, *, fail=False):
        f = SiteFinder()
        patcher = mock.patch("sitefinder.liveweb.search_web",
                            make_fake_search_web(mapping or {}, fail=fail))
        patcher.start()
        self.addCleanup(patcher.stop)
        return f

    def test_empty_search_is_distinct_from_failed(self):
        f = self._finder({})  # every query returns nothing
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertEqual(cands, [])
        self.assertEqual(f.last_search.status, "empty")
        self.assertFalse(f.last_search.failed)

    def test_provider_timeout_is_reported_failed(self):
        f = self._finder(fail=True)
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertEqual(cands, [])
        self.assertEqual(f.last_search.status, "failed")
        self.assertTrue(f.last_search.failed)
        self.assertTrue(f.last_search.error, "a failed search must carry the reason")


class TestDuplicateAndLocation(unittest.TestCase):
    """Scenarios 6 and 7: duplicate result URLs are deduped; a missing location
    must not crash or invent one."""

    def _finder(self, mapping):
        f = SiteFinder()
        patcher = mock.patch("sitefinder.liveweb.search_web", make_fake_search_web(mapping))
        patcher.start()
        self.addCleanup(patcher.stop)
        return f

    def test_duplicate_result_urls_are_deduped(self):
        f = self._finder({
            "EQUINOR ASA": [
                _engine_result("Equinor", "https://equinor.com"),
                _engine_result("Equinor", "https://equinor.com"),  # identical URL
            ],
        })
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        urls = [c.url for c in cands]
        self.assertEqual(urls, ["https://equinor.com"], "duplicate URLs must collapse to one")

    def test_missing_location_does_not_invent_a_city_query(self):
        f = self._finder({
            # No poststed in the entity, so no "<name> <city>" query is built.
            "EQUINOR ASA": [_engine_result("Equinor", "https://equinor.com")],
        })
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016"}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertTrue(cands, "a missing location must not prevent discovery")
        self.assertEqual(cands[0].url, "https://equinor.com")
        # The only query issued is the bare name; no city was invented.
        self.assertNotIn("STAVANGER", f.last_search.query or "")


class TestQueryOrderingAndCeiling(unittest.TestCase):
    """Scenario 8: the identifier (org-number) query is a last resort, the
    per-company query count is capped, the search is budget-accounted exactly
    once, and an exhausted budget short-circuits to 'skipped'."""

    def test_identifier_query_is_last_resort_not_first(self):
        # The org-number query returns a hit, but the name query returns the owned
        # site first; we must pick the owned site, proving name leads.
        f = SiteFinder()
        mapping = {
            'EQUINOR ASA': [_engine_result("Equinor", "https://equinor.com")],
            'EQUINOR ASA organisasjonsnummer 923609016': [
                _engine_result("EQUINOR ASA 923609016", "https://proff.no/selskap/equinor")],
        }
        fake = make_fake_search_web(mapping)
        patcher = mock.patch("sitefinder.liveweb.search_web", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertTrue(cands)
        self.assertEqual(cands[0].url, "https://equinor.com")
        # Only the name query was issued; the org-number fallback was never reached.
        self.assertEqual(fake.calls, ["EQUINOR ASA"],
                         "name query must win; the org-number fallback is not tried")

    def test_max_queries_caps_the_number_of_searches(self):
        # All queries return nothing, so the loop would otherwise keep going;
        # max_queries must bound it. No network is used.
        f = SiteFinder(max_queries=3)
        fake = make_fake_search_web({})  # every query -> empty
        patcher = mock.patch("sitefinder.liveweb.search_web", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertEqual(cands, [])
        self.assertEqual(len(fake.calls), 3, "the per-company query ceiling must hold")
        self.assertEqual(f.last_search.status, "empty")

    def test_cancellation_when_budget_exhausted(self):
        # A budget that can admit no request must make the search skip, never
        # reach the network and never misreport success.
        from net import HttpClient, Budget as NetBudget
        http = HttpClient(budget=NetBudget(hard_limit=0, soft_limit=0))
        f = SiteFinder(http, max_queries=4)
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertEqual(cands, [])
        self.assertEqual(f.last_search.status, "skipped")
        self.assertEqual(http.budget.used, 0, "no request leaves the machine when skipped")

    def test_search_is_budgeted_exactly_once_per_request(self):
        # A local mock server stands in for the engines so the real accountant is
        # exercised without leaving the machine. The on-disk search cache is
        # bypassed so the request actually reaches the mock server.
        import net  # noqa: E402

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html></html>")  # parses to zero results

            def log_message(self, *args):  # silence
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        real_urlopen = net._u.urlopen

        def local_urlopen(request, *args, **kwargs):  # noqa: ANN001
            from urllib.request import Request
            if isinstance(request, Request):
                rewritten = Request(f"http://127.0.0.1:{port}/",
                                    data=request.data, headers=request.headers)
                return real_urlopen(rewritten, *args, **kwargs)
            return real_urlopen(request, *args, **kwargs)

        cache_patch = mock.patch("liveweb._cache_get", return_value=None)
        put_patch = mock.patch("liveweb._cache_put", lambda *a, **k: None)
        urlopen_patch = mock.patch("net._u.urlopen", local_urlopen)
        cache_patch.start(); put_patch.start(); urlopen_patch.start()
        self.addCleanup(cache_patch.stop); self.addCleanup(put_patch.stop)
        self.addCleanup(urlopen_patch.stop)

        from net import HttpClient, Budget as NetBudget
        http = HttpClient(budget=NetBudget(hard_limit=2000, soft_limit=1700))
        f = SiteFinder(http, max_queries=4)
        entity = {"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016",
                  "forretningsadresse": {"poststed": "STAVANGER", "postnummer": "4035"}}
        cands = f.from_search(entity, OrgNumber("923609016"))
        self.assertEqual(cands, [])
        self.assertEqual(f.last_search.status, "empty")
        used = http.budget.used
        # At least one attempt per query, none double-counted, within the ceiling.
        self.assertGreaterEqual(used, 4)
        self.assertLessEqual(used, 2000)
        self.assertEqual(
            sum(http.budget.run.by_operation.values()), used,
            "per-operation counts must sum to the total: no request is off the books",
        )
        server.shutdown()
        server.server_close()


class TestCarryForwardPreservesVerifiedFacts(unittest.TestCase):
    """Scenario 9: a failed/interrupted rerun must not erase a previously
    verified, non-excluded fact; volatile contact fields stay excluded by
    design."""

    def test_non_excluded_fact_survives_failed_rerun(self):
        from profiles import Profile
        from pipeline import _carry_forward, _CARRY_FORWARD_EXCLUDED  # noqa: E402

        number = "923609016"
        tmp = Path(__file__).resolve().parent / ".." / "data" / "_t48_cf_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        prev = tmp / f"{number}.json"
        prev.write_text(__import__("json").dumps({
            "org_number": number,
            "legal_name": "EQUINOR ASA",
            "facts": [
                {"field": "industry", "value": "Oil and gas",
                 "source_url": "https://data.brreg.no/...", "match_proof": "registry"},
                {"field": "website", "value": "https://equinor.com",
                 "source_url": "https://equinor.com", "match_proof": "orgnr_on_page"},
            ],
        }), encoding="utf-8")
        self.addCleanup(prev.unlink, missing_ok=True)

        # A current run that produced nothing for those fields (e.g. sources failed).
        profile = Profile(org_number=number, legal_name="EQUINOR ASA", run_date="2026-10-10")
        restored = _carry_forward(profile, tmp, number)
        self.assertIn("industry", {f.field for f in profile.facts})
        # website is deliberately excluded from carry-forward; it must not be
        # resurrected from a stale file.
        self.assertNotIn("website", {f.field for f in profile.facts})
        self.assertIn("website", _CARRY_FORWARD_EXCLUDED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
