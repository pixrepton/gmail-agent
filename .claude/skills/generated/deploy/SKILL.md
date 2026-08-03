---
name: deploy
description: "Skill for the Deploy area of gmail-agent. 5 symbols across 1 files."
---

# Deploy

5 symbols | 1 files | Cohesion: 100%

## When to Use

- Working with code in `deploy/`
- Understanding how validate_drain, validate_pending_after, main work
- Modifying deploy-related functionality

## Key Files

| File | Symbols |
|------|---------|
| `deploy/vps-prove-operator-loop-validate.py` | _load, _first_drain_row, validate_drain, validate_pending_after, main |

## Entry Points

Start here when exploring this area:

- **`validate_drain`** (Function) — `deploy/vps-prove-operator-loop-validate.py:35`
- **`validate_pending_after`** (Function) — `deploy/vps-prove-operator-loop-validate.py:61`
- **`main`** (Function) — `deploy/vps-prove-operator-loop-validate.py:73`

## Key Symbols

| Symbol | Type | File | Line |
|--------|------|------|------|
| `validate_drain` | Function | `deploy/vps-prove-operator-loop-validate.py` | 35 |
| `validate_pending_after` | Function | `deploy/vps-prove-operator-loop-validate.py` | 61 |
| `main` | Function | `deploy/vps-prove-operator-loop-validate.py` | 73 |
| `_load` | Function | `deploy/vps-prove-operator-loop-validate.py` | 10 |
| `_first_drain_row` | Function | `deploy/vps-prove-operator-loop-validate.py` | 14 |

## How to Explore

1. `context({name: "validate_drain"})` — see callers and callees
2. `query({search_query: "deploy"})` — find related execution flows
3. Read key files listed above for implementation details
4. `explain({target: "<file or symbol>"})` — persisted taint findings (source→sink data flows), when indexed with `--pdg`
