---
name: unit
description: "Skill for the Unit area of gmail-agent. 7 symbols across 4 files."
---

# Unit

7 symbols | 4 files | Cohesion: 86%

## When to Use

- Working with code in `tools/`
- Understanding how build_missing_info, test_r6_build_missing_info_drops_awaited_decision, test_r6_build_missing_info_drops_speculative_conditional work
- Modifying unit-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/tests/unit/test_case_intelligence_units.py` | test_missing_info_empty, test_missing_info_critical_keyword, test_missing_info_weak_link_prompts_confirmation |
| `tools/gmail_audit/tests/test_understanding_quality_contract.py` | test_r6_build_missing_info_drops_awaited_decision, test_r6_build_missing_info_drops_speculative_conditional |
| `tools/gmail_audit/case_intelligence/missing_info.py` | build_missing_info |
| `tools/gmail_audit/case_intelligence/validators.py` | _missing_info_label_pl |

## Entry Points

Start here when exploring this area:

- **`build_missing_info`** (Function) — `tools/gmail_audit/case_intelligence/missing_info.py:37`
- **`test_r6_build_missing_info_drops_awaited_decision`** (Function) — `tools/gmail_audit/tests/test_understanding_quality_contract.py:83`
- **`test_r6_build_missing_info_drops_speculative_conditional`** (Function) — `tools/gmail_audit/tests/test_understanding_quality_contract.py:97`
- **`test_missing_info_empty`** (Method) — `tools/gmail_audit/tests/unit/test_case_intelligence_units.py:54`
- **`test_missing_info_critical_keyword`** (Method) — `tools/gmail_audit/tests/unit/test_case_intelligence_units.py:62`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `build_missing_info` | Function | `tools/gmail_audit/case_intelligence/missing_info.py` | 37 |
| `test_r6_build_missing_info_drops_awaited_decision` | Function | `tools/gmail_audit/tests/test_understanding_quality_contract.py` | 83 |
| `test_r6_build_missing_info_drops_speculative_conditional` | Function | `tools/gmail_audit/tests/test_understanding_quality_contract.py` | 97 |
| `test_missing_info_empty` | Method | `tools/gmail_audit/tests/unit/test_case_intelligence_units.py` | 54 |
| `test_missing_info_critical_keyword` | Method | `tools/gmail_audit/tests/unit/test_case_intelligence_units.py` | 62 |
| `test_missing_info_weak_link_prompts_confirmation` | Method | `tools/gmail_audit/tests/unit/test_case_intelligence_units.py` | 70 |
| `_missing_info_label_pl` | Function | `tools/gmail_audit/case_intelligence/validators.py` | 157 |

## Connected Areas

| Area | Connections |
|------|-------------|
| Gmail_audit | 1 calls |

## How to Explore

1. `context({name: "build_missing_info"})` — see callers and callees
2. `query({search_query: "unit"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
