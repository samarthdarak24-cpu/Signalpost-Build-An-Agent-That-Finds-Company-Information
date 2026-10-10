"""Unit tests for validation, the ownership gate, and delta detection.

These three are tested hardest because they are where a wrong answer comes from:
a bad number, a wrong match, or a change that was never noticed.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from delta import SnapshotStore, WebSnapshot, apply_registry_change, detect_web_changes  # noqa: E402
from coverage import location_facts, people_facts  # noqa: E402
from envelope import STATUSES, Envelope, envelopes_to_jsonl  # noqa: E402
from gate import CONFIDENCE, decide, evaluate  # noqa: E402
from normalize import Page, extract_postcodes, fold_name, social_links, visible_text  # noqa: E402
from profiles import Fact, Profile, SkippedField, whitespace_contains  # noqa: E402
from registry import RegistryStore, _number, address_line, entity_facts  # noqa: E402
from validate import (  # noqa: E402
    check_digit,
    clean,
    format_display,
    harvest_from_text,
    validate,
)


class TestOrgNumberValidation(unittest.TestCase):
    """The validator is the pipeline's first line of defence."""

    def test_known_good_numbers(self) -> None:
        for raw in ("986587098", "986 587 098", "986.587.098", "NO 986 587 098 MVA",
                    "923609016", "974760673"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(validate(raw), f"{raw} should validate")

    def test_check_digit_rejected(self) -> None:
        # Same payload, wrong check digit.
        self.assertIsNone(validate("986587099"))
        self.assertIsNone(validate("923609017"))

    def test_wrong_length_rejected(self) -> None:
        for raw in ("98658709", "9865870987", "", "abcdefghi", "98658709a", None):
            with self.subTest(raw=raw):
                self.assertIsNone(validate(raw))

    def test_modulus_eleven_weights(self) -> None:
        # 98658709 has weighted sum 3*9+2*8+7*6+6*5+5*8+4*7+3*0+2*9 = 225; 225 % 11 == 5
        # so the check digit is 11 - 5 = 6, and the published number ends in 8.
        # Verify the reference implementation directly instead.
        self.assertEqual(check_digit("98658709"), 8)
        self.assertEqual(check_digit("92360901"), 6)
        self.assertEqual(check_digit("97476067"), 3)

    def test_check_digit_ten_is_impossible(self) -> None:
        # A payload whose remainder is 1 would need a check digit of 10, which
        # no single digit can carry. 00000006 is such a payload.
        self.assertIsNone(check_digit("00000006"))
        self.assertIsNone(validate("000000060"))

    def test_harvest_finds_valid_numbers_only(self) -> None:
        # Note: a run of identical digits passes modulus-11 as often as not
        # (111111111 does), so the checksum is a strong filter, not a proof of
        # reality. That is why the gate also requires the register record.
        text = (
            "Org.nr: 986 587 098  ·  Tlf: 12345678  ·  Postnr 0582\n"
            "Also known as 923609016 and a rejected 986587099."
        )
        found = harvest_from_text(text)
        self.assertIn("986587098", found)
        self.assertIn("923609016", found)
        self.assertNotIn("12345678", found)   # too short to be considered
        self.assertNotIn("986587099", found)  # fails the checksum

    def test_harvest_ignores_longer_digit_runs(self) -> None:
        self.assertEqual(harvest_from_text("account 1234986587098999"), set())

    def test_display_and_clean_round_trip(self) -> None:
        self.assertEqual(clean("986 587 098"), "986587098")
        self.assertEqual(format_display("986587098"), "986 587 098")


def _page(url: str, body: str, *, status: int = 200) -> Page:
    return Page(url=url, status=status, markup=body, retrieved_at="2026-10-07T00:00:00Z")


class TestOwnershipGate(unittest.TestCase):
    """The gate is the wrong-company guard, so it is tested from both sides."""

    ORG = validate("986587098")
    NAME = "2CARE BPA AS"

    def test_accepts_page_with_number(self) -> None:
        page = _page(
            "https://havas.no/",
            "<html><body><h1>2Care BPA</h1>"
            "<p>Organisasjonsnummer: 986 587 098</p>"
            "<p>Brobekkveien 80, 0582 Oslo. Vi tilbyr brukerstyrt personlig assistanse "
            "til kommuner og bydeler over hele landet.</p></body></html>",
        )
        verdict = evaluate(page, self.ORG, name=self.NAME, postcode="0582", city="OSLO")
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.match_proof, "orgnr_on_page")
        self.assertGreaterEqual(verdict.confidence, CONFIDENCE["orgnr_on_page"])

    def test_rejects_page_with_a_different_number(self) -> None:
        """A group site showing the parent's number must not be accepted."""
        page = _page(
            "https://group.example/",
            "<html><body><h1>2Care BPA</h1><p>Org.nr 923 609 016</p>"
            "<p>Konsernets hovedkontor. Alle selskaper i gruppen rapporterer hit. "
            "Kontakt oss for mer informasjon om våre tjenester.</p></body></html>",
        )
        verdict = evaluate(page, self.ORG, name=self.NAME, postcode="0582", city="OSLO")
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.verdict, "rejected_wrong_orgnr")
        self.assertIn("923609016", verdict.other_org_numbers)

    def test_name_only_is_never_accepted(self) -> None:
        """The collision case: right name, no number, no address."""
        page = _page(
            "https://somewhere-else.no/",
            "<html><body><h1>2Care BPA AS</h1>"
            "<p>Vi er en helt annen bedrift med samme navn, lokalisert i Bergen. "
            "Ring oss gjerne for en hyggelig prat om våre tjenester.</p></body></html>",
        )
        verdict = evaluate(page, self.ORG, name=self.NAME, postcode="0582", city="OSLO")
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.verdict, "rejected_name_only")
        self.assertLess(verdict.confidence, 0.5)

    def test_name_plus_address_accepted_at_lower_confidence(self) -> None:
        page = _page(
            "https://havas.no/om-oss",
            "<html><body><h1>2Care BPA tilbyr brukerstyrt personlig assistanse</h1>"
            "<p>Besøk oss i Brobekkveien 80, 0582 OSLO. Vi har kontorer sentralt i byen "
            "og tar imot henvendelser på hverdager.</p></body></html>",
        )
        verdict = evaluate(page, self.ORG, name=self.NAME, postcode="0582", city="OSLO")
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.match_proof, "name_postcode")
        self.assertLess(verdict.confidence, CONFIDENCE["orgnr_on_page"])

    def test_name_with_wrong_postcode_rejected(self) -> None:
        page = _page(
            "https://elsewhere.no/",
            "<html><body><h1>2Care BPA AS</h1>"
            "<p>Adresse: Solskinnsveien 4, 5003 BERGEN. Vi holder til på Vestlandet og "
            "betjener kunder i hele regionen.</p></body></html>",
        )
        verdict = evaluate(page, self.ORG, name=self.NAME, postcode="0582", city="OSLO")
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.verdict, "rejected_name_only")

    def test_block_page_rejected(self) -> None:
        page = _page("https://blocked.no/", "<html><body>Access denied</body></html>")
        verdict = evaluate(page, self.ORG, name=self.NAME, postcode="0582", city="OSLO")
        self.assertEqual(verdict.verdict, "rejected_text_too_short")

    def test_error_status_rejected(self) -> None:
        page = _page("https://gone.no/", "<html><body>404</body></html>", status=404)
        verdict = evaluate(page, self.ORG, name=self.NAME)
        self.assertFalse(verdict.accepted)

    def test_decide_prefers_number_proof(self) -> None:
        weak = evaluate(
            _page("https://weak.no/", "<html><body><h1>2Care BPA</h1>"
                  "<p>Brobekkveien 80, 0582 OSLO. Kontakt oss for mer informasjon.</p>"
                  "</body></html>"),
            self.ORG, name=self.NAME, postcode="0582", city="OSLO",
        )
        strong = evaluate(
            _page("https://strong.no/", "<html><body><h1>2Care BPA</h1>"
                  "<p>Org.nr: 986 587 098. Vi er sertifisert og klar for oppdrag.</p>"
                  "</body></html>"),
            self.ORG, name=self.NAME, postcode="0582", city="OSLO",
        )
        decision = decide([weak, strong])
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.match_proof, "orgnr_on_page")
        self.assertEqual(decision.url, "https://strong.no/")

    def test_decide_all_rejected(self) -> None:
        bad = evaluate(_page("https://x.no/", "<html><body>Nothing here at all.</body></html>"),
                       self.ORG, name=self.NAME)
        self.assertFalse(decide([bad]).accepted)


