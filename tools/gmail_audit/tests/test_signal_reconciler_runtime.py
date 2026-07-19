from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_state_rebuilder import case_rebuild_from_journal
from adjudication_executioner import append_reject_same_case_override
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal
from signal_reconciler import SignalRuntimeContext, reconcile_signal


def _runtime_context() -> tuple[InMemoryMailboxMemoryStore, SignalRuntimeContext]:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    settings = SimpleNamespace(
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "signal-runtime-tests",
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode="active",
        groq_model="test-model",
    )
    context = SignalRuntimeContext(
        settings=settings,
        journal=SignalJournal(store),
        store=store,
        graph_store=None,
        run_state={},
        model="test-model",
        verbose=False,
        mode="active",
    )
    return store, context


class _FakePolicyReport:
    def __init__(self, status: str = "REJECTED") -> None:
        self._status = status

    def to_dict(self) -> dict[str, str]:
        return {"status": self._status}


def _mutate_fake_policy_attach(
    *,
    mailbox_memory_result: dict | None,
    case_intelligence_result: dict | None,
    policy_report: _FakePolicyReport,
    policy_action_proposal: dict,
) -> None:
    pr = policy_report.to_dict()
    if isinstance(mailbox_memory_result, dict):
        mailbox_memory_result["policy_report"] = pr
        mailbox_memory_result["policy_action_proposal"] = dict(policy_action_proposal)
    if isinstance(case_intelligence_result, dict):
        case_intelligence_result["policy_report"] = pr
        case_intelligence_result["policy_action_proposal"] = dict(policy_action_proposal)
        case_intelligence_result["automation_policy"] = {
            "blocked_automation_reasons": ["policy_engine_not_approved"],
            "primary_block_reason": "policy_engine_not_approved",
        }


def _attach_fake_policy(**kwargs: object) -> tuple[_FakePolicyReport, dict[str, str]]:
    fake_report = _FakePolicyReport()
    proposal = {"proposal_id": "prop-1"}
    _mutate_fake_policy_attach(
        mailbox_memory_result=kwargs.get("mailbox_memory_result"),  # type: ignore[arg-type]
        case_intelligence_result=kwargs.get("case_intelligence_result"),  # type: ignore[arg-type]
        policy_report=fake_report,
        policy_action_proposal=proposal,
    )
    return fake_report, proposal


