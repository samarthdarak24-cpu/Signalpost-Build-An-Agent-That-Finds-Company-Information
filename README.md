# Signalpost Evidence Engine

Takes a Norwegian organisation number and returns a company profile: facts, source
links, retrieval dates, a supporting quote and a confidence score for every value.

**Design rule: registry first, web second, LLM last.** Every fact carries evidence.
The agent leaves a field empty rather than guess.

```
make setup
make run ORGNR_FILE=orgnrs.txt
make score
make web
```

`make` is only a thin wrapper. If GNU make is not installed, the identical
commands work through the bundled runner, which uses nothing but the standard
library:

```
python run.py setup
python run.py run ORGNR_FILE=orgnrs.txt
python run.py score
python run.py web
```

Full documentation, cost tables and the scoring map are in [`docs/README.md`](docs/README.md)
(this file is the short entry point). Licence and data attribution are in
[`LICENSE`](LICENSE) and [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).

## Three ideas that set it apart

1. **Proof-of-ownership gate.** Norwegian businesses are legally required to display
   their name and organisation number. A website is accepted as belonging to a company
   only when a *checksum-valid* organisation number appears on the page.
2. **Evidence on every fact.** `source_url`, `retrieved_at`, verbatim `evidence` quote,
   `match_proof` and `confidence`. No exceptions.
3. **Delta engine.** Registry changes come from the official change-event stream; web
   pages are rechecked with conditional requests and content hashes. Every change is
   logged with old value, new value and detection date.

## Layout

```
src/validate.py       modulus-11 validation
src/normalize.py      page text normalisation, org-number harvesting
src/registry.py       Brreg client + SQLite/Parquet store + change sync
src/enrich.py         GLEIF + Wikidata, concurrent
src/sitefinder.py     registry website, else Brave Search
src/gate.py           proof-of-ownership gate
src/extract_rules.py  JSON-LD, OpenGraph, mailto/tel, socials — zero tokens
src/extract_llm.py    gap filling, strict schema, quote must exist on the page
src/verify.py         cross-source arbitration, abstention
src/explain.py        one explanation line per fact
src/delta.py          change log, content hashes, conditional GET
src/profiles.py       fact-record schema + writer
src/pipeline.py       orchestrator, run report
src/score_local.py    five-category local scorer
web/                  live console UI (zero dependencies)
```

Open `web/dist/index.html` for the interface; it works offline against
`profiles/` and `runs/`, and lights up when `make web` is running.
