"""Fault-injection proof for the FIX-RT01 stage deadline model.

These tests exist because the defect they cover was measured, not theorized: Fresh38's
pre-fix first-attempt baseline was 32/38, and all 6 failures were the same mechanism --
`gmail_intake._run_llm_with_timeout` wrapped the intake LLM call in
`ThreadPoolExecutor(...).result(timeout=60)` while the chain underneath was configured for
`http_timeout=60` per attempt x `http_max_retries=4` per provider across an
`openai_chat -> groq -> cerebras` fallback. The outer clock started first and was numerically
identical to a single inner attempt, so retry and fallback were structurally unreachable and
`future.cancel()` left the real request running unobserved.

Provider behavior is stubbed at the router/HTTP boundary so none of this depends on live
provider luck. Timing assertions are deliberately generous: they assert budget *ordering* and
*bounds*, never precise millisecond behavior.

    CASE A  primary fast success                -> succeeds, no fallback
    CASE B  primary quick retryable failure     -> fallback runs, later provider succeeds
    CASE C  primary stalls                      -> bounded, fallback still gets its turn
    CASE D  all providers unavailable           -> bounded terminal failure, no infinite retry
    CASE E  total deadline exhausted            -> fail closed with structured provenance
    CASE F  timeout/cancellation                -> no orphan work outlives the call
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import requests

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import groq_client  # noqa: E402
from llm_deadline import (  # noqa: E402
    DeadlineExhausted,
    attempt_timeout_sec,
    current_deadline,
    provider_budget_sec,
    retry_window_sec,
    stage_deadline,
)
from llm_provider_router import (  # noqa: E402
    LLMProvider,
    LLMRouter,
    LLMRouterError,
    classify_provider_error,
)


class _ProviderError(RuntimeError):
    """Stand-in for GroqClientError with the same ``details`` contract the router reads."""

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


def _provider(name: str, call, *, configured: bool = True, missing: str = "") -> LLMProvider:
    return LLMProvider(
        provider=name,
        backend=name,
        model=f"{name}-model",
        call=call,
        configured=configured,
        missing_config=missing,
    )


def _ok(name: str):
    def _call() -> tuple[dict, dict]:
        return {"result": name}, {"model": f"{name}-model"}

    return _call


def _fails(exc: Exception, *, after_sec: float = 0.0, log: list | None = None):
    def _call() -> tuple[dict, dict]:
        if log is not None:
            log.append(("start", time.monotonic()))
        if after_sec:
            time.sleep(after_sec)
        raise exc

    return _call


def _stalls_until_budget_gone(*, log: list | None = None):
    """Consume the provider's own slice, then fail the way a real timeout would.

    This is what a stalled provider looks like from the router's side: it burns the budget
    it was given and raises a retryable transport error, rather than returning.
    """

    def _call() -> tuple[dict, dict]:
        window = retry_window_sec()
        if log is not None:
            log.append(("stall_window", window))
        if window is not None:
            time.sleep(max(0.0, window))
        raise _ProviderError("HTTP timeout while calling provider.", details={"attempt": 1})

    return _call


# ── CASE A ────────────────────────────────────────────────────────────────────────────


def test_case_a_primary_fast_success_uses_no_fallback():
    providers = [_provider("openai_chat", _ok("openai_chat")), _provider("groq", _ok("groq"))]
    with stage_deadline("intake_reasoning", 30):
        response, meta = LLMRouter(providers).run()

    assert response == {"result": "openai_chat"}
    assert meta["llm_selected_provider"] == "openai_chat"
    assert meta["llm_fallback_used"] is False
    assert meta["llm_fallback_index"] == 0
    assert [a["status"] for a in meta["llm_provider_attempts"]] == ["success"]


def test_case_a_success_carries_budget_provenance():
    with stage_deadline("intake_reasoning", 30):
        _, meta = LLMRouter([_provider("openai_chat", _ok("openai_chat"))]).run()

    telemetry = meta["llm_stage_deadline"]
    assert telemetry["stage"] == "intake_reasoning"
    assert telemetry["configured_stage_budget_ms"] == 30_000
    assert 0 < telemetry["remaining_budget_ms"] <= 30_000
    attempt = meta["llm_provider_attempts"][0]
    assert attempt["provider_budget_ms"] > 0
    assert attempt["configured_stage_budget_ms"] == 30_000


# ── CASE B ────────────────────────────────────────────────────────────────────────────


def test_case_b_quick_retryable_failure_reaches_fallback_provider():
    """The pre-fix defect in one assertion: this is the fallback that never used to run."""
    rate_limited = _ProviderError("429 Too Many Requests", details={"status_code": 429})
    providers = [
        _provider("openai_chat", _fails(rate_limited)),
        _provider("groq", _ok("groq")),
    ]
    started = time.monotonic()
    with stage_deadline("intake_reasoning", 30):
        response, meta = LLMRouter(providers).run()
    elapsed = time.monotonic() - started

    assert response == {"result": "groq"}
    assert meta["llm_selected_provider"] == "groq"
    assert meta["llm_fallback_used"] is True
    assert meta["llm_fallback_index"] == 1
    assert meta["llm_fallback_reason"] == "openai_chat_rate_limit"
    statuses = [(a["provider"], a["status"], a["error_class"]) for a in meta["llm_provider_attempts"]]
    assert statuses == [("openai_chat", "failed", "rate_limit"), ("groq", "success", None)]
    # A fast failure must cost the chain nothing; it must not wait out the primary's share.
    assert elapsed < 5, f"quick retryable failure should fall through immediately, took {elapsed:.1f}s"


def test_case_b_unspent_budget_rolls_forward_to_the_next_provider():
    seen: dict[str, float] = {}

    def _record_then_fail(name: str):
        def _call() -> tuple[dict, dict]:
            seen[name] = retry_window_sec() or 0.0
            raise _ProviderError("503 Server Error", details={"status_code": 503})

        return _call

    def _record_then_ok(name: str):
        def _call() -> tuple[dict, dict]:
            seen[name] = retry_window_sec() or 0.0
            return {"result": name}, {}

        return _call

    providers = [
        _provider("openai_chat", _record_then_fail("openai_chat")),
        _provider("groq", _record_then_ok("groq")),
    ]
    with stage_deadline("intake_reasoning", 60):
        LLMRouter(providers).run()

    # Two configured providers -> the primary is reserved half the stage budget, so a slow
    # primary can never eat the fallback's turn.
    assert 25 <= seen["openai_chat"] <= 31, seen
    # It failed instantly, so the fallback inherits nearly the whole remaining budget.
    assert seen["groq"] >= 55, seen


# ── CASE C ────────────────────────────────────────────────────────────────────────────


def test_case_c_stalled_primary_is_bounded_and_fallback_still_runs():
    """This is the pre-fix failure inverted: a stalled primary must not eat the fallback.

    The budget is scaled down (and with it the minimum-useful-attempt floor) so the test runs
    in seconds; the property under test is the ordering, not the absolute numbers.
    """
    log: list = []
    providers = [
        _provider("openai_chat", _stalls_until_budget_gone(log=log)),
        _provider("groq", _ok("groq")),
    ]
    started = time.monotonic()
    with stage_deadline("intake_reasoning", 4):
        response, meta = LLMRouter(providers, min_attempt_sec=0.5).run()
    elapsed = time.monotonic() - started

    assert response == {"result": "groq"}, "fallback must still get a turn after a stalled primary"
    assert meta["llm_fallback_used"] is True
    # The stalled primary was handed half the stage budget, not the whole thing.
    stall_window = log[0][1]
    assert 1.5 <= stall_window <= 2.5, f"primary share should be ~half the budget, got {stall_window}"
    assert elapsed < 4, f"stage must finish inside its budget, took {elapsed:.1f}s"


def test_case_c_budget_too_small_to_fund_a_fallback_says_so_explicitly():
    """A budget that cannot fund a second real attempt must report that, not fake a try.

    With the production floor (5s) an 8s budget leaves 4s after the primary's share -- below
    the floor. The chain stops and names the reason instead of starting an attempt that could
    only time out. Production defaults (180s budget) leave ~90s for the fallback, far above it.
    """
    providers = [
        _provider("openai_chat", _stalls_until_budget_gone()),
        _provider("groq", _ok("groq")),
    ]
    with stage_deadline("intake_reasoning", 8):
        with pytest.raises(LLMRouterError) as caught:
            LLMRouter(providers).run()

    details = caught.value.details
    assert details["terminal_failure_reason"] == "stage_deadline_exhausted"
    skipped = details["llm_provider_attempts"][1]
    assert skipped["provider"] == "groq"
    assert skipped["error_class"] == "deadline_exhausted"
    assert "not attempted (stage budget exhausted" in str(caught.value)


def test_case_c_per_attempt_timeout_never_exceeds_remaining_budget():
    """A provider attempt may never be handed more time than the stage actually has."""
    with stage_deadline("intake_reasoning", 10):
        assert attempt_timeout_sec(60) <= 10
        assert attempt_timeout_sec(3) == pytest.approx(3, abs=0.1)

    # Outside a stage the configured value is passed through untouched.
    assert attempt_timeout_sec(60) == 60


# ── CASE D ────────────────────────────────────────────────────────────────────────────


def test_case_d_all_providers_unavailable_fails_closed_without_infinite_retry():
    calls: list[str] = []

    def _always_fails(name: str):
        def _call() -> tuple[dict, dict]:
            calls.append(name)
            raise _ProviderError("503 Server Error", details={"status_code": 503})

        return _call

    providers = [
        _provider("openai_chat", _always_fails("openai_chat")),
        _provider("groq", _always_fails("groq")),
        _provider("cerebras", _always_fails("cerebras")),
    ]
    with stage_deadline("intake_reasoning", 30):
        with pytest.raises(LLMRouterError) as caught:
            LLMRouter(providers).run()

    # Each provider is tried exactly once by the router; no unbounded re-entry.
    assert calls == ["openai_chat", "groq", "cerebras"]
    details = caught.value.details
    assert [a["status"] for a in details["llm_provider_attempts"]] == ["failed"] * 3


def test_case_d_unconfigured_providers_are_skipped_not_counted_as_attempts():
    providers = [
        _provider("openai_chat", _ok("x"), configured=False, missing="OPENAI_COMPAT_API_KEY"),
        _provider("groq", _ok("groq")),
    ]
    with stage_deadline("intake_reasoning", 30):
        response, meta = LLMRouter(providers).run()

    assert response == {"result": "groq"}
    attempts = meta["llm_provider_attempts"]
    assert attempts[0]["status"] == "skipped"
    assert attempts[0]["error_class"] == "config"


# ── CASE E ────────────────────────────────────────────────────────────────────────────


def test_case_e_exhausted_deadline_fails_closed_with_structured_provenance():
    providers = [_provider("openai_chat", _ok("openai_chat")), _provider("groq", _ok("groq"))]
    with stage_deadline("intake_reasoning", 0.0):
        with pytest.raises(LLMRouterError) as caught:
            LLMRouter(providers).run()

    details = caught.value.details
    assert details["terminal_failure_reason"] == "stage_deadline_exhausted"
    assert details["llm_stage_deadline"]["stage"] == "intake_reasoning"
    first = details["llm_provider_attempts"][0]
    assert first["status"] == "skipped"
    assert first["error_class"] == "deadline_exhausted"
    assert first["retryable"] is False
    # The message must name the cause, not just say "no provider".
    assert "stage budget exhausted" in str(caught.value).lower()


def test_case_e_no_provider_is_called_once_the_budget_is_gone():
    called: list[str] = []

    def _tracking(name: str):
        def _call() -> tuple[dict, dict]:
            called.append(name)
            return {"result": name}, {}

        return _call

    providers = [_provider("openai_chat", _tracking("openai_chat"))]
    with stage_deadline("intake_reasoning", 0.0):
        with pytest.raises(LLMRouterError):
            LLMRouter(providers).run()

    assert called == [], "starting an attempt with no budget only manufactures a timeout"


def test_case_e_budget_exhausted_inside_the_last_provider_is_still_a_deadline_terminal():
    """Both terminal paths out of the router must label themselves the same way.

    The router can raise from two places: the pre-call budget check, and inside the loop when
    the last provider fails. `gmail_intake._run_llm_with_timeout` keys on
    `terminal_failure_reason` to decide between a structured `central_llm_failed` and
    re-raising, so a budget exhausted inside the last provider must not look like an ordinary
    provider fault.
    """
    def _exhausted() -> tuple[dict, dict]:
        raise DeadlineExhausted("budget gone", details={"error_class": "deadline_exhausted"})

    with stage_deadline("intake_reasoning", 30):
        with pytest.raises(LLMRouterError) as caught:
            LLMRouter([_provider("openai_chat", _exhausted)]).run()

    details = caught.value.details
    assert details["terminal_failure_reason"] == "stage_deadline_exhausted"
    assert details["llm_stage_deadline"]["stage"] == "intake_reasoning"


def test_case_d_ordinary_provider_exhaustion_is_labelled_as_such():
    """The counterpart: a real provider fault must not masquerade as a budget timeout."""
    def _fails() -> tuple[dict, dict]:
        raise _ProviderError("401 Unauthorized", details={"status_code": 401})

    with stage_deadline("intake_reasoning", 30):
        with pytest.raises(LLMRouterError) as caught:
            LLMRouter([_provider("openai_chat", _fails)]).run()

    assert caught.value.details["terminal_failure_reason"] == "provider_chain_failed"


def test_case_e_deadline_exhaustion_is_terminal_not_retryable():
    info = classify_provider_error(DeadlineExhausted("budget gone", details={"error_class": "deadline_exhausted"}))
    assert info.error_class == "deadline_exhausted"
    assert info.retryable is False


def test_case_e_nested_stage_cannot_extend_an_outer_budget():
    with stage_deadline("outer", 5):
        with stage_deadline("inner", 3600) as inner:
            assert inner.budget_sec <= 5, "an inner stage must never outlive its caller's contract"


# ── CASE F ────────────────────────────────────────────────────────────────────────────


def test_case_f_no_orphan_work_survives_a_bounded_stage():
    """The old wrapper returned while its worker thread kept running. Nothing may now.

    Under `ThreadPoolExecutor(...).result(timeout=60)` + `future.cancel()`, an already-running
    provider call was abandoned: the caller moved on, the request kept going, and its outcome
    was never observed. This asserts the property that failure violated -- when the stage
    returns, no injected provider work is still in flight.
    """
    in_flight = 0
    lock = threading.Lock()
    peak_after_return = []

    def _slow_provider() -> tuple[dict, dict]:
        nonlocal in_flight
        with lock:
            in_flight += 1
        try:
            time.sleep(0.3)
            raise _ProviderError("503 Server Error", details={"status_code": 503})
        finally:
            with lock:
                in_flight -= 1

    providers = [_provider("openai_chat", _slow_provider), _provider("groq", _ok("groq"))]
    threads_before = threading.active_count()

    with stage_deadline("intake_reasoning", 10):
        response, _meta = LLMRouter(providers).run()

    with lock:
        peak_after_return.append(in_flight)

    assert response == {"result": "groq"}
    assert peak_after_return == [0], "provider work must be finished, not abandoned, when the stage returns"
    assert threading.active_count() <= threads_before, "the bounded path must not leave worker threads behind"


def test_case_f_intake_wrapper_no_longer_spawns_its_own_executor():
    """`gmail_intake._run_llm_with_timeout` must not reintroduce a second timeout system."""
    source = (TOOL_DIR / "gmail_intake.py").read_text(encoding="utf-8")
    start = source.index("def _run_llm_with_timeout(")
    end = source.index("def run_intake_reasoning(", start)
    body = source[start:end]

    assert "ThreadPoolExecutor" not in body.split('"""')[2], (
        "the intake wrapper must not create an executor; the stage deadline owns the bound"
    )
    assert "future.result(timeout=" not in body
    assert "executor.submit(" not in body


