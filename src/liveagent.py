"""The live research agent: any identifier in, a sourced answer out, per session.

This is the layer the request asks for -- *accept any query or identifier, fetch
the latest information from the internet, and answer with source references,
handling follow-ups in the same session* -- built so it does not contradict the
discipline of everything below it.

The design is a small, deliberate pipeline:

    CLASSIFY   what kind of input is this?
               an organisation number, a company name, a bare domain, or a
               free-text question?
    RESOLVE    turn the input into a *target*. A number is looked up in the
               register live (exactly the path ``run.py lookup`` uses); a name
               or domain searches the open web and picks the best owned page;
               a question is searched directly.
    GATHER     pour the register facts and/or the search results into one
               evidence pool, each entry carrying its URL, quote and timestamp.
    ANSWER     select the evidence that actually answers *this* question, group
               it into a concise reply, and cite every sentence.

The session object keeps the resolved target and the running Q&A so a follow-up
("and what about their finances?", "who runs it?") reuses the company already
established instead of making the user repeat the identifier. That is the whole
point of "within the same session".

Two guardrails carry over from the rest of the project and must not be relaxed:

* **An answer is a pointer to evidence, never a new claim.** Every line of the
  reply names the source it came from. If nothing in the pool answers the
  question, the reply says so and lists what was searched -- it does not reach
  for a plausible-sounding sentence.
* **A search snippet is a lead, not proof.** Snippets are labelled ``web``
  (lower confidence); a value read off the register or a verified page is
  labelled ``registry`` / ``web_rules`` and outranks them when both exist.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from profiles import Fact, Profile, utcnow_iso
from validate import clean, format_display

from liveweb import (
    SearchOutcome,
    SearchResult,
    fetch_page_text,
    general_search,
    search_web,
    wikipedia_summary,
)

__all__ = [
    "Evidence",
    "ResearchAnswer",
    "LiveSession",
    "SessionStore",
    "classify_input",
    "InputKind",
]

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


# ------------------------------------------------------------------ classifier

#: The four shapes an input can take. Kept as plain strings so they serialise.
InputKind = str  # "orgnr" | "name" | "domain" | "question"

_DOMAIN_RE = re.compile(r"^(?:https?://)?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z]{2,})(?:[/:?#].*)?$", re.I)


def _looks_like_domain(text: str) -> bool:
    """True when ``text`` is a bare host (optionally with a scheme or path).

    Deliberately strict about the TLD being alphabetic and at least two
    characters, so ``995849364`` (all digits) and ``AS`` (no dot) are excluded --
    but permissive about a scheme, a ``www.`` prefix and a trailing path, all of
    which still describe a single site.
    """
    stripped = text.strip().casefold()
    if stripped.startswith(("http://", "https://")):
        stripped = stripped.split("://", 1)[1]
    host = stripped.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split(":", 1)[0].removeprefix("www.")
    if not host or " " in host:
        return False
    parts = host.split(".")
    if len(parts) < 2:
        return False
    tld = parts[-1]
    if not tld.isalpha() or len(tld) < 2:
        return False
    return all(p and re.fullmatch(r"[a-z0-9-]+", p) for p in parts)
_QUESTION_STARTS = (
    "what", "who", "when", "where", "why", "how", "which", "is ", "are ", "does ",
    "do ", "did ", "can ", "could ", "should ", "will ", "hva", "hvem", "hvor",
    "når", "nar", "hvorfor", "hvordan", "er ", "har ", "kan ",
)
_COMPANY_SUFFIXES = (
    " as", " asa", " ans", " da", " sa", " nuf", " enk", " ba", " sti", " fkf",
    " kf", " iks", " pbl", " ab", " ltd", " plc", " gmbh", " inc", " llc",
)


def classify_input(raw: str) -> InputKind:
    """Decide what shape ``raw`` is -- number, name, domain or question.

    The order matters and is deliberate: a checksum-valid number is a number no
    matter what else it looks like; a bare ``something.tld`` with no spaces is a
    domain; a sentence that starts with a question word or ends in ``?`` is a
    question; everything else that reads like a proper noun is a company name.
    The rule is transparent so a user can predict what the agent will do.
    """
    text = (raw or "").strip()
    if not text:
        return "question"
    if clean(text) is not None:
        return "orgnr"
    if text.endswith("?"):
        return "question"
    # A domain, with or without a scheme/path: "equinor.no", "www.equinor.no",
    # "https://www.equinor.no/careers". No spaces is the tell that it is a host
    # and not a sentence.
    if " " not in text and _looks_like_domain(text):
        return "domain"
    lowered = text.casefold()
    if any(lowered.startswith(word) for word in _QUESTION_STARTS) and len(text.split()) >= 2:
        return "question"
    # A trailing legal-form suffix is a strong "this is a company" signal.
    if any(lowered.endswith(suffix) for suffix in _COMPANY_SUFFIXES):
        return "name"
    # Several words with no question word: treat as a name (the common case for
    # "Equinor" or "Norsk Hydro ASA live") rather than a question.
    return "name"


# -------------------------------------------------------------------- evidence


@dataclass(slots=True)
class Evidence:
    """One sourced statement in the live pool.

    ``kind`` is the provenance tier and drives confidence ordering:
    ``registry`` (official register / accounts), ``web_rules`` (read off a page
    we fetched), ``web`` (a search snippet -- a lead), ``identifier`` (GLEIF /
    Wikidata). A field present at a higher tier wins over a lower one.
    """

    field: str
    value: str
    source_url: str
    quote: str = ""
    kind: str = "web"
    retrieved_at: str = ""
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field, "value": self.value, "source_url": self.source_url,
            "quote": self.quote, "kind": self.kind,
            "retrieved_at": self.retrieved_at, "confidence": round(self.confidence, 3),
        }


_TIER = {"registry": 0, "identifier": 1, "web_rules": 2, "web": 3}


@dataclass(slots=True)
class ResearchAnswer:
    """The concise reply plus every source behind it."""

    question: str
    kind: InputKind
    target: str = ""              #: the resolved company/domain the answer is about
    org_number: str = ""
    answer: str = ""
    answered: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    engine: str = ""
    profile_path: str = ""
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question, "kind": self.kind, "target": self.target,
            "org_number": self.org_number, "answer": self.answer,
            "answered": self.answered, "sources": self.sources,
            "evidence": self.evidence, "searched": self.searched,
            "engine": self.engine, "profile_path": self.profile_path,
            "seconds": round(self.seconds, 3), "notes": self.notes,
        }

    def render(self) -> str:
        """A plain-text rendering for the CLI."""
        lines: list[str] = []
        head = self.target or self.org_number or self.question
        lines.append(f"{head}")
        lines.append("-" * min(len(head) + 4, 72))
        lines.append(self.answer or "No sourced answer was found.")
        if self.sources:
            lines.append("")
            lines.append("SOURCES")
            for i, src in enumerate(self.sources, 1):
                label = src.get("title") or src.get("field") or src.get("url")
                lines.append(f"  [{i}] {label}")
                lines.append(f"      {src.get('url', '')}")
                if src.get("quote"):
                    lines.append(f"      \"{src['quote'][:120]}\"")
        if not self.answered and self.searched:
            lines.append("")
            lines.append("searched: " + ", ".join(self.searched))
        return "\n".join(lines)


# ------------------------------------------------------------- register lookup


def _facts_to_evidence(profile: Profile, retrieved_at: str) -> list[Evidence]:
    """Every published profile fact becomes a ``registry``-tier evidence entry."""
    out: list[Evidence] = []
    for fact in profile.facts:
        out.append(Evidence(
            field=fact.field,
            value=str(fact.value),
            source_url=fact.source_url,
            quote=fact.evidence,
            kind=fact.layer if fact.layer in _TIER else "registry",
            retrieved_at=fact.retrieved_at or retrieved_at,
            confidence=fact.confidence,
        ))
    return out


def _run_register_lookup(org: str, *, runs_dir: Path, no_web: bool) -> tuple[Profile | None, dict[str, Any], str]:
    """Run the full evidence pipeline for one number, live. Returns (profile, report, error)."""
    from pipeline import Settings, run  # noqa: PLC0415

    settings = Settings(
        profiles_dir=runs_dir / "profiles",
        runs_dir=runs_dir,
        fetch_web=not no_web,
        fetch_financials=True,
        use_llm=False,
        workers=1,
        quiet=True,
    )
    # Anchor identity offline from the public universe when the number is there.
    try:
        from registry import RegistryStore  # noqa: PLC0415
        from universe import seed_one_from_universe  # noqa: PLC0415

        store = RegistryStore(ROOT / "data" / "registry.sqlite")
        seed_one_from_universe(store, org)
        store.close()
    except Exception:
        pass

    reporter = run([org], settings=settings)
    path = settings.profiles_dir / f"{org}.json"
    if not path.exists():
        return None, reporter.to_dict() if hasattr(reporter, "to_dict") else {}, \
            "; ".join(reporter.envelopes_by_status and [] or []) or "no profile produced"
    return Profile.read(path), {"envelopes_by_status": reporter.envelopes_by_status}, ""


# ------------------------------------------------------------------- web gather


def _gather_web_evidence(
    results: list[SearchResult],
    *,
    company_name: str = "",
    read_top: int = 2,
    client: Any = None,
) -> list[Evidence]:
    """Turn ranked results into evidence: snippets always, page text for the top few.

    A snippet is a *lead* (``kind="web"``). The page a top-ranked result points to
    is fetched and quoted where the phrase that answers the question actually
    appears (``kind="web_rules"``) -- which is the honest upgrade from "a search
    engine said this" to "this page says this".
    """
    out: list[Evidence] = []
    stamp = utcnow_iso()
    for result in results:
        if result.snippet:
            out.append(Evidence(
                field="search_result",
                value=result.title or result.url,
                source_url=result.url,
                quote=result.snippet,
                kind="web",
                retrieved_at=stamp,
                confidence=max(0.30, 0.55 - result.rank * 0.03),
            ))

    # Read pages from the top and pick the *best describing* one, rather than
    # stopping at the first that yields prose. A company's rank-0 hit is often its
    # own marketing homepage ("bestill mobilabonnement i dag"), which is readable
    # but does not describe the company; the useful paragraph usually sits a rank
    # or two lower on a "about us" / group / encyclopedia page. So every candidate
    # is scored and the highest wins, with click-through cost capped by read_cap.
    best_knowledge: tuple[int, str, SearchResult] | None = None
    readable = 0
    read_cap = max(read_top, min(len(results), read_top + 5))
    for result in results[:read_cap]:
        if result.engine.startswith("wikipedia"):
            lang = result.engine.split(":", 1)[1] if ":" in result.engine else "no"
            text = wikipedia_summary(result.title, client=client, lang=lang)
        else:
            text = fetch_page_text(result.url, client=client)
        if not text:
            continue
        excerpt = _best_excerpt(text, company_name) if company_name else text[:400]
        if not excerpt:
            continue
        score = _content_score(excerpt, company_name)
        if score <= 0:
            # Navigation, a login wall or an error page -- never used as knowledge.
            continue
        readable += 1
        out.append(Evidence(
            field="page_excerpt",
            value=result.title or result.url,
            source_url=result.url,
            quote=excerpt,
            kind="web_rules",
            retrieved_at=stamp,
            confidence=0.72,
        ))
        # Prefer an excerpt that names the company (it is describing *it*), then
        # more text, then an earlier rank. The name check is what lifts an "about
        # Telenor" paragraph above a product page that never says "Telenor".
        named = 0
        if company_name:
            tokens = [t for t in re.split(r"\W+", company_name.casefold()) if len(t) >= 4]
            if tokens and any(t in excerpt.casefold() for t in tokens):
                named = 1
        score = (named, score)
        if best_knowledge is None or score > best_knowledge[0]:
            best_knowledge = (score, excerpt, result)
        # Enough to choose from: more pages cost requests without adding quality.
        if readable >= read_top + 1:
            break

    if best_knowledge is not None:
        _, excerpt, result = best_knowledge
        out.append(Evidence(
            field="knowledge",
            value=result.title or result.url,
            source_url=result.url,
            quote=excerpt,
            kind="web_rules",
            retrieved_at=stamp,
            confidence=0.70,
        ))
    return out


#: Phrases that mark navigation chrome, cookie banners or footers rather than the
#: page's actual content. A homepage's nav bar mentions the company name more
#: often than its prose does, so a naive "window around the name" excerpt lands on
#: "Jobs Cookies Privacy" instead of a sentence.
_CHROME_MARKERS = (
    "cookie", "personvern", "privacy", "javascript", "logg inn", "log in",
    "sign in", "subscribe", "newsletter", "nyhetsbrev", "menu", "skip to",
    "samfunnsansvar", "pressesenter", "åpenhetsloven", "informasjonskapsler",
    "view map", "follow us", "share this", "posted by",
)

#: Phrases that mark a WAF/error/interstitial page rather than content. These are
#: never evidence about anything, so they are rejected outright rather than
#: demoted -- an error page quoted as a company description would be a fabrication
#: of the worst kind (it looks sourced).
_ERROR_MARKERS = (
    "support id", "please consult with your administrator", "access denied",
    "403 forbidden", "404 not found", "page not found", "siden finnes ikke",
    "service unavailable", "bad gateway", "request blocked", "not acceptable",
    "error 1", "cloudflare", "incapsula", "akamai reference",
)


def _is_error_page(text: str) -> bool:
    """True when text is a WAF/error interstitial, never content."""
    low = text.casefold()
    return any(marker in low for marker in _ERROR_MARKERS)


def _is_chrome(text: str) -> bool:
    """True when a candidate excerpt looks like navigation/footer, not content.

    The test is deliberately about *structure*, not length: a short sentence can be
    perfectly good prose ("Selskapet har hovedkontor på Fornebu."), so a low word
    count alone is not chrome. What marks chrome is a chrome *marker* word, an
    error page, or a run of mostly capitalised fragments with no verb-like flow.
    """
    if not text:
        return True
    low = text.casefold()
    if _is_error_page(text):
        return True
    if any(marker in low for marker in _CHROME_MARKERS):
        return True
    words = text.split()
    if len(words) < 3:
        return True
    # A menu is mostly capitalised fragments ("Digital Sikkerhet Jobb i Telenor").
    caps = sum(1 for w in words if w[:1].isupper())
    if caps > len(words) * 0.6 and len(words) >= 6:
        return True
    return False


def _content_score(text: str, company_name: str = "") -> int:
    """How much an excerpt reads like prose *about the company*, 0 = unusable.

    This is the signal that decides which fetched page becomes the answer. A
    descriptive paragraph names the company and contains several real sentences,
    so it scores high; a footer, a login wall or a WAF error scores zero. Using a
    graded score rather than a single chrome/not-chrome flag is what stops the
    reader from over-correcting -- descending past a thin homepage into an error
    page and quoting *that*.
    """
    if not text or _is_error_page(text):
        return 0
    if _is_chrome(text):
        return 0
    words = text.split()
    if len(words) < 12:
        return 0
    # Count sentence-ending punctuation as a rough "is this prose" proxy.
    sentences = sum(text.count(p) for p in (". ", "! ", "? "))
    score = min(len(words), 120) // 6 + min(sentences, 8)
    if company_name:
        key = company_name.casefold()
        # Reward the company being named, but not so heavily that a menu wins.
        tokens = [t for t in re.split(r"\W+", key) if len(t) >= 4]
        if tokens and any(t in text.casefold() for t in tokens):
            score += 4
    return score


def _best_excerpt(text: str, needle: str = "", *, width: int = 480) -> str:
    """The best *readable* run of prose in ``text``.

    A fetched page is a mixture: navigation, a header block, the article itself,
    then a footer. The article is a *run of consecutive sentences that read as
    content*, while the chrome is sprinkled above and below it. So rather than
    windowing around a name (which lands on the nav, because the nav names the
    company too), this splits the page into sentences and returns the longest
    consecutive run that is not chrome. ``needle`` biases the choice toward the run
    that mentions the company when two runs are otherwise comparable, and is never
    required -- a page that describes the company without naming it in the chosen
    run still yields a usable excerpt.
    """
    if not text:
        return ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not sentences:
        return text[:width].strip()

    best: tuple[int, int, int] = (0, 0, -1)  # (score, length, start)
    start = 0
    while start < len(sentences):
        if _is_chrome(sentences[start]):
            start += 1
            continue
        end = start
        while end < len(sentences) and not _is_chrome(sentences[end]):
            end += 1
        run = sentences[start:end]
        blob = " ".join(run)
        length = len(blob)
        score = length
        if needle:
            tokens = [t for t in re.split(r"\W+", needle.casefold()) if len(t) >= 4]
            if tokens and any(t in blob.casefold() for t in tokens):
                score += 200
        if score > best[0]:
            best = (score, length, start)
        start = end + 1

    _, _, start = best
    if best[0] <= 0:
        # No run read as content: the page is chrome from top to bottom, or an
        # error page. Return nothing -- an empty excerpt is reported as a gap, and
        # never dressed up as an answer.
        return ""
    # Rebuild the winning run with its full extent (the loop recorded the start).
    end = start
    while end < len(sentences) and not _is_chrome(sentences[end]):
        end += 1
    body = " ".join(sentences[start:end]).strip()
    return _trim_quote(body, limit=width)



# ---------------------------------------------------------------- answer logic

#: question intent -> the fields (register profile) that answer it, in order.
_INTENT_FIELDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("who runs", "ceo", "chief exec", "managing director", "daglig leder", "board",
      "chair", "styre", "leder", "ledelse", "hvem"),
     ("ceo", "board_chair", "board_members", "auditor")),
    (("financial", "finance", "revenue", "turnover", "result", "profit", "equity",
      "assets", "debt", "accounts", "omsetning", "regnskap", "overskudd", "lønnsom"),
     ("revenue_latest", "net_result", "operating_result", "total_assets", "equity",
      "debt", "latest_accounts_year", "accounting_year")),
    (("where", "address", "located", "location", "office", "adresse", "hvor", "poststed"),
     ("address", "postcode", "city", "municipality", "location_count", "business_locations")),
    (("hiring", "jobs", "vacanc", "career", "stilling", "jobb", "ansatt", "rekrutter"),
     ("careers_page", "open_roles", "latest_job_title")),
    (("how many employee", "employees", "staff", "ansatte", "bemann"),
     ("employees",)),
    (("what does", "what do", "do they do", "about", "describe", "industry", "bransje",
      "hva gjør", "hva driver", "purpose", "formål"),
     ("description", "purpose", "industry", "nace_code")),
    (("when", "founded", "established", "registered", "stiftet", "etablert", "når"),
     ("founded", "registered")),
    (("contact", "email", "phone", "telefon", "epost", "e-post"),
     ("email", "phone", "website")),
    (("legal form", "org form", "organisasjonsform", "selskapsform"),
     ("legal_form", "sector")),
)

_LABELS: dict[str, str] = {
    "legal_name": "Name", "description": "What it does", "purpose": "Purpose",
    "industry": "Industry", "nace_code": "NACE code", "address": "Registered address",
    "city": "City", "municipality": "Municipality", "postcode": "Postcode",
    "ceo": "CEO / managing director", "board_chair": "Board chair",
    "board_members": "Board members", "auditor": "Auditor",
    "revenue_latest": "Revenue", "operating_result": "Operating result",
    "net_result": "Annual result", "total_assets": "Assets", "equity": "Equity",
    "debt": "Debt", "latest_accounts_year": "Latest accounts year",
    "accounting_year": "Reporting period", "reporting_period": "Reporting period",
    "employees": "Employees", "founded": "Founded", "registered": "Registered",
    "legal_form": "Legal form", "sector": "Sector", "status": "Status",
    "careers_page": "Careers page", "open_roles": "Open roles",
    "latest_job_title": "Latest job", "location_count": "Registered locations",
    "business_locations": "Workplaces", "website": "Website",
    "email": "Email", "phone": "Phone", "lei": "LEI",
    "search_result": "Search result", "page_excerpt": "Page",
    "knowledge": "From the web",
}

#: When a question is not about a specific register field (odd phrasing, a general
#: query), the answer falls back to the best page we read. These are the fields
#: that carry a general answer, most specific first.
_FALLBACK_WEB_FIELDS: tuple[str, ...] = ("knowledge", "page_excerpt")

_DEFAULT_FIELDS: tuple[str, ...] = (
    "legal_name", "description", "industry", "city", "municipality", "ceo",
    "revenue_latest", "website",
)

#: Short phrases that read as a follow-up about the company in session rather than
#: a new company to look up. Matched as a whole phrase or a leading word so
#: "contact info" and "their employees" stay on the current company.
_FOLLOWUP_HINTS = (
    "contact", "kontakt", "phone", "telefon", "email", "e-post", "epost", "website",
    "nettside", "their", "deres", "its ", "the company", "selskapet", "more", "mer",
    "finances", "financials", "økonomi", "employees", "ansatte", "details",
    "info", "informasjon", "address", "adresse", "ceo", "board", "leadership",
)


def _is_followup_phrase(text: str) -> bool:
    """True when a bare phrase is asking about the company already in session."""
    low = text.strip().casefold()
    if len(low.split()) > 4:
        return False
    return any(low == hint or low.startswith(hint) for hint in _FOLLOWUP_HINTS)


def _wanted_fields(question: str) -> tuple[str, ...]:
    q = question.casefold()
    for keywords, fields in _INTENT_FIELDS:
        if any(k in q for k in keywords):
            return fields
    return _DEFAULT_FIELDS


def _label(field_name: str) -> str:
    return _LABELS.get(field_name, field_name.replace("_", " ").title())


def _format_value(field_name: str, value: str) -> str:
    """Format a raw value for reading: money gets thousands separators, codes kept."""
    if field_name in {"revenue_latest", "net_result", "operating_result", "total_assets",
                      "equity", "debt", "employees"}:
        digits = re.sub(r"[^\d-]", "", value)
        if digits:
            try:
                return f"{int(digits):,}".replace(",", " ")
            except ValueError:
                return value
    return value


def _compose_answer(
    question: str,
    evidence: list[Evidence],
    *,
    kind: InputKind,
    target: str,
    searched: list[str],
) -> tuple[str, list[Evidence], bool]:
    """Select the evidence that answers ``question`` and write the reply.

    Returns ``(text, chosen_evidence, answered)``. Only evidence whose ``field``
    the question asked about is used; when none match, the fallback is the
    company brief -- never a general-prose answer.
    """
    # Highest tier wins per field, then highest confidence.
    best: dict[str, Evidence] = {}
    for item in evidence:
        current = best.get(item.field)
        if current is None or (_TIER.get(item.kind, 9), -item.confidence) < \
                (_TIER.get(current.kind, 9), -current.confidence):
            best[item.field] = item

    wanted = _wanted_fields(question)
    chosen: list[Evidence] = []
    seen_fields: set[str] = set()
    for name in wanted:
        item = best.get(name)
        if item is not None and name not in seen_fields:
            chosen.append(item)
            seen_fields.add(name)

    if not chosen:
        # Nothing matched the specific intent; fall back to the brief fields.
        for name in _DEFAULT_FIELDS:
            item = best.get(name)
            if item is not None:
                chosen.append(item)
                seen_fields.add(name)

    # A general question with no register field to answer it: use the page we read.
    # The quote itself becomes the answer, attributed to the page -- this is the
    # "answer from the open web, with a source" case.
    if not chosen:
        for name in _FALLBACK_WEB_FIELDS:
            item = best.get(name)
            if item is not None and item.quote:
                chosen.append(item)
                break

    if not chosen:
        # Last resort: any register evidence at all (name at minimum).
        for item in evidence:
            if item.field == "legal_name":
                chosen.append(item)
                break

    if not chosen:
        searched_line = ", ".join(searched) if searched else "no source returned data"
        return (
            f"I could not find a sourced answer for \"{question}\". "
            f"What was searched: {searched_line}. "
            "Rather than guess, I am reporting the gap.",
            [], False,
        )

    # When the answer is a web page quote, present it as prose with the source.
    if chosen[0].field in _FALLBACK_WEB_FIELDS:
        quote = _trim_quote(chosen[0].quote)
        prefix = f"{target}: " if target and kind == "orgnr" else ""
        return f"{prefix}{quote}", chosen, True

    intro = ""
    if target:
        who = target
        if kind == "orgnr":
            intro = f"{who}: "
        else:
            intro = f"About {who}: "
    parts = [f"{_label(item.field)} — {_format_value(item.field, item.value)}" for item in chosen]
    text = intro + "; ".join(parts) + "."
    return text, chosen, True


def _trim_quote(text: str, *, limit: int = 480) -> str:
    """A quote cut to whole sentences near ``limit``, for a readable answer."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    # Prefer to end on the last sentence boundary before the cut.
    for stop in (". ", "! ", "? "):
        idx = clipped.rfind(stop)
        if idx > limit // 2:
            return clipped[:idx + 1].strip()
    return clipped.rstrip() + "…"


