"""P1.2: deterministic argument-level constraints for the first enforced slice.

Principle:

    ALLOWED TOOL != ALLOWED ARBITRARY ARGUMENTS
    Planner may propose. Planner may not establish canonical execution state.

This module is deliberately bounded to the first enforced slice
(``ask_for_missing_data / customer / mail -> generate_draft_reply``). It is a
typed, deterministic contract -- NOT a generic constraint DSL. Constraint
values always carry a canonical owner/provenance reference
(``source_kind``/``source_ref``) and the decision identity they belong to
(``decision_id``, ``decision_version_id``, ``semantic_hash``).

Supported constraint modes (only the ones this slice needs):

    EXACT            proposed == canonical expected (after typed normalization)
    ONE_OF           proposed in canonical allowed set
    SUBSET_OF        proposed set subset of canonical allowed set
    PRESENT          required canonical argument must be supplied
    ABSENT           argument must not be supplied by the planner
    PLANNER_GENERATED no canonical binding (generative content)
"""

from __future__ import annotations

from typing import Any

ARGUMENT_CONSTRAINT_MODES = (
    "EXACT",
    "ONE_OF",
    "SUBSET_OF",
    "PRESENT",
    "ABSENT",
    "PLANNER_GENERATED",
)

# Deterministic reason codes (reuse the existing taxonomy; no new framework).
REASON_CANONICAL_ARGUMENT_MISMATCH = "CANONICAL_ARGUMENT_MISMATCH"
REASON_ARGUMENT_NOT_ALLOWED = "ARGUMENT_NOT_ALLOWED"
REASON_ARGUMENT_OUTSIDE_CANONICAL_SET = "ARGUMENT_OUTSIDE_CANONICAL_SET"
REASON_MISSING_REQUIRED_CANONICAL_ARGUMENT = "MISSING_REQUIRED_CANONICAL_ARGUMENT"
REASON_UNBOUND_EXECUTION_ARGUMENT = "UNBOUND_EXECUTION_ARGUMENT"
REASON_STALE_DECISION_REVISION = "STALE_DECISION_REVISION"

# Canonical-semantics fields that must NEVER be planner-supplied for the first
# enforced slice tool (generate_draft_reply). Their values are projected from
# CAD / case state / envelope; ABSENT denies planner-side overrides.
_CANONICAL_SEMANTIC_FIELD_NAMES = (
    "case_id",
    "thread_id",
    "customer_id",
    "decision_id",
    "decision_version_id",
    "semantic_hash",
    "action_type",
    "target",
    "channel",
    "recipient",
    "required_information",
    "attachment_ids",
    "draft_hash",
    "approval_receipt",
    "body",
    "subject",
)


