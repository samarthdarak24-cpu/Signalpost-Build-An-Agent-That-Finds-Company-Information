"""Task #49 — HTTP surface tests for the console server (src/serve.py).

Verifies the endpoints the browser console depends on, including the regression
that a company with no profile on disk must answer 200 with a soft body (so the
UI renders "not found") rather than a 404 that surfaces as a browser console
error. Only read-only endpoints are exercised, on a real localhost socket.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import serve  # noqa: E402


class TestConsoleHttpSurface(unittest.TestCase):
    """Drive the real handler over a loopback socket."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        cls.server.verbose = False  # type: ignore[attr-defined]
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.status, r.read().decode("utf-8")

    def test_health_is_ok(self) -> None:
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))

    def test_missing_profile_answers_200_with_soft_body(self) -> None:
        """Regression (Task #49): an unresearched number must not 404.

        A 404 here throws in the frontend's strict ``api()`` and logs a console
        error; the console instead expects a soft body it can render as
        "not found".
        """
        status, body = self._get("/api/profile/999999999")
        self.assertEqual(status, 200, "a missing profile must not be a 4xx")
        data = json.loads(body)
        self.assertEqual(data.get("error"), "not found")
        self.assertEqual(data.get("org_number"), "999999999")

    def test_existing_profile_returns_the_document(self) -> None:
        # Pick any profile that exists on disk, if the corpus is present.
        profiles = sorted((ROOT / "profiles").glob("*.json"))
        if not profiles:
            self.skipTest("no profiles on disk")
        number = profiles[0].stem
        status, body = self._get(f"/api/profile/{number}")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(str(data.get("org_number")), number)
        self.assertIn("facts", data)

    def test_missing_trace_always_answers_json(self) -> None:
        status, body = self._get("/api/trace/999999999")
        self.assertIn(status, (200, 404))
        self.assertIn("error", body)

    def test_root_serves_the_console_shell(self) -> None:
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Signalpost", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