# ------------------------------------------------------------------ the session


class LiveSession:
    """One conversation. Holds the resolved target so follow-ups stay in context."""

    def __init__(
        self,
        session_id: str = "default",
        *,
        client: Any = None,
        runs_dir: Path | None = None,
        no_web: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.session_id = session_id or "default"
        self.client = client
        self.runs_dir = runs_dir or (ROOT / "runs" / "live" / self.session_id)
        self.no_web = no_web
        self.progress = progress
        #: The company/target established by the first turn, reused by follow-ups.
        self.target_org: str = ""
        self.target_name: str = ""
        self.target_domain: str = ""
        self.turns: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers

    def _say(self, message: str) -> None:
        if self.progress is not None:
            try:
                self.progress(message)
            except Exception:
                pass

    def _client(self) -> Any:
        if self.client is None:
            from net import HttpClient  # noqa: PLC0415

            self.client = HttpClient()
        return self.client

    def _register_evidence(self, org: str) -> tuple[list[Evidence], Profile | None]:
        profile, report, error = _run_register_lookup(
            org, runs_dir=self.runs_dir, no_web=self.no_web or True
        )
        if profile is None:
            return [], None
        return _facts_to_evidence(profile, utcnow_iso()), profile

    def _resolve_in_register(self, name: str) -> str:
        """The organisation number the register itself gives for ``name``, or ''.

        A last resort before answering a named company from web prose: the register
        is the authority on who a company *is*, and a name is usually enough to
        find it there even when no page on the open web prints its number.

        The register's name search is fuzzy -- "Norsk Hydro" returns
        NORSK HYDROGENBILFORENING before NORSK HYDRO ASA -- so a hit is only
        accepted when :func:`_names_agree` confirms it, preferring an exact
        case-folded match over a token-subset match. When nothing agrees, the
        answer stays a web answer rather than becoming a profile of the wrong
        company.
        """
        try:
            from registry import BrregClient  # noqa: PLC0415
        except Exception:
            return ""
        try:
            candidates = BrregClient(client=self._client()).search_by_name(name, size=10)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is a gap, not a crash
            self._say(f"register name search failed: {type(exc).__name__}")
            return ""

        exact = ""
        agreeing: list[tuple[int, int, str]] = []
        for item in candidates:
            number = str(item.get("organisasjonsnummer") or "")
            found = str(item.get("navn") or "")
            if not number or not found:
                continue
            if found.strip().casefold() == name.strip().casefold():
                exact = exact or number
                continue
            # Strict direction only: the candidate must contain every distinctive
            # token of the name searched.
            if not _name_resolves_to(name, found):
                continue
            form = str((item.get("organisasjonsform") or {}).get("kode") or "")
            # Rank by legal form first, then by how close the name is to exactly
            # the name asked for. A bare company name means the *company*: among
            # the "Telenor" hits, TELENOR ASA is the telecoms operator, while
            # NITO TELENOR is a union, TELENOR PAKISTAN a foreign branch and
            # TELENOR PENSJONSKASSE a pension fund. Length alone picked TELENOR
            # A/S (a foreign entity); legal form picks the ASA.
            agreeing.append((_FORM_RANK.get(form, 50), len(found), number))
        chosen = exact or (min(agreeing)[2] if agreeing else "")
        if chosen:
            self._say(f"register name search {name!r} -> {chosen}")
        return chosen

    def _resolve_subject(self, subject: str) -> list[Evidence]:
        """Resolve a company named inside a question, and adopt it as the target.

        Searches for the name, keeps only results that actually concern it, and
        resolves the authoritative register profile. The session target is set, so
        subsequent follow-ups stay on this company. Returns the evidence gathered
        (possibly empty, honestly).

        The register is consulted **directly by name**, not only when a page
        happens to carry an organisation number. A company's own pages almost
        never print their number, so "who runs Equinor?" used to fall through to a
        marketing homepage and answer with a slogan, while the register holding
        "Anders Opedal" was one query away. A page-harvested number still wins when
        it exists (it is the strongest signal), and it is still name-checked.
        """
        out = general_search(subject, client=self._client(), company_name=subject)
        results = _relevant_to(subject, out.results) if self.target_name else out.results
        # Drop directory-only hits when a company page exists; the register is
        # fetched separately below anyway.
        evidence = _gather_web_evidence(results, company_name=subject, client=self._client())

        harvested = _harvest_orgnr(out.results, expected_name=subject)
        if not harvested:
            # No page printed the number. Ask the register by name instead of
            # settling for web prose about the company.
            harvested = self._resolve_in_register(subject)
        if harvested:
            self._say(f"register lookup {harvested} (subject)")
            reg_evidence, profile = self._register_evidence(harvested)
            # Only a register profile whose name agrees with the subject may be
            # adopted -- otherwise a number found on an unrelated page would make
            # the question about the wrong company.
            if profile is not None and _names_agree(subject, profile.legal_name or ""):
                self.target_org = harvested
                self.target_name = profile.legal_name or subject
                evidence.extend(reg_evidence)
            else:
                # The web results that do concern the subject are still evidence.
                if results:
                    self.target_name = subject
        elif results:
            self.target_name = subject
        return evidence

    # -------------------------------------------------------------------- ask

    def ask(self, query: str) -> ResearchAnswer:
        """Answer ``query``, resolving a company first if one is not yet set."""
        started = time.monotonic()
        query = (query or "").strip()

        # An empty query has nothing to answer, and must not be treated as a
        # follow-up. classify_input("") returns "question", which would then ride
        # the follow-up path onto whatever company happens to be in session --
        # so a blank box would confidently report on the *previous* company.
        # That is a wrong answer dressed as a right one, so it is refused here.
        if not query:
            answer = ResearchAnswer(question="", kind="empty")
            answer.notes.append("the query was empty, so nothing was searched")
            answer.seconds = round(time.monotonic() - started, 2)
            return answer

        kind = classify_input(query)

        # A follow-up about a company already in context stays on that company.
        # A short bare phrase ("contact info", "their finances") that is not
        # itself a number, a domain, or a new company name is a question about
        # the company in session -- not a fresh search that would drift.
        followup = bool(self.target_org or self.target_name) and kind in {"question", "name"}
        if followup and kind == "name":
            followup = _is_followup_phrase(query)
            if followup:
                kind = "question"

        answer = ResearchAnswer(question=query, kind=kind)
        if followup:
            answer.notes.append(
                f"follow-up on the company already in this session: "
                f"{self.target_name or self.target_org}"
            )

        # A question with no company yet ("who runs Equinor?") names its subject
        # inside the sentence. Resolve that subject first, so the question is
        # answered *about* it -- otherwise the search engine answers whatever it
        # feels is closest, which is how a question about one company returns a
        # page about another. When a company is already in session, the earlier
        # identifier wins and this is skipped.
        evidence: list[Evidence] = []
        if kind == "question" and not (self.target_org or self.target_name):
            subject = _subject_of(query)
            if subject:
                self._say(f"resolving subject {subject!r}")
                resolved = self._resolve_subject(subject)
                if resolved:
                    evidence.extend(resolved)

        searched: list[str] = []

        # -- A number: run the full register pipeline live.
        if kind == "orgnr":
            org = clean(query) or ""
            self._say(f"register lookup {org}")
            searched.append(f"brreg:{org}")
            reg_evidence, profile = self._register_evidence(org)
            evidence.extend(reg_evidence)
            if profile is not None:
                self.target_org = org
                self.target_name = profile.legal_name or self.target_name
                answer.target = profile.legal_name or org
                answer.org_number = org
                answer.profile_path = str(self.runs_dir / "profiles" / f"{org}.json")
                # A name/no question about a known company does not need the web.
                if not self.no_web and not followup:
                    out = search_web(profile.legal_name or query, client=self._client(),
                                     company_name=profile.legal_name)
                    searched += out.engines_tried
                    evidence.extend(_gather_web_evidence(
                        out.results, company_name=profile.legal_name, client=self._client()))
                    answer.engine = out.engine
            else:
                answer.notes.append("no register profile was produced for this number")

        # -- A name or domain: search the open web, read the top pages.
        elif kind in {"name", "domain"}:
            self._say(f"web search {query!r}")
            wanted_name = self.target_name or query
            out = general_search(
                query, client=self._client(),
                company_name=wanted_name)
            searched += out.engines_tried
            answer.engine = out.engine
            # Keep only results that actually concern the name that was searched.
            # A search engine answers "VasaTest AS" with VETASSESS in Australia,
            # Merriam-Webster's entry for "vastest", and Melbourne asbestos
            # testers; pouring those straight into the pool is how one company's
            # question gets answered with another page entirely. The same filter
            # already guards the harvest and the question path -- it was simply
            # missing here, which is the difference between "found the company"
            # and "found a page that mentioned a similar word".
            relevant = _relevant_to(query, out.results)
            dropped = len(out.results) - len(relevant)
            if dropped:
                answer.notes.append(
                    f"{dropped} of {len(out.results)} results did not mention "
                    f"{query!r} and were not used")
            evidence.extend(_gather_web_evidence(
                relevant, company_name=wanted_name, client=self._client()))

            # A bare name rarely states the organisation number on the top page,
            # but a second query aimed at it usually surfaces a register or
            # directory page that does. Only worth it when the first pass found
            # no number -- otherwise the extra request is waste.
            harvested = _harvest_orgnr(out.results, expected_name=query)
            if not harvested and kind == "name":
                self._say(f"web search {query!r} (number)")
                out2 = general_search(
                    f"{query} organisasjonsnummer", client=self._client(),
                    company_name=query)
                searched += out2.engines_tried
                relevant2 = _relevant_to(query, out2.results)
                dropped2 = len(out2.results) - len(relevant2)
                if dropped2:
                    answer.notes.append(
                        f"{dropped2} of {len(out2.results)} results did not mention "
                        f"{query!r} and were not used")
                evidence.extend(_gather_web_evidence(
                    relevant2, company_name=query, client=self._client()))
                harvested = _harvest_orgnr(out2.results, expected_name=query)
                if not out.results and out2.results:
                    out = out2
                    answer.engine = out2.engine

            # Still no number? A company's own pages rarely print their
            # organisation number, so "who runs Equinor?" would otherwise be
            # answered from a marketing homepage -- while the register that
            # actually answers it sits one query away. Ask the register by name.
            # The hit is *not* taken on faith: "Norsk Hydro" returns
            # NORSK HYDROGENBILFORENING first, so only a candidate whose
            # registered name genuinely agrees is adopted.
            if not harvested and kind == "name":
                by_name = self._resolve_in_register(query)
                if by_name:
                    harvested = by_name
                    searched.append(f"brreg:name:{query}")

            if relevant:
                named = _identifying_result(query, relevant)
                if named is not None:
                    answer.target = self.target_name or named.title or query
                else:
                    # Something matched the name, but nothing that could *be* the
                    # company (a video channel, a dictionary entry, a social page).
                    # Keep the name we were asked about rather than renaming the
                    # subject after whatever mentioned the word.
                    answer.target = self.target_name or query
            elif out.results:
                # Results came back, but none of them concerned the searched name.
                # The name is still the identity we were asked about; adopting an
                # unrelated page's title as the company would be the wrong-entity
                # error in its purest form. So the identity stays the query and
                # the mismatch is reported rather than papered over.
                answer.target = self.target_name or query
                answer.notes.append(
                    f"no result mentioned {query!r}; the pages returned were not "
                    f"attributed to it")
            # A harvested number is *provisional*: it is only adopted once the
            # register confirms the name it belongs to actually matches what was
            # searched. A number lifted from an unrelated listing is discarded,
            # which is the difference between "found the company" and "found a
            # company that happens to be on the same page".
            if harvested:
                want_name = query if kind == "name" else (self.target_name or query)
                # Cheap pre-check against the local mirror first: if the mirror
                # already knows this number and its name does not match, there is
                # no point fetching the full profile -- we already know it is the
                # wrong company. Only when the mirror is silent (a company not in
                # the seeded universe) do we pay for the live lookup and confirm
                # against the fetched name.
                prov_name = _mirror_name(harvested)
                if prov_name and not _names_agree(want_name, prov_name):
                    self._say(
                        f"ignored register number {harvested} ({prov_name}) — "
                        f"not {want_name!r}")
                    answer.notes.append(
                        f"a register number ({harvested}) appeared in the results "
                        f"but it belongs to {prov_name!r}, not {want_name!r}; it was "
                        f"not attributed to this query")
                else:
                    self._say(f"register lookup {harvested} (from web result)")
                    reg_evidence, profile = self._register_evidence(harvested)
                    searched.append(f"brreg:{harvested}")
                    if profile is not None and _names_agree(
                            want_name, profile.legal_name or ""):
                        answer.org_number = harvested
                        self.target_org = harvested
                        self.target_name = profile.legal_name or self.target_name
                        answer.target = profile.legal_name or answer.target
                        answer.profile_path = str(
                            self.runs_dir / "profiles" / f"{harvested}.json")
                        evidence.extend(reg_evidence)
                    else:
                        # The single most important guard here: never let a register
                        # profile of a *different* company stand in for this search.
                        answer.notes.append(
                            f"a register number ({harvested}) appeared in the results "
                            f"but its registered name did not match "
                            f"{want_name!r}; it was not attributed to this query")
            if out.error and not out.results:
                answer.notes.append(f"search: {out.error}")

        # -- A question: answer from the in-session company first, then the web.
        else:
            # If a company is in context, its register facts join the pool and may
            # answer the question outright -- no search needed, which is both
            # cheaper and more authoritative than a snippet.
            if self.target_org:
                reg_evidence, profile = self._register_evidence(self.target_org)
                evidence.extend(reg_evidence)
                if profile is not None:
                    answer.target = self.target_name or profile.legal_name
                    answer.org_number = self.target_org
                    answer.profile_path = str(
                        self.runs_dir / "profiles" / f"{self.target_org}.json")

            _, already, _ = _compose_answer(
                query, evidence, kind=kind, target=answer.target, searched=[])
            needs_web = not already or not evidence
            if needs_web:
                search_term = _strip_question(query)
                if self.target_name and self.target_name.casefold() not in search_term.casefold():
                    search_term = f"{self.target_name} {search_term}"
                self._say(f"web search {search_term!r}")
                out = general_search(search_term, client=self._client(),
                                     company_name=self.target_name,
                                     prefer_wikipedia=_is_definitional(query))
                searched += out.engines_tried
                answer.engine = out.engine
                web_results = out.results
                # When a company is in context, a result that does not mention it
                # is not an answer about it -- a search engine will happily return
                # something tangential, and attaching that to the company would be
                # exactly the wrong-entity error this project refuses to make.
                if self.target_name:
                    web_results = _relevant_to(self.target_name, web_results)
                evidence.extend(_gather_web_evidence(
                    web_results, company_name=self.target_name, client=self._client()))
                if out.error and not out.results:
                    answer.notes.append(f"search: {out.error}")
                if self.target_name and out.results and not web_results:
                    answer.notes.append(
                        "search returned results that did not mention the company; "
                        "they were not attributed to it")
            else:
                searched.append(f"register:{self.target_org}")

        # -- Compose.
        text, chosen, answered = _compose_answer(
            query, evidence, kind=kind, target=answer.target, searched=searched)
        answer.answer = text
        answer.answered = answered
        answer.evidence = [item.to_dict() for item in chosen]
        answer.sources = _sources_from(chosen)
        answer.searched = searched
        answer.seconds = time.monotonic() - started

        self.turns.append({
            "query": query, "kind": kind, "answered": answered,
            "target": answer.target, "at": utcnow_iso(),
        })
        return answer


def _names_agree(searched: str, found: str) -> bool:
    """True when a register name plausibly *is* the name that was searched for.

    A harvested organisation number must be confirmed before it is adopted as
    the subject of an answer: a number lifted from the wrong page would otherwise
    turn "Equinor ASA" into a profile of some unrelated company. The test is
    deliberately conservative -- every distinctive token of the searched name must
    appear in the register name (so "Equinor" matches "EQUINOR ASA", and "Telenor"
    matches "TELENOR ASA"), while a generic-only search name cannot confirm
    anything and is treated as a non-match.
    """
    def tokens(text: str) -> set[str]:
        raw = re.split(r"\W+", (text or "").casefold())
        return {t for t in raw if len(t) >= 4 and t not in {
            "norsk", "norge", "noreg", "norway", "group", "holding", "invest",
            "selskap", "company", "gruppen", "nordic", "nordisk", "as", "asa",
            "ans", "enk", "the", "and",
        }}

    want = tokens(searched)
    have = tokens(found)
    if not want:
        # A name of only generic words cannot be confirmed by name alone.
        return False
    return want.issubset(have) or have.issubset(want)


#: Generic tokens that carry no identity, shared by the name checks below.
_GENERIC_NAME_TOKENS = {
    "norsk", "norge", "noreg", "norway", "group", "holding", "invest",
    "selskap", "company", "gruppen", "nordic", "nordisk", "as", "asa",
    "ans", "enk", "the", "and", "nito", "tekna", "forbund", "forening",
}


def _name_tokens(text: str) -> set[str]:
    """The distinctive tokens of a company name, generic words removed."""
    raw = re.split(r"\W+", (text or "").casefold())
    return {t for t in raw if len(t) >= 4 and t not in _GENERIC_NAME_TOKENS}


#: Legal-form ranks for disambiguating a bare company name, lower is a better
#: match for "the company itself". The register's name search is fuzzy, and for a
#: name like "Telenor" it returns unions, clubs, foreign branches and pension
#: funds alongside the operating company. Those forms are real entities, but none
#: of them is what a bare name means -- so they rank below the ordinary company
#: forms, and a genuinely unusual query still resolves through `exact` or through
#: the web path rather than being forced onto a company it is not.
_FORM_RANK: dict[str, int] = {
    "ASA": 0,   # public limited company
    "AS": 1,    # private limited company
    "SA": 2,    # co-operative
    "ANS": 3,   # general partnership
    "DA": 4,    # partnership with shared liability
    "ENK": 5,   # sole trader
    "IKS": 6,   # inter-municipal company
    "KF": 7, "FKF": 8, "BA": 9, "STI": 10, "PBL": 11,
    "PK": 40, "FLI": 41, "NUI": 42, "UTLA": 43, "NUF": 44,
    "ORGL": 45, "STAT": 46, "FYLK": 47, "KOMM": 48, "ANNA": 49,
}


def _name_resolves_to(searched: str, found: str) -> bool:
    """True when ``found`` is a *plausible best match* for ``searched``.

    Stricter than :func:`_names_agree`, which is symmetric because it confirms a
    number already in hand. Here we are *choosing* among fuzzy register hits, so
    the direction matters: every distinctive token of what was searched must
    appear in the candidate. ``have.issubset(want)`` is deliberately not allowed,
    because it lets "Telenor" select "NITO TELENOR" -- a trade union, not the
    telecoms company -- and that is the wrong-entity error in a new costume.
    """
    want = _name_tokens(searched)
    have = _name_tokens(found)
    if not want:
        return False
    return want.issubset(have)


def _mirror_name(org: str) -> str:
    """The name the local registry mirror holds for ``org``, or '' if unknown.

    A cheap, offline read used to reject a wrong harvested number *before* paying
    for a live register fetch. A miss (the company is simply not seeded) returns
    '', which signals "unknown, fall through to the live lookup" -- never a
    rejection on absence of information.
    """
    try:
        from registry import RegistryStore  # noqa: PLC0415

        store = RegistryStore(ROOT / "data" / "registry.sqlite")
        try:
            entity = store.lookup(org)
        finally:
            store.close()
        if not entity:
            return ""
        return entity.get("navn") or entity.get("name") or ""
    except Exception:
        return ""


#: Leading words that add nothing to a search query. Stripped from a question so
#: "what is Norsk Hydro" searches for "Norsk Hydro", not the literal sentence --
#: which is what a search engine would otherwise match word for word.
_QUESTION_STOPWORDS = (
    "what is", "what are", "what was", "who is", "who are", "who was", "tell me about",
    "give me", "find", "search for", "show me", "explain", "describe",
    "hva er", "hvem er", "fortell meg om", "finn", "vis meg",
)


def _strip_question(text: str) -> str:
    """Turn a question into a search term by dropping leading interrogatives."""
    cleaned = text.strip().rstrip("?").strip()
    low = cleaned.casefold()
    for prefix in _QUESTION_STOPWORDS:
        if low.startswith(prefix):
            return cleaned[len(prefix):].strip(" ?.,:") or cleaned
    return cleaned


#: Words in a question that are never the company name being asked about.
_SUBJECT_STOPWORDS = {
    "who", "what", "when", "where", "why", "how", "which", "is", "are", "does", "do",
    "did", "the", "their", "its", "a", "an", "of", "for", "and", "or", "runs", "run",
    "owns", "own", "make", "makes", "revenue", "employees", "many", "much", "contact",
    "hva", "hvem", "hvor", "når", "hvorfor", "hvordan", "er", "har", "kan", "deres",
    # Role and section words: they name the *question*, not the subject company.
    "ceo", "cfo", "cto", "chair", "board", "director", "manager", "leder", "ceo?",
    "daglig", "styre", "styret", "eier", "owner", "founder", "grunnlegger", "sjef",
    "revenue", "omsetning", "result", "profit", "overskudd", "accounts", "regnskap",
}


def _subject_of(question: str) -> str:
    """The likely company name inside a question, or '' when none is apparent.

    "who runs Equinor?" -> "Equinor"; "what does Norsk Hydro ASA do?" -> "Norsk
    Hydro ASA". The heuristic is deliberately conservative: it keeps runs of
    capitalised or otherwise non-stopword tokens and returns them joined, so a
    question with no evident name ("how does this work?") yields '' and the
    caller falls back to a plain web search.
    """
    cleaned = _strip_question(question)
    tokens = [t for t in re.split(r"\s+", cleaned) if t]
    kept: list[str] = []
    for token in tokens:
        bare = re.sub(r"[^\w]", "", token)
        if not bare:
            continue
        if bare.casefold() in _SUBJECT_STOPWORDS:
            if kept:
                break
            continue
        # Capitalised mid-sentence or an all-caps/legal-form token: part of a name.
        if token[0].isupper() or bare.casefold() in {"as", "asa", "ans", "da", "sa"}:
            kept.append(token)
        elif kept:
            break
    subject = " ".join(kept).strip(" ?.,:;")
    return subject if len(subject) >= 3 else ""


def _bare_host(text: str) -> str:
    """The registrable host of a domain-shaped input, else ''.

    ``www.dnb.no`` and ``dnb.no`` are the same site, and a result served from that
    host concerns that site by construction. Tokenising the literal string instead
    loses this: ``"www.dnb.no"`` becomes tokens ``["www", "dnb"]``, and ``www``
    appears in no page text, so every genuine result was being discarded as
    irrelevant -- the domain path returned a gap for a site that had answered.
    """
    raw = (text or "").strip().casefold()
    if not raw or " " in raw:
        return ""
    if raw.startswith(("http://", "https://")):
        raw = raw.split("://", 1)[1]
    host = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split(":", 1)[0]
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) < 2 or not parts[-1].isalpha() or len(parts[-1]) < 2:
        return ""
    return host