def _norm_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm_set(value: Any) -> tuple[str, ...]:
    """Deterministic set-like normalization (ordering/whitespace/case neutral)."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = _norm_text(item)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return tuple(sorted(out, key=str.lower))


def normalize_argument_value(value: Any) -> Any:
    """Typed, deterministic normalization: representation != semantic difference."""
    if isinstance(value, (list, tuple, set)):
        return _norm_set(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return _norm_text(value)


def _text_equal(left: Any, right: Any) -> bool:
    return _norm_text(left).lower() == _norm_text(right).lower()


def build_argument_constraint(
    *,
    argument_name: str,
    constraint_mode: str,
    expected_value: Any = "",
    allowed_values: list[Any] | None = None,
    source_kind: str = "",
    source_ref: str = "",
    decision_id: str = "",
    decision_version_id: str = "",
    semantic_hash: str = "",
) -> dict[str, Any]:
    """Typed argument constraint with canonical owner/provenance and revision."""
    mode = str(constraint_mode or "").strip().upper()
    if mode not in ARGUMENT_CONSTRAINT_MODES:
        raise ValueError(f"unknown argument constraint mode: {mode}")
    return {
        "argument_name": _norm_text(argument_name),
        "constraint_mode": mode,
        "expected_value": expected_value,
        "allowed_values": list(allowed_values or []),
        "source_kind": _norm_text(source_kind),
        "source_ref": _norm_text(source_ref),
        "decision_id": _norm_text(decision_id),
        "decision_version_id": _norm_text(decision_version_id),
        "semantic_hash": _norm_text(semantic_hash),
    }


def project_slice_argument_constraints(
    *,
    action_intent: str = "",
    action_target: str = "",
    action_channel: str = "",
    canonical_decision_id: str = "",
    decision_version_id: str = "",
    source_semantic_hash: str = "",
    allowed_action_tools: list[str] | tuple[str, ...] | None = None,
    tool: str = "generate_draft_reply",
) -> list[dict[str, Any]]:
    """Deterministic constraint projection for the first enforced slice.

    Pure function of (envelope/canonical state + revision); never a decision
    maker. Returns an empty list for paths outside the bounded slice.
    """
    allowed_action = {
        str(item).strip() for item in (allowed_action_tools or []) if str(item).strip()
    }
    if str(tool or "").strip() not in allowed_action:
        return []
    target = _norm_text(action_target).lower()
    channel = _norm_text(action_channel).lower()
    if target != "customer" or (channel and channel != "mail"):
        return []

    intent_raw = _norm_text(action_intent).lower()
    intent_allowed: list[str] = []
    if intent_raw in {"ask_for_missing_data", "request_missing_info"}:
        intent_allowed = ["missing_info"]
    elif intent_raw in {"quote", "provide_offer", "provide_quote"}:
        intent_allowed = ["quote"]
    else:
        # Ambiguous/legacy action vocabulary (e.g. APv2 execution-stage
        # "prepare_reply_draft" without a persisted canonical projection):
        # keep the tool-schema domain (quote|missing_info) -- no semantic
        # narrowing, but execution-critical ABSENT fields still apply.
        intent_allowed = ["quote", "missing_info"]

    common = {
        "source_kind": "canonical_action_decision",
        "source_ref": decision_version_id,
        "decision_id": canonical_decision_id,
        "decision_version_id": decision_version_id,
        "semantic_hash": source_semantic_hash,
    }
    constraints: list[dict[str, Any]] = []
    constraints.append(
        build_argument_constraint(
            argument_name="intent",
            constraint_mode="ONE_OF",
            allowed_values=list(intent_allowed),
            **common,
        )
    )
    # ABSENT for every canonical-semantics field: the planner may not carry
    # execution-critical identities/destinations in tool arguments. The tool
    # schema already forbids them (additionalProperties=false); this is the
    # reference-monitor defense in depth (works even against a mock planner).
    for name in _CANONICAL_SEMANTIC_FIELD_NAMES:
        constraints.append(
            build_argument_constraint(argument_name=name, constraint_mode="ABSENT", **common)
        )
    return constraints


def _check_one(
    *,
    argument_name: str,
    constraint: dict[str, Any],
    proposed: Any,
) -> dict[str, Any] | None:
    mode = str(constraint.get("constraint_mode") or "").strip().upper()
    base = {
        "argument_name": argument_name,
        "constraint_mode": mode,
        "source_kind": _norm_text(constraint.get("source_kind")),
        "source_ref": _norm_text(constraint.get("source_ref")),
        "decision_id": _norm_text(constraint.get("decision_id")),
        "decision_version_id": _norm_text(constraint.get("decision_version_id")),
        "semantic_hash": _norm_text(constraint.get("semantic_hash")),
        "proposed": _compact_repr(proposed),
    }
    if mode == "ABSENT":
        return {
            **base,
            "reason_code": REASON_ARGUMENT_NOT_ALLOWED,
            "expected": "(absent)",
        }
    if mode == "EXACT":
        expected = constraint.get("expected_value")
        if not _text_equal(proposed, expected):
            return {
                **base,
                "reason_code": REASON_CANONICAL_ARGUMENT_MISMATCH,
                "expected": _compact_repr(expected),
            }
        return None
    if mode == "ONE_OF":
        allowed = {_norm_text(item).lower() for item in (constraint.get("allowed_values") or [])}
        if _norm_text(proposed).lower() not in allowed:
            return {
                **base,
                "reason_code": REASON_ARGUMENT_OUTSIDE_CANONICAL_SET,
                "expected": ",".join(_norm_text(item) for item in (constraint.get("allowed_values") or [])),
            }
        return None
    if mode == "SUBSET_OF":
        allowed = {_norm_text(item).lower() for item in (constraint.get("allowed_values") or [])}
        proposed_set = {_norm_text(item).lower() for item in _norm_set(proposed)}
        outside = sorted(proposed_set - allowed)
        if outside:
            return {
                **base,
                "reason_code": REASON_ARGUMENT_OUTSIDE_CANONICAL_SET,
                "expected": ",".join(_norm_text(item) for item in (constraint.get("allowed_values") or [])),
            }
        return None
    if mode == "PRESENT":
        return None  # handled by caller-level presence check
    return None  # PLANNER_GENERATED and unknown modes are not enforced here


def _compact_repr(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ",".join(_norm_text(item) for item in _norm_set(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    return _norm_text(value)


def constraint_violations(
    arguments: dict[str, Any] | None,
    constraints: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Deterministic argument validation. Returns a list of violations ([] = ok).

    Unknown arguments (not declared by the constraint projection) are denied:
    a planner may not smuggle execution-critical fields under a new name.
    """
    if not isinstance(arguments, dict):
        arguments = {}
    if not isinstance(constraints, list) or not constraints:
        return []
    typed: list[dict[str, Any]] = [dict(item) for item in constraints if isinstance(item, dict)]
    if not typed:
        return []

    by_name: dict[str, dict[str, Any]] = {}
    for constraint in typed:
        name = _norm_text(constraint.get("argument_name"))
        if name:
            by_name[name] = constraint

    violations: list[dict[str, Any]] = []
    for raw_name, proposed in arguments.items():
        name = _norm_text(raw_name)
        if not name:
            continue
        constraint = by_name.get(name)
        if constraint is None:
            violations.append(
                {
                    "argument_name": name,
                    "constraint_mode": "ABSENT",
                    "reason_code": REASON_ARGUMENT_NOT_ALLOWED,
                    "expected": "(not part of tool contract)",
                    "proposed": _compact_repr(proposed),
                    "source_kind": "canonical_action_decision",
                    "source_ref": "",
                    "decision_id": "",
                    "decision_version_id": "",
                    "semantic_hash": "",
                }
            )
            continue
        violation = _check_one(
            argument_name=name,
            constraint=constraint,
            proposed=proposed,
        )
        if violation is not None:
            violations.append(violation)

    for constraint in typed:
        mode = str(constraint.get("constraint_mode") or "").strip().upper()
        if mode != "PRESENT":
            continue
        name = _norm_text(constraint.get("argument_name"))
        value = arguments.get(name)
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            violations.append(
                {
                    "argument_name": name,
                    "constraint_mode": mode,
                    "reason_code": REASON_MISSING_REQUIRED_CANONICAL_ARGUMENT,
                    "expected": "(required)",
                    "proposed": "",
                    "source_kind": _norm_text(constraint.get("source_kind")),
                    "source_ref": _norm_text(constraint.get("source_ref")),
                    "decision_id": _norm_text(constraint.get("decision_id")),
                    "decision_version_id": _norm_text(constraint.get("decision_version_id")),
                    "semantic_hash": _norm_text(constraint.get("semantic_hash")),
                }
            )
    return violations


def violations_reason_codes(violations: list[dict[str, Any]]) -> list[str]:
    return sorted({str(item.get("reason_code") or "") for item in violations if item.get("reason_code")})


__all__ = [
    "ARGUMENT_CONSTRAINT_MODES",
    "REASON_ARGUMENT_NOT_ALLOWED",
    "REASON_ARGUMENT_OUTSIDE_CANONICAL_SET",
    "REASON_CANONICAL_ARGUMENT_MISMATCH",
    "REASON_MISSING_REQUIRED_CANONICAL_ARGUMENT",
    "REASON_STALE_DECISION_REVISION",
    "REASON_UNBOUND_EXECUTION_ARGUMENT",
    "build_argument_constraint",
    "constraint_violations",
    "normalize_argument_value",
    "project_slice_argument_constraints",
    "violations_reason_codes",
]
