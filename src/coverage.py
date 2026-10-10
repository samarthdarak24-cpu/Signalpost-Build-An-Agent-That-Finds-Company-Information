"""Broad-research coverage: people and locations.

The challenge's largest scoring band is recall, and two of the categories it
names that the identity layer alone cannot answer are people and locations. Both
come from the same free, keyless register surfaces, so they cost nothing to
collect and they carry the strongest available proof — the register itself.

Financials are deliberately **not** handled here. They already have a single
home in ``pipeline._financial_facts``, and a second implementation with slightly
different field names is precisely how the register field table drifted apart
earlier in this project. One source per fact category.

Two deliberate constraints run through this module:

*   **People are office-holders, not private individuals.** The Enhetsregisteret
    publishes who holds which role precisely so the public can see it. We
    publish the role and the name. We do *not* publish dates of birth, even
    though the register carries them, because the role is the fact of interest
    and the birth date adds nothing but exposure.
*   **A filed zero survives; an unfiled value stays absent.**
"""

from __future__ import annotations

from typing import Any, Iterable

from profiles import Fact, freshness_of
from research_fields import DOMAINS, DOMAIN_LABELS, FIELD_CATALOG, domain_fields

__all__ = [
    "ROLE_LABELS",
    "PEOPLE_FIELDS",
    "people_facts",
    "location_facts",
    "domain_coverage",
    "coverage_summary",
    "recall_matrix",
    "missing_high_value_fields",
    "render_recall_matrix",
    "freshness_report",
]

#: Brreg role codes worth publishing, in the order they should be tried.
#: Codes not listed here (deputy board members, contact persons) are noise for
#: a company profile and would dilute precision without adding recall.
ROLE_LABELS: dict[str, tuple[str, str]] = {
    "DAGL": ("ceo", "Chief executive"),
    "LEDE": ("board_chair", "Board chair"),
    "NEST": ("board_deputy", "Deputy board chair"),
    "MEDL": ("board_members", "Board member"),
    "VARA": ("board_deputies", "Deputy board member"),
    "REVI": ("auditor", "Auditor"),
    "REGN": ("accountant", "Accountant"),
    "INNH": ("owner", "Owner"),
    "DTPR": ("partners", "Partner"),
    "KOMR": ("general_partner", "General partner"),
    "FFØR": ("business_manager", "Business manager"),
}

#: Fields where several people are expected and the value is a joined list.
MULTI_ROLES = {"board_members", "board_deputies", "partners"}

PEOPLE_FIELDS = tuple({field for field, _ in ROLE_LABELS.values()})


