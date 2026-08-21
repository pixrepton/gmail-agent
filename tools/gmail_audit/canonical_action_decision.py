"""CanonicalActionDecision (CAD) — deterministic semantic authority boundary.

P0 of ``AI-OS INTELLIGENCE SPINE — CONTRACT + FIRST ENFORCED SLICE``.
Contract owner: ``knowledge/docs/AI_OS_INTELLIGENCE_SPINE_CONTRACT.md``
(§CanonicalActionDecision).

Core invariant: once a CAD is created for goal/action_type/target/channel, no
further layer may change those four fields. Downstream may execute, restrict,
block, or request an explicit revision (``DecisionRevisionRequest``), but must
not reinterpret the decision.

Lifecycle split:

```text
BusinessDecisionProposal
  -> CanonicalizationFailure        # BEFORE CAD exists; no decision_id yet
  -> NEEDS_REVIEW                   # workflow state, never a new action_type

CanonicalActionDecision (FROZEN)
  -> DecisionRevisionRequest        # AFTER CAD exists; carries decision_id
  -> decision owner -> new CAD
```

This module is deliberately narrow: the first enforced slice is
``ask_for_missing_data / customer / mail``. Other action classes are added
only after the pattern is proven on this slice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

CANONICAL_ACTION_DECISION_SCHEMA_VERSION = "canonical_action_decision.v1"
BUSINESS_DECISION_PROPOSAL_SCHEMA_VERSION = "business_decision_proposal.v1"
CANONICALIZATION_FAILURE_SCHEMA_VERSION = "canonicalization_failure.v1"
DECISION_REVISION_REQUEST_SCHEMA_VERSION = "decision_revision_request.v1"

# Canonical vocabularies. The first slice is intentionally narrow; each new
# action class is added with its own legal channel/target rules and tests.
CANONICAL_GOALS = ("obtain_missing_service_information",)
CANONICAL_ACTION_TYPES = ("ask_for_missing_data",)
CANONICAL_TARGETS = ("customer", "operator", "supplier", "internal", "none")
CANONICAL_CHANNELS = ("mail", "phone", "internal", "none")

# Legal channels per canonical action_type (mirrors the case_intelligence
# ACTION_CHANNEL table for the slice; no channel may be invented downstream).
ACTION_TYPE_CHANNELS: dict[str, tuple[str, ...]] = {
    "ask_for_missing_data": ("mail",),
}

# Default target when a proposal omits it (the slice has exactly one mapping).
ACTION_TYPE_DEFAULT_TARGET: dict[str, str] = {
    "ask_for_missing_data": "customer",
}

# CanonicalizationFailure reason codes (deterministic, machine-readable).
FAILURE_REASON_CODES = (
    "proposal_incomplete",
    "action_type_not_in_contract",
    "goal_not_in_contract",
    "target_not_in_contract",
    "channel_not_in_contract",
    "channel_illegal_for_action_type",
    "required_information_empty",
    "required_information_not_in_state",
    "missing_information_unavailable",
    "conflicted_fact_as_certainty",
)

# DecisionRevisionRequest reason codes (P0 contract only; full handling in P1).
REVISION_REASON_CODES = (
    "NEW_CONFLICTING_EVIDENCE",
    "IMPOSSIBLE_PRECONDITION",
    "OUT_OF_SCOPE_REQUEST",
)


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, round(number, 4)))


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _string(value: Any, default: str = "") -> str:
    return str(value or "").strip() or default


def _canonical_payload(
    *,
    schema_version: str,
    case_id: str,
    situation_version: str,
    goal: str,
    action_type: str,
    target: str,
    channel: str,
    required_information: list[str],
) -> str:
    """Canonical JSON for the semantic hash.

    Deliberately excludes created_at, rationale, confidence and presentation
    fields: identity of the semantic signature must not depend on them.
    required_information is sorted so ordering does not change the hash.
    """
    payload = {
        "schema_version": schema_version,
        "case_id": _string(case_id),
        "situation_version": _string(situation_version),
        "goal": _string(goal),
        "action_type": _string(action_type),
        "target": _string(target),
        "channel": _string(channel),
        "required_information": sorted(_as_list(required_information)),
        "semantic_status": "FROZEN",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def semantic_hash_of(
    *,
    schema_version: str = CANONICAL_ACTION_DECISION_SCHEMA_VERSION,
    case_id: str = "",
    situation_version: str = "",
    goal: str = "",
    action_type: str = "",
    target: str = "",
    channel: str = "",
    required_information: list[str] | None = None,
) -> str:
    """SHA256 of the canonical semantic payload (Semantic Conservation basis)."""
    canonical = _canonical_payload(
        schema_version=schema_version,
        case_id=case_id,
        situation_version=situation_version,
        goal=goal,
        action_type=action_type,
        target=target,
        channel=channel,
        required_information=list(required_information or []),
    )
    return "sh_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _proposal_id() -> str:
    return "bprop_" + uuid.uuid4().hex[:22]


def _decision_id() -> str:
    return "dec_" + uuid.uuid4().hex[:22]


def _risk_class_from_business(business_result: dict[str, Any]) -> str:
    urgency = str(business_result.get("urgency") or "").strip().lower()
    if urgency in {"high", "critical"}:
        return "high"
    if urgency in {"medium"}:
        return "medium"
    return "low"


def build_business_decision_proposal(
    business_reasoning_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Derive a typed BusinessDecisionProposal from the existing BR result.

    P0 supports exactly one proposal class: ``collect_data`` ->
    ``ask_for_missing_data / customer / mail``. Other BR actions return None
    (the legacy path keeps working unchanged; there is no proposal to
    canonicalize and therefore no CanonicalizationFailure).

    ``BusinessReasoningResult`` itself is NOT extended (consumer inventory:
    ~80 files touch the recommendation surface).
    """
    br = business_reasoning_result if isinstance(business_reasoning_result, dict) else {}
    action = _string(br.get("recommended_next_action"))
    if action != "collect_data":
        return None

    missing = _as_list(br.get("missing_information"))
    confidence = 0.0
    conf = br.get("confidence")
    if isinstance(conf, dict):
        confidence = _as_float(conf.get("action_confidence"))
    elif isinstance(conf, (int, float)):
        confidence = _as_float(conf)

    return {
        "schema_version": BUSINESS_DECISION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": _proposal_id(),
        "goal": "obtain_missing_service_information",
        "action_type": "ask_for_missing_data",
        "target": ACTION_TYPE_DEFAULT_TARGET["ask_for_missing_data"],
        "channel": "mail",
        "required_information": missing,
        "confidence": confidence,
        "reason": _string(br.get("recommended_action_reason")),
        "risk_class": _risk_class_from_business(br),
    }


