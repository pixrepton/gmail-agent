"""CL-01: the agent planner's timeout must bound the mechanism it wraps, not race it.

The previous verdict on `openai_agent_client._call_llm_with_timeout` was "30s inner < 45s outer,
correctly nested". That was wrong, and these tests pin why.

`LLM_CLIENT_TIMEOUT_SEC = 30` was handed to `client.chat.completions.create(timeout=...)`. The
OpenAI SDK (1.109.1) defaults to `max_retries=2` and applies `timeout` **per attempt**, retrying
inside a single `create()` call:

    for retries_taken in range(max_retries + 1):   # 3 attempts
        request = self._build_request(options, ...)   # options.timeout each time

So one `create()` could legitimately need 3 x 30s plus backoff -- roughly 90s+ -- while the outer
`ThreadPoolExecutor(...).result(timeout=45)` fired at 45s. The outer envelope was *shorter than*
the inner mechanism, exactly the RT01 defect class. It also never called `cancel()`, so the
abandoned request kept running and kept holding one of only two slots in a shared pool, letting
orphaned work block later planner calls at the queue.

The repair follows the existing stage-deadline model rather than enlarging a constant.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime import openai_agent_client as oac  # noqa: E402
from llm_deadline import attempt_timeout_sec, stage_deadline  # noqa: E402


# ── the budget contract ───────────────────────────────────────────────────────────────


def test_planner_budget_exceeds_a_single_attempt_and_is_derived_from_it():
    """A budget at or below one attempt reproduces the defect being removed."""
    assert oac.AGENT_PLANNER_BUDGET_SEC == 3 * oac.LLM_CLIENT_TIMEOUT_SEC
    assert oac.AGENT_PLANNER_BUDGET_SEC > oac.LLM_CLIENT_TIMEOUT_SEC


def test_old_hard_kill_constant_is_gone():
    """`LLM_TIMEOUT_SEC = 45` was the outer kill that preempted the SDK's own retry."""
    assert not hasattr(oac, "LLM_TIMEOUT_SEC")


def test_module_no_longer_wraps_the_call_in_an_executor():
    """Structural: no wrapper thread means nothing can be abandoned mid-flight.

    Checked on the AST, not the text, so the prose explaining the old design cannot satisfy
    or break the assertion.
    """
    import ast

    tree = ast.parse((TOOL_DIR / "agent_runtime" / "openai_agent_client.py").read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)

    assert "ThreadPoolExecutor" not in called, "the planner must not create a wrapper thread pool"
    assert "submit" not in called, "no work may be handed to an executor it cannot cancel"
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "ThreadPoolExecutor" not in imported


# ── SDK-internal retry must not be a second, invisible layer ──────────────────────────


class _FakeSDKClient:
    """Stands in for a real `openai` client, including `with_options`."""

    __module__ = "openai._client"

    def __init__(self) -> None:
        self.options: dict = {}
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(tool_calls=None, content="{}"))],
                    usage=SimpleNamespace(total_tokens=1),
                )

        self.chat = SimpleNamespace(completions=_Completions())

    def with_options(self, **kwargs):
        self.options = dict(kwargs)
        return self


def test_real_sdk_client_is_re_optioned_with_retries_disabled():
    client = _FakeSDKClient()
    with stage_deadline("agent_planner", 90):
        oac._call_llm_with_timeout(
            client=client, model="m", messages=[], tools=[], provider_name="p",
        )
    assert client.options["max_retries"] == 0, "the SDK must do exactly one attempt per endpoint"
    assert client.options["timeout"] <= 30


def test_injected_test_double_is_not_re_optioned():
    """A MagicMock would auto-create with_options() and silently detach the call."""
    from unittest.mock import MagicMock

    mock = MagicMock()
    assert oac._bounded_client(mock, 10) is mock


# ── per-attempt timeout is clamped by the remaining budget ────────────────────────────


def test_attempt_timeout_is_clamped_by_remaining_budget():
    client = _FakeSDKClient()
    with stage_deadline("agent_planner", 4):
        oac._call_llm_with_timeout(
            client=client, model="m", messages=[], tools=[], provider_name="p",
        )
    assert client.calls[0]["timeout"] <= 4, client.calls[0]["timeout"]


def test_attempt_refuses_to_start_with_no_budget_left():
    client = _FakeSDKClient()
    with stage_deadline("agent_planner", 0.0):
        with pytest.raises(oac.LLMTimeoutError):
            oac._call_llm_with_timeout(
                client=client, model="m", messages=[], tools=[], provider_name="p",
            )
    assert client.calls == [], "an attempt with no budget only manufactures a timeout"


