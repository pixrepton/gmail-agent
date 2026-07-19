"""Shared helpers for fixture-driven Gmail Intake v2 checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_planner import plan_actions
from attachment_intelligence import build_attachment_intelligence
from business_context import build_business_context_bundle
from case_linker import link_case
from case_intelligence import build_case_intelligence
from confidence_review import apply_confidence_to_intelligence, build_confidence_domains, route_review
from dash_preview import build_dash_preview
from thread_memory import build_thread_memory
from dash_projection_v2 import build_v2_shadow_projection
from intake_payload import build_source_snapshot
from intake_schema import (
    validate_business_reasoning_result,
    validate_intake_result,
    validate_reply_draft_result,
)
from preclassifier import preclassify_snapshot


FIXTURES_DIR = TOOL_DIR / "fixtures"
MESSAGES_DIR = FIXTURES_DIR / "messages"
EXPECTED_DIR = FIXTURES_DIR / "expected"


def fixture_names() -> list[str]:
    return sorted(path.stem for path in MESSAGES_DIR.glob("*.json"))


def load_fixture(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    message_payload = json.loads((MESSAGES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    expected_payload = json.loads((EXPECTED_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return message_payload, expected_payload


def build_fixture_snapshot(message_payload: dict[str, Any]) -> dict[str, Any]:
    return build_source_snapshot(
        mailbox=str(message_payload.get("mailbox") or "ops@topinstal.local"),
        source_message=message_payload.get("source_message") or {},
        context_messages=message_payload.get("context_messages") or [],
        observed_at=str((message_payload.get("source_message") or {}).get("date") or ""),
    )


def run_fixture(name: str) -> dict[str, Any]:
    message_payload, expected = load_fixture(name)
    snapshot = build_fixture_snapshot(message_payload)
    preclassification = preclassify_snapshot(snapshot)
    intake_candidate = build_fixture_intake_candidate(snapshot, expected["intake"])
    intake_result = validate_intake_result(intake_candidate, final_output_origin="raw_valid")
    case_link_result = link_case(snapshot, intake_result, {})
    business_result = validate_business_reasoning_result(expected["business_reasoning"])
    reply_result = validate_reply_draft_result(expected["reply_draft"])
    action_plan = plan_actions(intake_result, case_link_result, business_result, reply_result)
    att_intel = build_attachment_intelligence(snapshot, intake_result=intake_result, case_link_result=case_link_result)
    thread_mem = build_thread_memory(snapshot, intake_result=intake_result, case_link_result=case_link_result, business_result=business_result)
    case_intelligence = build_case_intelligence(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result,
        business_result=business_result,
        reply_result=reply_result,
        action_plan_result=action_plan,
        feedback_memory_seed=message_payload.get("feedback_memory_seed") or expected.get("feedback_memory_seed"),
        current_note_state=message_payload.get("current_note_state") or expected.get("current_note_state"),
        attachment_intelligence=att_intel,
        thread_memory=thread_mem,
    )
    conf_domains = build_confidence_domains(
        intake_result=intake_result,
        case_link_result=case_link_result,
        business_result=business_result,
        attachment_intelligence=att_intel,
        thread_memory=thread_mem,
        action_plan_result=action_plan,
        case_intelligence_result=case_intelligence,
    )
    review_routing = route_review(conf_domains, intake_result=intake_result, case_intelligence_result=case_intelligence)
    case_intelligence = apply_confidence_to_intelligence(case_intelligence, confidence_domains=conf_domains, review_routing=review_routing)
    case_intelligence["attachment_intelligence"] = att_intel
    case_intelligence["thread_memory"] = thread_mem
    preview = build_dash_preview(
        intake_result,
        stage_outputs={
            "intake_result_final": intake_result,
            "preclassification_result": preclassification,
            "case_link_result": case_link_result,
            "business_reasoning_result": business_result,
            "reply_draft_result": reply_result,
            "action_plan_result": action_plan,
            "case_intelligence_result": case_intelligence,
        },
    )
    v2_projection = build_v2_shadow_projection(
        intake_result,
        run_id=f"fixture:{name}",
        stage_outputs={
            "intake_result_final": intake_result,
            "preclassification_result": preclassification,
            "case_link_result": case_link_result,
            "business_reasoning_result": business_result,
            "reply_draft_result": reply_result,
            "action_plan_result": action_plan,
            "case_intelligence_result": case_intelligence,
        },
    )
    business_context_bundle = build_business_context_bundle(snapshot, intake_result, case_link_result)
    return {
        "snapshot": snapshot,
        "expected": expected,
        "preclassification": preclassification,
        "intake_result": intake_result,
        "case_link_result": case_link_result,
        "business_result": business_result,
        "reply_result": reply_result,
        "action_plan": action_plan,
        "case_intelligence": case_intelligence,
        "preview": preview,
        "v2_projection": v2_projection,
        "business_context_bundle": business_context_bundle,
    }


def build_fixture_intake_candidate(snapshot: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    source_message = snapshot.get("source_message") or {}
    reference_tokens = source_message.get("reference_tokens") or {}
    if not isinstance(reference_tokens, dict):
        reference_tokens = {}
    linked_case_candidates = [
        {
            "case_key": str(item.get("case_key") or "").strip(),
            "case_type": str(item.get("case_type") or "").strip() or "message_context",
            "match_confidence": float(item.get("match_confidence") or 0.0),
        }
        for item in (snapshot.get("case_link_candidates") or [])[:3]
        if isinstance(item, dict) and str(item.get("case_key") or "").strip()
    ]
    linked_case_candidates.extend(spec.get("linked_case_candidates") or [])
    confidence = spec.get("confidence") or {}

    candidate = {
        "schema_version": "1.0",
        "source": {
            "channel": "gmail",
            "mailbox": str(snapshot.get("mailbox") or ""),
            "observed_at": str(snapshot.get("observed_at") or ""),
        },
        "message": {
            "message_id": str(source_message.get("message_id") or ""),
            "date": str(source_message.get("date") or ""),
            "sender": str(source_message.get("sender") or ""),
            "to": list(source_message.get("to") or []),
            "cc": list(source_message.get("cc") or []),
            "subject": str(source_message.get("subject") or ""),
            "snippet": str(source_message.get("snippet") or ""),
            "has_attachments": bool(source_message.get("has_attachments")),
            "labels": list(source_message.get("labels") or []),
        },
        "thread": {
            "thread_id": str(source_message.get("thread_id") or ""),
            "thread_position": str(source_message.get("thread_position_hint") or "unknown"),
            "is_reply_or_forward": bool(source_message.get("is_reply_or_forward_hint")),
            "thread_summary": spec.get("thread_summary")
            or "; ".join(str(item).strip() for item in (snapshot.get("thread_context") or {}).get("reasons", []) if str(item).strip())
            or "Fixture thread summary.",
            "linked_case_candidates": linked_case_candidates,
        },
        "business_area": str(spec["business_area"]),
        "primary_signal": {
            "code": str(spec["primary_signal_code"]),
            "name": str(spec["primary_signal_name"]),
            "description": str(spec.get("primary_signal_description") or spec["primary_signal_name"]),
            "business_significance": str(spec.get("business_significance") or spec["reason"]),
        },
        "secondary_signals": list(spec.get("secondary_signals") or []),
        "case_assessment": {
            "case_family": str(spec["case_family"]),
            "is_new_case": bool(spec.get("is_new_case", False)),
            "state_detected": str(spec.get("state_detected") or "none"),
            "state_change": spec.get("state_change") or {"detected": False},
        },
        "decision": {
            "action": str(spec["decision_action"]),
            "action_rationale": str(spec["action_rationale"]),
        },
        "priority": str(spec["priority"]),
        "confidence": {
            "signal_confidence": float(confidence.get("signal_confidence") or 0.75),
            "case_link_confidence": float(confidence.get("case_link_confidence") or 0.0),
            "decision_confidence": float(confidence.get("decision_confidence") or 0.75),
            "extraction_confidence": float(confidence.get("extraction_confidence") or 0.65),
        },
        "review": {
            "required": bool(spec.get("review_required", False)),
            "flags": list(spec.get("review_flags") or []),
        },
        "reason": str(spec["reason"]),
        "extracted_data": {
            "entities": {
                "people": list(spec.get("entities", {}).get("people") or []),
                "organizations": list(spec.get("entities", {}).get("organizations") or []),
                "locations": list(spec.get("entities", {}).get("locations") or []),
                "products": list(spec.get("entities", {}).get("products") or []),
            },
            "dates": list(spec.get("dates") or []),
            "amounts": list(spec.get("amounts") or []),
            "references": {
                "invoice_numbers": list(spec.get("references", {}).get("invoice_numbers") or reference_tokens.get("invoice") or []),
                "shipment_numbers": list(spec.get("references", {}).get("shipment_numbers") or reference_tokens.get("shipment") or []),
                "order_numbers": list(spec.get("references", {}).get("order_numbers") or reference_tokens.get("order") or []),
                "transaction_numbers": list(spec.get("references", {}).get("transaction_numbers") or reference_tokens.get("transaction") or []),
                "case_ids": list(spec.get("references", {}).get("case_ids") or reference_tokens.get("case") or []),
            },
            "deadlines": list(spec.get("deadlines") or []),
        },
    }
    return candidate


def assert_fixture_expectations(result: dict[str, Any]) -> None:
    expected = result["expected"]
    preview = result["preview"]
    metadata = preview.get("metadata") or {}

    if result["preclassification"]["lane"] != expected["preclassification_lane"]:
        raise AssertionError(f"preclassification lane mismatch: {result['preclassification']['lane']} != {expected['preclassification_lane']}")

    expected_case_link = expected["expected_case_link"]
    if result["case_link_result"]["decision"] != expected_case_link["decision"]:
        raise AssertionError(f"case-link decision mismatch: {result['case_link_result']['decision']} != {expected_case_link['decision']}")
    if result["case_link_result"]["selected_case_key"] != expected_case_link["selected_case_key"]:
        raise AssertionError(
            f"case-link key mismatch: {result['case_link_result']['selected_case_key']} != {expected_case_link['selected_case_key']}"
        )

    expected_action_plan = expected["action_plan"]
    if result["action_plan"]["primary_action"] != expected_action_plan["primary_action"]:
        raise AssertionError(f"action primary mismatch: {result['action_plan']['primary_action']} != {expected_action_plan['primary_action']}")
    if result["action_plan"]["daszek_projection_mode"] != expected_action_plan["daszek_projection_mode"]:
        raise AssertionError(
            "projection mode mismatch: "
            f"{result['action_plan']['daszek_projection_mode']} != {expected_action_plan['daszek_projection_mode']}"
        )
    if bool(result["action_plan"]["safe_for_live_push"]) != bool(expected_action_plan["safe_for_live_push"]):
        raise AssertionError("safe_for_live_push mismatch")
    if "safe_for_operator_projection" in expected_action_plan:
        if bool(result["action_plan"]["safe_for_operator_projection"]) != bool(
            expected_action_plan["safe_for_operator_projection"]
        ):
            raise AssertionError("safe_for_operator_projection mismatch")

    if bool(result["reply_result"]["draft_enabled"]) != bool(expected["reply_draft"]["draft_enabled"]):
        raise AssertionError("reply draft availability mismatch")
    if result["business_result"]["recommended_next_action"] != expected["business_reasoning"]["recommended_next_action"]:
        raise AssertionError("business recommended_next_action mismatch")
    if metadata.get("business_interpretation_summary", "") == "":
        raise AssertionError("preview business_interpretation_summary should not be empty")
    if metadata.get("operator_note", "") != result["business_result"]["operator_note"]:
        raise AssertionError("preview operator_note mismatch")
    if metadata.get("reply_draft_available") != bool(expected["reply_draft"]["draft_enabled"]):
        raise AssertionError("preview reply_draft_available mismatch")
    if metadata.get("recommended_next_action") != result["action_plan"]["primary_action"]:
        raise AssertionError("preview recommended_next_action mismatch")
    if preview.get("ignored") != (result["action_plan"]["daszek_projection_mode"] == "ignore"):
        raise AssertionError("preview ignored flag mismatch")
