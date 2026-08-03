---
name: mailbox-memory
description: "Skill for the Mailbox_memory area of gmail-agent. 165 symbols across 16 files."
---

# Mailbox_memory

165 symbols | 16 files | Cohesion: 69%

## When to Use

- Working with code in `tools/`
- Understanding how test_active_v2_feed_ingest_returns_current_context_snapshot, test_postgres_connect_uses_bounded_connect_timeout, replace_rows work
- Modifying mailbox_memory-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/mailbox_memory/postgres.py` | fetch_case_snapshot_versions, fetch_resolved_cases_by_family_and_fact_keys, fetch_messages_for_case, fetch_cases, fetch_events (+77) |
| `tools/gmail_audit/mailbox_memory/protocol.py` | upsert_message, replace_message_facts, fetch_events_for_case, bootstrap, upsert_drive_ingest_run (+20) |
| `tools/gmail_audit/mailbox_memory/inmemory.py` | fetch_snapshot, fetch_drive_facts_for_case, fetch_drive_documents, fetch_chunks_for_case, fetch_drive_chunks_for_case (+11) |
| `tools/gmail_audit/mailbox_memory_runtime.py` | stage_allows, ingest_message, _extract_first_email, _guess_customer_name, build_case_context_pack (+4) |
| `tools/gmail_audit/tests/test_mailbox_memory_runtime.py` | _build_snapshot, test_ingest_and_finalize_build_snapshot_conflicts_and_context, test_refresh_document_intelligence_emits_completion_event, test_postgres_replace_paths_are_serially_idempotent_and_concurrency_safe, replace_rows (+4) |
| `tools/gmail_audit/drive_ingest_runtime.py` | bootstrap, ingest_batch, rebuild_graph, bounded_refresh_document_intelligence_for_cases, _persist_normalized_candidate (+1) |
| `tools/gmail_audit/tests/test_drive_ingest_runtime.py` | _build_docx_bytes, test_ingest_batch_stores_drive_docs_handles_blocked_extraction_and_upserts_graph, test_media_asset_inherits_case_from_parent_folder_anchor |
| `tools/gmail_audit/mailbox_memory/facts.py` | _is_real_email, _build_fact, extract_facts_from_text |
| `tools/gmail_audit/tests/test_mailbox_memory_store.py` | test_postgres_connect_uses_bounded_connect_timeout, test_postgres_mutate_case_uses_one_cursor_and_preserves_requested_case_id |
| `tools/gmail_audit/mailbox_memory/schema.py` | _parse_vector_literal_coords, _cosine_similarity |

## Entry Points

Start here when exploring this area:

- **`test_active_v2_feed_ingest_returns_current_context_snapshot`** (Function) — `tools/gmail_audit/tests/test_case_context_snapshot_coherence.py:178`
- **`test_postgres_connect_uses_bounded_connect_timeout`** (Function) — `tools/gmail_audit/tests/test_mailbox_memory_store.py:74`
- **`replace_rows`** (Function) — `tools/gmail_audit/tests/test_mailbox_memory_runtime.py:680`
- **`patch_signal_engagement`** (Function) — `tools/gmail_audit/agent_runtime/signal_engagement.py:11`
- **`test_patch_signal_engagement_in_memory`** (Function) — `tools/gmail_audit/tests/test_agent_pr_a_b_complete.py:149`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `test_active_v2_feed_ingest_returns_current_context_snapshot` | Function | `tools/gmail_audit/tests/test_case_context_snapshot_coherence.py` | 178 |
| `test_postgres_connect_uses_bounded_connect_timeout` | Function | `tools/gmail_audit/tests/test_mailbox_memory_store.py` | 74 |
| `replace_rows` | Function | `tools/gmail_audit/tests/test_mailbox_memory_runtime.py` | 680 |
| `patch_signal_engagement` | Function | `tools/gmail_audit/agent_runtime/signal_engagement.py` | 11 |
| `test_patch_signal_engagement_in_memory` | Function | `tools/gmail_audit/tests/test_agent_pr_a_b_complete.py` | 149 |
| `build_case_context_pack` | Function | `tools/gmail_audit/mailbox_memory_runtime.py` | 1512 |
| `build_source_refs` | Function | `tools/gmail_audit/mailbox_memory_runtime.py` | 2039 |
| `extract_facts_from_text` | Function | `tools/gmail_audit/mailbox_memory/facts.py` | 104 |
| `test_another_signal_or_case_never_overwrites_the_existing_record` | Function | `tools/gmail_audit/tests/test_slice3b_policy_execution_spine.py` | 150 |
| `test_foreign_case_or_message_correlation_is_rejected_without_a_write` | Function | `tools/gmail_audit/tests/test_slice3b_policy_execution_spine.py` | 173 |
| `run_mutation` | Function | `tools/gmail_audit/tests/test_mailbox_memory_runtime.py` | 758 |
| `test_postgres_mutate_case_uses_one_cursor_and_preserves_requested_case_id` | Function | `tools/gmail_audit/tests/test_mailbox_memory_store.py` | 91 |
| `fetch_case_snapshot_versions` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 474 |
| `fetch_resolved_cases_by_family_and_fact_keys` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 589 |
| `fetch_messages_for_case` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 659 |
| `fetch_cases` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 670 |
| `fetch_events` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 691 |
| `fetch_action_proposals` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 746 |
| `fetch_policy_decisions` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 807 |
| `fetch_action_proposals_v2` | Method | `tools/gmail_audit/mailbox_memory/postgres.py` | 879 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Ingest_message → Get_engagement` | cross_community | 5 |
| `Ingest_message → Update_identity_display_name` | cross_community | 5 |
| `Ingest_message → Find_recent_engagement_for_email` | cross_community | 5 |
| `Main → Fetch_facts_for_case` | cross_community | 5 |
| `Rebuild_graph → Stable_graph_node_id` | cross_community | 5 |
| `Rebuild_graph → Stable_graph_edge_id` | cross_community | 5 |
| `Ingest_message → Upsert_link` | cross_community | 4 |
| `Finalize_case → Fetch_thread_memory` | cross_community | 4 |
| `Main → Bootstrap` | cross_community | 4 |
| `Rebuild_graph → Infer_manufacturer` | cross_community | 4 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 61 calls |
| Tests | 18 calls |
| Agent_runtime | 2 calls |

## How to Explore

1. `context({name: "test_active_v2_feed_ingest_returns_current_context_snapshot"})` — see callers and callees
2. `query({search_query: "mailbox_memory"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