def test_outside_a_deadline_the_configured_timeout_is_unchanged():
    """Callers outside a planner call keep their previous behavior."""
    assert attempt_timeout_sec(oac.LLM_CLIENT_TIMEOUT_SEC) == oac.LLM_CLIENT_TIMEOUT_SEC


# ── endpoint chain: a slow endpoint must not eat the fallback's turn ──────────────────


def _endpoint(label: str, base_url: str, model: str = "m"):
    return SimpleNamespace(
        label=label, base_url=base_url, api_key="k", model=model,
        reasoning_effort="", thinking_enabled=False,
    )


def _planner_with_endpoints(monkeypatch, endpoints):
    monkeypatch.setattr(oac, "build_agent_planner_endpoints", lambda _s: endpoints)
    settings = MagicMock(spec=[])
    settings.agent_timeout_seconds = 45
    settings.agent_planner_personality_yaml_path = ""
    settings.openai_api_key = "k"
    settings.openai_base_url = "https://example.invalid/v1"
    return oac.OpenAIToolPlanner(settings=settings, client=MagicMock())


def test_no_orphan_work_survives_a_planner_call(monkeypatch):
    """When the planner returns, no injected endpoint call may still be running.

    This is the property the old wrapper violated: it stopped *waiting* at 45s while the
    request kept going on a shared two-slot pool.
    """
    in_flight = 0
    lock = threading.Lock()

    def _slow_then_fail(**_kwargs):
        nonlocal in_flight
        with lock:
            in_flight += 1
        try:
            time.sleep(0.3)
            raise oac.LLMTimeoutError("endpoint stalled", context={})
        finally:
            with lock:
                in_flight -= 1

    monkeypatch.setattr(oac, "_call_llm_with_timeout", _slow_then_fail)
    planner = _planner_with_endpoints(monkeypatch, [_endpoint("a", "https://a.invalid")])
    threads_before = threading.active_count()

    with pytest.raises(Exception):
        planner.plan_next_tool(
            snapshot=_snapshot(), available_tools=("noop",), constitution=_constitution(),
        )

    with lock:
        assert in_flight == 0, "endpoint work must be finished, not abandoned, when the call returns"
    assert threading.active_count() <= threads_before, "no worker threads may be left behind"


def test_endpoint_budget_share_reserves_the_fallback_turn(monkeypatch):
    seen: list[float] = []

    def _record_budget(**_kwargs):
        from llm_deadline import retry_window_sec

        seen.append(retry_window_sec() or 0.0)
        raise oac.LLMTimeoutError("stalled", context={})

    monkeypatch.setattr(oac, "_call_llm_with_timeout", _record_budget)
    monkeypatch.setattr(oac.time, "sleep", lambda _s: None)
    planner = _planner_with_endpoints(
        monkeypatch,
        [_endpoint("a", "https://a.invalid"), _endpoint("b", "https://b.invalid")],
    )

    with pytest.raises(Exception):
        planner.plan_next_tool(
            snapshot=_snapshot(), available_tools=("noop",), constitution=_constitution(),
        )

    assert len(seen) == 2, "both endpoints must get a turn"
    # Two endpoints -> the first is reserved half the planner budget, not all of it.
    assert 40 <= seen[0] <= 46, seen
    assert seen[1] >= 80, seen


def _snapshot():
    """Minimal structure `_compact_view` needs — same shape the chaos test uses."""
    operational_status = SimpleNamespace(model_dump=lambda: {"code": "raw_inquiry"})
    hvac_profile = SimpleNamespace(model_dump=lambda exclude_none: {})
    return SimpleNamespace(
        case_id="",
        case_kind="lead_opportunity",
        user_instruction="",
        operational_status=operational_status,
        hvac_profile=hvac_profile,
        gaps=[SimpleNamespace(model_dump=lambda: {"label": "test"})],
        actions=[SimpleNamespace(model_dump=lambda: {"tool": "noop"})],
        agent_memory=SimpleNamespace(reasoning_trace=[SimpleNamespace(summary_pl="test step")]),
    )


def _constitution():
    from agent_runtime.constitution import AgentConstitution

    return AgentConstitution(
        hvac_rules="", company_context="", forbidden_actions=(), tool_allowlist=("noop",),
    )
