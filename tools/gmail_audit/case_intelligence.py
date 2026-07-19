"""Shim for backward compatibility -- re-exports from the case_intelligence package.
Original file was split during Quality Sprint Faza 5.
"""
from .case_intelligence.orchestrator import (  # noqa: PLC0415, F401, F403
    apply_hot_state_to_case_intelligence,
    build_case_intelligence,
    merge_data,
)
from .case_intelligence.understanding import (  # noqa: PLC0415, F401, F403
    build_case_operator_brief,
    build_case_understanding_snapshot,
)
from .case_intelligence.risks import (  # noqa: PLC0415, F401, F403
    build_risk_assessment,
)
from .case_intelligence.missing_info import (  # noqa: PLC0415, F401, F403
    build_missing_info,
)
from .case_intelligence.next_best_action import (  # noqa: PLC0415, F401, F403
    build_next_best_action,
)
from .case_intelligence.desk import (  # noqa: PLC0415, F401, F403
    build_desk_composition,
    merge_case_guidance_into_intelligence,
)
from .case_intelligence.lifecycle import (  # noqa: PLC0415, F401, F403
    build_feedback_learning_memory,
    build_lifecycle_revision,
    build_merge_split_suggestions,
)
from .case_intelligence.validators import (  # noqa: PLC0415, F401, F403
    _normalize_case_guidance,
    validate_case_intelligence_result,
)
