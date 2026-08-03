---
name: scripts
description: "Skill for the Scripts area of gmail-agent. 214 symbols across 29 files."
---

# Scripts

214 symbols | 29 files | Cohesion: 79%

## When to Use

- Working with code in `tools/`
- Understanding how main, build_period_query, bounded_text_tail work
- Modifying scripts-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `scripts/gate_b_runtime_proof.py` | _sha256_token, _row, classify_activation, _row3_selected_count, _row3_cohort_minimums (+37) |
| `tools/scripts/agent_harness_audit.py` | check_files_exist, check_governance_hubs_exist, check_mcp_server_allowlist, check_rules_governance, check_export_gitnexus (+26) |
| `tools/gmail_audit/scripts/daszek_local_133_proof.py` | _utc_ts, _token, _node_b_token, _base_url, _node_b_base (+16) |
| `scripts/sequential_gmail_ingress_daszek.py` | _repo_root, _extract_mid, _exclude_message_ids_set, _message_ids_list, _selection_fetch_limit (+9) |
| `tools/scripts/agent_preflight.py` | _run, _git_status, _node_context, _ssh_alias, _redacted_db_ref (+8) |
| `tools/scripts/export_hardening.py` | build_clean_export, _copy_ignore_factory, _validate_export_paths, _is_relative_to, _ignore (+8) |
| `tools/gmail_audit/scripts/case_os_architecture_proof.py` | _verify_p1, _verify_p2, _verify_p3, _verify_p5, main (+4) |
| `tools/gmail_audit/sequential_ingress_helpers.py` | bounded_text_tail, load_failed_items_records, make_failed_item_record, parse_newer_than_days, build_gmail_intake_message_command (+1) |
| `tools/gmail_audit/scripts/case_os_live_docker_proof.py` | _node_b_base, _token, _verify_node_b_health, _verify_skrzat_live, _verify_daszek_html (+1) |
| `tools/gmail_audit/scripts/agent_job_store_fallback_guard_proof.py` | _find_call_sites, _run_guard_unit_tests, _assert_guard_behavior, _audit_callers, main |

## Entry Points

Start here when exploring this area:

- **`main`** (Function) — `scripts/sequential_gmail_ingress_daszek.py:129`
- **`build_period_query`** (Function) — `tools/gmail_audit/gmail_fetch.py:284`
- **`bounded_text_tail`** (Function) — `tools/gmail_audit/sequential_ingress_helpers.py:21`
- **`load_failed_items_records`** (Function) — `tools/gmail_audit/sequential_ingress_helpers.py:56`
- **`make_failed_item_record`** (Function) — `tools/gmail_audit/sequential_ingress_helpers.py:81`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `main` | Function | `scripts/sequential_gmail_ingress_daszek.py` | 129 |
| `build_period_query` | Function | `tools/gmail_audit/gmail_fetch.py` | 284 |
| `bounded_text_tail` | Function | `tools/gmail_audit/sequential_ingress_helpers.py` | 21 |
| `load_failed_items_records` | Function | `tools/gmail_audit/sequential_ingress_helpers.py` | 56 |
| `make_failed_item_record` | Function | `tools/gmail_audit/sequential_ingress_helpers.py` | 81 |
| `parse_newer_than_days` | Function | `tools/gmail_audit/sequential_ingress_helpers.py` | 213 |
| `build_gmail_intake_message_command` | Function | `tools/gmail_audit/sequential_ingress_helpers.py` | 231 |
| `compute_retry_delay` | Function | `tools/gmail_audit/sequential_ingress_helpers.py` | 339 |
| `main` | Function | `tools/gmail_audit/scripts/daszek_local_133_proof.py` | 439 |
| `main` | Function | `tools/gmail_audit/scripts/daszek_os_event_w0_proof.py` | 36 |
| `check_files_exist` | Function | `tools/scripts/agent_harness_audit.py` | 94 |
| `check_governance_hubs_exist` | Function | `tools/scripts/agent_harness_audit.py` | 106 |
| `check_mcp_server_allowlist` | Function | `tools/scripts/agent_harness_audit.py` | 154 |
| `check_rules_governance` | Function | `tools/scripts/agent_harness_audit.py` | 204 |
| `check_export_gitnexus` | Function | `tools/scripts/agent_harness_audit.py` | 232 |
| `check_registry_skill_dirs` | Function | `tools/scripts/agent_harness_audit.py` | 280 |
| `check_codex_agents_knowledge_spine` | Function | `tools/scripts/agent_harness_audit.py` | 325 |
| `check_doc_lengths` | Function | `tools/scripts/agent_harness_audit.py` | 358 |
| `check_hooks_json` | Function | `tools/scripts/agent_harness_audit.py` | 377 |
| `check_stale_summary_refs` | Function | `tools/scripts/agent_harness_audit.py` | 439 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Main → _env_is_truthy` | cross_community | 5 |
| `Main → _env_is_truthy` | cross_community | 5 |
| `Main → _env_is_truthy` | cross_community | 5 |
| `Main → Fetch_facts_for_case` | cross_community | 5 |
| `Main → _repo_root` | intra_community | 4 |
| `Main → Default_env_candidates` | cross_community | 4 |
| `Main → Validate_agent_runtime_mode_not_primary` | cross_community | 4 |
| `Main → _case_os_profile_env_overrides` | cross_community | 4 |
| `Main → Default_env_candidates` | cross_community | 4 |
| `Main → Validate_agent_runtime_mode_not_primary` | cross_community | 4 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 25 calls |
| Agent_runtime | 8 calls |
| Tests | 8 calls |

## How to Explore

1. `context({name: "main"})` — see callers and callees
2. `query({search_query: "scripts"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
