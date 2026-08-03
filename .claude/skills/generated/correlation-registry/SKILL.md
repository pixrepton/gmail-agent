---
name: correlation-registry
description: "Skill for the Correlation_registry area of gmail-agent. 141 symbols across 18 files."
---

# Correlation_registry

141 symbols | 18 files | Cohesion: 74%

## When to Use

- Working with code in `tools/`
- Understanding how normalize_link_type, find_engagement_by_technical_precedence, plan_mailbox_case_sync work
- Modifying correlation_registry-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/correlation_registry/store.py` | merge_identity_metadata, merge_engagement_metadata, update_identity_display_name, find_engagement_by_link, find_engagement_for_identity_recent (+50) |
| `tools/gmail_audit/correlation_registry/identity_binding.py` | normalize_nip, normalize_phone, _metadata_signal, _identity_signals, _fuzzy_name_address_score (+9) |
| `tools/gmail_audit/correlation_registry/identity_metadata.py` | _coerce_dict, normalize_address_norm, normalize_nip, infer_identity_kind, build_property_anchor (+6) |
| `tools/gmail_audit/correlation_registry/heuristics.py` | _link_targets, _has_unlinked_engagement_split_targets, find_engagement_by_technical_precedence, resolve_engagement_for_links, resolve_identity_and_engagement (+4) |
| `tools/gmail_audit/correlation_registry/snapshot.py` | _workflow_base_url, _workflow_auth_headers, fetch_workflow_context_pack_async, fetch_workflow_context_packs_parallel, fetch_workflow_context_pack_http (+3) |
| `tools/gmail_audit/correlation_registry/preview.py` | _mailbox_case_links, _count_missing_links, plan_mailbox_case_sync, plan_workflow_sync, empty_dry_run_stats (+2) |
| `tools/gmail_audit/scripts/run_backfill_correlation_registry.py` | _sync_cases, _sync_workflows, _load_workflow_rows, _fetch_delta_cases, _fetch_delta_links (+1) |
| `tools/gmail_audit/api_app.py` | scan_identity_binding_suggestions, list_identity_binding_suggestions, get_identity_binding_suggestion_detail, update_identity_binding_suggestion, engagement_snapshot |
| `tools/gmail_audit/correlation_registry/identity_email_dedup.py` | find_duplicate_email_groups, merge_email_duplicate_group, count_duplicate_email_groups, plan_email_dedup_groups, run_email_identity_dedup |
| `tools/gmail_audit/tests/test_correlation_registry.py` | test_technical_precedence_beats_recent_email_only_engagement, test_parallel_workflow_fetch_partial_on_timeout, test_same_email_one_identity_two_repo_links_via_message_id, test_calc_request_snapshot_technical_precedence_reuses_engagement |

## Entry Points

Start here when exploring this area:

- **`normalize_link_type`** (Function) — `tools/gmail_audit/correlation_registry/link_types.py:32`
- **`find_engagement_by_technical_precedence`** (Function) — `tools/gmail_audit/correlation_registry/heuristics.py:91`
- **`plan_mailbox_case_sync`** (Function) — `tools/gmail_audit/correlation_registry/preview.py:85`
- **`plan_workflow_sync`** (Function) — `tools/gmail_audit/correlation_registry/preview.py:161`
- **`empty_dry_run_stats`** (Function) — `tools/gmail_audit/correlation_registry/preview.py:231`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `normalize_link_type` | Function | `tools/gmail_audit/correlation_registry/link_types.py` | 32 |
| `find_engagement_by_technical_precedence` | Function | `tools/gmail_audit/correlation_registry/heuristics.py` | 91 |
| `plan_mailbox_case_sync` | Function | `tools/gmail_audit/correlation_registry/preview.py` | 85 |
| `plan_workflow_sync` | Function | `tools/gmail_audit/correlation_registry/preview.py` | 161 |
| `empty_dry_run_stats` | Function | `tools/gmail_audit/correlation_registry/preview.py` | 231 |
| `accumulate_plan` | Function | `tools/gmail_audit/correlation_registry/preview.py` | 242 |
| `test_dry_run_stats_count_planned_links` | Function | `tools/gmail_audit/tests/test_backfill_correlation_registry.py` | 38 |
| `test_technical_precedence_beats_recent_email_only_engagement` | Function | `tools/gmail_audit/tests/test_correlation_registry.py` | 236 |
| `normalize_address_norm` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 42 |
| `normalize_nip` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 50 |
| `infer_identity_kind` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 55 |
| `build_property_anchor` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 77 |
| `normalize_identity_metadata` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 101 |
| `normalize_engagement_metadata` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 111 |
| `merge_identity_metadata` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 131 |
| `merge_engagement_metadata` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 145 |
| `extract_property_hints_from_links` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 173 |
| `extract_identity_hints_from_payload` | Function | `tools/gmail_audit/correlation_registry/identity_metadata.py` | 192 |
| `test_infer_identity_kind_person_vs_organization` | Function | `tools/gmail_audit/tests/test_identity_metadata.py` | 23 |
| `test_property_anchor_shape` | Function | `tools/gmail_audit/tests/test_identity_metadata.py` | 29 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Update_identity_binding_suggestion → _env_is_truthy` | cross_community | 7 |
| `Update_identity_binding_suggestion → Default_env_candidates` | cross_community | 6 |
| `Update_identity_binding_suggestion → Validate_agent_runtime_mode_not_primary` | cross_community | 6 |
| `Update_identity_binding_suggestion → _case_os_profile_env_overrides` | cross_community | 6 |
| `Main → _env_is_truthy` | cross_community | 5 |
| `Ingest_message → Get_engagement` | cross_community | 5 |
| `Ingest_message → Update_identity_display_name` | cross_community | 5 |
| `Ingest_message → Find_recent_engagement_for_email` | cross_community | 5 |
| `Update_identity_binding_suggestion → _capture_initial_env_values` | cross_community | 5 |
| `Update_identity_binding_suggestion → _collect_config_source_details` | cross_community | 5 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Tests | 11 calls |
| Gmail_audit | 9 calls |
| Agent_runtime | 1 calls |

## How to Explore

1. `context({name: "normalize_link_type"})` — see callers and callees
2. `query({search_query: "correlation_registry"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
