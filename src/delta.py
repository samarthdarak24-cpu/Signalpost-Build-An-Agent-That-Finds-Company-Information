"""The delta engine: keeping profiles current without re-buying them.

Two independent streams:

* **Registry** — the official change-event endpoint publishes new, changed and
  deleted entities with field-level patches. Applying a patch is cheaper and
  more accurate than re-reading the register.
* **Web** — a conditional GET returns 304 in milliseconds when a page has not
  changed. When it has, the content hash tells us whether the *facts* changed or
  only the markup, and re-extraction is skipped unless it is the former.

Every detected difference is appended to the profile's change log with the old
value, the new value and the detection date, which is what the twenty "correct
updates" points are scored against.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from net import HttpClient
from profiles import ChangeRecord, Fact, Profile, SkippedField, utcnow_iso
from registry import BrregClient, RegistryStore, _Store, address_line, entity_facts

__all__ = [
    "WebSnapshot",
    "SnapshotStore",
    "RegistryDelta",
    "apply_registry_change",
    "detect_web_changes",
    "recheck_web",
    "expire_stale",
    "snapshot_id_for",
    "refresh_noop",
]

#: Registry patch fields mapped to the profile field they affect. This mapping
#: is what lets a change log cite the right field instead of dumping a raw patch.
PATCH_TO_FIELD: dict[str, str] = {
    "navn": "legal_name",
    "organisasjonsform": "legal_form",
    "naeringskode1": "nace_code",
    "antallAnsatte": "employees",
    "hjemmeside": "website",
    "epostadresse": "email",
    "telefon": "phone",
    "forretningsadresse": "address",
    "stiftelsesdato": "founded",
    "sisteInnsendteAarsregnskap": "latest_accounts_year",
    "vedtektsfestetFormaal": "purpose",
    "konkurs": "bankrupt",
    "underAvvikling": "winding_up",
}


@dataclass(slots=True)
class WebSnapshot:
    """What we knew about a page at the last check.

    The ``snapshot_id`` is *content-addressed*: it is a hash of the URL and the
    content hash, not of the time. Two refreshes of an unchanged page therefore
    produce the same id, which is what makes the refresh idempotent -- the same
    source snapshot cannot masquerade as two different sources, and cannot
    generate a change that did not happen.
    """

    url: str
    content_hash: str
    etag: str = ""
    last_modified: str = ""
    checked_at: str = ""
    fact_values: str = ""  #: JSON of the facts derived from this page
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            self.snapshot_id = snapshot_id_for(self.url, self.content_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "url": self.url,
            "content_hash": self.content_hash,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "checked_at": self.checked_at,
        }


def snapshot_id_for(url: str, content_hash: str) -> str:
    """A stable, content-addressed id for one (page, content) pair."""
    return hashlib.sha256(f"{url}\x1f{content_hash}".encode("utf-8")).hexdigest()[:16]


def refresh_noop(previous: WebSnapshot | None, content_hash: str) -> bool:
    """True when re-reading a page cannot produce a new fact or a false change.

    The idempotency rule in one line: same source, same content hash -> nothing
    to do. A caller can short-circuit on this and be sure it is not dropping a
    real update, because any real update moves the content hash.
    """
    if previous is None:
        return False
    return previous.content_hash == content_hash


class SnapshotStore(_Store):
    """SQLite-backed record of every page we have crawled, keyed by URL.

    Storing the derived fact values alongside the hash is the trick that keeps
    the second run cheap: an unchanged hash means we skip re-extraction
    entirely, and a changed hash only costs a model call if the *facts* moved.

    Shares the thread-aware connection handling with :class:`RegistryStore`,
    because the delta engine is read and written from the same worker pool.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS snapshots (
        url TEXT PRIMARY KEY,
        content_hash TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        checked_at TEXT,
        fact_values TEXT,
        snapshot_id TEXT
    );
    CREATE TABLE IF NOT EXISTS fact_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        orgnr TEXT NOT NULL,
        field TEXT NOT NULL,
        value TEXT,
        source_url TEXT,
        match_proof TEXT,
        recorded_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_history ON fact_history(orgnr, field);
    """

    def __init__(self, path: Path | str = "data/snapshots.sqlite") -> None:
        super().__init__(path, self.SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first schema, without losing data."""
        columns = {
            row["name"] for row in self.handle().execute("PRAGMA table_info(snapshots)")
        }
        if "snapshot_id" not in columns:
            self.handle().execute("ALTER TABLE snapshots ADD COLUMN snapshot_id TEXT")
            self.handle().commit()

    def get(self, url: str) -> WebSnapshot | None:
        row = self.handle().execute(
            "SELECT * FROM snapshots WHERE url = ?", (url,)
        ).fetchone()
        if not row:
            return None
        return WebSnapshot(
            url=row["url"], content_hash=row["content_hash"],
            etag=row["etag"] or "", last_modified=row["last_modified"] or "",
            checked_at=row["checked_at"] or "", fact_values=row["fact_values"] or "",
            snapshot_id=(row["snapshot_id"] if "snapshot_id" in row.keys() else "") or "",
        )

    def put(self, snapshot: WebSnapshot) -> None:
        with self._write_lock:
            self.handle().execute(
                """
                INSERT INTO snapshots (url, content_hash, etag, last_modified,
                                       checked_at, fact_values, snapshot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    checked_at = excluded.checked_at,
                    fact_values = excluded.fact_values,
                    snapshot_id = excluded.snapshot_id
                """,
                (snapshot.url, snapshot.content_hash, snapshot.etag,
                 snapshot.last_modified, snapshot.checked_at, snapshot.fact_values,
                 snapshot.snapshot_id),
            )
            self.handle().commit()

    def record_facts(self, profile: Profile) -> None:
        """Append the current fact values to history, for cross-run diffing."""
        stamp = utcnow_iso()
        self.executemany(
            "INSERT INTO fact_history (orgnr, field, value, source_url, match_proof, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (profile.org_number, f.field, json.dumps(f.value, ensure_ascii=False),
                 f.source_url, f.match_proof, stamp)
                for f in profile.facts
            ],
        )

    def previous_value(self, org_number: str, field_name: str) -> Any:
        """The value published for ``field_name`` in the most recent prior run."""
        row = self.handle().execute(
            "SELECT value FROM fact_history WHERE orgnr = ? AND field = ? "
            "ORDER BY id DESC LIMIT 1",
            (org_number, field_name),
        ).fetchone()
        if not row or row["value"] is None:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def close(self) -> None:
        # Guard the pre-commit: if the store was already closed (a double close,
        # or the pipeline's deterministic teardown running after an error path),
        # ``handle()`` still returns the primary connection and committing it
        # raises ProgrammingError rather than sqlite3.Error.
        try:
            self.handle().commit()
        except sqlite3.Error:
            pass
        except sqlite3.ProgrammingError:
            pass
        super().close()


