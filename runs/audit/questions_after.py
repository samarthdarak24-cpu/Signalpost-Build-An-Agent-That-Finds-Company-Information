"""Re-test free-text questions after the register-by-name fix."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

LOG = ROOT / "runs" / "audit" / "questions_after.log"


def log(line: str = "") -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


from liveagent import LiveSession  # noqa: E402

LOG.write_text("", encoding="utf-8")

cases = [
    ("who runs Equinor?", "Anders Opedal"),
    ("what does Telenor ASA do?", "tele"),
    ("hva gjør Norsk Hydro?", "hydro"),
    ("who is the CEO of DNB Bank?", "Kjerstin"),
]

hits = 0
for q, needle in cases:
    s = LiveSession("qa-" + str(abs(hash(q))), no_web=False)
    t0 = time.time()
    a = s.ask(q)
    got = needle.casefold() in a.answer.casefold()
    hits += got
    log(f"{'OK ' if got else '!! '} {q!r}")
    log(f"     answered={a.answered} target={a.target!r} org={a.org_number!r} "
        f"sources={len(a.sources)} {time.time() - t0:.0f}s")
    log(f"     answer: {a.answer[:320]}")
    for src in a.sources[:3]:
        log(f"     [{src['kind']}] {src['url'][:85]}")
    for n in a.notes:
        log(f"     note: {n}")
    log("")

log(f"SUMMARY: {hits}/{len(cases)} answers contained the expected fact")
print(f"done {hits}/{len(cases)} -> {LOG}")