def test_reconcile_drive_signal_updates_case_snapshot_and_runtime_state() -> None:
    store, context = _runtime_context()
    signal = build_canonical_signal(
        signal_kind="drive_document_added",
        source_kind="drive",
        source_ref={
            "file_id": "drv-1",
            "change_id": "chg-1",
            "revision_id": "rev-1",
            "modified_time": "2026-04-13T08:00:00+02:00",
            "source_ref": "https://drive.google.com/file/d/drv-1",
        },
        observed_at="2026-04-13T08:01:00+02:00",
        effective_at="2026-04-13T08:00:00+02:00",
        case_key_hint="case-key-1",
        thread_key_hint="case-key-1",
        business_lane="finance",
        signal_summary_pl="Nowa faktura Drive",
        payload={
            "document_row": {
                "document_id": "doc-1",
                "drive_item_id": "drv-1",
                "file_name": "invoice.pdf",
                "source_ref": "https://drive.google.com/file/d/drv-1",
                "lane": "finance",
                "document_kind": "invoice",
                "scope": "case_specific",
                "case_id": "case-1",
                "probable_case_key": "case-key-1",
                "extraction_confidence": 0.9,
                "link_confidence": 0.95,
                "metadata": {"link_reasons": ["invoice_number_match"]},
            },
            "fact_rows": [
                {
                    "fact_id": "fact-1",
                    "drive_document_id": "doc-1",
                    "case_id": "case-1",
                    "probable_case_key": "case-key-1",
                    "fact_family": "transaction",
                    "entity_scope": "document",
                    "fact_key": "invoice_number",
                    "normalized_value": "FV-12",
                    "raw_value": "FV-12",
                    "confidence": 0.95,
                    "observed_at": "2026-04-13T08:01:00+02:00",
                    "source_ref": "https://drive.google.com/file/d/drv-1",
                    "status": "active",
                    "metadata": {},
                    "created_at": "2026-04-13T08:01:00+02:00",
                }
            ],
            "event_rows": [
                {
                    "event_id": "evt-1",
                    "case_id": "case-1",
                    "message_id": "",
                    "thread_id": "",
                    "event_type": "drive_document_ingested",
                    "occurred_at": "2026-04-13T08:01:00+02:00",
                    "summary_text": "Drive document ingested: invoice.pdf",
                    "payload": {"document_id": "doc-1"},
                    "source_refs": [],
                }
            ],
            "graph_upsert": {"nodes": [], "edges": []},
            "case_seed_row": {
                "case_id": "case-1",
                "case_key": "case-key-1",
                "thread_id": "",
                "case_family": "finance",
                "mailbox": "drive",
                "subject": "invoice.pdf",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {"source": "drive"},
                "created_at": "2026-04-13T08:01:00+02:00",
                "updated_at": "2026-04-13T08:01:00+02:00",
            },
            "case_id": "case-1",
            "case_key": "case-key-1",
            "linkage_status": "deterministic",
            "link_reasons": ["invoice_number_match"],
        },
        artifacts={"source": "test", "raw_observation_id": "obs-drive-1"},
        revision_marker="rev-1",
        created_by_runtime="test",
    )

    result = reconcile_signal(signal, runtime_context=context, dry_run=False)

    case_row = store.fetch_case("case-1")
    snapshot_row = store.fetch_snapshot("case-1")
    next_action = store.fetch_next_action("case-1")
    latest_hot_state = store.fetch_latest_case_snapshot_version("case-1")
    stored_facts = store.fetch_facts_for_case("case-1")
    assert result.case_id == "case-1"
    assert result.snapshot_refresh_decision is not None and result.snapshot_refresh_decision.should_refresh is True
    assert result.projection_refresh_decision is not None and result.projection_refresh_decision.should_refresh is True
    assert case_row["latest_signal_id"] == signal.signal_id
    assert snapshot_row["snapshot_json"]["case_id"] == "case-1"
    assert next_action["case_id"] == "case-1"
    assert latest_hot_state["snapshot_json"]["case_id"] == "case-1"
    assert latest_hot_state["snapshot_json"]["version"] == 1
    assert any(item["source_ref"] == "obs-drive-1" for item in stored_facts)


