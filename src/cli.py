"""Command line entry point.

Two commands, both required by the brief:

    python src/cli.py setup   build the local registry mirror
    python src/cli.py run     produce profiles and print the run report

``--offline`` runs the whole cascade against a bundled sample with no network
access at all, which is what makes the demo reproducible on a judge's machine
with no API keys.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from net import Budget, HttpClient  # noqa: E402
from registry import BrregClient, RegistryStore, sync_bulk, sync_changes  # noqa: E402


def cmd_setup(args: argparse.Namespace) -> int:
    """Mirror the register into SQLite, then apply any change events."""
    settings_path = ROOT / "data" / "registry.sqlite"
    store = RegistryStore(settings_path)
    budget = Budget()
    client = BrregClient(HttpClient(cache_dir=ROOT / "data" / "cache", budget=budget))

    if args.offline:
        seeded = seed_offline(store)
        print(f"seeded {seeded} entities from data/offline_seed.json (no network)")
        store.close()
        return 0

    print("building the local registry mirror")
    print(f"  destination   {settings_path}")
    print("  source        https://data.brreg.no/enhetsregisteret/api  (NLOD 2.0)")
    print("  forms         AS, ASA, ANS, DA, SA, NUF, ENK, BA, STI, FKF, KF, IKS, PBL")
    print("")

    max_records = args.max_records
    written = 0
    try:
        written = sync_bulk(store, client, max_records=max_records, verbose=not args.quiet)
    except KeyboardInterrupt:
        print("\ninterrupted; the mirror is consistent up to the last committed page")

    if written:
        try:
            events = sync_changes(store, client, max_pages=2)
            print(f"\napplied {len(events)} change events")
        except Exception as exc:
            print(f"\nchange sync skipped: {exc}")

    print("")
    print(f"  entities      {store.count():,}")
    print(f"  requests used {budget.used}")
    print(f"  last sync     {store.get_meta('last_bulk_sync') or '-'}")
    store.close()
    return 0 if store.count() else 1


def seed_offline(store: RegistryStore) -> int:
    """Load the bundled sample entities so the demo works with no network."""
    seed_path = ROOT / "data" / "offline_seed.json"
    if not seed_path.exists():
        return 0
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    from profiles import utcnow_iso

    stamp = utcnow_iso()
    for entity in payload.get("entities", []):
        store.upsert(entity, fetched_at=stamp)
    store.commit()
    store.set_meta("last_bulk_sync", stamp)
    store.set_meta("source", "bundled offline seed")
    return len(payload.get("entities", []))


def cmd_run(args: argparse.Namespace) -> int:
    from pipeline import main as pipeline_main

    forwarded = list(args.numbers)
    forwarded += ["--workers", str(args.workers)]
    if args.offline:
        forwarded.append("--offline")
    if args.no_web:
        forwarded.append("--no-web")
    if args.no_llm:
        forwarded.append("--no-llm")
    if args.financials:
        forwarded.append("--financials")
    if args.quiet:
        forwarded.append("--quiet")
    if args.limit:
        forwarded += ["--limit", str(args.limit)]
    return pipeline_main(forwarded)


def cmd_seed(args: argparse.Namespace) -> int:
    store = RegistryStore(ROOT / "data" / "registry.sqlite")
    seeded = seed_offline(store)
    store.close()
    print(f"seeded {seeded} entities from the bundled offline set")
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    """Research ONE organisation number live and print its full profile.

    This is the interactive path: give it any number and it fetches the register
    entity live (even if never seen before), then runs the whole cascade --
    identifiers, roles, sub-units, annual accounts, the ownership-gated website,
    rule extraction, jobs and activity, verification -- and prints the result
    with the evidence for every fact.
    """
    import os

    from coverage import coverage_summary
    from pipeline import Settings, run
    from profiles import Profile
    from synthesis import render_text, synthesis
    from validate import clean

    number = clean(args.number)
    if number is None:
        print(f"not a valid organisation number: {args.number!r}")
        return 2

    runs_dir = ROOT / "runs" / "lookup"
    settings = Settings(
        profiles_dir=runs_dir / "profiles",
        runs_dir=runs_dir,
        fetch_web=not args.no_web,
        fetch_financials=not args.no_financials,
        use_llm=bool(os.environ.get("ANTHROPIC_API_KEY")) and not args.no_llm,
        workers=1,
        quiet=True,
    )
    # Anchor identity offline from the public universe when the number is there,
    # so a momentary register outage cannot turn a real company into a failure.
    try:
        from universe import find_universe_file, seed_one_from_universe

        store = RegistryStore(ROOT / "data" / "registry.sqlite")
        if seed_one_from_universe(store, number):
            print(f"   identity anchored from {find_universe_file().name}")
        store.close()
    except Exception:
        pass

    print(f"-> researching {number} live")
    print("   registry -> identifiers -> roles -> sub-units -> accounts -> web -> verify")
    reporter = run([number], settings=settings)

    path = settings.profiles_dir / f"{number}.json"
    if not path.exists():
        print("\nno profile produced for this number")
        print(reporter.render())
        return 1

    profile = Profile.read(path)
    print()
    print(render_text(synthesis(profile)))
    print()
    print("EVIDENCE")
    for fact in profile.facts:
        value = str(fact.value)
        value = value if len(value) <= 46 else value[:43] + "..."
        print(f"  {fact.field:<20} {value:<46} {fact.match_proof:<14} "
              f"{fact.confidence:.2f}  {fact.source_url}")
    cov = coverage_summary(profile)
    print()
    print(f"facts {cov['facts_found']} · evidence-backed {cov['evidence_backed']} "
          f"· unavailable {cov['unavailable']}")
    if reporter.envelopes_path:
        print(f"envelope  {reporter.envelopes_path}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Answer any query live: a number, a company name, a domain, or a question.

    This is the live research agent in one command. It classifies the input,
    fetches the freshest information (register for a number, the open web plus
    Wikipedia for a name or question), and prints a concise, sourced answer. Pass
    ``--session <id>`` to keep asking follow-ups that reuse the same company.
    """
    from liveagent import LiveSession

    session = LiveSession(
        args.session or "cli",
        no_web=args.no_web,
        progress=(None if args.quiet else (lambda m: print(f"   [{m}]"))),
    )
    if args.orgnr_only:
        session.no_web = True

    if args.ask:
        # Interactive: read queries from stdin until EOF or an empty line.
        print("Signalpost live research — ask a company, a number, or a question.")
        print("(blank line or ctrl-d to stop)")
        for line in sys.stdin:
            query = line.strip()
            if not query:
                break
            print()
            print(session.ask(query).render())
        return 0

    answer = session.ask(args.query)
    print(answer.render())
    return 0 if answer.answered else 0