def _state_missing_information(
    *,
    situation_understanding: dict[str, Any] | None,
    case_context_pack: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
) -> list[str]:
    """Collect the state's missing-information surface.

    SituationUnderstanding is state, not a decision owner: this function only
    proves that the proposal's required_information is supported by the state.
    """
    state: list[str] = []
    su = situation_understanding if isinstance(situation_understanding, dict) else {}
    state.extend(_as_list(su.get("missing_information")))
    state.extend(_as_list(su.get("missing_critical_fields")))
    state.extend(_as_list(su.get("required_information")))

    pack = case_context_pack if isinstance(case_context_pack, dict) else {}
    for row in pack.get("completeness_gaps") or []:
        if isinstance(row, dict):
            label = _string(row.get("label"), _string(row.get("field_name"), _string(row.get("fact_key"))))
            if label:
                state.append(label)

    intake = intake_result if isinstance(intake_result, dict) else {}
    state.extend(_as_list(intake.get("missing_information")))
    return _dedupe(state)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _conflicted_fact_keys(case_context_pack: dict[str, Any] | None) -> set[str]:
    """Keys whose live value set is conflicted/uncertain (decision_usable=False)."""
    pack = case_context_pack if isinstance(case_context_pack, dict) else {}
    keys: set[str] = set()
    for row in pack.get("conflicting_facts") or []:
        if not isinstance(row, dict):
            continue
        key = _string(row.get("fact_key"), _string(row.get("key")))
        if not key:
            continue
        decision_usable = row.get("decision_usable")
        trust_state = _string(row.get("trust_state"))
        if decision_usable is False or trust_state == "conflicted":
            keys.add(key.lower())
    return keys


def _proposal_failure(proposal: dict[str, Any], reason_codes: list[str], failed_precondition: str = "") -> dict[str, Any]:
    return {
        "schema_version": CANONICALIZATION_FAILURE_SCHEMA_VERSION,
        "proposal_id": _string(proposal.get("proposal_id")),
        "reason_codes": list(reason_codes),
        "failed_precondition": failed_precondition or (reason_codes[0] if reason_codes else ""),
        "occurred_at": _utc(),
        "decision_state": "NO_CANONICAL_DECISION",
    }


