"""Case Intelligence package — split into 7 domain sub-modules + orchestrator.

Module structure:
  constants.py      — shared constants/enums used by all sub-modules
  validators.py     — _normalize_* functions and pure helper utilities
  understanding.py  — build_case_understanding_snapshot, build_case_operator_brief
  risks.py          — build_risk_assessment
  missing_info.py   — build_missing_info
  next_best_action.py — build_next_best_action
  desk.py           — build_desk_composition, merge_case_guidance_into_intelligence
  lifecycle.py      — build_lifecycle_revision, build_feedback_learning_memory, build_merge_split_suggestions
  orchestrator.py   — build_case_intelligence, apply_hot_state_to_case_intelligence, merge_data
"""
from .orchestrator import (
    apply_hot_state_to_case_intelligence,
    build_case_intelligence,
    merge_data,
)
from .understanding import _resolve_case_id, build_case_operator_brief, build_case_understanding_snapshot
from .risks import build_risk_assessment
from .missing_info import build_missing_info
from .next_best_action import build_next_best_action
from .desk import build_desk_composition, merge_case_guidance_into_intelligence
from .lifecycle import build_feedback_learning_memory, build_lifecycle_revision, build_merge_split_suggestions
from .validators import (
    _normalize_case_guidance,
    validate_case_intelligence_result,
)

__all__ = [
    "apply_hot_state_to_case_intelligence",
    "build_case_intelligence",
    "build_case_operator_brief",
    "build_case_understanding_snapshot",
    "build_desk_composition",
    "build_feedback_learning_memory",
    "build_lifecycle_revision",
    "build_merge_split_suggestions",
    "build_missing_info",
    "build_next_best_action",
    "build_risk_assessment",
    "merge_case_guidance_into_intelligence",
    "merge_data",
    "validate_case_intelligence_result",
    "_normalize_case_guidance",
    "_resolve_case_id",
]
