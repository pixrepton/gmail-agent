"""CL-02: the Anthropic path must not abandon work it cannot cancel.

`central_llm_stage._call_with_retry` used to run every attempt as:

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        result = future.result(timeout=hard_timeout)      # hard_timeout = client_timeout + 30
        ...
        except concurrent.futures.TimeoutError:
            future.cancel()

Two independent problems, both mechanical:

1. `future.cancel()` cannot stop a request already in flight, so the work was abandoned.
2. `Executor.__exit__` calls `shutdown(wait=True)`, so the block then **waited for that
   abandoned work anyway**. The "hard timeout" therefore bounded nothing at all -- it only
   changed which exception was recorded, while burning a thread per attempt.

On top of that the callee ran its own retry loop (`max_retries=min(3, http_max_retries)`), so one
logical call could expand to 4 x 3 HTTP attempts with nothing observing the multiplication.

The path is inactive in this deployment (`anthropic_api_key` unset), which is not a reason to
leave a proven defect in place. Everything below is deterministic; no provider is contacted.
"""

from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import central_llm_stage as cls  # noqa: E402
from exceptions import LLMError, LLMRateLimitError, LLMTimeoutError  # noqa: E402
from llm_deadline import stage_deadline  # noqa: E402


# ── structural: the wrapper that could not cancel is gone ─────────────────────────────


def test_call_with_retry_no_longer_uses_an_executor():
    tree = ast.parse((TOOL_DIR / "central_llm_stage.py").read_text(encoding="utf-8"))
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_call_with_retry"
    )
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(func) if isinstance(n, ast.Call)
    }
    assert "ThreadPoolExecutor" not in called
    assert "submit" not in called
    assert "cancel" not in called


def test_hard_timeout_parameter_is_gone():
    """A parameter that bounds nothing is worse than no parameter: it implies a guarantee."""
    import inspect

    assert "hard_timeout" not in inspect.signature(cls._call_with_retry).parameters


def test_anthropic_client_does_not_run_a_second_retry_loop():
    """Retry belongs to _call_with_retry; the client must do one attempt per call."""
    import inspect

    assert inspect.signature(cls._anthropic_client).parameters["max_retries"].default == 1


# ── CASE A: fast success ──────────────────────────────────────────────────────────────


def test_fast_success_returns_without_retrying():
    calls = []

    def _ok():
        calls.append(1)
        return "ok"

    with stage_deadline("anthropic_stage", 30):
        assert cls._call_with_retry(_ok, stage_name="s", model="m") == "ok"
    assert len(calls) == 1


# ── CASE B: fast retryable failure then success ───────────────────────────────────────


def test_retryable_failure_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(cls.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise LLMRateLimitError("429", context={})
        return "recovered"

    with stage_deadline("anthropic_stage", 60):
        assert cls._call_with_retry(_flaky, stage_name="s", model="m") == "recovered"
    assert len(calls) == 3


def test_non_retryable_llm_error_propagates_immediately():
    calls: list[int] = []

    def _permanent():
        calls.append(1)
        raise LLMError("401 unauthorized")

    with stage_deadline("anthropic_stage", 60):
        with pytest.raises(LLMError):
            cls._call_with_retry(_permanent, stage_name="s", model="m")
    assert len(calls) == 1, "a permanent error must not be retried"


# ── CASE C/D: timeout, then bounded terminal failure ──────────────────────────────────


def test_timeout_is_retried_and_then_fails_terminally(monkeypatch):
    monkeypatch.setattr(cls.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def _always_times_out():
        calls.append(1)
        raise LLMTimeoutError("attempt timed out", context={})

    with stage_deadline("anthropic_stage", 60):
        with pytest.raises(LLMTimeoutError):
            cls._call_with_retry(_always_times_out, stage_name="s", model="m", max_retries=2)
    assert len(calls) == 3, "bounded: initial attempt + max_retries, then terminal"


# ── CASE E: deadline exhaustion stops the loop ────────────────────────────────────────


def test_retries_stop_when_the_budget_cannot_fund_an_attempt(monkeypatch):
    monkeypatch.setattr(cls.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def _fails():
        calls.append(1)
        raise LLMRateLimitError("429", context={})

    # min_attempt defaults to 5s; a 1s budget can never fund an attempt.
    with stage_deadline("anthropic_stage", 1.0):
        with pytest.raises(LLMTimeoutError) as caught:
            cls._call_with_retry(_fails, stage_name="s", model="m", max_retries=5)
    assert calls == [], "no attempt may start below the minimum useful window"
    # Terminal result must be structured and attributable, not "unknown reason".
    context = getattr(caught.value, "context", {}) or {}
    assert context.get("terminal_failure_reason") == "stage_deadline_exhausted"
    assert "budget exhausted" in str(caught.value).lower()


def test_anthropic_attempt_refuses_to_start_with_no_budget():
    settings = SimpleNamespace(
        anthropic_api_key="", anthropic_model="claude", http_timeout=60, http_max_retries=4,
    )
    with stage_deadline("anthropic_stage", 0.0):
        with pytest.raises(LLMTimeoutError):
            cls._call_anthropic_raw_text(
                settings, system="s", user_json="{}", case_id=None, model=None, temperature=0,
            )


# ── CASE F: no orphan work ────────────────────────────────────────────────────────────


def test_no_orphan_work_survives_the_call():
    """When _call_with_retry returns, no injected work may still be running."""
    in_flight = 0
    lock = threading.Lock()

    def _slow():
        nonlocal in_flight
        with lock:
            in_flight += 1
        try:
            time.sleep(0.2)
            return "done"
        finally:
            with lock:
                in_flight -= 1

    threads_before = threading.active_count()
    with stage_deadline("anthropic_stage", 30):
        assert cls._call_with_retry(_slow, stage_name="s", model="m") == "done"

    with lock:
        assert in_flight == 0
    assert threading.active_count() <= threads_before, "no worker threads may be left behind"


def test_provider_semaphore_is_released_on_failure():
    """The semaphore must not leak when an attempt raises — it gates real concurrency."""
    sem = cls._get_provider_semaphore("anthropic")
    before = sem._value  # noqa: SLF001 - asserting the guard itself

    def _boom():
        raise LLMError("permanent")

    with stage_deadline("anthropic_stage", 30):
        with pytest.raises(LLMError):
            cls._call_with_retry(_boom, stage_name="s", model="m", provider="anthropic")

    assert sem._value == before  # noqa: SLF001
