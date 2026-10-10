"""Enhetsregisteret client and the local registry store.

The register is free, needs no API key and publishes nightly bulk files under
NLOD 2.0. We build a local SQLite database from the bulk listing so per-company
lookups cost milliseconds and zero requests, then keep it current from the
official change-event stream.

Two surfaces are exposed:

* :class:`BrregClient` — live API access, used for a single-company lookup and
  for change events.
* :class:`RegistryStore` — the local SQLite mirror, used by the pipeline.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote as _quote

from net import HttpClient
from validate import OrgNumber, clean

__all__ = [
    "BASE",
    "BrregClient",
    "RegnskapClient",
    "RegistryStore",
    "REGISTRY_FACTS",
    "entity_facts",
    "RegnskapFigures",
    "fetch_regnskap",
]

BASE = "https://data.brreg.no/enhetsregisteret/api"
REGN_SKAP_BASE = "https://data.brreg.no/regnskapsregisteret"

#: Registry key(s) -> the profile field name we publish it under, with the
#: confidence we assign. Everything here is register-sourced, so the proof is
#: ``registry`` and the confidence is high by construction, not by estimate.
#:
#: A key may be given as a tuple of alternatives, tried left to right: the first
#: that carries a value wins. That is how ``phone`` prefers the switchboard
#: (``telefon``) but still publishes the mobile (``mobil``) for the many small
#: companies whose only published number is a mobile. Both are register values,
#: so both carry the register proof and the same confidence.
#:
#: The cardinal design rule of the challenge is registry first, web second, LLM
#: last. Every field the register already answers is a field we never have to
#: crawl for, never have to pay a search API for, and never have to ask a model
#: to read. So the mapping is deliberately generous: ``poststed`` gives us the
#: city, ``aktivitet`` gives us the activity text, and ``vedtektsfestetFormaal``
#: gives us the articles-of-association purpose.
REGISTRY_FACTS: tuple[tuple[Any, str, float], ...] = (
    ("navn", "legal_name", 1.00),
    ("organisasjonsform.kode", "legal_form", 1.00),
    ("naeringskode1.kode", "nace_code", 0.99),
    ("naeringskode1.beskrivelse", "industry", 0.99),
    ("antallAnsatte", "employees", 0.97),
    ("stiftelsesdato", "founded", 0.98),
    ("registreringsdatoEnhetsregisteret", "registered", 0.98),
    ("hjemmeside", "website", 0.95),
    ("epostadresse", "email", 0.95),
    # The register publishes two phone fields. Prefer the switchboard, fall back
    # to the mobile, never invent one.
    (("telefon", "mobil"), "phone", 0.95),
    ("forretningsadresse.kommune", "municipality", 0.97),
    ("forretningsadresse.postnummer", "postcode", 0.97),
    # The city is already sitting right next to the postcode in the register's
    # own address block; not publishing it was a plain omission.
    ("forretningsadresse.poststed", "city", 0.97),
    ("institusjonellSektorkode.beskrivelse", "sector", 0.96),
    ("sisteInnsendteAarsregnskap", "latest_accounts_year", 0.94),
    ("maalform", "language_form", 0.95),
    ("vedtektsfestetFormaal", "purpose", 0.93),
    # ``aktivitet`` is the register's own one-line activity statement and is
    # present for essentially every entity, which makes it the most reliable
    # descriptive text available without touching the web at all.
    ("aktivitet", "description", 0.90),
)

_SELECT = """
SELECT orgnr, navn, raw FROM enheter WHERE orgnr = ? AND slettet = 0
"""


def _dig(payload: dict[str, Any], dotted: str) -> Any:
    """Read ``a.b.c`` out of nested dicts, returning ``None`` on any miss."""
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def entity_facts(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a register payload into ``{profile_field: value}``.

    Empty strings, empty lists and missing keys all collapse to ``None``. This
    is the null-never-zero rule: an absent value stays absent rather than
    becoming ``0`` or ``""``.

    A mapping may name several source keys as alternatives, tried in order; the
    first non-empty one wins. Nothing here is inferred or interpolated -- if the
    register does not carry the value, the field simply does not appear.
    """
    out: dict[str, Any] = {}
    for key, field_name, _ in REGISTRY_FACTS:
        alternatives = key if isinstance(key, tuple) else (key,)
        for candidate in alternatives:
            value = _dig(payload, candidate)
            if isinstance(value, list):
                value = " ".join(str(v).strip() for v in value if str(v).strip()) or None
            if isinstance(value, str):
                value = value.strip() or None
            if value is not None:
                out[field_name] = value
                break
    return out