# ── HTTP boundary: the per-attempt timeout actually handed to requests ────────────────


def _http_settings(**overrides):
    base = SimpleNamespace(
        openai_chat_completions_url="https://example.invalid/v1/chat/completions",
        openai_compat_api_key="test-key",
        http_timeout=60,
        http_max_retries=4,
        http_retry_base_delay=2.0,
        llm_stage_budget_sec=180,
        llm_min_attempt_sec=5,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_http_attempt_timeout_is_clamped_to_remaining_budget(monkeypatch):
    """`requests.post(timeout=...)` must never be handed more time than the stage has left."""
    seen_timeouts: list[float] = []

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        seen_timeouts.append(timeout)
        raise requests.Timeout("stalled")

    monkeypatch.setattr(groq_client.requests, "post", _fake_post)

    with stage_deadline("intake_reasoning", 12):
        with pytest.raises(groq_client.GroqClientError):
            groq_client._post_openai_chat_payload(_http_settings(), {"messages": []}, mode="default")

    assert seen_timeouts, "at least one attempt must have been made"
    # Configured http_timeout is 60s; the stage only ever had 12.
    assert max(seen_timeouts) <= 12, seen_timeouts
    assert all(t > 0 for t in seen_timeouts), seen_timeouts
    # Each attempt sees a smaller window than the last -- the budget is genuinely shared.
    assert seen_timeouts == sorted(seen_timeouts, reverse=True), seen_timeouts


def test_http_retries_stop_when_the_budget_can_no_longer_fund_an_attempt(monkeypatch):
    """Bounded terminal failure, not the configured 4 attempts regardless of the clock."""
    attempts: list[float] = []

    def _fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        attempts.append(timeout)
        time.sleep(min(0.4, timeout))
        raise requests.Timeout("stalled")

    monkeypatch.setattr(groq_client.requests, "post", _fake_post)
    settings = _http_settings(http_timeout=5, http_retry_base_delay=0.05, llm_min_attempt_sec=1)

    with stage_deadline("intake_reasoning", 2):
        with pytest.raises(groq_client.GroqClientError):
            groq_client._post_openai_chat_payload(settings, {"messages": []}, mode="default")

    assert 0 < len(attempts) < 4, f"retries must stop on budget, not run the full loop: {attempts}"


def test_http_first_attempt_with_no_budget_is_reported_as_deadline_exhausted(monkeypatch):
    def _fake_post(*_args, **_kwargs):
        pytest.fail("no HTTP attempt may start with an exhausted budget")

    monkeypatch.setattr(groq_client.requests, "post", _fake_post)

    with stage_deadline("intake_reasoning", 0.0):
        with pytest.raises(DeadlineExhausted) as caught:
            groq_client._post_openai_chat_payload(_http_settings(), {"messages": []}, mode="default")

    assert caught.value.details["error_class"] == "deadline_exhausted"
    assert caught.value.details["provider"] == "openai_chat"


def test_backoff_never_sleeps_away_the_window_its_retry_needs():
    """A long Retry-After must not silently convert 'we will retry' into 'we will time out'."""
    slept: list[float] = []
    settings = _http_settings(http_retry_base_delay=300.0)

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "600"}

    with mock.patch.object(groq_client.time, "sleep", side_effect=slept.append):
        with stage_deadline("intake_reasoning", 20):
            groq_client._sleep_before_retry(settings, 1, reason="http-429", response=_Resp(), retry_events=[])

    assert slept, "a retry sleep should still occur"
    # 20s budget minus the 5s an attempt needs leaves at most 15s of legitimate backoff.
    assert slept[0] <= 15, slept


def test_case_f_budget_helpers_are_inert_outside_a_stage():
    """Scripts and one-off tools that never open a stage keep their previous behavior."""
    assert current_deadline() is None
    assert retry_window_sec() is None
    assert attempt_timeout_sec(60) == 60
    assert provider_budget_sec(providers_remaining=3) is None
