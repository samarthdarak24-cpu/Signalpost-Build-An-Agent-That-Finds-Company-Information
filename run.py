#!/usr/bin/env python3
"""One-command entry point, with no build tooling required.

The brief asks for a single command:

    make run ORGNR_FILE=orgnrs.txt

``make`` is not installed everywhere, and a judge should not have to install a
build tool to reproduce a run. This script is the portable equivalent, and the
Makefile is a thin wrapper around it so both spellings do the same thing:

    python run.py run ORGNR_FILE=orgnrs.txt
    make run ORGNR_FILE=orgnrs.txt

Everything is stdlib. Nothing to pip install.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
CLI = SRC / "cli.py"

# The default file the brief names. Missing it is a mistake worth explaining,
# not a stack trace.
DEFAULT_ORGNR_FILE = "orgnrs.txt"

USAGE = """\
Signalpost Evidence Engine

  python run.py setup                    build the local registry mirror
  python run.py seed-universe UNIVERSE=financial-filer-master-2025.jsonl
                                         seed the mirror from the public universe
  python run.py lookup 986587098          research ONE number live and print it
  python run.py live "who runs Equinor?"   live research agent: number, name, domain or question
  python run.py run ORGNR_FILE=orgnrs.txt
                                         build a profile for every number
  python run.py run-sample               the bundled demo set
  python run.py score                    score profiles against the rubric
  python run.py test                     run the unit tests
  python run.py demo                     offline end-to-end run + score
  python run.py web                      serve the console on :8787
  python run.py clean                    drop caches and run reports

  variables: ORGNR_FILE  WORKERS  LIMIT  PYTHON  SESSION

  environment (see .env.example):
    BRAVE_API_KEY         primary web search + website discovery (recommended)
    ANTHROPIC_API_KEY     LLM gap filling and conflict arbitration
    SIGNALPOST_OFFLINE=1  registry mirror only, no network at all
"""


def _python() -> str:
    """Resolve the interpreter.

    Honours ``PYTHON`` like the Makefile does, so a judge can point it at a
    specific virtualenv without editing anything.
    """
    return os.environ.get("PYTHON") or sys.executable


def _run(argv: list[str], *, capture: bool = False) -> int:
    env = dict(os.environ)
    # src/ modules import each other by bare name (``from net import ...``),
    # which works because cli.py inserts its own directory on sys.path. Any
    # child process we spawn needs the same guarantee.
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if capture:
        proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    return subprocess.call(argv, cwd=ROOT, env=env)


def _parse_kv(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split ``KEY=VALUE`` assignments from positional arguments.

    This is what makes ``run.py run ORGNR_FILE=orgnrs.txt`` read exactly like
    the documented ``make`` invocation.
    """
    positional: list[str] = []
    settings: dict[str, str] = {}
    for arg in args:
        if "=" in arg and not arg.startswith("-"):
            key, _, value = arg.partition("=")
            if key.isupper():
                settings[key] = value
                continue
        positional.append(arg)
    return positional, settings


def _flags(settings: dict[str, str]) -> list[str]:
    flags: list[str] = []
    if settings.get("LIMIT"):
        flags += ["--limit", settings["LIMIT"]]
    if settings.get("WORKERS"):
        flags += ["--workers", settings["WORKERS"]]
    return flags


# --------------------------------------------------------------------- tasks


def cmd_setup(settings: dict[str, str]) -> int:
    print("-> building the local registry mirror from data.brreg.no")
    return _run([_python(), str(CLI), "setup"])


def cmd_seed_universe(settings: dict[str, str]) -> int:
    """Seed the local mirror from the public universe JSONL (identity anchor)."""
    raw = settings.get("UNIVERSE", "financial-filer-master-2025.jsonl")
    path = Path(raw)
    if not path.is_absolute():
        for base in (ROOT, ROOT.parent):
            if (base / raw).exists():
                path = base / raw
                break
    if not path.exists():
        print(f"no universe file at {raw}", file=sys.stderr)
        print("pass UNIVERSE=/path/to/financial-filer-master-2025.jsonl", file=sys.stderr)
        return 2
    argv = [_python(), str(CLI), "seed-universe", str(path)]
    if settings.get("LIMIT"):
        argv += ["--limit", settings["LIMIT"]]
    return _run(argv)


