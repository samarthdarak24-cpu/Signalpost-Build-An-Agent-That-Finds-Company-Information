"""End-to-end feature + live-agent check on real, external inputs.

Everything here goes over the network to live endpoints. Results are appended to
a log so a killed shell does not lose them.

Usage: python runs/audit/feature_check.py <part>
    parts: http | api | classify | names | questions | followup | invalid
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

LOG = ROOT / "runs" / "audit" / "feature_check.log"
BASE = "http://127.0.0.1:8787"


def log(line: str = "") -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def get(path: str, timeout: float = 60.0):
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as h:
            return h.status, json.loads(h.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def post(path: str, payload: dict, timeout: float = 120.0):
    url = BASE + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as h:
            return h.status, json.loads(h.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def part_http() -> None:
    log("### HTTP static + docs")
    for path in ("/", "/app.js", "/styles.css", "/api/health"):
        try:
            with urllib.request.urlopen(BASE + path, timeout=15) as h:
                body = h.read()
                log(f"  {path:<16} {h.status}  {len(body)}b")
        except Exception as e:  # noqa: BLE001
            log(f"  {path:<16} FAIL {e}")


def part_api() -> None:
    log("### API endpoints (GET)")
    endpoints = [
        "/api/health", "/api/profiles", "/api/runs", "/api/score", "/api/golden",
        "/api/recall", "/api/audit", "/api/live", "/api/search?q=as",
        "/api/profile/923609016", "/api/synthesis/923609016", "/api/trace/923609016",
        "/api/lookup?orgnr=923609016",
    ]
    for path in endpoints:
        status, body = get(path)
        shape = ""
        if isinstance(body, dict):
            shape = ",".join(sorted(body)[:7])
        elif isinstance(body, list):
            shape = f"list[{len(body)}]"
        log(f"  {path:<38} {status}  {shape}")


def part_classify() -> None:
    from liveagent import classify_input  # noqa: PLC0415

    log("### Input classifier")
    cases = [
        ("923609016", "orgnr"), ("986 587 098", "orgnr"), ("NO 986 587 098 MVA", "orgnr"),
        ("equinor.no", "domain"), ("https://www.equinor.no/careers", "domain"),
        ("vasatest.no", "domain"),
        ("Equinor ASA", "name"), ("Telenor", "name"),
        ("who runs Equinor?", "question"), ("hva gjør Telenor?", "question"),
        ("Hva er et aksjeselskap", "question"),
        ("", "question"),
    ]
    bad = 0
    for text, expect in cases:
        got = classify_input(text)
        ok = got == expect
        bad += 0 if ok else 1
        log(f"  {'OK ' if ok else '!! '} {text!r:<36} -> {got:<9} (expected {expect})")
    log(f"  classifier mismatches: {bad}")


def part_invalid() -> None:
    from liveagent import LiveSession  # noqa: PLC0415

    log("### Invalid / edge numbers (must NOT fabricate)")
    bad_numbers = ["923609017", "123456789", "123", "000000000", "999999999"]
    for n in bad_numbers:
        sess = LiveSession(f"inv-{n}", no_web=True)
        a = sess.ask(n)
        log(f"  {n:<12} kind={a.kind:<8} answered={a.answered} "
            f"target={a.target!r} facts={len(a.evidence)}")
        log(f"      answer: {a.answer[:160]}")


def part_names() -> None:
    from liveagent import LiveSession  # noqa: PLC0415

    log("### Name queries (live web leg)")
    names = ["Equinor ASA", "Telenor ASA", "Norsk Hydro ASA", "VasaTest AS"]
    for n in names:
        sess = LiveSession(f"nm-{n.replace(' ','_')}", no_web=False)
        t0 = time.time()
        a = sess.ask(n)
        log(f"  {n!r}  kind={a.kind} answered={a.answered} "
            f"target={a.target!r} org={a.org_number!r} "
            f"sources={len(a.sources)} {time.time()-t0:.1f}s engine={a.engine}")
        for s in a.sources[:3]:
            log(f"      src[{s['kind']}] {s['url'][:90]}")
        log(f"      answer: {a.answer[:200]}")
        for note in a.notes:
            log(f"      note: {note}")


def part_questions() -> None:
    from liveagent import LiveSession  # noqa: PLC0415

    log("### Free-text questions (live web leg)")
    questions = ["who runs Equinor?", "what does Telenor ASA do?", "hva gjør Norsk Hydro?"]
    for q in questions:
        sess = LiveSession(f"q-{abs(hash(q))}", no_web=False)
        t0 = time.time()
        a = sess.ask(q)
        log(f"  {q!r}  kind={a.kind} answered={a.answered} target={a.target!r} "
            f"sources={len(a.sources)} {time.time()-t0:.1f}s engine={a.engine}")
        log(f"      answer: {a.answer[:220]}")
        for s in a.sources[:3]:
            log(f"      src[{s['kind']}] {s['url'][:90]}")
        for note in a.notes:
            log(f"      note: {note}")


def part_followup() -> None:
    from liveagent import LiveSession  # noqa: PLC0415

    log("### Session follow-ups (context must persist)")
    sess = LiveSession("fu-demo", no_web=True)
    turns = ["923609016", "who runs it?", "contact info", "how many employees?"]
    for t in turns:
        a = sess.ask(t)
        log(f"  {t!r}  kind={a.kind} target={a.target!r} answered={a.answered}")
        log(f"      answer: {a.answer[:200]}")
        for note in a.notes:
            log(f"      note: {note}")
    log(f"  session target after turns: org={sess.target_org!r} name={sess.target_name!r}")


PARTS = {
    "http": part_http, "api": part_api, "classify": part_classify,
    "invalid": part_invalid, "names": part_names,
    "questions": part_questions, "followup": part_followup,
}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "http"
    if which == "all":
        for name, fn in PARTS.items():
            log(f"\n===== {name} =====")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log(f"  PART FAILED: {type(e).__name__}: {e}")
    else:
        fn = PARTS[which]
        fn()
    print(f"part {which} done -> {LOG}")
