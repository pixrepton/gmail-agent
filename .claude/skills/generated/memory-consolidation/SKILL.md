---
name: memory-consolidation
description: "Skill for the Memory_consolidation area of gmail-agent. 3 symbols across 1 files."
---

# Memory_consolidation

3 symbols | 1 files | Cohesion: 100%

## When to Use

- Working with code in `tools/`
- Understanding how extract_facts_from_turns, dedupe_facts, consolidate_engagement_turns work
- Modifying memory_consolidation-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/agent_runtime/memory_consolidation/__init__.py` | extract_facts_from_turns, dedupe_facts, consolidate_engagement_turns |

## Entry Points

Start here when exploring this area:

- **`extract_facts_from_turns`** (Function) — `tools/gmail_audit/agent_runtime/memory_consolidation/__init__.py:18`
- **`dedupe_facts`** (Function) — `tools/gmail_audit/agent_runtime/memory_consolidation/__init__.py:39`
- **`consolidate_engagement_turns`** (Function) — `tools/gmail_audit/agent_runtime/memory_consolidation/__init__.py:51`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `extract_facts_from_turns` | Function | `tools/gmail_audit/agent_runtime/memory_consolidation/__init__.py` | 18 |
| `dedupe_facts` | Function | `tools/gmail_audit/agent_runtime/memory_consolidation/__init__.py` | 39 |
| `consolidate_engagement_turns` | Function | `tools/gmail_audit/agent_runtime/memory_consolidation/__init__.py` | 51 |

## How to Explore

1. `context({name: "extract_facts_from_turns"})` — see callers and callees
2. `query({search_query: "memory_consolidation"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
