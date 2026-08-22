"""P1.4: bounded production-faithful multi-intent runtime slice.

Real seams used:
  - build_case_understanding_projection -> snapshot.case_understanding with
    customer_intents (production wiring);
  - generate_draft_reply handler (deterministic multi-intent composer);
  - draft sanity with intent coverage (MULTI_INTENT_DROPPED etc.);
  - single-intent regression stays on the legacy deterministic path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import build_case_understanding_projection
from agent_runtime.draft_sanity import evaluate_draft_sanity
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools.handlers import generate_draft_reply
from llm_contracts.engagement_snapshot_v2 import CaseUnderstandingProjection
from mailbox_memory import InMemoryMailboxMemoryStore


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=2,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=1,
    )


def _three_intent_understanding(case_id: str = "case_p1_4", signal_id: str = "sig_p1_4") -> dict:
    """Simulate understanding_output carrying a 3-intent projection."""
    return {
        "source_signal_id": signal_id,
        "situation_summary_pl": "Klient zgłasza awarię, przegląd i prośbę o fakturę.",
        "operator_explanation": {
            "essence_pl": "Wielotematyczna wiadomość serwisowa.",
            "customer_intent_pl": "obsługa serwisowa",
        },
        "created_at": "2026-08-22T16:00:00Z",
        "missing_critical_fields": ["exact_symptoms", "device_model"],
        "customer_intents": [
            {
                "intent_type": "service_problem",
                "description": "Pompa H70, brak CWU.",
                "required_information": ["exact_symptoms", "device_model"],
            },
            {
                "intent_type": "schedule_service",
                "description": "Przegląd w przyszłym tygodniu.",
                "required_information": ["device_model", "preferred_service_date"],
            },
            {
                "intent_type": "document_request",
                "description": "Kopia ostatniej faktury.",
                "required_information": ["invoice_period"],
            },
        ],
        "risks": [],
        "case_understanding": {"case_family": "awaria_naprawa"},
        "situation_summary": {"case_family": "awaria_naprawa", "business_area": "service"},
    }


def _snapshot(*, with_intents: bool = True):
    from agent_runtime.store import build_initial_snapshot

    snap = build_initial_snapshot(
        case_id="case_p1_4",
        engagement_id="eng_p1_4",
        signal_id="sig_p1_4",
        trace_id="t_p1_4",
    )
    understanding = _three_intent_understanding()
    projection = build_case_understanding_projection(
        {"understanding_output": understanding},
        message_id="sig_p1_4",
    )
    assert projection is not None
    if not with_intents:
        projection.pop("customer_intents", None)
    return snap.model_copy(
        update={
            "case_kind": "awaria_naprawa",
            "case_understanding": CaseUnderstandingProjection(**projection),
        }
    )


def _ctx(snapshot) -> ToolExecutionContext:
    from agent_runtime.constitution import load_constitution

    return ToolExecutionContext.from_snapshot(
        snapshot,
        settings=_settings(),
        mailbox_store=InMemoryMailboxMemoryStore(),
        signal_payload={"harness_mode": True},
        constitution=load_constitution(),
    )


def _plan() -> ToolCallPlan:
    return ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        semantic_hash="sh_test",
    )


def test_worker_projection_wiring_carries_customer_intents() -> None:
    projection = build_case_understanding_projection(
        {"understanding_output": _three_intent_understanding()},
        message_id="sig_p1_4",
    )
    assert projection is not None
    assert len(projection["customer_intents"]) == 3
    assert projection["customer_intents"][0]["intent_type"] == "service_problem"


def test_positive_multi_intent_trajectory_is_hitl_ready() -> None:
    ctx = _ctx(_snapshot(with_intents=True))
    result = generate_draft_reply(_plan(), ctx)
    assert result.status == "ok"
    delta = result.snapshot_delta or {}
    actions = delta.get("actions") or []
    assert actions and actions[0]["enabled"] is True
    coverage = actions[0].get("intent_coverage") or {}
    assert coverage.get("intent_ids")
    assert coverage["intent_ids"] == coverage["covered_intent_ids"]
    assert not coverage["ignored_intent_ids"]
    assert "umówienie przeglądu" in actions[0]["payload_pl"]
    assert "kopii faktury" in actions[0]["payload_pl"]
    assert (delta.get("hitl_gate") or {}).get("required") is True


def test_single_intent_regression_uses_legacy_deterministic_path() -> None:
    ctx = _ctx(_snapshot(with_intents=False))
    result = generate_draft_reply(_plan(), ctx)
    assert result.status == "ok"
    actions = (result.snapshot_delta or {}).get("actions") or []
    assert actions and actions[0]["enabled"] is True
    # No intent_coverage on the legacy single-intent path.
    assert not actions[0].get("intent_coverage")


def test_dropped_intent_fails_closed_at_sanity() -> None:
    ctx = _ctx(_snapshot(with_intents=True))
    result = generate_draft_reply(_plan(), ctx)
    assert result.status == "ok"
    actions = (result.snapshot_delta or {}).get("actions") or []
    coverage = dict(actions[0]["intent_coverage"])
    coverage["ignored_intent_ids"] = [coverage["intent_ids"][-1]]
    body = actions[0]["payload_pl"]
    sanity = evaluate_draft_sanity(
        body=body,
        case_kind="awaria_naprawa",
        intent="missing_info",
        intent_coverage=coverage,
    )
    assert sanity["ok"] is False
    assert "MULTI_INTENT_DROPPED" in sanity["reason_codes"]


def test_execution_assertion_fails_closed_at_sanity() -> None:
    ctx = _ctx(_snapshot(with_intents=True))
    result = generate_draft_reply(_plan(), ctx)
    assert result.status == "ok"
    actions = (result.snapshot_delta or {}).get("actions") or []
    body = actions[0]["payload_pl"] + "\nWizyta została umówiona na poniedziałek."
    sanity = evaluate_draft_sanity(
        body=body,
        case_kind="awaria_naprawa",
        intent="missing_info",
        intent_coverage=actions[0]["intent_coverage"],
    )
    assert sanity["ok"] is False
    assert "INTENT_EXECUTION_ASSERTED_WITHOUT_EVIDENCE" in sanity["reason_codes"]
