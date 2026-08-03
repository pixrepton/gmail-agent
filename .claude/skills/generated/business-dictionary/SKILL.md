---
name: business-dictionary
description: "Skill for the Business_dictionary area of gmail-agent. 22 symbols across 5 files."
---

# Business_dictionary

22 symbols | 5 files | Cohesion: 61%

## When to Use

- Working with code in `tools/`
- Understanding how run_extract_cli, run_sync_cli, extract_terms_from_text work
- Modifying business_dictionary-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/business_dictionary/store.py` | _utc_now, _new_id, write_outbox_entry, upsert_term, ensure_dictionary_table (+4) |
| `tools/gmail_audit/business_dictionary/graph_store.py` | _neo4j_driver, upsert_term_node, search_graph, get_graph_stats, process_outbox (+1) |
| `tools/gmail_audit/business_dictionary/cli.py` | run_extract_cli, run_sync_cli, run_search_cli, run_outbox_process_cli |
| `tools/gmail_audit/api_app.py` | list_business_dictionary_terms, business_dictionary_stats |
| `tools/gmail_audit/business_dictionary/extractor.py` | extract_terms_from_text |

## Entry Points

Start here when exploring this area:

- **`run_extract_cli`** (Function) — `tools/gmail_audit/business_dictionary/cli.py:19`
- **`run_sync_cli`** (Function) — `tools/gmail_audit/business_dictionary/cli.py:109`
- **`extract_terms_from_text`** (Function) — `tools/gmail_audit/business_dictionary/extractor.py:33`
- **`upsert_term_node`** (Function) — `tools/gmail_audit/business_dictionary/graph_store.py:34`
- **`search_graph`** (Function) — `tools/gmail_audit/business_dictionary/graph_store.py:94`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `run_extract_cli` | Function | `tools/gmail_audit/business_dictionary/cli.py` | 19 |
| `run_sync_cli` | Function | `tools/gmail_audit/business_dictionary/cli.py` | 109 |
| `extract_terms_from_text` | Function | `tools/gmail_audit/business_dictionary/extractor.py` | 33 |
| `upsert_term_node` | Function | `tools/gmail_audit/business_dictionary/graph_store.py` | 34 |
| `search_graph` | Function | `tools/gmail_audit/business_dictionary/graph_store.py` | 94 |
| `write_outbox_entry` | Function | `tools/gmail_audit/business_dictionary/store.py` | 73 |
| `upsert_term` | Function | `tools/gmail_audit/business_dictionary/store.py` | 89 |
| `list_business_dictionary_terms` | Function | `tools/gmail_audit/api_app.py` | 1263 |
| `business_dictionary_stats` | Function | `tools/gmail_audit/api_app.py` | 1288 |
| `run_search_cli` | Function | `tools/gmail_audit/business_dictionary/cli.py` | 74 |
| `get_graph_stats` | Function | `tools/gmail_audit/business_dictionary/graph_store.py` | 159 |
| `ensure_dictionary_table` | Function | `tools/gmail_audit/business_dictionary/store.py` | 59 |
| `search_terms` | Function | `tools/gmail_audit/business_dictionary/store.py` | 144 |
| `get_stats` | Function | `tools/gmail_audit/business_dictionary/store.py` | 203 |
| `delete_term` | Function | `tools/gmail_audit/business_dictionary/store.py` | 231 |
| `run_outbox_process_cli` | Function | `tools/gmail_audit/business_dictionary/cli.py` | 184 |
| `process_outbox` | Function | `tools/gmail_audit/business_dictionary/graph_store.py` | 178 |
| `ensure_sync_outbox_table` | Function | `tools/gmail_audit/business_dictionary/store.py` | 66 |
| `_neo4j_driver` | Function | `tools/gmail_audit/business_dictionary/graph_store.py` | 20 |
| `_utc_now` | Function | `tools/gmail_audit/business_dictionary/store.py` | 51 |

## Execution Flows

| Flow | Type | Steps |
|------|------|-------|
| `Business_dictionary_stats → _env_is_truthy` | cross_community | 5 |
| `Business_dictionary_stats → Default_env_candidates` | cross_community | 4 |
| `Business_dictionary_stats → Validate_agent_runtime_mode_not_primary` | cross_community | 4 |
| `Business_dictionary_stats → _case_os_profile_env_overrides` | cross_community | 4 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 20 calls |

## How to Explore

1. `context({name: "run_extract_cli"})` — see callers and callees
2. `query({search_query: "business_dictionary"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