def address_line(payload: dict[str, Any]) -> str | None:
    """``Brobekkveien 80, 0582 OSLO`` — the register's own composition."""
    addr = payload.get("forretningsadresse") or {}
    parts = [*(addr.get("adresse") or [])]
    line = ", ".join(p.strip() for p in parts if p and p.strip())
    tail = " ".join(x for x in (addr.get("postnummer"), addr.get("poststed")) if x)
    if line and tail:
        return f"{line}, {tail}"
    return line or (tail or None)


class BrregClient:
    """Thin client over the two Brreg REST surfaces."""

    def __init__(self, client: HttpClient | None = None) -> None:
        self.http = client or HttpClient()

    def entity(self, org: OrgNumber | str) -> dict[str, Any] | None:
        """Single-company lookup. Returns ``None`` on 404 rather than raising."""
        number = org.value if isinstance(org, OrgNumber) else clean(org)
        if number is None:
            raise ValueError(f"not a valid organisation number: {org!r}")
        try:
            return self.http.get_json(f"{BASE}/enheter/{number}")
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise

    def search_by_name(self, name: str, *, size: int = 10) -> list[dict[str, Any]]:
        """Companies whose registered name contains ``name``, live.

        The register's own name search. It is deliberately left *unfiltered* here:
        a query like "Norsk Hydro" returns NORSK HYDROGENBILFORENING and
        NORSK HYDROGENFORUM before NORSK HYDRO ASA, so deciding which hit is the
        company asked about is the caller's job (see ``liveagent._names_agree``).
        Returning the raw candidates keeps that judgement in one place instead of
        hiding it in a "best match" this method is not equipped to make.

        A blank name returns nothing rather than the whole register.
        """
        term = (name or "").strip()
        if not term:
            return []
        payload = self.http.get_json(
            f"{BASE}/enheter?navn={_quote(term)}&size={max(1, min(size, 100))}"
        )
        return (payload.get("_embedded") or {}).get("enheter", []) or []

    def sub_units(self, org: OrgNumber | str) -> list[dict[str, Any]]:
        """Registered sub-entities (``underenheter``) of a company.

        Returns ``[]`` both when the company genuinely has none *and* when the
        lookup failed. Callers that publish a **derived** value from the count --
        ``location_count`` is ``len(units) + 1`` -- must use
        :meth:`sub_units_strict` instead, because an outage would otherwise be
        published as "1 registered location" with high confidence.
        """
        number = org.value if isinstance(org, OrgNumber) else clean(org)
        if number is None:
            return []
        try:
            payload = self.http.get_json(
                f"{BASE}/underenheter?overordnetEnhet={number}&size=100"
            )
        except Exception:
            return []
        return (payload.get("_embedded") or {}).get("underenheter", [])

    def try_sub_units(self, org: OrgNumber | str) -> tuple[bool, list[dict[str, Any]]]:
        """``(ok, units)`` -- separates "answered, none" from "could not ask".

        ``ok=False`` means the lookup did not complete, so any count derived from
        ``units`` must not be published. This mirrors the ``SearchAttempt``
        contract: a missing result is not evidence that the answer is zero.
        """
        number = org.value if isinstance(org, OrgNumber) else clean(org)
        if number is None:
            return False, []
        try:
            payload = self.http.get_json(
                f"{BASE}/underenheter?overordnetEnhet={number}&size=100"
            )
        except Exception:
            return False, []
        units = (payload.get("_embedded") or {}).get("underenheter", [])
        return True, units

    def roles(self, org: OrgNumber | str) -> list[dict[str, Any]]:
        """Registered role holders: management, board, auditor, accountant.

        The Enhetsregisteret exists in order to publish who runs a company, so
        this is a public register of office-holders rather than personal data
        scraped off the open web. That distinction is what makes it legitimate
        to publish, and it is why the web layer still refuses to harvest named
        individuals from company sites.
        """
        number = org.value if isinstance(org, OrgNumber) else clean(org)
        if number is None:
            return []
        try:
            payload = self.http.get_json(f"{BASE}/enheter/{number}/roller")
        except Exception:
            return []
        return payload.get("rollegrupper") or []

    def page(
        self,
        *,
        page: int = 0,
        size: int = 100,
        organisasjonsform: str | None = None,
    ) -> dict[str, Any]:
        """One page of the bulk listing.

        The API refuses ``page * size > 10000``, so a full mirror is built by
        slicing on ``organisasjonsform`` (see :func:`sync_bulk`).
        """
        query = [f"page={page}", f"size={size}"]
        if organisasjonsform:
            query.append(f"organisasjonsform={organisasjonsform}")
        return self.http.get_json(f"{BASE}/enheter?{'&'.join(query)}")

    def changes(
        self,
        *,
        since: str | None = None,
        cursor: int | None = None,
        page: int = 0,
        size: int = 1000,
    ) -> dict[str, Any]:
        """The change-event stream: new, changed and deleted entities.

        Two incremental modes:

        * ``since`` — a ``dato`` (ISO date) lower bound. Cheap to reason about but
          coarse: a day with a million bulk edits returns them all at once.
        * ``cursor`` — the monotonic ``oppdateringsid`` from the last event we saw.
          This is the true CDC anchor: the feed is append-only and ``oppdateringsid``
          only ever increases, so paging forward from the last id we processed is
          exact and never re-reads history.

        Each event also carries ``organisasjonsnummer`` (the entity to re-fetch) and
        ``endringstype`` (Ny / Endret / Slettet / Ukjent).
        """
        query = [f"page={page}", f"size={size}"]
        if cursor is not None:
            query.append(f"oppdateringsid={cursor}")
        elif since:
            query.append(f"dato={since}")
        return self.http.get_json(f"{BASE}/oppdateringer/enheter?{'&'.join(query)}")