def test_reconcile_gmail_signal_reuses_shared_downstream_and_stamps_case_runtime_state() -> None:
    store, context = _runtime_context()
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"mailbox": "ops@example.com", "message_id": "msg-1", "thread_id": "thr-1", "history_id": "321"},
        observed_at="2026-04-13T09:00:00+02:00",
        effective_at="2026-04-13T08:59:00+02:00",
        thread_key_hint="thr-1",
        business_lane="finance",
        signal_summary_pl="Wiadomosc Gmail test",
        payload={
            "snapshot": {"source_message": {"message_id": "msg-1", "thread_id": "thr-1", "subject": "Test"}},
            "intake_result_final": {"decision": {"action": "review"}, "review_required": False},
            "preclassification_result": {"lane": "finance"},
            "lane_stage_plan": {"run_case_linking": True},
            "context_bundle": {},
        },
        artifacts={"source": "test", "raw_observation_id": "obs-gmail-1"},
        revision_marker="321",
        created_by_runtime="test",
    )
    fake_preview = {"message_id": "msg-1", "decision_action": "review"}
    fake_v2 = {
        "signal_projection": {"message_key": "msg-1"},
        "case_patch": {"case_id": "case-gmail"},
        "desk_note_patch": {"note_id": "note-1"},
        "decision_trace": {"trace_id": "trace-1"},
    }
    fake_policy_report = _FakePolicyReport()

    def _preview_side_effect(*_args: object, **kwargs: object) -> dict[str, str]:
        mailbox_memory_result = kwargs["mailbox_memory_result"]
        assert isinstance(mailbox_memory_result, dict)
        assert mailbox_memory_result["policy_report"]["status"] == "REJECTED"
        assert mailbox_memory_result["case_snapshot_hot_state"]["case"]["case_id"] == "case-gmail"
        return fake_preview

    def _v2_side_effect(*_args: object, **kwargs: object) -> dict[str, dict[str, str]]:
        mailbox_memory_result = kwargs["mailbox_memory_result"]
        case_intelligence_result = kwargs["case_intelligence_result"]
        assert isinstance(mailbox_memory_result, dict)
        assert isinstance(case_intelligence_result, dict)
        assert mailbox_memory_result["policy_report"]["status"] == "REJECTED"
        assert case_intelligence_result["automation_policy"]["blocked_automation_reasons"] == [
            "policy_engine_not_approved"
        ]
        return fake_v2

    with patch("gmail_intake.hydrate_intelligence_seam_config", return_value=None), patch(
        "gmail_intake.link_case_context",
        return_value={"selected_case_key": "case-key-gmail", "decision": "linked", "reasons": ["subject_match"]},
    ), patch(
        "gmail_intake.ingest_mailbox_memory",
        return_value={"case_id": "case-gmail", "snapshot": {}, "context_pack": {}, "facts": [], "documents": [], "events": []},
    ), patch(
        "gmail_intake.run_business_reasoning",
        return_value={"business_summary_short": "test"},
    ), patch(
        "gmail_intake.draft_reply",
        return_value={"draft_enabled": False, "drafts": []},
    ), patch(
        "gmail_intake.plan_actions",
        return_value={"primary_action": "review", "safe_for_live_push": False},
    ), patch(
        "gmail_intake.build_case_intelligence_layer",
        return_value={"case_guidance_result": {}},
    ), patch(
        "gmail_intake.finalize_mailbox_memory",
        return_value={
            "case_id": "case-gmail",
            "snapshot": {"status": "open"},
            "context_pack": {"source_refs": []},
            "next_action": {},
            "facts": [],
            "documents": [],
            "events": [],
        },
    ), patch(
        "policy_action_proposal.attach_policy_and_proposals",
        side_effect=_attach_fake_policy,
    ), patch(
        "gmail_intake.build_projection_preview",
        side_effect=_preview_side_effect,
    ), patch(
        "projection_snapshot_transport.build_operator_projection_snapshot",
        side_effect=lambda *_a, **_k: {"v2_projection": fake_v2, "decision_view": {}},
    ):
        result = reconcile_signal(signal, runtime_context=context, dry_run=False)

    case_row = store.fetch_case("case-gmail")
    latest_hot_state = store.fetch_latest_case_snapshot_version("case-gmail")
    assert result.case_id == "case-gmail"
    assert result.preview == fake_preview
    assert result.v2_projection == fake_v2
    assert case_row["latest_signal_id"] == signal.signal_id
    assert latest_hot_state["snapshot_json"]["case_id"] == "case-gmail"
    assert latest_hot_state["snapshot_json"]["version"] == 1


