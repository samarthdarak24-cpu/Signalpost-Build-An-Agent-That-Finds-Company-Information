# Signalpost — Submission Manifest

Everything an evaluator needs to run and audit the agent: models, APIs, licenses,
authentication, expected usage and cost, plus the exact commands.

## How to run (clean machine)

The core agent is **Python 3 standard library only** — nothing to install.

```bash
python -m venv .venv && . .venv/bin/activate      # optional; stdlib needs no venv
python run.py setup                                # build the local registry mirror (keyless)
python run.py seed-universe UNIVERSE=financial-filer-master-2025.jsonl
                                                   # seed the mirror from the public universe
python run.py run ORGNR_FILE=orgnrs.txt            # research every number, write one envelope each
python run.py score                                # score against the golden set
python run.py web                                  # serve the console (web-next) on :8787
```

`ORGNR_FILE` accepts the evaluator's batch as **either** a plain one-number-per-line
file **or** the public-universe JSONL (`financial-filer-master-2025.jsonl`); the
format is auto-detected. `seed-universe` loads the universe into the local mirror so
the register layer answers offline; a company that is not mirrored is still fetched
live from the register, so unseen companies are first-class inputs.

`make <target>` is a thin wrapper over `run.py`, so both spellings are identical
where `make` exists.

- **Install step:** none for the core (`requirements.txt` is intentionally empty).
- **Optional live layer:** `pip install -r requirements-live.txt` (pinned) enables
  the FastAPI + SSE bridge (`src/live_api.py`). The core never imports it.
- **Input contract:** `ORGNR_FILE` — one organisation number per line, `#` comments
  and blank lines allowed; numbers are normalised and de-duplicated.
- **Output contract:** exactly one terminal envelope per input, appended to
  `runs/envelopes.jsonl` (see `docs/SMOKE_TEST_REPORT.md`).

## Models (Anthropic)

| Model | Purpose | When it is called |
|---|---|---|
| `claude-haiku-4-5-20251001` | Gap filling — extract one missing field and return `value` + verbatim `quote` | Only when a field is still empty after all deterministic layers, and only on a page that already passed the ownership gate |
| `claude-sonnet-5-5` | Conflict resolution — arbitrate between two plausible values for the same field | Rarely; capped per profile |

Both are **optional**. With no `ANTHROPIC_API_KEY` the agent runs fully and
degrades gracefully (the LLM layer is skipped, never faked).

## External APIs / data sources

| Component | Provider | Purpose | Auth | License / terms |
|---|---|---|---|---|
| Enhetsregisteret | Brønnøysundregistrene | Company record, roles, sub-units, change feed | None (keyless) | NLOD 2.0 |
| Regnskapsregisteret | Brønnøysundregistrene | Annual accounts (revenue, result, assets, equity, debt, period) | None (keyless) | NLOD 2.0 |
| GLEIF | GLEIF | LEI identifiers + parent relationship | None (keyless) | GLEIF open data |
| Wikidata (SPARQL) | Wikimedia | Cross-identifiers via P2338, official website P856 | None (keyless) | CC0 |
| Brave Search API | Brave | Website discovery **only when the register has no URL** | `BRAVE_API_KEY` (optional) | Commercial API |
| Company websites | the companies | Contact details, jobs, public activity — read only after the ownership gate passes | None | robots.txt respected |

No crawler framework is used: fetching is `urllib` behind a polite, cached,
budget-aware client (`src/net.py`) that respects `robots.txt`.

## Pricing used for the cost model

| Item | Rate |
|---|---|
| Claude Haiku 4.5 | $1.00 / M input tokens · $5.00 / M output tokens |
| Claude Sonnet 5.5 | $3.00 / M input tokens · $15.00 / M output tokens |
| Brave Search query | $0.005 / query |

The **keyless path** (registry + financials + gate-verified web, no LLM, no search)
costs **$0.00**. Measured figures are in `docs/COST_REPORT.md`.

## Licenses / provenance

- Code: see `LICENSE`.
- Register data is reused under **NLOD 2.0** with attribution — see `docs/DATA_SOURCES.md`.
- Every published fact carries its own `source_url`, `retrieved_at`, `evidence`,
  `match_proof` and `confidence`, so the provenance of any value is auditable.

## Measured results (100-company smoke test on the real public universe)

Run on the first 100 records of `financial-filer-master-2025.jsonl`, keyless
(no LLM, no search), 8 workers:

| Metric | Value |
|---|---|
| Input companies | 100 |
| Output envelopes | 100 (0 missing, 0 duplicate) |
| Status | 100 `available` |
| Facts extracted | 3,074 (all evidence-backed) |
| Runtime | 310 s (~3.1 s/company) |
| Requests | 206 |
| External API cost | **$0.00** |

Domain recall (companies % / facts %): company record 100/79 · financials
100/98 · people & locations 100/50 · website 70/20 · hiring & activity 2/1.

## Submission checklist

- [x] One command to run (`python run.py run ORGNR_FILE=…`)
- [x] Accepts an evaluator-supplied file of unseen companies
- [x] Exactly one terminal envelope per input (asserted in tests + smoke report)
- [x] Six terminal states: available / not_available / not_applicable / ambiguous / blocked / failed
- [x] Pinned optional dependencies (`requirements-live.txt`)
- [x] 100-company smoke test (`docs/SMOKE_TEST_REPORT.md`)
- [x] Measured cost & runtime (`docs/COST_REPORT.md`)
- [x] Recall gap report (`docs/RECALL_GAPS.md`)
- [x] ≥1,000 company profiles — **profiles committed under `profiles/`**
- [ ] Repository URL — _push to your GitHub remote, then paste the URL here_
- [x] Exact commit hash — `1757a25833d481706ba3baefaae2aca7d912f741` (short `1757a25`)
- [ ] Contact details — _fill in: name / email_

## Delivering the repository

The first commit already exists locally. To publish:

```bash
git remote add origin <YOUR_GITHUB_REPO_URL>
git branch -M main
git push -u origin main
```

Then copy the repository URL and the commit hash above into the submission email
to `submit@builderr.ai`.

