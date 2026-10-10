"""Phase 0 — the stdlib live watchdog.

Keeps profiles current by watching Brreg's change-event stream and re-validating
only the companies that actually moved. No third-party packages, so the keyless
demo still runs on one command; this is an *optional* companion process.

The loop (the three-layer live model from docs/LIVE_DATA_STACK.md):

    INGEST   poll oppdateringer/enheter with the oppdateringsid cursor
    PROCESS  re-run the existing pipeline for the changed organisasjonsnummer(s)
    DELIVER  merge the fresh envelope into envelopes.jsonl and append the
             detected ChangeRecords to changes.jsonl (what the dashboard reads)

The envelope file is the execution contract: one line per input, never dropped.
So a live tick does NOT call pipeline.run against the real runs dir (that would
replace the whole file with only the changed few). Instead it runs the tick in an
isolated runs/.live directory and merges the result back, preserving every other
company's envelope.

    python src/live.py --once --limit 5          # one tick, 5 companies, then exit
    python src/live.py --dry-run                 # report what would change, touch nothing
    python src/live.py --interval 60             # daemon: poll every 60s
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from registry import BrregClient  # noqa: E402

__all__ = [
    "load_watermark",
    "save_watermark",
    "collect_changes",
    "run_once",
    "merge_envelopes",
    "append_changes",
    "main",
]

WATERMARK_PATH = ROOT / "runs" / "watermark.txt"
LIVE_DIR = ROOT / "runs" / ".live"
CHANGES_PATH = ROOT / "runs" / "changes.jsonl"
ENVELOPES_PATH = ROOT / "runs" / "envelopes.jsonl"
DEFAULT_INTERVAL = 60
DEFAULT_PAGE_SIZE = 500
DEFAULT_PER_TICK = 50


# --------------------------------------------------------------------- watermark


def load_watermark(path: Path = WATERMARK_PATH) -> int:
    """The highest ``oppdateringsid`` we have already processed (0 = none)."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_watermark(cursor: int, path: Path = WATERMARK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(cursor), encoding="utf-8")


# --------------------------------------------------------------------- ingest


