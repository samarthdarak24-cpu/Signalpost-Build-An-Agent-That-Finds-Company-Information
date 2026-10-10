# Task #47 — Why web-only coverage was 0.0% while keyless search worked

**Date:** 2026-10-10
**Status:** Root cause found, documented, and the misattribution risk it exposed is fixed.
**Script:** `runs/audit/prove_two_search_paths.py` (re-runnable, no keys required).

---

## 1. The apparent contradiction

Two observations were both true at the same time:

| Observation | Where it was measured |
|---|---|
| Web-only coverage **0.0%** (0 of 96 companies) | `python run.py evaluate` — the batch harness |
| Keyless search **returns results** (10 hits, no key) | `liveweb.search_web(...)` — the live agent |

The contradiction dissolves once you notice they are **different code paths**.

## 2. Root cause: two paths, two engine stacks

| | Path A — batch website discovery | Path B — keyless live search |
|---|---|---|
| Module | `src/sitefinder.py` | `src/liveweb.py` |
| Function | `SiteFinder.search_attempt()` | `liveweb.search_web()` |
| Engines | **Brave only** | **`duckduckgo`, `mojeek`, `bing`** (+ `wikipedia`) |
| Key required | **Yes** — `BRAVE_API_KEY` | **No** |
| No key → | `status="skipped"`, 0 results | real results |

`sitefinder.py` declared exactly one endpoint:

```python
BASE = "https://api.search.brave.com/res/v1/web/search"
```

and its entry point bailed immediately when no key was present:

```python
if not self.brave_key:
    return SearchAttempt(query=query, status="skipped",
        error="no BRAVE_API_KEY configured, so no search was performed")
```

`liveweb.py`, by contrast, declared a keyless chain:

```python
SEARCH_ENGINES = (
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={q}&kl=no-no"),
    ("mojeek",     "https://www.mojeek.com/search?q={q}"),
    ("bing",       "https://www.bing.com/search?q={q}&setlang=nb&cc=NO"),
)
```

**The batch pipeline's website discovery never used `liveweb`.** With no
`BRAVE_API_KEY` set — which is the submission's keyless configuration — every
website lookup returned `skipped`, so no company could gain a website fact beyond
what the register already carried. 0.0% web-only coverage is the *correct*
measurement of a Brave-gated path with no key.

`grep` confirms the isolation: `liveweb` is imported by `liveagent.py` (the live
CLI path) but **not** by `pipeline.py`, which imported only `SiteFinder`.

## 3. The fix — and the bug it exposed

`SiteFinder.search_attempt` now splits into `_search_brave` (paid, cost-reserved,
unchanged) and `_search_keyless` (the `liveweb` chain, charged against the
*request* budget). Brave is tried first when a key exists; the keyless chain
covers the no-key case and also acts as a fallback when Brave is refused or fails.
`SearchAttempt.engine` now records which engine actually answered, and
`Candidate.origin` inherits it (previously hard-coded `"brave"`).

**Building the fallback immediately exposed a real misattribution bug.** Running
the fixed path on Norsk Hydro ASA (register has no `hjemmeside`):

```
duckduckgo  0.85  https://visverdi.no/selskap/norsk-hydro-asa-914778271  ACCEPTED
```

That is a **directory page**, and the ownership gate accepted it with
`orgnr_on_page` confidence **0.96** — because the directory *verbatim displays the
organisation number* `914 778 271`. The gate's checksum rule could not distinguish
"the company published its number" from "a directory quoted it", and the deny
list was applied only as a *pre-filter*, never inside the gate.

Left unfixed, improving web-only coverage this way would have **published
directory pages as company websites** — a fabricated ownership claim, the exact
failure mode the brief prohibits.

### The fix

A single canonical directory list now lives in `normalize.DIRECTORY_HOSTS`, with
`is_directory_host()` used by all three layers (`sitefinder` pre-filter,
`liveweb` ranker, `gate` post-fetch). The gate grew a new verdict,
`rejected_directory_host`, which runs **before** the number-on-page rule so a
quoted number can never masquerade as ownership.

After the fix, the same Norsk Hydro run yields **no candidates and no fact** — a
correct abstention.

## 4. Honest conclusion — and why coverage did *not* jump

The keyless fallback alone does **not** raise web-only coverage, and pretending
otherwise would be gaming the metric. The reason is structural:

The batch query is `"Name" organisasjonsnummer NNNNNNNNN`. That query is
*literally optimised to return directory pages*, because directories are the
pages that index a company by its org number. A company's own homepage rarely
writes its org number in its title. Observed results for two real companies:

```
Borregaard AS -> 20thdata.com, nor47business.com, 21st.ai, firmadatabasen.no,
                 godkjentfirma.no, byggsikt.no, proff.no, virksomhet.brreg.no, yra.no
Tine SA       -> northdata.com, sokfirma.no, bestillelektriker.no, forvalt.no,
                 1890.no, proff.no, virksomhet.brreg.no
```

**Every result is a directory. Not one is an owned site.** The gate correctly
rejects them all. So the fallback is a *correctness* improvement (the pipeline
now genuinely looks, and reports `ok`/`empty`/`failed` honestly instead of
`skipped`), not yet a *coverage* improvement.

The real lever is the **query shape**, not the engine. Task #48 (improve
web-only discovery) should therefore pursue owned-site-targeted queries — e.g.
bare name, name + `hjemmeside`, or name + city — rather than name + org number,
and keep the gate as the sole authority on ownership.

## 5. Secondary finding: throttling is real and correctly classified

During this investigation the keyless engines began returning anti-bot
challenges. That is a **failure to look**, not an empty web, and it is classified
as such:

```
engines_tried: ['duckduckgo', 'mojeek', 'bing']
error        : mojeek: throttled (anti-bot challenge)
```

`_search_keyless` maps this to `status="failed"`; a genuine zero-result answer
from a responsive engine maps to `status="empty", error=""`. The two never share
a representation (this is the Task #43 contract, now extended to the keyless path).

## 6. Evidence limits

* The claim "every result is a directory" is from two spot-checked companies, not
  the full corpus. A systematic measurement belongs in Task #48.
* The keyless engines are third-party HTML scrapers subject to throttling and
  markup changes; the 0.0% → *correct abstention* result is stable, but any
  future coverage number depends on live engine availability.
* No paid provider was added and no key was required to reach these conclusions.
