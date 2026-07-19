from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_snapshot_hot_state_contract import CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION
from gmail_intake import (
    build_case_intelligence_layer,
    draft_reply,
    inject_latest_hot_state_for_resolved_case,
    load_hot_state_preflight_for_stage_config,
    plan_actions,
    run_business_reasoning,
)
from tests.fixture_helpers import run_fixture


def _hot_state(*, case_id: str = "case-1", summary: str = "Hot summary from snapshot") -> dict[str, object]:
    return {
        "schema_version": CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
        "snapshot_id": "snap-1",
        "case": {
            "case_id": case_id,
            "case_key": "case-key-1",
            "case_family": "operations",
            "lifecycle_status": "open",
            "operational_status": "awaiting_review",
            "waiting_for": "operator",
            "priority": "high",
            "summary_text": summary,
        },
        "key_facts": [
            {
                "fact_key": "invoice_number",
                "entity_scope": "case",
                "value": "FV-100",
                "confidence": 0.93,
                "source_ref": "obs-1",
                "provenance": {"kind": "signal", "ref": "obs-1"},
            }
        ],
        "open_loops": [{"loop_id": "ol-1", "description": "Confirm totals."}],
        "active_conflicts": [{"fact_key": "city", "values": ["A", "B"]}],
        "documents_summary": [{"document_id": "doc-1", "summary_text": "Invoice"}],
        "recommended_next_step": "review",
        "cold_evidence_pointers": {"signal_ids": ["sig-1"]},
        "snapshot_meta": {"version": 1, "source_signal_id": "sig-1", "confidence": 0.9},
    }


def _runtime_with_hot_state(hot_state: dict[str, object]) -> SimpleNamespace:
    store = SimpleNamespace(fetch_latest_case_snapshot_version=lambda _case_id: {"snapshot_json": hot_state})
    return SimpleNamespace(store=store)


