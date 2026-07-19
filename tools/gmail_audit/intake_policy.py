"""Canonical runtime vocabulary and policy constants for Gmail Intake V1."""

from __future__ import annotations

import os
from typing import Any


INTAKE_SCHEMA_VERSION = "1.0"
INTAKE_SCHEMA_NAME = "intake_output_v1"
INTAKE_PROMPT_VERSION = "intake-v1"
SOURCE_CHANNEL_GMAIL = "gmail"
SNAPSHOT_VERSION = "1.1"
INFERENCE_MODE_FULL = "full"
INFERENCE_MODE_COMPACT = "compact"
INFERENCE_MODE_REDUCED_COMPACT = "reduced_compact"
INFERENCE_MODE_MINIMAL_SAFE = "minimal_safe"
INFERENCE_MODES = (
    INFERENCE_MODE_FULL,
    INFERENCE_MODE_COMPACT,
    INFERENCE_MODE_REDUCED_COMPACT,
    INFERENCE_MODE_MINIMAL_SAFE,
)
RUN_MODE_SHADOW = "shadow"
PREVIEW_ADAPTER_VERSION = "1.2"
PREVIEW_TARGET_DASZEK = "daszek_local_tasks_api"
DASZEK_TASKS_ENDPOINT = "/wp-json/daszek/v1/tasks"
DASZEK_SOURCE = "gmail_intake"

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_FAILED_AUTH = "failed_auth"
RUN_STATUS_FAILED_PREFLIGHT = "failed_preflight"
RUN_STATUS_ABORTED = "aborted"
RUN_STATUSES = (
    RUN_STATUS_RUNNING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_COMPLETED_WITH_ERRORS,
    RUN_STATUS_FAILED,
    RUN_STATUS_FAILED_AUTH,
    RUN_STATUS_FAILED_PREFLIGHT,
    RUN_STATUS_ABORTED,
)

PREFLIGHT_STATUS_OK = "ok"
PREFLIGHT_STATUS_WARNING = "warning"
PREFLIGHT_STATUS_FAILED = "failed"
PREFLIGHT_STATUSES = (
    PREFLIGHT_STATUS_OK,
    PREFLIGHT_STATUS_WARNING,
    PREFLIGHT_STATUS_FAILED,
)

CHECK_STATUS_OK = "ok"
CHECK_STATUS_FAILED = "failed"
CHECK_STATUS_SKIPPED = "skipped"
CHECK_STATUSES = (CHECK_STATUS_OK, CHECK_STATUS_FAILED, CHECK_STATUS_SKIPPED)

DOCTOR_STATUS_OK = "ok"
DOCTOR_STATUS_FAILED = "failed"
DOCTOR_STATUS_FAILED_AUTH = "failed_auth"
DOCTOR_STATUS_FAILED_CONFIG = "failed_config"
DOCTOR_STATUSES = (
    DOCTOR_STATUS_OK,
    DOCTOR_STATUS_FAILED,
    DOCTOR_STATUS_FAILED_AUTH,
    DOCTOR_STATUS_FAILED_CONFIG,
)

RUN_STAGES = (
    "preflight",
    "selection",
    "fetch",
    "snapshot",
    "preclassification",
    "model",
    "validation",
    "case_linking",
    "business_reasoning",
    "reply_drafter",
    "action_planner",
    "case_intelligence",
    "preview",
    "daszek_push",
    "eval",
)

PRECLASSIFICATION_LANES = (
    "skip",
    "reference_only",
    "review_direct",
    "intake_llm",
)

ERROR_CATEGORIES = (
    "auth",
    "config",
    "network",
    "throttle",
    "payload_too_large",
    "parse",
    "schema",
    "semantic",
    "validation",
    "preview",
    "other",
)

THREAD_CONTEXT_QUALITY = ("strong", "partial", "weak")
THREAD_POSITIONS = ("new_thread", "reply", "forward", "unknown")

BUSINESS_AREAS = (
    "sales",
    "finance",
    "procurement",
    "logistics",
    "operations",
    "service",
    "security",
    "supplier_commercial",
    "marketing_growth",
    "compliance_legal",
    "internal_coordination",
    "general_admin",
)

