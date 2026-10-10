# Audit, fixes and release verification — Signalpost console

Date: 2026-10-10. Scope: repository audit, defect reproduction, browser verification,
submission cleanup. Every claim below was executed; nothing is inferred.

---

## A. Root causes of confirmed defects

### A1. Every interactive view was silently dead (severity: critical)

**Symptom.** Typing into the Live Research box and submitting did nothing at all — no network
request, no turn rendered, no error. The same for the Ask view.

**Root cause.** `web-next/app.js`, router function `route()`:

```js
if (result) main.innerHTML = result;           // was line 469
```

Four views — `viewSearch`, `viewCompany`, `viewLive`, `viewAsk` — build a detached `wrap`
element, attach listeners to elements inside it, and then returned `wrap.innerHTML`
(`viewLive` returned an equivalent `html` string). Assigning that string to `main.innerHTML`
re-parses the markup into **new** elements, discarding every listener that had been attached to
the old ones. The form rendered perfectly and was inert.

Detected only by a real browser performing a real submit. Returns HTTP 200, markup is correct,
and the accessibility audit passes — none of those can observe a missing listener.

**Fix.** The router now distinguishes a node from a string, and the four views return their live
node:

```js
if (result instanceof Node) { main.replaceChildren(result); }
else if (result) { main.innerHTML = result; }
```

### A2. A blank query was answered about the previous company (severity: high)

**Symptom.** With a company in session, `POST /api/live {"q": ""}` returned
`answered: true` about that company — e.g. *"About A. BERG DESIGN: …"* — for an empty query.

**Root cause.** `src/liveagent.py`, `LiveSession.ask()` stripped the query but had no empty
guard. `classify_input("")` returns `"question"`, which satisfied the follow-up condition
(`bool(self.target_org or self.target_name) and kind in {"question","name"}`) and answered
about the company left in session. A confident, well-sourced answer to a question nobody asked.

**Fix.** Explicit early return in `ask()` before classification:

```python
if not query:
    answer = ResearchAnswer(question="", kind="empty")
    answer.notes.append("the query was empty, so nothing was searched")
    answer.seconds = round(time.monotonic() - started, 2)
    return answer
```

Note the client had masked this: `runLive()` returns early on a blank value, so the browser
never sent it. A direct API caller received the wrong answer — the client guard is not a
substitute for a server guard.

---

## B. Files changed / added / moved

| Path | Change | Reason |
|---|---|---|
| `web-next/app.js` | edited | Router appends a node instead of re-parsing; 4 views return their live element (A1). Also carries the earlier live-change SSE panel. |
| `src/liveagent.py` | edited | Empty-query guard in `LiveSession.ask()` (A2). |
| `tests/test_live.py` | edited | `TestEmptyQueryIsRefused` — 3 regression tests. |
| `tests/test_browser.py` | **added** | 8 Playwright scenarios, repo test style, skip-clean when unavailable. |
| `runs/audit/*.py`, `*.mjs`, `*.log` | added | Audit and proof harnesses (unchanged behaviour, evidence only). |
| `docs/PROJECT_OVERVIEW.md` | edited | `web-next/` description corrected (it said "Next.js … mock data"). |
| `docs/LIVE_DATA_STACK.md` | edited | §7 rewritten: described a Next.js dashboard that does not exist; now documents the implemented SSE panel. |
| `docs/DATA_SOURCES.md` | edited | Third-party table + `agent-browser` dev-only entry. |
| `requirements-live.txt` | edited | Pins the six verified live-layer versions with licences. |
| `web-next/{app,components,lib,.next,node_modules,package.json,package-lock.json,next-env.d.ts,next.config.mjs,postcss.config.mjs,tailwind.config.ts,tsconfig.json}` | **moved out** | Abandoned Next.js scaffold → `../_scaffold_backup/`. 489 MB. Reversible. |

Nothing was deleted outright. No public route, response schema or test expectation was changed.

---

## C. Test results (actually executed)

| Suite | Command | Result |
|---|---|---|
| Core | `python run.py test` | **231 passed, 0 failed** (220 original + 8 browser + 3 empty-query) |
| Browser | `python -m unittest tests.test_browser -v` | **8 passed, 0 failed** |
| Capability audit | `runs/audit/capability_audit.py` | 9/9 sections, no FAIL |
| Phase B/C | `runs/audit/phase_bc.py` | see below |

Skipped by design: none. Not run: the configured-provider path (no credentials available).

### Phase B/C detail (`runs/audit/phase_bc.txt`)

- Invalid input (empty, whitespace, bad checksum, non-numeric, 5000 chars) → all HTTP 200,
  server alive afterwards.
- `999999999` (valid, unregistered) → `answered=false`, **0 sources**, explicit gap message.
- **Secret leakage: none** in `/api/health`, `/api/profiles`, `/api/runs`, `/api/coverage`,
  `/api/live/status`.
