"""Task #48 Phase 4 — isolated web-enabled evaluation.

Runs the *current* (post-fix) pipeline with web discovery turned on, onto a
directory that is NOT the committed `profiles/`, for the same 40 companies the
frozen baseline used (`--seed 20261010`). Then scores that isolated corpus with
the same evaluation harness, so the before/after comparison is apples-to-apples
and the committed baseline is never mutated.

Safety notes:
* The Brreg delta-sync at pipeline startup is no-op'd. The local registry mirror
  already holds every entity, so entity data is identical; only the (network,
  best-effort) change-feed is skipped, which cannot change any fact.
* Budget enforcement is the pipeline's default `Budget()` + `HttpClient`, so the
  2,000-request ceiling is live exactly as in production.
* `use_llm=False` keeps the run keyless and deterministic and avoids any API
  spend; email/phone come from the unchanged rule extractor, which only has
  something to extract once a site is discovered.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import registry as _registry  # noqa: E402
from evaluate import (  # noqa: E402
    _random_numbers,
    _seed_payload,
    evaluate,
)
import pipeline  # noqa: E402
from pipeline import Settings, run  # noqa: E402

ISOLATED = ROOT / "runs" / "audit" / "t48_isolated_corpus"
SEED = 20261010
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40


def main() -> int:
    numbers = _random_numbers(N, rng=random.Random(SEED))
    payload = _seed_payload()

    # Skip the best-effort Brreg change-feed sync; the local mirror is complete.
    _registry.sync_changes = lambda *a, **k: []  # type: ignore[assignment]

    settings = Settings(
        profiles_dir=ISOLATED / "profiles",
        runs_dir=ISOLATED / "runs",
        cache_dir=ISOLATED / "cache",
        registry_db=ROOT / "data" / "registry.sqlite",
        snapshot_db=ISOLATED / "snapshots.sqlite",
        llm_cache=ROOT / "data" / "llm_cache",
        offline=False,
        fetch_web=True,
        fetch_financials=False,
        fetch_jobs=False,
        fetch_activity=False,
        use_llm=False,
        workers=3,
        quiet=True,
    )

    report = run(numbers, settings=settings)
    rep = report.to_dict()

    # Score the isolated corpus with the same harness.
    import evaluate as ev_mod
    ev_mod.PROFILES = ISOLATED / "profiles"
    ev = evaluate(numbers, seed_payload=payload)

    summary = {
        "n": N,
        "seed": SEED,
        "run": {
            "companies_requested": rep["execution"]["profiles"]["requested"],
            "companies_completed": rep["execution"]["profiles"]["completed"],
            "outbound_attempts": rep["execution"]["outbound"]["attempts"],
            "outbound_succeeded": rep["execution"]["outbound"]["succeeded"],
            "outbound_failed": rep["execution"]["outbound"]["failed"],
            "search_outcomes": rep["execution"]["searches"],
            "websites_accepted": rep["execution"].get("websites_accepted", 0),
            "websites_rejected": rep["execution"].get("websites_rejected", 0),
        },
        "eval": ev.to_dict(),
    }

    out = ISOLATED / f"t48_after_n{N}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print(ev.report())
    print()
    print("OUTBOUND ATTEMPTS:", rep["execution"]["outbound"]["attempts"],
          "| succeeded:", rep["execution"]["outbound"]["succeeded"],
          "| failed:", rep["execution"]["outbound"]["failed"])
    print("SEARCH OUTCOMES:", rep["execution"]["searches"])
    print("WROTE:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