def test_reconcile_gmail_signal_keeps_projection_when_case_intelligence_fails() -> None:
    store, context = _runtime_context()
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"mailbox": "ops@example.com", "message_id": "msg-ci-fail", "thread_id": "thr-ci-fail", "history_id": "322"},
        observed_at="2026-04-13T09:05:00+02:00",
        effective_at="2026-04-13T09:04:00+02:00",
        thread_key_hint="thr-ci-fail",
        business_lane="service",
        signal_summary_pl="Wiadomosc Gmail test bez enrichmentu",
        payload={
            "snapshot": {"source_message": {"message_id": "msg-ci-fail", "thread_id": "thr-ci-fail", "subject": "Pilny serwis"}},
            "intake_result_final": {"decision": {"action": "review"}, "review_required": True},
            "preclassification_result": {"lane": "review_direct"},
            "lane_stage_plan": {"run_case_linking": True},
            "context_bundle": {},
        },
        artifacts={"source": "test", "raw_observation_id": "obs-gmail-ci-fail"},
        revision_marker="322",
        created_by_runtime="test",
    )
    fake_v2 = {
        "signal_projection": {"message_key": "msg-ci-fail"},
        "case_patch": {"case_id": "case-ci-fail"},
        "desk_note_patch": {"note_id": "note-ci-fail"},
        "decision_trace": {"trace_id": "trace-ci-fail"},
    }

    def _v2_side_effect(*_args: object, **kwargs: object) -> dict[str, dict[str, str]]:
        case_intelligence_result = kwargs["case_intelligence_result"]
        assert isinstance(case_intelligence_result, dict)
        assert case_intelligence_result["execution_metadata"]["source_mode"] == "fallback"
        assert case_intelligence_result["execution_metadata"]["fallback_reason"] == "case_intelligence_exception"
        return fake_v2

    with patch("gmail_intake.hydrate_intelligence_seam_config", return_value=None), patch(
        "gmail_intake.link_case_context",
        return_value={"selected_case_key": "case-key-ci-fail", "decision": "linked", "reasons": ["subject_match"]},
    ), patch(
        "gmail_intake.ingest_mailbox_memory",
        return_value={"case_id": "case-ci-fail", "snapshot": {}, "context_pack": {}, "facts": [], "documents": [], "events": []},
    ), patch(
        "gmail_intake.run_business_reasoning",
        return_value={"business_summary_short": "serwis"},
    ), patch(
        "gmail_intake.draft_reply",
        return_value={"draft_enabled": False, "drafts": []},
    ), patch(
        "gmail_intake.plan_actions",
        return_value={"primary_action": "review", "safe_for_operator_projection": True, "safe_for_live_push": False},
    ), patch(
        "gmail_intake.build_case_intelligence_layer",
        side_effect=RuntimeError("case intelligence provider timeout"),
    ), patch(
        "gmail_intake.finalize_mailbox_memory",
        return_value={
            "case_id": "case-ci-fail",
            "snapshot": {"status": "open"},
            "context_pack": {"source_refs": []},
            "next_action": {},
            "facts": [],
            "documents": [],
            "events": [],
        },
    ), patch(
        "policy_action_proposal.attach_policy_and_proposals",
        side_effect=lambda **kw: (
            _mutate_fake_policy_attach(
                mailbox_memory_result=kw.get("mailbox_memory_result"),  # type: ignore[arg-type]
                case_intelligence_result=kw.get("case_intelligence_result"),  # type: ignore[arg-type]
                policy_report=_FakePolicyReport(status="NEEDS_HUMAN"),
                policy_action_proposal={"proposal_id": "prop-ci-fail"},
            ),
            (_FakePolicyReport(status="NEEDS_HUMAN"), {"proposal_id": "prop-ci-fail"}),
        )[1],
    ), patch(
        "gmail_intake.build_projection_preview",
        return_value={"message_id": "msg-ci-fail", "decision_action": "review"},
    ), patch(
        "projection_snapshot_transport.build_operator_projection_snapshot",
        side_effect=lambda *_a, **_k: {"v2_projection": fake_v2, "decision_view": {}},
    ):
        result = reconcile_signal(signal, runtime_context=context, dry_run=False)

    assert result.processing_state == "reconciled"
    assert result.case_id == "case-ci-fail"
    assert result.v2_projection == fake_v2
    assert "case_intelligence_exception" in result.warnings


