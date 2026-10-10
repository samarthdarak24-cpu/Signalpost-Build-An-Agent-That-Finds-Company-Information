# Task #48 — Improve Web-Only Coverage Safely: Final Report

**Status:** Implementation complete. The query-strategy fix is correct, unit-tested and
regression-clean. The *live coverage number did not move on the corpus tested*, and this report
explains exactly why, with evidence — it is **not** an agent-logic failure.

---

## 1. Files changed and why

| File | Change | Why |
|---|---|---|
| `src/sitefinder.py` | 3 edits (below) | Make web discovery surface the company's *own* site instead of directories; add a hard query ceiling; fix a real `empty`→`failed` misclassification |
| `tests/test_web_discovery.py` | **NEW** — 12 deterministic tests | Phase 3 regression coverage for all 10 required scenarios |
| `runs/audit/t48_isolated_run.py` | **NEW** — eval driver | Run the post-fix pipeline web-enabled onto an **isolated** corpus (never `profiles/`) and score it |
| `runs/audit/t48_probe.py` | **NEW** — read-only probe | Determine the correct query shape empirically before changing code |
| `runs/audit/t48_baseline_eval.json` | **NEW** — frozen baseline | The "before" numbers, for an apples-to-apples comparison |
| `.gitignore` | **NEW** | Submission prep: exclude caches/logs, keep `profiles/` |

No changes were made to: `gate.py`, `normalize.py` (directory deny-list), identity matching,
provenance fields, the candidate scorer's weights, or the budget accountant.

---

## 2. Exact implementation changes (`src/sitefinder.py`)

### 2.1 `SiteFinder.__init__` — strict query ceiling
```python
def __init__(self, client=None, *, brave_key=None, keyless=True, max_queries=4):
    ...
    self.max_queries = max(0, int(max_queries))   # 0 disables search entirely
```
Bounds how many distinct queries one company can issue, so query expansion can never
breach the 2,000-request budget.

### 2.2 `from_search` — name-led queries, identifier last
```python
raw_queries = [
    name,                                      # bare name -> official site is top hit
    f"{name} hjemmeside",                      # explicit "website" intent
    (f"{name} {city}" if city else None),      # name + locality disambiguates
    f'"{name}" "{org.display}"',               # identifier query (last resort)
    f"{name} organisasjonsnummer {org.value}", # number query (last resort)
]
# dedupe, then cap by self.max_queries
```
The old order led with the org-number query, which is **optimised to return directory pages** —
and the ownership gate correctly rejects every one of them. The new order leads with the name.

### 2.3 `_search_keyless` — real bug fix (empty vs failed)
```python
error = outcome.error or ""
if "throttled" in error or error.endswith("HTTP 429"):
    return SearchAttempt(query=query, engine=engine, status="failed", error=error)
if error and error != "no engine returned results":
    return SearchAttempt(query=query, engine=engine, status="failed", error=error)
return SearchAttempt(query=query, engine=engine, status="empty", error="")
```
Previously a genuine empty result (sentinel `"no engine returned results"`) was classified as
`failed`. Now only throttling/hard errors are `failed`, and a clean zero-hit answer is `empty`.
This **strengthens** the Task #43 contract (`ok`/`empty`/`skipped`/`failed` stay distinct).

---

## 3. Commands and results

### 3.1 New regression suite — 12/12 PASS
```
python -m unittest tests.test_web_discovery -v      →  Ran 12 tests ... OK
```
Covers: official-site discovery, directory-only rejection, wrong-org rejection, empty≠failed,
provider-timeout→failed, duplicate-URL dedupe, missing-location, identifier-query-is-last-resort,
`max_queries` cap, budget-exhausted→skipped with 0 used, budgeted-exactly-once (mock HTTP server),
carry-forward preservation of a non-excluded fact.

### 3.2 Existing regression suites — no regressions
```
tests.test_search_failure + tests.test_budget + tests.test_web_discovery  →  60 tests, OK
tests.test_resumability   →  14 tests, OK
tests.test_run_report     →  13 tests, OK (1 skipped)
tests.test_evaluate       →  13 tests, OK
```

