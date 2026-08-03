---
name: case-intelligence
description: "Skill for the Case_intelligence area of gmail-agent. 73 symbols across 13 files."
---

# Case_intelligence

73 symbols | 13 files | Cohesion: 76%

## When to Use

- Working with code in `tools/`
- Understanding how validate_case_intelligence_result, build_risk_assessment, test_r3_humanize_risk_signal_never_leaks_snake_case work
- Modifying case_intelligence-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/case_intelligence/validators.py` | _string_or_default, _bounded_float, _normalize_channel, _normalize_string_list, _attention_reason_pl (+28) |
| `tools/gmail_audit/tests/unit/test_case_intelligence_units.py` | test_risk_item_defaults, test_dedupe_risk_items_highest_severity_wins, test_build_risk_assessment_empty, test_build_risk_assessment_with_aging, test_build_feedback_learning_memory_empty (+7) |
| `tools/gmail_audit/case_intelligence/risks.py` | _severity_rank, _grounding, _risk_item, _dedupe_risk_items, _humanize_risk_signal (+2) |
| `tools/gmail_audit/case_intelligence/understanding.py` | build_case_operator_brief, _latest_meaningful_change_pl, _attention_reason_pl, _business_priority, build_case_understanding_snapshot |
| `tools/gmail_audit/case_intelligence/lifecycle.py` | build_merge_split_suggestions, build_feedback_learning_memory, build_lifecycle_revision |
| `tools/gmail_audit/tests/test_understanding_quality_contract.py` | test_r3_humanize_risk_signal_never_leaks_snake_case, test_r3_unanswered_question_risk_is_grounded_and_human |
| `tools/gmail_audit/case_intelligence/desk.py` | merge_case_guidance_into_intelligence, build_desk_composition |
| `tools/gmail_audit/tests/test_v2_semantics.py` | test_move_to_case_only_keeps_distinct_trace_semantics, test_deescalate_presence_maps_to_persistence_and_trace |
| `tools/gmail_audit/v2_semantics.py` | is_case_only_transition, decision_type_from_command |
| `tools/gmail_audit/tests/test_operator_visibility_policy.py` | test_suppress_desk_for_promotional_preclassification_lane, test_suppress_desk_for_wait_marketing_business_reasoning |

## Entry Points

Start here when exploring this area:

- **`validate_case_intelligence_result`** (Function) — `tools/gmail_audit/case_intelligence/validators.py:548`
- **`build_risk_assessment`** (Function) — `tools/gmail_audit/case_intelligence/risks.py:140`
- **`test_r3_humanize_risk_signal_never_leaks_snake_case`** (Function) — `tools/gmail_audit/tests/test_understanding_quality_contract.py:43`
- **`test_r3_unanswered_question_risk_is_grounded_and_human`** (Function) — `tools/gmail_audit/tests/test_understanding_quality_contract.py:49`
- **`build_merge_split_suggestions`** (Function) — `tools/gmail_audit/case_intelligence/lifecycle.py:10`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `validate_case_intelligence_result` | Function | `tools/gmail_audit/case_intelligence/validators.py` | 548 |
| `build_risk_assessment` | Function | `tools/gmail_audit/case_intelligence/risks.py` | 140 |
| `test_r3_humanize_risk_signal_never_leaks_snake_case` | Function | `tools/gmail_audit/tests/test_understanding_quality_contract.py` | 43 |
| `test_r3_unanswered_question_risk_is_grounded_and_human` | Function | `tools/gmail_audit/tests/test_understanding_quality_contract.py` | 49 |
| `build_merge_split_suggestions` | Function | `tools/gmail_audit/case_intelligence/lifecycle.py` | 10 |
| `build_feedback_learning_memory` | Function | `tools/gmail_audit/case_intelligence/lifecycle.py` | 64 |
| `build_case_intelligence` | Function | `tools/gmail_audit/case_intelligence/orchestrator.py` | 41 |
| `build_case_operator_brief` | Function | `tools/gmail_audit/case_intelligence/understanding.py` | 178 |
| `merge_case_guidance_into_intelligence` | Function | `tools/gmail_audit/case_intelligence/desk.py` | 17 |
| `build_lifecycle_revision` | Function | `tools/gmail_audit/case_intelligence/lifecycle.py` | 108 |
| `is_case_only_transition` | Function | `tools/gmail_audit/v2_semantics.py` | 38 |
| `decision_type_from_command` | Function | `tools/gmail_audit/v2_semantics.py` | 51 |
| `build_desk_composition` | Function | `tools/gmail_audit/case_intelligence/desk.py` | 56 |
| `should_suppress_desk_and_tasks` | Function | `tools/gmail_audit/operator_visibility_policy.py` | 27 |
| `test_suppress_desk_for_promotional_preclassification_lane` | Function | `tools/gmail_audit/tests/test_operator_visibility_policy.py` | 43 |
| `test_suppress_desk_for_wait_marketing_business_reasoning` | Function | `tools/gmail_audit/tests/test_operator_visibility_policy.py` | 48 |
| `build_next_best_action` | Function | `tools/gmail_audit/case_intelligence/next_best_action.py` | 8 |
| `build_case_understanding_snapshot` | Function | `tools/gmail_audit/case_intelligence/understanding.py` | 81 |
| `test_risk_item_defaults` | Method | `tools/gmail_audit/tests/unit/test_case_intelligence_units.py` | 16 |
| `test_dedupe_risk_items_highest_severity_wins` | Method | `tools/gmail_audit/tests/unit/test_case_intelligence_units.py` | 23 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Build_case_understanding_snapshot → Top_case_candidate` | cross_community | 6 |
| `Build_case_intelligence → _grounding` | cross_community | 5 |
| `Build_case_understanding_snapshot → Derive_case_key` | cross_community | 5 |
| `Build_case_intelligence → _humanize_risk_signal` | cross_community | 4 |
| `Build_case_intelligence → _severity_rank` | cross_community | 4 |
| `Build_case_intelligence → _bounded_float` | cross_community | 4 |
| `Build_case_understanding_snapshot → Canonicalize_case_anchor` | cross_community | 4 |
| `Build_case_understanding_snapshot → Stable_case_id` | cross_community | 4 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 10 calls |
| Unit | 1 calls |

## How to Explore

1. `context({name: "validate_case_intelligence_result"})` — see callers and callees
2. `query({search_query: "case_intelligence"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