def _relevant_to(company: str, results: list[SearchResult]) -> list[SearchResult]:
    # A domain query is matched against the host of each result: a page served
    # from dnb.no is about dnb.no, whatever words its title happens to use.
    host = _bare_host(company)
    if host:
        out: list[SearchResult] = []
        for result in results:
            target_host = _bare_host(result.url) or ""
            if not target_host:
                continue
            if target_host == host \
                    or target_host.endswith("." + host) \
                    or host.endswith("." + target_host):
                out.append(result)
        if out:
            return out
        # Nothing came from that host; fall through to the text match below, which
        # still catches a page that discusses the site without being served by it.
    tokens = [t for t in re.split(r"\W+", company.casefold()) if len(t) >= 4]
    tokens = [t for t in tokens if t not in {"norsk", "norge", "noreg", "norway", "group",
                                              "holding", "invest", "selskap", "company",
                                              "gruppen", "nordic", "nordisk", "http",
                                              "https", "www"}]
    if not tokens:
        # A name made only of generic words (":-) INVEST AS", "Nordic Holding AS")
        # carries no distinctive token, so no web result can be attributed to it
        # on the strength of the name alone. Returning nothing is the honest
        # outcome: a gap, never a tangential page dressed up as this company.
        return []
    out = []
    for result in results:
        blob = f"{result.title} {result.snippet} {result.url}".casefold()
        if any(token in blob for token in tokens):
            out.append(result)
    return out


