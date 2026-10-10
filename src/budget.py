"""The run budget: one accountant for every outbound request and every dollar.

Why this is a runtime service rather than a counter checked at the end: the
failure mode the brief guards against is a single pathological company (a huge
site, a slow host, a model that loops) eating the whole allowance while the rest
of the batch gets nothing. So every tool *asks* before it spends, and a refusal
is a normal, typed outcome — not an exception that aborts the run.

Three ceilings are enforced, from the competition rules:

* **45 minutes** wall clock (:data:`DEFAULT_MAX_SECONDS`)
* **2,000** outbound HTTP attempts (:data:`DEFAULT_MAX_REQUESTS`)
* **$10** declared external API cost (:data:`DEFAULT_MAX_USD`)

Design decisions that matter, each earned by a real defect found in this repo:

* **Accounting happens before the socket, not after.** A request that is refused
  must never reach the network, so :meth:`RunBudget.begin` reserves *before* the
  call and the caller commits the outcome afterwards. Reserving after the fact
  would allow an over-limit request to have already happened.
* **One lock, one counter.** The existing `net.Budget` incremented a bare int
  from worker threads. :class:`RunBudget` guards every mutation with an
  :class:`threading.RLock`, so 20 threads cannot between them exceed 2,000.
* **Unknown cost is unknown, never zero.** A paid provider with no price table
  reports :data:`UNKNOWN`; it is counted, reported separately, and (because its
  cost cannot be bounded) it is refused under :attr:`strict_cost`.
* **Reservation for metered calls.** An LLM call's true cost is unknown until it
  returns, so :meth:`reserve_cost` holds a worst-case amount *before* the call and
  :meth:`reconcile_cost` trues it up afterwards. A new paid operation is refused
  if its worst-case reservation would breach the ceiling.
* **Redirects are counted where observable.** ``urllib`` follows redirects
  internally, so one logical GET can be several socket exchanges. The final
  response's URL is compared to the requested URL; a difference is recorded as a
  redirect so the undercount is *visible* rather than silent.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

__all__ = [
    "RunBudget",
    "BudgetController",
    "BudgetExceeded",
    "Reservation",
    "UNKNOWN",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_MAX_USD",
    "PROVIDER_PRICING",
    "CALL_KINDS",
]

#: The brief's ceilings, as module constants so they appear in one place.
DEFAULT_MAX_SECONDS: float = 45 * 60.0
DEFAULT_MAX_REQUESTS: int = 2000
DEFAULT_MAX_USD: float = 10.0

#: Returned by cost lookups when a provider has no documented price. Sentinel
#: rather than ``0.0`` because conflating "free" with "we do not know" is how a
#: cost ceiling silently stops being a ceiling.
UNKNOWN: float = -1.0

Reason = Literal["ok", "requests", "deadline", "cost", "cancelled", "unknown_cost"]

#: Every spendable call kind. ``free`` here means *no direct API charge* -- it
#: still consumes the outbound-request budget, and provider rate limits still
#: apply. Nothing is exempt from the request ceiling.
CALL_KINDS: dict[str, dict[str, Any]] = {
    # Brreg open data: free, no key, no documented charge.
    "registry": {"provider": "brreg", "free": True, "metered": False},
    "regnskap": {"provider": "brreg", "free": True, "metered": False},
    "web_request": {"provider": "generic", "free": True, "metered": False},
    "robots": {"provider": "generic", "free": True, "metered": False},
    # Search. Keyless engines are free; Brave bills per query under a paid plan.
    "search_keyless": {"provider": "keyless", "free": True, "metered": False},
    "search_brave": {"provider": "brave", "free": False, "metered": True},
    # Identifier enrichment: both keyless and free.
    "gleif": {"provider": "gleif", "free": True, "metered": False},
    "wikidata": {"provider": "wikidata", "free": True, "metered": False},
    "wikipedia": {"provider": "wikipedia", "free": True, "metered": False},
    # The one genuinely metered provider in this project.
    "llm": {"provider": "anthropic", "free": False, "metered": True},
}

#: Documented per-unit prices in USD. Sources are recorded beside each entry so
#: an auditor can re-derive the number rather than trust the table.
PROVIDER_PRICING: dict[str, dict[str, Any]] = {
    "anthropic": {
        # https://www.anthropic.com/pricing (list price, USD per 1M tokens).
        # Keyed by model so a model change cannot silently inherit a stale price.
        "unit": "usd_per_1m_tokens",
        "models": {
            "claude-3-5-haiku-latest": {"input": 0.80, "output": 4.00},
            "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
            "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        },
        "note": "list price; verify against the current pricing page before submitting",
    },
    "brave": {
        # https://brave.com/search/api/ -- per-query billing on paid plans.
        # Conservative per-query estimate; the keyless path is used when unset.
        "unit": "usd_per_query",
        "per_query": 0.005,
        "note": "conservative per-query estimate for a paid plan",
    },
}


class BudgetExceeded(RuntimeError):
    """Raised when a spend is attempted that the budget has already refused."""


@dataclass(slots=True)
class Reservation:
    """A worst-case cost hold placed before a metered call, trued up after it."""

    provider: str
    operation: str
    reserved: float
    actual: float = 0.0
    settled: bool = False
    unknown: bool = False

    @property
    def overrun(self) -> float:
        """Reserved shortfall, if the actual cost exceeded the reservation."""
        if self.unknown or self.reserved < 0:
            return 0.0
        return max(0.0, self.actual - self.reserved)


@dataclass(slots=True)
class RunBudget:
    """Caps, live counters and the audit trail for one run.

    Safe for concurrent use: every mutation holds ``_lock``.
    """

    max_seconds: float = DEFAULT_MAX_SECONDS
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_usd: float = DEFAULT_MAX_USD
    #: Stop starting new expensive work once this fraction of the request budget
    #: is all that remains, so the batch can still finish its in-flight work.
    reserve_fraction: float = 0.05
    #: When True, an operation whose cost cannot be bounded is refused rather
    #: than allowed. This is what lets the $10 claim be made honestly.
    strict_cost: bool = True
    run_id: str = ""
    started_at: str = ""

    # -- counters (mutated only under the lock)
    requests_used: int = 0
    requests_succeeded: int = 0
    requests_failed: int = 0
    requests_redirected: int = 0
    requests_retried: int = 0
    cache_hits: int = 0
    internal_ops: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    by_operation: dict[str, int] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)

    # Tokens are the actual metered units the provider bills on.
    input_tokens: int = 0
    output_tokens: int = 0

    cost_actual_usd: float = 0.0
    cost_reserved_usd: float = 0.0
    cost_unknown_ops: int = 0
    reservations: list[Reservation] = field(default_factory=list)

    cancelled: bool = False
    termination_reason: str = ""

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _start_monotonic: float = field(default_factory=time.monotonic, repr=False)

    # ------------------------------------------------------------- queries

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start_monotonic

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed)

    @property
    def requests_remaining(self) -> int:
        with self._lock:
            return max(0, self.max_requests - self.requests_used)

    @property
    def deadline_reached(self) -> bool:
        return self.elapsed >= self.max_seconds

    @property
    def cost_usd(self) -> float:
        """Committed cost plus unsettled reservations — the worst case right now."""
        with self._lock:
            return round(self.cost_actual_usd + self.cost_reserved_usd, 6)

    @property
    def cost_headroom(self) -> float:
        return max(0.0, self.max_usd - self.cost_usd)

    @property
    def request_reserve(self) -> int:
        """Requests held back so in-flight work can finish during winding down."""
        return max(0, int(self.max_requests * self.reserve_fraction))

    @property
    def winding_down(self) -> bool:
        """True when the run should stop starting new work.

        Covers deadline, cancellation, the request ceiling and the cost ceiling.
        :meth:`winding_down_for` is the per-kind refinement: it exempts *free*
        calls from the cost trigger, because a free request cannot move the cost
        and so should not be stopped by it.
        """
        if self.cancelled or self.deadline_reached:
            return True
        with self._lock:
            if self.requests_used >= self.max_requests:
                return True
            reserve = self.request_reserve
            if reserve and (self.max_requests - self.requests_used) <= reserve:
                return True
            if self.cost_actual_usd + self.cost_reserved_usd >= self.max_usd:
                return True
        return False

    def winding_down_for(self, kind: str) -> bool:
        """Whether ``kind`` specifically should stop being started.

        Identical to :attr:`winding_down`, except that a *free* call is not
        stopped by an exhausted cost ceiling -- it adds no cost.
        """
        meta = CALL_KINDS.get(kind)
        if meta is not None and meta["free"]:
            if self.cancelled or self.deadline_reached:
                return True
            with self._lock:
                if self.requests_used >= self.max_requests:
                    return True
                reserve = self.request_reserve
                return bool(reserve and (self.max_requests - self.requests_used) <= reserve)
        return self.winding_down

    # ------------------------------------------------------- authorisation

    def _refuse(self, reason: str, label: str) -> None:
        with self._lock:
            self.refusals[label] = self.refusals.get(label, 0) + 1
            if not self.termination_reason:
                self.termination_reason = reason

    def allow(self, kind: str = "web_request", *, cost: float = 0.0) -> bool:
        """Whether ``kind`` may spend now. Never raises; records the refusal.

        ``cost`` is the *worst-case* cost of the operation about to run. It is
        checked against headroom so a paid call cannot start if even its ceiling
        would breach the cap.
        """
        meta = CALL_KINDS.get(kind)
        if meta is None:
            self._refuse("unknown operation kind", kind)
            return False
        if self.cancelled:
            self._refuse("cancelled", kind)
            return False
        if self.deadline_reached:
            self._refuse("deadline", kind)
            return False
        if self.winding_down_for(kind):
            # Winding down means "stop starting new work" -- the reserve exists so
            # in-flight work can still finish. Refusing here is the whole point of
            # the reserve; allowing it would make the reserve decorative.
            if self.requests_used >= self.max_requests:
                self._refuse("request limit", kind)
            else:
                self._refuse("winding down (reserve or cost ceiling)", kind)
            return False
        with self._lock:
            if self.requests_used >= self.max_requests:
                self.refusals[kind] = self.refusals.get(kind, 0) + 1
                if not self.termination_reason:
                    self.termination_reason = "request limit"
                return False
            # A metered call whose cost cannot be bounded is refused under
            # strict_cost, because allowing it would make the $10 ceiling a lie.
            if meta["metered"] and cost < 0 and self.strict_cost:
                self.refusals[kind] = self.refusals.get(kind, 0) + 1
                if not self.termination_reason:
                    self.termination_reason = "unbounded cost refused"
                return False
            if cost > 0 and (self.cost_actual_usd + self.cost_reserved_usd + cost) > self.max_usd:
                self.refusals[kind] = self.refusals.get(kind, 0) + 1
                if not self.termination_reason:
                    self.termination_reason = "cost limit"
                return False
        return True

    # ------------------------------------------------------------- spending

    def spend(self, kind: str = "web_request", *, enforce: bool = False) -> int:
        """Account for one outbound attempt. Returns the running total.

        With ``enforce`` a spend that :meth:`allow` would have refused raises
        :class:`BudgetExceeded` instead of proceeding. Prefer :meth:`begin`.
        """
        if enforce and not self.allow(kind):
            raise BudgetExceeded(
                f"budget refused {kind!r}; used {self.requests_used}/{self.max_requests}, "
                f"elapsed {self.elapsed:.1f}s/{self.max_seconds:.0f}s"
            )
        meta = CALL_KINDS.get(kind, {"provider": "unknown", "free": False})
        with self._lock:
            self.requests_used += 1
            provider = meta["provider"]
            self.by_provider[provider] = self.by_provider.get(provider, 0) + 1
            self.by_operation[kind] = self.by_operation.get(kind, 0) + 1
            return self.requests_used

    def begin(
        self, kind: str = "web_request", *, cost: float = 0.0,
        provider_override: str = "",
    ) -> int:
        """Reserve and account before a call. Raises if the budget refuses.

        This is the only correct way to start an outbound request: the check and
        the increment happen together under the lock, so concurrent callers
        cannot both pass the check and between them exceed the cap.

        ``provider_override`` lets a caller that knows the real destination label
        the traffic correctly -- e.g. a robots.txt fetch is operation ``robots``
        but still belongs to the provider it was sent to, so it is not dumped in
        a vague generic bucket.
        """
        ok = False
        with self._lock:
            meta = CALL_KINDS.get(kind)
            if meta is None:
                self._refuse("unknown operation kind", kind)
            elif self.cancelled:
                self._refuse("cancelled", kind)
            elif self.deadline_reached:
                self._refuse("deadline", kind)
            elif self.winding_down_for(kind):
                self._refuse(
                    "request limit" if self.requests_used >= self.max_requests
                    else "winding down (reserve or cost ceiling)",
                    kind,
                )
            elif self.requests_used >= self.max_requests:
                self._refuse("request limit", kind)
            elif meta["metered"] and cost < 0 and self.strict_cost:
                self._refuse("unbounded cost refused", kind)
            elif cost > 0 and (self.cost_actual_usd + self.cost_reserved_usd + cost) > self.max_usd:
                self._refuse("cost limit", kind)
            else:
                self.requests_used += 1
                provider = provider_override or meta["provider"]
                self.by_provider[provider] = self.by_provider.get(provider, 0) + 1
                self.by_operation[kind] = self.by_operation.get(kind, 0) + 1
                ok = True
                used = self.requests_used
        if not ok:
            raise BudgetExceeded(
                f"refused {kind!r}: {self.termination_reason or 'budget exhausted'}"
            )
        return used

    # ------------------------------------------------------------ outcomes

    def note_success(self, *, redirected_from: str = "", requested: str = "") -> None:
        with self._lock:
            self.requests_succeeded += 1
            if redirected_from and requested and redirected_from != requested:
                self.requests_redirected += 1

    def note_failure(self) -> None:
        with self._lock:
            self.requests_failed += 1

    def note_retry(self) -> None:
        with self._lock:
            self.requests_retried += 1

    def note_cache_hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def note_internal(self, operation: str) -> None:
        """Record an internal operation that made no network request."""
        with self._lock:
            self.internal_ops += 1
            self.by_operation[operation] = self.by_operation.get(operation, 0) + 1

    # ---------------------------------------------------------------- cost

    def price(self, provider: str, *, model: str = "") -> float:
        """Documented price, or :data:`UNKNOWN`. Never returns 0.0 for unknown."""
        table = PROVIDER_PRICING.get(provider)
        if table is None:
            return UNKNOWN
        if table["unit"] == "usd_per_query":
            return float(table.get("per_query", UNKNOWN))
        if table["unit"] == "usd_per_1m_tokens":
            models = table.get("models", {})
            entry = models.get(model)
            if entry is None:
                return UNKNOWN
            # Returned as a blended marker; token_estimate() does the real work.
            return float(entry["input"])
        return UNKNOWN

    def token_estimate(
        self, provider: str, *, model: str, max_input_tokens: int, max_output_tokens: int
    ) -> float:
        """Worst-case cost of a metered call, or :data:`UNKNOWN` if unpriced."""
        table = PROVIDER_PRICING.get(provider)
        if table is None or table["unit"] != "usd_per_1m_tokens":
            return UNKNOWN
        entry = table.get("models", {}).get(model)
        if entry is None:
            return UNKNOWN
        return (
            max_input_tokens / 1_000_000 * entry["input"]
            + max_output_tokens / 1_000_000 * entry["output"]
        )

    def reserve_cost(
        self, provider: str, operation: str, amount: float
    ) -> Reservation:
        """Hold a worst-case amount before a metered call."""
        unknown = amount < 0
        with self._lock:
            if unknown:
                self.cost_unknown_ops += 1
            else:
                self.cost_reserved_usd = round(self.cost_reserved_usd + amount, 6)
            res = Reservation(
                provider=provider, operation=operation,
                reserved=amount, unknown=unknown,
            )
            self.reservations.append(res)
            return res

    def reconcile_cost(
        self, reservation: Reservation, *, actual_usd: float, unknown: bool = False
    ) -> None:
        """True a reservation up to what was actually charged.

        Three outcomes:

        * **known** -- release the hold, add the true charge;
        * **unknown** -- keep the hold. An unmeasurable charge must not free
          headroom that later spend would treat as available, because that is how
          a cost ceiling silently stops being a ceiling;
        * already settled -- no-op, so a retry cannot double charge.
        """
        with self._lock:
            if reservation.settled:
                return
            reservation.settled = True

            if unknown:
                reservation.unknown = True
                self.cost_unknown_ops += 1
                # The hold from reserve_cost is *already* in cost_reserved_usd, so
                # there is nothing to re-add -- the reservation simply stays
                # outstanding. That is the point: an unmeasurable charge must not
                # free headroom that later spend would treat as available.
                if reservation.reserved > 0:
                    reservation.actual = reservation.reserved
                return

            if reservation.reserved > 0:
                self.cost_reserved_usd = round(
                    self.cost_reserved_usd - reservation.reserved, 6
                )
            reservation.actual = actual_usd
            self.cost_actual_usd = round(self.cost_actual_usd + actual_usd, 6)

    def charge(self, kind: str, *, cost_usd: float = 0.0,
               input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Record a completed (non-reserved) charge, typically a fixed price."""
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            if cost_usd < 0:
                self.cost_unknown_ops += 1
            else:
                self.cost_actual_usd = round(self.cost_actual_usd + cost_usd, 6)

    # ------------------------------------------------------------ control

    def cancel(self, reason: str = "cancelled") -> None:
        """Request graceful shutdown: no new requests start after this returns."""
        with self._lock:
            self.cancelled = True
            if not self.termination_reason:
                self.termination_reason = reason

    def check_deadline(self, stage: str = "") -> None:
        """Raise between processing stages when the deadline has passed."""
        if self.deadline_reached:
            if not self.termination_reason:
                self.termination_reason = "deadline"
            raise BudgetExceeded(
                f"deadline reached after {self.elapsed:.1f}s"
                + (f" (during {stage})" if stage else "")
            )

    # -------------------------------------------------------------- report

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "ended_at": _now_iso(),
                "elapsed_seconds": round(self.elapsed, 2),
                "limits": {
                    "max_seconds": self.max_seconds,
                    "max_requests": self.max_requests,
                    "max_usd": self.max_usd,
                    "strict_cost": self.strict_cost,
                },
                "requests": {
                    "outbound_attempts": self.requests_used,
                    "succeeded": self.requests_succeeded,
                    "failed": self.requests_failed,
                    "redirected": self.requests_redirected,
                    "retried": self.requests_retried,
                    "remaining": max(0, self.max_requests - self.requests_used),
                },
                "cache_hits": self.cache_hits,
                "internal_operations": self.internal_ops,
                "by_provider": dict(self.by_provider),
                "by_operation": dict(self.by_operation),
                "cost": {
                    "actual_usd": round(self.cost_actual_usd, 6),
                    "reserved_usd": round(self.cost_reserved_usd, 6),
                    "worst_case_usd": round(
                        self.cost_actual_usd + self.cost_reserved_usd, 6
                    ),
                    "headroom_usd": round(
                        max(0.0, self.max_usd - self.cost_actual_usd - self.cost_reserved_usd), 6
                    ),
                    "unknown_cost_operations": self.cost_unknown_ops,
                    "pricing_sources": {
                        k: v.get("note", "") for k, v in PROVIDER_PRICING.items()
                    },
                },
                "tokens": {"input": self.input_tokens, "output": self.output_tokens},
                "refusals": dict(self.refusals),
                "cancelled": self.cancelled,
                "termination_reason": self.termination_reason,
                "winding_down": self.winding_down,
            }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Compatibility shim
