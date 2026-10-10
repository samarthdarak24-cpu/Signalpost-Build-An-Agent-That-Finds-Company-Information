"""The research planner: for this company, what can I get from each domain?

The planner is the layer the spec puts directly under the validator. Before a
single request is spent, it answers one question per domain:

    *Can source A answer this?  YES -> use it.  NO -> try source B.
     Still missing?  -> hand the residue to the LLM.*

Crucially it only *decides*; it fetches nothing. That separation is what lets
the same planner run offline (to explain what a run *would* do), drive the live
pipeline, and populate the coverage matrix, without any of them duplicating the
source-priority rules -- those live in one place, :mod:`research_fields`.

The plan is deliberately honest about the two things that gate everything else:

*   **Sole traders.** An ``ENK`` is a natural person, so the website domain is
    *skipped*, not deferred -- there is nothing to wait for.
*   **The web depends on a verified site.** Hiring and activity are read *off a
    verified site*, so if the website domain is not ready they are deferred
    rather than attempted against an unverified host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from research_fields import (
    DOMAIN_LABELS,
    DOMAINS,
    SOURCE_ACCOUNTS,
    SOURCE_ACTIVITY,
    SOURCE_CAREERS,
    SOURCE_REGISTRY,
    SOURCE_REGISTRY_SITE,
    SOURCE_ROLES,
    SOURCE_SEARCH,
    SOURCE_SUBUNITS,
    domain_fields,
)

__all__ = ["DomainPlan", "ResearchPlan", "plan", "build_plan"]

#: Domain readiness. ``ready`` = attempt it now; ``deferred`` = would attempt
#: it, but a precondition (network, a verified site) is missing; ``skip`` = it
#: must not be attempted at all, and why is part of the record.
READY = "ready"
DEFERRED = "deferred"
SKIP = "skip"


@dataclass(slots=True)
class DomainPlan:
    """What the planner intends to do for one domain."""

    domain: str
    fields: tuple[str, ...]
    primary: str
    fallback: str = ""
    status: str = READY
    reason: str = ""

    @property
    def label(self) -> str:
        return DOMAIN_LABELS.get(self.domain, self.domain)

    @property
    def will_attempt(self) -> bool:
        return self.status == READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "label": self.label,
            "fields": list(self.fields),
            "primary": self.primary,
            "fallback": self.fallback,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(slots=True)
class ResearchPlan:
    """The five-domain plan for one company."""

    orgnr: str
    legal_name: str = ""
    domains: list[DomainPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def domain(self, key: str) -> DomainPlan | None:
        for entry in self.domains:
            if entry.domain == key:
                return entry
        return None

    def ready_domains(self) -> list[str]:
        return [d.domain for d in self.domains if d.will_attempt]

    def attempted_fields(self) -> tuple[str, ...]:
        """Every field the plan intends to try to fill, in domain order."""
        out: list[str] = []
        for entry in self.domains:
            if entry.will_attempt:
                out.extend(entry.fields)
        return tuple(out)

    def summary(self) -> str:
        bits = [f"{d.label}: {d.status}" for d in self.domains]
        return f"{self.orgnr or '-'} -> " + "; ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "orgnr": self.orgnr,
            "legal_name": self.legal_name,
            "domains": [d.to_dict() for d in self.domains],
            "ready_domains": self.ready_domains(),
            "notes": self.notes,
        }


def _fields(domain: str) -> tuple[str, ...]:
    return tuple(f.field for f in domain_fields(domain))


def _web_allowed(entity: dict[str, Any], override: bool | None) -> bool:
    if override is not None:
        return override
    try:
        from extract_rules import is_organisational_contact  # noqa: PLC0415

        return is_organisational_contact(entity)
    except Exception:
        # If the guard cannot be evaluated, fail closed: do not touch the web.
        return False


def plan(
    entity: dict[str, Any],
    *,
    org: Any = None,
    offline: bool = False,
    fetch_web: bool = True,
    fetch_financials: bool = True,
    search_available: bool = False,
    web_allowed: bool | None = None,
) -> ResearchPlan:
    """Decide, per domain, what to retrieve and from which source.

    ``search_available`` reflects whether a paid search fallback exists (a Brave
    key). It only affects the *stated* fallback, never whether a free source is
    used: the registry and the register's own role/sub-unit listings are always
    tried first and cost nothing.
    """
    number = ""
    if org is not None:
        number = getattr(org, "value", None) or str(org)
    if not number:
        number = str((entity.get("organisasjonsnummer") or "")).strip()
    name = str(entity.get("navn") or "").strip()

    plan_out = ResearchPlan(orgnr=number, legal_name=name)

    # 1. company record -- the register answers almost all of it, always.
    plan_out.domains.append(
        DomainPlan(
            domain="company",
            fields=_fields("company"),
            primary=SOURCE_REGISTRY,
            status=SKIP if offline else READY,
            reason="" if not offline else "offline: registry mirror only",
        )
    )

    # 2. financials -- Regnskapsregisteret, free, but only when enabled.
    if offline:
        plan_out.domains.append(
            DomainPlan("financials", _fields("financials"), SOURCE_ACCOUNTS,
                       status=DEFERRED, reason="offline: no network")
        )
    elif not fetch_financials:
        plan_out.domains.append(
            DomainPlan("financials", _fields("financials"), SOURCE_ACCOUNTS,
                       status=DEFERRED, reason="financials disabled for this run")
        )
    else:
        plan_out.domains.append(
            DomainPlan("financials", _fields("financials"), SOURCE_ACCOUNTS, status=READY)
        )

    # 3. people & locations -- the register's own role and sub-unit listings.
    plan_out.domains.append(
        DomainPlan(
            domain="people_locations",
            fields=_fields("people_locations"),
            primary=SOURCE_ROLES,
            fallback=SOURCE_SUBUNITS,
            status=DEFERRED if offline else READY,
            reason="offline: no network" if offline else "",
        )
    )

    # 4. website -- registry URL first, search only as a fallback.
    allowed = _web_allowed(entity, web_allowed)
    if not allowed:
        plan_out.domains.append(
            DomainPlan("website", _fields("website"), SOURCE_REGISTRY_SITE, SOURCE_SEARCH,
                       status=SKIP,
                       reason="sole trader (ENK): web contact details are personal data")
        )
    elif offline:
        plan_out.domains.append(
            DomainPlan("website", _fields("website"), SOURCE_REGISTRY_SITE, SOURCE_SEARCH,
                       status=DEFERRED, reason="offline: no network")
        )
    elif not fetch_web:
        plan_out.domains.append(
            DomainPlan("website", _fields("website"), SOURCE_REGISTRY_SITE, SOURCE_SEARCH,
                       status=DEFERRED, reason="web disabled for this run")
        )
    else:
        plan_out.domains.append(
            DomainPlan("website", _fields("website"), SOURCE_REGISTRY_SITE, SOURCE_SEARCH,
                       status=READY,
                       reason="" if search_available else "no search fallback configured")
        )

    # 5. hiring & public activity -- read off a verified site, so it is gated on
    #    the website domain being ready.
    website = plan_out.domain("website")
    if website is not None and website.will_attempt:
        plan_out.domains.append(
            DomainPlan("hiring_activity", _fields("hiring_activity"),
                       SOURCE_CAREERS, SOURCE_ACTIVITY, status=READY,
                       reason="" if search_available else "no search fallback configured")
        )
    else:
        plan_out.domains.append(
            DomainPlan("hiring_activity", _fields("hiring_activity"),
                       SOURCE_CAREERS, SOURCE_ACTIVITY, status=DEFERRED,
                       reason="needs a verified site; website domain is not ready")
        )

    if not name:
        plan_out.notes.append("no legal name on the register payload; identity is weaker")
    if offline:
        plan_out.notes.append("offline run: only the registry mirror and the bundled seed answer")
    return plan_out


#: Alias kept so callers can read ``build_plan`` as the verb.
build_plan = plan
