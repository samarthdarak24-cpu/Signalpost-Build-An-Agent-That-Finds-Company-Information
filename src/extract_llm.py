"""LLM gap filling, and only for the gaps.

Two model roles, chosen for cost asymmetry:

* **Gap filling** — ``claude-haiku-4-5-20251001``. Cheap, structured output,
  called once per company at most and only when the rules left holes.
* **Conflict resolution** — ``claude-sonnet-5-5``. Called once, rarely, and only
  when two sources disagree on a value the pipeline already holds.

The contract is deliberately severe. The model must return, for every field, a
``quote`` that appears verbatim in the page text we sent. A field whose quote
cannot be located in the source is discarded before it ever reaches the profile,
which means a hallucination cannot become a fact — it can only become an
abstention.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from net import HttpClient
from profiles import whitespace_contains

__all__ = [
    "GapCandidate",
    "GapFillResult",
    "LlmClient",
    "GAP_FIELDS",
    "build_gap_prompt",
    "resolve_conflict",
    "MODEL_GAP",
    "MODEL_VERIFY",
]

#: Model strings are recorded in .env.example and echoed into every run report,
#: because the brief asks for the exact identifiers used.
MODEL_GAP = os.environ.get("SIGNALPOST_MODEL_GAP", "claude-haiku-4-5-20251001")
MODEL_VERIFY = os.environ.get("SIGNALPOST_MODEL_VERIFY", "claude-sonnet-5-5")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

#: Fields the model is allowed to attempt. Anything not on this list is never
#: asked for, which is how the "don't collect personal data" rule is enforced:
#: there is no field for a named employee and no field for a mobile number.
GAP_FIELDS: tuple[str, ...] = (
    "description",
    "email",
    "phone",
    "address",
    "postcode",
    "city",
    "founding_year",
    "employees_range",
)

#: Per-million-token prices, used to fill the run report's cost table. Update
#: when providers change pricing; the pipeline reads from here, not literals.
PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-5-5": {"input": 3.00, "output": 15.00},
    "brave-search-query": {"input": 0.005, "output": 0.0},
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "properties": {
                name: {
                    "type": ["object", "null"],
                    "properties": {
                        "value": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["value", "quote"],
                    "additionalProperties": False,
                }
                for name in GAP_FIELDS
            },
            "additionalProperties": False,
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class GapCandidate:
    """One field the model returned, still unverified."""

    field: str
    value: str
    quote: str
    verified: bool = False
    reject_reason: str = ""


@dataclass(slots=True)
class GapFillResult:
    """Outcome of one gap-filling call."""

    url: str
    candidates: list[GapCandidate] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False
    error: str = ""

    @property
    def accepted(self) -> list[GapCandidate]:
        return [c for c in self.candidates if c.verified]

    @property
    def rejected(self) -> list[GapCandidate]:
        return [c for c in self.candidates if not c.verified]

    @property
    def cost_usd(self) -> float:
        return _cost(MODEL_GAP, self.input_tokens, self.output_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "accepted": [c.field for c in self.accepted],
            "rejected": [
                {"field": c.field, "reason": c.reject_reason} for c in self.rejected
            ],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit": self.cached,
            "cost_usd": round(self.cost_usd, 6),
            "error": self.error,
        }


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICING.get(model)
    if not price:
        return 0.0
    return (input_tokens / 1_000_000) * price["input"] + (
        output_tokens / 1_000_000
    ) * price["output"]


SYSTEM_PROMPT = """You extract company contact details from a Norwegian web page.

You are the last resort. Rules and structured data have already failed, so the \
only thing that matters is that whatever you return is literally on the page.

