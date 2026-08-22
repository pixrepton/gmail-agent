"""P1.2-A/B: typed argument-constraint contract, normalization, projection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.tool_argument_constraints import (
    ARGUMENT_CONSTRAINT_MODES,
    REASON_ARGUMENT_NOT_ALLOWED,
    REASON_ARGUMENT_OUTSIDE_CANONICAL_SET,
    REASON_CANONICAL_ARGUMENT_MISMATCH,
    REASON_MISSING_REQUIRED_CANONICAL_ARGUMENT,
    build_argument_constraint,
    constraint_violations,
    normalize_argument_value,
    project_slice_argument_constraints,
    violations_reason_codes,
)


# --------------------------------------------------------------------------
# typed contract
# --------------------------------------------------------------------------


def test_constraint_contract_carries_owner_and_revision() -> None:
    constraint = build_argument_constraint(
        argument_name="intent",
        constraint_mode="ONE_OF",
        allowed_values=["missing_info"],
        source_kind="canonical_action_decision",
        source_ref="dec_1:r2",
        decision_id="dec_1",
        decision_version_id="dec_1:r2",
        semantic_hash="sh_abc",
    )
    assert constraint["argument_name"] == "intent"
    assert constraint["constraint_mode"] == "ONE_OF"
    assert constraint["allowed_values"] == ["missing_info"]
    assert constraint["source_kind"] == "canonical_action_decision"
    assert constraint["source_ref"] == "dec_1:r2"
    assert constraint["decision_id"] == "dec_1"
    assert constraint["decision_version_id"] == "dec_1:r2"
    assert constraint["semantic_hash"] == "sh_abc"


def test_unknown_constraint_mode_rejected() -> None:
    with pytest.raises(ValueError):
        build_argument_constraint(
            argument_name="x",
            constraint_mode="GENERIC_ABAC_RULE",
        )


def test_modes_are_bounded_no_generic_dsl() -> None:
    assert set(ARGUMENT_CONSTRAINT_MODES) == {
        "EXACT",
        "ONE_OF",
        "SUBSET_OF",
        "PRESENT",
        "ABSENT",
        "PLANNER_GENERATED",
    }


# --------------------------------------------------------------------------
# deterministic typed normalization
# --------------------------------------------------------------------------


def test_set_ordering_and_whitespace_are_representation_not_semantics() -> None:
    left = normalize_argument_value(["error_code", "exact_symptoms"])
    right = normalize_argument_value([" exact_symptoms ", "error_code"])
    assert left == right
    assert left == ("error_code", "exact_symptoms")


def test_case_folding_is_representation_for_enum_text() -> None:
    assert normalize_argument_value("Missing_Info") == "Missing_Info"
    from agent_runtime.tool_argument_constraints import _text_equal

    assert _text_equal("missing_info", "Missing_Info") is True
    assert _text_equal("mail", "phone") is False


# --------------------------------------------------------------------------
# slice projection
# --------------------------------------------------------------------------


def test_slice_projection_binds_intent_and_absent_fields() -> None:
    constraints = project_slice_argument_constraints(
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        canonical_decision_id="dec_1",
        decision_version_id="dec_1:r2",
        source_semantic_hash="sh_abc",
        allowed_action_tools=["generate_draft_reply"],
    )
    by_name = {c["argument_name"]: c for c in constraints}
    assert by_name["intent"]["constraint_mode"] == "ONE_OF"
    assert by_name["intent"]["allowed_values"] == ["missing_info"]
    assert by_name["intent"]["decision_version_id"] == "dec_1:r2"
    assert by_name["intent"]["semantic_hash"] == "sh_abc"
    for field in (
        "case_id",
        "target",
        "channel",
        "recipient",
        "required_information",
        "attachment_ids",
        "approval_receipt",
        "draft_hash",
    ):
        assert by_name[field]["constraint_mode"] == "ABSENT"


def test_slice_projection_outside_slice_is_empty() -> None:
    assert (
        project_slice_argument_constraints(
            action_intent="ask_for_missing_data",
            action_target="operator",
            action_channel="mail",
            allowed_action_tools=["generate_draft_reply"],
        )
        == []
    )
    assert (
        project_slice_argument_constraints(
            action_intent="ask_for_missing_data",
            action_target="customer",
            action_channel="mail",
            allowed_action_tools=["request_operator_clarification"],
        )
        == []
    )


def test_projection_is_deterministic() -> None:
    kwargs = dict(
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        canonical_decision_id="dec_1",
        decision_version_id="dec_1:r2",
        source_semantic_hash="sh_abc",
        allowed_action_tools=["generate_draft_reply"],
    )
    first = project_slice_argument_constraints(**kwargs)
    second = project_slice_argument_constraints(**kwargs)
    assert first == second


# --------------------------------------------------------------------------
# deterministic validation
# --------------------------------------------------------------------------


def _slice_constraints() -> list[dict]:
    return project_slice_argument_constraints(
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        canonical_decision_id="dec_1",
        decision_version_id="dec_1:r2",
        source_semantic_hash="sh_abc",
        allowed_action_tools=["generate_draft_reply"],
    )


def test_valid_intent_only_arguments_pass() -> None:
    violations = constraint_violations({"intent": "missing_info"}, _slice_constraints())
    assert violations == []


@pytest.mark.parametrize(
    "arguments,expected_reason",
    [
        ({"intent": "quote"}, REASON_ARGUMENT_OUTSIDE_CANONICAL_SET),
        ({"intent": "missing_info", "target": "operator"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "channel": "internal"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "case_id": "case_x"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "recipient": "attacker@example.com"}, REASON_ARGUMENT_NOT_ALLOWED),
        (
            {"intent": "missing_info", "required_information": ["a", "b", "c"]},
            REASON_ARGUMENT_NOT_ALLOWED,
        ),
        ({"intent": "missing_info", "attachment_ids": ["a1", "a3"]}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "approval_receipt": "appr_x"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "invented_arg": "x"}, REASON_ARGUMENT_NOT_ALLOWED),
    ],
)
def test_adversarial_arguments_denied(arguments: dict, expected_reason: str) -> None:
    violations = constraint_violations(arguments, _slice_constraints())
    assert violations
    assert expected_reason in violations_reason_codes(violations)


def test_exact_mismatch_reason() -> None:
    constraints = [
        build_argument_constraint(
            argument_name="decision_version_id",
            constraint_mode="EXACT",
            expected_value="dec_1:r2",
        )
    ]
    violations = constraint_violations({"decision_version_id": "dec_1:r1"}, constraints)
    assert violations[0]["reason_code"] == REASON_CANONICAL_ARGUMENT_MISMATCH


def test_subset_of_denies_expansion_but_allows_reordering() -> None:
    constraints = [
        build_argument_constraint(
            argument_name="required_information",
            constraint_mode="SUBSET_OF",
            allowed_values=["error_code", "exact_symptoms"],
        )
    ]
    ok = constraint_violations(
        {"required_information": ["exact_symptoms", "error_code"]}, constraints
    )
    assert ok == []
    bad = constraint_violations(
        {"required_information": ["error_code", "exact_symptoms", "installer_password"]},
        constraints,
    )
    assert bad[0]["reason_code"] == REASON_ARGUMENT_OUTSIDE_CANONICAL_SET


def test_present_requires_canonical_argument() -> None:
    constraints = [
        build_argument_constraint(
            argument_name="decision_version_id",
            constraint_mode="PRESENT",
        )
    ]
    missing = constraint_violations({}, constraints)
    assert missing[0]["reason_code"] == REASON_MISSING_REQUIRED_CANONICAL_ARGUMENT
