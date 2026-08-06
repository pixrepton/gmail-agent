"""PF-01 — draft sanity must gate kalk / transfer materialize / HITL mint-edit paths."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.draft_identity import (
    apply_operator_draft_edit,
    compute_body_hash,
    mint_gap_only_draft_action,
)
from agent_runtime.draft_lineage_transport import (
    materialize_transferred_draft_action,
    tool_result_from_upstream_transport,
)
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools.handlers import call_kalk_top_quote


BAD_PLACEHOLDER = "Dzień dobry, prosimy uzupełnić [TODO] dane oferty."
BAD_PROMISE = "Dzień dobry, gwarantujemy montaż i cena ostateczna wynosi 10000 zł."
CLEAN_BODY = (
    "Dzień dobry,\n\ndziękujemy za zapytanie. Wracamy z odpowiedzią po weryfikacji.\n\n"
    "Zespół TOP-INSTAL"
)


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=4,
        openai_api_key="test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="http://127.0.0.1:8091",
        kalk_top_agent_key="k",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=1,
    )


def _transport(*, body: str) -> dict:
    body_hash = compute_body_hash(body)
    return {
        "draft_id": "draft_pf01_test",
        "revision": 1,
        "body": body,
        "body_hash": body_hash,
        "source": "brain1",
        "created_at": "2026-08-06T00:00:00+00:00",
        "action_id": "draft_reply",
        "case_id": "case_pf01",
        "source_signal_id": "sig_pf01",
    }


def test_kalk_path_placeholder_not_enabled() -> None:
    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(update={"case_kind": "wycena_oferta"})
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings())
    poisoned = {
        "pricing": {"totals": {"note": "[TODO] internal placeholder", "gross": 1}},
    }
    with patch(
        "agent_runtime.tools.handlers.call_calculate_offer",
        return_value=poisoned,
    ):
        result = call_kalk_top_quote(
            ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
            ctx,
        )
    assert result.status == "error"
    assert result.failure_class == "DRAFT_SANITY_FAILED"
    actions = result.snapshot_delta.get("actions") or []
    assert actions
    assert actions[0].get("enabled") is False
    assert str(actions[0].get("disabled_reason_pl") or "").startswith("DRAFT_SANITY_FAILED")


def test_kalk_path_forbidden_promise_not_enabled() -> None:
    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(update={"case_kind": "wycena_oferta"})
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings())
    poisoned = {
        "pricing": {
            "totals": {
                "marketing": "gwarantujemy cenę — cena ostateczna wynosi 9999",
            }
        },
    }
    with patch(
        "agent_runtime.tools.handlers.call_calculate_offer",
        return_value=poisoned,
    ):
        result = call_kalk_top_quote(
            ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
            ctx,
        )
    assert result.status == "error"
    assert result.failure_class == "DRAFT_SANITY_FAILED"
    assert (result.snapshot_delta.get("actions") or [{}])[0].get("enabled") is False


def test_kalk_path_clean_totals_enabled() -> None:
    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    snap = snap.model_copy(update={"case_kind": "wycena_oferta"})
    ctx = ToolExecutionContext.from_snapshot(snap, settings=_settings())
    clean = {"pricing": {"totals": {"net": 12000, "gross": 14760}}}
    with patch(
        "agent_runtime.tools.handlers.call_calculate_offer",
        return_value=clean,
    ):
        result = call_kalk_top_quote(
            ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
            ctx,
        )
    assert result.status == "ok"
    assert (result.snapshot_delta.get("actions") or [{}])[0].get("enabled") is True


def test_transfer_materialize_bad_body_not_enabled() -> None:
    transport = _transport(body=BAD_PLACEHOLDER)
    action = materialize_transferred_draft_action(transport)
    assert action.get("enabled") is False
    assert str(action.get("disabled_reason_pl") or "").startswith("DRAFT_SANITY_FAILED")

    result = tool_result_from_upstream_transport(transport)
    assert result.status == "error"
    assert (result.snapshot_delta.get("actions") or [{}])[0].get("enabled") is False


def test_transfer_materialize_clean_body_enabled() -> None:
    transport = _transport(body=CLEAN_BODY)
    action = materialize_transferred_draft_action(transport)
    assert action.get("enabled") is True
    assert action.get("disabled_reason_pl") is None


def test_hitl_apply_operator_edit_bad_body_not_enabled() -> None:
    item = {
        "id": "draft_reply",
        "enabled": True,
        "payload_pl": CLEAN_BODY,
        "body_hash": compute_body_hash(CLEAN_BODY),
        "revision": 1,
    }
    out = apply_operator_draft_edit(
        item,
        draft_text=BAD_PROMISE,
        case_id="case_pf01",
        source_signal_id="sig_pf01",
        action_id="draft_reply",
        case_kind="wycena_oferta",
    )
    assert out.get("enabled") is False
    assert "forbidden_promise" in str(out.get("disabled_reason_pl") or "")


def test_hitl_mint_gap_only_bad_body_not_enabled() -> None:
    action = mint_gap_only_draft_action(
        action_id="draft_reply",
        draft_text=BAD_PLACEHOLDER,
        case_id="case_pf01",
        source_signal_id="sig_pf01",
        case_kind="wycena_oferta",
    )
    assert action.get("enabled") is False
    assert "placeholder_or_internal_token" in str(action.get("disabled_reason_pl") or "")


def test_hitl_mint_gap_only_clean_body_enabled() -> None:
    action = mint_gap_only_draft_action(
        action_id="draft_reply",
        draft_text=CLEAN_BODY,
        case_id="case_pf01",
        source_signal_id="sig_pf01",
        case_kind="wycena_oferta",
    )
    assert action.get("enabled") is True
    assert action.get("disabled_reason_pl") is None