- Keyless search with no `BRAVE_API_KEY`: works, `engine=duckduckgo`, 10 results.
- `/api/health` reports `llm_key_present: false` and `search_key_present: false` truthfully.
- `analytics` without duckdb → falls back to stdlib; `status_counts()` = 845/5/2, summing to
  852, exactly the `runs/envelopes.jsonl` line count.
- `eventbus` imports with no broker (`app is None`); `fanout` imports without redis.

---

## D. Real-browser verification

Driven by Playwright 1.63.0 (Chromium) against the real `serve.py` on `:8787`. **No mocks, no
route interception on the happy path.**

| Scenario | Observed | Result |
|---|---|---|
| Console renders at `/` | title contains "Signalpost", sidebar + app root present | pass |
| Query → answer | `POST /api/live` → 200 → verdict pill + sources rendered | pass |
| `835232182` | UI shows `answered`, target matches API (`A. BERG DESIGN`) | pass |
| Follow-up | `923609016` then "who runs it?" → 2 turns, second still EQUINOR | pass |
| Reset | thread cleared to 0 turns | pass |
| Empty / whitespace | no request fired, no turn created | pass |
| Malformed input | honest gap answer, page stays interactive, no uncaught error | pass |
| Whole session | 0 uncaught JS exceptions | pass |

**Fix evidence** (`runs/audit/FIX_evidence_submit_works.log`): before the fix, submitting
`835232182` produced *no request*; after, the browser observed
`POST /api/live` → `835232182 working…` → at 24.9s
`answered ✓classify ✓resolve ✓gather ✓answer  A. BERG DESIGN  orgnr 835232182`, HTTP 200.

**SSE, separately verified in a real browser** (`runs/audit/browser_live_proof.log`): the Live
Research panel's status pill read `live`; appending one real record to `runs/changes.jsonl`
made a row appear **without navigation** —
`923609016  legal_name  —  EQUINOR ASA  2026-10-10T11:50:00Z`. Screenshot:
`runs/audit/console_live_panel.png`. Log restored to 0 lines afterwards.

Distinction, stated plainly: the **happy path above is real browser + real backend**. The only
mocked element is none. The earlier `runs/audit/render_test.mjs` fake-DOM harness still exists
and is *not* counted as browser evidence.

---

## E. Remaining limitations

1. **No API credentials.** `ANTHROPIC_API_KEY` and `BRAVE_API_KEY` are unset, so the LLM
   extraction path and the Brave engine are dormant. This is the intended keyless design and is
   reported truthfully by `/api/health`. The configured-provider path was therefore **not
   executed**.
