# Signalpost Evidence Engine

Takes a Norwegian organisation number and returns a company profile: facts, source
links, retrieval dates, a verbatim supporting quote and a confidence score for every
value. Facts belong to the right company, or they are not published.

**Design rule: registry first, web second, LLM last.** Every fact carries evidence.
The agent leaves a field empty rather than guess.

```bash
python run.py setup                              # build the local registry mirror
python run.py seed-universe UNIVERSE=financial-filer-master-2025.jsonl
                                                 # seed the mirror from the public universe
python run.py run ORGNR_FILE=orgnrs.txt          # produce profiles, print the run report
python run.py lookup 995849364                   # research ONE number live
python run.py live "who runs Equinor?"           # live agent: any number, name, domain or question
python run.py score                              # score against the rubric
python run.py web                                # browse the profiles in the console UI
```

`make <target>` is a thin wrapper over `run.py`, so both spellings are identical.

**Live research agent.** `run.py live` accepts *anything* — an organisation
number, a company name, a bare domain, or a free-text question — classifies it,
fetches the freshest information (the register live for a number; the open web
plus Wikipedia for a name or question), and answers with sources. Add `--ask` to
keep asking follow-ups in one session; the company you first named stays in
context, so "who runs it?" and "and their finances?" resolve against it. The same
engine backs the **Live Research** panel in the web console and `POST /api/live`.

Three guardrails make the live layer trustworthy rather than merely functional,
and they are the ones to check first when extending it:

* **A wrong company is never substituted for the one asked about.** If a search
  for "Equinor ASA" surfaces a directory page listing Borregaard's organisation
  number, that number is *rejected unless the register confirms the name matches*
  — the mirror is checked first, the live profile second, and a mismatch is
  reported ("ignored register number 895623032 (BORREGAARD AS) — not 'Equinor
  ASA'") rather than quietly answered. See `_names_agree`, `_harvest_orgnr` and
  `_mirror_name` in `liveagent.py`.
* **A challenge page is never an empty result set.** An HTTP 202/429 or a captcha
  interstitial is reported as `throttled` and the next engine is tried, so a rate
  limit never looks like a fact about the world (`liveweb.is_challenge_page`).
* **A page's navigation is never quoted as its content.** The excerpt is the
  longest run of consecutive sentences that reads as prose, so a cookie footer or
  a WAF error ("Please consult with your administrator") is scored out and the
  reader descends to a page that actually describes the company
  (`_best_excerpt`, `_content_score`, `_is_chrome`).

**Input is the evaluator's file.** `ORGNR_FILE` accepts either a plain
one-number-per-line file or the universe JSONL (`organisation_number`, one record
per line); the format is detected automatically. The agent reads the batch, and an
unseen company is fetched live from the register rather than dropped — so it works
on companies it has never researched before.

---

## Why this is different in three ways

### 1. Proof-of-ownership gate

Norwegian businesses are legally required to display their name and organisation
number. That requirement is the cheapest reliable link between a company and a
website, so it is the gate. A page is accepted as belonging to a company only when a
**checksum-valid** organisation number appears on it — not merely the right name.

This kills the single most damaging error class in company intelligence: attaching a
website to the wrong company. Name collisions are common (`HAV AS` and `HAV 2CARE AS`
are the same entity at different times; dozens of companies share generic names), and
a name-only match is never treated as high confidence.

One fallback exists and is documented rather than hidden: name **plus postcode plus
city**, together, at a lower confidence of `0.74`. If the page shows a *different*
organisation number — the classic group-website trap — the page is rejected outright,
even when the name matches.

### 2. Evidence on every fact

```json
{
  "field": "email",
  "value": "bpa@2care.no",
  "source_url": "https://havas.no",
  "retrieved_at": "2026-10-07T13:31:00Z",
  "evidence": "Vi tilbyr skreddersydde BPA-ordninger ... bpa@2care.no",
  "match_proof": "orgnr_on_page",
  "confidence": 0.92,
  "layer": "web_rules",
  "method": "mailto link or role address in page text"
}
```

`match_proof` records *how the value was tied to this company*, which is the question
that actually matters. Values corroborated by two independent sources keep both
receipts in `sources[]` and gain confidence.

### 3. Delta engine

- **Registry**: the official change-event stream, applied as field-level patches.
  Every patch is logged with the field it touched, the old value and the new one.
- **Web**: a conditional GET (`If-None-Match` / `If-Modified-Since`) returns `304 Not
  Modified` in milliseconds. When the page did change, the content hash decides
  whether the *facts* moved or only the markup. Re-extraction happens only in the
  first case.
- **Expiry**: a page with no `ETag` and no `Last-Modified` can never answer 304, so
  web facts older than the expiry window are revalidated regardless.

---

## How the cascade works

```
input ──► validate ──► deterministic layer ──► web layer ──► trust layer
          modulus-11   registry · GLEIF        gate · rules    verify · explain
                       Wikidata · changes       LLM · delta     write profile
          free         free                     metered         evidence
```

Each layer runs **only when the cheaper layer above it could not answer**. A company
whose register record already carries a website, an email and a phone number never
touches the web layer at all.

| Step | Module | What it does | Cost |
|---|---|---|---|
| 1 | `validate.py` | nine digits, modulus-11 check digit, weights 3 2 7 6 5 4 3 2 | free |
| 2 | `registry.py` | local SQLite mirror of Enhetsregisteret; `lookup`, `search`, `sync_changes` | free |
| 3 | `enrich.py` | GLEIF LEI + parent, Wikidata cross-links, concurrent with timeouts | free |
| 4 | `sitefinder.py` | registry website first, else Brave Search with name **and number** | $0.005/query |
| 5 | `gate.py` | proof-of-ownership gate; abstains rather than guesses | free |
| 6 | `extract_rules.py` | JSON-LD, OpenGraph, `mailto:`/`tel:`, socials, address blocks | **free** |
| 7 | `extract_llm.py` | unresolved fields only; quote must exist on the page | per token |
| 8 | `verify.py` | cross-source checks, one arbitration call, otherwise drop | rare |
| 9 | `explain.py` | one line per fact, naming the proof type | free |
| 10 | `delta.py` | change log, content hashes, conditional GET, expiry | free |

### The gate's decision table

| Page shows | Verdict | `match_proof` | Confidence |
|---|---|---|---|
| A valid org number for this company | accepted | `orgnr_on_page` | 0.96 |
| A *different* valid org number | **rejected** | — | 0.00 |
| Name + postcode + city, no number | accepted, weaker | `name_postcode` | 0.74 |
| Name only | **rejected** | `name_only` | 0.35 |
| Under 60 characters of visible text | **rejected** | likely a block page | 0.00 |

Evidence quotes survive whitespace collapse, because a quote copied off a rendered
page almost never matches byte-for-byte.

---

## Repository layout

```
signalpost/
  README.md  Makefile  .env.example  orgnrs.txt
  src/
    validate.py       modulus-11 validation and org-number harvesting
    normalize.py      page text, name folding, postcodes, social links
    net.py            cached, courteous, budgeted HTTP  (not http.py — stdlib clash)
    registry.py       Brreg client, SQLite mirror, annual accounts
    enrich.py         GLEIF and Wikidata, concurrent
    sitefinder.py     registry website, else Brave Search
    gate.py           the proof-of-ownership gate
    extract_rules.py  JSON-LD, OpenGraph, links, addresses — zero tokens
    extract_llm.py    gap filling, strict schema, quote verification
    verify.py         cross-source arbitration and abstention
    explain.py        one explanation line per fact
    delta.py          change log, content hashes, conditional GET
    profiles.py       the fact record and the profile document
    pipeline.py       the orchestrator and the run report
    synthesis.py      the five evidence-backed views
    coverage.py       per-domain recall matrix and freshness
    liveweb.py        the browser-based web layer: search, parse, rank
    liveagent.py      the live research agent: classify, resolve, answer, session
    score_local.py    the five-category local scorer
    cli.py            setup | run | lookup | live
    serve.py          the console API
  tests/test_core.py  36 unit tests: validation, gate, delta, abstention
  tools/
    build_seed.py       build the offline sample from the live register
    export_web_data.py  snapshot the output for the offline UI
  data/
    offline_seed.json   120 real register entities, committed
    sample_orgnrs.txt   the matching number list
    golden/             hand-checked companies for the scorer
  profiles/             one JSON file per company
  runs/                 run-<timestamp>.json and score.json
  web/
    dist/index.html     the console (zero dependencies)
    dist/app.js  dist/styles.css
```

The console is dependency-free on purpose: `make web` starts a small stdlib server
and opens it. Open `web/dist/index.html` directly and it falls back to the bundled
snapshot, so the UI works with no server and no network.

---

## Installation

Nothing to install for the core path. Python 3.11+ and the standard library are
enough — `urllib`, `sqlite3`, `asyncio`, `json`. `make setup` downloads the registry
mirror; no package manager is invoked, which means no version drift on a judge's
machine.

Optional, if you want faster HTML parsing:

```bash
python -m venv .venv && . .venv/bin/activate
pip install crawl4ai        # optional: richer JS-rendered page extraction
```

Crawl4AI is used when importable and is not required. The rule extractor falls back
to the standard library and produces the same output on ordinary server-rendered
sites.

### Running with no keys at all

```bash
make demo
```

Seeds the registry mirror from the committed sample and produces real profiles for
120 real Norwegian companies with no network access, no API keys and no cost. This
is the path that proves the cascade actually cascades.

---

## Models and APIs

| Role | Choice | Why |
|---|---|---|
| Gap filling | `claude-haiku-4-5-20251001` | low cost, strict structured output, called at most once per company |
| Conflict resolution | `claude-sonnet-5-5` | used rarely, only when two sources disagree |
| Web search | Brave Search API | own index, `country=NO`, `search_lang=no`, freshness filters |
| Crawling / extraction | **rules first**, `crawl4ai` if available | most contact data needs no model at all |

All keys come from the environment; see `.env.example`. The exact model strings used
are echoed into every run report under `models`, so a reviewer can see what produced
the output without reading the source.

### Cost model

```
cost = (search calls × $0.005) + (input tokens × rate) + (output tokens × rate)
```

The design keeps it down four ways: the registry, GLEIF and Wikidata are free; search
runs only for companies with no registry website, one query each; model calls run only
for fields the rules missed, cached by organisation number and content hash; and
unchanged pages are skipped by conditional request on later runs.

### Measured costs

Populated by `make run` and printed in the run report. The table below is the shape it
fills in; run `make run ORGNR_FILE=orgnrs.txt` and read `runs/run-*.json → cost`.

| Item | Measured value |
|---|---|
| Companies in test run | see `runs/run-*.json → companies_requested` |
| Search calls | `cost.search_calls` |
| LLM input / output tokens | `cost.input_tokens` / `cost.output_tokens` |
| Total cost for 1,000 profiles | `cost.per_1000_profiles_usd` |
| Average seconds per company | `seconds / companies_written` |

**Observed on the offline sample** (120 companies, registry layer only):
zero API cost, `0.08s` per company, `70.5%` coverage of the 20 scored useful fields
(1693 of 2400 slots filled with evidence). Local score **88.2/100**, of which
coverage is **24.7/35** — above the 21/35 bar, so the keyless path clears
qualification on its own.

**Observed on a live single company** (`986587098`, registry + identifiers + web):
`81.8%` coverage, one accepted website proven by the on-page organisation number, one
corroborated field across two sources, **$0.00** — because the register supplied the
website and no search query or model call was needed.

### Where the offline coverage comes from

The registry layer is free, so the pipeline is deliberately generous about what it
takes from it. These register fields all become published facts with `registry` proof:

| Profile field | Register source | Present in the sample |
|---|---|---|
| `legal_name` | `navn` | 120 / 120 |
| `legal_form` | `organisasjonsform.kode` | 120 / 120 |
| `nace_code`, `industry` | `naeringskode1` | 120 / 120 |
| `city`, `postcode`, `municipality` | `forretningsadresse.*` | 120 / 120 |
| `description` | `aktivitet` | 120 / 120 |
| `address` | composed from `forretningsadresse` | 120 / 120 |
| `email` | `epostadresse` | 86 / 120 |
| `phone` | `telefon`, falling back to `mobil` | 91 / 120 |
| `website` | `hjemmeside` | 85 / 120 |
| `purpose` | `vedtektsfestetFormaal` | 105 / 120 |
| `latest_accounts_year` | `sisteInnsendteAarsregnskap` | 87 / 120 |
| `employees` | `antallAnsatte` | 39 / 120 |

The field table lives once, in `registry.REGISTRY_FACTS`, and `pipeline` imports it.
It used to be duplicated, and the two copies drifted: `poststed` (city), `aktivitet`
(description) and the `mobil` fallback were read from the register and then thrown
away, costing roughly four coverage points for no reason. The duplicate is gone and
`tests/test_core.py` asserts the mapping is the single source, so it cannot come back.


---

## Keeping profiles current

1. **Registry facts** — change events applied as patches at the start of every run, so
   all profiles in one run reflect one register state.
2. **Web facts** — conditional request first; on a changed hash, re-extract only the
   fields whose values moved.
3. **Change log** — every entry carries `field`, `old`, `new`, `detected_at`, `kind`
   and `source_url`, and appears in both the profile and the console's Change log tab.
4. **Expiry** — pages with no validators are rechecked after the expiry window.

---

## Scoring map

| Area | Points | How the system targets it |
|---|---|---|
| Useful information (coverage) | 35 | 20 scored fields: registry identity, identifiers, website, contact, description, socials, addresses. The pipeline fills up to 22, including `parent_org_number` and `revenue_latest` |
| Correct matching with evidence | 30 | ownership gate, verbatim quote, `match_proof`, entity graph, cross-source corroboration |
| Correct updates | 20 | change sync, content-hash diffing, change log, freshness signals |
| Useful explanations | 10 | one line per fact naming the value, proof type, layer and date |
| Ease of use | 5 | one command, `.env.example`, run report, dependency-free console |

`make score` runs the local scorer against the five categories, applies the published
weights, and states whether the run clears the qualification bar of **65/100 overall
and 21/35 on coverage**.

```
make test     # 40 unit tests
make score    # five-category score against data/golden
```

---

## Compliance and ethics

- **Permitted public sources only.** Enhetsregisteret, Regnskapsregisteret (NLOD 2.0),
  GLEIF, Wikidata (CC0), and company websites after the gate passes.
- **`robots.txt` respected.** A blanket `Disallow: /` means the site is skipped
  entirely and the pipeline falls back to the register and search snippets.
- **Personal data is not collected.** A sole trader (`ENK`) *is* a natural person, so
  the web layer is skipped for those entities rather than filtered afterwards —
  `is_organisational_contact()` in `extract_rules.py`. Only role-based mailboxes and
  switchboards are harvested; named individuals are rejected at extraction time.
- **No access-control bypass.** No log-in walls, no anti-bot circumvention. A page
  that blocks us is recorded as rejected and the profile carries the gap.
- **Null, never zero.** An unfiled figure stays absent. A *genuinely filed* zero stays
  zero, because a dormant company reporting no revenue is a real fact.

---

## Known limits

- The register publishes **no revenue or profit** in the Enhetsregisteret; those come
  from Regnskapsregisteret, which the brief describes as a temporary R&D API. It is
  therefore optional (`SIGNALPOST_FINANCIALS=1`) and never required.
- Search is capped at 10,000 results per query by the register API; the mirror is
  built organisation-form by organisation-form to stay under the ceiling.
- HTML parsing is regex-based by default. It handles server-rendered Norwegian small
  business sites well and does poorly on JavaScript-only shells; Crawl4AI closes that
  gap when installed.
- The golden set is twelve hand-checked companies. It tunes thresholds; it does
  not measure the hidden coverage set.