class TestPageParsing(unittest.TestCase):
    ORG = validate("986587098")

    def test_visible_text_strips_scripts(self) -> None:
        markup = ("<html><head><script>var org='986587098';</script>"
                  "<style>.a{color:red}</style></head>"
                  "<body><p>Hello&nbsp;world</p></body></html>")
        text = visible_text(markup)
        self.assertIn("Hello world", text)
        self.assertNotIn("var org", text)
        self.assertNotIn("color:red", text)

    def test_page_scans_markup_for_numbers(self) -> None:
        """A number only present in structured data still proves ownership."""
        page = _page(
            "https://x.no/",
            '<html><head><script type="application/ld+json">'
            '{"@type":"Organization","vatID":"NO 986 587 098 MVA"}</script></head>'
            "<body><p>Velkommen til oss. Vi hjelper deg med det du trenger, "
            "hver eneste dag, i hele Norge.</p></body></html>",
        )
        self.assertTrue(page.proves(self.ORG))

    def test_fold_name_drops_legal_suffixes(self) -> None:
        self.assertEqual(fold_name("2CARE BPA AS"), fold_name("2Care BPA"))
        self.assertEqual(fold_name("HAV AS"), "hav")

    def test_postcode_range(self) -> None:
        found = extract_postcodes("0582 OSLO, 9999 place, 0001 start, 5003 BERGEN")
        self.assertIn("0582", found)
        self.assertNotIn("0001", found)  # below the valid band
        self.assertNotIn("9999", found)  # above the valid band

    def test_social_links_from_anchors_only(self) -> None:
        markup = (
            '<a href="https://www.facebook.com/2carebpa">F</a>'
            '<a href="https://facebook.com/sharer/sharer.php?u=x">Share</a>'
            '<a href="mailto:post@havas.no">Mail</a>'
        )
        links = social_links(markup, "https://havas.no")
        self.assertEqual(links["facebook"], "https://www.facebook.com/2carebpa")


