"""P2 — causal observability for Brain1 draft path (gate / execution / postcheck)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from draft_path_observability import (
    SCHEMA_VERSION,
    attach_causal_observability,
    build_causal_observability,
    classify_drafter_failure,
    derive_draft_path_outcome,
    evaluate_draft_gate,
    evaluate_draft_postcheck,
    evaluate_drafter_execution,
)
from redaction import sanitize_for_storage
from reply_drafter import annotate_reply_causal_observability, should_draft_reply


def _snapshot(*, sender: str = "anna@example.com", message_id: str = "msg-1") -> dict[str, Any]:
    return {
        "snapshot_version": "v1",
        "source_message": {"sender": sender, "message_id": message_id},
    }


def _intake(*, action: str = "create_case", review_required: bool = False) -> dict[str, Any]:
    return {"decision": {"action": action}, "review_required": review_required}


def _business(
    *,
    next_action: str = "escalate_review",
    reply_recommended: bool = False,
) -> dict[str, Any]:
    return {
        "recommended_next_action": next_action,
        "reply_recommended": reply_recommended,
        "human_review_bias": "low",
        "missing_information": ["adres"],
        "risks": [],
        "urgency": "normal",
        "confidence": {"business_confidence": 0.7, "action_confidence": 0.6},
        "assumptions": [],
        "unsupported_claims": [],
        "conflict_refs": [],
        "execution_metadata": {
            "stage_name": "business_reasoning",
            "central_llm_provider": "groq",
            "model_name": "test-model",
            "attempt_count": 1,
            "fallback_used": False,
            "latency_ms": 12,
            "parse_status": "pydantic_validated",
        },
    }


class DraftGateRunTests(unittest.TestCase):
    def test_a_reply_recommended_runs_gate(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(review_required=False),
            _business(next_action="escalate_review", reply_recommended=True),
        )
        self.assertEqual(gate["decision"], "RUN")
        self.assertEqual(gate["primary_reason_code"], "BR_REPLY_RECOMMENDED")
        self.assertTrue(gate["decision_inputs"]["reply_recommended"])
        self.assertIn("input_identity", gate)
        self.assertTrue(str(gate["input_identity"]).startswith("sha256:"))

        assembled = build_causal_observability(
            snapshot=_snapshot(),
            intake_result=_intake(review_required=False),
            business_result=_business(reply_recommended=True),
            gate=gate,
            execution=evaluate_drafter_execution(status="SUCCESS", draft_present=True),
            postcheck=evaluate_draft_postcheck(
                {"draft_enabled": True, "drafts": [{"body": "Dzien dobry"}], "do_not_send_reasons": []},
                execution_status="SUCCESS",
            ),
        )
        self.assertEqual(assembled["drafter_execution"]["status"], "SUCCESS")
        self.assertNotEqual(assembled["drafter_execution"]["status"], "NOT_STARTED")
        self.assertEqual(assembled["draft_path_outcome"], "DRAFT_ACCEPTED")


class DraftGateSkipTests(unittest.TestCase):
    def test_b_review_required_escalate_skips_and_does_not_start_drafter(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(review_required=True),
            _business(next_action="escalate_review", reply_recommended=True),
        )
        self.assertEqual(gate["decision"], "SKIP")
        self.assertEqual(
            gate["primary_reason_code"],
            "REVIEW_REQUIRED_WITHOUT_REPLY_OR_COLLECT_DATA",
        )
        assembled = build_causal_observability(
            snapshot=_snapshot(),
            intake_result=_intake(review_required=True),
            business_result=_business(reply_recommended=True),
            gate=gate,
        )
        self.assertEqual(assembled["drafter_execution"]["status"], "NOT_STARTED")
        self.assertEqual(assembled["draft_postcheck"]["decision"], "NOT_APPLICABLE")
        self.assertEqual(assembled["draft_path_outcome"], "SKIPPED_PRE_DRAFTER")
        self.assertEqual(
            assembled["first_terminal_reason_code"],
            "REVIEW_REQUIRED_WITHOUT_REPLY_OR_COLLECT_DATA",
        )


class DrafterExecutionFailureTests(unittest.TestCase):
    def test_c_provider_failure_is_not_pre_drafter_skip(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(review_required=False),
            _business(next_action="reply", reply_recommended=True),
        )
        self.assertEqual(gate["decision"], "RUN")
        execution = evaluate_drafter_execution(
            status="PROVIDER_FAILURE",
            draft_present=False,
            reason_code="CENTRAL_LLM_STAGE_UNAVAILABLE",
        )
        assembled = build_causal_observability(
            snapshot=_snapshot(),
            intake_result=_intake(review_required=False),
            business_result=_business(next_action="reply", reply_recommended=True),
            gate=gate,
            execution=execution,
        )
        self.assertEqual(assembled["drafter_execution"]["status"], "PROVIDER_FAILURE")
        self.assertEqual(assembled["draft_path_outcome"], "DRAFTER_FAILED")
        self.assertNotEqual(assembled["draft_path_outcome"], "SKIPPED_PRE_DRAFTER")
        self.assertEqual(assembled["first_terminal_reason_code"], "CENTRAL_LLM_STAGE_UNAVAILABLE")
        self.assertEqual(assembled["draft_postcheck"]["decision"], "NOT_APPLICABLE")


class DraftPostcheckTests(unittest.TestCase):
    def test_d_empty_body_sanity_blocks_after_successful_execution(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(review_required=False),
            _business(next_action="reply", reply_recommended=True),
        )
        parsed = {
            "draft_enabled": False,
            "drafts": [{"body": "   "}],
            "do_not_send_reasons": ["draft_sanity:empty_body"],
            "requires_manual_edit": True,
        }
        postcheck = evaluate_draft_postcheck(parsed, execution_status="SUCCESS")
        self.assertEqual(postcheck["decision"], "BLOCK")
        assembled = build_causal_observability(
            snapshot=_snapshot(),
            intake_result=_intake(review_required=False),
            business_result=_business(next_action="reply"),
            gate=gate,
            execution=evaluate_drafter_execution(status="SUCCESS", draft_present=True),
            postcheck=postcheck,
        )
        self.assertEqual(assembled["draft_path_outcome"], "DRAFT_BLOCKED_POSTCHECK")
        self.assertIn("draft_sanity:empty_body", assembled["draft_postcheck"]["reason_codes"])


class ReviewControlTests(unittest.TestCase):
    def test_e_escalate_plus_reply_recommended_runs_when_review_not_required(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(review_required=False),
            _business(next_action="escalate_review", reply_recommended=True),
        )
        self.assertEqual(gate["decision"], "RUN")
        self.assertEqual(gate["primary_reason_code"], "BR_REPLY_RECOMMENDED")

    def test_e_escalate_plus_reply_recommended_skips_when_review_required(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(review_required=True),
            _business(next_action="escalate_review", reply_recommended=True),
        )
        self.assertEqual(gate["decision"], "SKIP")
        self.assertEqual(
            gate["primary_reason_code"],
            "REVIEW_REQUIRED_WITHOUT_REPLY_OR_COLLECT_DATA",
        )


class LineageTests(unittest.TestCase):
    def test_final_record_links_br_and_gate_from_same_execution(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(),
            _business(reply_recommended=True),
        )
        assembled = build_causal_observability(
            snapshot=_snapshot(),
            intake_result=_intake(),
            business_result=_business(reply_recommended=True),
            gate=gate,
            lineage={
                "run_id": "run-1",
                "case_id": "INT-01",
                "engagement_id": "eng-1",
                "message_id": "msg-1",
                "snapshot_version": "v1",
            },
        )
        self.assertEqual(assembled["schema_version"], SCHEMA_VERSION)
        br = assembled["business_reasoning"]
        gate_rec = assembled["draft_gate"]
        self.assertEqual(br["stage"], "BUSINESS_REASONING")
        self.assertEqual(gate_rec["stage"], "DRAFT_GATE")
        self.assertEqual(gate_rec["parent_stage_record_id"], br["stage_record_id"])
        self.assertEqual(br["lineage"]["run_id"], "run-1")
        self.assertEqual(gate_rec["lineage"]["case_id"], "INT-01")
        self.assertEqual(gate_rec["lineage"]["message_id"], "msg-1")
        self.assertEqual(br["recommended_next_action"], "escalate_review")
        self.assertTrue(br["reply_recommended"])


class SecretAndWriteFailTests(unittest.TestCase):
    def test_secret_bearing_fields_are_redacted_in_persisted_payload(self) -> None:
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(),
            _business(reply_recommended=True),
        )
        assembled = build_causal_observability(
            snapshot=_snapshot(),
            intake_result=_intake(),
            business_result=_business(reply_recommended=True),
            gate=gate,
            extra={"api_key": "gsk_secretvalue", "note": "Bearer abc.def.ghi"},
        )
        stored = sanitize_for_storage(assembled)
        blob = str(stored)
        self.assertNotIn("gsk_secretvalue", blob)
        self.assertNotIn("Bearer abc.def.ghi", blob)
        self.assertIn("<redacted>", blob)

    def test_observability_write_failure_does_not_change_product_result(self) -> None:
        product = {
            "draft_enabled": False,
            "drafts": [],
            "do_not_send_reasons": ["reply_not_recommended"],
        }
        original = dict(product)

        def _boom(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("disk full")

        with patch("draft_path_observability.build_causal_observability", side_effect=_boom):
            attached = attach_causal_observability(
                product,
                snapshot=_snapshot(),
                intake_result=_intake(review_required=True),
                business_result=_business(),
            )
        self.assertEqual(attached["draft_enabled"], original["draft_enabled"])
        self.assertEqual(attached["drafts"], original["drafts"])
        self.assertEqual(attached["do_not_send_reasons"], original["do_not_send_reasons"])
        self.assertTrue(attached["observability_write_failed"])
        self.assertNotIn("causal_observability", attached)


class OutcomeDerivationTests(unittest.TestCase):
    def test_outcome_is_derived_not_classified_by_llm(self) -> None:
        outcome = derive_draft_path_outcome(
            gate={"decision": "SKIP", "primary_reason_code": "INTAKE_ACTION_IGNORE"},
            execution={"status": "NOT_STARTED"},
            postcheck={"decision": "NOT_APPLICABLE"},
        )
        self.assertEqual(outcome["draft_path_outcome"], "SKIPPED_PRE_DRAFTER")
        self.assertEqual(outcome["first_terminal_reason_code"], "INTAKE_ACTION_IGNORE")


class WiringParityTests(unittest.TestCase):
    def test_should_draft_reply_matches_gate_boolean(self) -> None:
        cases = [
            (_snapshot(), _intake(review_required=False), _business(reply_recommended=True)),
            (_snapshot(), _intake(review_required=True), _business(next_action="escalate_review")),
            (_snapshot(), _intake(review_required=False), _business(next_action="escalate_review", reply_recommended=True)),
            (_snapshot(sender="noreply@example.com"), _intake(), _business(reply_recommended=True)),
            (_snapshot(), _intake(action="ignore"), _business(next_action="reply", reply_recommended=True)),
        ]
        for snapshot, intake, business in cases:
            gate = evaluate_draft_gate(snapshot, intake, business)
            self.assertEqual(
                should_draft_reply(snapshot, intake, business),
                gate["decision"] == "RUN",
            )

    def test_annotate_preserves_product_fields_and_separates_execution(self) -> None:
        product = {
            "draft_enabled": False,
            "drafts": [],
            "do_not_send_reasons": ["central_llm_stage_unavailable"],
            "skipped": "drive_signal",
        }
        attached = annotate_reply_causal_observability(
            product,
            snapshot=_snapshot(),
            intake_result=_intake(),
            business_result=_business(reply_recommended=True),
            skip_draft_reply=True,
        )
        self.assertFalse(attached["draft_enabled"])
        self.assertEqual(attached["drafts"], [])
        self.assertEqual(attached["do_not_send_reasons"], ["central_llm_stage_unavailable"])
        self.assertEqual(attached["skipped"], "drive_signal")
        causal = attached["causal_observability"]
        self.assertEqual(causal["draft_gate"]["decision"], "SKIP")
        self.assertEqual(causal["draft_gate"]["primary_reason_code"], "DRIVE_SIGNAL_SKIP")
        self.assertEqual(causal["drafter_execution"]["status"], "NOT_STARTED")
        self.assertEqual(causal["draft_path_outcome"], "SKIPPED_PRE_DRAFTER")

    def test_provider_failure_is_not_pre_drafter_skip(self) -> None:
        status, code = classify_drafter_failure(reason="central_llm_stage_unavailable")
        self.assertEqual(status, "PROVIDER_FAILURE")
        self.assertEqual(code, "CENTRAL_LLM_STAGE_UNAVAILABLE")
        gate = evaluate_draft_gate(
            _snapshot(),
            _intake(),
            _business(reply_recommended=True),
        )
        execution = evaluate_drafter_execution(
            status=status,
            reason_code=code,
            fallback_used=True,
        )
        outcome = derive_draft_path_outcome(gate=gate, execution=execution)
        self.assertEqual(outcome["draft_path_outcome"], "DRAFTER_FAILED")
        self.assertNotEqual(outcome["draft_path_outcome"], "SKIPPED_PRE_DRAFTER")

    def test_pydantic_failed_with_recovered_body_is_success(self) -> None:
        from draft_path_observability import execution_from_stage_call

        execution = execution_from_stage_call(
            {"parse_status": "pydantic_failed", "central_llm_provider": "groq"},
            {"drafts": [{"body": "Dzien dobry, sprawdzimy instalacje."}]},
        )
        self.assertEqual(execution["status"], "SUCCESS")
        self.assertTrue(execution["draft_present"])

        failed = execution_from_stage_call(
            {"parse_status": "pydantic_failed"},
            {"drafts": []},
        )
        self.assertEqual(failed["status"], "VALIDATION_FAILURE")


if __name__ == "__main__":
    unittest.main()
