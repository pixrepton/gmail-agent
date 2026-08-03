---
name: tests
description: "Skill for the Tests area of gmail-agent. 1951 symbols across 321 files."
---

# Tests

1951 symbols | 321 files | Cohesion: 73%

## When to Use

- Working with code in `tools/`
- Understanding how load_company_context, load_constitution, guard_tool_plan work
- Modifying tests-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/tests/test_llm_provider_fallback.py` | _settings, _call, test_structured_alternation_defaults_on_without_explicit_env, test_structured_alternation_explicit_off, test_structured_alternation_distributes_by_stable_correlation (+39) |
| `tools/gmail_audit/tests/test_d1_mutating_routes_auth_gate.py` | _make_client, _client_with_registry, _auth_headers, _clear_all_tokens, _mock_conn (+34) |
| `tools/gmail_audit/tests/test_slice3a_structured_understanding_handoff.py` | _understanding_output, _br_meta, _intel, _snapshot_with_understanding, test_clean_model_result_is_available_and_clean (+23) |
| `tools/gmail_audit/tests/test_gate_b_runtime_proof.py` | _load_guard, test_render_vps_script_resolves_curated_row3_cohort, test_render_vps_script_forces_image_activation_and_host_visible_artifacts, test_render_phase_row3_stop_writes_handoff_and_exits_before_row4_block, test_render_phase_row4_only_skips_activation_prefix_branch (+23) |
| `tools/gmail_audit/tests/test_search_rag_knowledge_tool.py` | test_active_constitution_allows_search_rag_knowledge, _snapshot, _wire_embedding_runtime, test_dispatch_reaches_search_rag_knowledge_handler, test_search_rag_knowledge_no_store_is_reported_as_backend_unavailable (+22) |
| `tools/gmail_audit/tests/test_slice2b1_feed_correctness.py` | test_outcome_unknown_forces_attention_even_with_no_executive_evidence, _signal, _persist_first_signal, test_production_path_noise_then_business_then_noise_on_one_engagement, test_production_path_business_first_is_visible_immediately (+22) |
| `tools/gmail_audit/tests/test_desk_maintenance.py` | _iso_at, _build_detail, test_closed_case_moves_note_to_case_only, test_duplicate_active_note_withdraws_non_keeper, test_stale_note_softens_only_one_presence_level (+19) |
| `tools/gmail_audit/tests/test_slice2b_operator_feed_membership.py` | _snapshot, _classify, test_noise_orphan_snapshot_exists_but_is_not_a_main_feed_card, test_noise_with_pending_hitl_is_visible_as_attention_required, test_noise_with_pending_operator_status_is_visible (+18) |
| `tools/gmail_audit/mailbox_memory/inmemory.py` | append_fact_rows, upsert_case, fetch_case, fetch_case_by_message_id, fetch_execution_results (+18) |
| `tools/gmail_audit/tests/test_cross_repo_contracts.py` | _fetch, test_health_returns_required_fields, test_os_events_returns_items, test_os_events_items_structure, test_decision_queue_returns_items (+18) |

## Entry Points

Start here when exploring this area:

- **`load_company_context`** (Function) — `tools/gmail_audit/agent_runtime/constitution.py:94`
- **`load_constitution`** (Function) — `tools/gmail_audit/agent_runtime/constitution.py:171`
- **`guard_tool_plan`** (Function) — `tools/gmail_audit/agent_runtime/policy_guardrails.py:31`
- **`build_initial_snapshot`** (Function) — `tools/gmail_audit/agent_runtime/store.py:143`
- **`test_snapshot_schema_defaults_case_understanding_to_none`** (Function) — `tools/gmail_audit/tests/test_a1_case_understanding_projection.py:60`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `load_company_context` | Function | `tools/gmail_audit/agent_runtime/constitution.py` | 94 |
| `load_constitution` | Function | `tools/gmail_audit/agent_runtime/constitution.py` | 171 |
| `guard_tool_plan` | Function | `tools/gmail_audit/agent_runtime/policy_guardrails.py` | 31 |
| `build_initial_snapshot` | Function | `tools/gmail_audit/agent_runtime/store.py` | 143 |
| `test_snapshot_schema_defaults_case_understanding_to_none` | Function | `tools/gmail_audit/tests/test_a1_case_understanding_projection.py` | 60 |
| `test_second_reconcile_does_not_re_add_area_gap` | Function | `tools/gmail_audit/tests/test_agent_episodic_memory.py` | 17 |
| `test_load_constitution_has_allowlist` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 161 |
| `test_graph_extract_then_stop` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 168 |
| `test_planner_exception_converges_to_safe_escalation_not_silent_death` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 192 |
| `test_heuristic_planner_single_extract` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 222 |
| `test_budget_exhaustion_sets_hitl` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 261 |
| `test_graph_stops_on_constitution_per_tool_budget` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 289 |
| `test_completed_search_gmail_thread_is_not_offered_again` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 319 |
| `test_successful_distinct_rag_queries_remain_allowed` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 361 |
| `test_successful_rag_research_accumulates_for_next_planner_turn` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 410 |
| `test_semantic_duplicate_rag_objective_is_blocked_after_success` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 465 |
| `test_mi02_old_heater_removal_research_objective_is_blocked_after_success` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 513 |
| `test_int06_generic_context_research_objective_is_blocked_after_success` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 557 |
| `test_int06_source_message_content_research_objective_is_blocked_after_success` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 601 |
| `test_graph_rejects_tool_not_offered_this_turn_after_read_once` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 645 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Build_case_intelligence_layer → _hard_pdf_coverage_threshold` | cross_community | 8 |
| `Build_case_intelligence_layer → _hard_pdf_lane_enabled` | cross_community | 7 |
| `Engagement_materialize_approve → _load_agent_runtime_env_file` | cross_community | 6 |
| `Engagement_materialize_approve → _parse_bool` | cross_community | 6 |
| `Engagement_materialize_approve → _parse_positive_int` | cross_community | 6 |
| `Main → _coerce_int` | cross_community | 6 |
| `Engagement_materialize_approve → _env_is_truthy` | cross_community | 5 |
| `Skrzat_ask → _list_of_dicts` | cross_community | 5 |
| `Main → Summarize_extracted_text_for_operator` | cross_community | 5 |
| `Ingest_message → Get_engagement` | cross_community | 5 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 455 calls |
| Agent_runtime | 114 calls |
| Mailbox_memory | 22 calls |
| Correlation_registry | 22 calls |
| Event_spine | 7 calls |
| Tools | 3 calls |
| Case_intelligence | 2 calls |
| Daszek_engagement_feed | 2 calls |

## How to Explore

1. `context({name: "load_company_context"})` — see callers and callees
2. `query({search_query: "tests"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
