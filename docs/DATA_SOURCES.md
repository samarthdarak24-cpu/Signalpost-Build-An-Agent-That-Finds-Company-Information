# Data sources, licences and attribution

Signalpost publishes facts about Norwegian companies using only permitted public
sources. This file records what each source is, what it is used for, and the licence
it is published under. The attribution line required by NLOD is reproduced in full.

## Enhetsregisteret — the company register

- **Publisher:** Brønnøysundregistrene (the Brønnøysund Register Centre)
- **Endpoint:** `https://data.brreg.no/enhetsregisteret/api`
- **Licence:** NLOD 2.0 (Norwegian Licence for Open Government Data)
- **Cost:** free, no API key
- **Used for:** legal name, organisation form, industry code, address, employee count,
  founding and registration dates, registered website, email, phone, parent unit and
  sub-units, register status flags. Also the source of the local SQLite mirror.
- **Limits:** the search endpoint refuses `page × size > 10000`. The mirror is built
  one organisation form at a time to stay under that ceiling.

**Required attribution:** *Inneholder data under norsk lisens for offentlige data
(NLOD) tilgjengeliggjort av Brønnøysundregistrene.*

## Enhetsregisteret change events

- **Endpoint:** `https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter`
- **Licence:** NLOD 2.0 · **Cost:** free
- **Used for:** detecting new, changed and deleted entities between runs. Each event
  carries a field-level patch, which the delta engine applies instead of re-reading
  the whole register.

## Regnskapsregisteret — filed annual accounts

- **Endpoint:** `https://data.brreg.no/regnskapsregisteret`
- **Licence:** NLOD 2.0 · **Cost:** free, no key
- **Used for:** operating revenue, operating result, result for the year, total assets,
  equity and total debt, from the latest filed period only.
- **Optional by design.** The brief describes this as a temporary R&D API that may be
  shut down, so financials are behind `SIGNALPOST_FINANCIALS=1` and their absence
  never fails a profile.

## GLEIF — Legal Entity Identifier

- **Endpoint:** `https://api.gleif.org/api/v1/lei-records`
- **Licence:** GLEIF data is published under CC0 / public domain terms
- **Cost:** free, no key
- **Used for:** the LEI, the entity's registration status and the direct-parent
  relationship. Joined on the Norwegian registration authority code plus the
  organisation number, which is an exact match — no fuzzy name comparison, so no
  collision risk.
- **Note:** most small Norwegian companies have no LEI. That is a normal answer, and
  produces no fact rather than a null.

## Wikidata

- **Endpoint:** `https://query.wikidata.org/sparql`
- **Licence:** CC0 1.0 (public domain dedication)
- **Cost:** free
- **Used for:** the Wikidata item for the company and, from it, the official website and
  legal form. Matched on **P2338** (Norwegian organisation number) rather than on the
  label, so a name collision cannot attach the wrong company.

## Company websites

- **Accessed:** only after the proof-of-ownership gate passes
- **Used for:** contact details, a short description, social profile links and the
  organisation number that proves ownership
- **Compliance:**
  - `robots.txt` is fetched and honoured. A blanket `Disallow: /` means the site is
    skipped entirely and the pipeline falls back to the register and search snippets.
  - Requests are spaced (~120 ms), carry an identifying `User-Agent`, and are cached on
    disk so re-runs during development cost nothing.
  - No access controls are bypassed. There is no log-in wall circumvention, no anti-bot
    evasion and no headless-browser fingerprint spoofing. A site that blocks us is
    recorded as rejected and the profile carries the gap.
  - 404 is treated as genuine absence. Other 4xx responses fail fast rather than being
    retried, so a bad request cannot burn budget.

## Brave Search API

- **Endpoint:** `https://api.search.brave.com/res/v1/web/search`
- **Cost:** paid per query; requires `BRAVE_API_KEY`
- **Used for:** discovering a company website when the register record has no
  `hjemmeside`. At most one query per company, and the query includes the organisation
  number so the search is discriminating rather than a fuzzy name lookup.