CASE_FAMILIES = (
    "lead_opportunity",
    "finance_settlement",
    "procurement_delivery",
    "supplier_commercial_review",
    "platform_service_security",
    "compliance_legal_review",
    "marketing_performance_review",
    "internal_coordination",
    "unknown",
)

DECISION_ACTIONS = (
    "create_case",
    "append_to_existing_case",
    "update_case_state",
    "create_task",
    "create_case_and_task",
    "mark_reference",
    "mark_watchlist",
    "review",
    "ignore",
)

PRIORITIES = ("critical", "high", "medium", "low")

REVIEW_FLAGS = (
    "ambiguous_signal",
    "multiple_competing_signals",
    "possible_existing_case_but_no_match",
    "deadline_found_without_owner",
    "financial_document_without_payable_context",
    "supplier_mail_may_be_noise_or_opportunity",
    "self_forward_requires_meaning_inference",
    "legal_or_compliance_risk",
    "security_or_platform_risk",
    "insufficient_thread_context",
)

DASZEK_KINDS = ("task", "review", "reference", "watchlist", "case", "case_update")
DASZEK_EXTERNAL_REF_KEYS = ("channel", "mailbox", "message_id", "thread_id", "case_key", "received_at")
DASZEK_INTAKE_KEYS = (
    "decision_action",
    "business_area",
    "case_family",
    "case_key",
    "case_key_source",
    "primary_signal_code",
    "primary_signal_name",
    "review_required",
    "review_flags",
    "confidence",
    "reason",
    "action_rationale",
    "state_detected",
    "state_change",
    "extracted_data",
)

DASZEK_KIND_BY_DECISION = {
    "create_case": ("case",),
    "create_case_and_task": ("case", "task"),
    "append_to_existing_case": ("case_update",),
    "update_case_state": ("case_update",),
    "create_task": ("task",),
    "mark_reference": ("reference",),
    "mark_watchlist": ("watchlist",),
    "review": ("review",),
    "ignore": (),
}
DASZEK_PRIORITY_BY_INTAKE_PRIORITY = {priority: priority for priority in PRIORITIES}

HIGH_RISK_AREAS = frozenset({"security", "compliance_legal"})
ACTION_DECISIONS = frozenset(
    {
        "create_case",
        "create_case_and_task",
        "append_to_existing_case",
        "update_case_state",
        "create_task",
    }
)
HIGH_RISK_ACTIONS = frozenset(
    {
        "create_case",
        "create_case_and_task",
        "append_to_existing_case",
        "update_case_state",
        "create_task",
    }
)

# ── Business reasoning safety guards ─────────────────────────────────

# Akcje zablokowane w stanie "czekamy na klienta"
BUSINESS_BLOCKED_IN_WAITING_CLIENT: frozenset[str] = frozenset({
    "send_offer", "send_invoice", "schedule_service", "create_offer",
})

# Akcje wysokiego ryzyka business — wymagają podwyższonego confidence
BUSINESS_HIGH_RISK_ACTIONS: frozenset[str] = frozenset({
    "send_offer", "merge_case", "close_case", "send_invoice",
})
REFERENCE_ONLY_ACTIONS = frozenset({"mark_reference", "mark_watchlist", "ignore"})
REFERENCE_DECISIONS = frozenset({"mark_reference", "mark_watchlist"})
NEW_CASE_ACTIONS = frozenset({"create_case", "create_case_and_task"})
UPDATE_ACTIONS = frozenset({"append_to_existing_case", "update_case_state"})
ACTION_BEARING_DECISIONS = frozenset(ACTION_DECISIONS)
FORCED_REVIEW_FLAGS = frozenset(REVIEW_FLAGS)

