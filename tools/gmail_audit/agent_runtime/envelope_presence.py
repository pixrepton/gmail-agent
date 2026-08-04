"""Policy envelope presence classification (PLANNER-EXEC-FIDELITY-01).

Separates expected absence (benchmark/harness without Brain1 spine, dry paths)
from wiring failures (Understanding present but envelope missing).
"""

from __future__ import annotations

from typing import Any, Literal

from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1

EnvelopePresence = Literal[
    "present_current",
    "present_stale",
    "expected_absence",
    "wiring_failure",
    "store_unavailable",
]


def classify_envelope_presence(
    envelope: PolicyActionEnvelopeV1 | None,
    *,
    case_understanding_present: bool = False,
    policy_required: bool = False,
    harness_mode: bool = False,
) -> dict[str, Any]:
    """Classify why a policy_action_envelope is present or absent."""
    if envelope is not None and envelope.freshness == "current":
        return {
            "status": "present_current",
            "expected": True,
            "wiring_ok": True,
            "reason_codes": [],
            "policy_decision_id": envelope.policy_decision_id,
            "action_proposal_id": envelope.action_proposal_id,
            "decision_candidate_id": envelope.decision_candidate_id,
            "source_signal_id": envelope.source_signal_id,
        }
    if envelope is not None and envelope.freshness == "stale":
        return {
            "status": "present_stale",
            "expected": True,
            "wiring_ok": True,
            "reason_codes": list(envelope.reason_codes or []),
            "policy_decision_id": envelope.policy_decision_id,
            "action_proposal_id": envelope.action_proposal_id,
            "decision_candidate_id": envelope.decision_candidate_id,
            "source_signal_id": envelope.source_signal_id,
        }

    reasons: list[str] = []
    if envelope is not None:
        reasons.extend(str(r) for r in (envelope.reason_codes or [])[:8])
    else:
        reasons.append("policy_action_envelope_absent")

    store_reasons = {
        "canonical_action_proposal_v2_store_unavailable",
        "canonical_action_proposal_v2_not_found",
        "canonical_policy_decision_not_found",
    }
    if any(r in store_reasons for r in reasons) and not case_understanding_present:
        return {
            "status": "store_unavailable",
            "expected": not policy_required,
            "wiring_ok": False,
            "reason_codes": reasons,
            "policy_decision_id": "",
            "action_proposal_id": "",
            "decision_candidate_id": "",
            "source_signal_id": "",
        }

    if harness_mode and not policy_required:
        return {
            "status": "expected_absence",
            "expected": True,
            "wiring_ok": True,
            "reason_codes": reasons + ["harness_skips_policy_spine"],
            "policy_decision_id": "",
            "action_proposal_id": "",
            "decision_candidate_id": "",
            "source_signal_id": "",
        }

    if case_understanding_present or policy_required:
        return {
            "status": "wiring_failure",
            "expected": False,
            "wiring_ok": False,
            "reason_codes": reasons + ["policy_envelope_required_after_brain1"],
            "policy_decision_id": "",
            "action_proposal_id": "",
            "decision_candidate_id": "",
            "source_signal_id": "",
        }

    return {
        "status": "expected_absence",
        "expected": True,
        "wiring_ok": True,
        "reason_codes": reasons,
        "policy_decision_id": "",
        "action_proposal_id": "",
        "decision_candidate_id": "",
        "source_signal_id": "",
    }


def policy_path_requires_envelope(
    snapshot: Any,
    signal_payload: dict[str, Any] | None = None,
) -> bool:
    """True when this planner run is semantically post-Brain1 / policy-gated."""
    payload = signal_payload if isinstance(signal_payload, dict) else {}
    if payload.get("policy_required") is True:
        return True
    if payload.get("harness_mode") is True and payload.get("policy_required") is not True:
        return False
    if getattr(snapshot, "case_understanding", None) is not None:
        return True
    if isinstance(payload.get("case_understanding_projection"), dict):
        return True
    if isinstance(payload.get("case_intelligence_result"), dict):
        return True
    return False


__all__ = [
    "EnvelopePresence",
    "classify_envelope_presence",
    "policy_path_requires_envelope",
]
