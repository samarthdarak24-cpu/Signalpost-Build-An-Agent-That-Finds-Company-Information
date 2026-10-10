"""The five research domains, their fields, and the source-priority matrix.

The Builderr reference profile is organised around five information areas, and
so is the coverage it reports:

1. **Company record** — identity, legal form, industry, registration.
2. **Financials** — the latest annual filing, figure by figure.
3. **People & locations** — who runs it, and where it operates.
4. **Website** — the verified company site and the contact data on it.
5. **Hiring & public activity** — open roles and dated public updates.

This module is the single place that decides *which field belongs to which
domain* and *which source answers it first*. Two rules from the rest of the
project carry over unchanged:

*   **Registry first, web second, LLM last.** A field the register already
    answers never gets crawled for, searched for, or modelled.
*   **One source per field category.** The catalog names a *primary* source and
    at most one *fallback*; it never lists a third, because a third source is
    how two implementations of the same field drift apart.

It is pure data with no network or file dependency, so the planner, the
coverage matrix and the console can all import it without pulling in HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

__all__ = [
    "SOURCE_REGISTRY",
    "SOURCE_ROLES",
    "SOURCE_SUBUNITS",
    "SOURCE_ACCOUNTS",
    "SOURCE_GLEIF",
    "SOURCE_WIKIDATA",
    "SOURCE_REGISTRY_SITE",
    "SOURCE_SEARCH",
    "SOURCE_CAREERS",
    "SOURCE_ACTIVITY",
    "SOURCE_LLM",
    "DOMAINS",
    "DOMAIN_LABELS",
    "ResearchField",
    "FIELD_CATALOG",
    "SOURCE_PRIORITY",
    "field_domain",
    "domain_fields",
    "all_fields",
    "primary_source_for",
    "fallback_source_for",
    "catalog_matrix",
]

# ------------------------------------------------------------------ sources
#: Canonical source identifiers. The planner and the coverage matrix both speak
#: in these tokens, so a field's provenance is comparable across the codebase.
SOURCE_REGISTRY = "registry"
SOURCE_ROLES = "registry_roles"
SOURCE_SUBUNITS = "registry_subunits"
SOURCE_ACCOUNTS = "regnskapsregisteret"
SOURCE_GLEIF = "gleif"
SOURCE_WIKIDATA = "wikidata"
SOURCE_REGISTRY_SITE = "registry_website"
SOURCE_SEARCH = "brave_search"
SOURCE_CAREERS = "careers_page"
SOURCE_ACTIVITY = "activity_feed"
SOURCE_LLM = "llm"

# ------------------------------------------------------------------ domains
#: The five domains, in the order the reference profile presents them.
DOMAINS: tuple[str, ...] = (
    "company",
    "financials",
    "people_locations",
    "website",
    "hiring_activity",
)

DOMAIN_LABELS: dict[str, str] = {
    "company": "Company record",
    "financials": "Financials",
    "people_locations": "People & locations",
    "website": "Website",
    "hiring_activity": "Hiring & public activity",
}


@dataclass(frozen=True, slots=True)
class ResearchField:
    """One field we try to fill, and the source that answers it first."""

    field: str
    label: str
    domain: str
    primary: str
    fallback: str = ""
    #: Relative importance inside its domain, used only to rank gaps. A weight
    #: of 1.0 is the default; a few identity and financial fields carry more
    #: because the reference profile treats them as headline facts.
    weight: float = 1.0

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.primary, self.fallback) if self.fallback else (self.primary,)


#: The catalog. Field names match the ones the pipeline already publishes, so a
#: catalog entry and a real fact line up without a translation table.
FIELD_CATALOG: tuple[ResearchField, ...] = (
    # ---------------------------------------------------- company record
    ResearchField("legal_name", "Legal name", "company", SOURCE_REGISTRY, weight=2.0),
    ResearchField("org_number", "Organisation number", "company", SOURCE_REGISTRY, weight=2.0),
    ResearchField("status", "Status", "company", SOURCE_REGISTRY),
    ResearchField("legal_form", "Legal form", "company", SOURCE_REGISTRY),
    ResearchField("industry", "Industry", "company", SOURCE_REGISTRY),
    ResearchField("nace_code", "NACE code", "company", SOURCE_REGISTRY),
    ResearchField("registered", "Registration date", "company", SOURCE_REGISTRY),
    ResearchField("founded", "Founded", "company", SOURCE_REGISTRY),
    ResearchField("employees", "Employees", "company", SOURCE_REGISTRY),
    ResearchField("description", "Description", "company", SOURCE_REGISTRY, SOURCE_REGISTRY_SITE),
    ResearchField("municipality", "Municipality", "company", SOURCE_REGISTRY),
    ResearchField("city", "City", "company", SOURCE_REGISTRY),
    ResearchField("postcode", "Postcode", "company", SOURCE_REGISTRY),
    ResearchField("sector", "Sector", "company", SOURCE_REGISTRY),
    ResearchField("language_form", "Language form", "company", SOURCE_REGISTRY),
    ResearchField("purpose", "Purpose", "company", SOURCE_REGISTRY),
    ResearchField("parent_org_number", "Parent company", "company", SOURCE_REGISTRY),
    ResearchField("lei", "LEI", "company", SOURCE_GLEIF),
    ResearchField("wikidata", "Wikidata", "company", SOURCE_WIKIDATA),
    # --------------------------------------------------------- financials
    ResearchField("latest_accounts_year", "Latest accounts year", "financials", SOURCE_REGISTRY, weight=1.5),
    ResearchField("reporting_period", "Reporting period", "financials", SOURCE_ACCOUNTS),
    ResearchField("revenue_latest", "Revenue", "financials", SOURCE_ACCOUNTS, weight=1.5),
    ResearchField("operating_result", "Operating result", "financials", SOURCE_ACCOUNTS, weight=1.2),
    ResearchField("net_result", "Annual result", "financials", SOURCE_ACCOUNTS, weight=1.2),
    ResearchField("total_assets", "Assets", "financials", SOURCE_ACCOUNTS),
    ResearchField("equity", "Equity", "financials", SOURCE_ACCOUNTS),
    ResearchField("debt", "Debt", "financials", SOURCE_ACCOUNTS),
    # -------------------------------------------------- people & locations
    ResearchField("ceo", "CEO / managing director", "people_locations", SOURCE_ROLES, weight=1.5),
    ResearchField("board_chair", "Board chair", "people_locations", SOURCE_ROLES, weight=1.2),
    ResearchField("board_deputy", "Deputy board chair", "people_locations", SOURCE_ROLES),
    ResearchField("board_members", "Board members", "people_locations", SOURCE_ROLES, weight=1.2),
    ResearchField("board_deputies", "Deputy board members", "people_locations", SOURCE_ROLES),
    ResearchField("auditor", "Auditor", "people_locations", SOURCE_ROLES),
    ResearchField("accountant", "Accountant", "people_locations", SOURCE_ROLES),
    ResearchField("owner", "Owner", "people_locations", SOURCE_ROLES),
    ResearchField("address", "Registered address", "people_locations", SOURCE_REGISTRY),
    ResearchField("location_count", "Registered locations", "people_locations", SOURCE_SUBUNITS),
    ResearchField("business_locations", "Workplaces / sub-units", "people_locations", SOURCE_SUBUNITS),
    # ------------------------------------------------------------ website
    ResearchField("website", "Website", "website", SOURCE_REGISTRY_SITE, SOURCE_SEARCH, weight=1.5),
    ResearchField("email", "Public email", "website", SOURCE_REGISTRY, SOURCE_REGISTRY_SITE),
    ResearchField("phone", "Public phone", "website", SOURCE_REGISTRY, SOURCE_REGISTRY_SITE),
    ResearchField("facebook", "Facebook", "website", SOURCE_REGISTRY_SITE),
    ResearchField("linkedin", "LinkedIn", "website", SOURCE_REGISTRY_SITE),
    ResearchField("instagram", "Instagram", "website", SOURCE_REGISTRY_SITE),
    # -------------------------------------------------- hiring & activity
    ResearchField("careers_page", "Careers page", "hiring_activity", SOURCE_CAREERS, SOURCE_SEARCH),
    ResearchField("open_roles", "Open roles", "hiring_activity", SOURCE_CAREERS),
    ResearchField("latest_job_title", "Latest job title", "hiring_activity", SOURCE_CAREERS),
    ResearchField("latest_job_date", "Latest job date", "hiring_activity", SOURCE_CAREERS),
    ResearchField("activity_count", "Public updates", "hiring_activity", SOURCE_ACTIVITY),
    ResearchField("latest_update_title", "Latest update", "hiring_activity", SOURCE_ACTIVITY),
    ResearchField("latest_update_date", "Latest update date", "hiring_activity", SOURCE_ACTIVITY),
)

_BY_FIELD: dict[str, ResearchField] = {f.field: f for f in FIELD_CATALOG}

#: The source-priority matrix, exactly as the spec lays it out: information
#: type -> first source -> fallback. ``None`` means there is no second source,
#: which is itself a fact worth stating rather than leaving blank.
SOURCE_PRIORITY: tuple[tuple[str, str, str | None], ...] = (
    ("Identity", SOURCE_REGISTRY, SOURCE_REGISTRY_SITE),
    ("Roles", SOURCE_ROLES, SOURCE_REGISTRY_SITE),
    ("Locations", SOURCE_SUBUNITS, SOURCE_REGISTRY_SITE),
    ("Financials", SOURCE_ACCOUNTS, None),
    ("Website", SOURCE_REGISTRY_SITE, SOURCE_SEARCH),
    ("Jobs", SOURCE_CAREERS, SOURCE_SEARCH),
    ("Activity", SOURCE_ACTIVITY, SOURCE_SEARCH),
    ("LEI", SOURCE_GLEIF, None),
    ("Identifiers", SOURCE_WIKIDATA, None),
)


# ------------------------------------------------------------------ helpers


def field_domain(field: str) -> str:
    """The domain a field belongs to, or ``""`` when the field is unknown."""
    entry = _BY_FIELD.get(field)
    return entry.domain if entry else ""


def domain_fields(domain: str) -> tuple[ResearchField, ...]:
    """Every catalog field in ``domain``, in catalog order."""
    return tuple(f for f in FIELD_CATALOG if f.domain == domain)


def all_fields() -> tuple[str, ...]:
    return tuple(f.field for f in FIELD_CATALOG)


def primary_source_for(field: str) -> str:
    entry = _BY_FIELD.get(field)
    return entry.primary if entry else ""


def fallback_source_for(field: str) -> str:
    entry = _BY_FIELD.get(field)
    return entry.fallback if entry else ""


def iter_catalog() -> Iterator[ResearchField]:
    return iter(FIELD_CATALOG)


def catalog_matrix() -> dict[str, list[dict[str, str]]]:
    """The catalog grouped by domain, ready to render in a console or report."""
    out: dict[str, list[dict[str, str]]] = {}
    for domain in DOMAINS:
        out[domain] = [
            {"field": f.field, "label": f.label, "primary": f.primary, "fallback": f.fallback}
            for f in domain_fields(domain)
        ]
    return out