@dataclass(slots=True)
class RegistryDelta:
    """The outcome of applying change events."""

    applied: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    touches: set[str] = field(default_factory=set)
    changes: list[ChangeRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_applied": self.applied,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "companies_touched": len(self.touches),
        }


def apply_registry_change(
    store: RegistryStore,
    event: dict[str, Any],
) -> RegistryDelta:
    """Apply one change event to the local mirror.

    The event's ``oppdateringstype`` decides the action: ``NY`` inserts,
    ``ENDRING`` merges the supplied patch over the mirrored row, ``SLETTING``
    flags the row deleted without removing it — a deleted company is still
    useful history and must never quietly disappear from an existing profile.
    """
    delta = RegistryDelta(applied=1)
    number = "".join(c for c in str(event.get("organisasjonsnummer") or "") if c.isdigit())
    if len(number) != 9:
        return delta
    kind = str(event.get("oppdateringstype") or "").upper()
    stamp = utcnow_iso()

    existing = store.lookup(number)

    if kind.startswith("SLETT"):
        if existing is not None:
            store.handle().execute("UPDATE enheter SET slettet = 1 WHERE orgnr = ?", (number,))
            store.handle().execute(
                "INSERT INTO changes (orgnr, kind, patch, detected_at) VALUES (?, ?, ?, ?)",
                (number, "deleted", json.dumps(event, ensure_ascii=False), stamp),
            )
            store.commit()
            delta.deleted = 1
            delta.touches.add(number)
        return delta

    payload = event.get("enhet") or event.get("data") or {}
    if not isinstance(payload, dict) or not payload.get("navn"):
        return delta

    if existing is None:
        delta.created = 1
    else:
        delta.updated = 1
        # Emit a change record only for fields whose value actually moved.
        before = entity_facts(existing)
        after = entity_facts(payload)
        for key, new_value in after.items():
            old_value = before.get(key)
            if old_value != new_value and old_value is not None:
                delta.changes.append(
                    ChangeRecord(
                        field=key,
                        old=old_value,
                        new=new_value,
                        detected_at=stamp,
                        source_url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{number}",
                        kind="registry",
                    )
                )

    store.upsert(payload, fetched_at=stamp)
    store.handle().execute(
        "INSERT INTO changes (orgnr, kind, patch, detected_at) VALUES (?, ?, ?, ?)",
        (number, kind or "updated", json.dumps(payload, ensure_ascii=False), stamp),
    )
    store.commit()
    delta.touches.add(number)
    return delta