def _person_name(person: dict[str, Any]) -> str:
    """``{"fornavn": "Anders", "etternavn": "Opedal"}`` -> ``"Anders Opedal"``.

    Returns an empty string when the register has no name, so the caller can
    skip the role rather than publish a placeholder.
    """
    name = person.get("navn") or {}
    parts = [
        str(name.get("fornavn") or "").strip(),
        str(name.get("mellomnavn") or "").strip(),
        str(name.get("etternavn") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def people_facts(
    role_groups: Iterable[dict[str, Any]],
    *,
    number: str,
    retrieved_at: str,
    source_url: str,
) -> list[Fact]:
    """Turn ``/roller`` groups into one fact per role.

    Deregistered role holders are skipped: a board member who stepped down is
    history, not current coverage, and publishing them as current would be a
    precision error rather than a recall win.
    """
    collected: dict[str, list[str]] = {}
    methods: dict[str, str] = {}

    for group in role_groups or []:
        for role in group.get("roller") or []:
            if role.get("avregistrert"):
                continue
            code = str((role.get("type") or {}).get("kode") or "")
            mapped = ROLE_LABELS.get(code)
            if not mapped:
                continue
            field, label = mapped
            name = _person_name(role.get("person") or {})
            if not name:
                continue
            bucket = collected.setdefault(field, [])
            if name not in bucket:
                bucket.append(name)
            methods[field] = f"Enhetsregisteret role {code} ({label})"

    facts: list[Fact] = []
    for field, names in collected.items():
        if not names:
            continue
        if field in MULTI_ROLES:
            value: Any = ", ".join(names)
            evidence = f"roles: {value}"
            # More board members is not more confidence in any one of them.
            confidence = 0.95
        else:
            value = names[0]
            evidence = f'"navn": "{value}"'
            confidence = 0.96
        facts.append(
            Fact(
                field=field,
                value=value,
                source_url=source_url,
                retrieved_at=retrieved_at,
                evidence=evidence,
                match_proof="registry",
                confidence=confidence,
                layer="registry",
                method=methods.get(field, "Enhetsregisteret roller"),
                org_number=number,
            )
        )
    return facts


def location_facts(
    sub_units: Iterable[dict[str, Any]],
    *,
    number: str,
    retrieved_at: str,
    source_url: str,
    lookup_ok: bool = True,
) -> list[Fact]:
    """Turn ``underenheter`` into a location count plus a readable list.

    A company with no registered sub-units genuinely has one location, so the
    count is published as ``1`` in that case — that is a real fact about the
    entity, not an invented one. But that reasoning only holds when the register
    actually *answered*. ``lookup_ok=False`` means the sub-unit listing could not
    be read, so ``sub_units`` is empty because we could not look, not because the
    company has none; in that case **no count is published at all**, since
    publishing ``1`` would be a fabricated fact with 0.95 confidence.

    The list is only published when there is more than one place, because
    repeating the head-office address back as a "business location" would pad
    the profile without informing it.
    """
    if not lookup_ok:
        # The count is derived from a listing we never obtained. Emitting
        # nothing is the only honest outcome; the field is then either carried
        # forward from a previous run or reported as an un-filled gap.
        return []
    units = [u for u in (sub_units or []) if u.get("beliggenhetsadresse")]
    places: list[str] = []
    for unit in units:
        addr = unit.get("beliggenhetsadresse") or {}
        parts = [p for p in (addr.get("adresse") or []) if p]
        tail = " ".join(
            x for x in (addr.get("postnummer"), addr.get("poststed")) if x
        )
        line = ", ".join(parts)
        if line and tail:
            places.append(f"{line}, {tail}")
        elif line or tail:
            places.append(line or tail)
        else:
            places.append(str(addr.get("kommune") or "").strip())
    places = [p for p in places if p]

    facts: list[Fact] = [
        Fact(
            field="location_count",
            value=len(units) + 1,
            source_url=source_url,
            retrieved_at=retrieved_at,
            evidence=f'"underenheter": {len(units)} registered sub-entities',
            match_proof="registry",
            confidence=0.95,
            layer="registry",
            method="Enhetsregisteret underenheter count, plus the head office",
            org_number=number,
        )
    ]

    if places:
        facts.append(
            Fact(
                field="business_locations",
                value=" | ".join(places[:8]),
                source_url=source_url,
                retrieved_at=retrieved_at,
                evidence=f'"beliggenhetsadresse": {places[0]!r} (first of {len(places)})',
                match_proof="registry",
                confidence=0.93,
                layer="registry",
                method="Enhetsregisteret underenheter beliggenhetsadresse",
                org_number=number,
            )
        )
    return facts


# ------------------------------------------------------------ coverage matrix
#: A fact is "evidence-backed" when it names a source *and* carries a verbatim
#: quote. A fact with a URL but no quote, or a quote but no URL, is not auditable
#: and does not count here -- the point of the matrix is to reward evidence, not
#: mere presence.
def _is_evidence_backed(fact: Fact) -> bool:
    return bool(getattr(fact, "source_url", "")) and bool(getattr(fact, "evidence", ""))


def _as_facts(subject: Any) -> list[Fact]:
    """Accept either a :class:`~profiles.Profile` or a bare list of facts."""
    if subject is None:
        return []
    facts = getattr(subject, "facts", None)
    if facts is not None:
        return list(facts)
    return list(subject)


def domain_coverage(subject: Any) -> dict[str, dict[str, Any]]:
    """Field-aware, source-aware coverage across the five research domains.

    ``subject`` may be a ``Profile`` or a plain list of ``Fact``. For each domain
    the result says which catalog fields were filled, which are still missing,
    the fill ratio, and -- for each filled field -- the *layer* (source) it came
    in on. That last part is what makes the matrix source-aware: a reviewer can
    see at a glance whether a field came from the register, the web, an
    identifier join, or the model.
    """
    facts = _as_facts(subject)
    by_field: dict[str, Fact] = {}
    extras: dict[str, list[str]] = {d: [] for d in DOMAINS}
    for fact in facts:
        name = getattr(fact, "field", "")
        if not name:
            continue
        by_field.setdefault(name, fact)

    matrix: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        catalog = [f.field for f in domain_fields(domain)]
        present: list[str] = []
        sources: dict[str, str] = {}
        for name in catalog:
            fact = by_field.get(name)
            if fact is not None:
                present.append(name)
                sources[name] = getattr(fact, "layer", "") or ""
        missing = [name for name in catalog if name not in present]
        matrix[domain] = {
            "label": DOMAIN_LABELS.get(domain, domain),
            "fields_total": len(catalog),
            "fields_present": len(present),
            "ratio": round(len(present) / len(catalog), 3) if catalog else 0.0,
            "present": present,
            "missing": missing,
            "sources": sources,
        }
    # Fields we publish that the catalog does not (yet) name, per domain, so a
    # catalog gap is visible rather than hidden.
    catalogued = {f.field for d in DOMAINS for f in domain_fields(d)}
    for name, fact in by_field.items():
        if name in catalogued:
            continue
        domain = _domain_of_extra(name)
        if domain in extras and name not in extras[domain]:
            extras[domain].append(name)
    for domain in DOMAINS:
        matrix[domain]["extra_fields"] = sorted(extras[domain])
    return matrix


def _domain_of_extra(name: str) -> str:
    """Best-effort domain for a published field the catalog does not list."""
    from research_fields import field_domain

    known = field_domain(name)
    if known:
        return known
    if name in {"parent_lei", "official_website"}:
        return "company"
    if name in {"accounting_year"}:
        return "financials"
    return ""


def coverage_summary(subject: Any) -> dict[str, Any]:
    """The headline counters the console shows above the matrix.

    ``facts_found``   total facts on the profile.
    ``evidence_backed`` facts that carry both a source URL and a quote.
    ``ambiguous``     facts whose only proof is a bare name match.
    ``unavailable``   fields the agent deliberately left empty (SkippedField).
    ``domains``       per-domain fill ratio, for the five-category bar chart.
    """
    facts = _as_facts(subject)
    empty = list(getattr(subject, "empty_fields", []) or [])
    ambiguous = sum(1 for f in facts if getattr(f, "match_proof", "") == "name_only")
    matrix = domain_coverage(subject)
    return {
        "facts_found": len(facts),
        "evidence_backed": sum(1 for f in facts if _is_evidence_backed(f)),
        "ambiguous": ambiguous,
        "unavailable": len(empty),
        "fields": len({getattr(f, "field", "") for f in facts} - {""}),
        "domains": {
            domain: {
                "label": entry["label"],
                "present": entry["fields_present"],
                "total": entry["fields_total"],
                "ratio": entry["ratio"],
            }
            for domain, entry in matrix.items()
        },
    }


# --------------------------------------------------------------- recall
#: The order to optimise in: the domains with the most headroom and the most
#: score behind them first. ``company`` is omitted because the register already
#: fills it; the rest are ranked by expected gain.
OPTIMIZATION_ORDER: tuple[str, ...] = (
    "financials", "people_locations", "website", "hiring_activity",
)


def _value(profile: Any, field: str) -> Any:
    getter = getattr(profile, "value", None)
    if callable(getter):
        return getter(field)
    for fact in _as_facts(profile):
        if getattr(fact, "field", "") == field:
            return getattr(fact, "value", None)
    return None


def recall_matrix(subjects: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Per-domain recall: how many companies are covered, and how many facts found.

    Two numbers, because they answer two questions. *Companies* is the share of
    companies with at least one field in the domain -- did we reach them at all.
    *Facts* is the share of the domain's field-slots that were actually filled --
    how deep we got. A domain can be 90% companies and 40% facts, and that gap is
    exactly where the next score is coming from.
    """
    profiles = [p for p in subjects if p is not None]
    n = len(profiles)
    matrix: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        fields = [f.field for f in domain_fields(domain)]
        companies_with = sum(
            1 for p in profiles if any(_value(p, f) is not None for f in fields)
        )
        present_slots = sum(
            1 for p in profiles for f in fields if _value(p, f) is not None
        )
        matrix[domain] = {
            "label": DOMAIN_LABELS.get(domain, domain),
            "companies_pct": round(companies_with / n, 3) if n else 0.0,
            "facts_pct": round(present_slots / (n * len(fields)), 3) if n and fields else 0.0,
            "field_count": len(fields),
        }
    return matrix


def missing_high_value_fields(
    subjects: Iterable[Any], *, limit: int = 10
) -> list[dict[str, Any]]:
    """The high-weight fields that are absent most often, ranked by headroom.

    This is the "where is the next score" list: a field that is both important
    (high weight) and almost always empty (high absence rate) is the cheapest
    recall to win next.
    """
    profiles = [p for p in subjects if p is not None]
    n = len(profiles)
    if not n:
        return []
    ranked: list[dict[str, Any]] = []
    for entry in FIELD_CATALOG:
        absent = sum(1 for p in profiles if _value(p, entry.field) is None)
        if absent == 0:
            continue
        ranked.append({
            "field": entry.field,
            "label": entry.label,
            "domain": entry.domain,
            "domain_label": DOMAIN_LABELS.get(entry.domain, entry.domain),
            "absent": absent,
            "absent_pct": round(absent / n, 3),
            "weight": entry.weight,
            "headroom": round(entry.weight * absent / n, 3),
        })
    ranked.sort(key=lambda r: (-r["headroom"], r["field"]))
    return ranked[:limit]


def freshness_report(subject: Any) -> dict[str, dict[str, Any]]:
    """Per-domain freshness: the most recent retrieval and its bucket.

    "Registry 2 hours ago, financials 1 day ago, jobs 6 hours ago" -- the delta
    engine reads this to decide what is worth re-checking, and the console shows
    it so a stale domain is visible rather than silently trusted.
    """
    facts = _as_facts(subject)
    out: dict[str, dict[str, Any]] = {}
    for domain in DOMAINS:
        fields = {f.field for f in domain_fields(domain)}
        stamps = [
            getattr(f, "retrieved_at", "")
            for f in facts
            if getattr(f, "field", "") in fields and getattr(f, "retrieved_at", "")
        ]
        latest = max(stamps) if stamps else ""
        out[domain] = {
            "label": DOMAIN_LABELS.get(domain, domain),
            "latest_retrieved_at": latest,
            "freshness": freshness_of(latest) if latest else "unknown",
        }
    return out


def render_recall_matrix(
    matrix: dict[str, dict[str, Any]],
    missing: list[dict[str, Any]] | None = None,
) -> str:
    """The recall matrix as fixed-width text, for the terminal and the console."""
    lines = ["SIGNALPOST RECALL MATRIX", ""]
    lines.append(f"{'':<22}{'Companies':>12}{'Facts':>10}")
    for domain in DOMAINS:
        entry = matrix.get(domain)
        if not entry:
            continue
        lines.append(
            f"{entry['label']:<22}{entry['companies_pct'] * 100:>10.0f}%"
            f"{entry['facts_pct'] * 100:>9.0f}%"
        )
    if missing:
        lines.append("")
        lines.append("Missing high-value fields")
        for item in missing[:8]:
            lines.append(f"  - {item['label']} ({item['domain_label']}): "
                         f"{item['absent_pct'] * 100:.0f}% absent")
    return "\n".join(lines)

