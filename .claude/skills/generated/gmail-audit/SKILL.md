---
name: gmail-audit
description: "Skill for the Gmail_audit area of gmail-agent. 2643 symbols across 317 files."
---

# Gmail_audit

2643 symbols | 317 files | Cohesion: 70%

## When to Use

- Working with code in `tools/`
- Understanding how build_run_artifact_paths, empty_preflight_summary, build_run_manifest work
- Modifying gmail_audit-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/gmail_intake.py` | attach_daszek_v2_manifest_from_settings, run_live_command, run_rerun_command, run_eval_command, _enrich (+119) |
| `tools/gmail_audit/daszek_v3_operational_feed.py` | apply_cieplo_desk_brief_to_feed_row, _ensure_list, _humanize_family, _omit_empty_list_fields, _essence_pl_from_projection_routes (+56) |
| `tools/gmail_audit/gmail_historical_bootstrap.py` | timed_out, run_gmail_historical_bootstrap, scan_gmail_metadata, select_bootstrap_candidates, select_recommended_batch (+50) |
| `tools/gmail_audit/mailbox_memory_runtime.py` | _append_event, stable_id, split_conflicting_facts, bootstrap, get_context_pack (+50) |
| `tools/gmail_audit/api_app.py` | system_os_events_recent, system_health_status, system_briefing, list_tasks, _default_runtime_provider (+49) |
| `tools/gmail_audit/case_context_contract.py` | format_vnext_human_summary, sort_gaps_for_operator_projection, feed_projection_summary_line, _operator_feed_sanitize_free_text, operator_feed_plain_summary (+49) |
| `tools/gmail_audit/_case_intelligence_legacy.py` | build_case_intelligence, validate_case_intelligence_result, build_case_operator_brief, build_next_best_action, build_missing_info (+46) |
| `tools/gmail_audit/groq_client.py` | is_auth_error_message, extract_mcp_output, format_connector_tool_error, call_groq, _post_responses_payload (+44) |
| `tools/gmail_audit/neo4j_pilot.py` | build_case_projection_payload, _node_row, _relationship_row, _facts_by_document, _document_has_location (+44) |
| `tools/gmail_audit/drive_ingest_runtime.py` | _build_skipped_candidate_result, process_candidate, process_removed_item, _build_drive_triage_result, _append_event (+40) |

## Entry Points

Start here when exploring this area:

- **`build_run_artifact_paths`** (Function) — `tools/gmail_audit/artifact_contracts.py:396`
- **`empty_preflight_summary`** (Function) — `tools/gmail_audit/artifact_contracts.py:401`
- **`build_run_manifest`** (Function) — `tools/gmail_audit/artifact_contracts.py:482`
- **`build_run_checkpoint`** (Function) — `tools/gmail_audit/artifact_contracts.py:516`
- **`read_jsonl`** (Function) — `tools/gmail_audit/artifact_io.py:21`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `TopInstalError` | Class | `tools/gmail_audit/exceptions.py` | 39 |
| `DataValidationError` | Class | `tools/gmail_audit/exceptions.py` | 182 |
| `ContractViolationError` | Class | `tools/gmail_audit/exceptions.py` | 186 |
| `IntakeError` | Class | `tools/gmail_audit/exceptions.py` | 190 |
| `PreclassificationError` | Class | `tools/gmail_audit/exceptions.py` | 194 |
| `BusinessReasoningError` | Class | `tools/gmail_audit/exceptions.py` | 202 |
| `MaterializeError` | Class | `tools/gmail_audit/exceptions.py` | 228 |
| `CaseLookupError` | Class | `tools/gmail_audit/exceptions.py` | 232 |
| `SkrzatError` | Class | `tools/gmail_audit/exceptions.py` | 239 |
| `ExternalServiceError` | Class | `tools/gmail_audit/exceptions.py` | 58 |
| `GmailAPIError` | Class | `tools/gmail_audit/exceptions.py` | 82 |
| `GmailAuthError` | Class | `tools/gmail_audit/exceptions.py` | 86 |
| `DaszekClientError` | Class | `tools/gmail_audit/exceptions.py` | 90 |
| `RAGError` | Class | `tools/gmail_audit/exceptions.py` | 94 |
| `KalkTopError` | Class | `tools/gmail_audit/exceptions.py` | 98 |
| `SignalProcessingError` | Class | `tools/gmail_audit/exceptions.py` | 105 |
| `SignalParseError` | Class | `tools/gmail_audit/exceptions.py` | 109 |
| `SignalClassificationError` | Class | `tools/gmail_audit/exceptions.py` | 113 |
| `SignalReconcileError` | Class | `tools/gmail_audit/exceptions.py` | 117 |
| `StagingDeduplicationError` | Class | `tools/gmail_audit/exceptions.py` | 121 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Build_case_intelligence_layer → _hard_pdf_coverage_threshold` | cross_community | 8 |
| `Update_identity_binding_suggestion → _env_is_truthy` | cross_community | 7 |
| `Run_google_direct_auth_check → Default_env_candidates` | cross_community | 7 |
| `Run_google_direct_auth_check → Validate_agent_runtime_mode_not_primary` | cross_community | 7 |
| `Run_google_direct_auth_check → _case_os_profile_env_overrides` | cross_community | 7 |
| `Get_system_health_snapshot → Case_family_value` | cross_community | 7 |
| `Build_case_intelligence_layer → _hard_pdf_lane_enabled` | cross_community | 7 |
| `Main → _coerce_int` | cross_community | 6 |
| `Update_identity_binding_suggestion → Default_env_candidates` | cross_community | 6 |
| `Update_identity_binding_suggestion → Validate_agent_runtime_mode_not_primary` | cross_community | 6 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Tests | 257 calls |
| Agent_runtime | 34 calls |
| Mailbox_memory | 20 calls |
| Case_intelligence | 8 calls |
| Event_spine | 6 calls |
| Business_dictionary | 4 calls |
| Daszek_engagement_feed | 2 calls |
| Integration | 2 calls |

## How to Explore

1. `context({name: "build_run_artifact_paths"})` — see callers and callees
2. `query({search_query: "gmail_audit"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
