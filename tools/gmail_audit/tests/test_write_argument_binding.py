"""P1.2B: post-HITL write-execution argument binding.

Proves the invariant:

    AN APPROVED ACTION IS NOT AUTHORIZED WITH ARBITRARY EXECUTION ARGUMENTS

through the real production seam (agent_hitl_bridge.execute_hitl_send_from_bridge_row
-> write-boundary binding -> hitl_gmail_send tombstone) plus deterministic unit
tests of the binding evaluator. LIVE_SEND stays false everywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_hitl_bridge import execute_hitl_send_from_bridge_row
from agent_runtime.draft_identity import compute_body_hash, compute_draft_id
from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.settings import load_agent_runtime_settings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.write_argument_binding import (
    REASON_APPROVAL_ARTIFACT_MISMATCH,
    REASON_APPROVAL_MISSING,
    REASON_CANONICAL_ARGUMENT_MISMATCH,
    REASON_STALE_DECISION_REVISION,
    REASON_UNBOUND_EXECUTION_ARGUMENT,
    VERDICT_READY,
    WriteBoundaryDeniedError,
    evaluate_write_execution_binding,
)
from llm_contracts.engagement_snapshot_v2 import (
    ActionItem,
    CommunicationReceipt,
    PolicyActionEnvelopeV1,
)
from mailbox_memory import InMemoryMailboxMemoryStore


DECISION_ID = "dec_p1_2b"
SEMANTIC_HASH = "sh_p1_2b"
CASE_ID = "case_p1_2b"
ENGAGEMENT_ID = "eng_p1_2b"
CUSTOMER_EMAIL = "customer@example.com"
THREAD_ID = "thread_1"
BODY = "Zatwierdzony draft P1.2B"
ACTION_ID = "draft_reply"


def _bound_snapshot(
    *,
    case_id: str = CASE_ID,
    engagement_id: str = ENGAGEMENT_ID,
    decision_id: str = DECISION_ID,
    revision: int = 1,
    semantic_hash: str = SEMANTIC_HASH,
    body: str = BODY,
    target_email: str = CUSTOMER_EMAIL,
    thread_id: str = THREAD_ID,
    gate_required: bool = False,
    receipt_state: str = "ready_for_manual_send",
    with_envelope: bool = True,
):
    version_id = f"{decision_id}:r{revision}"
    draft_id = compute_draft_id(
        case_id=case_id,
        source_signal_id="sig_p1_2b",
        action_id=ACTION_ID,
    )
    body_hash = compute_body_hash(body)
    snap = build_initial_snapshot(
        case_id=case_id,
        engagement_id=engagement_id,
        signal_id="sig_p1_2b",
        trace_id="t_p1_2b",
    )
    action = ActionItem(
        id=ACTION_ID,
        enabled=True,
        payload_pl=body,
        draft_id=draft_id,
        revision=1,
        body_hash=body_hash,
        case_id=case_id,
        source_signal_id="sig_p1_2b",
        decision_version_id=version_id,
        source_semantic_hash=semantic_hash,
        identity_state="complete",
        parent_policy_decision_id="pdec_p1_2b",
        parent_action_proposal_v2_id="apv2_p1_2b",
        parent_decision_candidate_id="dc_p1_2b",
    )
    delta: dict = {
        "actions": [action.model_dump(mode="python")],
        "hitl_gate": {
            "required": gate_required,
            "reason": "" if not gate_required else "draft_ready_for_approval",
        },
        "communication_receipt": CommunicationReceipt(
            state=receipt_state,
            draft_id=draft_id,
            body_hash=body_hash,
            target_email=target_email,
            thread_id=thread_id,
        ).model_dump(mode="python"),
    }
    if with_envelope:
        delta["policy_action_envelope"] = PolicyActionEnvelopeV1(
            canonical_decision_id=decision_id,
            decision_version_id=version_id,
            source_semantic_hash=semantic_hash,
            policy_decision_id="pdec_p1_2b",
            action_proposal_id="apv2_p1_2b",
            decision_candidate_id="dc_p1_2b",
            source_signal_id="sig_p1_2b",
            source_message_id="msg_p1_2b",
            action_intent="ask_for_missing_data",
            action_target="customer",
            action_channel="mail",
            allowed_action_tools=["generate_draft_reply"],
            freshness="current",
            requires_operator_approval=True,
        ).model_dump(mode="python")
    return apply_snapshot_delta(snap, delta)


def _store_with_revisions(
    *,
    current_revision: int = 1,
    decision_id: str = DECISION_ID,
    semantic_hash: str = SEMANTIC_HASH,
    case_id: str = CASE_ID,
    customer_email: str = CUSTOMER_EMAIL,
) -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    for rev in range(1, current_revision + 1):
        store.append_decision_revision(
            {
                "decision_id": decision_id,
                "revision": rev,
                "decision_version_id": f"{decision_id}:r{rev}",
                "semantic_hash": semantic_hash if rev == current_revision else f"sh_old_r{rev}",
                "revision_status": "CURRENT" if rev == current_revision else "SUPERSEDED",
                "case_id": case_id,
            }
        )
    store.upsert_case(
        {
            "case_id": case_id,
            "case_family": "mail_case",
            "status": "open",
            "customer_email": customer_email,
            "metadata": {},
        }
    )
    return store


class _RuntimeDouble:
    def __init__(self, store, *, to: str = CUSTOMER_EMAIL, thread_id: str = THREAD_ID) -> None:
        self.store = store
        self._to = to
        self._thread_id = thread_id

    def bootstrap(self) -> None:
        return None

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = ""):
        return {
            "intake_output": {
                "message": {"from": self._to, "thread_id": self._thread_id}
            },
            "facts": [],
        }


def _ledger(store) -> Any:
    from canonical_action_decision import build_store_backed_decision_ledger

    return build_store_backed_decision_ledger(store)


def _evaluate(*, snapshot=None, proposed=None, resolved_target=None, ledger=None, **kwargs):
    snap = snapshot or _bound_snapshot()
    store = _store_with_revisions()
    return evaluate_write_execution_binding(
        snapshot=snap,
        action_id=ACTION_ID,
        proposed=proposed,
        resolved_target=resolved_target,
        ledger=ledger if ledger is not None else _ledger(store),
        **kwargs,
    )


def _proposed(**overrides) -> dict:
    snap = _bound_snapshot()
    action = snap.actions[0]
    return {
        "case_id": snap.case_id,
        "draft_id": action.draft_id,
        "body_hash": action.body_hash,
        "revision": action.revision,
        "decision_version_id": action.decision_version_id,
        "semantic_hash": action.source_semantic_hash,
        **overrides,
    }


# ---------------------------------------------------------------------------
# Unit tests: evaluate_write_execution_binding
# ---------------------------------------------------------------------------


def test_binding_positive_verdict_is_write_boundary_ready() -> None:
    result = _evaluate(proposed=_proposed(), resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID})
    assert result["status"] == "pass"
    assert result["verdict"] == VERDICT_READY
    assert result["reason_codes"] == []


def test_binding_denies_wrong_recipient() -> None:
    result = _evaluate(
        proposed=_proposed(),
        resolved_target={"to": "attacker@example.com", "thread_id": THREAD_ID},
    )
    assert result["status"] == "deny"
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in result["reason_codes"]
    recipient_violation = next(
        v for v in result["violations"] if v.get("argument_name") == "recipient"
    )
    assert recipient_violation["expected"] == CUSTOMER_EMAIL


def test_binding_denies_wrong_thread() -> None:
    result = _evaluate(
        proposed={**_proposed(), "thread_id": "thread_attacker"},
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
    )
    assert result["status"] == "deny"
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in result["reason_codes"]


def test_binding_denies_foreign_case() -> None:
    result = _evaluate(
        proposed=_proposed(case_id="case_foreign"),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
    )
    assert result["status"] == "deny"
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in result["reason_codes"]


def test_binding_denies_modified_draft_after_approval() -> None:
    result = _evaluate(
        proposed=_proposed(body_hash=compute_body_hash("Zmieniony draft po approval")),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
    )
    assert result["status"] == "deny"
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in result["reason_codes"]


def test_binding_denies_approval_for_another_draft() -> None:
    result = _evaluate(
        proposed=_proposed(draft_id="draft_other"),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
    )
    assert result["status"] == "deny"
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in result["reason_codes"]


def test_binding_denies_stale_decision_revision() -> None:
    store = _store_with_revisions(current_revision=2)
    snap = _bound_snapshot(revision=1)
    result = evaluate_write_execution_binding(
        snapshot=snap,
        action_id=ACTION_ID,
        proposed=_proposed(),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
        ledger=_ledger(store),
    )
    assert result["status"] == "deny"
    assert REASON_STALE_DECISION_REVISION in result["reason_codes"]


def test_binding_denies_missing_approval() -> None:
    snap = _bound_snapshot(gate_required=True)
    result = evaluate_write_execution_binding(
        snapshot=snap,
        action_id=ACTION_ID,
        proposed=_proposed(),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
        ledger=_ledger(_store_with_revisions()),
    )
    assert result["status"] == "deny"
    assert REASON_APPROVAL_MISSING in result["reason_codes"]


def test_binding_denies_approval_artifact_mismatch() -> None:
    snap = _bound_snapshot()
    receipt = snap.communication_receipt.model_copy(
        update={"body_hash": compute_body_hash("Inny zatwierdzony draft")}
    )
    snap = snap.model_copy(update={"communication_receipt": receipt})
    result = evaluate_write_execution_binding(
        snapshot=snap,
        action_id=ACTION_ID,
        proposed=_proposed(),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
        ledger=_ledger(_store_with_revisions()),
    )
    assert result["status"] == "deny"
    assert REASON_APPROVAL_ARTIFACT_MISMATCH in result["reason_codes"]


def test_binding_denies_missing_current_revision() -> None:
    result = evaluate_write_execution_binding(
        snapshot=_bound_snapshot(),
        action_id=ACTION_ID,
        proposed=_proposed(),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
        ledger=None,
    )
    assert result["status"] == "deny"
    assert REASON_UNBOUND_EXECUTION_ARGUMENT in result["reason_codes"]


def test_binding_denies_stale_preview_packet_hash() -> None:
    result = _evaluate(
        proposed=_proposed(),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
        expected_body_hash=compute_body_hash("Stary podglad operatora"),
    )
    assert result["status"] == "deny"
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in result["reason_codes"]


def test_binding_denies_unknown_execution_argument() -> None:
    result = _evaluate(
        proposed={**_proposed(), "attachment_ids": ["a1"]},
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
    )
    assert result["status"] == "deny"


def test_binding_verdict_is_pure_of_timestamps() -> None:
    base = _evaluate(
        proposed=_proposed(),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
    )
    snap = _bound_snapshot()
    snap = snap.model_copy(
        update={
            "communication_receipt": snap.communication_receipt.model_copy(
                update={"sent_at": "2026-08-22T10:00:00Z"}
            )
        }
    )
    reordered = evaluate_write_execution_binding(
        snapshot=snap,
        action_id=ACTION_ID,
        proposed=_proposed(),
        resolved_target={"to": CUSTOMER_EMAIL, "thread_id": THREAD_ID},
        ledger=_ledger(_store_with_revisions()),
    )
    assert base["verdict"] == reordered["verdict"] == VERDICT_READY


# ---------------------------------------------------------------------------
# Production-path tests: execute_hitl_send_from_bridge_row (real seam)
# ---------------------------------------------------------------------------


def _fake_execute(**kwargs: object) -> dict[str, object]:
    kwargs["on_effect_start"]()
    return {
        "executed": True,
        "effect_started": True,
        "decision_status": "executed",
        "mode": "bounded_dry_run",
    }


def _run_bridge_send(
    snapshot,
    store,
    runtime,
    *,
    row_overrides: dict | None = None,
    executor=None,
):
    operator_store = InMemoryOperatorEngagementStore()
    operator_store.insert_snapshot(snapshot)
    service = AgentMcpService(
        store=operator_store,
        settings=load_agent_runtime_settings(),
    )
    row = {
        "queue_id": "bq_p1_2b",
        "engagement_id": snapshot.engagement_id,
        "action_id": ACTION_ID,
        "operator_id": "operator_1",
        "case_id": snapshot.case_id,
        **(row_overrides or {}),
    }
    settings = SimpleNamespace(
        daszek_operational_feed_auto_push_enabled=False,
        mailbox_memory_database_url="",
    )
    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.build_mailbox_memory_runtime", return_value=runtime):
            with patch("agent_hitl_bridge.publish_os_event", return_value=None):
                with patch(
                    "agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl",
                    return_value={"skipped": True},
                ):
                    with patch(
                        "agent_hitl_bridge.execute_hitl_gmail_send",
                        side_effect=executor or _fake_execute,
                    ) as exec_mock:
                        out = execute_hitl_send_from_bridge_row(
                            row=row,
                            settings=settings,
                        )
    return out, exec_mock


def test_production_path_correct_approved_artifact_reaches_write_boundary() -> None:
    snap = _bound_snapshot()
    store = _store_with_revisions()
    runtime = _RuntimeDouble(store)
    out, exec_mock = _run_bridge_send(snap, store, runtime)
    assert out["ok"] is True
    assert exec_mock.call_count == 1
    assert out["execution"]["executed"] is True
    assert out["execution"]["mode"] == "bounded_dry_run"


def test_production_path_denies_wrong_recipient_without_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _bound_snapshot()
    store = _store_with_revisions()
    runtime = _RuntimeDouble(store, to="attacker@example.com")
    with pytest.raises(WriteBoundaryDeniedError) as exc_info:
        _run_bridge_send(snap, store, runtime)
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in exc_info.value.verdict["reason_codes"]


def test_production_path_env_recipient_override_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _bound_snapshot()
    store = _store_with_revisions()
    runtime = _RuntimeDouble(store, to=CUSTOMER_EMAIL)
    monkeypatch.setenv("AGENT_HITL_SEND_TO", "attacker@example.com")
    with pytest.raises(WriteBoundaryDeniedError) as exc_info:
        _run_bridge_send(snap, store, runtime)
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in exc_info.value.verdict["reason_codes"]


def test_production_path_denies_foreign_case_without_executor() -> None:
    snap = _bound_snapshot()
    store = _store_with_revisions()
    runtime = _RuntimeDouble(store)
    with pytest.raises(WriteBoundaryDeniedError) as exc_info:
        _run_bridge_send(snap, store, runtime, row_overrides={"case_id": "case_foreign"})
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in exc_info.value.verdict["reason_codes"]


def test_production_path_denies_modified_draft_without_executor() -> None:
    snap = _bound_snapshot()
    store = _store_with_revisions()
    runtime = _RuntimeDouble(store)
    with pytest.raises(WriteBoundaryDeniedError) as exc_info:
        _run_bridge_send(
            snap,
            store,
            runtime,
            row_overrides={"operator_draft_pl": "Zmieniony draft po approval"},
        )
    assert REASON_CANONICAL_ARGUMENT_MISMATCH in exc_info.value.verdict["reason_codes"]


def test_production_path_denies_stale_revision_without_executor() -> None:
    snap = _bound_snapshot(revision=1)
    store = _store_with_revisions(current_revision=2)
    runtime = _RuntimeDouble(store)
    with pytest.raises(WriteBoundaryDeniedError) as exc_info:
        _run_bridge_send(snap, store, runtime)
    assert REASON_STALE_DECISION_REVISION in exc_info.value.verdict["reason_codes"]


def test_production_path_thread_is_runtime_owned_only() -> None:
    """The bridge never accepts a thread input; runtime resolution is the owner.

    A hostile row field cannot change the thread used at the write boundary --
    the binding uses only the canonical runtime resolution (runtime ownership
    itself is the constraint; foreign thread claims are denied by the
    evaluator, see test_binding_denies_wrong_thread).
    """
    snap = _bound_snapshot()
    store = _store_with_revisions()
    runtime = _RuntimeDouble(store, thread_id=THREAD_ID)
    out, exec_mock = _run_bridge_send(
        snap,
        store,
        runtime,
        row_overrides={"thread_id": "thread_attacker"},
    )
    assert out["ok"] is True
    assert exec_mock.call_count == 1