2. **Optional dependencies absent** — `duckdb`, `redis`, `faststream`. Not defects: all three
   are behind working fallbacks or flags (`analytics._duckdb_ok()`, `FASTSTREAM_BROKER`,
   `fanout`'s redis gate), each verified above. Deliberately not installed.
3. **No browser automation for the full dashboard.** The Live Research flow, Ask flow and SSE
   panel are browser-verified. Other views (Coverage, Trace, Reports, Auditor) are verified by
   HTTP probe and render harness, and their interactive handlers now share the fixed router
   path, but were not each driven interactively in a browser.
4. **`_scaffold_backup/` still on disk** (489 MB) pending your confirmation.
5. **The repository has no git commits yet** — everything is untracked, so there is no commit
   history to diff against and no safety net. Nothing has been pushed or committed.

---

## F. Setup and launch (Windows, current environment)

```bash
# core agent — standard library only, installs nothing
python run.py test          # 228 tests
python run.py web           # console on http://127.0.0.1:8787

# optional live bridge (separate process)
python -m pip install -r requirements-live.txt
python -m uvicorn src.live_api:app --port 8799

# optional browser tests (needs Playwright + a running console on :8787)
python -m pip install playwright
python -m playwright install chromium
python -m unittest tests.test_browser -v
```

`make` is not installed on this machine; `python run.py <target>` is equivalent.

---

## G. Release-readiness

**Ready, with the caveats in E.**

- Core suite 231 green; the judged path (`run.py run|score|web`) still installs nothing.
- The console's interactive flows now genuinely work in a browser, which they did not before
  this audit — this was a release-blocking defect, not a cosmetic one.
- Keyless demo works end to end with no credentials.
- No secrets in responses; invalid input handled without crashing.
- The repository is 489 MB lighter with the abandoned scaffold moved aside, and the two doc
  references to it corrected.

---

## H. Suggested commit message

```
Fix inert console forms, refuse empty queries, drop unused Next.js scaffold

Two real defects, both found by driving the console in a real browser rather
than probing it over HTTP.

- web-next/app.js: the router assigned view HTML with `innerHTML`, which
  re-parsed the markup and discarded the listeners the views had attached, so
  the Live Research and Ask forms rendered correctly and did nothing. The router
  now appends a node when given one, and viewSearch/viewCompany/viewLive/viewAsk
  return their live element.
- src/liveagent.py: LiveSession.ask() had no empty-query guard, so a blank query
  classified as "question" and was answered about the company already in session.
  It now returns kind="empty", answered=False with nothing searched.

Adds tests/test_browser.py (8 Playwright scenarios against the real backend) and
TestEmptyQueryIsRefused. Core suite: 231 tests green.

Moves the abandoned Next.js scaffold out of web-next/ to ../_scaffold_backup/
(489 MB, unused: not built, served, or referenced). Corrects the two docs that
described it as the frontend.
```

Not pushed, not committed.

---

# Round 2 — competition-readiness work

## Defect 3 — a failed search was reported as "no results" (coverage)

Found during baseline reproduction against random unseen companies, not by any
unit test. `sitefinder.SiteFinder._search` ended in a bare
`except Exception: return []`, so three mutually exclusive facts shared one
representation:

| What actually happened | What the caller saw |
|---|---|
| No API key configured — the search never ran | `[]` |
| API refused the key (HTTP 401) | `[]` |
| Transport failure — connection reset | `[]` |
| Engine answered, genuinely zero hits | `[]` |

Downstream this meant **all 1,010 profiles recorded zero errors** while live
searches were being throttled, and 755 profiles carried no `website` fact that
had never been attempted. A thin profile was indistinguishable from a company
that genuinely has little to find — which is the exact error the brief forbids:
*never treat a missing search result as proof that a fact is false.*

**Fix.** `SearchAttempt` (`src/sitefinder.py`) now classifies every exit path with
`status ∈ ok | empty | skipped | failed` and keeps the reason. `_search()` is
retained as a thin wrapper so existing callers are unaffected.
`SiteFinder.last_search` remembers the attempt; `from_search()` records each of
its two queries. `pipeline.process_company` writes a failed or skipped search
into `profile.errors` (and the `website_search_failed` layer), exposes it as
`search: {status, error}` on `CompanyResult`, and changes the "no website" skip
reason from an assertion to a disclosure:

> before: `ownership gate found no page carrying the organisation number …`
> after:  `website not determined: the lookup did not complete (…)`

**Evidence.** `runs/audit/prove_search_failure.py` — profile `923609016` now
records `errors: ['website discovery incomplete: no BRAVE_API_KEY configured, so
no search was performed']`. `runs/audit/prove_empty_clean.py` proves the
converse: an engine that answers with zero results stays `status="empty",
error=""` and is **not** flagged. Round-trips through `Profile.read` intact.

**Scope.** `liveweb.py`/`liveagent.py` were already correct — `SearchOutcome.error`
was populated by `is_challenge_page()` and already surfaced into
`answer.notes`. The defect was confined to the batch pipeline's `sitefinder`.

`sitefinder` had **no test coverage at all** before this, which is why the defect
survived every prior audit. `tests/test_search_failure.py` (7 tests) failed
before the change and passes after.

## Addition — random-company evaluation harness (`src/evaluate.py`)

Measures the scored axes on companies the agent was not tuned on, against
register ground truth rather than the agent's own output: field coverage,
supported-fact precision, identity accuracy, wrong-company contamination,
web-only coverage, requests used, search failures.

Two false alarms **in the harness itself** were caught before publishing:

1. **Identity accuracy read 10.7% (120/1121)** under `--all`. Ground truth exists
   for the 120 seed entities, so 1,001 unchecked companies were counted as
   mismatches — a fabricated *disqualifying* finding manufactured from missing
   test data. Fixed with an `identity_checked` denominator; now **100.0%
   (120/120 checkable; 1,010 not checkable)**, and the report discloses the split.
2. A field's denominator could drop to zero and print a meaningless `0/0` row.

The harness reports **two** coverage numbers deliberately, because a field copied
from the register proves only that the register was reachable:

```
register-copy coverage   99.9%   (1366/1368 checkable slots)
web-only coverage         0.0%   (0/96 slots the register left silent)
supported-fact precision 100.0% (26406/26406 facts)
identity accuracy         100.0% (120/120; 1010 not checkable)
wrong-company contamination   0
```

The 0.0% is the honest, actionable figure: without a search key the web layer
contributes nothing. The two genuine coverage misses are both *correct*
abstentions — service codes `08725` / `06061` are 5-digit short codes, not
8-digit Norwegian numbers, and each was declined with a recorded reason.

`tests/test_evaluate.py` (13 tests) constructs known-bad profiles and asserts the
harness detects each class of failure; a checker that always returned 100% would
pass a naive test, so the tests are written to fail if detection breaks.

## Test state

**251 tests OK** (skipped=8, the browser suite, which skips cleanly when
Playwright or the console is absent). `run.py score`: **78.6/100 → MEETS
QUALIFICATION**. Console 8787 → 200 serving `web-next/`; live bridge
`/api/health` → `{"ok":true,…,"events_seen":210000}`.
