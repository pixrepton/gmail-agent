---
name: llm-contracts
description: "Skill for the Llm_contracts area of gmail-agent. 22 symbols across 3 files."
---

# Llm_contracts

22 symbols | 3 files | Cohesion: 100%

## When to Use

- Working with code in `tools/`
- Understanding how engagement_snapshot_v2_json_schema, main, test_schema_json_file_matches_pydantic work
- Modifying llm_contracts-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | StrictModel, OperationalStatus, HvacLocation, HvacProfile, GapItem (+15) |
| `tools/gmail_audit/scripts/export_engagement_snapshot_schema.py` | main |
| `tools/gmail_audit/tests/test_agent_pr_a_b_complete.py` | test_schema_json_file_matches_pydantic |

## Entry Points

Start here when exploring this area:

- **`engagement_snapshot_v2_json_schema`** (Function) — `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py:301`
- **`main`** (Function) — `tools/gmail_audit/scripts/export_engagement_snapshot_schema.py:18`
- **`test_schema_json_file_matches_pydantic`** (Function) — `tools/gmail_audit/tests/test_agent_pr_a_b_complete.py:45`
- **`StrictModel`** (Class) — `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py:9`
- **`OperationalStatus`** (Class) — `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py:13`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `StrictModel` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 9 |
| `OperationalStatus` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 13 |
| `HvacLocation` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 25 |
| `HvacProfile` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 30 |
| `GapItem` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 39 |
| `ActionItem` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 45 |
| `HitlGate` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 56 |
| `ReasoningTraceItem` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 61 |
| `UnderstandingRiskItem` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 66 |
| `CaseUnderstandingProjection` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 72 |
| `CaseUnderstandingProvenance` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 91 |
| `PolicyActionEnvelopeV1` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 124 |
| `SemanticPolicyPlanConsistencyV1` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 144 |
| `DecisionDivergenceObservationV1` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 169 |
| `ToolCallItem` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 203 |
| `MaterializeProposalItem` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 208 |
| `AgentMemory` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 221 |
| `FeedVisibility` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 255 |
| `EngagementSnapshotV2` | Class | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 277 |
| `engagement_snapshot_v2_json_schema` | Function | `tools/gmail_audit/llm_contracts/engagement_snapshot_v2.py` | 301 |

## How to Explore

1. `context({name: "engagement_snapshot_v2_json_schema"})` — see callers and callees
2. `query({search_query: "llm_contracts"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