Hard rules:
1. Return a value only if it is written on the page. If it is not there, return null.
2. Every value must come with a `quote` copied character-for-character from the \
page text. Do not paraphrase, do not translate, do not tidy spelling.
3. Never infer, never complete a partial value, never use knowledge of the company \
from outside this page.
4. Do not return personal names, personal email addresses or mobile numbers \
(beginning 4 or 9). Company switchboards and role mailboxes only.
5. Norwegian postal addresses are written "<street> <number>, <4-digit postcode> <CITY>".
"""


def build_gap_prompt(
    page_text: str,
    *,
    wanted: Iterable[str],
    company_name: str,
    org_display: str,
) -> str:
    """Compose the user turn: the page, the gaps, and the schema restated in prose.

    Text is capped because a longer page does not contain more contact details —
    it contains more navigation — and the cap is what keeps the per-company cost
    predictable.
    """
    wanted = [w for w in wanted if w in GAP_FIELDS]
    trimmed = page_text[:6000]
    return (
        f"Company: {company_name}\n"
        f"Organisation number: {org_display}\n\n"
        f"Extract only these fields: {', '.join(wanted) or '(none)'}.\n"
        "For each field return {\"value\": ..., \"quote\": ...} or null.\n\n"
        f"--- PAGE TEXT START ---\n{trimmed}\n--- PAGE TEXT END ---"
    )


class LlmClient:
    """Minimal Anthropic Messages client with disk caching and quote verification."""

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        api_key: str | None = None,
        cache_dir: str = "data/llm_cache",
    ) -> None:
        from pathlib import Path

        self.http = client or HttpClient()
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self.http.budget.winding_down

    # ------------------------------------------------------------------ cache

    def _cache_path(self, key: str) -> "Any":
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _cache_put(self, key: str, payload: dict[str, Any]) -> None:
        self._cache_path(key).write_text(json.dumps(payload), encoding="utf-8")

    # ------------------------------------------------------------------- call

    def _messages(
        self,
        model: str,
        system: str,
        user: str,
        *,
        max_tokens: int = 900,
        temperature: float = 0.0,
    ) -> tuple[dict[str, Any], int, int]:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # The shared client is GET-only; POST lives here so the rest of the
        # codebase keeps one HTTP surface -- and, more importantly, one budget.
        # This is the only *metered* provider in the project, so it is the only
        # path that must reserve a worst-case cost before spending.
        import urllib.error
        import urllib.request

        run = self.http.budget.run
        # Worst case: assume the input is as long as we are about to send and the
        # model emits its full max_tokens. The true cost is only known after the
        # response, so reserve high and reconcile down -- never the reverse.
        max_input_tokens = max(1, len(body["system"]) + len(user)) // 4
        reserve_amount = run.token_estimate(
            "anthropic", model=model,
            max_input_tokens=max_input_tokens, max_output_tokens=max_tokens,
        )
        if reserve_amount < 0:
            # No documented price for this model. Under strict_cost this is
            # refused rather than charged as free: an unpriced paid call would
            # make the $10 ceiling unverifiable.
            if run.strict_cost:
                run.cancel("unpriced anthropic model refused")
                raise RuntimeError(
                    f"refusing paid call: no documented price for model {model!r}; "
                    f"add it to budget.PROVIDER_PRICING or set strict_cost=False"
                )
        # Account the outbound request itself, then hold the cost. Both happen
        # before the socket so a refusal never reaches the network.
        run.begin(kind="llm", cost=reserve_amount if reserve_amount > 0 else 0.0)
        reservation = run.reserve_cost("anthropic", "fill_gaps", reserve_amount)

        request = urllib.request.Request(
            ANTHROPIC_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "user-agent": self.http.user_agent,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.http.timeout) as handle:
                response = json.loads(handle.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            run.note_failure()
            # A failed billable call may still be charged; keep the reservation
            # rather than releasing it, so headroom is not overstated.
            run.reconcile_cost(reservation, actual_usd=0.0, unknown=True)
            raise RuntimeError(f"anthropic {exc.code}: {exc.read()[:200]!r}") from exc
        except Exception:
            run.note_failure()
            run.reconcile_cost(reservation, actual_usd=0.0, unknown=True)
            raise

        usage = response.get("usage") or {}
        in_tokens = int(usage.get("input_tokens", 0))
        out_tokens = int(usage.get("output_tokens", 0))
        actual = run.token_estimate(
            "anthropic", model=model,
            max_input_tokens=in_tokens, max_output_tokens=out_tokens,
        )
        if actual < 0:
            run.reconcile_cost(reservation, actual_usd=0.0, unknown=True)
        else:
            run.reconcile_cost(reservation, actual_usd=actual)
            run.charge("llm", cost_usd=0.0, input_tokens=in_tokens, output_tokens=out_tokens)
        run.note_success()

        text = "".join(
            block.get("text", "") for block in response.get("content", [])
            if block.get("type") == "text"
        )
        return {"text": text}, in_tokens, out_tokens

    # -------------------------------------------------------------- gap filling

    def fill_gaps(
        self,
        page_text: str,
        *,
        url: str,
        wanted: Iterable[str],
        company_name: str,
        org_display: str,
    ) -> GapFillResult:
        """Ask the small model for the unresolved fields, then verify its quotes."""
        wanted = [w for w in wanted if w in GAP_FIELDS]
        result = GapFillResult(url=url)
        if not wanted:
            return result
        if not self.enabled:
            result.error = "llm disabled: no ANTHROPIC_API_KEY or budget winding down"
            return result

        cache_key = json.dumps(
            {"model": MODEL_GAP, "url": url, "wanted": sorted(wanted),
             "hash": hashlib.sha256(page_text.encode()).hexdigest()[:32]},
            sort_keys=True,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            result.cached = True
            result.input_tokens = int(cached.get("input_tokens", 0))
            result.output_tokens = int(cached.get("output_tokens", 0))
            for item in cached.get("fields", []):
                result.candidates.append(GapCandidate(**item))
            self._verify(result, page_text)
            return result

        prompt = build_gap_prompt(
            page_text, wanted=wanted, company_name=company_name, org_display=org_display
        )
        try:
            response, in_tokens, out_tokens = self._messages(
                MODEL_GAP, SYSTEM_PROMPT, prompt, max_tokens=900
            )
        except Exception as exc:
            result.error = str(exc)
            return result

        result.input_tokens, result.output_tokens = in_tokens, out_tokens
        self.http.budget.llm_input_tokens += in_tokens
        self.http.budget.llm_output_tokens += out_tokens

        parsed = _parse_json_block(response["text"])
        raw_fields = (parsed or {}).get("fields") or {}
        for name in wanted:
            entry = raw_fields.get(name)
            if not isinstance(entry, dict):
                continue
            value = str(entry.get("value") or "").strip()
            quote = str(entry.get("quote") or "").strip()
            if not value:
                continue
            result.candidates.append(GapCandidate(field=name, value=value, quote=quote))

        self._verify(result, page_text)
        self._cache_put(cache_key, {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "fields": [
                {"field": c.field, "value": c.value, "quote": c.quote,
                 "verified": c.verified, "reject_reason": c.reject_reason}
                for c in result.candidates
            ],
        })
        return result

    @staticmethod
    def _verify(result: GapFillResult, page_text: str) -> None:
        """Discard any candidate whose quote is not genuinely on the page.

        This is the anti-hallucination wall. Note that ``verified`` is set from
        the quote check *alone* — a value can be wrong while its quote is real,
        and that case is the verifier's job, not this one's.
        """
        for candidate in result.candidates:
            if not candidate.quote:
                candidate.verified = False
                candidate.reject_reason = "no quote returned"
                continue
            if len(candidate.quote) < 8:
                candidate.verified = False
                candidate.reject_reason = "quote too short to be evidence"
                continue
            if not whitespace_contains(page_text, candidate.quote):
                candidate.verified = False
                candidate.reject_reason = "quote does not appear in the page text"
                continue
            candidate.verified = True
            candidate.reject_reason = ""


def _parse_json_block(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response."""
    if not text:
        return None
    fenced = re.search(r"(?s)```(?:json)?\s*(\{.*?\})\s*```", text)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