### 3.3 Frozen baseline (unchanged committed profiles)
```
python -m src.evaluate --n 40 --seed 20261010 --out runs/audit/t48_baseline_eval.json
```
```
field coverage              100.0%   (446/446 checkable slots)
supported-fact precision    100.0%   (944/944 facts)
identity accuracy           100.0%   (40/40)
wrong-company contamination 0
web-only coverage             0.0%   (0/38)
requests used               0
```

### 3.4 Isolated web-enabled run (same seed, temp corpus, production budget)
```
python runs/audit/t48_isolated_run.py 8
```
```
companies 8 · identity 100% · contamination 0 · web-only 0/4
SEARCH OUTCOMES: {'ok': 0, 'empty': 0, 'skipped': 0, 'failed': 8}
```
Per-profile error: `website discovery incomplete: mojeek: HTTP 403` /
`mojeek: throttled (anti-bot challenge)`.

### 3.5 Deterministic proof the fix works (cached DDG, no throttling dependence)
```
python runs/audit/t48_probe.py
```
```
EQUINOR ASA                     -> ok, duckduckgo, equinor.com (top hits)
EQUINOR ASA hjemmeside          -> ok, duckduckgo, equinor.com
EQUINOR ASA offisiell nettside  -> ok, duckduckgo, equinor.com
EQUINOR ASA STAVANGER           -> failed, mojeek: throttled
EQUINOR ASA kontakt             -> failed, mojeek: throttled
```

---

## 4. Before/after coverage & safety metrics

| Metric | Before (baseline) | After (isolated web run) | Verdict |
|---|---|---|---|
| Web-only coverage | 0/38 (0.0%) | 0/4 (0.0%) | **No change on this corpus** |
| Identity accuracy | 100% | 100% | Preserved |
| Wrong-company contamination | 0 | 0 | Preserved |
| Supported-fact precision | 100% | 100% | Preserved |
| Directory rejection | strict | strict | Unchanged |
| Task #43 ok/empty/failed distinct | yes | yes (bug fixed) | Strengthened |

**Honest reading:** the coverage number did not improve on the corpus tested. Two structural
reasons, both verified:

1. **The register already supplies the data.** For the n=8 sample, only **3** register-silent
   web slots existed in total (`00NATION` email+phone; `BJØRGAN SETERVEG` website; `AKKSANG`
   email). There was almost nothing for the web layer to *add*.
2. **Keyless engines are currently throttling.** With `cache=False`, *every* engine returned
   0 hits. No live coverage gain can be measured while no engine answers.

The fix itself is proven by §3.5: a name-led query surfaces the company's official site, while
the old org-number query returned directories.

---

## 5. Remaining limitations

- **Live coverage is engine-availability-bound.** The keyless stack (DuckDuckGo/Mojeek/Bing)
  is intermittently throttled; measurement is not reproducible day-to-day. A configured
  `BRAVE_API_KEY` would remove this variability entirely.
- **Register-silent slots are rare in the sample.** Most companies have `hjemmeside` in the
  register, so the web layer's opportunity set is small on this corpus.
- **`requests_used` in `evaluate.py` is structurally 0** — it scores on-disk profiles and never
  runs the pipeline. The real request count comes from the run report, not the evaluator.
- **Live end-to-end coverage improvement is not demonstrated** on this corpus. Claiming one
  would be unsupported by the evidence.

---

## 6. Unexpected regressions

None. All suites that passed before still pass. Two *improvements* surfaced:
- The `empty`/`failed` misclassification in `_search_keyless` was a genuine latent bug, now fixed
  and covered by a test.
- No double-counting of search requests was introduced: `_search_keyless` uses `budget.allow()`
  (a pure check), so each engine request is counted exactly once inside `liveweb.search_web`.

---

## 7. Submission status (answer to "is everything done?")

| Requirement | State |
|---|---|
| ≥1,000 company profiles | ✅ **3,458** profiles on disk |
| One command to run | ✅ `python run.py run ORGNR_FILE=orgnrs.txt` |
| Model/API details | ✅ `docs/SUBMISSION_MANIFEST.md` |
| Expected run costs | ✅ keyless = $0.00 (`docs/COST_REPORT.md`) |
| Repository link | ❌ **missing** — no remote configured |
| Exact commit hash | ❌ **missing** — repo has **0 commits**, everything untracked |
| Contact details | ❌ **missing** — `_fill in_` in the manifest |

Task #48 does **not** block submission. The submission blockers are the three unchecked items
above (repo URL, commit hash, contact).
