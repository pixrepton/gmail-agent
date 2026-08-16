# AGENTS.md — gmail-agent

Status: active L2 adapter (Typ A). **Node B.**

## Safety capsule

- Code and local proof in this repo beat historical docs.
- Default target is **local Docker**, not VPS/production.
- No production mutation without explicit operator order and dedicated proof.
- Do not write other repos without explicit `ai_os_task` scope expansion.
- Cross-repo contract changes require Gate A here and in every changed consumer.
- Do not declare `done` without the appropriate Gate A / runtime proof for the layer changed.
- When opened inside `top-code workspace`, root `../AGENTS.md` also applies.
- Missing root does not waive these local rules.

## Role

Mailbox intake, cases, engagements, policy, decisions, execution state, operational feed, journal/replay.  
**Operational Source of Truth for cases.**

## Owns / Must not

| Owns | Must not |
|------|----------|
| Case / engagement / mailbox runtime | HVAC pricing / sizing / `OfferDTO` (`kalk-top`) |
| Policy and HITL execution contracts | Being a second UI SoT (Daszek is projection-only) |
| Durable Postgres case truth; Node B feed/projection builders | Live Gmail send or Calendar write without separate decision + proof |

## Read first

1. This file
2. Root `../AGENTS.md` when available
3. `../knowledge/INDEX.md` when the task needs cross-repo knowledge routing
4. `docs/core/PROJECT_README.md`, `docs/core/CONSTITUTION_V2_1.md`
5. `docs/runbooks/LAST_PROVEN_STATE.md` **only** for runtime claims
6. Current code and targeted tests

Do not treat historical handoffs, archives, or raw exports as active truth.

## Write and task scope

- Scope via `scripts/ai_os_task.py` (`gmail-agent:<path>`).
- Do not write `../knowledge/memory/*` without an explicit operator instruction.
- Do not create a local memory-bank.

## Gate A

**Full package gate (closeout):**

```powershell
python -m pytest tools/gmail_audit/tests -q
```

**Focused gate (iteration):** run the specific test modules covering the changed surface.

Doctor / Docker / host-container parity are **runtime/Gate B** concerns — use only when the change touches runtime images, compose, or live API behavior. They are not a substitute for the pytest Gate A above.

## Cross-repo contract changes

1. Identify the owning repo of the contract (often this repo for case/feed/HITL).
2. Change owner contract + tests first.
3. Then change consumers (e.g. `daszek`).
4. Run Gate A for owner and every changed consumer.
5. Do not invent client-only compatibility.
6. A `knowledge` doc update alone does not change runtime.

## Anti-goals

- Production as default target
- Stale full-row case overwrite; preserve atomic mutation and `decision_key` semantics
- Shadow Case OS outside this repo
- HVAC calculation inside Node B
- Final UI success semantics owned here but implemented only in Daszek without Node B proof

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **gmail-agent** (17005 symbols, 44580 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "handoff/20260719-1403-latest"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/gmail-agent/context` | Codebase overview, check index freshness |
| `gitnexus://repo/gmail-agent/clusters` | All functional areas |
| `gitnexus://repo/gmail-agent/processes` | All execution flows |
| `gitnexus://repo/gmail-agent/process/{name}` | Step-by-step execution trace |

## Cross-Repo Groups

This repository is listed under GitNexus **group(s): topinstal-workspace** (see `~/.gitnexus/groups/`). For cross-repo analysis, use MCP tools `impact`, `query`, and `context` with `repo` set to `@<groupName>` or `@<groupName>/<memberPath>` (paths match keys in that group’s `group.yaml`). Use `group_list` / `group_sync` for membership and sync. From the project root: `node .gitnexus/run.cjs group list`, `node .gitnexus/run.cjs group sync <name>`, `node .gitnexus/run.cjs group impact <name> --target <symbol> --repo <group-path>` (the `.gitnexus/run.cjs` path is repo-root-relative).

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
