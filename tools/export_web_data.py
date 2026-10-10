"""Export the run output into the UI's static snapshot.

The console prefers the live API, but must also work when ``web/dist/index.html``
is opened straight from disk with no server running. This copies the profile
index, the full profiles, the latest run report and the score into
``web/dist/data/``.

    python tools/export_web_data.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "web" / "dist" / "data"


def main() -> int:
    profiles_dir = ROOT / "profiles"
    runs_dir = ROOT / "runs"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "profiles").mkdir(exist_ok=True)

    index: list[dict] = []
    for path in sorted(profiles_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        facts = raw.get("facts", [])
        proofs = [f.get("match_proof") for f in facts]
        index.append({
            "org_number": raw.get("org_number", path.stem),
            "legal_name": raw.get("legal_name", ""),
            "fact_count": len(facts),
            "empty_count": len(raw.get("empty_fields", [])),
            "change_count": len(raw.get("changes", [])),
            "run_date": raw.get("run_date", ""),
            "run_id": raw.get("run_id", ""),
            "has_website": any(f.get("field") == "website" for f in facts),
            "proves_ownership": "orgnr_on_page" in proofs,
            "weak_matches": proofs.count("name_postcode"),
            "fields": [f.get("field") for f in facts],
        })
        shutil.copyfile(path, OUT / "profiles" / path.name)

    (OUT / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    runs = sorted(runs_dir.glob("run-*.json"), reverse=True)[:10]
    reports = []
    for path in runs:
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    (OUT / "runs.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")

    score_path = runs_dir / "score.json"
    if score_path.exists():
        shutil.copyfile(score_path, OUT / "score.json")

    size = sum(p.stat().st_size for p in OUT.rglob("*.json"))
    print(f"exported {len(index)} profiles and {len(reports)} run reports to "
          f"{OUT.relative_to(ROOT)} ({size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
