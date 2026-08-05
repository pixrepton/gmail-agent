"""AI-OS Roadmap 3.2 — Draft lineage residual (Brain1 transfer, no second draft)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.draft_identity import compute_body_hash, compute_draft_id
from agent_runtime.draft_lineage_transport import (
    DraftLineageContractError,
    build_upstream_draft_transport,
    materialize_transferred_draft_action,
    resolve_generate_draft_reply,
    validate_upstream_draft_transport,
)
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.policy_action_spine import annotate_action_parent_refs
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools.handlers import generate_draft_reply
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, HitlGate, OperationalStatus


CASE_ID = "case_aios_32"
SIGNAL_ID = "sig_aios_32"
BRAIN1_BODY = (
    "Dzien dobry,\n\npotwierdzamy otrzymanie zapytania i przygotowujemy odpowiedz.\n\nZespol TOP-INSTAL"
)


def _brain1_reply_result() -> dict:
    return {
        "draft_enabled": True,
        "recommended_variant": "short_operational",
        "drafts": [
            {
                "variant": "short_operational",
                "body": BRAIN1_BODY,
                "subject_suggestion": "Re: zapytanie",
                "goal": "odpowiedz",
            }
        ],
        "do_not_send_reasons": [],
        "requires_manual_edit": False,
    }


def _transport(**overrides: object) -> dict:
    base = build_upstream_draft_transport(
        reply_result=_brain1_reply_result(),
        case_id=CASE_ID,
        source_signal_id=SIGNAL_ID,
    )
    assert base is not None
    base.update(overrides)
    return base


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )


def _snapshot(**kwargs: object) -> EngagementSnapshotV2:
    base = {
        "engagement_id": "eng_aios_32",
        "case_id": CASE_ID,
        "signal_id": SIGNAL_ID,
        "version": 1,
        "trace_id": "trace_aios_32",
        "operational_status": {"code": "enriching", "steps_remaining": 8},
        "hvac_profile": {"location": {"city": "Krakow"}, "heated_area_m2": 120},
        "gaps": [],
        "agent_memory": {
            "reasoning_trace": [],
            "tool_calls": [],
            "constitution_sections_used": [],
        },
        "actions": [],
        "hitl_gate": {"required": False, "reason": ""},
    }
    base.update(kwargs)
    return EngagementSnapshotV2.model_validate(base)


class TestUpstreamTransportContract:
    def test_build_transport_from_brain1_reply(self) -> None:
        transport = _transport()
        assert transport["source"] == "brain1"
        assert transport["body"] == BRAIN1_BODY
        assert transport["body_hash"] == compute_body_hash(BRAIN1_BODY)
        assert transport["draft_id"] == compute_draft_id(
            case_id=CASE_ID, source_signal_id=SIGNAL_ID, action_id="draft_reply"
        )
        assert transport["revision"] == 1

    def test_missing_brain1_draft_returns_none(self) -> None:
        assert (
            build_upstream_draft_transport(
                reply_result={"draft_enabled": False, "drafts": []},
                case_id=CASE_ID,
                source_signal_id=SIGNAL_ID,
            )
            is None
        )

    def test_body_hash_mismatch_is_fail_closed(self) -> None:
        transport = _transport(body_hash="deadbeefdeadbeef")
        with pytest.raises(DraftLineageContractError, match="body_hash_mismatch"):
            validate_upstream_draft_transport(transport)

    def test_incomplete_transport_is_fail_closed(self) -> None:
        transport = _transport()
        transport.pop("draft_id")
        with pytest.raises(DraftLineageContractError, match="incomplete"):
            validate_upstream_draft_transport(transport)


class TestResolveGenerateDraftReply:
    def test_brain1_transport_transfers_without_calling_handler(self) -> None:
        signal = {"upstream_draft_transport": _transport()}

        def _boom(*_args, **_kwargs):
            raise AssertionError("generate_draft_reply must not run when Brain1 draft exists")

        with patch("agent_runtime.tools.handlers.generate_draft_reply", side_effect=_boom):
            result, allow_fallback = resolve_generate_draft_reply(signal)
        assert allow_fallback is False
        assert result is not None and result.status == "ok"
        action = result.snapshot_delta["actions"][0]
        assert action["payload_pl"] == BRAIN1_BODY
        assert action["draft_id"] == _transport()["draft_id"]
        assert action["revision"] == 1
        assert action["body_hash"] == _transport()["body_hash"]

    def test_no_upstream_allows_fallback(self) -> None:
        result, allow_fallback = resolve_generate_draft_reply({})
        assert result is None
        assert allow_fallback is True

    def test_bad_hash_blocks_fallback(self) -> None:
        result, allow_fallback = resolve_generate_draft_reply(
            {"upstream_draft_transport": _transport(body_hash="deadbeefdeadbeef")}
        )
        assert allow_fallback is False
        assert result is not None and result.status == "error"
        assert "draft_lineage_contract" in str(result.snapshot_delta["hitl_gate"]["reason"])

    def test_repeat_transfer_is_idempotent_on_identity(self) -> None:
        signal = {"upstream_draft_transport": _transport()}
        first, _ = resolve_generate_draft_reply(signal)
        second, _ = resolve_generate_draft_reply(signal)
        assert first is not None and second is not None
        a1 = first.snapshot_delta["actions"][0]
        a2 = second.snapshot_delta["actions"][0]
        assert a1["draft_id"] == a2["draft_id"]
        assert a1["revision"] == a2["revision"] == 1
        assert a1["body_hash"] == a2["body_hash"]


class TestBrain2FallbackPath:
    def test_fallback_produces_distinct_body_from_brain1(self) -> None:
        from agent_runtime.tool_context import ToolExecutionContext

        snapshot = _snapshot()
        ctx = ToolExecutionContext.from_snapshot(snapshot)
        plan = ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"})
        result = generate_draft_reply(plan, ctx)
        assert result.status == "ok"
        action = result.snapshot_delta["actions"][0]
        assert action["payload_pl"] != BRAIN1_BODY
        assert action["draft_id"]
        assert action["body_hash"]


class TestHitlLineagePreserved:
    def test_transferred_draft_survives_annotation_to_hitl(self) -> None:
        transport = _transport()
        action = materialize_transferred_draft_action(transport)
        plan = ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"})
        annotated = annotate_action_parent_refs(
            {"actions": [action]},
            plan=plan,
            envelope=None,
        )
        merged = apply_snapshot_delta(_snapshot(), annotated)
        hitl_action = merged.actions[0]
        assert hitl_action.payload_pl == BRAIN1_BODY
        assert hitl_action.draft_id == transport["draft_id"]
        assert hitl_action.body_hash == transport["body_hash"]
        assert hitl_action.revision == 1

    def test_mcp_approve_preserves_transferred_identity(self) -> None:
        store = InMemoryOperatorEngagementStore()
        snapshot = _snapshot(
            hitl_gate=HitlGate(required=True, reason="draft_ready_for_approval"),
            operational_status=OperationalStatus(code="pending_operator", steps_remaining=4),
        )
        transport = _transport()
        action = materialize_transferred_draft_action(transport)
        snapshot = apply_snapshot_delta(
            snapshot,
            {
                "actions": [action],
                "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            },
        )
        store.insert_snapshot(snapshot)
        service = AgentMcpService(store=store, settings=_settings())
        out = service.approve_hitl_action(
            engagement_id=snapshot.engagement_id,
            action_id="draft_reply",
            expected_body_hash=transport["body_hash"],
            operator_id="op_test",
        )
        assert out.get("ok") is True
        saved = store.load_snapshot(snapshot.engagement_id)
        assert saved is not None
        row = next(a for a in saved.actions if a.id == "draft_reply")
        assert row.draft_id == transport["draft_id"]
        assert row.body_hash == transport["body_hash"]


class TestDraftLineageProvenancePersistence:
    def test_brain1_transport_persists_draft_origin(self) -> None:
        transport = _transport()
        result = resolve_generate_draft_reply({"upstream_draft_transport": transport})
        assert result[0] is not None
        provenance = result[0].snapshot_delta.get("draft_lineage_provenance") or {}
        assert provenance.get("draft_origin") == "brain1"
        assert provenance.get("origin_correlation_id") == SIGNAL_ID

    def test_fallback_handler_persists_brain2_origin(self) -> None:
        from agent_runtime.tool_context import ToolExecutionContext

        snapshot = _snapshot()
        ctx = ToolExecutionContext.from_snapshot(snapshot)
        plan = ToolCallPlan(tool_name="generate_draft_reply", arguments={"intent": "quote"})
        result = generate_draft_reply(plan, ctx)
        provenance = result.snapshot_delta.get("draft_lineage_provenance") or {}
        assert provenance.get("draft_origin") == "brain2_fallback"
        assert provenance.get("origin_producer") == "generate_draft_reply"

    def test_hitl_reload_preserves_brain1_origin(self) -> None:
        store = InMemoryOperatorEngagementStore()
        transport = _transport()
        snapshot = apply_snapshot_delta(
            _snapshot(
                hitl_gate=HitlGate(required=True, reason="draft_ready_for_approval"),
                operational_status=OperationalStatus(code="pending_operator", steps_remaining=4),
            ),
            resolve_generate_draft_reply({"upstream_draft_transport": transport})[0].snapshot_delta,
        )
        store.insert_snapshot(snapshot)
        loaded = store.load_snapshot(snapshot.engagement_id)
        assert loaded is not None
        assert loaded.draft_lineage_provenance is not None
        assert loaded.draft_lineage_provenance.draft_origin == "brain1"

    def test_approve_copies_origin_to_communication_receipt(self) -> None:
        store = InMemoryOperatorEngagementStore()
        transport = _transport()
        snapshot = apply_snapshot_delta(
            _snapshot(
                hitl_gate=HitlGate(required=True, reason="draft_ready_for_approval"),
                operational_status=OperationalStatus(code="pending_operator", steps_remaining=4),
            ),
            resolve_generate_draft_reply({"upstream_draft_transport": transport})[0].snapshot_delta,
        )
        store.insert_snapshot(snapshot)
        service = AgentMcpService(store=store, settings=_settings())
        out = service.approve_hitl_action(
            engagement_id=snapshot.engagement_id,
            action_id="draft_reply",
            expected_body_hash=transport["body_hash"],
            operator_id="op_test",
        )
        assert out.get("ok") is True
        saved = store.load_snapshot(snapshot.engagement_id)
        assert saved is not None
        assert saved.communication_receipt is not None
        assert saved.communication_receipt.draft_origin == "brain1"

    def test_legacy_snapshot_without_provenance_still_loads(self) -> None:
        snapshot = _snapshot()
        loaded = EngagementSnapshotV2.model_validate(snapshot.model_dump())
        assert loaded.draft_lineage_provenance is None


class TestGraphIntercept:
    def test_graph_never_invokes_handler_when_transport_present(self) -> None:
        from agent_runtime.constitution import load_constitution
        from agent_runtime.tool_context import ToolExecutionContext

        transport = _transport()
        signal_payload = {"upstream_draft_transport": transport, "subject": "test"}

        class _Planner:
            def plan_next_tool(self, **_kwargs):
                return ToolCallPlan(
                    tool_name="generate_draft_reply",
                    arguments={"intent": "quote"},
                )

            last_effective_tools = {}

        engine = AgentGraphEngine(
            planner=_Planner(),
            constitution=load_constitution(),
        )
        ctx = ToolExecutionContext.from_snapshot(
            _snapshot(),
            signal_payload=signal_payload,
        )

        def _boom(*_args, **_kwargs):
            raise AssertionError("handler must not be called")

        with patch("agent_runtime.graph.AgentGraphEngine._execute_tool", side_effect=_boom):
            result = engine.run(_snapshot(), context=ctx)
        assert result.snapshot.hitl_gate.required is True
        draft = next(a for a in result.snapshot.actions if a.id == "draft_reply")
        assert draft.payload_pl == BRAIN1_BODY
        assert draft.draft_id == transport["draft_id"]
