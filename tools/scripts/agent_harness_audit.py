#!/usr/bin/env python3
"""Dev-only harness audit: no network, stdlib only. Exit non-zero on FAIL."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_DEV_DOCS = [
    "AGENTS.md",
    "README.md",
    "docs/README.md",
    "docs/core/PROJECT_README.md",
    "docs/runbooks/LAST_PROVEN_STATE.md",
    "docs/contracts/engagement_snapshot_v2.schema.json",
]

GOVERNANCE_HUB_FILES = [
    "AGENTS.md",
    "docs/core/PROJECT_README.md",
    "docs/runbooks/LAST_PROVEN_STATE.md",
]

CANONICAL_CONTEXT_FILES = [
    "AGENTS.md",
    "README.md",
    "docs/README.md",
    "docs/core/PROJECT_README.md",
    "docs/runbooks/LAST_PROVEN_STATE.md",
]

# Project `.cursor/mcp.json` — only these server ids (keys under mcpServers). Extend when a row is
# approved in MCP_ALLOWLIST_AND_RISK_MATRIX.md and mcp.json is updated.
ALLOWED_PROJECT_MCP_SERVER_IDS = frozenset({"gmail-agent-repo-assistant", "playwright"})


FORBIDDEN_CLAIMS = [
    "gitnexus proves runtime behavior",
    "static repo graph is business/case graph",
    "browser screenshot proves backend mutation by itself",
    "http proxy trace alone proves state mutation",
    "agent development harness closes gate b",
    "mcp tool is source of truth",
]



# Polish harness headings apply only to canonical control-plane docs (not scans/matrices).
_DOCS_DEV_HEADING_REQUIRED_FILES = frozenset(
    {
        "AGENT_DEVELOPMENT_HARNESS.md",
        "MCP_OPERATING_MODEL.md",
        "AGENT_SIDE_TOOLING.md",
        "GITNEXUS_REPO_INTEL.md",
        "LSP_FIRST_NAVIGATION.md",
        "AGENT_RULES_REGRESSION_PACK.md",
        "AGENT_HANDOVER_LEDGER.md",
        "DASZEK_BROWSER_PROOF_HARNESS.md",
        "REST_BRIDGE_DEBUG_PROOF_HARNESS.md",
        "HOOK_HYGIENE_AND_FAILURE_POLICY.md",
        "AGENT_CONTEXT_BUDGET_POLICY.md",
        "CURSOR_AGENT_TOOLING_STACK.md",
        "DASZEK_V3_TASK_UI_PRINCIPLES.md",
        "DASZEK_V3_UI_COPY.md",
    }
)

# Required `##` headings (exact titles) in each docs/dev harness doc.
_DOCS_DEV_HEADING_TITLES = [
    "Status",
    "Cel",
    "Czym to jest",
    "Czym to nie jest",
    "Kiedy używać",
    "Kiedy nie używać",
    "Checklist",
    "Failure modes",
]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig", errors="replace")


def _has_sources_heading_text(text: str) -> bool:
    return bool(re.search(r"(?m)^##\s+.*(Źródła|Linki|powiązane pliki)", text))


def check_files_exist() -> tuple[list[str], list[str]]:
    fails: list[str] = []
    oks: list[str] = []
    for rel in REQUIRED_DEV_DOCS:
        path = REPO_ROOT / rel
        if not path.is_file():
            fails.append(f"Missing required file: {rel}")
        else:
            oks.append(rel)
    return oks, fails


def check_governance_hubs_exist() -> list[str]:
    fails: list[str] = []
    for rel in GOVERNANCE_HUB_FILES:
        if not (REPO_ROOT / rel).is_file():
            fails.append(f"Missing governance hub: {rel}")
    return fails


def check_canonical_context() -> list[str]:
    fails: list[str] = []
    for rel in CANONICAL_CONTEXT_FILES:
        if not (REPO_ROOT / rel).is_file():
            fails.append(f"Missing canonical context file: {rel}")
    if fails:
        return fails

    agents_text = _read(REPO_ROOT / "AGENTS.md") if (REPO_ROOT / "AGENTS.md").is_file() else ""
    router_path = REPO_ROOT / ".cursor" / "rules" / "00-topinstal-core-router.mdc"
    router_text = _read(router_path) if router_path.is_file() else ""
    stop_hook_path = REPO_ROOT / ".cursor" / "hooks" / "stop-followup.js"
    stop_hook_text = _read(stop_hook_path) if stop_hook_path.is_file() else ""

    for rel in ("docs/core/PROJECT_README.md", "docs/runbooks/LAST_PROVEN_STATE.md"):
        if rel not in agents_text:
            fails.append(f"AGENTS.md must route through {rel}")
    if "memory-bank" in agents_text or "memory-bank" in router_text or "memory-bank" in stop_hook_text:
        fails.append("active agent routing must not reference memory-bank")
    return fails


def check_exactly_one_always_apply_rule() -> list[str]:
    fails: list[str] = []
    rules = REPO_ROOT / ".cursor" / "rules"
    if not rules.is_dir():
        return fails
    active = [
        f.name
        for f in sorted(rules.glob("*.mdc"))
        if re.search(r"(?m)^alwaysApply:\s*true\s*$", _read(f))
    ]
    if active != ["00-topinstal-core-router.mdc"]:
        fails.append(
            "Cursor rules must have exactly one alwaysApply true router: "
            f"expected ['00-topinstal-core-router.mdc'], got {active}"
        )
    return fails


def check_mcp_server_allowlist() -> list[str]:
    """FAIL if mcp.json enables a server id outside the project allowlist (no LLM)."""
    fails: list[str] = []
    p = REPO_ROOT / ".cursor" / "mcp.json"
    if not p.is_file():
        return fails
    try:
        data = json.loads(_read(p))
    except json.JSONDecodeError:
        return fails
    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return fails
    for key in servers:
        if key not in ALLOWED_PROJECT_MCP_SERVER_IDS:
            fails.append(
                f"mcp.json: server {key!r} not in ALLOWED_PROJECT_MCP_SERVER_IDS; "
                "update MCP_OPERATING_MODEL.md (appendix A allowlist) + this audit before enabling"
            )
    return fails


def _governance_scan_paths() -> list[Path]:
    paths: list[Path] = [REPO_ROOT / "AGENTS.md"]
    rules = REPO_ROOT / ".cursor" / "rules"
    if rules.is_dir():
        paths.extend(sorted(rules.glob("*.mdc")))
    skills_root = REPO_ROOT / ".agents" / "skills"
    if skills_root.is_dir():
        paths.extend(skills_root.rglob("*.md"))
    cursor_skills = REPO_ROOT / ".cursor" / "skills"
    if cursor_skills.is_dir():
        paths.extend(cursor_skills.rglob("*.md"))
    return paths


def check_agents_harness_link() -> list[str]:
    fails: list[str] = []
    agents = REPO_ROOT / "AGENTS.md"
    if not agents.is_file():
        fails.append("AGENTS.md missing")
        return fails
    text = _read(agents)
    if "docs/core/PROJECT_README.md" not in text:
        fails.append("AGENTS.md must link to docs/core/PROJECT_README.md")
    if "docs/runbooks/LAST_PROVEN_STATE.md" not in text:
        fails.append("AGENTS.md must link to docs/runbooks/LAST_PROVEN_STATE.md")
    return fails


def check_rules_governance() -> list[str]:
    fails: list[str] = []
    rules_dir = REPO_ROOT / ".cursor" / "rules"
    if not rules_dir.is_dir():
        fails.append(".cursor/rules missing")
        return fails
    blob = ""
    for mdc in sorted(rules_dir.glob("*.mdc")):
        blob += _read(mdc).lower()
    if "harness" not in blob:
        fails.append(".cursor/rules: no 'harness' in any .mdc (add rule 31 or extend 30)")
    if "gitnexus" not in blob:
        fails.append(".cursor/rules: no 'gitnexus' mention")
    if not re.search(r"lsp|symbol|call chain|call hierarchy", blob):
        fails.append(".cursor/rules: no LSP/symbol/call-chain governance wording")
    return fails


def check_gitignore_gitnexus() -> list[str]:
    fails: list[str] = []
    gi = REPO_ROOT / ".gitignore"
    if gi.is_file():
        content = _read(gi)
        if ".gitnexus" not in content:
            fails.append(".gitignore must ignore .gitnexus/")
    return fails


def check_export_gitnexus() -> list[str]:
    fails: list[str] = []
    ex = REPO_ROOT / "tools" / "scripts" / "export_hardening.py"
    if not ex.is_file():
        return fails
    content = _read(ex)
    if ".gitnexus" not in content:
        fails.append("export_hardening.py should list .gitnexus in forbidden export set")
    return fails


def check_forbidden_claims() -> list[str]:
    fails: list[str] = []
    targets: list[Path] = [REPO_ROOT / "AGENTS.md"]
    dev = REPO_ROOT / "docs" / "dev"
    if dev.is_dir():
        targets.extend(sorted(dev.glob("*.md")))
    for p in targets:
        if not p.is_file():
            continue
        lower = _read(p).lower()
        rel = p.relative_to(REPO_ROOT).as_posix()
        for phrase in FORBIDDEN_CLAIMS:
            if phrase in lower:
                fails.append(f"Forbidden claim phrase in {rel}: {phrase!r}")
    return fails


def check_docs_dev_headings() -> list[str]:
    warns: list[str] = []
    dev = REPO_ROOT / "docs" / "dev"
    if not dev.is_dir():
        return warns
    for p in sorted(dev.glob("*.md")):
        if p.name not in _DOCS_DEV_HEADING_REQUIRED_FILES:
            continue
        text = _read(p)
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if first_line.startswith("# Moved") or first_line.lower() == "moved":
            continue
        missing = [t for t in _DOCS_DEV_HEADING_TITLES if not re.search(rf"(?m)^##\s+{re.escape(t)}\s*$", text)]
        if missing:
            warns.append(f"{p.name}: missing headings: {', '.join(missing)}")
        elif not _has_sources_heading_text(text):
            warns.append(f"{p.name}: add «Źródła / powiązane pliki» (rename «Linki» if needed)")
    return warns


def check_registry_skill_dirs() -> list[str]:
    """WARN if a directory under .agents/skills lacks a reference line in the registry."""
    warns: list[str] = []
    reg = REPO_ROOT / "docs" / "dev" / "AGENT_SKILLS_REGISTRY.md"
    if not reg.is_file():
        return warns
    body = _read(reg)
    if body.lstrip().startswith("# Moved"):
        return warns
    root = REPO_ROOT / ".agents" / "skills"
    if not root.is_dir():
        return warns
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if name not in body:
            warns.append(f"AGENT_SKILLS_REGISTRY.md missing row or mention for `.agents/skills/{name}/`")
    cursor = REPO_ROOT / ".cursor" / "skills"
    if cursor.is_dir():
        for d in sorted(cursor.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            if name not in body:
                warns.append(f"AGENT_SKILLS_REGISTRY.md missing row or mention for `.cursor/skills/{name}/`")
    return warns


def check_codex_agents_in_registry() -> list[str]:
    warns: list[str] = []
    reg = REPO_ROOT / "docs" / "dev" / "AGENT_SKILLS_REGISTRY.md"
    agents_dir = REPO_ROOT / ".codex" / "agents"
    if not reg.is_file() or not agents_dir.is_dir():
        return warns
    body = _read(reg)
    if body.lstrip().startswith("# Moved"):
        return warns
    for f in sorted(agents_dir.glob("*.toml")):
        stem = f.stem
        if stem not in body:
            warns.append(f"AGENT_SKILLS_REGISTRY.md missing `.codex/agents/{f.name}` stem `{stem}`")
    return warns


def check_codex_agents_knowledge_spine() -> list[str]:
    fails: list[str] = []
    agents_dir = REPO_ROOT / ".codex" / "agents"
    if not agents_dir.is_dir():
        return fails
    for f in sorted(agents_dir.glob("*.toml")):
        text = _read(f)
        if "memory-bank/" in text:
            fails.append(f"{f.relative_to(REPO_ROOT).as_posix()} must not route through memory-bank")
    shared = REPO_ROOT / ".codex" / "SHARED_AGENT_BASE.md"
    if shared.is_file():
        text = _read(shared)
        if "memory-bank/" in text:
            fails.append(".codex/SHARED_AGENT_BASE.md must not route through memory-bank")
    return fails


def check_optional_harness_index_links() -> list[str]:
    warns: list[str] = []
    needle = "AGENT_DEVELOPMENT_HARNESS.md"
    targets = [
        ("docs/README.md", REPO_ROOT / "docs" / "README.md"),
        ("docs/00_INDEX.md", REPO_ROOT / "docs" / "00_INDEX.md"),
    ]
    missing: list[str] = []
    for label, path in targets:
        if path.is_file() and needle not in _read(path):
            missing.append(label)
    if missing:
        warns.append("Harness map link missing in: " + ", ".join(missing))
    return warns


def check_doc_lengths() -> list[str]:
    warns: list[str] = []
    dev = REPO_ROOT / "docs" / "dev"
    if not dev.is_dir():
        return warns
    for p in dev.glob("*.md"):
        n = len(_read(p).splitlines())
        if n > 220:
            warns.append(f"docs/dev doc long ({n} lines): {p.name}")
    return warns


def _parse_hook_command(cmd: str) -> str | None:
    cmd = cmd.strip()
    if cmd.startswith("node "):
        return cmd[5:].strip()
    return None


def check_hooks_json() -> list[str]:
    """WARN: invalid hooks.json or missing hook script targets."""
    warns: list[str] = []
    p = REPO_ROOT / ".cursor" / "hooks.json"
    if not p.is_file():
        warns.append(".cursor/hooks.json missing (hooks optional)")
        return warns
    try:
        data = json.loads(_read(p))
    except json.JSONDecodeError as e:
        warns.append(f".cursor/hooks.json invalid JSON: {e}")
        return warns
    hooks = data.get("hooks") or {}
    for event, arr in hooks.items():
        if not isinstance(arr, list):
            continue
        for h in arr:
            if not isinstance(h, dict):
                continue
            c = h.get("command")
            if not isinstance(c, str):
                continue
            script = _parse_hook_command(c)
            if script and not (REPO_ROOT / script).is_file():
                warns.append(f"hook {event}: target missing: {script}")
    return warns


def check_mcp_json() -> list[str]:
    warns: list[str] = []
    p = REPO_ROOT / ".cursor" / "mcp.json"
    if not p.is_file():
        return warns
    try:
        data = json.loads(_read(p))
    except json.JSONDecodeError as e:
        warns.append(f".cursor/mcp.json invalid JSON: {e}")
        return warns
    servers = data.get("mcpServers") or {}
    g = servers.get("gmail-agent-repo-assistant")
    if not isinstance(g, dict):
        warns.append("mcp.json: gmail-agent-repo-assistant entry missing")
        return warns
    args = g.get("args")
    if not isinstance(args, list) or not args:
        warns.append("mcp.json: gmail-agent-repo-assistant args missing")
        return warns
    arg0 = args[0]
    if isinstance(arg0, str) and "${workspaceFolder}" in arg0:
        rel = arg0.replace("${workspaceFolder}/", "").replace("${workspaceFolder}\\", "")
        if not (REPO_ROOT / rel).is_file():
            warns.append(f"mcp server script missing: {rel}")
    elif isinstance(arg0, str) and not arg0.startswith("${"):
        target = REPO_ROOT / arg0.replace("\\", "/")
        if not target.is_file():
            warns.append(f"mcp server script missing: {arg0}")
    js = REPO_ROOT / ".cursor" / "mcp" / "repo-assistant-server.js"
    if not js.is_file():
        warns.append(".cursor/mcp/repo-assistant-server.js missing")
    return warns


def check_stale_summary_refs() -> list[str]:
    """WARN active harness paths referencing removed summary-and-next-steps.md."""
    warns: list[str] = []
    needle = "summary-and-next-steps.md"
    scan_roots = [
        REPO_ROOT / ".cursor" / "rules",
        REPO_ROOT / ".cursor" / "commands",
        REPO_ROOT / "docs" / "dev",
    ]
    for root in scan_roots:
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.suffix not in (".md", ".mdc"):
                continue
            if needle in _read(f):
                warns.append(f"stale ref {needle} in {f.relative_to(REPO_ROOT).as_posix()}")
    return warns


def check_daszek_v2_product_hints_rules() -> list[str]:
    """WARN if .cursor/rules still assert Daszek V2 as product surface."""
    warns: list[str] = []
    rules = REPO_ROOT / ".cursor" / "rules"
    if not rules.is_dir():
        return warns
    for f in sorted(rules.glob("*.mdc")):
        t = _read(f)
        if re.search(r"Daszek\s+V2\s+PHP", t):
            warns.append(f"possible Daszek V2 product-truth wording in {f.name}")
    return warns


def check_rules_always_apply_count() -> list[str]:
    warns: list[str] = []
    rules = REPO_ROOT / ".cursor" / "rules"
    if not rules.is_dir():
        return warns
    n = 0
    for f in rules.glob("*.mdc"):
        if re.search(r"(?m)^alwaysApply:\s*true\s*$", _read(f)):
            n += 1
    if n > 5:
        warns.append(f".cursor/rules: {n} files with alwaysApply true (prefer minimal global noise; target ≤5)")
    return warns


def check_rule_frontmatter() -> list[str]:
    warns: list[str] = []
    rules = REPO_ROOT / ".cursor" / "rules"
    if not rules.is_dir():
        return warns
    for f in sorted(rules.glob("*.mdc")):
        t = _read(f)
        if not t.startswith("---"):
            warns.append(f"{f.name}: missing YAML frontmatter")
            continue
        if "description:" not in t.split("---", 2)[1]:
            warns.append(f"{f.name}: frontmatter missing description")
    return warns


def check_cursorignore() -> list[str]:
    warns: list[str] = []
    p = REPO_ROOT / ".cursorignore"
    if not p.is_file():
        warns.append(".cursorignore missing")
        return warns
    text = _read(p)
    for pat in (".gitnexus", "__pycache__", "gmail_audit/runs", "Daszek/uploads"):
        if pat not in text:
            warns.append(f".cursorignore: consider adding pattern containing `{pat}`")
    return warns


def check_operational_handoff() -> list[str]:
    info: list[str] = []
    if (REPO_ROOT / "OPERATIONAL_HANDOFF.md").is_file():
        info.append("OPERATIONAL_HANDOFF.md present")
    else:
        info.append("OPERATIONAL_HANDOFF.md missing")
    return info


def report_context_footprint() -> list[str]:
    """INFO lines: largest .cursor/rules and .codex/agents developer_instructions (token budget hints)."""
    infos: list[str] = []
    rules_dir = REPO_ROOT / ".cursor" / "rules"
    if rules_dir.is_dir():
        rule_rows: list[tuple[str, int]] = []
        for f in sorted(rules_dir.glob("*.mdc")):
            rule_rows.append((f.name, f.stat().st_size))
        rule_rows.sort(key=lambda x: x[1], reverse=True)
        for name, sz in rule_rows[:8]:
            infos.append(f"context footprint: .cursor/rules/{name} {sz} bytes")
    agents_dir = REPO_ROOT / ".codex" / "agents"
    if agents_dir.is_dir():
        rows: list[tuple[str, int]] = []
        total = 0
        for f in sorted(agents_dir.glob("*.toml")):
            text = _read(f)
            m = re.search(r'developer_instructions\s*=\s*"""(.*?)"""', text, re.DOTALL)
            n = len(m.group(1)) if m else 0
            total += n
            rows.append((f.name, n))
        rows.sort(key=lambda x: x[1], reverse=True)
        for name, n in rows[:8]:
            infos.append(f"context footprint: .codex/agents/{name} developer_instructions {n} chars")
        infos.append(f"context footprint: .codex/agents/*.toml developer_instructions total {total} chars")
    return infos


def check_generic_skills_manual_flag() -> list[str]:
    """WARN when large generic skills lack disable-model-invocation."""
    warns: list[str] = []
    generic = (
        "context-optimization",
        "memory-systems",
        "multi-agent-patterns",
        "tool-design",
        "improve-codebase-architecture",
        "request-refactor-plan",
        "ubiquitous-language",
        "courier-notification-skills",
    )
    root = REPO_ROOT / ".agents" / "skills"
    if not root.is_dir():
        return warns
    for name in generic:
        p = root / name / "SKILL.md"
        if not p.is_file():
            continue
        fm = _read(p).split("---", 2)
        if len(fm) < 3:
            continue
        block = fm[1]
        if not re.search(r"(?m)^disable-model-invocation:\s*true\s*$", block):
            warns.append(f".agents/skills/{name}/SKILL.md should set disable-model-invocation: true for manual-only")
    return warns


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent development harness audit")
    parser.add_argument("--json", action="store_true", help="Print JSON summary after text report")
    args = parser.parse_args()

    pass_n = warn_n = fail_n = info_n = 0
    findings: list[tuple[str, str]] = []

    def add(level: str, msg: str) -> None:
        nonlocal pass_n, warn_n, fail_n, info_n
        findings.append((level, msg))
        if level == "PASS":
            pass_n += 1
        elif level == "WARN":
            warn_n += 1
        elif level == "INFO":
            info_n += 1
        else:
            fail_n += 1

    _oks, fe = check_files_exist()
    for m in fe:
        add("FAIL", m)
    for rel in REQUIRED_DEV_DOCS:
        if (REPO_ROOT / rel).is_file():
            add("PASS", f"exists: {rel}")

    gov_fails = check_governance_hubs_exist()
    for m in gov_fails:
        add("FAIL", m)
    if not gov_fails:
        add("PASS", "governance hub files present")

    spine_fails = check_canonical_context()
    for m in spine_fails:
        add("FAIL", m)
    if not spine_fails:
        add("PASS", "canonical context files and routers present")

    always_fails = check_exactly_one_always_apply_rule()
    for m in always_fails:
        add("FAIL", m)
    if not always_fails:
        add("PASS", "exactly one Cursor alwaysApply router")

    mcp_allow_fails = check_mcp_server_allowlist()
    for m in mcp_allow_fails:
        add("FAIL", m)
    if not mcp_allow_fails:
        add("PASS", "mcp.json server ids match project allowlist")

    agents_fails = check_agents_harness_link()
    for m in agents_fails:
        add("FAIL", m)
    if not agents_fails:
        add("PASS", "AGENTS.md links to harness map")

    rules_fails = check_rules_governance()
    for m in rules_fails:
        add("FAIL", m)
    if not rules_fails:
        add("PASS", ".cursor/rules governance (harness / GitNexus / LSP)")

    for m in check_gitignore_gitnexus():
        add("FAIL", m)
    if not check_gitignore_gitnexus():
        add("PASS", ".gitignore covers .gitnexus")

    for m in check_export_gitnexus():
        add("FAIL", m)
    if not check_export_gitnexus():
        add("PASS", "export_hardening mentions .gitnexus")

    for m in check_forbidden_claims():
        add("FAIL", m)
    if not check_forbidden_claims():
        add("PASS", "no forbidden harness phrases in AGENTS/docs/dev")

    for m in check_docs_dev_headings():
        add("WARN", m)

    for m in check_registry_skill_dirs():
        add("WARN", m)

    for m in check_codex_agents_in_registry():
        add("WARN", m)

    codex_spine_fails = check_codex_agents_knowledge_spine()
    for m in codex_spine_fails:
        add("FAIL", m)
    if not codex_spine_fails:
        add("PASS", ".codex agents avoid removed memory-bank routing")

    for m in check_optional_harness_index_links():
        add("WARN", m)

    for m in check_doc_lengths():
        add("WARN", m)

    hook_w = check_hooks_json()
    for m in hook_w:
        add("WARN", m)
    if not hook_w:
        add("PASS", "hooks.json and hook script targets ok")

    mcp_w = check_mcp_json()
    for m in mcp_w:
        add("WARN", m)
    if not mcp_w:
        add("PASS", "mcp.json and repo-assistant-server.js look sane")

    stale_w = check_stale_summary_refs()
    for m in stale_w:
        add("WARN", m)
    if not stale_w:
        add("PASS", "no stale summary-and-next-steps refs in harness paths")

    dv2_w = check_daszek_v2_product_hints_rules()
    for m in dv2_w:
        add("WARN", m)
    if not dv2_w:
        add("PASS", "no Daszek V2 PHP product-truth pattern in rules")

    for m in check_rules_always_apply_count():
        add("WARN", m)

    for m in check_rule_frontmatter():
        add("WARN", m)

    for m in check_cursorignore():
        add("WARN", m)

    for m in check_generic_skills_manual_flag():
        add("WARN", m)

    for msg in check_operational_handoff():
        add("INFO", msg)

    for msg in report_context_footprint():
        add("INFO", msg)

    print("agent_harness_audit.py — summary")
    print(f"PASS: {pass_n}  INFO: {info_n}  WARN: {warn_n}  FAIL: {fail_n}")
    for level, msg in findings:
        print(f"[{level}] {msg}")

    verdict = "PASS" if fail_n == 0 else "FAIL"
    print(f"\nverdict: {verdict}")

    if args.json:
        print(
            json.dumps(
                {
                    "pass": pass_n,
                    "info": info_n,
                    "warn": warn_n,
                    "fail": fail_n,
                    "verdict": verdict,
                    "findings": findings,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    return 1 if fail_n else 0


if __name__ == "__main__":
    sys.exit(main())