class HotStatePreflightTests(unittest.TestCase):
    def test_load_hot_state_preflight_updates_effective_context_pack(self) -> None:
        hot = _hot_state()
        mailbox_memory_result = {
            "enabled": True,
            "case_id": "case-1",
            "context_pack": {
                "case_id": "case-1",
                "snapshot": {"open_questions": ["legacy question"]},
            },
        }
        config = {
            "mailbox_memory_runtime": _runtime_with_hot_state(hot),
            "mailbox_memory_context_pack": mailbox_memory_result["context_pack"],
        }

        loaded = load_hot_state_preflight_for_stage_config(
            mailbox_memory_result=mailbox_memory_result,
            config=config,
        )

        self.assertEqual(loaded["snapshot_id"], "snap-1")
        self.assertEqual(
            config["mailbox_memory_context_pack_preflight"]["snapshot"]["open_questions"],
            ["Confirm totals."],
        )
        self.assertEqual(
            config["mailbox_memory_context_pack_preflight"]["snapshot"]["summary_text"],
            "Hot summary from snapshot",
        )

    def test_inject_latest_hot_state_overlays_context_pack_and_intelligence(self) -> None:
        hot = _hot_state(summary="Injected summary")
        mailbox_memory_result = {
            "enabled": True,
            "case_id": "case-1",
            "context_pack": {
                "case_id": "case-1",
                "snapshot": {"open_questions": ["legacy question"]},
            },
        }
        case_intelligence_result = {"case_understanding": {"summary_short": "legacy"}, "execution_metadata": {}}

        merged_mailbox, merged_intelligence = inject_latest_hot_state_for_resolved_case(
            mailbox_memory_result=mailbox_memory_result,
            case_intelligence_result=case_intelligence_result,
            mailbox_memory_runtime=_runtime_with_hot_state(hot),
        )

        self.assertEqual(
            merged_mailbox["context_pack"]["snapshot"]["open_questions"],
            ["Confirm totals."],
        )
        self.assertEqual(merged_mailbox["case_snapshot_hot_state"]["snapshot_id"], "snap-1")
        self.assertEqual(
            merged_intelligence["case_understanding"]["summary_short"],
            "Injected summary",
        )

    def test_run_business_reasoning_prefers_preflight_context_bundle(self) -> None:
        with patch("gmail_intake.run_shadow_business_reasoning", return_value={"ok": True}) as mocked:
            result = run_business_reasoning(
                snapshot={
                    "source_message": {"sender": "client@example.com", "subject": "Test"},
                    "thread_context_quality": "weak",
                },
                intake_result={"decision": {"action": "create_case"}, "business_area": "operations"},
                case_link_result={"decision": "linked"},
                context_bundle={"thread_context": {"quality": "weak"}},
                config={
                    "settings": object(),
                    "model": "test-model",
                    "verbose": False,
                    "preclassification_result": {"lane": "intake_llm"},
                    "lane_stage_plan": {"run_business_reasoning": True},
                    "mailbox_memory_context_pack_preflight": {
                        "case_id": "case-1",
                        "snapshot": {"open_questions": ["Confirm totals."]},
                    },
                },
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            mocked.call_args.kwargs["context_bundle"]["case_context_pack"]["snapshot"]["open_questions"],
            ["Confirm totals."],
        )

    def test_draft_reply_prefers_preflight_context_bundle(self) -> None:
        with patch("gmail_intake.run_shadow_reply_drafter", return_value={"draft_enabled": False, "drafts": []}) as mocked:
            result = draft_reply(
                snapshot={
                    "source_message": {"sender": "client@example.com", "subject": "Test"},
                    "thread_context_quality": "weak",
                },
                intake_result={"decision": {"action": "create_case"}, "business_area": "operations"},
                business_result={"recommended_next_action": "reply", "reply_recommended": True},
                context_bundle={"thread_context": {"quality": "weak"}},
                config={
                    "settings": object(),
                    "model": "test-model",
                    "verbose": False,
                    "preclassification_result": {"lane": "intake_llm"},
                    "lane_stage_plan": {"run_reply_drafter": True},
                    "case_link_result": {"decision": "linked"},
                    "mailbox_memory_context_pack_preflight": {
                        "case_id": "case-1",
                        "snapshot": {"open_questions": ["Confirm totals."]},
                    },
                },
            )

        self.assertFalse(result["draft_enabled"])
        self.assertEqual(
            mocked.call_args.kwargs["context_bundle"]["case_context_pack"]["snapshot"]["open_questions"],
            ["Confirm totals."],
        )

    def test_plan_actions_prefers_preflight_context_pack(self) -> None:
        result = plan_actions(
            intake_result={"decision": {"action": "create_task"}, "review_required": False},
            case_link_result={"decision": "linked"},
            business_result={"recommended_next_action": "wait", "missing_information": [], "urgency": "normal"},
            reply_result={"draft_enabled": False},
            config={
                "mailbox_memory_context_pack_preflight": {
                    "case_id": "case-1",
                    "snapshot": {"open_questions": ["Confirm totals."]},
                }
            },
        )

        self.assertIn("resolve open question: Confirm totals.", result["operator_checklist"])

    def test_case_intelligence_layer_prefers_preflight_hot_state(self) -> None:
        fixture = run_fixture("new_lead")
        hot = _hot_state(summary="Hot summary from preflight")
        result = build_case_intelligence_layer(
            snapshot=fixture["snapshot"],
            intake_result=fixture["intake_result"],
            case_link_result=fixture["case_link_result"],
            business_result=fixture["business_result"],
            reply_result=fixture["reply_result"],
            action_plan_result=fixture["action_plan"],
            config={
                "mailbox_memory_context_pack_preflight": {
                    "case_id": "case-1",
                    "snapshot": {"open_questions": ["Confirm totals."]},
                },
                "case_snapshot_hot_state_preflight": hot,
            },
        )

        self.assertEqual(result["case_understanding"]["summary_short"], "Hot summary from preflight")
        self.assertIn("Mailbox memory: Confirm totals.", result["case_understanding"]["blockers"])


if __name__ == "__main__":
    unittest.main()
