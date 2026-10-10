"""Live correctness check: run the agent on unseen numbers, diff against Brreg.

Ground truth comes straight from the public Brreg API over urllib -- it does not
import a single line of the project, so agreement is real evidence and not the
code agreeing with itself.

Usage:
    python runs/audit/live_check.py register   # numbers only, no web
    python runs/audit/live_check.py web        # numbers + open-web leg
    python runs/audit/live_check.py names      # name queries
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "runs" / "audit" / "live_check_report.json"
LOG = ROOT / "runs" / "audit" / "live_check.log"


def log(line: str = "") -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def brreg(org: str) -> dict:
    """Independent ground truth, straight from the register. No project code."""
    try:
        with urllib.request.urlopen(
            f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", timeout=30
        ) as h:
            return json.loads(h.read().decode())
    except urllib.error.HTTPError as e:
        return {"__status": e.code}
    except Exception as e:  # noqa: BLE001
        return {"__err": f"{type(e).__name__}: {e}"}


def truth_of(g: dict) -> dict:
    if "__status" in g or "__err" in g:
        return {}
    adr = g.get("forretningsadresse", {}) or {}
    return {
        "navn": g.get("navn", ""),
        "legal_form": (g.get("organisasjonsform", {}) or {}).get("kode", ""),
        "city": adr.get("poststed", ""),
        "municipality": (adr.get("kommune", "") or "").upper(),
        "industry": (g.get("naeringskode1", {}) or {}).get("beskrivelse", ""),
        "employees": g.get("antallAnsatte"),
        "website": g.get("hjemmeside", ""),
        "registered": g.get("registreringsdatoEnhetsregisteret", ""),
    }


def compare(org: str, agent_evidence: list[dict], agent_target: str, truth: dict) -> dict:
    got = {}
    for e in agent_evidence:
        got.setdefault(e["field"], e["value"])
    checks: list[dict] = []

    name_ok = agent_target.strip().casefold() == truth["navn"].strip().casefold()
    checks.append({"field": "legal_name", "agent": agent_target,
                   "register": truth["navn"], "ok": name_ok})

    for field, key in (("legal_form", "legal_form"), ("city", "city"),
                       ("municipality", "municipality"), ("industry", "industry")):
        if field not in got:
            continue  # absent stays absent -- not a failure
        a = str(got[field]).strip().casefold()
        b = str(truth[key]).strip().casefold()
        checks.append({"field": field, "agent": got[field],
                       "register": truth[key], "ok": a == b})
    return {"org": org, "checks": checks,
            "wrong": [c for c in checks if not c["ok"]],
            "fact_count": len(agent_evidence)}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "register"
    LOG.write_text("", encoding="utf-8")
    targets = json.loads((ROOT / "runs" / "random_targets.json").read_text())

    from liveagent import LiveSession  # noqa: PLC0415

    reports = []
    for i, org in enumerate(targets, 1):
        g = brreg(org)
        truth = truth_of(g)
        if not truth:
            log(f"[skip] {org}: {g}")
            continue
        sess = LiveSession(f"chk-{mode}-{org}", no_web=(mode == "register"))
        started = time.time()
        try:
            ans = sess.ask(org)
        except Exception as e:  # noqa: BLE001
            log(f"[ERROR] {org}: {type(e).__name__}: {e}")
            reports.append({"org": org, "error": f"{type(e).__name__}: {e}"})
            continue
        rec = compare(org, ans.evidence, ans.target or "", truth)
        rec["answered"] = ans.answered
        rec["seconds"] = round(time.time() - started, 2)
        rec["answer"] = ans.answer
        rec["engine"] = ans.engine
        rec["notes"] = ans.notes
        rec["sources"] = len(ans.sources)
        reports.append(rec)

        mark = "OK " if not rec["wrong"] else "!! "
        log(f"[{mark}] {org}  facts={rec['fact_count']} sources={rec['sources']} "
            f"{rec['seconds']}s answered={rec['answered']}")
        log(f"      agent   : {ans.target}")
        log(f"      register: {truth['navn']}  form={truth['legal_form']} "
            f"city={truth['city']}")
        for w in rec["wrong"]:
            log(f"      WRONG {w['field']}: agent={w['agent']!r} "
                f"register={w['register']!r}")
        log(f"      answer  : {ans.answer[:180]}")
        log("")

    OUT.write_text(json.dumps(reports, indent=1, ensure_ascii=False), encoding="utf-8")
    total_wrong = sum(len(r.get("wrong", [])) for r in reports)
    name_wrong = sum(1 for r in reports
                     for c in r.get("checks", []) if c["field"] == "legal_name" and not c["ok"])
    log(f"SUMMARY mode={mode} companies={len(reports)} "
        f"name_mismatches={name_wrong} field_mismatches={total_wrong}")
    print(f"done: {len(reports)} companies, {name_wrong} name mismatches, "
          f"{total_wrong} field mismatches -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
