"""Capability audit: exercise each feature and record what actually happens.

Writes a plain report so a killed shell does not lose the findings.
Nothing here is mocked -- every check touches the real code path.
"""

from __future__ import annotations

import importlib
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

OUT = ROOT / "runs" / "audit" / "capability_audit.txt"


def main() -> int:
    lines: list[str] = []

    def rec(s: str = "") -> None:
        lines.append(s)

    # 1. Every module imports?
    rec("=" * 72)
    rec("1. MODULE IMPORTS")
    rec("=" * 72)
    mods = sorted(
        p.stem for p in (ROOT / "src").glob("*.py")
        if p.stem not in {"__init__"}
    )
    for m in mods:
        try:
            importlib.import_module(m)
            rec(f"   OK    {m}")
        except Exception as e:  # noqa: BLE001
            rec(f"   FAIL  {m}: {type(e).__name__}: {e}")

    # 2. Which optional third-party deps are actually installed?
    rec("")
    rec("=" * 72)
    rec("2. OPTIONAL DEPENDENCIES")
    rec("=" * 72)
    for dep in ["fastapi", "uvicorn", "sse_starlette", "httpx", "aiolimiter",
                "apscheduler", "duckdb", "redis", "faststream"]:
        try:
            mod = importlib.import_module(dep)
            ver = getattr(mod, "__version__", "?")
            rec(f"   present  {dep} {ver}")
        except Exception as e:  # noqa: BLE001
            rec(f"   MISSING  {dep}: {type(e).__name__}")

    # 3. Env: are the keys present that the agent would use for live work?
    rec("")
    rec("=" * 72)
    rec("3. ENVIRONMENT KEYS")
    rec("=" * 72)
    import os
    for k in ["BRAVE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
              "SIGNALPOST_OFFLINE"]:
        v = os.environ.get(k, "")
        rec(f"   {k:<20} {'set (' + str(len(v)) + ' chars)' if v else 'not set'}")

    # 4. CLI targets advertised by run.py -- do they exist?
    rec("")
    rec("=" * 72)
    rec("4. CLI TARGETS")
    rec("=" * 72)
    try:
        import cli  # noqa: PLC0415
        targets = sorted(
            n for n in dir(cli)
            if not n.startswith("_") and callable(getattr(cli, n))
        )
        rec(f"   cli exports: {', '.join(targets[:25])}")
    except Exception as e:  # noqa: BLE001
        rec(f"   cli import failed: {e}")

    # 5. Profiles: the committed dataset
    rec("")
    rec("=" * 72)
    rec("5. DATASET")
    rec("=" * 72)
    profs = list((ROOT / "profiles").glob("*.json"))
    rec(f"   profiles: {len(profs)}")
    golden = list((ROOT / "data" / "golden").glob("*.json"))
    rec(f"   golden:   {len(golden)}")
    seed = ROOT / "data" / "offline_seed.json"
    if seed.exists():
        try:
            d = json.loads(seed.read_text())
            rec(f"   offline_seed: {len(d) if isinstance(d, list) else 'object'}")
        except Exception as e:  # noqa: BLE001
            rec(f"   offline_seed unreadable: {e}")

    # 6. Live agent: the four input shapes, offline-safe (no network)
    rec("")
    rec("=" * 72)
    rec("6. LIVE AGENT -- classification (no network)")
    rec("=" * 72)
    try:
        from liveagent import classify_input  # noqa: PLC0415
        for text, want in [
            ("923609016", "orgnr"), ("984851006", "orgnr"),
            ("equinor.no", "domain"), ("https://www.dnb.no/x", "domain"),
            ("Equinor ASA", "name"), ("who runs Equinor?", "question"),
            ("hva gjør Telenor?", "question"),
        ]:
            got = classify_input(text)
            rec(f"   {'OK ' if got == want else '!! '} {text!r:<32} -> {got}")
    except Exception as e:  # noqa: BLE001
        rec(f"   FAILED: {type(e).__name__}: {e}")

    # 7. Ask module (company-scoped Q&A over stored facts)
    rec("")
    rec("=" * 72)
    rec("7. ASK (stored-fact question answering)")
    rec("=" * 72)
    try:
        # ask()/synthesis() operate on *parsed* Profile objects, not raw JSON.
        # Passing json.loads(...) here was the harness's bug, not the code's: the
        # facts come back as plain strings and synthesis then calls .value on them.
        from ask import ask  # noqa: PLC0415
        from profiles import Profile  # noqa: PLC0415
        prof_obj = Profile.read(profs[0]) if profs else None
        if prof_obj is not None:
            for q in ["who runs it?", "what does it do?"]:
                ans = ask(prof_obj, q)
                rec(f"   {q!r} -> section={ans.section!r} answered={ans.answered}")
    except Exception as e:  # noqa: BLE001
        rec(f"   FAILED: {type(e).__name__}: {e}")
        rec(traceback.format_exc()[-500:])

    # 8. Synthesis / trace / coverage / delta / score
    rec("")
    rec("=" * 72)
    rec("8. DERIVED VIEWS")
    rec("=" * 72)
    try:
        from profiles import Profile  # noqa: PLC0415
        from synthesis import synthesis  # noqa: PLC0415
        s = synthesis(Profile.read(profs[0]))
        rec(f"   synthesis: views={len(s.get('views', []))} missing={len(s.get('missing', []))}")
    except Exception as e:  # noqa: BLE001
        rec(f"   synthesis FAILED: {type(e).__name__}: {e}")

    # 9. The live_api optional layer -- does it import without deps?
    rec("")
    rec("=" * 72)
    rec("9. LIVE API LAYER (optional FastAPI bridge)")
    rec("=" * 72)
    try:
        import live_api  # noqa: PLC0415
        rec(f"   live_api imported; app={getattr(live_api, 'app', None) is not None}")
    except Exception as e:  # noqa: BLE001
        rec(f"   live_api not runnable: {type(e).__name__}: {e}")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"audit written -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
