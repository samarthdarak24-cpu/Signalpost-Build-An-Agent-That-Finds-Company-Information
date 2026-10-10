"""Task #48 probe: compare current vs alternative web-discovery queries.

Read-only inspection of what the keyless search engines actually return for a
handful of real companies. No fact is written; only candidate URLs are printed.

Run:
  python runs/audit/t48_probe.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from net import HttpClient  # noqa: E402
from sitefinder import SiteFinder, normalize_url, _registrable  # noqa: E402
from normalize import DIRECTORY_HOSTS, fold_name  # noqa: E402
from validate import validate as validate_org  # noqa: E402

SEED = ROOT / "data" / "offline_seed.json"

# A few well-known companies in the seed, chosen so results are interpretable.
TARGETS = [
    "923609016",  # Equinor ASA
    "948767006",  # DNB Bank ASA (likely register has website)
    "997339130",  # Telenor ASA
]


def load_entity(number: str) -> dict | None:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    for e in data.get("entities", []):
        if e.get("organisasjonsnummer") == number:
            return e
    return None


def show(heading: str, items: list[dict]) -> None:
    print(f"  {heading}: {len(items)} raw results")
    for it in items[:8]:
        url = normalize_url(it.get("url", ""))
        host = _registrable(url)
        tag = "DIR" if any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS) else "   "
        print(f"    [{tag}] {host:<32} {it.get('title','')[:40]}")


def main() -> int:
    http = HttpClient()
    finder = SiteFinder(http)

    alt_queries = [
        lambda e, o, c: f'{e.get("navn","")}',                                   # name only
        lambda e, o, c: f'{e.get("navn","")} hjemmeside',                        # name + hjemmeside
        lambda e, o, c: f'{e.get("navn","")} offisiell nettside',                # name + official
        lambda e, o, c: f'{e.get("navn","")} {c}',                               # name + city
        lambda e, o, c: f'{e.get("navn","")} kontakt',                           # name + contact
    ]

    for number in TARGETS:
        e = load_entity(number)
        if not e:
            print(f"{number}: not in seed, skip")
            continue
        name = e.get("navn", "")
        org = validate_org(number)
        city = (e.get("forretningsadresse") or {}).get("poststed", "")
        print("=" * 72)
        print(f"{number}  {name}  (city={city})")

        # Current behaviour
        print("-- CURRENT from_search --")
        cands = finder.from_search(e, org)
        for c in cands[:6]:
            print(f"   {c.origin:<10} score={c.score:.2f} rej={c.rejected_reason or '-':<40} {c.url}")

        # Alternative queries (raw search results only)
        for qfn in alt_queries:
            q = qfn(e, org, city).strip()
            if not q:
                continue
            attempt = finder.search_attempt(q, count=8)
            print(f"\n-- ALT QUERY: {q!r}  -> status={attempt.status} engine={attempt.engine} n={len(attempt.results)}")
            if attempt.error:
                print(f"   error: {attempt.error[:90]}")
            show("   results", attempt.results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