class TestRegistryHelpers(unittest.TestCase):
    def test_entity_facts_is_null_not_zero(self) -> None:
        payload = {
            "navn": "TEST AS",
            "antallAnsatte": 0,
            "organisasjonsform": {"kode": "AS"},
            "hjemmeside": "",
            "forretningsadresse": {"postnummer": "0582", "kommune": "OSLO"},
        }
        facts = entity_facts(payload)
        self.assertEqual(facts["employees"], 0)      # a filed zero stays zero
        self.assertNotIn("website", facts)           # an empty string becomes absent
        self.assertEqual(facts["legal_name"], "TEST AS")

    def test_number_conversion_never_fabricates(self) -> None:
        self.assertIsNone(_number(None))
        self.assertIsNone(_number(""))
        self.assertIsNone(_number("not a number"))
        self.assertEqual(_number(0.0), 0)
        self.assertEqual(_number(36177737.0), 36177737)
        self.assertEqual(_number("36 177 737"), 36177737)

    def test_address_line(self) -> None:
        payload = {"forretningsadresse": {
            "adresse": ["Brobekkveien 80"], "postnummer": "0582", "poststed": "OSLO"}}
        self.assertEqual(address_line(payload), "Brobekkveien 80, 0582 OSLO")

    def test_register_fields_that_were_silently_dropped(self) -> None:
        """Regression: city, description and the mobile fallback must survive.

        A second, hardcoded copy of the field table once lived in ``pipeline``
        and drifted from this one, so ``poststed``, ``aktivitet`` and ``mobil``
        were read from the register and then thrown away. These three assertions
        are what make that drift impossible to reintroduce unnoticed.
        """
        payload = {
            "navn": "TEST AS",
            "forretningsadresse": {
                "adresse": ["Storgata 1"],
                "postnummer": "6062",
                "poststed": "BRANDAL",
                "kommune": "HAREID",
            },
            "aktivitet": ["Uttak av tømmer."],
            "mobil": "901 73 343",
        }
        facts = entity_facts(payload)
        self.assertEqual(facts["city"], "BRANDAL")
        self.assertEqual(facts["description"], "Uttak av tømmer.")
        self.assertEqual(facts["phone"], "901 73 343")

    def test_phone_prefers_switchboard_over_mobile(self) -> None:
        """Both numbers are register values; the landline is the better default."""
        payload = {"telefon": "22 00 00 00", "mobil": "901 73 343"}
        self.assertEqual(entity_facts(payload)["phone"], "22 00 00 00")
        # and the mobile is used when it is the only number published
        self.assertEqual(entity_facts({"mobil": "901 73 343"})["phone"], "901 73 343")
        # neither present means the field is simply absent, never blank
        self.assertNotIn("phone", entity_facts({"navn": "TEST AS"}))

    def test_registry_facts_table_is_the_only_source(self) -> None:
        """The pipeline must build register facts from REGISTRY_FACTS itself.

        If someone re-inlines the table, the two copies can drift again -- which
        is exactly the bug this guards. Checking the import keeps one source.
        """
        from registry import REGISTRY_FACTS  # noqa: PLC0415

        fields = {field for _, field, _ in REGISTRY_FACTS}
        for required in ("city", "description", "phone", "legal_name"):
            self.assertIn(required, fields)

    def test_pipeline_emits_the_new_register_fields(self) -> None:
        """End to end: a register payload must produce city/description facts."""
        from pipeline import _registry_facts  # noqa: PLC0415

        payload = {
            "navn": "TEST AS",
            "forretningsadresse": {"poststed": "BRANDAL", "kommune": "HAREID"},
            "aktivitet": ["Uttak av tømmer."],
            "mobil": "901 73 343",
        }
        facts = _registry_facts(
            payload, number="986587098",
            retrieved_at="2026-10-07T00:00:00Z",
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/986587098",
        )
        by_field = {f.field: f for f in facts}
        self.assertEqual(by_field["city"].value, "BRANDAL")
        self.assertEqual(by_field["description"].value, "Uttak av tømmer.")
        self.assertEqual(by_field["phone"].value, "901 73 343")
        # every register fact carries the register proof, so it is scoreable
        for name in ("city", "description", "phone"):
            self.assertEqual(by_field[name].match_proof, "registry")
            self.assertEqual(by_field[name].layer, "registry")


