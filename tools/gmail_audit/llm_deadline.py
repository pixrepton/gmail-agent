"""One coherent deadline model for structured LLM stage calls.

Why this module exists
----------------------
Before AIOS-RUNTIME-RELIABILITY-01 the intake-reasoning call had two independent,
mutually unaware timeout systems stacked on top of each other:

    gmail_intake._run_llm_with_timeout : ThreadPoolExecutor(...).result(timeout=60)
    settings.http_timeout              : requests.post(..., timeout=60)   PER ATTEMPT
    settings.http_max_retries          : 4 attempts PER PROVIDER
    llm_fallback_providers             : openai_chat -> groq -> cerebras

The outer hard-kill was numerically identical to a single inner HTTP attempt and its
clock started first, so the inner retry/fallback machinery was structurally unreachable:
the first provider attempt was still in flight when the outer envelope fired. Fresh38
measured the consequence directly -- 6 of 38 first attempts died as `INTAKE_LLM_TIMEOUT`
with the `groq` fallback (4 live keys) never invoked. `future.cancel()` could not stop the
already-running request either, so the abandoned thread kept running unobserved.

The model
---------
There is exactly one budget owner: the *stage*. Everything below it asks how much time is
left rather than keeping a private clock.

    stage budget                       (contract, settings.llm_stage_budget_sec)
      \\_ per-provider share            (remaining / providers still to try)
           \\_ per-attempt timeout      min(http_timeout, provider budget, stage budget)
                \\_ retry               only while the provider share allows a real attempt
           \\_ fallback                 only while the stage budget allows a real attempt

Properties this guarantees:

* A hard upper bound still exists -- the stage budget, not a wrapper thread.
* Retry and fallback actually get time, because no single attempt may consume the share
  reserved for the providers that have not been tried yet.
* Fast provider errors (429/5xx/auth) still fall through immediately; nothing here adds
  latency to a quick failure.
* A slow provider cannot consume unbounded total runtime.
* Terminal timeout is structured and attributable (see :meth:`StageDeadline.telemetry`).
* No wrapper abandons in-flight work, so no orphan thread outlives the call.

Threading note: the deadline lives in a :class:`~contextvars.ContextVar`, and a stage sets
it inside its own thread. ``ThreadPoolExecutor`` does not copy context into workers, so a
worker thread simply sees no deadline rather than inheriting a stale one. Code that hands
work to another thread must pass the remaining budget explicitly.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

# A stage budget must fit, at minimum, a real attempt on the primary provider, one retry,
# and a real attempt on the first fallback -- otherwise the configured resilience is
# decorative. At the shipped defaults (http_timeout=60, http_max_retries=4, one live
# fallback) that floor is 60 + backoff + 60 ~= 130s; 180 leaves headroom for a second
# retry without letting one stalled stage dominate a whole case.
DEFAULT_LLM_STAGE_BUDGET_SEC = 180

# Starting an HTTP attempt with less than this left cannot produce a useful result; it only
# converts remaining budget into a guaranteed timeout. Below this floor we stop and report.
DEFAULT_LLM_MIN_ATTEMPT_SEC = 5


class DeadlineExhausted(RuntimeError):
    """The stage budget ran out before a provider produced a terminal result."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class StageDeadline:
    """A monotonic budget for one structured LLM stage call."""

    stage_name: str
    budget_sec: float
    started_monotonic: float

    @property
    def deadline_monotonic(self) -> float:
        return self.started_monotonic + self.budget_sec

    def elapsed_sec(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def remaining_sec(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def expired(self) -> bool:
        return self.remaining_sec() <= 0.0

    def has_room_for_attempt(self, min_attempt_sec: float = DEFAULT_LLM_MIN_ATTEMPT_SEC) -> bool:
        return self.remaining_sec() >= max(0.0, min_attempt_sec)

    def telemetry(self) -> dict[str, Any]:
        """Budget provenance for logs and stage metadata. Contains no request content."""
        return {
            "stage": self.stage_name,
            "configured_stage_budget_ms": int(round(self.budget_sec * 1000)),
            "elapsed_ms": int(round(self.elapsed_sec() * 1000)),
            "remaining_budget_ms": int(round(self.remaining_sec() * 1000)),
        }


_CURRENT: ContextVar[StageDeadline | None] = ContextVar("llm_stage_deadline", default=None)


def current_deadline() -> StageDeadline | None:
    """The deadline governing the call in progress, or None outside any stage."""
    return _CURRENT.get()


def remaining_sec() -> float | None:
    """Seconds left in the active stage budget, or None when no deadline is active."""
    deadline = _CURRENT.get()
    return None if deadline is None else deadline.remaining_sec()


@contextmanager
def stage_deadline(stage_name: str, budget_sec: float) -> Iterator[StageDeadline]:
    """Own a stage budget for the duration of the block.

    A nested stage never extends an outer one: it is clamped to whatever the outer
    deadline has left, so the outermost caller's contract always holds.
    """
    requested = max(0.0, float(budget_sec))
    outer = _CURRENT.get()
    effective = requested if outer is None else min(requested, outer.remaining_sec())
    deadline = StageDeadline(
        stage_name=str(stage_name or "unknown_stage"),
        budget_sec=effective,
        started_monotonic=time.monotonic(),
    )
    token = _CURRENT.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT.reset(token)


@dataclass(frozen=True, slots=True)
class ProviderBudget:
    """The slice of the stage budget reserved for one provider's own retry loop."""

    provider: str
    budget_sec: float
    started_monotonic: float

    def elapsed_sec(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def remaining_sec(self) -> float:
        return max(0.0, self.started_monotonic + self.budget_sec - time.monotonic())

    def has_room_for_attempt(self, min_attempt_sec: float = DEFAULT_LLM_MIN_ATTEMPT_SEC) -> bool:
        return self.remaining_sec() >= max(0.0, min_attempt_sec)


_CURRENT_PROVIDER: ContextVar[ProviderBudget | None] = ContextVar("llm_provider_budget", default=None)


def current_provider_budget() -> ProviderBudget | None:
    """The budget slice for the provider currently being tried, if the router set one."""
    return _CURRENT_PROVIDER.get()


@contextmanager
def provider_budget_scope(provider: str, budget_sec: float | None) -> Iterator[ProviderBudget | None]:
    """Reserve a slice of the stage budget for one provider attempt sequence.

    ``budget_sec is None`` means "no deadline is active" -- the scope becomes a no-op so
    behavior outside a stage is unchanged.
    """
    if budget_sec is None:
        yield None
        return
    scope = ProviderBudget(
        provider=str(provider or "unknown_provider"),
        budget_sec=max(0.0, float(budget_sec)),
        started_monotonic=time.monotonic(),
    )
    token = _CURRENT_PROVIDER.set(scope)
    try:
        yield scope
    finally:
        _CURRENT_PROVIDER.reset(token)


def retry_window_sec(min_attempt_sec: float = DEFAULT_LLM_MIN_ATTEMPT_SEC) -> float | None:
    """How much time a provider's own retry loop still has, or None when unbounded.

    Returns the tighter of the provider slice and the whole stage, so an in-provider retry
    can never outlive the stage even if the slice arithmetic says otherwise.
    """
    stage = _CURRENT.get()
    provider = _CURRENT_PROVIDER.get()
    if stage is None and provider is None:
        return None
    windows = [w.remaining_sec() for w in (stage, provider) if w is not None]
    return min(windows)


def resolve_stage_budget_sec(settings: Any) -> float:
    """Read the configured stage budget, falling back to the documented default."""
    raw = getattr(settings, "llm_stage_budget_sec", None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_LLM_STAGE_BUDGET_SEC)
    return value if value > 0 else float(DEFAULT_LLM_STAGE_BUDGET_SEC)


def resolve_min_attempt_sec(settings: Any) -> float:
    """Read the configured minimum useful attempt window."""
    raw = getattr(settings, "llm_min_attempt_sec", None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_LLM_MIN_ATTEMPT_SEC)
    return value if value > 0 else float(DEFAULT_LLM_MIN_ATTEMPT_SEC)


def provider_budget_sec(
    *,
    providers_remaining: int,
    deadline: StageDeadline | None = None,
) -> float | None:
    """Share of the remaining stage budget reserved for the provider about to be tried.

    Splitting evenly across the providers that have not been tried yet is what keeps a slow
    primary from eating the fallback's turn -- the exact failure Fresh38 measured. Time a
    provider does not spend rolls forward to the next one, so a fast failure costs the chain
    nothing, and the last provider in the chain gets everything that is left.
    """
    active = deadline if deadline is not None else _CURRENT.get()
    if active is None:
        return None
    return active.remaining_sec() / max(1, int(providers_remaining))


def attempt_timeout_sec(
    configured_timeout: float,
    *,
    provider_budget: float | None = None,
    deadline: StageDeadline | None = None,
) -> float:
    """Per-attempt HTTP timeout: never more than the budget that is actually left.

    With no deadline active this returns ``configured_timeout`` unchanged, so callers
    outside a stage (scripts, one-off tools, tests) keep their previous behavior.
    """
    candidates = [float(configured_timeout)]
    if provider_budget is not None:
        candidates.append(max(0.0, float(provider_budget)))
    else:
        scope = _CURRENT_PROVIDER.get()
        if scope is not None:
            candidates.append(scope.remaining_sec())
    active = deadline if deadline is not None else _CURRENT.get()
    if active is not None:
        candidates.append(active.remaining_sec())
    return max(0.0, min(candidates))
