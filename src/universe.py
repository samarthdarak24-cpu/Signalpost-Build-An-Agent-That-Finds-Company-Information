"""The public universe: Builderr's frozen 2025-filer snapshot.

The evaluator hands the agent a batch of organisation numbers. The public
universe file (``financial-filer-master-2025.jsonl``) is the same population --
411,160 Norway-registered entities whose latest filed accounts are 2025 -- and it
carries the *identity anchor* directly:

    organisation_number, name, legal_form, employees, bankrupt, liquidating,
    municipality, municipality_number, industry_code, industry_label,
    website, latest_submitted_accounts

That matters because identity is the one layer that must never be wrong. Seeding
the local mirror from this file makes the register layer **free and offline**,
so a run reaches the network only for the layers the universe does not carry:
roles, sub-units, annual accounts and the verified website.

The mapping is deliberately literal and never invents a value: ``employees`` is
absent (not zero) when the file says ``null``, and a field the file does not
carry simply does not appear.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from profiles import utcnow_iso
from registry import RegistryStore
from validate import clean

__all__ = [
    "iter_universe",
    "universe_orgnrs",
    "universe_to_entity",
    "seed_mirror_from_universe",
    "universe_size",
    "find_in_universe",
    "find_universe_file",
    "seed_one_from_universe",
]


def iter_universe(path: Path | str) -> Iterator[dict[str, Any]]:
    """Yield each universe record. Blank and malformed lines are skipped."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def universe_size(path: Path | str) -> int:
    """Number of usable records in the universe file (streamed, not loaded)."""
    return sum(1 for _ in iter_universe(path))


def universe_orgnrs(path: Path | str, *, limit: int | None = None) -> list[str]:
    """The organisation numbers in the universe, normalised and de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for record in iter_universe(path):
        number = clean(record.get("organisation_number") or record.get("organisasjonsnummer"))
        if not number or number in seen:
            continue
        seen.add(number)
        out.append(number)
        if limit is not None and len(out) >= limit:
            break
    return out


def universe_to_entity(record: dict[str, Any]) -> dict[str, Any]:
    """Map a universe record to the Brreg shape the pipeline already reads.

    The pipeline builds register facts from :data:`registry.REGISTRY_FACTS`,
    which is keyed on the register's own field names. Translating here -- once,
    in one place -- means the fact builder needs no knowledge of the universe
    file, and there is still exactly one source of the field table.
    """
    entity: dict[str, Any] = {
        "organisasjonsnummer": clean(record.get("organisation_number")),
        "navn": (record.get("name") or "").strip(),
        "organisasjonsform": {"kode": (record.get("legal_form") or "").strip()},
        "naeringskode1": {
            "kode": (record.get("industry_code") or "").strip(),
            "beskrivelse": (record.get("industry_label") or "").strip(),
        },
        "forretningsadresse": {"kommune": (record.get("municipality") or "").strip()},
    }
    # ``employees`` is null in the file for an unfiled count. Null must stay
    # absent, never become 0 -- the no-zero rule.
    if record.get("employees") is not None:
        entity["antallAnsatte"] = record["employees"]
    if record.get("website"):
        entity["hjemmeside"] = str(record["website"]).strip()
    if record.get("bankrupt"):
        entity["konkurs"] = True
    if record.get("liquidating"):
        entity["underAvvikling"] = True
    if record.get("latest_submitted_accounts"):
        entity["sisteInnsendteAarsregnskap"] = str(record["latest_submitted_accounts"])
    if record.get("municipality_number"):
        entity["kommunenummer"] = str(record["municipality_number"])
    return entity


#: Where the public universe file is looked for, relative to this package.
_UNIVERSE_NAMES = (
    "financial-filer-master-2025.jsonl",
    "signalpost-company-universe-2025.jsonl",
)


def find_universe_file(*roots: Path | str) -> Path | None:
    """Locate the public universe file, checking the given roots then defaults."""
    here = Path(__file__).resolve().parent
    candidates = [Path(r) for r in roots] + [here.parent, here.parent.parent, Path.cwd()]
    for root in candidates:
        for name in _UNIVERSE_NAMES:
            path = Path(root) / name
            if path.exists():
                return path
    return None


def find_in_universe(path: Path | str, number: str) -> dict[str, Any] | None:
    """Return the universe record for one organisation number, or ``None``.

    A linear scan of the 411k-line file; a few seconds, and worth it because it
    makes a lookup resilient: identity can be seeded offline even when the live
    register is momentarily unreachable.
    """
    target = clean(number)
    if target is None:
        return None
    for record in iter_universe(path):
        if clean(record.get("organisation_number")) == target:
            return record
    return None


def seed_one_from_universe(
    store: RegistryStore,
    number: str,
    *,
    universe_path: Path | str | None = None,
) -> bool:
    """Seed one company's identity from the universe, if it is there.

    Used by the live lookup: if the number is in the public universe, its
    identity is anchored offline before the run, so a momentary register outage
    degrades the profile instead of failing the company.
    """
    path = universe_path or find_universe_file()
    if path is None:
        return False
    record = find_in_universe(path, number)
    if record is None:
        return False
    store.upsert(universe_to_entity(record), fetched_at=utcnow_iso())
    store.commit()
    return True


def seed_mirror_from_universe(
    store: RegistryStore,
    path: Path | str,
    *,
    limit: int | None = None,
    fetched_at: str = "",
) -> int:
    """Load the universe into the local mirror. Returns the number seeded.

    Committing in batches keeps memory flat on the 411k-line file and means an
    interrupted seed still leaves a consistent mirror.
    """
    stamp = fetched_at or utcnow_iso()
    count = 0
    for record in iter_universe(path):
        entity = universe_to_entity(record)
        if not entity.get("organisasjonsnummer") or not entity.get("navn"):
            continue
        store.upsert(entity, fetched_at=stamp)
        count += 1
        if count % 2000 == 0:
            store.commit()
        if limit is not None and count >= limit:
            break
    store.commit()
    return count