def cmd_seed_universe(args: argparse.Namespace) -> int:
    """Seed the local mirror from the public universe JSONL.

    This is the identity anchor: the register layer then answers offline, so a
    run only reaches the network for roles, sub-units, accounts and the website.
    """
    from universe import seed_mirror_from_universe

    store = RegistryStore(ROOT / "data" / "registry.sqlite")
    seeded = seed_mirror_from_universe(store, args.path, limit=args.limit)
    total = store.count()
    store.close()
    print(f"seeded {seeded} entities from {args.path}")
    print(f"  mirror now holds {total:,} entities")
    return 0 if seeded else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signalpost", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="build the local registry mirror")
    setup.add_argument("--max-records", type=int, default=None)
    setup.add_argument("--offline", action="store_true",
                       help="seed from the bundled sample instead of the network")
    setup.add_argument("--quiet", action="store_true")
    setup.set_defaults(func=cmd_setup)

    run = sub.add_parser("run", help="produce profiles")
    run.add_argument("numbers", nargs="*", help="organisation numbers or files containing them")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--offline", action="store_true")
    run.add_argument("--no-web", action="store_true")
    run.add_argument("--no-llm", action="store_true")
    run.add_argument("--financials", action="store_true")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(func=cmd_run)

    lookup = sub.add_parser("lookup", help="research one organisation number live")
    lookup.add_argument("number")
    lookup.add_argument("--no-web", action="store_true")
    lookup.add_argument("--no-financials", action="store_true")
    lookup.add_argument("--no-llm", action="store_true")
    lookup.set_defaults(func=cmd_lookup)

    live = sub.add_parser(
        "live",
        help="live research agent: a number, a name, a domain, or a question",
    )
    live.add_argument("query", nargs="?", default="",
                      help="anything: 995849364, 'Norsk Hydro ASA', equinor.no, "
                           "or 'who runs Equinor?' (omit with --ask)")
    live.add_argument("--session", default="cli", help="session id (keeps follow-up context)")
    live.add_argument("--ask", action="store_true", help="read follow-up queries from stdin")
    live.add_argument("--no-web", action="store_true", help="register only, no web search")
    live.add_argument("--orgnr-only", action="store_true", help="alias for --no-web")
    live.add_argument("--quiet", action="store_true")
    live.set_defaults(func=cmd_live)

    seed = sub.add_parser("seed", help="seed the offline sample registry")
    seed.set_defaults(func=cmd_seed)

    seedu = sub.add_parser(
        "seed-universe",
        help="seed the local mirror from the public universe JSONL (identity anchor)",
    )
    seedu.add_argument("path", help="path to financial-filer-master-2025.jsonl")
    seedu.add_argument("--limit", type=int, default=None)
    seedu.set_defaults(func=cmd_seed_universe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
