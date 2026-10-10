"""Text normalisation and identifier harvesting from raw page text.

Everything here is deterministic and free. It runs before any model call, and
its output is what the ownership gate and the rule extractor reason over.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from validate import OrgNumber, harvest_from_text

__all__ = [
    "Page",
    "normalize_text",
    "strip_tags",
    "strip_scripts",
    "visible_text",
    "fold_name",
    "extract_postcodes",
    "social_links",
    "jsonld_objects",
    "host_of",
    "is_directory_host",
    "DIRECTORY_HOSTS",
]

#: Hosts that are directories, registers, agencies, marketplaces or social pages
#: rather than a company's own site.
#:
#: This is the *single* canonical list. It is deliberately one constant rather
#: than three: ``sitefinder`` (pre-filter), ``liveweb`` (result ranking) and
#: ``gate`` (post-fetch ownership) each need it, and a list that drifts between
#: them is how a directory page gets published as a company's own website. That
#: is not hypothetical -- a keyless search for "Norsk Hydro ASA" returns
#: ``visverdi.no/selskap/norsk-hydro-asa-914778271``, which *verbatim displays the
#: organisation number*, so the number-on-page rule accepts it. The host list is
#: what stops a number quoted by a directory from being read as ownership.
DIRECTORY_HOSTS: tuple[str, ...] = (
    # registers and public bodies
    "brreg.no", "data.brreg.no", "virksomhet.brreg.no", "skatteetaten.no",
    "altinn.no", "virksomhet.vigo.no", "companieshouse.gov.uk",
    # company-data directories
    "proff.no", "1881.no", "gulesider.no", "regnskapstall.no", "purehelp.no",
    "orgnummer.no", "orgn.no", "yra.no", "visverdi.no", "virksomhet.no",
    "bedriftsopplysninger.no", "kred.no", "soliditet.no", "bisnode.no",
    "dnb.no", "finn.no",
    # business/finance aggregators
    "bloomberg.com", "theorg.com", "apollo.io", "crunchbase.com", "pitchbook.com",
    "bbb.org", "yelp.com", "glassdoor.com", "indeed.com", "linkedin.com",
    # social and encyclopaedic
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "tiktok.com", "wikipedia.org", "no.wikipedia.org", "en.wikipedia.org",
    "google.com", "maps.google.com",
)

#: Faster membership test; built once from the canonical tuple.
_DIRECTORY_SET = frozenset(
    h.removeprefix("www.") for h in DIRECTORY_HOSTS
)


def host_of(url: str) -> str:
    """The registrable host of ``url``, lower-cased, without a ``www.`` prefix."""
    try:
        host = (urlparse(url).netloc or "").casefold()
    except Exception:
        return ""
    return host.removeprefix("www.")


def is_directory_host(url_or_host: str) -> bool:
    """True when ``url_or_host`` is a directory/aggregator rather than an owned site.

    Accepts either a full URL or a bare host, so callers do not have to agree on
    which form they hold. Matches the host itself or any subdomain of a listed
    host, so ``selskap.proff.no`` is caught by ``proff.no``.
    """
    if not url_or_host:
        return False
    host = host_of(url_or_host) if "/" in url_or_host or ":" in url_or_host else url_or_host
    host = host.casefold().removeprefix("www.")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _DIRECTORY_SET)


_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"(?is)<script\b.*?</script>")
_STYLE = re.compile(r"(?is)<style\b.*?</style>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")
_BLOCK = re.compile(r"(?i)</(?:p|div|li|tr|h[1-6]|section|article|br)\s*>|<br\s*/?>")

_LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "sa", "ba", "nr", "nuf", "as", "iks", "kf",
    "enk", "enkeltpersonforetak", "forening", "lag", "stiftelse", "avd",
}

_POSTCODE = re.compile(r"\b(\d{4})\b")
#: Norwegian postcodes run 0001-9999 but the register's valid band is 0001-9999
#: with real values between 0010 and 9990; we validate the band, not the city.
_POSTCODE_MIN, _POSTCODE_MAX = 10, 9990

_SOCIAL_HOSTS = {
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "linkedin.com": "linkedin",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "github.com": "github",
    "pinterest.com": "pinterest",
}


def normalize_text(text: str) -> str:
    """Collapse horizontal whitespace and collapse blank-line runs."""
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n\n", text).strip()


def strip_scripts(markup: str) -> str:
    """Remove comments, ``<style>`` and ``<script>`` blocks."""
    markup = _COMMENT.sub(" ", markup)
    markup = _STYLE.sub(" ", markup)
    return _SCRIPT.sub(" ", markup)


def strip_tags(markup: str, *, block_break: bool = True) -> str:
    """Turn markup into text, preserving paragraph boundaries as newlines.

    JSON-LD and OpenGraph parsing is done on the raw markup; this function is
    only for the human-readable body that the gate matches against and that gets
    handed to the model.
    """
    if block_break:
        markup = _BLOCK.sub("\n", markup)
    text = _TAG.sub(" ", markup)
    return normalize_text(html.unescape(text))


def visible_text(markup: str) -> str:
    """Scripts removed, then tags stripped — what a reader would actually see."""
    return strip_tags(strip_scripts(markup))


_JSONLD = re.compile(
    r'(?is)<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)


def _flatten_jsonld(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            yield from _flatten_jsonld(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            yield from _flatten_jsonld(graph)
        yield data


def jsonld_objects(markup: str) -> list[dict[str, Any]]:
    """Every ``application/ld+json`` object on a page, flattened.

    Handles the three shapes seen in the wild: a single object, a list, and a
    ``@graph`` wrapper. A malformed block is skipped rather than raising, because
    one broken script tag is not a reason to lose the whole page.
    """
    out: list[dict[str, Any]] = []
    for match in _JSONLD.finditer(markup or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        out.extend(_flatten_jsonld(data))
    return out


def fold_name(name: str) -> str:
    """Fold a company name into a comparable key.

    ``"2Care BPA AS"`` and ``"2CARE BPA"`` both become ``"2care bpa"``. Legal
    form suffixes and punctuation are dropped so register and web spellings can
    be compared, but the distinguishing tokens are preserved.
    """
    if not name:
        return ""
    text = name.casefold()
    text = re.sub(r"[^\w\sæøå]", " ", text)
    tokens = [t for t in text.split() if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def extract_postcodes(text: str) -> set[str]:
    """Four digit runs in the Norwegian postal range."""
    found = set()
    for match in _POSTCODE.finditer(text):
        value = int(match.group(1))
        if _POSTCODE_MIN <= value <= _POSTCODE_MAX:
            found.add(f"{value:04d}")
    return found


def social_links(markup: str, base_url: str = "") -> dict[str, str]:
    """Map social platform -> profile URL, from anchors only.

    Anchors are used rather than raw host mentions because a page linking to
    ``facebook.com/sharer`` is sharing, not owning a profile.
    """
    out: dict[str, str] = {}
    for match in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>', markup):
        href = match.group(1).strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        absolute = urljoin(base_url, href) if base_url else href
        host = urlparse(absolute).netloc.casefold().removeprefix("www.")
        for candidate, label in _SOCIAL_HOSTS.items():
            if host == candidate or host.endswith("." + candidate):
                path = urlparse(absolute).path.strip("/")
                if not path or path in {"sharer", "share.php", "tr", "intent", "home"}:
                    continue
                out.setdefault(label, absolute)
    return out


@dataclass(slots=True)
class Page:
    """A fetched web page, already reduced to the forms we reason over."""

    url: str
    status: int = 200
    markup: str = ""
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str = ""
    retrieved_at: str = ""

    _text: str | None = field(default=None, repr=False)
    _orgnrs: list[str] | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        """Visible text, computed once and memoised."""
        if self._text is None:
            self._text = visible_text(self.markup)
        return self._text

    @property
    def org_numbers(self) -> list[str]:
        """Checksum-valid organisation numbers appearing anywhere on the page.

        Both the visible text and the raw mailto/tel/JSON-LD payloads are
        scanned, because a number is often present only in structured data or in
        a link target rather than in the rendered prose.
        """
        if self._orgnrs is None:
            blob = self.text + "\n" + self.markup
            self._orgnrs = sorted(harvest_from_text(blob))
        return self._orgnrs

    def proves(self, org: OrgNumber) -> bool:
        """The ownership gate's primary test."""
        return org.value in self.org_numbers