def test_reconcile_gmail_adjudication_conflict_still_appends_hot_state_version() -> None:
    store, context = _runtime_context()
    store.upsert_case(
        {
            "case_id": "case-conflict",
            "case_key": "thread:thr-conflict",
            "thread_id": "thr-conflict",
            "case_family": "supplier_commercial_review",
            "mailbox": "test",
            "subject": "Zapytanie",
            "status": "open",
            "customer_name": "",
            "customer_email": "arek@kliwent.eu",
            "metadata": {},
            "created_at": "2026-04-16T10:00:00+02:00",
            "updated_at": "2026-04-16T10:00:00+02:00",
        }
    )
    signal = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"mailbox": "ops@example.com", "message_id": "msg-conflict", "thread_id": "thr-conflict", "history_id": "321"},
        observed_at="2026-04-13T09:00:00+02:00",
        effective_at="2026-04-13T08:59:00+02:00",
        case_key_hint="thread:thr-conflict",
        thread_key_hint="thr-conflict",
        business_lane="intake_llm",
        signal_summary_pl="Wiadomosc Gmail test adjudication",
        payload={
            "snapshot": {"source_message": {"message_id": "msg-conflict", "thread_id": "thr-conflict", "subject": "Zapytanie"}},
            "intake_result_final": {"decision": {"action": "review"}, "review_required": True},
            "preclassification_result": {"lane": "intake_llm"},
            "lane_stage_plan": {"run_case_linking": True},
            "context_bundle": {},
            "case_id": "case-conflict",
        },
        artifacts={"source": "test", "raw_observation_id": "obs-gmail-conflict"},
        revision_marker="321",
        created_by_runtime="test",
    )
    append_reject_same_case_override(
        store,
        signal_id=signal.signal_id,
        rejected_case_id="case-conflict",
        adjudication_event_id="adj-conflict-1",
        trace_id="trace-conflict",
    )
    fake_policy_report = _FakePolicyReport(status="NEEDS_HUMAN")

    with patch("gmail_intake.hydrate_intelligence_seam_config", return_value=None), patch(
        "gmail_intake.link_case_context",
        return_value={"selected_case_key": "thread:thr-conflict", "decision": "linked", "reasons": ["same_thread_id"]},
    ), patch(
        "gmail_intake.ingest_mailbox_memory",
        return_value={"enabled": False, "execution_metadata": {"stage_name": "mailbox_memory", "parse_status": "disabled"}},
    ), patch(
        "gmail_intake.run_business_reasoning",
        return_value={"business_summary_short": "test"},
    ), patch(
        "gmail_intake.draft_reply",
        return_value={"draft_enabled": False, "drafts": []},
    ), patch(
        "gmail_intake.plan_actions",
        return_value={"primary_action": "review", "safe_for_live_push": False},
    ), patch(
        "gmail_intake.build_case_intelligence_layer",
        return_value={"case_understanding": {}, "execution_metadata": {}, "case_guidance_result": {}},
    ), patch(
        "gmail_intake.finalize_mailbox_memory",
        return_value={"enabled": False, "execution_metadata": {"stage_name": "mailbox_memory", "parse_status": "disabled"}},
    ), patch(
        "policy_action_proposal.attach_policy_and_proposals",
        side_effect=_attach_fake_policy,
    ), patch(
        "gmail_intake.build_projection_preview",
        return_value={"message_id": "msg-conflict", "decision_action": "review"},
    ), patch(
        "projection_snapshot_transport.build_operator_projection_snapshot",
        return_value={
            "v2_projection": {"signal_projection": {}, "case_patch": {}, "desk_note_patch": {}, "decision_trace": {}},
            "decision_view": {},
        },
    ):
        result = reconcile_signal(signal, runtime_context=context, dry_run=False)

    latest_hot_state = store.fetch_latest_case_snapshot_version("case-conflict")
    assert result.case_id == ""
    assert latest_hot_state is not None
    assert latest_hot_state["snapshot_json"]["case"]["case_id"] == "case-conflict"
    assert latest_hot_state["snapshot_json"]["case"]["operational_status"] == "CONFLICT"
    assert latest_hot_state["snapshot_json"]["version"] == 1


def test_reconcile_drive_signal_builds_v2_after_policy_attach() -> None:
    store, context = _runtime_context()
    signal = build_canonical_signal(
        signal_kind="drive_document_added",
        source_kind="drive",
        source_ref={
            "file_id": "drv-3",
            "change_id": "chg-3",
            "revision_id": "rev-3",
            "modified_time": "2026-04-13T11:00:00+02:00",
            "source_ref": "https://drive.google.com/file/d/drv-3",
        },
        observed_at="2026-04-13T11:01:00+02:00",
        effective_at="2026-04-13T11:00:00+02:00",
        case_key_hint="case-key-3",
        thread_key_hint="case-key-3",
        business_lane="finance",
        signal_summary_pl="Drive policy order test",
        payload={
            "document_row": {
                "document_id": "doc-3",
                "drive_item_id": "drv-3",
                "file_name": "invoice-3.pdf",
                "source_ref": "https://drive.google.com/file/d/drv-3",
                "lane": "finance",
                "document_kind": "invoice",
                "scope": "case_specific",
                "case_id": "case-3",
                "probable_case_key": "case-key-3",
                "extraction_confidence": 0.9,
                "link_confidence": 0.95,
                "metadata": {"link_reasons": ["invoice_number_match"]},
            },
            "fact_rows": [],
            "event_rows": [],
            "graph_upsert": {"nodes": [], "edges": []},
            "case_seed_row": {
                "case_id": "case-3",
                "case_key": "case-key-3",
                "thread_id": "",
                "case_family": "finance",
                "mailbox": "drive",
                "subject": "invoice-3.pdf",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {"source": "drive"},
                "created_at": "2026-04-13T11:01:00+02:00",
                "updated_at": "2026-04-13T11:01:00+02:00",
            },
            "case_id": "case-3",
            "case_key": "case-key-3",
            "linkage_status": "deterministic",
            "link_reasons": ["invoice_number_match"],
        },
        artifacts={"source": "test", "raw_observation_id": "obs-drive-3"},
        revision_marker="rev-3",
        created_by_runtime="test",
    )
    fake_policy_report = _FakePolicyReport()
    fake_v2 = {
        "signal_projection": {"message_key": "drv-3"},
        "case_patch": {"case_id": "case-3"},
        "desk_note_patch": {"note_id": "note-3"},
        "decision_trace": {"trace_id": "trace-3"},
    }

    def _drive_v2_side_effect(*_args: object, **kwargs: object) -> dict[str, dict[str, str]]:
        stage_outputs = kwargs["stage_outputs"]
        mailbox_memory_result = stage_outputs["mailbox_memory_result"]
        case_intelligence_result = stage_outputs["case_intelligence_result"]
        assert mailbox_memory_result["policy_report"]["status"] == "REJECTED"
        assert case_intelligence_result["automation_policy"]["blocked_automation_reasons"] == [
            "policy_engine_not_approved"
        ]
        return fake_v2

    def _drive_snapshot_side_effect(*_args: object, **kwargs: object) -> dict[str, object]:
        stage_outputs = kwargs["stage_outputs"]
        assert isinstance(stage_outputs, dict)
        mailbox_memory_result = stage_outputs["mailbox_memory_result"]
        case_intelligence_result = stage_outputs["case_intelligence_result"]
        assert mailbox_memory_result["policy_report"]["status"] == "REJECTED"
        assert case_intelligence_result["automation_policy"]["blocked_automation_reasons"] == [
            "policy_engine_not_approved"
        ]
        return {"v2_projection": fake_v2, "decision_view": {}}

    with patch(
        "policy_action_proposal.attach_policy_and_proposals",
        side_effect=_attach_fake_policy,
    ), patch(
        "projection_snapshot_transport.build_operator_projection_snapshot",
        side_effect=_drive_snapshot_side_effect,
    ):
        result = reconcile_signal(signal, runtime_context=context, dry_run=False)

    assert result.v2_projection == fake_v2