def _identifying_result(company: str, results: list[SearchResult]) -> SearchResult | None:
    """The result that may stand as ``company``'s *identity*, or ``None``.

    Stricter than :func:`_relevant_to` on purpose. Relevance decides what is worth
    *reading*; identity decides what the answer is *about*, and the two are not the
    same bar. A YouTube channel called "VasaTest" is relevant to the string
    "VasaTest AS" -- it shares the distinctive token -- but it is not the company,
    and letting its title become ``target`` would rename a Norwegian business after
    a video channel.

    So a result may name the company only when every distinctive token of the name
    appears in it **and** the result is not a generic third-party surface (a social
    profile, a dictionary entry, a video channel). When nothing clears that bar the
    caller keeps the name it was given, which is always safe.
    """
    if not results:
        return None
    tokens = [t for t in re.split(r"\W+", company.casefold()) if len(t) >= 4]
    tokens = [t for t in tokens if t not in {
        "norsk", "norge", "noreg", "norway", "group", "holding", "invest",
        "selskap", "company", "gruppen", "nordic", "nordisk", "as", "asa",
        "ans", "enk", "the", "and",
    }]
    if not tokens:
        return None
    for result in results:
        blob = f"{result.title} {result.snippet} {result.url}".casefold()
        if not all(t in blob for t in tokens):
            continue
        if _host_class(result.url) in {"social", "reference"}:
            continue
        return result
    return None


