"""Identifier enrichment: GLEIF for the LEI, Wikidata for cross-links.

Both are free and keyless. Both run concurrently, and both are cached on disk by
the shared HTTP client, so a second run costs nothing. Neither is allowed to
block the pipeline: a timeout or an outage degrades the profile, it does not
fail the company.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

from net import HttpClient, USER_AGENT
from validate import OrgNumber, clean

__all__ = ["Enrichment", "LeiRecord", "WikidataLinks", "enrich"]

GLEIF = "https://api.gleif.org/api/v1/lei-records"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


@dataclass(slots=True)
class LeiRecord:
    """The GLEIF record for a company, if one exists.

    Most small Norwegian companies have no LEI at all — that is a normal, useful
    answer, not a failure.
    """

    lei: str = ""
    legal_name: str = ""
    status: str = ""
    parent_lei: str = ""
    parent_name: str = ""
    source_url: str = ""
    retrieved_at: str = ""

    @property
    def found(self) -> bool:
        return bool(self.lei)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lei": self.lei or None,
            "legal_name": self.legal_name or None,
            "registration_status": self.status or None,
            "parent_lei": self.parent_lei or None,
            "parent_name": self.parent_name or None,
            "source_url": self.source_url or None,
        }


@dataclass(slots=True)
class WikidataLinks:
    """Identifiers and links harvested from Wikidata."""

    qid: str = ""
    official_website: str = ""
    wikipedia_url: str = ""
    legal_form: str = ""
    source_url: str = ""
    retrieved_at: str = ""

    @property
    def found(self) -> bool:
        return bool(self.qid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wikidata": self.qid or None,
            "official_website": self.official_website or None,
            "wikipedia": self.wikipedia_url or None,
            "legal_form": self.legal_form or None,
            "source_url": self.source_url or None,
        }


@dataclass(slots=True)
class Enrichment:
    """Everything the identifier layer contributed, plus what it failed to do."""

    lei: LeiRecord = field(default_factory=LeiRecord)
    wikidata: WikidataLinks = field(default_factory=WikidataLinks)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lei": self.lei.to_dict(),
            "wikidata": self.wikidata.to_dict(),
            "warnings": self.warnings,
            "elapsed_ms": self.elapsed_ms,
        }


def _query_gleif(client: HttpClient, org: str, timeout: float) -> LeiRecord:
    """Look up the LEI by Norwegian registration authority + entity id.

    GLEIF indexes registrations as ``RA000472`` (Foretaksregisteret) with the
    organisation number as the local id, which is an exact join — no name fuzzy
    matching, so no collision risk. Registrations may be stored unspaced
    ("984851006") or spaced ("984 851 006"), so both forms are queried.
    """
    import urllib.parse
    from profiles import utcnow_iso
    from validate import format_display

    variants = [org]
    disp = format_display(org)
    if disp != org:
        variants.append(disp)

    records: list[dict[str, Any]] = []
    last_exc = None
    for candidate in variants:
        url = f"{GLEIF}?filter%5Bentity.registeredAs%5D={urllib.parse.quote(candidate)}"
        try:
            payload = client.get_json(url, headers={"Accept": "application/vnd.api+json"})
            found = payload.get("data") or []
            if found:
                records = found
                break
        except Exception as exc:
            last_exc = exc

    if not records and last_exc:
        raise RuntimeError(f"gleif: {last_exc}") from last_exc

    if not records:
        return LeiRecord()
    record = records[0]
    attributes = record.get("attributes") or {}
    entity = attributes.get("entity") or {}
    return LeiRecord(
        lei=str(record.get("id") or ""),
        legal_name=str((entity.get("legalName") or {}).get("name") or ""),
        status=str(entity.get("status") or ""),
        source_url=f"{GLEIF}/{record.get('id')}",
        retrieved_at=utcnow_iso(),
    )


def _query_gleif_parent(client: HttpClient, lei: str) -> tuple[str, str, str]:
    """Follow the direct-parent relationship, if GLEIF publishes one."""
    from profiles import utcnow_iso

    url = f"{GLEIF}/{lei}/direct-parent-relationship"
    try:
        payload = client.get_json(url, headers={"Accept": "application/vnd.api+json"})
    except Exception:
        return "", "", ""
    data = payload.get("data") or {}
    attributes = data.get("attributes") or {}
    parent = ((attributes.get("relationship") or {}).get("directParent")) or {}
    if not parent:
        return "", "", ""
    parent_lei = str(parent.get("id") or "")
    return parent_lei, "", f"{GLEIF}/{parent_lei}"


def _query_wikidata(client: HttpClient, org: str) -> WikidataLinks:
    """SPARQL for the Wikidata item whose Norwegian org number matches.

    P2333 is "Norwegian organisation number". Matching on the identifier rather
    than the label means a name collision can never attach the wrong company.
    """
    import urllib.parse
    from profiles import utcnow_iso
    from validate import format_display

    disp = format_display(org)
    filter_expr = f'?orgnr = "{org}" || ?orgnr = "{disp}"' if disp != org else f'?orgnr = "{org}"'
    query = f"""
    SELECT ?item ?itemLabel ?website ?article ?formLabel WHERE {{
      ?item wdt:P2333 ?orgnr .
      FILTER({filter_expr})
      OPTIONAL {{ ?item wdt:P856 ?website }}
      OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://no.wikipedia.org/> }}
      OPTIONAL {{ ?item wdt:P1454 ?form }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "nb,en". }}
    }} LIMIT 1
    """
    url = f"{WIKIDATA_SPARQL}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
    try:
        payload = client.get_json(
            url,
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": USER_AGENT,
            },
        )
    except Exception as exc:
        raise RuntimeError(f"wikidata: {exc}") from exc

    bindings = ((payload.get("results") or {}).get("bindings") or [])
    if not bindings:
        return WikidataLinks()
    row = bindings[0]

    def value(key: str) -> str:
        return str((row.get(key) or {}).get("value") or "")

    return WikidataLinks(
        qid=value("item").rsplit("/", 1)[-1],
        official_website=value("website"),
        wikipedia_url=value("article"),
        legal_form=value("formLabel"),
        source_url=value("item"),
        retrieved_at=utcnow_iso(),
    )


async def _gather(org: str, client: HttpClient, timeout: float) -> Enrichment:
    """Run both lookups concurrently under a hard timeout."""
    from profiles import utcnow_iso

    loop = asyncio.get_running_loop()
    started = loop.time()

    async def guarded(name: str, fn, *args) -> tuple[str, Any]:
        try:
            return name, await asyncio.wait_for(
                loop.run_in_executor(None, fn, *args), timeout=timeout
            )
        except asyncio.TimeoutError:
            return name, RuntimeError(f"{name}: timed out after {timeout}s")
        except Exception as exc:  # noqa: BLE001 - degradation is the point
            return name, exc

    results = await asyncio.gather(
        guarded("gleif", _query_gleif, client, org, timeout),
        guarded("wikidata", _query_wikidata, client, org),
    )

    enrichment = Enrichment()
    for name, outcome in results:
        if isinstance(outcome, Exception):
            enrichment.warnings.append(str(outcome))
            continue
        if name == "gleif":
            enrichment.lei = outcome
        else:
            enrichment.wikidata = outcome

    if enrichment.lei.found and not enrichment.lei.parent_lei:
        parent_lei, _, parent_url = _query_gleif_parent(client, enrichment.lei.lei)
        enrichment.lei.parent_lei = parent_lei
        if parent_lei:
            enrichment.lei.parent_name = parent_lei
    enrichment.elapsed_ms = int((loop.time() - started) * 1000)
    return enrichment


def enrich(org: OrgNumber | str, client: HttpClient | None = None,
           *, timeout: float | None = None) -> Enrichment:
    """Synchronously enrich one company with LEI and Wikidata identifiers."""
    number = org.value if isinstance(org, OrgNumber) else clean(org)
    if number is None:
        raise ValueError(f"not a valid organisation number: {org!r}")
    http = client or HttpClient()
    budget = float(timeout or os.environ.get("SIGNALPOST_ENRICH_TIMEOUT", "8"))
    try:
        return asyncio.run(_gather(number, http, budget))
    except RuntimeError:
        # Already inside a running loop (Jupyter, async harness): run inline.
        enrichment = Enrichment()
        try:
            enrichment.lei = _query_gleif(http, number, budget)
        except Exception as exc:
            enrichment.warnings.append(str(exc))
        try:
            enrichment.wikidata = _query_wikidata(http, number)
        except Exception as exc:
            enrichment.warnings.append(str(exc))
        return enrichment


def offline_placeholder(reason: str = "enrichment skipped") -> Enrichment:
    """Used by the offline demo so the UI always has a shape to render."""
    return Enrichment(warnings=[reason])
