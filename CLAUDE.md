# CLAUDE.md — Claude Code adapter

Canonical project policy lives in root AGENTS.md, knowledge/INDEX.md
and gmail-agent/AGENTS.md.

Read those files first. The generated GitNexus section below contains
Claude Code tool-routing guidance only and does not override project policy,
ownership, memory or runtime-proof rules.

<!-- gitnexus:start -->

# GitNexus — Code Intelligence

This project is indexed by GitNexus as **gmail-agent** (65676 symbols, 187348 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user. For unified PDG impact, add `mode: "pdg"` with optional `line: <N>` — it returns statement-level `affectedStatements` over CDG + REACHING_DEF and inter-procedural symbols in `interproceduralByDepth`/`byDepth`; no-layer/degraded PDG results are UNKNOWN-risk notes (`--pdg` layer).
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "handoff/20260719-1403-latest"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).
- For control/data dependence, `pdg_query({mode: "controls", target: "fileOrSymbol"})` answers "under what condition does X run?" (CDG, incl. guard clauses) and `pdg_query({mode: "flows", target, variable})` traces "where does variable Y flow?" (REACHING_DEF). `--pdg` layer.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource                                     | Use for                                  |
| -------------------------------------------- | ---------------------------------------- |
| `gitnexus://repo/gmail-agent/context`        | Codebase overview, check index freshness |
| `gitnexus://repo/gmail-agent/clusters`       | All functional areas                     |
| `gitnexus://repo/gmail-agent/processes`      | All execution flows                      |
| `gitnexus://repo/gmail-agent/process/{name}` | Step-by-step execution trace             |

## Cross-Repo Groups

This repository is listed under GitNexus **group(s): topinstal-workspace** (see `~/.gitnexus/groups/`). For cross-repo analysis, use MCP tools `impact`, `query`, and `context` with `repo` set to `@<groupName>` or `@<groupName>/<memberPath>` (paths match keys in that group’s `group.yaml`). Use `group_list` / `group_sync` for membership and sync. From the project root: `node .gitnexus/run.cjs group list`, `node .gitnexus/run.cjs group sync <name>`, `node .gitnexus/run.cjs group impact <name> --target <symbol> --repo <group-path>` (the `.gitnexus/run.cjs` path is repo-root-relative).

## CLI

| Task                                                 | Read this skill file                                        |
| ---------------------------------------------------- | ----------------------------------------------------------- |
| Understand architecture / "How does X work?"         | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md`       |
| Blast radius / "What breaks if I change X?"          | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?"                     | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md`       |
| Rename / extract / split / refactor                  | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md`     |
| Tools, resources, schema reference                   | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md`           |
| Index, status, clean, wiki CLI commands              | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md`             |
| Work in the Gmail_audit area (2643 symbols)          | `.claude/skills/generated/gmail-audit/SKILL.md`             |
| Work in the Tests area (1951 symbols)                | `.claude/skills/generated/tests/SKILL.md`                   |
| Work in the Agent_runtime area (343 symbols)         | `.claude/skills/generated/agent-runtime/SKILL.md`           |
| Work in the Scripts area (214 symbols)               | `.claude/skills/generated/scripts/SKILL.md`                 |
| Work in the Mailbox_memory area (165 symbols)        | `.claude/skills/generated/mailbox-memory/SKILL.md`          |
| Work in the Correlation_registry area (141 symbols)  | `.claude/skills/generated/correlation-registry/SKILL.md`    |
| Work in the Case_intelligence area (73 symbols)      | `.claude/skills/generated/case-intelligence/SKILL.md`       |
| Work in the Event_spine area (50 symbols)            | `.claude/skills/generated/event-spine/SKILL.md`             |
| Work in the Daszek_engagement_feed area (40 symbols) | `.claude/skills/generated/daszek-engagement-feed/SKILL.md`  |
| Work in the Tools area (35 symbols)                  | `.claude/skills/generated/tools/SKILL.md`                   |
| Work in the Llm_contracts area (22 symbols)          | `.claude/skills/generated/llm-contracts/SKILL.md`           |
| Work in the Business_dictionary area (22 symbols)    | `.claude/skills/generated/business-dictionary/SKILL.md`     |
| Work in the Integration area (11 symbols)            | `.claude/skills/generated/integration/SKILL.md`             |
| Work in the Unit area (7 symbols)                    | `.claude/skills/generated/unit/SKILL.md`                    |
| Work in the Deploy area (5 symbols)                  | `.claude/skills/generated/deploy/SKILL.md`                  |
| Work in the Memory_consolidation area (3 symbols)    | `.claude/skills/generated/memory-consolidation/SKILL.md`    |

<!-- gitnexus:end -->