LOW_DECISION_CONFIDENCE_REQUIRES_REVIEW = float(os.getenv("LOW_CONFIDENCE_THRESHOLD", "0.55"))
HIGH_RISK_ACTION_MIN_DECISION_CONFIDENCE = float(os.getenv("HIGH_RISK_CONFIDENCE_THRESHOLD", "0.70"))
REFERENCE_ACTION_MIN_DECISION_CONFIDENCE = float(os.getenv("REFERENCE_CONFIDENCE_THRESHOLD", "0.65"))
EXTRACTION_CONFIDENCE_REQUIRES_REVIEW = float(os.getenv("EXTRACTION_CONFIDENCE_THRESHOLD", "0.45"))
POSSIBLE_EXISTING_CASE_THRESHOLD = float(os.getenv("EXISTING_CASE_THRESHOLD", "0.70"))
STRONG_EXISTING_CASE_THRESHOLD = float(os.getenv("STRONG_CASE_THRESHOLD", "0.85"))
SELF_FORWARD_REQUIRES_STRONG_MATCH = float(os.getenv("SELF_FORWARD_MATCH_THRESHOLD", "0.90"))
CREATE_TASK_CASE_LINK_SUSPICIOUS_THRESHOLD = float(os.getenv("CASE_LINK_SUSPICIOUS_THRESHOLD", "0.80"))

CASE_KEY_SOURCE_LINKED = "linked_case_candidate"
CASE_KEY_SOURCE_DERIVED = "derived_from_thread"
CASE_KEY_SOURCE_NONE = "none"
CASE_KEY_SOURCES = (
    CASE_KEY_SOURCE_LINKED,
    CASE_KEY_SOURCE_DERIVED,
    CASE_KEY_SOURCE_NONE,
)

OUTPUT_ORIGIN_RAW_VALID = "raw_valid"
OUTPUT_ORIGIN_NORMALIZED_VALID = "normalized_valid"
OUTPUT_ORIGIN_REPAIRED_VALID = "repaired_valid"
OUTPUT_ORIGIN_GUARDRAILED_REVIEW = "guardrailed_review"
OUTPUT_ORIGIN_INVALID = "invalid"
OUTPUT_ORIGINS = (
    OUTPUT_ORIGIN_RAW_VALID,
    OUTPUT_ORIGIN_NORMALIZED_VALID,
    OUTPUT_ORIGIN_REPAIRED_VALID,
    OUTPUT_ORIGIN_GUARDRAILED_REVIEW,
    OUTPUT_ORIGIN_INVALID,
)


def top_case_candidate(candidates: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Return the highest-confidence case-link candidate when one exists."""
    best_candidate: dict[str, Any] | None = None
    best_confidence = -1.0

    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        case_key = str(candidate.get("case_key") or "").strip()
        if not case_key:
            continue
        try:
            confidence = float(candidate.get("match_confidence", 0))
        except (TypeError, ValueError):
            continue
        if confidence > best_confidence:
            best_candidate = candidate
            best_confidence = confidence

    return best_candidate


def top_case_candidate_confidence(candidates: list[dict[str, Any]] | None) -> float:
    """Return the top case-link confidence or 0.0 when there is no valid candidate."""
    best = top_case_candidate(candidates)
    if best is None:
        return 0.0
    try:
        return float(best.get("match_confidence", 0))
    except (TypeError, ValueError):
        return 0.0


def extract_best_case_key(candidates: list[dict[str, Any]] | None) -> str:
    """Return the highest-confidence case key or an empty string."""
    best = top_case_candidate(candidates)
    if best is None:
        return ""
    return str(best.get("case_key") or "").strip()


def derive_case_key(*, case_family: str, thread_id: str) -> str | None:
    """Derive the conservative fallback case key used for preview-only artifacts."""
    if case_family == "unknown" or not thread_id.strip():
        return None
    return f"{SOURCE_CHANNEL_GMAIL}:{case_family}:{thread_id.strip()}"


def has_daszek_payload(action: str) -> bool:
    """Return True when an intake decision should produce at least one Daszek payload."""
    return bool(DASZEK_KIND_BY_DECISION.get(action, ()))


def case_key_allowed_for_action(action: str) -> bool:
    """Return True when the given decision action should carry a case key into Daszek."""
    return action in {
        "create_case",
        "create_case_and_task",
        "append_to_existing_case",
        "update_case_state",
        "create_task",
        "mark_watchlist",
    }
