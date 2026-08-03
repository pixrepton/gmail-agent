---
name: integration
description: "Skill for the Integration area of gmail-agent. 11 symbols across 4 files."
---

# Integration

11 symbols | 4 files | Cohesion: 82%

## When to Use

- Working with code in `tools/`
- Understanding how test_sanitize_user_input_truncates_long, test_sanitize_user_input_detects_injection, test_sanitize_user_input_passes_clean work
- Modifying integration-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py` | test_sanitize_user_input_truncates_long, test_sanitize_user_input_detects_injection, test_sanitize_user_input_passes_clean, test_sanitize_user_input_empty |
| `tools/gmail_audit/tests/integration/test_agent_e2e_flow.py` | _make_test_snapshot, setUp, test_agent_concurrency_semaphore_exists, test_hitl_gate_halts_loop |
| `tools/gmail_audit/tests/integration/test_reconcile_full_flow.py` | _make_signal, test_signal_contract_constructs |
| `tools/gmail_audit/api_app.py` | _sanitize_user_input |

## Entry Points

Start here when exploring this area:

- **`test_sanitize_user_input_truncates_long`** (Method) — `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py:15`
- **`test_sanitize_user_input_detects_injection`** (Method) — `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py:21`
- **`test_sanitize_user_input_passes_clean`** (Method) — `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py:27`
- **`test_sanitize_user_input_empty`** (Method) — `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py:33`
- **`setUp`** (Method) — `tools/gmail_audit/tests/integration/test_agent_e2e_flow.py:31`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `test_sanitize_user_input_truncates_long` | Method | `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py` | 15 |
| `test_sanitize_user_input_detects_injection` | Method | `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py` | 21 |
| `test_sanitize_user_input_passes_clean` | Method | `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py` | 27 |
| `test_sanitize_user_input_empty` | Method | `tools/gmail_audit/tests/integration/test_agent_chat_endpoint.py` | 33 |
| `setUp` | Method | `tools/gmail_audit/tests/integration/test_agent_e2e_flow.py` | 31 |
| `test_agent_concurrency_semaphore_exists` | Method | `tools/gmail_audit/tests/integration/test_agent_e2e_flow.py` | 49 |
| `test_hitl_gate_halts_loop` | Method | `tools/gmail_audit/tests/integration/test_agent_e2e_flow.py` | 73 |
| `test_signal_contract_constructs` | Method | `tools/gmail_audit/tests/integration/test_reconcile_full_flow.py` | 83 |
| `_sanitize_user_input` | Function | `tools/gmail_audit/api_app.py` | 121 |
| `_make_test_snapshot` | Function | `tools/gmail_audit/tests/integration/test_agent_e2e_flow.py` | 18 |
| `_make_signal` | Method | `tools/gmail_audit/tests/integration/test_reconcile_full_flow.py` | 60 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Tests | 3 calls |

## How to Explore

1. `context({name: "test_sanitize_user_input_truncates_long"})` — see callers and callees
2. `query({search_query: "integration"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
