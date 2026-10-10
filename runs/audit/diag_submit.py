"""Submit properly and watch: does the POST fire, does the thread fill?

Uses a longer observation window and checks the button state, so we can tell a
silent no-op click apart from a slow response.
"""
from playwright.sync_api import sync_playwright

CONSOLE = "http://127.0.0.1:8787"

with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_context().new_page()
    page.on("pageerror", lambda e: print(f"[pageerror] {e}"))
    page.on("request", lambda r: print(f"[req ] {r.method} {r.url}") if "/api/live" in r.url else None)
    page.on("response", lambda r: print(f"[resp] {r.status} {r.url}") if "/api/live" in r.url else None)

    page.goto(CONSOLE + "/#/live", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    print("thread BEFORE:", repr(page.evaluate("document.getElementById('liveThread').innerHTML.slice(0,150)")))

    page.fill("#liveQ", "835232182")
    print("input value   :", page.input_value("#liveQ"))
    print("btn disabled  :", page.is_disabled("#liveGo"))

    # Submit via the form's own submit path (what pressing Enter does).
    page.press("#liveQ", "Enter")
    print("submitted via Enter")

    for i in range(30):
        page.wait_for_timeout(2000)
        st = page.evaluate("""() => {
          const t = document.getElementById('liveThread');
          return {
            turns: t.querySelectorAll('.turn-q').length,
            spin: t.querySelectorAll('.spinner').length,
            pill: t.querySelectorAll('.pill').length,
            txt: t.innerText.replace(/\\s+/g,' ').slice(0,120)
          };
        }""")
        print(f"  t+{(i+1)*2:>3}s {st}")
        if st["turns"] and not st["spin"]:
            break

    b.close()
