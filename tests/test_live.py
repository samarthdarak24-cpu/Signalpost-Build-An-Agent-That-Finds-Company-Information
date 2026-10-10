"""Tests for the live research agent and its web layer.

These cover the properties that make the live agent trustworthy rather than just
functional: that an input is classified predictably, that a challenge page is
never mistaken for "no results", that a search result is only attributed to a
company when it actually concerns that company, and that a follow-up stays on the
company established earlier in the session. Nothing here touches the network --
the engine parsers, the ranker and the session logic are all exercised on fixed
markup and fixed result sets, so the suite runs anywhere.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from liveagent import (  # noqa: E402
    Evidence,
    LiveSession,
    ResearchAnswer,
    SessionStore,
    _best_excerpt,
    _compose_answer,
    _content_score,
    _harvest_orgnr,
    _host_class,
    _bare_host,
    _identifying_result,
    _FORM_RANK,
    _GENERIC_NAME_TOKENS,
    _name_resolves_to,
    _is_error_page,
    _is_followup_phrase,
    _names_agree,
    _relevant_to,
    _strip_question,
    _subject_of,
    classify_input,
)
from liveweb import (  # noqa: E402
    SearchResult,
    _parse_bing,
    _parse_duckduckgo,
    _parse_mojeek,
    is_challenge_page,
    rank_results,
    strip_html,
)


class TestClassification(unittest.TestCase):
    def test_numbers_are_numbers(self):
        self.assertEqual(classify_input("995849364"), "orgnr")
        self.assertEqual(classify_input("923 609 016"), "orgnr")

    def test_names_are_names(self):
        self.assertEqual(classify_input("Equinor ASA"), "name")
        self.assertEqual(classify_input("Norsk Hydro"), "name")

    def test_domains_are_domains(self):
        for text in ("equinor.no", "www.equinor.no", "https://www.equinor.no/careers",
                     "norskhydro.com"):
            with self.subTest(text=text):
                self.assertEqual(classify_input(text), "domain")

    def test_questions_are_questions(self):
        for text in ("who runs Equinor?", "what does Norsk Hydro do",
                     "where is Equinor located", "hva gjør Equinor"):
            with self.subTest(text=text):
                self.assertEqual(classify_input(text), "question")

    def test_a_short_phrase_is_a_name_when_no_session(self):
        self.assertEqual(classify_input("contact info"), "name")


class TestPageStripping(unittest.TestCase):
    def test_tags_and_entities_are_removed(self):
        self.assertEqual(strip_html("<b>Equinor</b> &amp; Co"), "Equinor & Co")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(strip_html("a\n\n   b"), "a b")


class TestChallengeDetection(unittest.TestCase):
    def test_202_is_a_challenge(self):
        self.assertTrue(is_challenge_page(202, ""))

    def test_429_is_a_challenge(self):
        self.assertTrue(is_challenge_page(429, ""))

    def test_captcha_title_is_a_challenge(self):
        self.assertTrue(is_challenge_page(200, "<html><title>Captcha</title>..."))

    def test_a_normal_result_page_is_not(self):
        markup = "<html><title>Equinor - search</title><li>...</li></html>"
        self.assertFalse(is_challenge_page(200, markup))


class TestParsers(unittest.TestCase):
    def test_duckduckgo_redirect_is_unwrapped(self):
        markup = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fequinor.no%2Fx">'
            "Equinor</a>"
            '<a class="result__snippet">The energy company</a>'
        )
        results = _parse_duckduckgo(markup)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://equinor.no/x")
        self.assertEqual(results[0].title, "Equinor")
        self.assertEqual(results[0].snippet, "The energy company")

    def test_mojeek_parses_organic_results(self):
        markup = (
            '<ul><li><h2><a href="https://equinor.no/">Equinor</a></h2>'
            '<p class="s">Norwegian energy</p></li></ul>'
        )
        results = _parse_mojeek(markup)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://equinor.no/")
        self.assertEqual(results[0].snippet, "Norwegian energy")

    def test_mojeek_skips_relative_links(self):
        markup = '<ul><li><h2><a href="/about">About</a></h2></li></ul>'
        self.assertEqual(_parse_mojeek(markup), [])

    def test_bing_parses_b_algo(self):
        markup = (
            '<li class="b_algo"><h2><a href="https://equinor.no/">Equinor</a></h2>'
            "<p>A Norwegian company</p></li>"
        )
        results = _parse_bing(markup)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://equinor.no/")

    def test_empty_markup_yields_nothing(self):
        for parser in (_parse_duckduckgo, _parse_mojeek, _parse_bing):
            with self.subTest(parser=parser.__name__):
                self.assertEqual(parser(""), [])


class TestRanking(unittest.TestCase):
    def _results(self):
        return [
            SearchResult(title="Proff", url="https://www.proff.no/x", rank=0, engine="duckduckgo"),
            SearchResult(title="Brreg", url="https://virksomhet.brreg.no/y", rank=1, engine="duckduckgo"),
            SearchResult(title="Hydro", url="https://www.hydro.com/en/", rank=2, engine="duckduckgo"),
            SearchResult(title="Wikipedia", url="https://en.wikipedia.org/wiki/Norsk_Hydro",
                         rank=3, engine="duckduckgo"),
        ]

    def test_owned_page_outranks_directories(self):
        ranked = rank_results(self._results(), company_name="Norsk Hydro ASA")
        self.assertEqual(ranked[0].url, "https://www.hydro.com/en/")

    def test_directory_hosts_are_demoted(self):
        ranked = rank_results(self._results(), company_name="Norsk Hydro ASA")
        last = ranked[-1].url
        self.assertTrue(
            any(h in last for h in ("proff.no", "brreg.no", "wikipedia.org")))

    def test_rank_is_rewritten_after_sort(self):
        ranked = rank_results(self._results(), company_name="Norsk Hydro ASA")
        self.assertEqual([r.rank for r in ranked], [0, 1, 2, 3])


class TestRelevance(unittest.TestCase):
    def test_unrelated_result_is_dropped(self):
        results = [SearchResult(title="Kryssfiner", url="https://no.wikipedia.org/wiki/Kryssfiner",
                                snippet="plywood", rank=0, engine="wikipedia:no")]
        self.assertEqual(_relevant_to("Norsk Hydro ASA", results), [])

    def test_related_result_is_kept(self):
        results = [SearchResult(title="Norsk Hydro ASA", url="https://no.wikipedia.org/wiki/Norsk_Hydro",
                                snippet="aluminium", rank=0, engine="wikipedia:no")]
        self.assertEqual(len(_relevant_to("Norsk Hydro ASA", results)), 1)

    def test_generic_name_attributes_nothing(self):
        # A name made only of words like "invest" has no distinctive token, so no
        # web result can be safely attributed to it.
        results = [SearchResult(title="Investopedia", url="https://investopedia.com",
                                snippet="invest", rank=0, engine="duckduckgo")]
        self.assertEqual(_relevant_to(":-) INVEST AS", results), [])

    def test_social_and_reference_hosts_are_classified(self):
        self.assertEqual(_host_class("https://www.youtube.com/@VasaTest"), "social")
        self.assertEqual(_host_class("https://www.linkedin.com/company/x"), "social")
        self.assertEqual(_host_class("https://merriam-webster.com/dictionary/vastest"),
                         "reference")
        self.assertEqual(_host_class("https://no.wikipedia.org/wiki/Equinor"), "reference")
        self.assertEqual(_host_class("https://www.equinor.com/"), "general")
        self.assertEqual(_host_class("https://www.dnb.no/"), "general")


class TestDomainRelevance(unittest.TestCase):
    """A domain query must match the host of a result, not its literal spelling.

    Regression guard for a live defect found on 2026-10-10: ``www.dnb.no`` returned
    "no sourced answer" even though the engine had returned ten pages served from
    dnb.no, because tokenising the input produced ["www", "dnb"] and "www" appears
    in no page text.
    """

    def test_bare_host_normalises_www_and_paths(self):
        self.assertEqual(_bare_host("www.dnb.no"), "dnb.no")
        self.assertEqual(_bare_host("dnb.no"), "dnb.no")
        self.assertEqual(_bare_host("https://www.equinor.no/careers"), "equinor.no")
        self.assertEqual(_bare_host("http://equinor.no:443/x"), "equinor.no")

    def test_non_hosts_yield_empty(self):
        self.assertEqual(_bare_host("Equinor ASA"), "")
        self.assertEqual(_bare_host("who runs it?"), "")
        self.assertEqual(_bare_host("123456789"), "")

    def test_results_from_the_named_host_are_relevant(self):
        results = [
            SearchResult(title="DNB - Bank fra A til Å", url="https://www.dnb.no/", rank=0),
            SearchResult(title="Nettbank", url="https://www.dnb.no/dagligbank/nettbank", rank=1),
            SearchResult(title="Unrelated", url="https://example.com/", rank=2),
        ]
        kept = _relevant_to("www.dnb.no", results)
        self.assertEqual(len(kept), 2)
        self.assertTrue(all("dnb.no" in r.url for r in kept))

    def test_a_subdomain_counts_as_the_same_site(self):
        results = [SearchResult(title="Careers", url="https://careers.equinor.no/x", rank=0)]
        self.assertEqual(len(_relevant_to("equinor.no", results)), 1)


class TestRegisterNameResolution(unittest.TestCase):
    """Choosing among fuzzy register hits must prefer the company, not a club.

    Regression guard for a live defect found on 2026-10-10: a bare name never
    reached the register at all (a company's own pages rarely print their
    organisation number), so "who runs Equinor?" was answered from a homepage
    slogan while the register holding the CEO's name sat one query away. When the
    register *was* consulted, its fuzzy search returned unions, clubs, pension
    funds and foreign branches alongside the operating company -- and picking the
    shortest name chose "TELENOR A/S" (foreign) over "TELENOR ASA".
    """

    def test_every_token_must_be_present(self):
        self.assertTrue(_name_resolves_to("Telenor", "TELENOR ASA"))
        self.assertTrue(_name_resolves_to("Norsk Hydro", "Norsk Hydro ASA"))
        # "Equinor" must not match a name that does not contain it.
        self.assertFalse(_name_resolves_to("Equinor", "STATOIL ASA"))

    def test_union_prefixes_are_treated_as_generic(self):
        # "NITO TELENOR" *does* contain the token "telenor", so this check alone
        # accepts it -- deliberately. Rejecting the union is the legal-form
        # ranking's job (see the test below), not this one's, because a union
        # whose name genuinely contains the company name is a real entity that
        # must be considered and then out-ranked rather than silently skipped.
        self.assertTrue(_name_resolves_to("Telenor", "NITO TELENOR"))
        self.assertIn("nito", _GENERIC_NAME_TOKENS)

    def test_generic_only_name_resolves_to_nothing(self):
        self.assertFalse(_name_resolves_to("Holding AS", "NORDIC HOLDING AS"))

    def test_form_rank_prefers_an_operating_company(self):
        # The ordering that fixed the live bug: ASA/AS outrank unions, funds and
        # foreign forms, so a bare name selects the company itself.
        self.assertLess(_FORM_RANK["ASA"], _FORM_RANK["FLI"])
        self.assertLess(_FORM_RANK["ASA"], _FORM_RANK["UTLA"])
        self.assertLess(_FORM_RANK["AS"], _FORM_RANK["NUF"])
        self.assertLess(_FORM_RANK["AS"], _FORM_RANK["PK"])
        # A union out-ranks nothing that is an operating company.
        for form in ("ASA", "AS", "SA", "ANS", "DA"):
            self.assertLess(_FORM_RANK[form], _FORM_RANK["FLI"])


class TestIdentifyingResult(unittest.TestCase):
    """Identity is a stricter bar than relevance, and must stay that way.

    Regression guard for a live defect found on 2026-10-10: searching a name that
    does not exist ("VasaTest AS") returned an Australian asbestos-testing page as
    the answer, and renamed the subject after a YouTube channel that merely shared
    the string. Relevance said yes; identity should have said no.
    """

    def test_video_channel_is_not_an_identity(self):
        results = [SearchResult(title="VasaTest - YouTube",
                                url="https://www.youtube.com/@VasaTest", rank=0)]
        self.assertIsNone(_identifying_result("VasaTest AS", results))

    def test_dictionary_entry_is_not_an_identity(self):
        results = [SearchResult(title="VASTEST Definition & Meaning",
                                url="https://merriam-webster.com/dictionary/vastest", rank=0)]
        self.assertIsNone(_identifying_result("VasaTest AS", results))

    def test_company_site_is_an_identity(self):
        results = [SearchResult(title="Equinor ASA", url="https://www.equinor.com/", rank=0)]
        named = _identifying_result("Equinor ASA", results)
        self.assertIsNotNone(named)
        self.assertEqual(named.url, "https://www.equinor.com/")

    def test_partial_token_overlap_is_not_an_identity(self):
        # "VETASSESS" shares no whole distinctive token with "VasaTest AS"; only a
        # substring, which is not agreement.
        results = [SearchResult(title="Australia's largest skills assessment service",
                                url="https://www.vetassess.com.au/", rank=0)]
        self.assertIsNone(_identifying_result("VasaTest AS", results))

    def test_empty_results_yield_none(self):
        self.assertIsNone(_identifying_result("Equinor ASA", []))


class TestUnmatchedNameIsNotFabricated(unittest.TestCase):
    """A name that matches nothing must report a gap, never quote a stray page."""

    def test_unrelated_pages_are_not_used_as_an_answer(self):
        # The exact shape of the live failure: irrelevant results, one of which is
        # readable prose, must not become the answer.
        evidence = [
            Evidence(field="search_result", value="Asbestos Testing Melbourne",
                     source_url="https://www.airsafe.net.au/x", quote="Asbestos testing",
                     kind="web", confidence=0.5),
            Evidence(field="page_excerpt", value="Asbestos Testing Melbourne",
                     source_url="https://www.airsafe.net.au/x",
                     quote="NSW VIC QLD WA TAS SA NT ACT Tell us a bit about the job The size "
                           "of the building or site, what's planned, anything unusual.",
                     kind="web_rules", confidence=0.72),
        ]
        text, chosen, answered = _compose_answer(
            "VasaTest AS", evidence, kind="name", target="VasaTest AS",
            searched=["duckduckgo"])
        # The generic page fallback would have answered this; with no register fact
        # and no page that concerns the company, there is nothing to answer with.
        self.assertTrue(answered or not chosen,
                        "a stray page must not be presented as the company's answer")
        if chosen:
            self.assertNotIn("Asbestos", text)


class TestSubjectExtraction(unittest.TestCase):
    def test_question_yields_the_company(self):
        self.assertEqual(_subject_of("who runs Equinor?"), "Equinor")
        self.assertEqual(_subject_of("what does Norsk Hydro ASA do?"), "Norsk Hydro ASA")
        self.assertEqual(_subject_of("who is the CEO of Telenor?"), "Telenor")

    def test_no_subject_returns_empty(self):
        self.assertEqual(_subject_of("how does this work?"), "")

    def test_question_prefix_is_stripped_for_search(self):
        self.assertEqual(_strip_question("what is Norsk Hydro"), "Norsk Hydro")
        self.assertEqual(_strip_question("who is Equinor?"), "Equinor")


class TestFollowupPhrase(unittest.TestCase):
    def test_short_phrases_are_followups(self):
        # A phrase with a question mark is already classified as a question, so
        # this helper only has to catch the bare, unpunctuated follow-ups.
        for text in ("contact info", "their finances", "employees", "more details"):
            with self.subTest(text=text):
                self.assertTrue(_is_followup_phrase(text))

    def test_questions_are_not_phrases(self):
        # "who runs it?" is handled by the question path, not the phrase path.
        self.assertFalse(_is_followup_phrase("who runs it?"))

    def test_a_new_company_name_is_not_a_followup(self):
        self.assertFalse(_is_followup_phrase("Norsk Hydro ASA"))
        self.assertFalse(_is_followup_phrase("Equinor"))


class TestAnswerComposition(unittest.TestCase):
    def _evidence(self):
        return [
            Evidence(field="legal_name", value="Equinor ASA", source_url="u1",
                     quote="Equinor ASA", kind="registry", confidence=1.0),
            Evidence(field="ceo", value="Anders Opedal", source_url="u2",
                     quote='"navn": "Anders Opedal"', kind="registry", confidence=0.96),
            Evidence(field="revenue_latest", value="1000", source_url="u3",
                     quote="revenue 1000", kind="registry", confidence=0.9),
        ]

    def test_leadership_question_picks_ceo(self):
        text, chosen, answered = _compose_answer(
            "who runs it?", self._evidence(), kind="question", target="Equinor ASA",
            searched=[])
        self.assertTrue(answered)
        self.assertIn("Anders Opedal", text)
        self.assertEqual([c.field for c in chosen], ["ceo"])

    def test_financials_question_picks_revenue(self):
        _, chosen, _ = _compose_answer(
            "what are the financials?", self._evidence(), kind="question",
            target="Equinor ASA", searched=[])
        self.assertIn("revenue_latest", [c.field for c in chosen])

    def test_no_evidence_reports_the_gap(self):
        text, chosen, answered = _compose_answer(
            "who runs it?", [], kind="question", target="Equinor ASA",
            searched=["duckduckgo"])
        self.assertFalse(answered)
        self.assertEqual(chosen, [])
        self.assertIn("duckduckgo", text)

    def test_web_page_quote_becomes_the_answer(self):
        evidence = [Evidence(field="knowledge", value="Hydro article",
                             source_url="https://no.wikipedia.org/wiki/Norsk_Hydro",
                             quote="Norsk Hydro ASA er en norsk produsent av aluminium.",
                             kind="web_rules", confidence=0.7)]
        text, chosen, answered = _compose_answer(
            "what does Norsk Hydro do?", evidence, kind="name",
            target="Norsk Hydro ASA", searched=[])
        self.assertTrue(answered)
        self.assertIn("aluminium", text)
        self.assertEqual(chosen[0].field, "knowledge")


class TestSessionStoreAndContext(unittest.TestCase):
    def test_store_returns_the_same_session(self):
        store = SessionStore()
        self.assertIs(store.session("a"), store.session("a"))
        self.assertIsNot(store.session("a"), store.session("b"))

    def test_reset_drops_the_session(self):
        store = SessionStore()
        s1 = store.session("a")
        store.reset("a")
        self.assertIsNot(store.session("a"), s1)

    def test_followup_reuses_the_company_without_search(self):
        # Seed the session context directly, then confirm a follow-up does not
        # clear it and is labelled as a follow-up.
        session = LiveSession("t", no_web=True)
        session.target_org = "995849364"
        session.target_name = ":-) INVEST AS"
        answer = session.ask("who runs it?")
        self.assertEqual(answer.kind, "question")
        self.assertTrue(any("follow-up" in n for n in answer.notes))


class TestWrongEntityGuard(unittest.TestCase):
    """The bug this class exists to prevent: a search for one company returning
    another. A register number lifted off an unrelated listing (directory pages
    show "related companies") must never be adopted as the answer's subject, and
    a harvested number must be confirmed by name before it stands in for the
    query."""

    def test_names_agree_on_the_same_company(self):
        self.assertTrue(_names_agree("Equinor ASA", "EQUINOR ASA"))
        self.assertTrue(_names_agree("Equinor", "EQUINOR ASA"))
        self.assertTrue(_names_agree("Telenor ASA", "TELENOR ASA"))

    def test_names_disagree_on_a_different_company(self):
        # The exact production failure: "Equinor ASA" must not resolve to Borregaard.
        self.assertFalse(_names_agree("Equinor ASA", "BORREGAARD AS"))
        self.assertFalse(_names_agree("Norsk Hydro ASA", "YARA INTERNATIONAL ASA"))

    def test_a_generic_only_name_cannot_be_confirmed(self):
        # "INVEST AS" has no distinctive token, so no register name can confirm it.
        self.assertFalse(_names_agree("INVEST AS", "EQUINOR ASA"))

    def test_harvest_ignores_numbers_on_unrelated_results(self):
        # A directory page listing Borregaard must not yield a number for a
        # search about Equinor.
        results = [
            SearchResult(title="Equinor ASA", url="https://www.equinor.no/",
                         snippet="Equinor is an energy company", rank=0, engine="bing"),
            SearchResult(title="Lignende selskaper - Borregaard AS org.nr 895 623 032",
                         url="https://www.proff.no/bransje/lignende", rank=1, engine="bing"),
        ]
        self.assertEqual(_harvest_orgnr(results, expected_name="Equinor ASA"), "")

    def test_harvest_accepts_a_number_from_a_relevant_result(self):
        results = [
            SearchResult(title="Equinor ASA - org.nr 923 609 016",
                         url="https://www.proff.no/equinor", rank=0, engine="bing",
                         snippet="Equinor ASA"),
        ]
        self.assertEqual(_harvest_orgnr(results, expected_name="Equinor ASA"), "923609016")

    def test_harvest_without_expected_name_is_backward_compatible(self):
        results = [SearchResult(title="Equinor ASA org.nr 923 609 016",
                                url="https://x.no", rank=0, engine="bing")]
        self.assertEqual(_harvest_orgnr(results), "923609016")


class TestContentQuality(unittest.TestCase):
    """A fetched page's nav, a cookie footer or a WAF error must never be quoted as
    a company description. These pin the signals that decide that."""

    FOOTER = ("enor Samfunnsansvar Digital Sikkerhet Jobb i Telenor Pressesenter "
              "Personvern Informasjonskapsler (Cookies) Åpenhetsloven © 1855-2026 Telenor ASA")
    ERROR = ("Please consult with your administrator. Your support ID is: "
             "4699327448522650977 Go Back")
    ARTICLE = ("Telenor ASA er et norsk telekommunikasjonsselskap og en av verdens "
               "største mobiloperatører. Selskapet har hovedkontor på Fornebu i Bærum. "
               "Telenor ble grunnlagt i 1855 og driver virksomhet i Norge og Norden.")

    def test_error_page_is_rejected_outright(self):
        self.assertTrue(_is_error_page(self.ERROR))
        self.assertEqual(_content_score(self.ERROR, "Telenor ASA"), 0)

    def test_footer_scores_zero(self):
        self.assertEqual(_content_score(self.FOOTER, "Telenor ASA"), 0)

    def test_prose_scores_positive(self):
        self.assertGreater(_content_score(self.ARTICLE, "Telenor ASA"), 0)

    def test_best_excerpt_prefers_prose_over_footer(self):
        # A realistic page: nav, header block, the article, then a footer. The
        # excerpt must be the article, not the surrounding chrome.
        page = ("Meny Hjem Tjenester Personvern Informasjonskapsler. "
                + self.FOOTER + ". "
                + self.ARTICLE + " "
                + self.FOOTER)
        excerpt = _best_excerpt(page, "Telenor ASA")
        self.assertNotIn("Informasjonskapsler", excerpt)
        self.assertIn("telekommunikasjonsselskap", excerpt)

    def test_best_excerpt_returns_empty_for_a_pure_error_page(self):
        self.assertEqual(_best_excerpt(self.ERROR, "Telenor ASA"), "")

    def test_best_excerpt_returns_empty_for_chrome_only(self):
        self.assertEqual(_best_excerpt(self.FOOTER, "Telenor ASA"), "")


class TestAnswerShape(unittest.TestCase):
    def test_to_dict_is_serialisable(self):
        import json

        answer = ResearchAnswer(question="q", kind="name", target="X", answer="y",
                                answered=True, sources=[{"url": "u"}])
        blob = json.dumps(answer.to_dict())
        self.assertIn("\"question\": \"q\"", blob)

    def test_render_lists_sources(self):
        answer = ResearchAnswer(question="q", kind="name", target="X", answer="y",
                                answered=True,
                                sources=[{"title": "T", "url": "https://e.no", "quote": "z"}])
        text = answer.render()
        self.assertIn("SOURCES", text)
        self.assertIn("https://e.no", text)


class TestEmptyQueryIsRefused(unittest.TestCase):
    """A blank query must never be answered about the company left in session.

    ``classify_input("")`` returns "question", so before this guard a blank box
    took the follow-up path and reported on whatever company the previous turn
    had resolved -- a confident, well-sourced answer to a question nobody asked.
    """

    def test_empty_string_answers_nothing(self):
        s = LiveSession()
        s.target_org = "923609016"
        s.target_name = "EQUINOR ASA"
        a = s.ask("")
        self.assertFalse(a.answered)
        self.assertEqual(a.sources, [])
        self.assertEqual(a.kind, "empty")

    def test_whitespace_answers_nothing(self):
        s = LiveSession()
        s.target_org = "923609016"
        s.target_name = "EQUINOR ASA"
        a = s.ask("     ")
        self.assertFalse(a.answered)
        self.assertEqual(a.kind, "empty")

    def test_empty_does_not_name_the_session_company(self):
        """The failure mode was naming the previous company, so assert that."""
        s = LiveSession()
        s.target_org = "923609016"
        s.target_name = "EQUINOR ASA"
        a = s.ask("")
        self.assertNotIn("EQUINOR", (a.target or "").upper())
        self.assertNotIn("EQUINOR", (a.answer or "").upper())


if __name__ == "__main__":
    unittest.main()
