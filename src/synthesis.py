"""Synthesis: the five reference views, built only from evidence we hold.

The reference profile reads as prose -- *Company brief*, *Latest financials*,
*Who runs it?*, *Working here?*, *Recent activity* -- but every sentence in it is
backed by a fact on the profile. That is the whole discipline of this module:
**a synthesis sentence is a pointer to a fact, never a new claim.** If a field
was not published, the view does not invent a sentence for it; it goes into the
missing panel instead, which is the honest place for a gap.

So a view is a list of :class:`ViewLine`, and each line carries the value, the
source URL and the verbatim evidence it came from. Nothing here calls a model or
touches the network; it only re-presents facts the pipeline already proved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from profiles import Profile
from research_fields import DOMAINS, DOMAIN_LABELS, domain_fields

__all__ = [
    "ViewLine",
    "SECTIONS",
    "synthesis",
    "missing_panel",
    "render_text",
]

#: The five views, in the order the reference presents them.
SECTIONS: tuple[str, ...] = (
    "company_brief",
    "latest_financials",
    "who_runs_it",
    "working_here",
    "recent_activity",
)

SECTION_LABELS: dict[str, str] = {
    "company_brief": "Company brief",
    "latest_financials": "Latest financials",
    "who_runs_it": "Who runs it?",
    "working_here": "Working here?",
    "recent_activity": "Recent activity",
}

#: field -> the human label a view line shows. Shared so a field reads the same
#: wherever it appears.
_FIELD_LABELS: dict[str, str] = {
    "legal_name": "Name",
    "description": "What it does",
    "industry": "Industry",
    "address": "Registered address",
    "city": "City",
    "municipality": "Municipality",
    "ceo": "CEO / managing director",
    "board_chair": "Board chair",
    "board_members": "Board members",
    "auditor": "Auditor",
    "revenue_latest": "Revenue",
    "operating_result": "Operating result",
    "net_result": "Annual result",
    "total_assets": "Assets",
    "equity": "Equity",
    "debt": "Debt",
    "latest_accounts_year": "Latest accounts year",
    "accounting_year": "Reporting period",
    "careers_page": "Careers page",
    "open_roles": "Open roles",
    "latest_job_title": "Latest job",
    "location_count": "Registered locations",
    "business_locations": "Workplaces",
    "latest_update_title": "Latest update",
    "latest_update_date": "Update date",
    "activity_count": "Public updates",
}


@dataclass(slots=True)
class ViewLine:
    """One evidence-backed sentence fragment in a view."""

    field: str
    label: str
    value: Any
    source_url: str = ""
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field, "label": self.label, "value": self.value,
            "source_url": self.source_url, "evidence": self.evidence,
        }


def _line(profile: Profile, field: str) -> ViewLine | None:
    fact = profile.get(field)
    if fact is None:
        return None
    return ViewLine(
        field=field,
        label=_FIELD_LABELS.get(field, field.replace("_", " ")),
        value=fact.value,
        source_url=fact.source_url,
        evidence=fact.evidence,
    )


def _view(profile: Profile, fields: tuple[str, ...]) -> list[ViewLine]:
    lines: list[ViewLine] = []
    for name in fields:
        line = _line(profile, name)
        if line is not None:
            lines.append(line)
    return lines


#: Which fields feed which view. Order within a view is the order a reader wants.
_VIEW_FIELDS: dict[str, tuple[str, ...]] = {
    "company_brief": ("legal_name", "description", "industry", "address", "city",
                      "municipality", "ceo", "revenue_latest"),
    "latest_financials": ("latest_accounts_year", "accounting_year", "revenue_latest",
                          "operating_result", "net_result", "total_assets",
                          "equity", "debt"),
    "who_runs_it": ("ceo", "board_chair", "board_members", "auditor"),
    "working_here": ("careers_page", "open_roles", "latest_job_title",
                     "location_count", "business_locations"),
    "recent_activity": ("latest_update_title", "latest_update_date", "activity_count"),
}


def synthesis(profile: Profile) -> dict[str, Any]:
    """Build the five views from the profile's published facts."""
    views: dict[str, Any] = {}
    for section in SECTIONS:
        lines = _view(profile, _VIEW_FIELDS[section])
        views[section] = {
            "label": SECTION_LABELS[section],
            "lines": [line.to_dict() for line in lines],
            "empty": not lines,
        }
    return {
        "orgnr": profile.org_number,
        "legal_name": profile.legal_name,
        "views": views,
        "missing": missing_panel(profile),
    }


def missing_panel(
    profile: Profile,
    *,
    sources_checked: tuple[str, ...] = (),
    checked_at: str = "",
) -> list[dict[str, Any]]:
    """The Unknown / Missing panel: what we looked for and did not find.

    A gap is published as a gap -- with the reason, the sources tried and the
    date -- rather than left as a silent dash. This is the challenge's rule that
    missing information must be clearly marked, not quietly omitted.
    """
    reasons = {s.field: s.reason for s in profile.empty_fields}
    out: list[dict[str, Any]] = []
    for domain in DOMAINS:
        for entry in domain_fields(domain):
            if profile.value(entry.field) is not None:
                continue
            out.append({
                "domain": domain,
                "domain_label": DOMAIN_LABELS.get(domain, domain),
                "field": entry.field,
                "label": entry.label,
                "reason": reasons.get(
                    entry.field,
                    "no verified source produced this field during this run",
                ),
                "sources_checked": list(sources_checked),
                "checked_at": checked_at,
            })
    return out


def render_text(synth: dict[str, Any]) -> str:
    """A plain-text rendering of the synthesis, for the CLI and the console."""
    lines: list[str] = []
    title = synth.get("legal_name") or synth.get("orgnr") or "Company"
    lines.append(f"{title} ({synth.get('orgnr', '')})")
    lines.append("=" * max(8, len(title)))
    for section in SECTIONS:
        view = synth["views"][section]
        lines.append("")
        lines.append(view["label"].upper())
        if view["empty"]:
            lines.append("  not available in this run")
            continue
        for line in view["lines"]:
            lines.append(f"  {line['label']}: {line['value']}")
    missing = synth.get("missing", [])
    if missing:
        lines.append("")
        lines.append(f"NOT AVAILABLE ({len(missing)} fields)")
        for item in missing[:12]:
            lines.append(f"  {item['label']} ({item['domain_label']})")
    return "\n".join(lines)