#: Host classes that may inform an answer but must never *be* the company.
_SOCIAL_HOSTS = ("youtube.com", "facebook.com", "linkedin.com", "instagram.com",
                 "twitter.com", "x.com", "tiktok.com", "pinterest.com", "reddit.com")
_REFERENCE_HOSTS = ("merriam-webster.com", "dictionary.com", "wikipedia.org",
                    "wiktionary.org", "thefreedictionary.com", "cambridge.org",
                    "collinsdictionary.com", "urbandictionary.com")


def _host_class(url: str) -> str:
    """Classify a URL's host: ``social``, ``reference`` or ``general``."""
    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        host = urlparse(url).netloc.casefold().removeprefix("www.")
    except Exception:
        return "general"
    if any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS):
        return "social"
    if any(host == h or host.endswith("." + h) for h in _REFERENCE_HOSTS):
        return "reference"
    return "general"


def _is_definitional(text: str) -> bool:
    """True for "what is X?" / "hvem er X?" -- questions an encyclopedia answers best."""
    low = text.strip().casefold().rstrip("?").strip()
    return any(low.startswith(p) for p in
               ("what is", "what are", "who is", "who are", "tell me about",
                "hva er", "hvem er", "fortell meg om"))


def _harvest_orgnr(results: list[SearchResult], *, expected_name: str = "") -> str:
    """The first checksum-valid organisation number that plausibly belongs to the target.

    Harvesting *any* nine-digit checksum-valid number off a result page is how a
    search for one company silently returns another: directory pages (Proff,
    orgnummer.no, 1881) list "related companies", and a valid number from such a
    block would otherwise be adopted as the answer's subject.

    So when ``expected_name`` is given, a number is only accepted from a result
    that actually mentions the target name -- the same relevance rule used
    elsewhere -- and, additionally, the number must appear in a result whose own
    text carries a distinctive name token. A number found only on an unrelated
    listing is discarded, because attributing it would be the wrong-entity error
    this project exists to prevent.
    """
    from validate import harvest_from_text  # noqa: PLC0415

    relevant = results
    if expected_name:
        relevant = _relevant_to(expected_name, results)
    for result in relevant:
        blob = f"{result.title} {result.snippet} {result.url}"
        found = harvest_from_text(blob)
        if found:
            return sorted(found)[0]
    return ""


