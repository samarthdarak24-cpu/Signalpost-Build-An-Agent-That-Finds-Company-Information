"""Task #47 evidence: why is batch web-only coverage 0.0% when keyless search works?

The question is not "is search broken". Keyless ``liveweb.search_web`` demonstrably
returns results. The question is whether the *batch pipeline's* website discovery
uses that same engine stack. This script answers it by running both paths on the
same query in the same process, with no API keys set, and printing what each did.

Run::

    python runs/audit/prove_two_search_paths.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from net import HttpClient  # noqa: E402
from sitefinder import SiteFinder  # noqa: E402
from validate import OrgNumber  # noqa: E402
import liveweb  # noqa: E402

#: A real, large Norwegian company whose website the register does not carry.
ENTITY = {
    "navn": "Norsk Hydro ASA",
    "organisasjonsnummer": "914778271",
    "forretningsadresse": {"poststed": "Oslo", "postnummer": "0283"},
}
ORGNR = "914778271"


def main() -> int:
    brave = os.environ.get("BRAVE_API_KEY", "")
    anthropic = os.environ.get("ANTHROPIC_API_KEY", "")
    print("=" * 72)
    print("ENVIRONMENT")
    print("=" * 72)
    print(f"  BRAVE_API_KEY     : {'set' if brave else '(unset)'}")
    print(f"  ANTHROPIC_API_KEY : {'set' if anthropic else '(unset)'}")
    print()

    org = OrgNumber(ORGNR)
    query = f'"Norsk Hydro ASA" "{org.display}"'

    print("=" * 72)
    print("PATH A -- batch pipeline website discovery (sitefinder.SiteFinder)")
    print("=" * 72)
    finder = SiteFinder(HttpClient())
    print(f"  engines available   : brave only (BASE={__import__('sitefinder').BASE})")
    print(f"  brave_key present   : {bool(finder.brave_key)}")
    attempt = finder.search_attempt(query)
    print(f"  query               : {query}")
    print(f"  status              : {attempt.status!r}")
    print(f"  results             : {len(attempt.results)}")
    print(f"  error               : {attempt.error!r}")
    print(f"  -> candidates() len : {len(finder.candidates(ENTITY, org))}")
    print()

    print("=" * 72)
    print("PATH B -- keyless live agent search (liveweb.search_web)")
    print("=" * 72)
    print(f"  engines available   : {[n for n, _ in liveweb.SEARCH_ENGINES]}")
    outcome = liveweb.search_web(
        query, company_name=ENTITY["navn"], cache=False, limit=5
    )
    print(f"  query               : {query}")
    print(f"  engine used         : {outcome.engine!r}")
    print(f"  engines_tried       : {outcome.engines_tried}")
    print(f"  results             : {len(outcome.results)}")
    print(f"  error               : {outcome.error!r}")
    for r in outcome.results[:5]:
        print(f"    - [{r.rank}] {r.url}  ({r.engine})")
    print()

    print("=" * 72)
    print("VERDICT")
    print("=" * 72)
    print("  The two paths do not share an engine stack.")
    print(f"  A (batch/website)  -> {attempt.status!r}, {len(attempt.results)} results")
    print(f"  B (keyless/live)   -> {len(outcome.results)} results via {outcome.engine!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
