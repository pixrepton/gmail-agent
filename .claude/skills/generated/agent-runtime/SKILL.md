---
name: agent-runtime
description: "Skill for the Agent_runtime area of gmail-agent. 343 symbols across 77 files."
---

# Agent_runtime

343 symbols | 77 files | Cohesion: 67%

## When to Use

- Working with code in `tools/`
- Understanding how get_breaker, filter_planner_allowlist, test_llm_timeout_falls_back work
- Modifying agent_runtime-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/agent_runtime/store.py` | _resolve_trace_id, build_snapshot_from_signal, load_snapshot, save_snapshot, init_snapshot_from_signal (+28) |
| `tools/gmail_audit/agent_runtime/agent_reconcile.py` | _canonical_staging_payload, build_operator_engagement_store, _check_cieplo_dedup, run_agent_reconcile_staging, _raise_agent_reconcile_failure (+14) |
| `tools/gmail_audit/agent_runtime/mcp_service.py` | evaluate_agent_mcp_smoke, _fake_run, from_env, get_agent_turns, dispatch_mcp_tool (+10) |
| `tools/gmail_audit/agent_runtime/graph.py` | _run, _filter_completed_read_once_tools, _apply_snapshot_delta_blocking, _is_loop_terminal, _planner_tokens (+10) |
| `tools/gmail_audit/agent_runtime/authz.py` | _expected_token, _expected_read_only_token, verify_operator_token, verify_read_only_token, token_scope (+8) |
| `tools/gmail_audit/agent_runtime/business_pulse.py` | _redact_for_logging, _log_bp_call, _utc_now, get_client_health, get_daily_delta (+7) |
| `tools/gmail_audit/agent_runtime/metrics.py` | record_request, record_agent_turn, record_hitl, _maybe_flush, report (+7) |
| `tools/gmail_audit/agent_runtime/feed_projection.py` | _utc_now_iso, _draft_pl, _agent_ci_stub, enrich_envelope_from_engagement, build_canonical_operator_snapshot (+7) |
| `tools/gmail_audit/tests/test_agent_mcp_pr_g.py` | test_mcp_server_tool_schema_names, test_get_agent_turns_from_memory_journal, test_dispatch_unknown_tool, test_get_engagement_snapshot_by_case_id, test_get_snapshot_include_full (+6) |
| `tools/gmail_audit/agent_runtime/digital_twin_dod.py` | _city_matches_radlin, _reconcile_path_agent, _no_shared_downstream, _v2_projection_agent_marker, _feed_case_visible (+5) |

## Entry Points

Start here when exploring this area:

- **`get_breaker`** (Function) — `tools/gmail_audit/agent_runtime/circuit_breaker.py:86`
- **`filter_planner_allowlist`** (Function) — `tools/gmail_audit/agent_runtime/policy_guardrails.py:18`
- **`test_llm_timeout_falls_back`** (Function) — `tools/gmail_audit/tests/chaos/test_llm_timeout.py:49`
- **`test_openai_planner_cerebras_transient_falls_back_to_nvidia`** (Function) — `tools/gmail_audit/tests/test_agent_planner_endpoints.py:41`
- **`test_filter_planner_allowlist_excludes_forbidden`** (Function) — `tools/gmail_audit/tests/test_agent_pr_c_complete.py:67`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `OperatorEngagementStore` | Class | `tools/gmail_audit/agent_runtime/store.py` | 32 |
| `InMemoryOperatorEngagementStore` | Class | `tools/gmail_audit/agent_runtime/store.py` | 170 |
| `PostgresOperatorEngagementStore` | Class | `tools/gmail_audit/agent_runtime/store.py` | 283 |
| `AgentTurnJournal` | Class | `tools/gmail_audit/agent_runtime/turn_journal.py` | 26 |
| `InMemoryAgentTurnJournal` | Class | `tools/gmail_audit/agent_runtime/turn_journal.py` | 42 |
| `PostgresAgentTurnJournal` | Class | `tools/gmail_audit/agent_runtime/turn_journal.py` | 77 |
| `KalkTopClientError` | Class | `tools/gmail_audit/agent_runtime/kalk_top_client.py` | 12 |
| `KalkTopUnreachableError` | Class | `tools/gmail_audit/agent_runtime/kalk_top_client.py` | 16 |
| `get_breaker` | Function | `tools/gmail_audit/agent_runtime/circuit_breaker.py` | 86 |
| `filter_planner_allowlist` | Function | `tools/gmail_audit/agent_runtime/policy_guardrails.py` | 18 |
| `test_llm_timeout_falls_back` | Function | `tools/gmail_audit/tests/chaos/test_llm_timeout.py` | 49 |
| `test_openai_planner_cerebras_transient_falls_back_to_nvidia` | Function | `tools/gmail_audit/tests/test_agent_planner_endpoints.py` | 41 |
| `test_filter_planner_allowlist_excludes_forbidden` | Function | `tools/gmail_audit/tests/test_agent_pr_c_complete.py` | 67 |
| `test_openai_planner_offers_search_rag_knowledge_when_allowlisted` | Function | `tools/gmail_audit/tests/test_search_rag_knowledge_tool.py` | 197 |
| `evaluate_agent_mcp_smoke` | Function | `tools/gmail_audit/agent_runtime/mcp_service.py` | 289 |
| `build_snapshot_from_signal` | Function | `tools/gmail_audit/agent_runtime/store.py` | 87 |
| `run_checks` | Function | `tools/gmail_audit/scripts/agent_checklist_gate.py` | 20 |
| `main` | Function | `tools/gmail_audit/scripts/agent_checklist_gate.py` | 203 |
| `main` | Function | `tools/gmail_audit/scripts/agent_mcp_smoke_gate.py` | 16 |
| `test_store_init_then_graph_round_trip` | Function | `tools/gmail_audit/tests/test_agent_graph_engine.py` | 239 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Engagement_materialize_approve → _load_agent_runtime_env_file` | cross_community | 6 |
| `Engagement_materialize_approve → _parse_bool` | cross_community | 6 |
| `Engagement_materialize_approve → _parse_positive_int` | cross_community | 6 |
| `Run_agent_reconcile → Legacy_feed_explicitly_requested` | cross_community | 5 |
| `Confirm_task → Apply_routing_to_case_row` | cross_community | 5 |
| `Reject_task → Apply_routing_to_case_row` | cross_community | 5 |
| `Execute_create_case → Get_engagement` | cross_community | 5 |
| `Execute_create_case → Update_identity_display_name` | cross_community | 5 |
| `Execute_create_case → Find_recent_engagement_for_email` | cross_community | 5 |
| `Main → _connect` | cross_community | 4 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Tests | 75 calls |
| Gmail_audit | 13 calls |
| Event_spine | 4 calls |
| Mailbox_memory | 3 calls |
| Daszek_engagement_feed | 2 calls |
| Correlation_registry | 1 calls |

## How to Explore

1. `context({name: "get_breaker"})` — see callers and callees
2. `query({search_query: "agent_runtime"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