def _sources_from(evidence: list[Evidence]) -> list[dict[str, Any]]:
    """Deduplicate evidence into a numbered source list for the reply."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        url = item.source_url or ""
        if url and url in seen:
            continue
        seen.add(url)
        out.append({
            "title": _label(item.field),
            "field": item.field,
            "url": url,
            "quote": item.quote[:240],
            "kind": item.kind,
            "retrieved_at": item.retrieved_at,
        })
        if len(out) >= 12:
            break
    return out


# --------------------------------------------------------------- session store


class SessionStore:
    """In-memory registry of live sessions, so a follow-up finds its context.

    Deliberately tiny: a dict keyed by session id, each value a
    :class:`LiveSession`. A server process holds one store for its lifetime; the
    CLI creates a throwaway one for a single non-interactive question. Nothing is
    persisted, because a live session is about *this* conversation, and a stale
    target from yesterday is exactly the kind of silent wrong-company answer the
    project exists to avoid.
    """

    def __init__(self, *, runs_root: Path | None = None, no_web: bool = False) -> None:
        self.runs_root = runs_root or (ROOT / "runs" / "live")
        self.no_web = no_web
        self._sessions: dict[str, LiveSession] = {}

    def session(self, session_id: str = "default") -> LiveSession:
        sid = session_id or "default"
        if sid not in self._sessions:
            self._sessions[sid] = LiveSession(
                sid, runs_dir=self.runs_root / sid, no_web=self.no_web)
        return self._sessions[sid]

    def reset(self, session_id: str = "default") -> None:
        self._sessions.pop(session_id or "default", None)

    def ids(self) -> list[str]:
        return sorted(self._sessions)