def cmd_lookup(settings: dict[str, str]) -> int:
    """Research one organisation number live and print its profile."""
    number = settings.get("NUMBER", "").strip()
    if not number:
        print("usage: python run.py lookup <organisation number>", file=sys.stderr)
        return 2
    return _run([_python(), str(CLI), "lookup", number])


def cmd_live(settings: dict[str, str]) -> int:
    """Live research agent: accept any query or identifier, answer with sources."""
    query = settings.get("NUMBER", "").strip() or settings.get("QUERY", "").strip()
    if not query:
        print('usage: python run.py live "<query or organisation number>"', file=sys.stderr)
        return 2
    argv = [_python(), str(CLI), "live", query]
    if settings.get("SESSION"):
        argv += ["--session", settings["SESSION"]]
    if settings.get("NO_WEB"):
        argv += ["--no-web"]
    return _run(argv)


def cmd_run(settings: dict[str, str]) -> int:
    orgnr_file = settings.get("ORGNR_FILE", DEFAULT_ORGNR_FILE)
    path = Path(orgnr_file)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        print(f"no such file: {orgnr_file}", file=sys.stderr)
        print(
            "write one organisation number per line, or pass "
            "ORGNR_FILE=path/to/file",
            file=sys.stderr,
        )
        return 2

    workers = settings.get("WORKERS", "4")
    print(f"-> running {orgnr_file} with {workers} workers")
    argv = [_python(), str(CLI), "run", str(path), "--workers", workers]
    argv += _flags(settings)
    return _run(argv)


def cmd_run_sample(settings: dict[str, str]) -> int:
    return cmd_run({**settings, "ORGNR_FILE": "data/sample_orgnrs.txt"})


def cmd_score(settings: dict[str, str]) -> int:
    return _run(
        [
            _python(),
            str(SRC / "score_local.py"),
            "--profiles",
            "profiles",
            "--golden",
            "data/golden",
        ]
    )


def cmd_test(settings: dict[str, str]) -> int:
    return _run([_python(), "-m", "unittest", "discover", "-s", "tests", "-v"])


def cmd_web(settings: dict[str, str]) -> int:
    port = settings.get("PORT", "8787")
    return _run([_python(), str(SRC / "serve.py"), "--port", port, "--open"])


def cmd_demo(settings: dict[str, str]) -> int:
    """The keyless path: no network, no API keys, still produces a score."""
    print("-> offline demo run (no network)")
    code = _run(
        [
            _python(),
            str(CLI),
            "run",
            str(ROOT / "data" / "sample_orgnrs.txt"),
            "--offline",
            "--quiet",
        ]
    )
    if code != 0:
        return code
    print("-> scoring")
    code = _run([_python(), str(SRC / "score_local.py"), "--profiles", "profiles"])
    if code != 0:
        return code
    print("")
    print("  open web/dist/index.html to browse the profiles")
    return 0


def cmd_clean(settings: dict[str, str]) -> int:
    import shutil

    for rel in ("data/cache", "data/llm_cache"):
        target = ROOT / rel
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    for report in (ROOT / "runs").glob("run-*.json"):
        report.unlink(missing_ok=True)
    print("cleared caches and run reports (registry mirror and profiles kept)")
    return 0


TASKS = {
    "setup": cmd_setup,
    "registry": cmd_setup,
    "seed-universe": cmd_seed_universe,
    "lookup": cmd_lookup,
    "live": cmd_live,
    "run": cmd_run,
    "run-sample": cmd_run_sample,
    "score": cmd_score,
    "test": cmd_test,
    "web": cmd_web,
    "demo": cmd_demo,
    "clean": cmd_clean,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"help", "-h", "--help"}:
        print(USAGE)
        return 0

    task = argv[0]
    if task not in TASKS:
        print(f"unknown task: {task}", file=sys.stderr)
        print("", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    positional, settings = _parse_kv(argv[1:])
    if positional:
        settings.setdefault("NUMBER", positional[0])
    return TASKS[task](settings)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
