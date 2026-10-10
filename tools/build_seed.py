"""Build data/offline_seed.json from the live register.

Run once, commit the output. The seed is what makes ``--offline`` produce real
profiles with real numbers on a machine that has no network and no API keys,
which is the whole point of a demo a judge can reproduce in one command.

    python tools/build_seed.py --count 60
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from net import Budget, HttpClient  # noqa: E402
from registry import BrregClient  # noqa: E402
from validate import clean  # noqa: E402

#: Organisation forms to sample. Mixed on purpose: AS gives employees and a
#: website, ENK gives sole traders (useful for exercising the personal-data
#: guard), ASA and SA give larger groups with parents and sub-units.
FORMS = ("AS", "ASA", "SA", "ANS", "ENK", "DA", "NUF", "STI")

#: Hand-picked, well-known Norwegian companies whose public data is stable and
#: whose websites are large enough to exercise the ownership gate properly.
#: Mixed sizes on purpose: 2CARE has 244 employees, Equinor has tens of
#: thousands, and the register office has none — all three must render.
PINNED = (
    "986587098",  # 2CARE BPA AS — the worked example, website havas.no
    "923609016",  # EQUINOR ASA
    "982463718",  # TELENOR ASA
    "984851006",  # DNB BANK ASA
    "914778271",  # NORSK HYDRO ASA
    "986228608",  # YARA INTERNATIONAL ASA
    "910747711",  # ORKLA ASA
    "974760673",  # REGISTERENHETEN I BRØNNØYSUND — a public body, no employees field
    "988257133",  # VY
    "914778639",  # AKER
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--per-form", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20261007)
    args = parser.parse_args()

    random.seed(args.seed)
    budget = Budget()
    http = HttpClient(cache_dir=ROOT / "data" / "cache", budget=budget)
    client = BrregClient(http)

    entities: dict[str, dict] = {}

    def want(entity: dict) -> bool:
        number = clean(entity.get("organisasjonsnummer"))
        if not number or not entity.get("navn"):
            return False
        # Prefer companies with something to extract.
        if not entity.get("forretningsadresse"):
            return False
        return True

    print("pinned companies")
    for number in PINNED:
        entity = client.entity(number)
        if entity and want(entity):
            entities[entity["organisasjonsnummer"]] = entity
            name = entity.get("navn", "")
            site = entity.get("hjemmeside") or "-"
            print(f"  {number}  {name[:44]:<46} {site}")

    print("\nbulk page sample")
    per_form = max(1, args.count // len(FORMS))
    for form in FORMS:
        for page in range(0, 4):
            try:
                payload = client.page(page=page, size=100, organisasjonsform=form)
            except Exception as exc:
                print(f"  ! {form}: {exc}")
                break
            batch = ((payload.get("_embedded") or {}).get("enheter")) or []
            if not batch:
                break
            eligible = [e for e in batch if want(e)]
            random.shuffle(eligible)
            # Two thirds with a website so the web layer is exercised, one third
            # without so the site-finder fallback path is exercised too.
            with_site = [e for e in eligible if e.get("hjemmeside")]
            without = [e for e in eligible if not e.get("hjemmeside")]
            picked = with_site[:max(1, per_form - 2)] + without[:2]
            for entity in picked:
                entities[entity["organisasjonsnummer"]] = entity
            print(f"  {form} page {page}: kept {len(picked)} (total {len(entities)})")
            if len(entities) >= args.count * 2:
                break
        if len(entities) >= args.count * 2:
            break

    ordered = sorted(entities.values(), key=lambda e: e["organisasjonsnummer"])
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "Enhetsregisteret, data.brreg.no (NLOD 2.0)",
        "note": (
            "Bundled sample of the Norwegian register so the engine can be run "
            "end to end with no network access and no API keys."
        ),
        "count": len(ordered),
        "entities": ordered,
    }
    target = ROOT / "data" / "offline_seed.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    numbers = ROOT / "data" / "sample_orgnrs.txt"
    lines = ["# Bundled demo numbers, taken from data/offline_seed.json",
             "# Run with: make demo   or   python src/cli.py run data/sample_orgnrs.txt"]
    lines += [e["organisasjonsnummer"] for e in ordered]
    numbers.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nwrote {len(ordered)} entities to {target.relative_to(ROOT)}")
    print(f"wrote {len(ordered)} numbers to {numbers.relative_to(ROOT)}")
    print(f"requests used: {budget.used}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
