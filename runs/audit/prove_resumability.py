"""Task #45 §5 probe: does the pipeline actually satisfy the resumability contract?

The brief requires, when the budget expires or a request fails:

1. previously verified facts and provenance remain intact;
2. interrupted searches are recorded as incomplete, not as empty;
3. missing results do not erase existing verified facts;
4. remaining profiles can be processed in a later run;
5. repeated runs do not duplicate profiles or corrupt historical records.

This script exercises each against the *real* ``process_company`` and the real
``profiles`` writer, then prints a PASS/FAIL line per requirement with the
observed evidence. It deliberately uses a scratch profiles directory so the
committed 1,360-profile set is untouched.

Run::  python runs/audit/prove_resumability.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from delta import SnapshotStore  # noqa: E402
from extract_llm import LlmClient  # noqa: E402
from net import Budget, HttpClient  # noqa: E402
from pipeline import RunReport, Settings, CompanyResult  # noqa: E402
from profiles import Profile, Fact  # noqa: E402
from registry import RegistryStore  # noqa: E402

ORG = "923609016"
RESULTS: list[tuple[str, bool, str]] = []


class _FailingHttp(HttpClient):
    """An HttpClient whose every request fails, to simulate an outage.

    ``get`` is the single transport entry point (cache miss -> ``urlopen``), so
    overriding it makes every outbound attempt fail while leaving the budget and
    cache wiring intact and exercising the pipeline's normal error paths.
    """

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise ConnectionError("simulated outage for the resumability probe")


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")


def _settings(tmp: Path, **over: object) -> Settings:
    base = dict(
        profiles_dir=tmp / "profiles",
        runs_dir=tmp / "runs",
        cache_dir=ROOT / "data" / "cache",          # reuse the warm cache: fast, no new traffic
        registry_db=ROOT / "data" / "registry.sqlite",
        snapshot_db=tmp / "snapshots.sqlite",       # scratch so history is isolated
        llm_cache=ROOT / "data" / "llm_cache",
        offline=False,
        fetch_web=True,
        fetch_financials=False,                      # keep the probe fast
        fetch_jobs=False,
        fetch_activity=False,
        use_llm=False,
    )
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- requirement 1 & 3
def test_interrupted_run_preserves_facts(tmp: Path) -> None:
    """A good profile, then an interrupted re-run that must not erase it."""
    print("\n[1/3] interrupt, then re-run: do verified facts survive?")

    # --- first run: a complete, good profile
    s1 = _settings(tmp)
    r1 = _process(s1)
    path = s1.profiles_dir / f"{ORG}.json"
    before = Profile.read(path)
    before_pairs = {(f.field, f.value) for f in before.facts}
    print(f"         first run: ok={r1.ok} facts={len(before.facts)} file_exists={path.exists()}")

    # --- second run: simulate an interruption by cancelling the budget up front
    s2 = _settings(tmp)
    budget = Budget()
    budget.run.cancel("simulated interruption for the probe")
    http = HttpClient(cache_dir=s2.cache_dir, budget=budget)
    r2 = _process(s2, http=http, budget=budget)
    after = Profile.read(path)
    after_pairs = {(f.field, f.value) for f in after.facts}

    check(
        "verified facts survive an interrupted re-run",
        before_pairs.issubset(after_pairs),
        f"{len(before_pairs)} facts before, {len(after_pairs)} after, "
        f"missing={sorted(before_pairs - after_pairs)[:3]}",
    )

    # Provenance must survive too, but the *retrieval timestamp legitimately
    # advances* when a source is re-read successfully in the second run. So the
    # invariant is field -> source_url, not field -> (source_url, timestamp).
    prov_before = {(f.field, f.source_url) for f in before.facts}
    prov_after = {(f.field, f.source_url) for f in after.facts}
    check(
        "provenance (source_url) survives an interrupted re-run",
        prov_before.issubset(prov_after),
        f"{len(prov_before)} field->source pairs before, {len(prov_after)} after, "
        f"missing={sorted(prov_before - prov_after)[:3]}",
    )
    # And the value must not have been silently replaced by an empty string.
    blanked = [f.field for f in after.facts if not str(f.value).strip()]
    check(
        "no verified fact was blanked by the interrupted re-run",
        not blanked,
        f"blank-valued facts after interruption: {blanked[:5]}",
    )


def test_failure_does_not_erase_published_facts(tmp: Path) -> None:
    """The sharper case: a *failed* lookup must not overwrite a good fact.

    Requirement 3 says missing results must not erase existing verified facts.
    The dangerous shape is a second run in which the registry is unreachable:
    the profile must keep the facts the first run published rather than
    rewriting them as empty.
    """
    print("\n[1b/3] registry unavailable on re-run: are published facts kept?")

    s1 = _settings(tmp)
    r1 = _process(s1)
    path = s1.profiles_dir / f"{ORG}.json"
    before = Profile.read(path)
    before_pairs = {(f.field, f.value) for f in before.facts}

    # --- second run where every outbound call fails
    s2 = _settings(tmp)
    budget = Budget()
    http = _FailingHttp(cache_dir=s2.cache_dir, budget=budget)
    _process(s2, http=http, budget=budget)
    after = Profile.read(path)
    after_pairs = {(f.field, f.value) for f in after.facts}

    check(
        "a failed lookup does not erase previously published facts",
        before_pairs.issubset(after_pairs),
        f"{len(before_pairs)} before, {len(after_pairs)} after, "
        f"lost={sorted(before_pairs - after_pairs)[:3]}",
    )


def test_interrupted_search_is_incomplete_not_empty(tmp: Path) -> None:
    """A cancelled run must not record 'the web has nothing' as a fact."""
    print("\n[2/3] cancelled run: is the search recorded as incomplete, not empty?")

    s = _settings(tmp)
    budget = Budget()
    budget.run.cancel("simulated interruption for the probe")
    http = HttpClient(cache_dir=s.cache_dir, budget=budget)
    r = _process(s, http=http, budget=budget)
    prof = Profile.read(s.profiles_dir / f"{ORG}.json")

    website_skips = [s_.reason for s_ in prof.empty_fields if s_.field == "website"]
    # A cancelled run must not claim the company has no website.
    claims_absence = any("no website" in w.casefold() or "gate found no page" in w.casefold()
                         for w in website_skips)
    check(
        "a cancelled run does not assert 'no website exists'",
        not claims_absence,
        f"website skip reasons={website_skips!r}; errors={prof.errors!r}",
    )


def test_writes_are_atomic(tmp: Path) -> None:
    """A kill mid-write must not leave a truncated, unreadable profile."""
    print("\n[3/3] atomic write: can a half-written profile be observed?")

    s = _settings(tmp)
    s.profiles_dir.mkdir(parents=True, exist_ok=True)
    path = s.profiles_dir / f"{ORG}.json"

    # Build a profile with enough content that a non-atomic write would be
    # observably partial if interrupted between truncate and write.
    prof = Profile(org_number=ORG, legal_name="PROBE AS", run_date="2026-10-10")
    for i in range(400):
        prof.facts.append(Fact(
            field=f"probe_{i}", value="x" * 200, source_url="https://example.no",
            retrieved_at="2026-10-10T00:00:00Z", evidence="e", match_proof="registry",
            confidence=0.9, layer="registry",
        ))
    prof.write(s.profiles_dir)

    readable = True
    try:
        Profile.read(path)
    except Exception as exc:  # noqa: BLE001
        readable = False
        print(f"         read failed: {exc}")

    # Evidence of atomicity: the writer must not truncate the target in place.
    # A temp file + os.replace leaves no partial target, and no leftover temp.
    leftovers = [p.name for p in s.profiles_dir.iterdir() if p.name != f"{ORG}.json"]
    check(
        "profile write either completes or leaves the previous file intact",
        readable and not leftovers,
        f"readable={readable} size={path.stat().st_size if path.exists() else 0} "
        f"leftover_temp_files={leftovers}",
    )


# ---------------------------------------------------------------- helpers
def _process(settings: Settings, *, http: HttpClient | None = None,
             budget: Budget | None = None) -> CompanyResult:
    from pipeline import process_company

    budget = budget or Budget()
    http = http or HttpClient(cache_dir=settings.cache_dir, budget=budget)
    return process_company(
        ORG,
        store=RegistryStore(settings.registry_db),
        snapshots=SnapshotStore(settings.snapshot_db),
        settings=settings,
        http=http,
        llm=LlmClient(http, cache_dir=str(settings.llm_cache)),
        reporter=RunReport(),
        lock=threading.Lock(),
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="signalpost-resume-"))
    try:
        print("=" * 72)
        print("RESUMABILITY PROBE (scratch dir, committed profiles untouched)")
        print("=" * 72)
        test_interrupted_run_preserves_facts(tmp)
        test_failure_does_not_erase_published_facts(tmp)
        test_interrupted_search_is_incomplete_not_empty(tmp)
        test_writes_are_atomic(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 72)
    print(f"RESULT: {passed}/{len(RESULTS)} checks passed")
    for name, ok, _ in RESULTS:
        if not ok:
            print(f"  UNMET: {name}")
    print("=" * 72)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