- **Not used for evidence.** Search snippets are a candidate filter only. A site found
  by search still has to pass the ownership gate on its actual pages.

## Personal data

- **Sole traders are excluded from the web layer entirely.** An `ENK`
  (enkeltpersonforetak) *is* a natural person, so any contact detail on its website is
  personal data. `is_organisational_contact()` in `src/extract_rules.py` skips the web
  layer for those entities rather than filtering results afterwards.
- **Only organisational contact details are harvested.** Role mailboxes
  (`post@`, `info@`, `kontakt@`) and switchboards are accepted; addresses shaped like a
  person (`first.last@`) are rejected at extraction time.
- **No field exists for a named individual.** The gap-filling schema in
  `src/extract_llm.py` enumerates the fields the model may return, and neither a person's
  name, personal email nor mobile number is among them. The rule is enforced by the
  schema, not by asking the model nicely.
- **Register-sourced details** for legal entities are published by the register itself
  under NLOD and are reproduced as-is.

## Bulk sample

`data/offline_seed.json` contains 120 real entities extracted from the register on
2026-10-07, committed so the engine can be run end to end with no network and no API
keys. It is register data and carries the same NLOD attribution as above.

---

# Third-party software

The **core agent is standard-library only** — `python run.py run`, `score` and `web`
install nothing, and none of the packages below are imported by that path. They are used
exclusively by the optional real-time bridge (`src/live_api.py`), which runs as a separate
process. See `requirements-live.txt`.

| Project | Version | Licence | Used for in this repo |
|---|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | 0.142.2 | MIT | HTTP application serving the bridge's read-only endpoints |
| [Starlette](https://github.com/encode/starlette) | 1.7.0 | BSD-3-Clause | ASGI toolkit underlying FastAPI (transitive) |
| [Uvicorn](https://github.com/encode/uvicorn) | 0.54.0 | BSD-3-Clause | ASGI server the bridge is launched with |
| [sse-starlette](https://github.com/sysid/sse-starlette) | 3.5.0 | BSD-3-Clause | W3C-compliant Server-Sent Events response for `/api/stream/{orgnr}` |
| [HTTPX](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause | Async HTTP client polling the register's change feed |
| [aiolimiter](https://github.com/mjpieters/aiolimiter) | 1.3.0 | MIT | Async rate limiting so the poll loop stays polite to Brreg |
| [APScheduler](https://github.com/agronholm/apscheduler) | 3.11.3 | MIT | Interval scheduling of the poll tick |

### Development-only: browser verification

| Project | Version | Licence | Used for in this repo |
|---|---|---|---|
| [agent-browser](https://github.com/vercel-labs/agent-browser) | 0.39.0 | Apache-2.0 | Driving a real Chromium to verify the console's live pane renders and updates |

`agent-browser` is **not** a runtime dependency of anything the agent does. It was installed
solely to close a verification gap: the console's Server-Sent Events panel had been proven at
the HTTP level but never watched in an actual browser. It bundles its own Chromium build
(Chrome 155.0.8059.39) and is named here for completeness, not because the agent imports it.

All seven runtime packages are permissively licensed and require no API key, so the
bridge runs on a judge's machine without credentials. Nothing here is a hosted service and
nothing sends project data anywhere: the bridge only *reads* the register's public change feed
and streams the result to a browser on localhost.

**No third-party code was vendored into the repository.** These are ordinary pinned
dependencies installed into a dedicated virtual environment, so upstream security fixes
remain available and the licence terms are carried by the packages themselves.

### Design constraint this preserves

`docs/LIVE_DATA_STACK.md` records the October 8 feedback that *the judged artifact is the
working agent, not the UI*. The live layer is therefore additive: the stdlib core runs,
scores and serves with zero installs, and the bridge is an opt-in extra. If it is not
running, the console says so explicitly rather than showing an empty panel that would
imply "no changes have occurred".