def canonicalize(
    *,
    proposal: dict[str, Any] | None,
    situation_understanding: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    case_id: str = "",
    situation_version: str = "",
) -> dict[str, Any]:
    """Deterministic canonicalization boundary.

    Returns either a frozen CanonicalActionDecision or a
    CanonicalizationFailure. A failure NEVER becomes a different business
    action (no escalate_review fallback) — the workflow outcome is
    NEEDS_REVIEW via ``canonicalization_failure_review_state``.
    """
    prop = proposal if isinstance(proposal, dict) else None
    if prop is None:
        return _proposal_failure(
            {"proposal_id": ""},
            ["proposal_incomplete"],
            failed_precondition="business_decision_proposal",
        )

    reason_codes: list[str] = []
    goal = _string(prop.get("goal"))
    action_type = _string(prop.get("action_type"))
    target = _string(prop.get("target"))
    channel = _string(prop.get("channel"))
    required = _as_list(prop.get("required_information"))

    if goal not in CANONICAL_GOALS:
        reason_codes.append("goal_not_in_contract")
    if action_type not in CANONICAL_ACTION_TYPES:
        reason_codes.append("action_type_not_in_contract")
    if target not in CANONICAL_TARGETS:
        reason_codes.append("target_not_in_contract")
    if channel not in CANONICAL_CHANNELS:
        reason_codes.append("channel_not_in_contract")
    if action_type in ACTION_TYPE_CHANNELS and channel not in ACTION_TYPE_CHANNELS[action_type]:
        reason_codes.append("channel_illegal_for_action_type")
    if not required:
        reason_codes.append("required_information_empty")

    if not reason_codes:
        state_missing = _state_missing_information(
            situation_understanding=situation_understanding,
            case_context_pack=case_context_pack,
            intake_result=intake_result,
        )
        if not state_missing:
            reason_codes.append("missing_information_unavailable")
        else:
            state_lower = {str(item).lower() for item in state_missing}
            unsupported = [
                item for item in required if str(item).lower() not in state_lower
            ]
            if unsupported:
                reason_codes.append("required_information_not_in_state")

        conflicted = _conflicted_fact_keys(case_context_pack)
        if conflicted:
            used_conflicted = [
                item for item in required if str(item).lower() in conflicted
            ]
            if used_conflicted:
                reason_codes.append("conflicted_fact_as_certainty")

    if reason_codes:
        return _proposal_failure(prop, reason_codes)

    cid = _string(case_id)
    sv = _string(situation_version)
    semantic_hash = semantic_hash_of(
        case_id=cid,
        situation_version=sv,
        goal=goal,
        action_type=action_type,
        target=target,
        channel=channel,
        required_information=required,
    )
    return {
        "schema_version": CANONICAL_ACTION_DECISION_SCHEMA_VERSION,
        "decision_id": _decision_id(),
        "revision": 1,
        "case_id": cid,
        "situation_version": sv,
        "goal": goal,
        "action_type": action_type,
        "target": target,
        "channel": channel,
        "required_information": required,
        "confidence": _as_float(prop.get("confidence")),
        "risk_class": _string(prop.get("risk_class"), "low"),
        "semantic_hash": semantic_hash,
        "semantic_status": "FROZEN",
        "proposal_id": _string(prop.get("proposal_id")),
        "provenance": {
            "proposal_id": _string(prop.get("proposal_id")),
            "situation_version": sv,
        },
        "created_at": _utc(),
    }


def canonical_decision_code(canonical_decision: dict[str, Any]) -> str:
    """Canonical code string for downstream normalization (A17 unification)."""
    if not isinstance(canonical_decision, dict):
        return ""
    return _string(canonical_decision.get("action_type"))


def semantic_signature_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Semantic Conservation check: same canonical semantic payload."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return _string(left.get("semantic_hash")) == _string(right.get("semantic_hash"))


def canonicalization_failure_review_state(failure: dict[str, Any]) -> dict[str, Any]:
    """Map a CanonicalizationFailure to a workflow state.

    NEEDS_REVIEW is a workflow state, never a new action_type and never an
    escalate_review substitution.
    """
    failure = failure if isinstance(failure, dict) else {}
    return {
        "workflow_state": "NEEDS_REVIEW",
        "decision_state": _string(failure.get("decision_state"), "NO_CANONICAL_DECISION"),
        "reason_codes": _as_list(failure.get("reason_codes")),
        "failed_precondition": _string(failure.get("failed_precondition")),
        "operational_status": "pending_operator_review",
        "proposal_id": _string(failure.get("proposal_id")),
    }


def build_decision_revision_request(
    *,
    decision_id: str,
    revision: int = 1,
    reason_code: str = "NEW_CONFLICTING_EVIDENCE",
    failed_precondition: str = "",
    source_layer: str = "",
) -> dict[str, Any]:
    """Build a DecisionRevisionRequest (P0 contract + event only).

    Emitted after a CAD exists when downstream discovers new evidence, a
    conflict, or an impossible precondition. Full handling (new CAD) is P1.
    """
    if reason_code not in REVISION_REASON_CODES:
        reason_code = "NEW_CONFLICTING_EVIDENCE"
    return {
        "schema_version": DECISION_REVISION_REQUEST_SCHEMA_VERSION,
        "decision_id": _string(decision_id),
        "revision": max(1, int(revision or 1)),
        "reason_code": reason_code,
        "failed_precondition": _string(failed_precondition),
        "source_layer": _string(source_layer),
        "requested_at": _utc(),
    }


__all__ = [
    "ACTION_TYPE_CHANNELS",
    "ACTION_TYPE_DEFAULT_TARGET",
    "BUSINESS_DECISION_PROPOSAL_SCHEMA_VERSION",
    "CANONICAL_ACTION_DECISION_SCHEMA_VERSION",
    "CANONICAL_ACTION_TYPES",
    "CANONICAL_CHANNELS",
    "CANONICAL_GOALS",
    "CANONICAL_TARGETS",
    "CANONICALIZATION_FAILURE_SCHEMA_VERSION",
    "DECISION_REVISION_REQUEST_SCHEMA_VERSION",
    "FAILURE_REASON_CODES",
    "REVISION_REASON_CODES",
    "build_business_decision_proposal",
    "build_decision_revision_request",
    "canonical_decision_code",
    "canonicalization_failure_review_state",
    "canonicalize",
    "semantic_hash_of",
    "semantic_signature_matches",
]