def test_case_rebuild_from_journal_is_deterministic_for_drive_case() -> None:
    store, context = _runtime_context()
    signal = build_canonical_signal(
        signal_kind="drive_document_added",
        source_kind="drive",
        source_ref={"file_id": "drv-2", "change_id": "chg-2", "revision_id": "rev-2", "modified_time": "2026-04-13T10:00:00+02:00"},
        observed_at="2026-04-13T10:01:00+02:00",
        effective_at="2026-04-13T10:00:00+02:00",
        case_key_hint="case-key-2",
        thread_key_hint="case-key-2",
        business_lane="finance",
        signal_summary_pl="Drive deterministic rebuild",
        payload={
            "document_row": {
                "document_id": "doc-2",
                "drive_item_id": "drv-2",
                "file_name": "invoice-2.pdf",
                "source_ref": "https://drive.google.com/file/d/drv-2",
                "lane": "finance",
                "document_kind": "invoice",
                "scope": "case_specific",
                "case_id": "case-2",
                "probable_case_key": "case-key-2",
                "extraction_confidence": 0.9,
                "link_confidence": 0.95,
                "metadata": {},
            },
            "fact_rows": [],
            "event_rows": [],
            "graph_upsert": {"nodes": [], "edges": []},
            "case_seed_row": {
                "case_id": "case-2",
                "case_key": "case-key-2",
                "thread_id": "",
                "case_family": "finance",
                "mailbox": "drive",
                "subject": "invoice-2.pdf",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {"source": "drive"},
                "created_at": "2026-04-13T10:01:00+02:00",
                "updated_at": "2026-04-13T10:01:00+02:00",
            },
            "case_id": "case-2",
            "case_key": "case-key-2",
            "linkage_status": "deterministic",
            "link_reasons": ["seeded_case"],
        },
        artifacts={"source": "test"},
        revision_marker="rev-2",
        created_by_runtime="test",
    )
    context.journal.append(signal)
    reconcile_signal(signal, runtime_context=context, dry_run=False)

    first = case_rebuild_from_journal(case_id="case-2", runtime_context=context).to_dict()
    second = case_rebuild_from_journal(case_id="case-2", runtime_context=context).to_dict()

    assert first["case_id"] == second["case_id"] == "case-2"
    assert first["updated_case_state"]["case_id"] == second["updated_case_state"]["case_id"] == "case-2"
    assert first["updated_next_action"]["next_action"] == second["updated_next_action"]["next_action"]
    assert first["updated_snapshot"]["recommended_next_action"] == second["updated_snapshot"]["recommended_next_action"]
    assert first["source_refs_used"] == second["source_refs_used"]
    assert first["rebuild_mode"] == "case_rebuild_from_journal"
