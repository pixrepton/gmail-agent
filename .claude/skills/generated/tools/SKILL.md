---
name: tools
description: "Skill for the Tools area of gmail-agent. 35 symbols across 9 files."
---

# Tools

35 symbols | 9 files | Cohesion: 82%

## When to Use

- Working with code in `tools/`
- Understanding how execute_update_case_status, execute_update_case_lifecycle, execute_archive_case work
- Modifying tools-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/agent_runtime/tools/handlers.py` | _proposal_result, propose_plan, _validate_proposal_args, propose_mutation, _add_coherence_warnings (+11) |
| `tools/gmail_audit/agent_runtime/tools/write_executors.py` | execute_update_case_status, execute_update_case_lifecycle, execute_archive_case, execute_restore_case, _write_fact_row (+4) |
| `tools/gmail_audit/tests/test_write_executors_status_metadata.py` | fetch_case, upsert_case, test_fail_closed_without_mutate_case |
| `tools/gmail_audit/agent_runtime/materialize.py` | new_proposal_id, append_materialize_proposal |
| `tools/gmail_audit/llm_contracts/case_lifecycle.py` | validate_transition |
| `tools/gmail_audit/agent_runtime/drive_file_reader.py` | download_and_parse_drive_file |
| `tools/gmail_audit/document_parse_contract.py` | to_extraction_dict |
| `tools/gmail_audit/tests/test_agent_pr_c_complete.py` | test_read_drive_file_uses_parser_chain |
| `tools/gmail_audit/mailbox_memory/facts.py` | stable_id |

## Entry Points

Start here when exploring this area:

- **`execute_update_case_status`** (Function) — `tools/gmail_audit/agent_runtime/tools/write_executors.py:194`
- **`execute_update_case_lifecycle`** (Function) — `tools/gmail_audit/agent_runtime/tools/write_executors.py:293`
- **`execute_archive_case`** (Function) — `tools/gmail_audit/agent_runtime/tools/write_executors.py:432`
- **`execute_restore_case`** (Function) — `tools/gmail_audit/agent_runtime/tools/write_executors.py:489`
- **`validate_transition`** (Function) — `tools/gmail_audit/llm_contracts/case_lifecycle.py:134`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `execute_update_case_status` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 194 |
| `execute_update_case_lifecycle` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 293 |
| `execute_archive_case` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 432 |
| `execute_restore_case` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 489 |
| `validate_transition` | Function | `tools/gmail_audit/llm_contracts/case_lifecycle.py` | 134 |
| `test_fail_closed_without_mutate_case` | Function | `tools/gmail_audit/tests/test_write_executors_status_metadata.py` | 144 |
| `new_proposal_id` | Function | `tools/gmail_audit/agent_runtime/materialize.py` | 129 |
| `append_materialize_proposal` | Function | `tools/gmail_audit/agent_runtime/materialize.py` | 133 |
| `propose_plan` | Function | `tools/gmail_audit/agent_runtime/tools/handlers.py` | 696 |
| `propose_mutation` | Function | `tools/gmail_audit/agent_runtime/tools/handlers.py` | 898 |
| `download_and_parse_drive_file` | Function | `tools/gmail_audit/agent_runtime/drive_file_reader.py` | 9 |
| `read_google_drive_file` | Function | `tools/gmail_audit/agent_runtime/tools/handlers.py` | 136 |
| `retry_hard_parse` | Function | `tools/gmail_audit/agent_runtime/tools/handlers.py` | 714 |
| `test_read_drive_file_uses_parser_chain` | Function | `tools/gmail_audit/tests/test_agent_pr_c_complete.py` | 241 |
| `apply_facts_to_snapshot_and_store` | Function | `tools/gmail_audit/agent_runtime/tools/handlers.py` | 91 |
| `stable_id` | Function | `tools/gmail_audit/mailbox_memory/facts.py` | 95 |
| `execute_add_case_note` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 362 |
| `execute_add_case_label` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 397 |
| `execute_generate_draft` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 729 |
| `execute_add_deadline` | Function | `tools/gmail_audit/agent_runtime/tools/write_executors.py` | 785 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Propose_mutation → New_proposal_id` | intra_community | 4 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 6 calls |
| Tests | 3 calls |

## How to Explore

1. `context({name: "execute_update_case_status"})` — see callers and callees
2. `query({search_query: "tools"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