# ---------------------------------------------------------------------------
#
# ``BudgetController`` predates :class:`RunBudget` and was never wired into the
# production path -- it was referenced only by its own tests, and its defaults
# (6000 requests, $5) contradicted the competition ceilings (2000, $10). It is
# kept here so that import path stays valid, but it now *delegates* to
# :class:`RunBudget` with the brief's real ceilings, so anything that does use it
# inherits correct limits rather than the old ones.
#
# Prefer ``RunBudget`` for new code.

#: Old kind names mapped onto the unified call kinds.
_LEGACY_KINDS: dict[str, str] = {
    "registry": "registry",
    "website_request": "web_request",
    "web_request": "web_request",
    "brave_search": "search_brave",
    "llm": "llm",
}


@dataclass
class BudgetController:
    """Deprecated. Delegates to :class:`RunBudget`; kept for import compatibility."""

    max_requests: int = DEFAULT_MAX_REQUESTS
    max_search: int = 200
    max_llm: int = 500
    max_seconds: float = DEFAULT_MAX_SECONDS
    max_usd: float = DEFAULT_MAX_USD
    reserve_fraction: float = 0.05
    _budget: RunBudget = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._budget is None:
            self._budget = RunBudget(
                max_requests=self.max_requests,
                max_seconds=self.max_seconds,
                max_usd=self.max_usd,
                reserve_fraction=self.reserve_fraction,
            )

    # -- counters exposed under the legacy names, computed from the real budget
    @property
    def requests_used(self) -> int:
        return self._budget.requests_used

    @property
    def search_used(self) -> int:
        return self._budget.by_operation.get("search_brave", 0)

    @property
    def llm_used(self) -> int:
        return self._budget.by_operation.get("llm", 0)

    @property
    def web_used(self) -> int:
        return self._budget.by_operation.get("web_request", 0)

    @property
    def input_tokens(self) -> int:
        return self._budget.input_tokens

    @property
    def output_tokens(self) -> int:
        return self._budget.output_tokens

    @property
    def cost_usd(self) -> float:
        return self._budget.cost_usd

    @property
    def elapsed(self) -> float:
        return self._budget.elapsed

    @property
    def requests_remaining(self) -> int:
        return self._budget.requests_remaining

    @property
    def winding_down(self) -> bool:
        return self._budget.winding_down

    @property
    def refusals(self) -> dict[str, int]:
        return self._budget.refusals

    def allow(self, kind: str) -> bool:
        mapped = _LEGACY_KINDS.get(kind)
        if mapped is None:
            return False
        # The legacy per-kind caps are preserved as an additional ceiling so the
        # existing tests' semantics (max_search / max_llm) still hold.
        if mapped == "search_brave" and self.search_used >= self.max_search:
            self._budget.refusals[kind] = self._budget.refusals.get(kind, 0) + 1
            return False
        if mapped == "llm" and self.llm_used >= self.max_llm:
            self._budget.refusals[kind] = self._budget.refusals.get(kind, 0) + 1
            return False
        return self._budget.allow(mapped)

    def spend(self, kind: str, *, cost_usd: float = 0.0, input_tokens: int = 0,
              output_tokens: int = 0, enforce: bool = False) -> None:
        mapped = _LEGACY_KINDS.get(kind)
        if mapped is None:
            raise ValueError(f"unknown budget kind: {kind!r}")
        if enforce and not self.allow(kind):
            raise BudgetExceeded(f"budget refused {kind}")
        self._budget.spend(mapped)
        if cost_usd or input_tokens or output_tokens:
            self._budget.charge(mapped, cost_usd=cost_usd,
                                input_tokens=input_tokens, output_tokens=output_tokens)

    def to_dict(self) -> dict[str, Any]:
        data = self._budget.to_dict()
        # Keep the legacy keys the old report promised.
        data.update({
            "max_requests": self.max_requests,
            "requests_used": self.requests_used,
            "requests_remaining": self.requests_remaining,
            "search_used": self.search_used,
            "search_max": self.max_search,
            "llm_used": self.llm_used,
            "llm_max": self.max_llm,
            "web_used": self.web_used,
            "cost_usd": self.cost_usd,
            "elapsed_seconds": round(self.elapsed, 2),
            "winding_down": self.winding_down,
        })
        return data