CONFLICT_SYSTEM = """You arbitrate between two candidate values for one company field.

You are given the field, two values, and the supporting quote for each. Choose the \
one that is better evidence of the company's own current contact detail.

Rules:
- A page carrying the company's organisation number outranks a page that does not.
- A company's own website outranks any directory.
- An official register outranks any website.
- If both are defensible and neither is clearly better, answer "unresolved". Do not \
guess, and do not blend the two values into a third.
"""


def resolve_conflict(
    llm: LlmClient,
    *,
    field_name: str,
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> tuple[str, str]:
    """One call to the larger model. Returns ``(choice, rationale)``.

    ``choice`` is ``"first"``, ``"second"`` or ``"unresolved"``. Anything
    unresolved causes the field to be dropped with a recorded reason — the brief
    is explicit that a conflict which survives arbitration means abstention, not
    a coin flip.
    """
    if not llm.enabled:
        return "unresolved", "llm disabled; conflicts are never resolved by guessing"
    prompt = (
        f"Field: {field_name}\n\n"
        f"Value A: {first.get('value')!r}\n"
        f"  quote: {first.get('quote', '')[:300]!r}\n"
        f"  source: {first.get('url')} (match proof: {first.get('match_proof')}, "
        f"confidence {first.get('confidence')})\n\n"
        f"Value B: {second.get('value')!r}\n"
        f"  quote: {second.get('quote', '')[:300]!r}\n"
        f"  source: {second.get('url')} (match proof: {second.get('match_proof')}, "
        f"confidence {second.get('confidence')})\n\n"
        'Answer with JSON only: {"choice": "first"|"second"|"unresolved", '
        '"rationale": "one sentence"}'
    )
    try:
        response, in_tokens, out_tokens = llm._messages(  # noqa: SLF001 - same module family
            MODEL_VERIFY, CONFLICT_SYSTEM, prompt, max_tokens=300
        )
    except Exception as exc:
        return "unresolved", f"arbitration call failed: {exc}"
    llm.http.budget.llm_input_tokens += in_tokens
    llm.http.budget.llm_output_tokens += out_tokens
    parsed = _parse_json_block(response["text"]) or {}
    choice = str(parsed.get("choice") or "unresolved").lower()
    if choice not in ("first", "second"):
        choice = "unresolved"
    return choice, str(parsed.get("rationale") or "").strip()
