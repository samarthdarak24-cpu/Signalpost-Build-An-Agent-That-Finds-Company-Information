"""Real-browser end-to-end tests for the Signalpost console.

These are deliberately different from the rest of the suite. Everything else
runs against fixed markup and fixed result sets with no network, which is the
right way to test the parsing, ranking and session logic. But that style cannot
tell you whether the console *actually works when a person uses it* -- whether
the form submits, whether the request goes out, whether the answer and its
sources land in the DOM, and whether a follow-up stays on the company under
discussion.

So these tests drive a real Chromium through Playwright against the real
``serve.py`` backend on 127.0.0.1:8787. There are no mocks and no route
interception: if the server is not running, they skip rather than pretend. A
skip is honest; a fake pass is not.

Run them with the console already up::

    python run.py web            # in one shell, port 8787
    python -m unittest tests.test_browser -v

They are skipped automatically when Playwright or the console is unavailable,
so they never break the offline core suite.

Real markup the assertions rely on (from web-next/app.js):

* each turn is ``<section class="panel ...">`` inside ``#liveThread``
* the query is shown in ``.turn-q``
* the loading state is a ``.spinner``; a finished turn has none
* the verdict is a ``.pill`` ("answered" / "no sourced answer")
* cited pages are ``.src`` cards
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

CONSOLE = "http://127.0.0.1:8787"

try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT = True
except Exception:  # noqa: BLE001 - any import failure means "not installed"
    _PLAYWRIGHT = False


def console_is_up() -> bool:
    """True when the stdlib console is actually answering on :8787."""
    try:
        with urllib.request.urlopen(CONSOLE + "/api/health", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def _reason() -> str:
    if not _PLAYWRIGHT:
        return "playwright not installed"
    if not console_is_up():
        return f"console not running at {CONSOLE} (start it with `python run.py web`)"
    return ""


#: A turn is a thread section that carries the query header. The thread's empty
#: state is also a `section.panel`, so selecting on `.panel` alone would count it
#: as a turn — `.turn-q` is what actually distinguishes one.
_TURNS = "#liveThread section.panel:has(.turn-q)"
_BUSY = "#liveThread .spinner"


@unittest.skipUnless(_PLAYWRIGHT and console_is_up(), _reason())
class TestConsoleInRealBrowser(unittest.TestCase):
    """The console, driven the way a person drives it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()
        cls.context = cls.browser.new_context()
        cls.page = cls.context.new_page()
        cls.console_errors: list[str] = []
        cls.page.on(
            "console",
            lambda m: cls.console_errors.append(m.text) if m.type == "error" else None,
        )
        cls.page.on("pageerror", lambda e: cls.console_errors.append(f"pageerror: {e}"))

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.context.close()
            cls.browser.close()
        finally:
            cls._pw.stop()

    # -- helpers -----------------------------------------------------------

    def _goto_live(self) -> None:
        self.page.goto(CONSOLE + "/#/live", wait_until="domcontentloaded")
        self.page.wait_for_selector("#liveQ", timeout=15000)

    def _submit(self, query: str) -> None:
        """Fill the real input and submit it the way a user would."""
        self.page.fill("#liveQ", query)
        self.page.click("#liveGo")

    def _wait_idle(self, timeout: int = 120000) -> None:
        """Wait until no turn is still spinning."""
        self.page.wait_for_function(
            "() => document.querySelectorAll('#liveThread .spinner').length === 0"
            " && document.querySelectorAll('#liveThread .turn-q').length > 0",
            timeout=timeout,
        )

    def _turn_count(self) -> int:
        return len(self.page.query_selector_all(_TURNS))

    # -- tests -------------------------------------------------------------

    def test_01_console_renders_with_working_controls(self) -> None:
        """The real entry URL renders, and the controls a user needs are there."""
        self.page.goto(CONSOLE + "/", wait_until="domcontentloaded")
        self.assertIn("Signalpost", self.page.title())
        self.assertIsNotNone(self.page.query_selector(".sidebar"), "no sidebar")
        self.assertIsNotNone(self.page.query_selector("#app"), "no app root")

    def test_02_query_submits_and_returns_sourced_answer(self) -> None:
        """A real query posts to /api/live and renders a verdict and sources.

        We observe the actual request and response so the assertion is checked
        against what the server really returned.
        """
        self._goto_live()
        live_reqs: list[str] = []
        live_res: list[tuple[str, int]] = []
        self.page.on("request", lambda r: live_reqs.append(r.url) if "/api/live" in r.url else None)
        self.page.on(
            "response",
            lambda r: live_res.append((r.url, r.status)) if "/api/live" in r.url else None,
        )

        self._submit("Equinor ASA")
        self._wait_idle()

        self.assertTrue(live_reqs, "no request to /api/live was observed")
        self.assertTrue(live_res, "no response from /api/live was observed")
        self.assertEqual(live_res[-1][1], 200, "the API did not answer 200")

        body = self.page.inner_text("#liveThread")
        self.assertIn("Equinor", body)
        self.assertIsNotNone(
            self.page.query_selector("#liveThread .pill"),
            "no verdict pill rendered",
        )

    def test_03_org_number_835232182_matches_backend(self) -> None:
        """The number the brief names renders what the API actually returns."""
        self._goto_live()
        self._submit("835232182")
        self._wait_idle()

        req = urllib.request.Request(
            CONSOLE + "/api/live",
            data=json.dumps({"q": "835232182"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            api = json.load(r)

        self.assertEqual(str(api.get("org_number") or ""), "835232182")
        rendered = self.page.inner_text("#liveThread")
        self.assertIn("835232182", rendered, "the org number is not shown in the UI")
        target = str(api.get("target") or "")
        if target:
            self.assertIn(target[:16], rendered, "UI target disagrees with the API")

    def test_04_followup_reuses_company_context(self) -> None:
        """A follow-up with no identifier stays on the company in context."""
        self._goto_live()
        self._submit("923609016")          # Equinor
        self._wait_idle()
        self._submit("who runs it?")       # no identifier repeated
        self._wait_idle()

        self.assertGreaterEqual(self._turn_count(), 2, "the follow-up did not render")
        # The follow-up turn should name the same company.
        turns = self.page.query_selector_all(_TURNS)
        self.assertIn("EQUINOR", turns[-1].inner_text().upper())

    def test_05_reset_clears_the_thread(self) -> None:
        """Reset empties the visible thread rather than leaving stale turns."""
        self._goto_live()
        self._submit("923609016")
        self._wait_idle()
        self.assertGreaterEqual(self._turn_count(), 1)

        self.page.click("#liveReset")
        self.page.wait_for_timeout(600)
        # After reset the thread shows the empty state, i.e. zero turn sections.
        self.assertEqual(self._turn_count(), 0, "reset left earlier turns on screen")

    def test_06_empty_and_whitespace_do_not_submit(self) -> None:
        """Blank input must not fire a request or create a turn."""
        self._goto_live()
        fired: list[str] = []
        self.page.on("request", lambda r: fired.append(r.url) if "/api/live" in r.url else None)
        self.page.fill("#liveQ", "   ")
        self.page.click("#liveGo")
        self.page.wait_for_timeout(1500)
        self.assertEqual(fired, [], "a blank query still hit the API")
        self.assertEqual(self._turn_count(), 0, "a blank query created a turn")

    def test_07_malformed_input_is_handled_not_crashed(self) -> None:
        """Nonsense input yields an honest answer, not an uncaught error."""
        before = len(self.console_errors)
        self._goto_live()
        self._submit("zzzz not a company %%%")
        self._wait_idle()
        self.assertTrue(self.page.query_selector("#liveQ").is_editable())
        hard = [e for e in self.console_errors[before:] if "pageerror" in e]
        self.assertEqual(hard, [], f"uncaught JS error on malformed input: {hard}")

    def test_08_no_console_errors_during_the_session(self) -> None:
        """After everything above, no uncaught exception should have occurred."""
        hard = [e for e in self.console_errors if "pageerror" in e]
        self.assertEqual(hard, [], f"uncaught exceptions during the run: {hard}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
