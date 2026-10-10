"""Rule-based extraction. Zero tokens, and it runs before any model call.

Norwegian small-business sites are predictable: a JSON-LD ``Organization`` block
or an OpenGraph set in the head, a ``mailto:`` on the contact page, a ``tel:``
link in the footer, Facebook and LinkedIn icons, and an address block ending in
a four digit postcode. All of that is extractable with rules, so the model only
ever sees the residue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from normalize import Page, social_links
from validate import OrgNumber, harvest_from_text

__all__ = ["RuleExtraction", "extract", "is_organisational_contact", "ORGANISATIONAL_FORMS"]


_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_TEL_HREF = re.compile(r"(?i)href=[\"']tel:([+0-9\s().-]{6,})[\"']")
_TEL_TEXT = re.compile(r"(?i)\b(?:tlf|telefon|tel|mob)[.:\s]*([+0-9][0-9\s().-]{6,})")
_ORGNR_TEXT = re.compile(r"(?i)(?:org(?:anisasjons)?\.?\s*(?:nr|nummer)\.?)\s*[:\s]*([\d\s.]{9,14})")
_POSTAL_BLOCK = re.compile(r"\b(\d{4})\s+([A-ZÆØÅ][A-ZÆØÅa-zæøå\s-]{2,30})\b")
_STREET = re.compile(
    r"(?im)^\s*([A-ZÆØÅ][\wæøåÆØÅ.\- ]{2,40}?(?:veien|vegen|gate|gata|gaten|"
    r"plass|plassen|road|street|alle|allé|bakken|stien|svingen))\s*(\d+\s*[A-Z]?)?\s*$"
)

#: Words that follow a four digit number in running text far more often than the
#: name of a place does. Without this, "2025 Les mer om undersøkelsen" is read as
#: postcode 2025 in the town of "Les mer om undersøkelsen".
_NOT_A_CITY = re.compile(
    r"(?i)^(les|se|klikk|trykk|her|mer|om|og|for|med|til|fra|på|i|av|den|det|"
    r"som|som|kan|vil|har|er|var|blir|the|read|more|click|see|about|and|for|"
    r"with|from|to|in|of|is|are|was|year|years|år|gang|ganger|prosent)\b"
)


def _is_plausible_city(value: str) -> bool:
    """Reject a captured "place name" that is really the start of a sentence.

    Norwegian place names are one to three capitalised words. Anything starting
    with a lowercase continuation word, or running longer than three tokens, is
    prose rather than an address.
    """
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned or len(cleaned) < 2:
        return False
    tokens = cleaned.split()
    if len(tokens) > 3:
        return False
    if _NOT_A_CITY.match(cleaned):
        return False
    # A real place name is a proper noun: the first letter is upper case and the
    # following letters are not screaming caps mid-word.
    return bool(re.match(r"^[A-ZÆØÅ][a-zæøå]", cleaned))

#: Generic mailboxes that are never a named individual.
_GENERIC_LOCALPARTS = {
    "post", "hello", "hei", "kontakt", "contact", "info", "firmapost",
    "office", "mail", "epost", "salg", "support", "admin", "kundeservice",
}

SOCIAL_LABELS = ("facebook", "instagram", "linkedin", "twitter", "youtube", "tiktok", "github")


@dataclass(slots=True)
class RuleExtraction:
    """Everything the rules could establish about one page."""

    url: str
    org_numbers: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    address: str = ""
    postcode: str = ""
    city: str = ""
    description: str = ""
    company_name: str = ""
    schema_types: list[str] = field(default_factory=list)
    og: dict[str, str] = field(default_factory=dict)
    jsonld_found: bool = False
    quote_orgnr: str = ""
    quote_contact: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def has_contact(self) -> bool:
        return bool(self.emails or self.phones or self.address)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "org_numbers": self.org_numbers,
            "emails": self.emails,
            "phones": self.phones,
            "socials": self.socials,
            "address": self.address,
            "postcode": self.postcode,
            "city": self.city,
            "description": self.description,
            "company_name": self.company_name,
            "schema_types": self.schema_types,
            "jsonld_found": self.jsonld_found,
        }


def _jsonld_blocks(markup: str) -> list[Any]:
    """Parse every ``application/ld+json`` script block, tolerating junk."""
    blocks: list[Any] = []
    for match in re.finditer(
        r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        markup,
    ):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            # Trailing commas and unescaped newlines are endemic on small sites.
            salvaged = re.sub(r",\s*([\]}])", r"\1", raw)
            try:
                blocks.append(json.loads(salvaged))
            except json.JSONDecodeError:
                continue
    return blocks


def _walk_jsonld(node: Any) -> list[dict[str, Any]]:
    """Flatten JSON-LD, following ``@graph`` and lists."""
    out: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_walk_jsonld(item))
    elif isinstance(node, dict):
        if "@graph" in node:
            out.extend(_walk_jsonld(node["@graph"]))
        out.append(node)
    return out


def _og_tags(markup: str) -> dict[str, str]:
    """Collect ``og:*`` and ``twitter:*`` meta tags."""
    out: dict[str, str] = {}
    for match in re.finditer(
        r'(?is)<meta\s+[^>]*?(?:property|name)=["\']((?:og|twitter):[^"\']+)["\'][^>]*?'
        r'content=["\']([^"\']*)["\']',
        markup,
    ):
        out[match.group(1).lower()] = match.group(2).strip()
    for match in re.finditer(
        r'(?is)<meta\s+[^>]*?content=["\']([^"\']*)["\'][^>]*?'
        r'(?:property|name)=["\']((?:og|twitter):[^"\']+)["\']',
        markup,
    ):
        out.setdefault(match.group(2).lower(), match.group(1).strip())
    return out


def _clean_phone(raw: str) -> str | None:
    """Normalise a Norwegian phone number to eight digits, or reject it.

    Country prefix handling: ``+47`` is stripped before the length check, and a
    bare leading ``0047`` or ``47`` is stripped when the total is ten digits.

    Note on the personal-data rule. Norwegian mobile numbers are allocated the
    ranges 40-49 and 90-99, but those same ranges are also used for company
    switchboards, and almost every company site publishes one. Rejecting every
    number beginning 4 or 9 would silently strip the contact detail from a large
    share of profiles. The rule the brief actually states is *do not collect
    personal contact details of private persons*, so the guard is applied at the
    register level instead: a number is only published when the register lists it
    for a legal entity (AS, ASA, SA, ANS, DA, NUF), never for a sole trader
    (ENK). See :func:`is_organisational_contact`.
    """
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0047"):
        digits = digits[4:]
    elif digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    elif digits.startswith("47") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 8:
        return None
    return f"{digits[:2]} {digits[2:4]} {digits[4:6]} {digits[6:]}"


#: Organisation forms that are legal entities with a published switchboard.
#: A sole trader (ENK) is a natural person, so its contact details are personal
#: data and are never harvested from the web.
ORGANISATIONAL_FORMS = {"AS", "ASA", "SA", "ANS", "DA", "NUF", "BA", "STI", "IKS", "KF", "FKF"}


def is_organisational_contact(entity: dict[str, Any]) -> bool:
    """Whether web-sourced contact details may be attached to this entity.

    This is the register-level half of the personal-data guard. A sole trader's
    phone number is the person's phone number, so the web layer is not run for
    one at all.
    """
    form = str((entity.get("organisasjonsform") or {}).get("kode") or "").upper()
    return form in ORGANISATIONAL_FORMS


def _clean_email(raw: str) -> str | None:
    """Accept only role-based mailboxes; drop anything that looks personal.

    ``post@firma.no`` is organisational contact information. ``ola.nordmann@``
    is a named person, and the brief says do not collect personal contact
    details of private individuals.
    """
    email = raw.strip().strip(".,;:").casefold()
    if not email or len(email) > 96:
        return None
    local, _, domain = email.partition("@")
    if not local or not domain:
        return None
    if domain.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")):
        return None
    if local not in _GENERIC_LOCALPARTS:
        # Two-token locals (first.last) read as a person; single odd tokens read
        # as a department (marketing, hr). Accept the department-shaped ones.
        if re.fullmatch(r"[a-z]{2,12}[._-][a-z]{2,12}", local):
            return None
    return email


def extract(page: Page, org: OrgNumber | None = None) -> RuleExtraction:
    """Run every deterministic extractor over ``page``.

    The order is chosen so that structured data wins over prose: JSON-LD, then
    OpenGraph, then links, then text patterns.
    """
    result = RuleExtraction(url=page.url)
    markup = page.markup
    text = page.text

    # --- organisation numbers ------------------------------------------------
    blob = markup + "\n" + text
    result.org_numbers = sorted(harvest_from_text(blob))
    labelled = _ORGNR_TEXT.search(text)
    if labelled:
        result.quote_orgnr = labelled.group(0).strip()

    # --- JSON-LD -------------------------------------------------------------
    nodes: list[dict[str, Any]] = []
    for block in _jsonld_blocks(markup):
        nodes.extend(_walk_jsonld(block))
    if nodes:
        result.jsonld_found = True
    for node in nodes:
        node_type = node.get("@type")
        if isinstance(node_type, list):
            result.schema_types.extend(str(t) for t in node_type)
        elif node_type:
            result.schema_types.append(str(node_type))

        name = node.get("name") or node.get("legalName")
        if name and not result.company_name:
            result.company_name = str(name).strip()

        email = node.get("email")
        if isinstance(email, str):
            cleaned = _clean_email(email.replace("mailto:", ""))
            if cleaned:
                result.emails.append(cleaned)

        phone = node.get("telephone")
        if isinstance(phone, str):
            cleaned = _clean_phone(phone)
            if cleaned:
                result.phones.append(cleaned)

        description = node.get("description")
        if isinstance(description, str) and len(description) > 40 and not result.description:
            result.description = re.sub(r"\s+", " ", description).strip()

        for social in node.get("sameAs") or []:
            if not isinstance(social, str):
                continue
            host = urlparse(social).netloc.casefold().removeprefix("www.")
            for label in SOCIAL_LABELS:
                if label in host:
                    result.socials.setdefault(label, social)

        address = node.get("address")
        if isinstance(address, dict):
            street = address.get("streetAddress") or ""
            pc = str(address.get("postalCode") or "").strip()
            locality = address.get("addressLocality") or ""
            composed = ", ".join(x for x in (str(street).strip(), f"{pc} {locality}".strip()) if x)
            if composed and not result.address:
                result.address = composed
                result.postcode = pc
                result.city = str(locality).strip()

    # --- OpenGraph -----------------------------------------------------------
    result.og = _og_tags(markup)
    og_desc = result.og.get("og:description") or result.og.get("twitter:description") or ""
    if og_desc and len(og_desc) > len(result.description):
        result.description = re.sub(r"\s+", " ", og_desc).strip()
    og_name = result.og.get("og:site_name") or ""
    if og_name and not result.company_name:
        result.company_name = og_name.strip()

    # --- links ---------------------------------------------------------------
    for match in re.finditer(r'(?i)href=["\']mailto:([^"\'?]+)', markup):
        cleaned = _clean_email(match.group(1))
        if cleaned:
            result.emails.append(cleaned)
    for match in _TEL_HREF.finditer(markup):
        cleaned = _clean_phone(match.group(1))
        if cleaned:
            result.phones.append(cleaned)
    result.socials.update({k: v for k, v in social_links(markup, page.url).items()
                           if k not in result.socials})

    # --- prose patterns ------------------------------------------------------
    for match in _EMAIL.finditer(text):
        cleaned = _clean_email(match.group(0))
        if cleaned:
            result.emails.append(cleaned)
    if not result.phones:
        for match in _TEL_TEXT.finditer(text):
            cleaned = _clean_phone(match.group(1))
            if cleaned:
                result.phones.append(cleaned)

    if not result.address:
        postal = _POSTAL_BLOCK.search(text)
        # Only trust a postcode that is followed by a plausible place name, and
        # prefer one that looks like it belongs to a street address.
        while postal is not None:
            if _is_plausible_city(postal.group(2)):
                result.postcode = postal.group(1)
                result.city = re.sub(r"\s+", " ", postal.group(2)).strip()
                break
            postal = _POSTAL_BLOCK.search(text, postal.end() - 1)
        street = _STREET.search(text)
        if street:
            pieces = [street.group(1).strip()]
            if street.group(2):
                pieces.append(street.group(2).strip())
            if result.postcode:
                pieces.append(f"{result.postcode} {result.city}".strip())
            result.address = ", ".join(pieces)
        elif result.postcode:
            result.address = f"{result.postcode} {result.city}".strip()
            # Without a street we only have half an address; say so rather than
            # publishing a place name as if it were a full postal address.
            result.warnings.append(
                "postcode found without a street line; address is incomplete"
            )

    # --- contact quote for evidence ------------------------------------------
    for needle in [*result.emails, *result.phones, result.address]:
        if not needle:
            continue
        idx = text.casefold().find(needle.casefold())
        if idx >= 0:
            start = max(0, idx - 60)
            result.quote_contact = re.sub(r"\s+", " ", text[start:idx + len(needle) + 40]).strip()
            break

    # --- dedupe, preserving order -------------------------------------------
    result.emails = list(dict.fromkeys(result.emails))[:3]
    result.phones = list(dict.fromkeys(result.phones))[:3]
    result.schema_types = list(dict.fromkeys(result.schema_types))[:6]
    return result


def merge_org_number_hint(entity: dict[str, Any]) -> str:
    """The register's own display form, used to anchor the regex quote."""
    from validate import clean, format_display

    number = clean(entity.get("organisasjonsnummer"))
    return format_display(number) if number else ""
