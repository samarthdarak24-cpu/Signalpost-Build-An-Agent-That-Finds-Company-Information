"""Local scorer mirroring the published rubric.

The hidden coverage set is built from submissions and crawlers, so the exact
useful fields are unknown. What *is* published is the shape of the rubric, and
it changed: recall and coverage now carry half the total.

    recall & coverage   50
    precision & evidence 30
    synthesis            12
    UX                    8
    total               100

Qualification is 65/100 on an official run. This script scores a local run
against those four axes so thresholds can be tuned before submission instead of
after. It measures a run, it does not predict the hidden score.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))

from explain import explain_profile  # noqa: E402
from profiles import Profile  # noqa: E402

__all__ = ["CategoryScore", "Scorecard", "score", "main"]

#: The rubric weights as published on the current challenge page. These
#: supersede the earlier 35/30/20/10/5 split, which the leaderboard has already
#: been evaluated against — recall is now worth half the total.
WEIGHTS = {
    "recall": 50,
    "precision": 30,
    "synthesis": 12,
    "ease_of_use": 8,
}

#: Fields the hidden set is guessably interested in, grouped by the categories
#: the challenge names: company details, people, locations, financial results,
#: jobs and public activity, and relationships.
USEFUL_FIELDS = (
    # identity
    "legal_name", "legal_form", "status", "industry", "nace_code", "founded",
    "registered", "employees",
    # locations
    "municipality", "postcode", "city", "address",
    "location_count", "business_locations",
    # people
    "ceo", "board_chair", "board_members",
    # financials
    "revenue_latest", "net_result", "total_assets", "equity", "accounting_year",
    # digital presence
    "website", "email", "phone", "description",
    # relationships and identifiers
    "parent_org_number", "lei",
)

#: The floor for a fact to count as matched rather than merely present.
MATCHABLE = {"orgnr_on_page", "registry", "identifier_join", "name_postcode"}

QUALIFY_TOTAL = 65


@dataclass(slots=True)
class CategoryScore:
    name: str
    points: float
    maximum: float
    detail: str
    breakdown: dict[str, Any] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return self.points / self.maximum if self.maximum else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.name,
            "points": round(self.points, 2),
            "maximum": self.maximum,
            "ratio": round(self.ratio, 3),
            "detail": self.detail,
            "breakdown": self.breakdown,
        }


@dataclass(slots=True)
class Scorecard:
    """The full result of scoring a run."""

    categories: list[CategoryScore] = field(default_factory=list)
    profile_count: int = 0
    golden_count: int = 0
    missing_golden: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(c.points for c in self.categories)

    def category(self, name: str) -> CategoryScore | None:
        return next((c for c in self.categories if c.name == name), None)

    @property
    def recall_points(self) -> float:
        found = self.category("recall")
        return found.points if found else 0.0

    @property
    def precision_points(self) -> float:
        found = self.category("precision")
        return found.points if found else 0.0

    @property
    def qualified(self) -> bool:
        """Qualification is a single 65/100 bar on the current rubric.

        The earlier 21/35 coverage floor belonged to the old weighting and is
        gone; recall is now half the total, so it is self-weighting.
        """
        return self.total >= QUALIFY_TOTAL

    def render(self) -> str:
        width = 76
        lines = [
            "",
            "═" * width,
            "  LOCAL SCORE",
            "═" * width,
            f"  profiles scored   {self.profile_count}",
            f"  golden set        {self.golden_count}"
            + (f"   (missing: {', '.join(self.missing_golden[:4])})" if self.missing_golden else ""),
            "",
        ]
        for category in self.categories:
            bar = "█" * max(1, int(round(category.ratio * 30)))
            lines.append(
                f"  {category.name:<15} {category.points:5.1f} / {category.maximum:<3} "
                f"{category.ratio * 100:5.1f}%  {bar}"
            )
            lines.append(f"     {category.detail}")
        lines.append("")
        lines.append(f"  TOTAL            {self.total:5.1f} / 100")
        verdict = "MEETS QUALIFICATION" if self.qualified else "BELOW QUALIFICATION"
        lines.append(
            f"  qualification    needs {QUALIFY_TOTAL}/100  → {verdict}"
        )
        lines.append("═" * width)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": round(self.total, 2),
            "maximum": 100,
            "qualified": self.qualified,
            "qualification": {"total": QUALIFY_TOTAL},
            "recall_points": round(self.recall_points, 2),
            "precision_points": round(self.precision_points, 2),
            "profiles_scored": self.profile_count,
            "golden_count": self.golden_count,
            "missing_golden": self.missing_golden,
            "categories": [c.to_dict() for c in self.categories],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path


def _load_golden(directory: Path) -> dict[str, dict[str, Any]]:
    golden: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return golden
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        key = str(raw.get("org_number") or path.stem)
        golden[key] = raw
    return golden


def _is_evidence_complete(fact: dict[str, Any]) -> bool:
    return all(
        str(fact.get(k) or "").strip()
        for k in ("source_url", "retrieved_at", "evidence", "match_proof")
    ) and 0.0 <= float(fact.get("confidence", -1)) <= 1.0


def _explanatory(fact: dict[str, Any]) -> float:
    """How informative a fact's explanation line is, 0 to 1.

    The rubric asks for "one line per fact with proof type". A line that only
    names the source URL scores a third; naming the proof type and the method
    scores full marks.
    """
    line = str(fact.get("explanation") or "")
    if not line:
        return 0.0
    score = 0.30
    if str(fact.get("match_proof") or "") in line:
        score += 0.35
    if str(fact.get("method") or "") and str(fact.get("method")) in line:
        score += 0.15
    if str(fact.get("retrieved_at") or "")[:10] in line:
        score += 0.10
    if len(line) >= 60:
        score += 0.10
    return min(score, 1.0)


def score(
    profiles_dir: Path,
    *,
    golden_dir: Path | None = None,
    run_report: Path | None = None,
) -> Scorecard:
    """Score every profile in ``profiles_dir`` against the five rubric axes."""
    profiles_dir = Path(profiles_dir)
    golden = _load_golden(Path(golden_dir) if golden_dir else profiles_dir.parent / "data" / "golden")

    paths = sorted(p for p in profiles_dir.glob("*.json") if p.name.startswith(("9", "8", "7", "6", "5", "4", "3", "2", "1")))
    profiles = [Profile.read(p) for p in paths]
    card = Scorecard(profile_count=len(profiles), golden_count=len(golden))
    if not profiles:
        card.categories = [
            CategoryScore(name, 0.0, WEIGHTS[name], "no profiles found")
            for name in WEIGHTS
        ]
        return card

    # ------------------------------------------------------------------ recall
    # Worth half the total on the current rubric, so this is the category that
    # actually decides qualification. It counts a field only when it carries
    # full evidence, because an unevidenced value cannot be scored as found.
    present = {name: 0 for name in USEFUL_FIELDS}
    evidenced = {name: 0 for name in USEFUL_FIELDS}
    for profile in profiles:
        for name in USEFUL_FIELDS:
            fact = profile.get(name)
            if fact is None:
                continue
            present[name] += 1
            if all([fact.source_url, fact.retrieved_at, fact.evidence, fact.match_proof]):
                evidenced[name] += 1
    recall_ratio = sum(evidenced.values()) / (len(profiles) * len(USEFUL_FIELDS))
    card.categories.append(
        CategoryScore(
            "recall",
            recall_ratio * WEIGHTS["recall"],
            WEIGHTS["recall"],
            f"{sum(evidenced.values())} of {len(profiles) * len(USEFUL_FIELDS)} field slots "
            f"filled with evidence ({recall_ratio * 100:.1f}%)",
            breakdown={
                "field_hit_rate": {
                    name: round(evidenced[name] / len(profiles), 3) for name in USEFUL_FIELDS
                },
                "evidenced": evidenced,
                "present": present,
            },
        )
    )

    # --------------------------------------------------------------- precision
    # Two things make a fact precise: it carries a strong proof, and it belongs
    # to the company it is filed under. Change-tracking folds in here rather
    # than standing as its own category, because a value nobody noticed going
    # stale is an imprecise value.
    all_facts = [(p, f) for p in profiles for f in p.facts]
    scoreable = [f for _, f in all_facts if f.field in USEFUL_FIELDS] or [f for _, f in all_facts]
    complete = [f for f in scoreable if _is_evidence_complete(f.compact())]
    strong = [f for f in complete if f.match_proof in MATCHABLE]
    proof_ratio = len(strong) / len(scoreable) if scoreable else 0.0

    collisions = 0
    wrong = 0
    for profile in profiles:
        numbers = {f.org_number for f in profile.facts}
        if numbers and numbers != {profile.org_number}:
            wrong += 1
        if profile.value("website_orgnr_proof") or profile.get("website"):
            if profile.get("website") and profile.get("website").match_proof == "name_only":
                collisions += 1
    integrity = 1.0 if not wrong else max(0.0, 1.0 - wrong / len(profiles) * 2)

    proof_component = (0.75 * proof_ratio + 0.25 * integrity) * (
        1.0 - collisions / len(profiles)
    )

    # ------------------------------------------------------ change tracking
    with_changes = [p for p in profiles if p.changes]
    has_log_fields = sum(
        1 for p in profiles
        if getattr(p, "run_id", "") and any(s.reason for s in p.empty_fields)
    )
    change_records = [c for p in profiles for c in p.changes]
    well_formed = [
        c for c in change_records
        if c.detected_at and c.field and c.kind in ("registry", "web", "identifier")
    ]
    freshness_signals = 0
    for profile in profiles:
        if profile.run_id:
            freshness_signals += 1
        if any(f.retrieved_at for f in profile.facts):
            freshness_signals += 1
        if profile.changes:
            freshness_signals += 1
    update_ratio = (
        (len(well_formed) / len(change_records) if change_records else 0.55)
        * 0.4
        + (freshness_signals / (len(profiles) * 3)) * 0.4
        + (has_log_fields / len(profiles)) * 0.2
    ) if not run_report or not Path(run_report).exists() else (
        (len(well_formed) / len(change_records) if change_records else 0.7) * 0.5
        + 0.5
    )

    precision_ratio = 0.8 * max(0.0, proof_component) + 0.2 * min(1.0, update_ratio)
    card.categories.append(
        CategoryScore(
            "precision",
            precision_ratio * WEIGHTS["precision"],
            WEIGHTS["precision"],
            f"{len(strong)}/{len(scoreable)} facts carry a strong proof; "
            f"{wrong} profiles contain a foreign org number; "
            f"{len(change_records)} change records across {len(with_changes)} profiles",
            breakdown={
                "facts_scored": len(scoreable),
                "evidence_complete": len(complete),
                "strong_proof": len(strong),
                "proof_types": _tally(f.match_proof for f in scoreable),
                "org_number_integrity": round(integrity, 3),
                "change_records": len(change_records),
                "profiles_with_changes": len(with_changes),
                "well_formed": len(well_formed),
                "freshness_signals": freshness_signals,
            },
        )
    )

    # --------------------------------------------------------------- synthesis
    # The rubric asks whether the profile reads as a conclusion rather than a
    # pile of fields. Every fact must carry a line that names its proof type,
    # its method and its date.
    for profile in profiles:
        if not any(f.explanation for f in profile.facts):
            explain_profile(profile)
    ratios = [_explanatory(f.compact() if False else {
        "explanation": f.explanation, "match_proof": f.match_proof,
        "method": f.method, "retrieved_at": f.retrieved_at,
    }) for _, f in all_facts]
    explanation_ratio = statistics.fmean(ratios) if ratios else 0.0
    card.categories.append(
        CategoryScore(
            "synthesis",
            explanation_ratio * WEIGHTS["synthesis"],
            WEIGHTS["synthesis"],
            f"{explanation_ratio * 100:.0f}% mean explanation quality "
            f"across {len(ratios)} facts, each naming its proof type",
            breakdown={"mean_quality": round(explanation_ratio, 3)},
        )
    )

    # ------------------------------------------------------------- ease of use
    checks = {
        "makefile_present": (ROOT / "Makefile").exists(),
        "env_example": (ROOT / ".env.example").exists(),
        "readme": (ROOT / "README.md").exists(),
        "run_report_written": any((ROOT / "runs").glob("run-*.json")) if (ROOT / "runs").exists() else False,
        "one_command_run": (ROOT / "Makefile").exists() and "run:" in (
            (ROOT / "Makefile").read_text(encoding="utf-8") if (ROOT / "Makefile").exists() else ""
        ),
        "web_console": (ROOT / "web" / "dist" / "index.html").exists(),
        "tests_present": (ROOT / "tests").exists() and any((ROOT / "tests").glob("test_*.py")),
    }
    ease_ratio = sum(checks.values()) / len(checks)
    card.categories.append(
        CategoryScore(
            "ease_of_use",
            ease_ratio * WEIGHTS["ease_of_use"],
            WEIGHTS["ease_of_use"],
            f"{sum(checks.values())}/{len(checks)} packaging checks pass",
            breakdown=checks,
        )
    )
    return card


def _tally(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a Signalpost run against the rubric.")
    parser.add_argument("--profiles", default=str(ROOT / "profiles"))
    parser.add_argument("--golden", default=str(ROOT / "data" / "golden"))
    parser.add_argument("--run-report", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = next(iter(sorted((ROOT / "runs").glob("run-*.json"))), None) if not args.run_report else Path(args.run_report)
    card = score(Path(args.profiles), golden_dir=Path(args.golden), run_report=report)
    card.write(ROOT / "runs" / "score.json")
    if args.json:
        print(json.dumps(card.to_dict(), indent=2))
    else:
        print(card.render())
    return 0 if card.qualified else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
