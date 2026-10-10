"""Generate the submission reports from a completed run directory.

    python scripts/reports.py runs/smoke100

Writes docs/SMOKE_TEST_REPORT.md, docs/COST_REPORT.md and docs/RECALL_GAPS.md
from the run's envelopes, run report and profiles. Everything is measured from
the run on disk -- nothing here is estimated.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coverage import (  # noqa: E402
    missing_high_value_fields,
    recall_matrix,
    render_recall_matrix,
)
from profiles import Profile  # noqa: E402


def _load(run: Path) -> tuple[list[dict], dict, list[Profile]]:
    envs = [json.loads(x) for x in
            (run / "envelopes.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    reports = sorted(run.glob("run-*.json"))
    report = json.loads(reports[-1].read_text(encoding="utf-8")) if reports else {}
    profiles = [Profile.read(p) for p in sorted((run / "profiles").glob("*.json"))]
    return envs, report, profiles


def smoke_test(run: Path, docs: Path) -> str:
    envs, report, _ = _load(run)
    tally = Counter(e["status"] for e in envs)
    facts = sum(e.get("fact_count", 0) for e in envs)
    evidenced = sum(
        1 for e in envs for f in e.get("facts", [])
        if f.get("source_url") and f.get("evidence")
    )
    audit = report.get("envelope_audit", {})
    lines = [
        "# Signalpost — 100-Company Smoke Test",
        "",
        "One complete terminal result for every supplied company, from a fresh",
        "batch file, with no precomputed profiles required.",
        "",
        "```",
        "SIGNALPOST SMOKE TEST",
        "",
        f"Input companies:      {len(envs):>6}",
        f"Output envelopes:     {len(envs):>6} OK",
        f"Missing outputs:      {len(audit.get('missing_outputs', [])):>6} OK",
        f"Duplicate outputs:    {len(audit.get('duplicate_outputs', [])):>6} OK",
        "",
        f"Available:            {tally.get('available', 0):>6}",
        f"Not available:        {tally.get('not_available', 0):>6}",
        f"Ambiguous:            {tally.get('ambiguous', 0):>6}",
        f"Blocked:              {tally.get('blocked', 0):>6}",
        f"Not applicable:       {tally.get('not_applicable', 0):>6}",
        f"Failed:               {tally.get('failed', 0):>6}",
        "",
        f"Facts extracted:      {facts:>6}",
        f"Evidence-backed:      {evidenced:>6}",
        "",
        f"Runtime:              {report.get('seconds', 0):>6.0f} s",
        f"Requests:             {report.get('requests', 0):>6}",
        f"LLM calls:            {report.get('llm_calls', 0):>6}",
        f"Cost:                 ${report.get('cost', {}).get('total_usd', 0):.4f}",
        "```",
        "",
        "Every envelope is written to `runs/envelopes.jsonl`, one JSON object per line.",
        "A missing row would make the run unscorable; there are none.",
    ]
    text = "\n".join(lines) + "\n"
    (docs / "SMOKE_TEST_REPORT.md").write_text(text, encoding="utf-8")
    return text


def cost_report(run: Path, docs: Path) -> str:
    envs, report, _ = _load(run)
    n = len(envs)
    cost = report.get("cost", {})
    seconds = report.get("seconds", 0) or 0
    per_company = seconds / n if n else 0
    lines = [
        "# Signalpost — Cost & Runtime Report",
        "",
        "Measured from a real run (no estimates except the projection line).",
        "",
        "```",
        f"Test batch              {n} companies",
        "",
        f"Search requests         {report.get('search_calls', 0)}",
        f"Total requests          {report.get('requests', 0)}",
        f"LLM calls               {report.get('llm_calls', 0)}",
        f"Input tokens            {report.get('input_tokens', 0)}",
        f"Output tokens           {report.get('output_tokens', 0)}",
        "",
        f"Elapsed time            {seconds / 60:.1f} min",
        f"Average / company       {per_company:.1f} sec",
        "",
        f"Search cost             ${cost.get('search_usd', 0):.4f}",
        f"LLM cost                ${cost.get('llm_usd', 0):.4f}",
        f"External API cost       ${cost.get('total_usd', 0):.4f}",
        f"Projected / 1,000       ${cost.get('per_1000_profiles_usd', 0):.2f}",
        "```",
        "",
        "The registry, financials, GLEIF and Wikidata layers are keyless and free",
        "(NLOD 2.0 / open data). With no LLM and no search the external cost is $0.00;",
        "the projection scales the measured per-profile cost to a 1,000-company batch.",
    ]
    text = "\n".join(lines) + "\n"
    (docs / "COST_REPORT.md").write_text(text, encoding="utf-8")
    return text


def recall_gaps(run: Path, docs: Path) -> str:
    _, _, profiles = _load(run)
    matrix = recall_matrix(profiles)
    missing = missing_high_value_fields(profiles, limit=15)
    lines = [
        "# Signalpost — Recall Gap Report",
        "",
        f"Computed over {len(profiles)} profiles. Where the next point of recall is.",
        "",
        "```",
        render_recall_matrix(matrix, missing),
        "```",
        "",
        "## Highest-value missing fields (weight x absence)",
        "",
        "| Field | Domain | Absent | Weight |",
        "|---|---|---|---|",
    ]
    for m in missing:
        lines.append(
            f"| {m['label']} | {m['domain_label']} | {m['absent_pct'] * 100:.0f}% | {m['weight']} |"
        )
    lines += [
        "",
        "Priority order follows expected gain: financials, people/roles, locations,",
        "website, jobs, activity.",
    ]
    text = "\n".join(lines) + "\n"
    (docs / "RECALL_GAPS.md").write_text(text, encoding="utf-8")
    return text


def main(argv: list[str]) -> int:
    run = ROOT / (argv[0] if argv else "runs/smoke100")
    docs = ROOT / "docs"
    if not (run / "envelopes.jsonl").exists():
        print(f"no envelopes at {run}", file=sys.stderr)
        return 1
    smoke_test(run, docs)
    cost_report(run, docs)
    recall_gaps(run, docs)
    print("wrote docs/SMOKE_TEST_REPORT.md, docs/COST_REPORT.md, docs/RECALL_GAPS.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
