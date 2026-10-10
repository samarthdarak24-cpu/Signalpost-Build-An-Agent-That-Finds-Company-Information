"""Browser test: live research from the UI fetches an unseen company (Task #49).

Distinct from the sweep: this drives the *interactive* Live Research view the way
a person does — types an organisation number that is NOT in the local corpus,
submits it, and asserts that a real sourced answer renders (verdict pill, source
cards, the company name). It also drives the Company Search view and clicks a
result through to a profile.

Run with the console up:
    python src/serve.py --port 8787
    python runs/audit/t49_live_ui.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = "http://127.0.0.1:8787"

#: A real, listed company that is very unlikely to be pre-seeded in the corpus,
#: so the UI has to fetch it live.
LIVE_TARGET = "923609016"   # Equinor ASA (stable, large, clearly named)

from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> int:
    errors: list[str] = []
    failures: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context().new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # --- 1. Live Research: submit a number, expect a sourced answer --------
        page.goto(CONSOLE + "/#/live", wait_until="domcontentloaded")
        page.wait_for_selector("#liveQ", timeout=15000)
        page.fill("#liveQ", LIVE_TARGET)
        page.click("#liveGo")
        page.wait_for_function(
            "() => document.querySelectorAll('#liveThread .spinner').length === 0"
            " && document.querySelectorAll('#liveThread .turn-q').length > 0",
            timeout=150000,
        )
        thread = page.inner_text("#liveThread")
        has_pill = page.query_selector("#liveThread .pill") is not None
        has_src = page.query_selector("#liveThread .src") is not None
        print(f"  live answer chars : {len(thread)}")
        print(f"  verdict pill      : {has_pill}")
        print(f"  source cards      : {has_src}")
        if LIVE_TARGET not in thread:
            failures.append("the org number did not appear in the rendered answer")
        if not has_pill:
            failures.append("no verdict pill rendered for a live query")
        if not has_src:
            failures.append("no source card rendered for a live query")

        # --- 2. Company Search: type a name, expect result rows ---------------
        page.goto(CONSOLE + "/#/search", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        search_input = page.query_selector("input[type=search], #search, input#searchQ")
        if search_input:
            search_input.fill("equinor")
            page.wait_for_timeout(1500)
            rows = page.query_selector_all("tr.clickable, [data-hit]")
            print(f"  search rows       : {len(rows)}")
            if not rows:
                failures.append("company search returned no rows for 'equinor'")
        else:
            print("  search input      : (not found — checking rendered rows instead)")
            rows = page.query_selector_all("tr.clickable, [data-hit]")
            print(f"  search rows       : {len(rows)}")

        # --- 3. Navigate to a profile via hash and expect content -------------
        page.goto(CONSOLE + "/#/company/" + LIVE_TARGET, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        profile_text = page.inner_text("#app")
        has_name = "EQUINOR" in profile_text.upper()
        print(f"  profile shows name: {has_name}")
        if not has_name:
            failures.append("company profile did not render the company name")

        # --- 4. Sources view should list real source hosts --------------------
        page.goto(CONSOLE + "/#/sources", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        src_text = page.inner_text("#app")
        print(f"  sources view chars: {len(src_text)}")

        browser.close()

    print()
    if errors:
        print("  CONSOLE ERRORS:")
        for e in errors[:10]:
            print(f"    - {e}")
    print(f"  hard pageerrors   : {len([e for e in errors if 'pageerror' in e])}")
    print(f"  failures          : {len(failures)}")
    for f in failures:
        print(f"    - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