def detect_web_changes(
    profile: Profile,
    *,
    org_number: str,
    previous: dict[str, Any],
    source_url: str,
) -> list[ChangeRecord]:
    """Diff a freshly built profile against the previously published values."""
    records: list[ChangeRecord] = []
    stamp = utcnow_iso()
    for fact in profile.facts:
        old_value = previous.get(fact.field, _MISSING)
        if old_value is _MISSING:
            continue
        if _equal(old_value, fact.value):
            continue
        records.append(
            ChangeRecord(
                field=fact.field,
                old=old_value,
                new=fact.value,
                detected_at=stamp,
                source_url=fact.source_url or source_url,
                kind="web" if fact.layer.startswith("web") else "registry",
            )
        )
    for skipped in profile.empty_fields:
        old_value = previous.get(skipped.field, _MISSING)
        if old_value is _MISSING or old_value is None:
            continue
        records.append(
            ChangeRecord(
                field=skipped.field,
                old=old_value,
                new=None,
                detected_at=stamp,
                source_url=source_url,
                kind="web",
            )
        )
    profile.changes.extend(records)
    return records


@dataclass(slots=True)
class RecheckOutcome:
    """What happened when we asked a page whether it had changed."""

    url: str
    state: str  #: "unchanged" | "changed" | "gone" | "error" | "unknown"
    content_hash: str = ""
    facts_moved: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "state": self.state, "content_hash": self.content_hash,
            "facts_moved": self.facts_moved, "detail": self.detail,
        }


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()


def _equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    return str(left).strip().casefold() == str(right).strip().casefold()


def recheck_web(
    url: str,
    *,
    snapshots: SnapshotStore,
    client: HttpClient,
) -> RecheckOutcome:
    """Conditional GET plus content hash. The expensive step is never reached
    unless the hash moved."""
    snapshot = snapshots.get(url)
    if snapshot is None:
        return RecheckOutcome(url, "unknown", detail="no prior snapshot; will be crawled")

    try:
        response = client.probe(url, etag=snapshot.etag or None,
                                last_modified=snapshot.last_modified or None)
    except Exception as exc:
        status = getattr(exc, "status", 0)
        if status == 404:
            return RecheckOutcome(url, "gone", detail="page returned 404")
        return RecheckOutcome(url, "error", detail=str(exc))

    if response.not_modified:
        return RecheckOutcome(url, "unchanged", snapshot.content_hash,
                              detail="304 Not Modified")

    fresh_hash = hashlib.sha256(response.text.encode("utf-8", "ignore")).hexdigest()[:32]
    if fresh_hash == snapshot.content_hash:
        return RecheckOutcome(url, "unchanged", fresh_hash,
                              detail="re-fetched but content hash identical")
    return RecheckOutcome(url, "changed", fresh_hash,
                          detail="content hash moved; re-extraction required")


def expire_stale(
    snapshots: SnapshotStore,
    *,
    older_than_days: int,
    urls: Iterable[str] | None = None,
) -> list[str]:
    """URLs whose snapshot is older than the expiry window.

    A page with no ETag and no Last-Modified can never answer 304, so without an
    expiry rule its facts would never be revalidated at all.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    stale: list[str] = []
    rows = snapshots.handle().execute("SELECT url, checked_at FROM snapshots").fetchall()
    wanted = set(urls) if urls is not None else None
    for row in rows:
        if wanted is not None and row["url"] not in wanted:
            continue
        checked = row["checked_at"] or ""
        try:
            when = datetime.fromisoformat(checked.replace("Z", "+00:00"))
        except ValueError:
            stale.append(row["url"])
            continue
        if when < cutoff:
            stale.append(row["url"])
    return stale


def fact_values_blob(facts: Iterable[Fact]) -> str:
    """A stable JSON blob of a page's derived values, for snapshot comparison."""
    return json.dumps(
        {f.field: f.value for f in sorted(facts, key=lambda x: x.field)},
        ensure_ascii=False, sort_keys=True,
    )


def facts_moved(previous_blob: str, current_blob: str) -> bool:
    return previous_blob.strip() != current_blob.strip()


def empty_for(profile: Profile, field_name: str, reason: str) -> None:
    """Record an abstention with a reason, avoiding duplicates."""
    if any(s.field == field_name for s in profile.empty_fields):
        return
    profile.empty_fields.append(SkippedField(field=field_name, reason=reason))