def collect_changes(
    client: BrregClient,
    *,
    last_cursor: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """Page forward through the change feed from ``last_cursor``.

    Returns the list of change events seen and the new high-water cursor.

    The feed is HAL-wrapped (``_embedded.oppdaterteEnheter``) and paged by
    ``page``/``size``. ``oppdateringsid`` is an *exclusive lower bound*: each call
    returns events with id strictly greater than the cursor, so the same cursor
    is passed on every page while ``page`` advances. The reported ``totalPages`` /
    ``totalElements`` are unreliable (they reflect the unfiltered set, not the
    remainder), so we stop on the first short or empty page rather than trusting
    them — and we obey Brreg's ``page * size <= 10000`` ceiling to avoid HTTP 400.
    """
    events: list[dict[str, Any]] = []
    cursor = last_cursor
    for page in range(max_pages):
        if page * page_size >= 10000:
            break  # Brreg refuses page*size >= 10000 (offset ceiling)
        payload = client.changes(cursor=last_cursor, page=page, size=page_size)
        batch = (payload.get("_embedded") or {}).get("oppdaterteEnheter", [])
        if not batch:
            break
        for ev in batch:
            events.append(ev)
            cid = ev.get("oppdateringsid")
            if isinstance(cid, int) and cid > cursor:
                cursor = cid
        if len(batch) < page_size:
            break  # last page reached
    return events, cursor


def seek_tail(client: BrregClient, hi: int = 150_000_000) -> int:
    """Find the current highest ``oppdateringsid`` so a fresh daemon starts at "now".

    The feed cannot be paged to its tail (Brreg caps ``page * size`` at 9999, and
    ``dato`` filtering is rejected), so we binary-search the cursor instead:
    ``oppdateringsid=X`` is an *exclusive* lower bound, so a non-empty first page
    means events exist with id > X. The largest X with events above it is M-1,
    where M is the current max id; one more fetch returns M.
    """
    def has(seq: int) -> bool:
        try:
            payload = client.changes(cursor=seq, page=0, size=1)
        except Exception:
            return False
        return bool((payload.get("_embedded") or {}).get("oppdaterteEnheter"))

    lo, hi_ = 1, hi
    while lo < hi_:
        mid = (lo + hi_ + 1) // 2
        if has(mid):
            lo = mid
        else:
            hi_ = mid - 1
    # lo == largest id with events strictly above it; the real max sits in this page.
    payload = client.changes(cursor=lo, page=0, size=DEFAULT_PAGE_SIZE)
    batch = (payload.get("_embedded") or {}).get("oppdaterteEnheter", [])
    return max((int(e["oppdateringsid"]) for e in batch if e.get("oppdateringsid") is not None), default=lo)


def changed_orgnrs(events: list[dict[str, Any]]) -> list[str]:
    """Distinct, checksum-valid organisation numbers from a batch of events.

    A change event carries ``organisasjonsnummer`` and ``endringstype`` (the
    delta code reads ``oppdateringstype`` — both spellings are tolerated so a
    feed-shape change cannot silently empty the queue).
    """
    from validate import clean

    seen: dict[str, str] = {}
    for ev in events:
        number = clean(ev.get("organisasjonsnummer"))
        if number is None:
            continue
        kind = str(ev.get("endringstype") or ev.get("oppdateringstype") or "").upper()
        # A deletion still warrants a re-validation so the envelope can move to
        # not_available rather than serving a company that left the register.
        seen.setdefault(number, kind)
    return list(seen)


# --------------------------------------------------------------------- process + deliver


def merge_envelopes(tick_env_path: Path, real_env_path: Path = ENVELOPES_PATH) -> int:
    """Merge a tick's envelopes into the real envelope file.

    The real file is the source of truth for the whole batch; this replaces the
    entry for each re-validated company and appends any company that was absent.
    Returns the number of envelopes updated/added.
    """
    if not tick_env_path.exists():
        return 0
    by_orgnr: dict[str, dict[str, Any]] = {}
    if real_env_path.exists():
        for line in real_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            env = json.loads(line)
            by_orgnr[env["orgnr"]] = env
    merged = 0
    for line in tick_env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        env = json.loads(line)
        by_orgnr[env["orgnr"]] = env
        merged += 1
    real_env_path.parent.mkdir(parents=True, exist_ok=True)
    with real_env_path.open("w", encoding="utf-8") as handle:
        for env in by_orgnr.values():
            handle.write(json.dumps(env, ensure_ascii=False) + "\n")
    return merged


def append_changes(envelopes: list[dict[str, Any]], changes_path: Path = CHANGES_PATH) -> int:
    """Append every ChangeRecord carried by a tick's envelopes to changes.jsonl.

    Each change is stamped with its orgnr so the dashboard can filter a stream by
    company. Returns the number of change records written.
    """
    written = 0
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    with changes_path.open("a", encoding="utf-8") as handle:
        for env in envelopes:
            orgnr = env.get("orgnr", "")
            for change in env.get("changes", []):
                change = dict(change)
                change["orgnr"] = orgnr
                handle.write(json.dumps(change, ensure_ascii=False) + "\n")
                written += 1
    return written


def process_orgnrs(
    orgnrs: list[str],
    *,
    no_web: bool = True,
    no_llm: bool = True,
    verbose: bool = True,
    limit: int = DEFAULT_PER_TICK,
) -> tuple[int, int]:
    """The process + deliver step, shared by Phase 0 (stdlib) and Phase 1 (async).

    Runs the existing pipeline for the given organisation numbers (in an isolated
    ``runs/.live`` directory so the real envelope file is never clobbered),
    merges the fresh envelopes back into ``runs/envelopes.jsonl`` and appends any
    detected ``ChangeRecord`` to ``runs/changes.jsonl`` for the dashboard.

    Returns ``(envelopes_merged, changes_written)``.
    """
    from pipeline import Settings, run  # noqa: E402

    batch = orgnrs[:limit] if limit else orgnrs
    settings = Settings.from_env()
    settings.runs_dir = LIVE_DIR
    settings.fetch_web = settings.fetch_web and not no_web
    settings.use_llm = settings.use_llm and not no_llm
    settings.quiet = not verbose

    run(batch, settings=settings)
    tick_env_path = LIVE_DIR / "envelopes.jsonl"
    merged = merge_envelopes(tick_env_path)
    tick_envelopes = [
        json.loads(line)
        for line in tick_env_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    written = append_changes(tick_envelopes)

    if verbose:
        print(f"[live] re-validated {len(batch)} orgnrs; merged {merged} envelopes, {written} changes")
    return merged, written


def run_once(
    *,
    last_cursor: int,
    limit: int = DEFAULT_PER_TICK,
    dry_run: bool = False,
    no_web: bool = True,
    no_llm: bool = True,
    verbose: bool = True,
) -> tuple[int, int, int]:
    """One live tick.

    Returns ``(events_seen, orgnrs_processed, new_cursor)``. With ``dry_run`` it
    only reports what *would* change and touches no output file.
    """
    client = BrregClient()
    events, new_cursor = collect_changes(client, last_cursor=last_cursor)
    orgnrs = changed_orgnrs(events)
    if verbose:
        print(
            f"[live] cursor {last_cursor} -> {new_cursor}: "
            f"{len(events)} events, {len(orgnrs)} distinct orgnrs"
        )
    if dry_run:
        return len(events), len(orgnrs), new_cursor

    if not orgnrs:
        return len(events), 0, new_cursor

    process_orgnrs(orgnrs, no_web=no_web, no_llm=no_llm, verbose=verbose, limit=limit)
    processed = len(orgnrs[:limit]) if limit else len(orgnrs)
    return len(events), processed, new_cursor


# --------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signalpost live watchdog (Phase 0).")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="poll seconds (daemon)")
    parser.add_argument("--cursor", type=int, default=None, help="start cursor (else watermark.txt)")
    parser.add_argument("--limit", type=int, default=DEFAULT_PER_TICK, help="max orgnrs per tick (0=all)")
    parser.add_argument("--web", action="store_true", help="also run the web layer (needs keys)")
    parser.add_argument("--llm", action="store_true", help="also run the LLM layer (needs keys)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    last_cursor = args.cursor if args.cursor is not None else load_watermark()
    if last_cursor == 0:
        # Fresh start: jump the watermark to "now" so we don't replay history.
        try:
            last_cursor = seek_tail(BrregClient())
            if not args.quiet:
                print(f"[live] seeded watermark at oppdateringsid={last_cursor} (tail of feed)")
        except Exception as exc:  # network blip at startup is not fatal
            if not args.quiet:
                print(f"[live] could not seek tail ({exc}); starting from 0")
            last_cursor = 0
    no_web = not args.web
    no_llm = not args.llm

    # A single tick, honouring an explicit --cursor for bounded testing.
    events, processed, new_cursor = run_once(
        last_cursor=last_cursor,
        limit=args.limit,
        dry_run=args.dry_run,
        no_web=no_web,
        no_llm=no_llm,
        verbose=not args.quiet,
    )
    if not args.dry_run:
        save_watermark(new_cursor)

    if args.once or args.dry_run:
        return 0

    if not args.quiet:
        print(f"[live] daemon polling every {args.interval}s (ctrl-c to stop)")
    try:
        while True:
            time.sleep(args.interval)
            _, _, new_cursor = run_once(
                last_cursor=new_cursor,
                limit=args.limit,
                dry_run=False,
                no_web=no_web,
                no_llm=no_llm,
                verbose=not args.quiet,
            )
            save_watermark(new_cursor)
    except KeyboardInterrupt:
        if not args.quiet:
            print("\n[live] stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