class TestDeltaEngine(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="signalpost-test-"))
        self.store = RegistryStore(self.tmp / "registry.sqlite")
        self.snapshots = SnapshotStore(self.tmp / "snapshots.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        self.snapshots.close()

    def test_registry_change_creates_then_updates(self) -> None:
        create = {
            "organisasjonsnummer": "986587098", "oppdateringstype": "NY",
            "enhet": {"organisasjonsnummer": "986587098", "navn": "2CARE BPA AS",
                      "antallAnsatte": 244},
        }
        delta = apply_registry_change(self.store, create)
        self.assertEqual(delta.created, 1)
        self.assertEqual(self.store.count(), 1)

        update = {
            "organisasjonsnummer": "986587098", "oppdateringstype": "ENDRING",
            "enhet": {"organisasjonsnummer": "986587098", "navn": "2CARE BPA AS",
                      "antallAnsatte": 250},
        }
        delta = apply_registry_change(self.store, update)
        self.assertEqual(delta.updated, 1)
        self.assertTrue(any(c.field == "employees" and c.old == 244 and c.new == 250
                            for c in delta.changes))
        self.assertEqual(self.store.lookup("986587098")["antallAnsatte"], 250)

    def test_deletion_flags_without_removing(self) -> None:
        apply_registry_change(self.store, {
            "organisasjonsnummer": "986587098", "oppdateringstype": "NY",
            "enhet": {"organisasjonsnummer": "986587098", "navn": "2CARE BPA AS"}})
        delta = apply_registry_change(self.store, {
            "organisasjonsnummer": "986587098", "oppdateringstype": "SLETTING"})
        self.assertEqual(delta.deleted, 1)
        self.assertEqual(self.store.count(), 1)             # kept as history
        self.assertIsNone(self.store.lookup("986587098"))   # but no longer served

    def test_rejects_malformed_change_events(self) -> None:
        self.assertEqual(apply_registry_change(self.store, {}).applied, 1)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(
            apply_registry_change(
                self.store,
                {"organisasjonsnummer": "12345", "oppdateringstype": "NY", "enhet": {}},
            ).created, 0,
        )

    def test_snapshot_round_trip(self) -> None:
        snapshot = WebSnapshot(url="https://havas.no/", content_hash="abc123",
                               etag='W/"1"', checked_at="2026-10-07T00:00:00Z")
        self.snapshots.put(snapshot)
        loaded = self.snapshots.get("https://havas.no/")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.content_hash, "abc123")
        self.assertEqual(loaded.etag, 'W/"1"')
        self.assertIsNone(self.snapshots.get("https://never-seen.no/"))

    def test_fact_history_enables_cross_run_diff(self) -> None:
        profile = Profile(org_number="986587098", legal_name="2CARE BPA AS",
                          run_date="2026-10-07", run_id="r1")
        profile.add(Fact(field="phone", value="45 22 22 88", source_url="u",
                         retrieved_at="2026-10-07T00:00:00Z", evidence="e",
                         match_proof="registry", confidence=0.95))
        self.snapshots.record_facts(profile)
        self.assertEqual(self.snapshots.previous_value("986587098", "phone"), "45 22 22 88")
        self.assertIsNone(self.snapshots.previous_value("986587098", "nothing"))

    def test_web_change_detection_reports_old_and_new(self) -> None:
        profile = Profile(org_number="986587098", legal_name="2CARE BPA AS",
                          run_date="2026-10-07", run_id="r2")
        profile.add(Fact(field="phone", value="45 22 22 99", source_url="u",
                         retrieved_at="2026-10-07T00:00:00Z", evidence="e",
                         match_proof="orgnr_on_page", confidence=0.96, layer="web_rules"))
        changes = detect_web_changes(
            profile, org_number="986587098",
            previous={"phone": "45 22 22 88", "email": "post@havas.no"},
            source_url="https://havas.no/",
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].old, "45 22 22 88")
        self.assertEqual(changes[0].new, "45 22 22 99")
        self.assertEqual(changes[0].kind, "web")

    def test_web_change_detection_ignores_identical_values(self) -> None:
        profile = Profile(org_number="986587098", legal_name="X", run_date="d", run_id="r3")
        profile.add(Fact(field="phone", value="45 22 22 88", source_url="u",
                         retrieved_at="t", evidence="e", match_proof="registry", confidence=0.9))
        self.assertEqual(
            detect_web_changes(profile, org_number="986587098",
                               previous={"phone": "45 22 22 88"}, source_url="u"), [])