# --------------------------------------------------------------------------- store


class _Store:
    """Thread-safe SQLite wrapper.

    The pipeline runs companies on a thread pool, and a SQLite connection must
    not cross threads. Rather than serialise every read behind one lock, each
    thread gets its own connection — reads are the hot path and SQLite handles
    concurrent readers natively. Writes are infrequent and short, so they share
    a lock.
    """

    def __init__(self, path: Path | str, schema: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema = schema
        self._local = threading.local()
        self._write_lock = threading.RLock()
        # Create the schema once on the creating thread, and keep that
        # connection: single-threaded callers (tests, CLI setup) use it directly
        # instead of paying for a fresh handle per call.
        self._primary = self._connect()
        self.conn = self._primary
        # Every connection handed out to a worker thread, so close() can release
        # them all. Without this a pool worker's handle outlives the run and, on
        # Windows, keeps the database file locked.
        self._connections: list[sqlite3.Connection] = [self._primary]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.executescript(self._schema)
        connection.commit()
        return connection

    def handle(self) -> sqlite3.Connection:
        """A connection owned by the calling thread."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        if threading.current_thread() is threading.main_thread():
            self._local.conn = self._primary
            return self._primary
        created = self._connect()
        self._local.conn = created
        with self._write_lock:
            self._connections.append(created)
        return created

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.handle().execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> None:
        with self._write_lock:
            handle = self.handle()
            handle.executemany(sql, rows)
            handle.commit()

    def commit(self) -> None:
        with self._write_lock:
            self.handle().commit()

    def close(self) -> None:
        """Release every connection. Idempotent, so a double close is harmless."""
        with self._write_lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            try:
                self._primary.commit()
            except sqlite3.Error:
                pass
            for connection in getattr(self, "_connections", [self._primary]):
                try:
                    connection.close()
                except sqlite3.Error:
                    pass


class RegistryStore(_Store):
    """Local SQLite mirror of the register.

    One row per entity, plus the raw JSON so any field can be re-derived without
    another network call. Lookups are indexed and cost microseconds.

    Deleted entities are retained with ``slettet = 1`` and are never served by
    :meth:`lookup` — a company that leaves the register must disappear from new
    profiles while remaining available as history.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS enheter (
        orgnr TEXT PRIMARY KEY,
        navn TEXT NOT NULL,
        organisasjonsform TEXT,
        kommune TEXT,
        nace TEXT,
        antall_ansatte INTEGER,
        registrert TEXT,
        slettet INTEGER DEFAULT 0,
        raw TEXT NOT NULL,
        fetched_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_navn ON enheter(navn);
    CREATE INDEX IF NOT EXISTS idx_form ON enheter(organisasjonsform);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        orgnr TEXT,
        kind TEXT,
        patch TEXT,
        detected_at TEXT
    );
    """

    def __init__(self, path: Path | str = "data/registry.sqlite") -> None:
        super().__init__(path, self.SCHEMA)

    # ------------------------------------------------------------ write side

    def upsert(self, payload: dict[str, Any], *, fetched_at: str = "") -> None:
        number = clean(payload.get("organisasjonsnummer"))
        if number is None or not payload.get("navn"):
            return
        addr = payload.get("forretningsadresse") or {}
        with self._write_lock:
            self.handle().execute(
                """
                INSERT INTO enheter (orgnr, navn, organisasjonsform, kommune, nace,
                                 antall_ansatte, registrert, slettet, raw, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(orgnr) DO UPDATE SET
                    navn = excluded.navn,
                    organisasjonsform = excluded.organisasjonsform,
                    kommune = excluded.kommune,
                    nace = excluded.nace,
                    antall_ansatte = excluded.antall_ansatte,
                    registrert = excluded.registrert,
                    slettet = 0,
                    raw = excluded.raw,
                    fetched_at = excluded.fetched_at
                """,
                (
                    number,
                    payload["navn"],
                    _dig(payload, "organisasjonsform.kode"),
                    addr.get("kommune"),
                    _dig(payload, "naeringskode1.kode"),
                    payload.get("antallAnsatte"),
                    payload.get("registreringsdatoEnhetsregisteret"),
                    json.dumps(payload, ensure_ascii=False),
                    fetched_at,
                ),
            )
            self.handle().commit()

    # ------------------------------------------------------------- read side

    def lookup(self, org: OrgNumber | str) -> dict[str, Any] | None:
        """Full register payload for ``org``, or ``None``.

        ``None`` covers three cases the caller must distinguish for itself:
        the number was never mirrored, the number is not a valid organisation
        number, or the entity has been deleted from the register. Deleted
        entities are kept in the table as history but are never served.
        """
        number = org.value if isinstance(org, OrgNumber) else clean(org)
        if number is None:
            return None
        row = self.handle().execute(_SELECT, (number,)).fetchone()
        return json.loads(row["raw"]) if row else None

    def lookup_any(self, org: OrgNumber | str) -> dict[str, Any] | None:
        """Like :meth:`lookup` but also returns deleted entities."""
        number = org.value if isinstance(org, OrgNumber) else clean(org)
        if number is None:
            return None
        row = self.handle().execute(
            "SELECT raw FROM enheter WHERE orgnr = ?", (number,)
        ).fetchone()
        return json.loads(row["raw"]) if row else None

    def search(self, term: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Substring search on name, plus exact org-number hit if the term is one."""
        results: list[dict[str, Any]] = []
        number = clean(term)
        if number:
            payload = self.lookup(number)
            if payload:
                results.append(payload)
        like = f"%{term.strip()}%"
        rows = self.handle().execute(
            "SELECT raw FROM enheter WHERE navn LIKE ? AND slettet = 0 "
            "ORDER BY navn LIMIT ?",
            (like, limit),
        ).fetchall()
        seen = {r.get("organisasjonsnummer") for r in results}
        for row in rows:
            payload = json.loads(row["raw"])
            if payload.get("organisasjonsnummer") not in seen:
                results.append(payload)
        return results[:limit]

    def count(self) -> int:
        return int(self.handle().execute("SELECT COUNT(*) FROM enheter").fetchone()[0])

    def employees_of(self, org: OrgNumber | str) -> int | None:
        number = org.value if isinstance(org, OrgNumber) else clean(org)
        if number is None:
            return None
        row = self.handle().execute(
            "SELECT antall_ansatte FROM enheter WHERE orgnr = ?", (number,)
        ).fetchone()
        return row["antall_ansatte"] if row else None

    def sample(self, n: int, *, with_website: bool = True) -> list[dict[str, Any]]:
        """A deterministic sample of substantial companies, for demos and tests."""
        clause = "WHERE organisasjonsform = 'AS' AND slettet = 0"
        if with_website:
            clause += " AND raw LIKE '%hjemmeside%'"
        rows = self.handle().execute(
            f"SELECT raw FROM enheter {clause} ORDER BY orgnr LIMIT ?", (n,)
        ).fetchall()
        return [json.loads(r["raw"]) for r in rows]

    def set_meta(self, key: str, value: str) -> None:
        self.handle().execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.handle().commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.handle().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def close(self) -> None:
        # Idempotent like the base: a second close must not raise, because the
        # pipeline tears stores down in a ``finally`` that can run after an
        # error path already closed them.
        try:
            self.handle().commit()
        except sqlite3.Error:
            pass
        except sqlite3.ProgrammingError:
            pass
        super().close()


# ------------------------------------------------------------- bulk + changes

#: Organisation forms worth mirroring. This keeps the database a few hundred
#: thousand rows instead of 1.18M, stays inside the 10,000-result-per-query
#: ceiling with room to spare, and keeps coverage on the companies that actually
#: have websites, employees and filed accounts.
DEFAULT_FORMS: tuple[str, ...] = (
    "AS", "ASA", "ANS", "DA", "SA", "NUF", "ENK", "BA", "STI", "FKF", "KF", "IKS", "PBL",
)


def sync_bulk(
    store: RegistryStore,
    client: BrregClient | None = None,
    *,
    forms: Iterable[str] = DEFAULT_FORMS,
    max_records: int | None = None,
    verbose: bool = True,
) -> int:
    """Mirror the register into SQLite, one organisation form at a time.

    Returns the number of entities written. Idempotent: re-running refreshes
    rows rather than duplicating them.
    """
    from profiles import utcnow_iso

    client = client or BrregClient()
    stamp = utcnow_iso()
    written = 0
    for form in forms:
        page = 0
        while True:
            try:
                payload = client.page(page=page, size=100, organisasjonsform=form)
            except Exception as exc:
                if verbose:
                    print(f"  ! {form} page {page}: {exc}")
                break
            batch = (payload.get("_embedded") or {}).get("enheter", [])
            if not batch:
                break
            for entity in batch:
                store.upsert(entity, fetched_at=stamp)
                written += 1
            store.commit()
            total_pages = int((payload.get("page") or {}).get("totalPages", 1))
            if verbose:
                print(f"  {form}: page {page + 1}/{total_pages} ({written} total)")
            page += 1
            if page >= total_pages or page * 100 >= 10000:
                break
            if max_records and written >= max_records:
                break
        if max_records and written >= max_records:
            break
    store.set_meta("last_bulk_sync", stamp)
    store.set_meta("record_count", str(store.count()))
    return written


def sync_changes(
    store: RegistryStore,
    client: BrregClient | None = None,
    *,
    since: str | None = None,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Apply the change-event stream, logging every patch we see.

    Returns the list of applied events so the pipeline can annotate the run
    report with "N registry events applied".
    """
    from profiles import utcnow_iso

    client = client or BrregClient()
    since = since or store.get_meta("last_change_sync") or None
    stamp = utcnow_iso()
    applied: list[dict[str, Any]] = []
    for page in range(max_pages):
        try:
            payload = client.changes(since=since, page=page, size=1000)
        except Exception:
            break
        events = (payload.get("_embedded") or {}).get("oppdateringer", [])
        if not events:
            break
        for event in events:
            number = clean(event.get("organisasjonsnummer"))
            kind = event.get("oppdateringstype", "ukjent")
            store.conn.execute(
                "INSERT INTO changes (orgnr, kind, patch, detected_at) VALUES (?, ?, ?, ?)",
                (number, kind, json.dumps(event, ensure_ascii=False), stamp),
            )
            applied.append(event)
        store.commit()
        if page + 1 >= int((payload.get("page") or {}).get("totalPages", 1)):
            break
    store.set_meta("last_change_sync", stamp)
    return applied


# ---------------------------------------------------------------- annual accounts

@dataclass(slots=True)
class RegnskapFigures:
    """Latest filed accounts, as integers. Absent figures stay ``None``."""

    year: int | None = None
    revenue: int | None = None
    operating_result: int | None = None
    net_result: int | None = None
    total_assets: int | None = None
    equity: int | None = None
    debt: int | None = None
    currency: str = "NOK"
    filing_type: str = ""
    period_end: str = ""
    source_url: str = ""

    @property
    def available(self) -> bool:
        return any(
            v is not None
            for v in (self.revenue, self.operating_result, self.net_result,
                      self.total_assets, self.equity, self.debt)
        )


def _number(value: Any) -> int | None:
    """Floats to rounded ints; junk, blanks and missing keys to ``None``.

    Never ``0``. A zero that was never filed is a fabricated value, which is an
    outright disqualification. A zero that *was* filed survives round-tripping
    because it arrives here as the float ``0.0``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, str):
        cleaned = value.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            return int(round(float(cleaned)))
        except ValueError:
            return None
    return None


def fetch_regnskap(
    org: OrgNumber | str,
    client: HttpClient | None = None,
) -> RegnskapFigures | None:
    """Fetch and normalise the latest filed accounts.

    Guards against publishing another company's numbers: the filing's own
    ``virksomhet.organisasjonsnummer`` is authoritative and a mismatch causes the
    filing to be discarded rather than reconciled.
    """
    number = org.value if isinstance(org, OrgNumber) else clean(org)
    if number is None:
        return None
    http = client or HttpClient()
    url = f"{REGN_SKAP_BASE}/regnskap/{number}"
    try:
        filings = http.get_json(url)
    except Exception:
        return None
    if not isinstance(filings, list) or not filings:
        return None

    def period_end(filing: dict[str, Any]) -> str:
        return str((filing.get("regnskapsperiode") or {}).get("tilDato") or "")

    usable = [
        f for f in filings
        if (f.get("virksomhet") or {}).get("organisasjonsnummer", number).strip() == number
        and period_end(f)
    ]
    if not usable:
        return None
    latest = max(usable, key=period_end)

    result = latest.get("resultatregnskapResultat") or {}
    drift = result.get("driftsresultat") or {}
    eiendeler = latest.get("eiendeler") or {}
    eg = latest.get("egenkapitalGjeld") or {}
    end = period_end(latest)

    return RegnskapFigures(
        year=int(end[:4]) if end[:4].isdigit() else None,
        revenue=_number((drift.get("driftsinntekter") or {}).get("sumDriftsinntekter")),
        operating_result=_number(drift.get("driftsresultat")),
        net_result=_number(result.get("aarsresultat")),
        total_assets=_number(eiendeler.get("sumEiendeler")),
        equity=_number((eg.get("egenkapital") or {}).get("sumEgenkapital")),
        debt=_number((eg.get("gjeldOversikt") or {}).get("sumGjeld")),
        currency=str(latest.get("valuta") or "NOK").strip() or "NOK",
        filing_type=str(latest.get("regnskapstype") or "").strip(),
        period_end=end,
        source_url=url,
    )
