"""Source layout note.

Every module in ``src/`` is imported as a top-level module (``from validate import
validate``), and ``src`` is put on ``sys.path`` by the entry point. That keeps
``python src/pipeline.py`` working with no package installation step, which is
what "one command to run it" requires.

Modules:
    validate        modulus-11 validation and org-number harvesting
    normalize       page text, names, postcodes, social links
    net             cached, courteous, budgeted HTTP
    registry        Brreg client, local SQLite mirror, annual accounts
    enrich          GLEIF and Wikidata, concurrent
    sitefinder      registry website, else Brave Search
    gate            proof-of-ownership gate
    extract_rules   JSON-LD, OpenGraph, links, addresses — zero tokens
    extract_llm     gap filling, strict schema, quote must exist
    verify          cross-source arbitration and abstention
    explain         one explanation line per fact
    delta           change log, content hashes, conditional GET
    profiles        the fact record and the profile document
    pipeline        the orchestrator and the run report
    score_local     local scorer mirroring the rubric
"""

__all__: list[str] = []