class TestProfileAbstention(unittest.TestCase):
    def test_below_threshold_abstains(self) -> None:
        profile = Profile(org_number="986587098", legal_name="X", run_date="d")
        kept = profile.add(
            Fact(field="email", value="guess@maybe.no", source_url="u", retrieved_at="t",
                 evidence="e", match_proof="name_only", confidence=0.35),
            minimum_confidence=0.60,
        )
        self.assertFalse(kept)
        self.assertEqual(profile.facts, [])
        self.assertEqual(len(profile.empty_fields), 1)
        self.assertIn("below threshold", profile.empty_fields[0].reason)

    def test_fact_refuses_null_values(self) -> None:
        with self.assertRaises(ValueError):
            Fact(field="x", value=None, source_url="u", retrieved_at="t",
                 evidence="e", match_proof="registry", confidence=0.9)

    def test_whitespace_folded_quote_matching(self) -> None:
        page = "Org.nr:\n    986 587 098\n\n   Oslo"
        self.assertTrue(whitespace_contains(page, "986 587 098"))
        self.assertTrue(whitespace_contains(page, "986 587   098"))
        self.assertFalse(whitespace_contains(page, "986 587 099"))

    def test_profile_round_trip(self) -> None:
        import tempfile
        profile = Profile(org_number="986587098", legal_name="2CARE BPA AS",
                          run_date="2026-10-07", run_id="r")
        profile.add(Fact(field="legal_form", value="AS", source_url="u", retrieved_at="t",
                         evidence="e", match_proof="registry", confidence=1.0))
        profile.empty_fields.append(SkippedField("revenue_latest", "no filing"))
        with tempfile.TemporaryDirectory() as tmp:
            path = profile.write(Path(tmp))
            loaded = Profile.read(path)
        self.assertEqual(loaded.org_number, "986587098")
        self.assertEqual(loaded.value("legal_form"), "AS")
        self.assertEqual(len(loaded.empty_fields), 1)


