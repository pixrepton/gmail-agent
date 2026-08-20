"""P1.4A: deterministic kalk-top eligibility + handler fail-closed + no fabrication.

Contract: KEEP_AND_RESTRICT. Business eligibility and technical readiness are
separate layers; the five Fresh38 residual cases must be blocked by the general
contract (no case-ID logic), eligible quote cases must still see the tool, and
an ineligible direct invocation must never reach the HTTP client.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST, MAIL_AGENT_TOOL_BUDGET
from agent_runtime.decision_divergence import build_decision_comparison_inputs
from agent_runtime.effective_tools import compute_effective_available_tools
from agent_runtime.kalk_eligibility import (
    decision_from_snapshot,
    evaluate_business_eligibility,
    evaluate_technical_readiness,
)
from agent_runtime.kalk_top_client import build_calc_request_from_profile
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import AgentToolRegistry
from llm_contracts.engagement_snapshot_v2 import HvacProfile, UnderstandingRiskItem


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="http://127.0.0.1:8091",
        kalk_top_agent_key="test",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=1,
    )


def _snapshot(
    *,
    case_kind: str = "wycena_oferta",
    heated_area_m2: int | None = 150,
    risks: list[UnderstandingRiskItem] | None = None,
) -> object:
    from llm_contracts.engagement_snapshot_v2 import CaseUnderstandingProjection

    snapshot = build_initial_snapshot(
        case_id="case-elig",
        engagement_id="eng-elig",
        trace_id="trace-elig",
    )
    understanding = None
    if risks:
        understanding = CaseUnderstandingProjection(risks=risks)
    return snapshot.model_copy(
        update={
            "case_kind": case_kind,
            "hvac_profile": HvacProfile(heated_area_m2=heated_area_m2),
            "case_understanding": understanding,
        }
    )


def _context(business_recommended_action: str) -> dict[str, object]:
    return {
        "schema_version": "decision_comparison_inputs.v1",
        "business_recommended_action": business_recommended_action,
        "action_planner_primary_action": "prepare_reply",
        "next_best_action_type": "answer_customer",
        "reply_draft_enabled": True,
        "case_family": "lead_opportunity",
    }


def _conflicted_area_context() -> dict[str, object]:
    pack = {
        "active_facts": [
            {
                "fact_key": "heated_area_m2",
                "normalized_value": "180",
                "trust_state": "conflicted",
                "decision_usable": False,
                "decision_block_reason": "fact_conflict",
            }
        ],
        "conflicting_facts": [
            {"entity_scope": "building", "fact_key": "heated_area_m2", "values": ["180", "220"]}
        ],
    }
    context = build_decision_comparison_inputs(
        {
            "understanding_output": {"source_signal_id": "msg-conflict"},
            "execution_metadata": {
                "input_business_next_action": "reply",
                "input_primary_action": "prepare_reply",
                "input_reply_draft_enabled": True,
            },
            "case_understanding": {"case_family": "lead_opportunity"},
            "next_best_action": {"primary_next_action": {"action_type": "answer_customer"}},
            "mailbox_memory_context_pack": pack,
        },
        message_id="msg-conflict",
    )
    assert context is not None
    return context


def _effective(snapshot: object, decision_context: dict[str, object] | None) -> tuple[str, ...]:
    constitution = load_constitution()
    assert "call_kalk_top_quote" in constitution.tool_allowlist
    result = compute_effective_available_tools(
        tuple(MAIL_AGENT_TOOL_ALLOWLIST),
        constitution=constitution,
        settings=_settings(),
        snapshot=snapshot,
        decision_context=decision_context,
    )
    return result.offered


def test_positive_eligible_case_offers_kalk() -> None:
    snapshot = _snapshot()
    decision = decision_from_snapshot(snapshot, decision_context=_context("reply"))
    assert decision.offered is True
    assert decision.business_eligible is True
    assert decision.technically_ready is True
    offered = _effective(snapshot, _context("reply"))
    assert "call_kalk_top_quote" in offered
    # unrelated tools unchanged
    for tool in ("search_gmail_thread", "generate_draft_reply", "request_operator_clarification"):
        assert tool in offered


def test_conflicted_area_blocks_kalk_but_not_independent_action() -> None:
    snapshot = _snapshot(case_kind="wycena_oferta", heated_area_m2=180)
    context = _conflicted_area_context()

    assert context["decision_fact_blocks"]["call_kalk_top_quote"] == {
        "blocked": True,
        "blocked_fact_keys": ["heated_area_m2"],
        "decision_block_reason": "fact_conflict",
    }
    assert context["decision_fact_blocks"]["acknowledge_documents"] == {
        "blocked": False,
        "blocked_fact_keys": [],
        "decision_block_reason": None,
    }

    decision = decision_from_snapshot(snapshot, decision_context=context)
    assert decision.offered is False
    assert decision.business_eligible is True
    assert decision.technically_ready is False
    assert "decision_fact_conflict:heated_area_m2" in decision.reasons

    offered = _effective(snapshot, context)
    assert "call_kalk_top_quote" not in offered
    assert "generate_draft_reply" in offered


@pytest.mark.parametrize(
    ("case_kind", "business_action", "heated_area_m2", "case_id_note"),
    [
        ("wycena_oferta", "collect_data", 90, "INT-05 pattern: business not quote-ready"),
        ("zapytanie_klienta", "collect_data", None, "DOC-02 pattern: technical question, no quote intent"),
        ("wycena_oferta", "collect_data", 150, "NEW-03 pattern: business not quote-ready"),
        ("wycena_oferta", "escalate_review", 150, "INT-01 pattern: escalate_review"),
        ("wycena_oferta", "escalate_review", 160, "CTX-03 pattern: escalate_review"),
        ("awaria_naprawa", "escalate_review", None, "complaint/service: journey not quote permitted"),
        ("wycena_oferta", "wait", 150, "follow-up-only: wait is not quote-ready"),
    ],
)
def test_five_historical_cases_blocked_by_general_contract(
    case_kind: str,
    business_action: str,
    heated_area_m2: int | None,
    case_id_note: str,
) -> None:
    # Business state derived from the 2026-08-16 frozen capture artifacts
    # (understanding/business_reasoning), not from case IDs.
    snapshot = _snapshot(case_kind=case_kind, heated_area_m2=heated_area_m2)
    decision = decision_from_snapshot(snapshot, decision_context=_context(business_action))
    assert decision.offered is False, case_id_note
    offered = _effective(snapshot, _context(business_action))
    assert "call_kalk_top_quote" not in offered, case_id_note
    # unrelated tools stay available and budget semantics unchanged
    assert "search_gmail_thread" in offered
    assert MAIL_AGENT_TOOL_BUDGET["call_kalk_top_quote"] == 2


def test_missing_heated_area_blocks_technically() -> None:
    snapshot = _snapshot(heated_area_m2=None)
    decision = decision_from_snapshot(snapshot, decision_context=_context("reply"))
    assert decision.offered is False
    assert decision.business_eligible is True
    assert decision.technically_ready is False
    assert "required_input_missing:heated_area_m2" in decision.reasons


def test_blocking_contradiction_blocks() -> None:
    risks = [
        UnderstandingRiskItem(
            risk_type="contradiction",
            severity="high",
            summary_pl="Sprzeczne dane metrazu 120 vs 160",
        )
    ]
    snapshot = _snapshot(risks=risks)
    decision = decision_from_snapshot(snapshot, decision_context=_context("reply"))
    assert decision.offered is False
    assert any("blocking_contradiction" in r for r in decision.reasons)


def test_non_quote_journey_blocks() -> None:
    snapshot = _snapshot(case_kind="awaria_naprawa")
    decision = decision_from_snapshot(snapshot, decision_context=_context("reply"))
    assert decision.offered is False
    assert any("journey_not_quote_permitted" in r for r in decision.reasons)


def test_technical_readiness_contract() -> None:
    ready, reasons = evaluate_technical_readiness(heated_area_m2=90)
    assert ready is True
    assert reasons == ()
    not_ready, reasons2 = evaluate_technical_readiness(heated_area_m2=None)
    assert not_ready is False
    assert reasons2 == ("required_input_missing:heated_area_m2",)
    not_ready2, _ = evaluate_technical_readiness(heated_area_m2="abc")
    assert not_ready2 is False


def test_business_eligibility_contract() -> None:
    ok, reasons = evaluate_business_eligibility(
        case_kind="wycena_oferta", business_recommended_action="reply"
    )
    assert ok is True and reasons == ()
    unknown, unknown_reasons = evaluate_business_eligibility(case_kind="wycena_oferta")
    assert unknown is False
    assert "business_readiness_unknown" in unknown_reasons
    blocked, reasons2 = evaluate_business_eligibility(
        case_kind="wycena_oferta", business_recommended_action="collect_data"
    )
    assert blocked is False
    assert "business_not_quote_ready:collect_data" in reasons2
    blocked2, reasons3 = evaluate_business_eligibility(case_kind="zapytanie_klienta")
    assert blocked2 is False
    assert any("quote_intent_missing" in r for r in reasons3)
    blocked3, reasons4 = evaluate_business_eligibility(
        case_kind="wycena_oferta", business_recommended_action="wait"
    )
    assert blocked3 is False
    assert "business_not_quote_ready:wait" in reasons4


def test_handler_fail_closed_zero_http(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _SpyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _SpyClient:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, *args: object, **kwargs: object) -> object:
            calls["n"] += 1
            raise AssertionError("HTTP client must not be called for ineligible case")

    monkeypatch.setattr(httpx, "Client", _SpyClient)
    snapshot = _snapshot(case_kind="wycena_oferta", heated_area_m2=90)
    context = ToolExecutionContext.from_snapshot(
        snapshot,
        settings=_settings(),
        signal_payload={"decision_comparison_inputs": _context("collect_data")},
    )
    result = AgentToolRegistry().execute(
        ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
        context=context,
    )
    assert result.status == "error"
    assert result.failure_class == "KALK_TOP_NOT_ELIGIBLE"
    assert result.retryable is False
    assert calls["n"] == 0


def test_handler_fail_closed_conflicted_decision_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _SpyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _SpyClient:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, *args: object, **kwargs: object) -> object:
            calls["n"] += 1
            raise AssertionError("HTTP client must not be called for conflicted decision fact")

    monkeypatch.setattr(httpx, "Client", _SpyClient)
    context = ToolExecutionContext.from_snapshot(
        _snapshot(case_kind="wycena_oferta", heated_area_m2=180),
        settings=_settings(),
        signal_payload={"decision_comparison_inputs": _conflicted_area_context()},
    )
    result = AgentToolRegistry().execute(
        ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
        context=context,
    )

    assert result.status == "error"
    assert result.failure_class == "KALK_TOP_NOT_ELIGIBLE"
    assert result.retryable is False
    detail = (result.snapshot_delta.get("execution_attribution") or {}).get("detail")
    assert "decision_fact_conflict:heated_area_m2" in str(detail)
    assert calls["n"] == 0


def test_handler_fail_closed_missing_case_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _SpyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _SpyClient:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, *args: object, **kwargs: object) -> object:
            calls["n"] += 1
            raise AssertionError("HTTP client must not be called without case identity")

    monkeypatch.setattr(httpx, "Client", _SpyClient)
    snapshot = _snapshot().model_copy(update={"case_id": ""})
    context = ToolExecutionContext.from_snapshot(
        snapshot,
        settings=_settings(),
        signal_payload={"decision_comparison_inputs": _context("reply")},
    )
    result = AgentToolRegistry().execute(
        ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
        context=context,
    )
    assert result.status == "error"
    assert result.failure_class == "KALK_TOP_NOT_ELIGIBLE"
    assert calls["n"] == 0


def test_anti_overrestriction_boundary_cohort() -> None:
    """KEEP_AND_RESTRICT: block overuse without converting the tool into REMOVE.

    Authoritative quote-intent signal in the current case model is CaseKindLiteral:
    `wycena_oferta` = quote/offer request; `zapytanie_klienta` = pre-offer inquiry.
    There is no second quote-intent field. Scenario C is therefore a documented
    model limitation, not a new ontology.
    """
    # A. wycena_oferta + ready -> YES
    snapshot_a = _snapshot(case_kind="wycena_oferta", heated_area_m2=150)
    assert decision_from_snapshot(snapshot_a, decision_context=_context("reply")).offered is True
    assert "call_kalk_top_quote" in _effective(snapshot_a, _context("reply"))

    # B. zapytanie_klienta + pure technical question -> NO
    snapshot_b = _snapshot(case_kind="zapytanie_klienta", heated_area_m2=120)
    decision_b = decision_from_snapshot(snapshot_b, decision_context=_context("reply"))
    assert decision_b.offered is False
    assert any("quote_intent_missing" in r for r in decision_b.reasons)

    # C. zapytanie_klienta + BR clearly requests calculation/offer
    # Current contract cannot express this without changing CaseKindLiteral.
    snapshot_c = _snapshot(case_kind="zapytanie_klienta", heated_area_m2=150)
    decision_c = decision_from_snapshot(snapshot_c, decision_context=_context("reply"))
    assert decision_c.offered is False
    assert any("quote_intent_missing" in r for r in decision_c.reasons)

    # D. wycena_oferta + collect_data -> NO
    snapshot_d = _snapshot(case_kind="wycena_oferta", heated_area_m2=150)
    decision_d = decision_from_snapshot(snapshot_d, decision_context=_context("collect_data"))
    assert decision_d.offered is False
    assert "business_not_quote_ready:collect_data" in decision_d.reasons

    # E. wycena_oferta + technically missing heated area -> NO
    snapshot_e = _snapshot(case_kind="wycena_oferta", heated_area_m2=None)
    decision_e = decision_from_snapshot(snapshot_e, decision_context=_context("reply"))
    assert decision_e.offered is False
    assert decision_e.business_eligible is True
    assert "required_input_missing:heated_area_m2" in decision_e.reasons

    # F. quote-ready + unrelated non-blocking information gap -> YES
    snapshot_f = _snapshot(case_kind="wycena_oferta", heated_area_m2=150)
    decision_f = decision_from_snapshot(snapshot_f, decision_context=_context("reply"))
    assert decision_f.offered is True
    payload = build_calc_request_from_profile(snapshot_f.model_dump(mode="python"))
    assert "city" not in payload["building"]


def test_low_severity_contradiction_does_not_remove_kalk() -> None:
    risks = [
        UnderstandingRiskItem(
            risk_type="contradiction",
            severity="low",
            summary_pl="Drobna niespójność adresu",
        )
    ]
    snapshot = _snapshot(risks=risks)
    decision = decision_from_snapshot(snapshot, decision_context=_context("reply"))
    assert decision.offered is True


def test_unrelated_high_conflict_fail_closed_without_relevance_metadata() -> None:
    risks = [
        UnderstandingRiskItem(
            risk_type="conflict",
            severity="high",
            summary_pl="Konflikt niezwiązany z metrażem",
        )
    ]
    snapshot = _snapshot(risks=risks)
    decision = decision_from_snapshot(snapshot, decision_context=_context("reply"))
    assert decision.offered is False
    assert any("blocking_contradiction" in r for r in decision.reasons)


def test_payload_has_no_fabricated_defaults() -> None:
    payload = build_calc_request_from_profile(
        {
            "trace_id": "t1",
            "hvac_profile": {"heated_area_m2": 150, "location": {"city": "Radlin"}},
        }
    )
    assert payload["building"]["heated_area"] == 150
    assert "building_type" not in payload["building"]
    assert payload["preferences"]["dhw"] == {}
    assert "persons" not in payload["preferences"]["dhw"]

    payload2 = build_calc_request_from_profile(
        {
            "trace_id": "t2",
            "hvac_profile": {
                "heated_area_m2": 150,
                "building_type": "single_family",
                "dhw_persons": 4,
                "location": {"city": "Radlin", "postal_code": "41-100"},
            },
        }
    )
    assert payload2["building"]["building_type"] == "single_family"
    assert payload2["preferences"]["dhw"] == {"enabled": True, "persons": 4}
    assert payload2["building"]["city"] == "Radlin"
    assert payload2["building"]["postal_code"] == "41-100"


def test_graph_replay_ineligible_case_zero_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic engine replay: planner tries kalk-top, gate blocks pre-planner."""
    calls = {"n": 0}

    class _SpyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _SpyClient:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, *args: object, **kwargs: object) -> object:
            calls["n"] += 1
            raise AssertionError("HTTP client must not be called for ineligible case")

    monkeypatch.setattr(httpx, "Client", _SpyClient)

    from agent_runtime.graph import AgentGraphEngine
    from agent_runtime.planner import MockSequencePlanner
    from agent_runtime.turn_journal import InMemoryAgentTurnJournal

    snapshot = _snapshot(case_kind="wycena_oferta", heated_area_m2=90)
    context = ToolExecutionContext.from_snapshot(
        snapshot,
        settings=_settings(),
        signal_payload={"decision_comparison_inputs": _context("collect_data")},
    )
    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["call_kalk_top_quote"]),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
        turn_journal=InMemoryAgentTurnJournal(),
    )
    result = engine.run(snapshot, context=context)

    assert calls["n"] == 0
    assert result.snapshot.hitl_gate.required is True
    # The deterministic mock planner cannot select kalk-top (it was filtered from
    # the offered set), so the engine converges to an attributable planner error
    # with HITL required — and the HTTP client was never reached.
    assert (
        "tool_not_offered:call_kalk_top_quote" in result.snapshot.hitl_gate.reason
        or "planner_error" in result.snapshot.hitl_gate.reason
    )
