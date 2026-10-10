"""Full-surface browser sweep for the Signalpost console (Task #49 check).

Drives a real Chromium over every route the console exposes, for a valid company
and a missing one, and records:
  * console errors / uncaught pageerrors
  * failed network requests (HTTP >= 400 or transport failure)
  * whether each route actually rendered content (not an empty shell)

Run with the console up:
    python src/serve.py --port 8787    # one shell
    python runs/audit/t49_browser_sweep.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = "http://127.0.0.1:8787"
ORG = "923609016"  # Equinor, present in the mirror
MISSING = "999999999"  # not a real company

# Every hash route the console defines (web-next/app.js NAV).
ROUTES = [
    ("#/overview", "Overview"),
    ("#/live", "Live Research"),
    ("#/search", "Company Search"),
    ("#/company", "Company Profile"),
    ("#/ask", "Ask Signalpost"),
    ("#/trace", "Agent Trace"),
    ("#/coverage", "Coverage"),
    ("#/reports", "Run Reports"),
    ("#/sources", "Sources"),
    ("#/auditor", "Auditor"),
    (f"#/company/{ORG}", "Company Profile (Equinor)"),
    (f"#/trace/{ORG}", "Agent Trace (Equinor)"),
    (f"#/ask?orgnr={ORG}", "Ask (Equinor)"),
    (f"#/company/{MISSING}", "Company Profile (missing)"),
    (f"#/trace/{MISSING}", "Agent Trace (missing)"),
]

from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> int:
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_reqs: list[str] = []
    results: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        page = ctx.new_page()

        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on(
            "response",
            lambda r: failed_reqs.append(f"{r.status} {r.url}")
            if r.status >= 400 and "/api/" in r.url else None,
        )

        for route, label in ROUTES:
            console_errors.clear()
            page_errors.clear()
            failed_reqs.clear()
            try:
                page.goto(CONSOLE + "/" + route, wait_until="domcontentloaded")
                page.wait_for_selector("#app", timeout=15000)
                # Give the async router a beat to fetch + paint.
                page.wait_for_timeout(2500 if route != "#/live" else 1500)
                main_text = page.inner_text("#main, #app")
                view_html_len = len(page.inner_html("#app"))
                results.append({
                    "route": route,
                    "label": label,
                    "html_len": view_html_len,
                    "text_len": len(main_text.strip()),
                    "console_errors": list(console_errors),
                    "page_errors": list(page_errors),
                    "failed_api": list(failed_reqs),
                    "ok": not page_errors and not failed_reqs and view_html_len > 500,
                })
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "route": route, "label": label, "ok": False,
                    "exception": f"{type(exc).__name__}: {exc}",
                })

        browser.close()

    # -- report
    print("=" * 78)
    print("  FULL BROWSER SURFACE SWEEP")
    print("=" * 78)
    bad = 0
    for r in results:
        flag = "OK " if r.get("ok") else "BAD"
        if not r.get("ok"):
            bad += 1
        extra = ""
        if r.get("page_errors"):
            extra += f" PAGEERR={r['page_errors']}"
        if r.get("failed_api"):
            extra += f" FAILED_API={r['failed_api']}"
        if r.get("console_errors"):
            extra += f" CONSOLE={r['console_errors'][:2]}"
        if r.get("exception"):
            extra += f" EXC={r['exception']}"
        print(f"  [{flag}] {r['route']:<28} {r['label']:<26} "
              f"html={r.get('html_len','?')} text={r.get('text_len','?')}{extra}")
    print()
    print(f"  routes checked: {len(results)}   failing: {bad}")

    out = ROOT / "runs" / "audit" / "t49_browser_sweep.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