class TestEnvelope(unittest.TestCase):
    """The envelope is the execution contract: one output per input, honestly."""

    def test_statuses_are_the_six_contract_states(self) -> None:
        self.assertEqual(
            STATUSES,
            ("available", "not_available", "not_applicable", "ambiguous",
             "blocked", "failed"),
        )

    def test_unknown_status_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Envelope(orgnr="986587098", status="maybe", reason="x")

    def test_available_may_carry_facts(self) -> None:
        env = Envelope(
            orgnr="986587098", status="available", reason="found",
            facts=[{"field": "legal_name", "value": "2CARE BPA AS"}],
        )
        env.validate()  # must not raise
        self.assertEqual(env.fact_count, 1)

    def test_ambiguous_may_carry_facts(self) -> None:
        """The code permits ``ambiguous`` to carry the facts it did resolve."""
        env = Envelope(
            orgnr="986587098", status="ambiguous", reason="identity not pinned",
            facts=[{"field": "legal_name", "value": "2CARE BPA AS"}],
        )
        env.validate()

    def test_non_fact_status_must_not_carry_facts(self) -> None:
        """A gap must be published as a gap, never padded with guesses."""
        for bad in ("not_available", "not_applicable", "blocked", "failed"):
            with self.subTest(status=bad):
                with self.assertRaises(ValueError):
                    Envelope(
                        orgnr="986587098", status=bad, reason="nothing here",
                        facts=[{"field": "legal_name", "value": "guess"}],
                    ).validate()

    def test_every_envelope_needs_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            Envelope(orgnr="986587098", status="not_available", reason="   ").validate()

    def test_jsonl_round_trip_is_one_line_per_envelope(self) -> None:
        import tempfile
        envs = [
            Envelope(orgnr="986587098", status="available", reason="found"),
            Envelope(orgnr="923609016", status="not_available", reason="no data"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = envelopes_to_jsonl(envs, Path(tmp) / "out.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["orgnr"], "986587098")
        self.assertEqual(first["status"], "available")
        self.assertIn("fact_count", first)
        # validate() ran for every line on write, so re-parsing is safe
        self.assertEqual(json.loads(lines[1])["status"], "not_available")

    def test_one_envelope_per_input_even_when_a_company_fails(self) -> None:
        """The execution contract: len(outputs) == len(inputs), always.

        A crashed company, a checksum-rejected number and a clean company all
        must come back as envelopes. This is the invariant the evaluator checks
        first, so it is asserted directly.
        """
        import tempfile

        from pipeline import CompanyResult, RunReport, Settings, build_envelopes  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(profiles_dir=Path(tmp), runs_dir=Path(tmp))
            reporter = RunReport(run_id="t", started_at="t", companies_requested=3)
            results = [
                CompanyResult(org_number="986587098", ok=True, facts=4, legal_name="X"),
                CompanyResult(org_number="923609016", error="boom"),           # crashed
                CompanyResult(org_number="974760673", error="rejected by checksum"),
            ]
            envelopes = build_envelopes(results, settings=settings, reporter=reporter)

        self.assertEqual(len(envelopes), len(results))
        self.assertEqual(
            {e.orgnr for e in envelopes}, {r.org_number for r in results}
        )
        for env in envelopes:
            self.assertIn(env.status, STATUSES)
            self.assertTrue(env.reason.strip())  # every envelope explains itself


class TestCoverage(unittest.TestCase):
    """People and locations come from the same keyless register; test the join."""

    SRC = "https://data.brreg.no/enhetsregisteret/api/enheter/986587098"
    AT = "2026-10-07T00:00:00Z"

    def _role_groups(self) -> list[dict[str, Any]]:
        return [{"roller": [
            {"type": {"kode": "DAGL"},
             "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}}},
            {"type": {"kode": "LEDE"},
             "person": {"navn": {"fornavn": "Kari", "etternavn": "Hansen"}}},
            {"type": {"kode": "MEDL"},
             "person": {"navn": {"fornavn": "Ola", "etternavn": "Nordmann"}}},
            {"type": {"kode": "MEDL"},
             "person": {"navn": {"fornavn": "Per", "etternavn": "Olsen"}}},
            {"type": {"kode": "REVI"},
             "person": {"navn": {"fornavn": "Revisor", "etternavn": "Firma"}}},
            # unknown code: must be ignored, not published
            {"type": {"kode": "ZZZ"},
             "person": {"navn": {"fornavn": "Skip", "etternavn": "Me"}}},
            # deregistered holder: history, not current coverage
            {"type": {"kode": "DAGL"},
             "person": {"navn": {"fornavn": "Former", "etternavn": "Ceo"}},
             "avregistrert": True},
        ]}]

    def test_people_facts_maps_roles(self) -> None:
        facts = people_facts(
            self._role_groups(), number="986587098",
            retrieved_at=self.AT, source_url=self.SRC,
        )
        by_field = {f.field: f.value for f in facts}
        self.assertEqual(by_field["ceo"], "Anders Opedal")
        self.assertEqual(by_field["board_chair"], "Kari Hansen")
        self.assertEqual(by_field["board_members"], "Ola Nordmann, Per Olsen")
        self.assertEqual(by_field["auditor"], "Revisor Firma")
        # every published person fact cites the register as proof
        for f in facts:
            self.assertEqual(f.match_proof, "registry")
            self.assertEqual(f.layer, "registry")

    def test_people_facts_skips_noise_and_deregistered(self) -> None:
        facts = people_facts(
            self._role_groups(), number="986587098",
            retrieved_at=self.AT, source_url=self.SRC,
        )
        fields = {f.field for f in facts}
        self.assertNotIn("ZZZ", fields)
        # the deregistered DAGL must not appear as the current ceo
        ceo = next(f for f in facts if f.field == "ceo")
        self.assertEqual(ceo.value, "Anders Opedal")

    def test_location_count_is_one_with_no_sub_units(self) -> None:
        facts = location_facts([], number="986587098",
                                retrieved_at=self.AT, source_url=self.SRC)
        by_field = {f.field: f.value for f in facts}
        self.assertEqual(by_field["location_count"], 1)
        # a single head office is not padded into a "business locations" list
        self.assertNotIn("business_locations", by_field)

    def test_location_facts_lists_sub_units(self) -> None:
        subs = [{
            "beliggenhetsadresse": {
                "adresse": ["Storgata 1"], "postnummer": "0155",
                "poststed": "OSLO",
            }
        }, {
            "beliggenhetsadresse": {
                "adresse": ["Strandveien 2"], "postnummer": "4005",
                "poststed": "STAVANGER",
            }
        }]
        facts = location_facts(subs, number="986587098",
                               retrieved_at=self.AT, source_url=self.SRC)
        by_field = {f.field: f.value for f in facts}
        # two sub-units + the head office
        self.assertEqual(by_field["location_count"], 3)
        self.assertIn("business_locations", by_field)
        self.assertIn("Storgata 1, 0155 OSLO", by_field["business_locations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
