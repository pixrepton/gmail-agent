const fs = require("fs");
const path = require("path");

const workspaceRoot = path.resolve(__dirname, "..", "..");

function full(relativePath) {
  return path.join(workspaceRoot, relativePath);
}

function exists(relativePath) {
  return fs.existsSync(full(relativePath));
}

function readRelative(relativePath) {
  if (!relativePath) return "No path provided.";
  const filePath = full(relativePath);
  if (!fs.existsSync(filePath)) {
    return `External or missing in this context pack: ${relativePath}`;
  }
  return fs.readFileSync(filePath, "utf8");
}

const MB_DEFAULT_MAX = 220;

function truncateLines(text, maxLines) {
  if (typeof maxLines !== "number" || maxLines <= 0) return text;
  const lines = text.split("\n");
  if (lines.length <= maxLines) return text;
  const omitted = lines.length - maxLines;
  return `${lines.slice(0, maxLines).join("\n")}\n\n[truncated: ${omitted} more lines]\n`;
}

function readBounded(relativePath, maxLines) {
  const raw = readRelative(relativePath);
  if (raw.startsWith("External or missing")) return raw;
  return truncateLines(raw, maxLines);
}

function extractGateBSection(markdown) {
  const lines = markdown.split("\n");
  const lower = lines.map((l) => l.toLowerCase());
  let start = -1;
  for (let i = 0; i < lower.length; i++) {
    if (lower[i].includes("gate b") && (lower[i].includes("#") || lower[i].includes("bram"))) {
      start = i;
      break;
    }
  }
  if (start < 0) {
    for (let i = 0; i < lower.length; i++) {
      if (lower[i].includes("## gate") && lower[i].includes("b")) {
        start = i;
        break;
      }
    }
  }
  if (start < 0) return "No Gate B section heading found; read full file via gate_b_operator_checklist or docs/runbooks/RELEASE_GATE_V2_1.md.";
  const slice = lines.slice(start, start + 200).join("\n");
  return truncateLines(slice, 200);
}

function summarizeDoctorJson(text, sourcePath) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return `Invalid JSON at ${sourcePath}: ${e.message}`;
  }
  const pick = (obj, keys) => {
    const out = {};
    for (const k of keys) {
      if (obj && Object.prototype.hasOwnProperty.call(obj, k)) out[k] = obj[k];
    }
    return out;
  };
  if (data && typeof data === "object" && !Array.isArray(data)) {
    const shallow = pick(data, [
      "status",
      "ok",
      "error",
      "errors",
      "summary",
      "result",
      "exit_code",
      "daszek",
      "gmail",
      "drive",
      "postgres",
      "pgvector",
    ]);
    const keys = Object.keys(data);
    return JSON.stringify(
      {
        source: sourcePath,
        shallow,
        topLevelKeys: keys.slice(0, 40),
        keyCount: keys.length,
      },
      null,
      2,
    );
  }
  return JSON.stringify({ source: sourcePath, note: "non-object JSON root", preview: String(text).slice(0, 2000) }, null, 2);
}

function firstExisting(paths) {
  for (const p of paths) {
    if (exists(p)) return p;
  }
  return null;
}

function safeRelativePath(rel) {
  if (!rel || typeof rel !== "string") return null;
  const trimmed = rel.replace(/^\//, "");
  const norm = path.normalize(trimmed);
  if (norm.includes("..")) return null;
  const abs = path.resolve(workspaceRoot, norm);
  const root = path.resolve(workspaceRoot);
  const absLow = abs.toLowerCase();
  const rootLow = root.toLowerCase();
  if (absLow !== rootLow && !absLow.startsWith(rootLow + path.sep)) return null;
  return norm.split(path.sep).join("/");
}

const MEMORY_BANK_SLICES = {
  "agent-brief": "memory-bank/agent-brief.md",
  "current-state": "memory-bank/current-state.md",
  "active-context": "memory-bank/active-context.md",
  "project-brief": "memory-bank/project-brief.md",
  decisions: "memory-bank/decisions.md",
};

function walkMarkdownFiles(dirRel, exts, out) {
  const abs = full(dirRel);
  if (!fs.existsSync(abs)) return;
  const st = fs.statSync(abs);
  if (!st.isDirectory()) return;
  for (const name of fs.readdirSync(abs)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const rel = path.posix.join(dirRel.split("\\").join("/"), name);
    const ap = full(rel);
    const s = fs.statSync(ap);
    if (s.isDirectory()) {
      walkMarkdownFiles(rel, exts, out);
    } else if (exts.some((e) => name.endsWith(e))) {
      out.push(rel.split("\\").join("/"));
    }
  }
}

function harnessScanStaleRefs() {
  const stale = [];
  const roots = [".cursor/rules", ".cursor/commands", "docs/dev"];
  const files = [];
  for (const r of roots) walkMarkdownFiles(r, [".mdc", ".md"], files);
  for (const rel of files) {
    let text;
    try {
      text = fs.readFileSync(full(rel), "utf8");
    } catch {
      continue;
    }
    if (text.includes("summary-and-next-steps.md")) stale.push(rel);
  }
  return stale;
}

function harnessScanDaszekV2ProductHints() {
  /** Flags active instructions that still assert Daszek V2 as the product surface (not historical narrative). */
  const hits = [];
  const files = [];
  walkMarkdownFiles(".cursor/rules", [".mdc"], files);
  for (const rel of files) {
    const text = fs.readFileSync(full(rel), "utf8");
    if (/Daszek\s+V2\s+PHP/i.test(text)) hits.push(rel);
  }
  return hits;
}

function countRulesAlwaysApply() {
  const dir = path.join(workspaceRoot, ".cursor", "rules");
  if (!fs.existsSync(dir)) return { ruleFiles: 0, alwaysApplyTrue: 0, files: [] };
  const files = fs.readdirSync(dir).filter((f) => f.endsWith(".mdc"));
  const rows = [];
  let alwaysApplyTrue = 0;
  for (const f of files) {
    const text = fs.readFileSync(path.join(dir, f), "utf8");
    const on = /alwaysApply:\s*true\b/i.test(text);
    if (on) alwaysApplyTrue++;
    rows.push(`${f}: alwaysApply=${on}`);
  }
  return { ruleFiles: files.length, alwaysApplyTrue, files: rows };
}

function listSkillDirs(rootRel) {
  const root = full(rootRel);
  if (!fs.existsSync(root)) return [];
  return fs
    .readdirSync(root, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
}

function skillManualOnly(relSkillMd) {
  if (!exists(relSkillMd)) return false;
  const text = fs.readFileSync(full(relSkillMd), "utf8");
  const fm = text.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!fm) return false;
  return /disable-model-invocation:\s*true\b/i.test(fm[1]);
}

function readHooksStatus() {
  const rel = ".cursor/hooks.json";
  if (!exists(rel)) return { detail: ".cursor/hooks.json missing (hooks optional)." };
  let data;
  try {
    data = JSON.parse(fs.readFileSync(full(rel), "utf8"));
  } catch (e) {
    return { detail: `invalid hooks JSON: ${e.message}` };
  }
  const hooks = data.hooks || {};
  const lines = [];
  for (const [event, arr] of Object.entries(hooks)) {
    if (!Array.isArray(arr)) continue;
    for (const h of arr) {
      const cmd = h && h.command ? String(h.command) : "";
      const script = cmd.replace(/^node\s+/, "").trim();
      const existsOk = script && fs.existsSync(full(script));
      lines.push(`${event}: ${cmd} -> ${existsOk ? "ok" : "missing target"}`);
    }
  }
  return { detail: lines.join("\n") || "(no hook commands)" };
}

function cursorignoreCoverageHints() {
  const rel = ".cursorignore";
  if (!exists(rel)) return "`.cursorignore` missing.";
  const text = fs.readFileSync(full(rel), "utf8");
  const want = [".gitnexus", "__pycache__", "gmail_audit/runs", "Daszek/uploads"];
  const missing = want.filter((w) => !text.includes(w));
  if (missing.length === 0) return "Key ignore patterns present (.gitnexus, __pycache__, gmail_audit runs, Daszek/uploads).";
  return `Missing suggested patterns: ${missing.join(", ")}`;
}

function mcpServerPathStatus() {
  const rel = ".cursor/mcp.json";
  if (!exists(rel)) return "`.cursor/mcp.json` missing.";
  try {
    const data = JSON.parse(fs.readFileSync(full(rel), "utf8"));
    const srv = data.mcpServers && data.mcpServers["gmail-agent-repo-assistant"];
    if (!srv || !Array.isArray(srv.args) || !srv.args[0]) return "gmail-agent-repo-assistant entry incomplete.";
    const arg0 = srv.args[0];
    const usesVar = arg0.includes("${workspaceFolder}");
    const target = usesVar ? path.join(workspaceRoot, ".cursor", "mcp", "repo-assistant-server.js") : full(arg0.replace(/^\.\//, ""));
    const ok = fs.existsSync(usesVar ? path.join(workspaceRoot, ".cursor", "mcp", "repo-assistant-server.js") : target);
    return `mcp args[0]=${arg0} | usesWorkspaceFolder=${usesVar} | serverJsExists=${ok}`;
  } catch (e) {
    return `mcp.json parse error: ${e.message}`;
  }
}

function buildOperationalHandoffResource() {
  const banner = [
    "# OPERATIONAL_HANDOFF.md — operational snapshot only",
    "",
    "This resource mirrors the repo root file `OPERATIONAL_HANDOFF.md`. It is a **handoff / last-known operational status** note.",
    "",
    "It is **not** the primary agent instruction (use `README.md` + `AGENTS.md`), **not** a proof-pack, **not** Gate B evidence, and **not** architectural authority.",
    "",
    "---",
    "",
  ].join("\n");
  if (!exists("OPERATIONAL_HANDOFF.md")) return banner + "`OPERATIONAL_HANDOFF.md` not found.\n";
  return banner + readBounded("OPERATIONAL_HANDOFF.md", 450);
}

function buildAgentHarnessStatus() {
  const rules = countRulesAlwaysApply();
  const stale = harnessScanStaleRefs();
  const daszekV2rules = harnessScanDaszekV2ProductHints();
  const agentsSkills = listSkillDirs(".agents/skills");
  const cursorSkills = listSkillDirs(".cursor/skills");
  const manualOnly = [];
  for (const name of agentsSkills) {
    const p = `.agents/skills/${name}/SKILL.md`;
    if (skillManualOnly(p)) manualOnly.push(name);
  }
  for (const name of cursorSkills) {
    const p = `.cursor/skills/${name}/SKILL.md`;
    if (skillManualOnly(p)) manualOnly.push(`cursor:${name}`);
  }
  const hookDetail = readHooksStatus().detail;
  const lines = [
    "# Agent harness status (dev tooling — not runtime truth)",
    "",
    "## Rules",
    `- Rule files (.mdc): ${rules.ruleFiles}`,
    `- alwaysApply: true → ${rules.alwaysApplyTrue}`,
    ...rules.files.map((x) => `  - ${x}`),
    "",
    "## Stale reference scan (harness paths)",
    stale.length ? stale.map((x) => `- ${x}`).join("\n") : "- (none) `summary-and-next-steps.md` in .cursor/rules, .cursor/commands, docs/dev",
    "",
    "## Daszek V2 product-truth hints (.cursor/rules)",
    daszekV2rules.length ? daszekV2rules.map((x) => `- ${x}`).join("\n") : "- (none) pattern `Daszek V2 PHP`",
    "",
    "## Skills (directories)",
    `- .agents/skills: ${agentsSkills.join(", ") || "(none)"}`,
    `- .cursor/skills: ${cursorSkills.join(", ") || "(none)"}`,
    "",
    "## Manual-only skills (disable-model-invocation)",
    manualOnly.length ? manualOnly.map((x) => `- ${x}`).join("\n") : "- (none detected)",
    "",
    "## Hooks",
    hookDetail,
    "",
    "## MCP",
    mcpServerPathStatus(),
    "",
    "## OPERATIONAL_HANDOFF.md",
    exists("OPERATIONAL_HANDOFF.md") ? "- present at repo root" : "- **missing**",
    "",
    "## .cursorignore",
    cursorignoreCoverageHints(),
    "",
    "## Local vs live (reminder)",
    "- `tools/gmail_audit/`, `scripts/`, `docker-compose*.yml`, `Daszek/` may exist locally; that does not prove live VPS/WordPress sync.",
  ];
  return lines.join("\n");
}

function localPaths() {
  return {
    authority: [
      "docs/core/CONSTITUTION_V2_1.md",
      "docs/core/ARCHITECTURE_PRECEDENCE.md",
      "docs/core/ARCHITECTURE_AUTHORITY_V2_1.json",
    ],
    scope: firstExisting([
      "docs/core/CONTEXT_PACK_SCOPE.md",
      "docs/core/PACKAGE_TRUTH_MAP.md",
    ]),
    readme: firstExisting(["README.md", "docs/README.md"]),
    currentState: firstExisting([
      "memory-bank/current-state.md",
      "memory-bank/project-brief.md",
    ]),
    activeContext: firstExisting(["memory-bank/active-context.md"]),
    decisions: firstExisting(["memory-bank/decisions.md"]),
    workflow: firstExisting([
      "docs/archive/runbooks/MAIL_INGRESS_OFFER_REVIEW_WORKFLOW.md",
      "docs/archive/runbooks/MAIL_INGRESS_OFFER_REVIEW_SMOKE_TEST.md",
    ]),
    validation: firstExisting(["docs/core/PACKAGE_VALIDATION.md"]),
  };
}

function listTools() {
  return [
    {
      name: "context_pack_status",
      description: "Summarize what exists locally in the context pack and what should be treated as external references.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "architecture_review_inputs",
      description: "Return the correct local authority and working-state inputs for an architecture review.",
      inputSchema: { type: "object", properties: { change: { type: "string" } } },
    },
    {
      name: "workflow_reference",
      description: "Return the best local workflow and validation references available in this pack.",
      inputSchema: { type: "object", properties: { area: { type: "string" } } },
    },
    {
      name: "memory_bank_slice",
      description:
        "Read bounded slices of memory-bank markdown (agent brief, current state, active context). Use when orienting the agent without loading full AGENTS.md or audit folders.",
      inputSchema: {
        type: "object",
        properties: {
          slices: {
            type: "array",
            items: {
              type: "string",
              enum: ["agent-brief", "current-state", "active-context", "project-brief", "decisions"],
            },
            description: "Which memory-bank files to include. Defaults to agent-brief + current-state.",
          },
          max_lines_per_file: { type: "integer", description: "Max lines per file (default 220)." },
        },
      },
    },
    {
      name: "gate_b_operator_checklist",
      description:
        "Return the Gate B operator checklist (A2) and pointers to RELEASE_GATE / live verification. Read-only; does not establish Gate B.",
      inputSchema: {
        type: "object",
        properties: {
          max_lines: { type: "integer", description: "Optional line cap (default 400)." },
        },
      },
    },
    {
      name: "release_gate_gate_b_excerpt",
      description:
        "Return an excerpt of docs/runbooks/RELEASE_GATE_V2_1.md around the Gate B section for release discipline.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "verify_local_baseline",
      description:
        "List the repo's canonical local verification commands from AGENTS.md (compileall, pytest, php -l, node --check, doctor variants). Does not execute them.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "doctor_json_summarize",
      description:
        "Summarize a doctor or proof JSON file already in the workspace (e.g. under tools/gmail_audit/runs/). Pass relative_path from repo root. Read-only.",
      inputSchema: {
        type: "object",
        properties: {
          relative_path: { type: "string", description: "Path under repo root to a JSON file." },
        },
        required: ["relative_path"],
      },
    },
    {
      name: "agent_harness_status",
      description:
        "Dev-only: counts Cursor rules and alwaysApply flags, lists skills and manual-only skills, scans harness docs for stale `summary-and-next-steps.md` refs, checks hooks targets, MCP server path, OPERATIONAL_HANDOFF presence, and .cursorignore coverage hints. Not runtime truth and not Gate B.",
      inputSchema: { type: "object", properties: {} },
    },
  ];
}

function listResources() {
  return [
    {
      uri: "gmail-agent://authority",
      name: "Architecture Authority",
      mimeType: "text/markdown",
      description: "Normative authority documents available locally.",
    },
    {
      uri: "gmail-agent://working-state",
      name: "Working State",
      mimeType: "text/markdown",
      description: "Current package-safe state and active context.",
    },
    {
      uri: "gmail-agent://context-pack-status",
      name: "Context Pack Status",
      mimeType: "text/markdown",
      description: "What is local here versus external to the package.",
    },
    {
      uri: "gmail-agent://gate-b-and-verify",
      name: "Gate B checklist + verify baseline",
      mimeType: "text/markdown",
      description: "Operator Gate B checklist excerpt plus local verification command list.",
    },
    {
      uri: "gmail-agent://operational-handoff",
      name: "Operational handoff",
      mimeType: "text/markdown",
      description:
        "OPERATIONAL_HANDOFF.md — last operational status / handoff only; not primary agent instructions, not a proof-pack, not Gate B evidence.",
    },
  ];
}

function buildAuthorityResource() {
  const docs = localPaths();
  return [
    "# Authority",
    ...docs.authority.map((p) => `## ${p}\n${readRelative(p)}`),
    docs.scope ? `## ${docs.scope}\n${readRelative(docs.scope)}` : "",
  ].filter(Boolean).join("\n\n");
}

function buildWorkingStateResource() {
  const docs = localPaths();
  return [
    "# Working State",
    docs.readme ? `## ${docs.readme}\n${readRelative(docs.readme)}` : "",
    docs.currentState ? `## ${docs.currentState}\n${readRelative(docs.currentState)}` : "",
    docs.activeContext ? `## ${docs.activeContext}\n${readRelative(docs.activeContext)}` : "",
    docs.decisions ? `## ${docs.decisions}\n${readRelative(docs.decisions)}` : "",
  ].filter(Boolean).join("\n\n");
}

function buildContextPackStatus() {
  const docs = localPaths();
  const localRuntimeHints = [
    "`tools/gmail_audit/` — local Python intake/doctor surface; exists in-tree does not prove live worker/VPS.",
    "`scripts/` — bootstrap/helpers; not automatic deployment.",
    "`docker-compose*.yml` — local or documented compose; not proof of current production topology.",
    "`Daszek/` — local Node A code/fixture slice; not production WordPress uploads.",
    "`wp-adapter/` — bridge seam review locally; production WordPress is external.",
  ];
  const externalHints = [
    "Live VPS Node B worker process and real Postgres beyond your machine",
    "Production WordPress / Daszek V3 operator host and uploads tree on Node A",
    "Sibling repos (`kalk-top`, etc.) unless checked out next to this workspace",
    "Secrets, OAuth tokens, production env values",
  ];
  return [
    "# Context Pack Status",
    "",
    "This workspace is a **working context pack**: it may include runnable code (`tools/gmail_audit/`, compose files, `Daszek/` sources). Presence locally **does not** prove live VPS/WordPress, Gate B, or automatic Node A/B sync.",
    "",
    "## Local anchors",
    `- Readme: ${docs.readme || "none"}`,
    `- Current state: ${docs.currentState || "none"}`,
    `- Active context: ${docs.activeContext || "none"}`,
    `- Workflow reference: ${docs.workflow || "none"}`,
    `- Validation reference: ${docs.validation || "none"}`,
    "- Historical proof bundles: not kept in-repo; operator archives externally",
    "",
    "## Local surfaces (may exist without proving production)",
    ...localRuntimeHints.map((x) => `- ${x}`),
    "",
    "## Treat as external / environment-dependent until proven",
    ...externalHints.map((x) => `- ${x}`),
  ].join("\n");
}

function buildVerifyLocalBaselineText() {
  return [
    "# Local verification baseline (from AGENTS.md — run in shell, not via MCP)",
    "",
    "- `python support/context_pack_audit.py --root .`",
    "- `python -m compileall tools/gmail_audit`",
    "- `python -m pytest tools/gmail_audit/tests -q`",
    "- `php -l` on changed PHP under `wp-adapter/` or `Daszek/`",
    "- `node --check Daszek/public/app.js` (when Daszek JS changed)",
    "",
    "After env is configured:",
    "- `python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose`",
    "- `python tools/gmail_audit/gmail_intake.py doctor --check-drive --verbose`",
    "- `python tools/gmail_audit/gmail_intake.py doctor --check-daszek --verbose`",
    "",
    "Canonical production profile: `GMAIL_AGENT_RUNTIME_PROFILE=canonical_production` (see docs/runbooks/CANONICAL_PRODUCTION_RUNTIME_AND_OPERATIONS.md).",
    "",
    "Do not claim Gate B or live Gmail success from this workspace alone; separate confirmed locally vs external.",
  ].join("\n");
}

function buildGateBAndVerifyResource() {
  const checklist = exists("docs/runbooks/GATE_B_OPERATOR_CHECKLIST_A2.md")
    ? readBounded("docs/runbooks/GATE_B_OPERATOR_CHECKLIST_A2.md", 120)
    : "Checklist file missing.";
  const gateB = exists("docs/runbooks/RELEASE_GATE_V2_1.md")
    ? extractGateBSection(readRelative("docs/runbooks/RELEASE_GATE_V2_1.md"))
    : "RELEASE_GATE_V2_1.md missing.";
  return ["# Gate B + verification (packaged excerpt)", "", "## Checklist (truncated)", checklist, "", "## Release gate — Gate B slice", gateB, "", buildVerifyLocalBaselineText()].join(
    "\n",
  );
}

function readResource(uri) {
  if (uri === "gmail-agent://authority") return buildAuthorityResource();
  if (uri === "gmail-agent://working-state") return buildWorkingStateResource();
  if (uri === "gmail-agent://context-pack-status") return buildContextPackStatus();
  if (uri === "gmail-agent://gate-b-and-verify") return buildGateBAndVerifyResource();
  if (uri === "gmail-agent://operational-handoff") return buildOperationalHandoffResource();
  return `Unknown resource: ${uri}`;
}

function callTool(name, args) {
  const docs = localPaths();
  const change = args && typeof args.change === "string" ? args.change : "unspecified change";
  const area = args && typeof args.area === "string" ? args.area : "general";

  if (name === "context_pack_status") {
    return buildContextPackStatus();
  }

  if (name === "architecture_review_inputs") {
    return [
      `Architecture review change: ${change}`,
      "",
      "Read first:",
      ...docs.authority.map((p) => `- ${p}`),
      docs.scope ? `- ${docs.scope}` : "- no local scope doc",
      docs.readme ? `- ${docs.readme}` : "- no local readme",
      docs.currentState ? `- ${docs.currentState}` : "- no current-state doc",
      docs.decisions ? `- ${docs.decisions}` : "- no decisions doc",
      "- historical proof JSON may exist under operator-local paths (e.g. tools/gmail_audit/runs/)",
      "",
      "Review output should separate:",
      "- confirmed locally",
      "- referenced externally",
      "- historical context",
      "- ownership / boundary risk",
      "- contract impact",
      "- verification scope",
    ].join("\n");
  }

  if (name === "workflow_reference") {
    return [
      `Workflow reference area: ${area}`,
      "",
      "Best local sources:",
      docs.workflow ? `- ${docs.workflow}` : "- no local workflow doc",
      docs.validation ? `- ${docs.validation}` : "- no local validation doc",
      docs.readme ? `- ${docs.readme}` : "- no local readme",
      "",
      "Rule:",
      "If a runbook points to absent runtime files, treat that step as an external workspace dependency.",
    ].join("\n");
  }

  if (name === "memory_bank_slice") {
    const maxLines =
      args && typeof args.max_lines_per_file === "number" && args.max_lines_per_file > 0
        ? args.max_lines_per_file
        : MB_DEFAULT_MAX;
    let keys = Array.isArray(args.slices) ? args.slices : ["agent-brief", "current-state"];
    keys = keys.filter((k) => MEMORY_BANK_SLICES[k]);
    if (keys.length === 0) keys = ["agent-brief", "current-state"];
    const parts = keys.map((k) => {
      const rel = MEMORY_BANK_SLICES[k];
      const body = readBounded(rel, maxLines);
      return `# ${k} (${rel})\n\n${body}`;
    });
    return parts.join("\n\n---\n\n");
  }

  if (name === "gate_b_operator_checklist") {
    const maxLines =
      args && typeof args.max_lines === "number" && args.max_lines > 0 ? args.max_lines : 400;
    if (!exists("docs/runbooks/GATE_B_OPERATOR_CHECKLIST_A2.md")) {
      return "docs/runbooks/GATE_B_OPERATOR_CHECKLIST_A2.md not found in this workspace.";
    }
    return readBounded("docs/runbooks/GATE_B_OPERATOR_CHECKLIST_A2.md", maxLines);
  }

  if (name === "release_gate_gate_b_excerpt") {
    if (!exists("docs/runbooks/RELEASE_GATE_V2_1.md")) {
      return "docs/runbooks/RELEASE_GATE_V2_1.md not found.";
    }
    return extractGateBSection(readRelative("docs/runbooks/RELEASE_GATE_V2_1.md"));
  }

  if (name === "verify_local_baseline") {
    return buildVerifyLocalBaselineText();
  }

  if (name === "doctor_json_summarize") {
    const rel = args && typeof args.relative_path === "string" ? safeRelativePath(args.relative_path) : null;
    if (!rel) return "Invalid or unsafe relative_path; provide a repo-relative JSON path without ..";
    if (!rel.toLowerCase().endsWith(".json")) return "Only .json files are supported for this summarizer.";
    if (!exists(rel)) return `Not found: ${rel}`;
    const text = readRelative(rel);
    return summarizeDoctorJson(text, rel);
  }

  if (name === "agent_harness_status") {
    return buildAgentHarnessStatus();
  }

  return `Unknown tool: ${name}`;
}

function getPrompt(name, args) {
  const changeArg =
    Array.isArray(args) && args.length > 0 && args[0] && args[0].value
      ? String(args[0].value)
      : "unspecified change";

  if (name === "architecture-review") {
    return {
      description: "Architecture review prompt for the gmail-agent context pack.",
      messages: [
        {
          role: "user",
          content: {
            type: "text",
            text:
              `Review this proposed change in the gmail-agent context pack: ${changeArg}\n\n` +
              "Use the authority docs, scope docs, current-state docs, and decisions docs that exist locally. " +
              "Separate confirmed local evidence from external repo references and from historical context. " +
              "Return ownership, boundary risks, contract risks, verification scope, and recommendation.",
          },
        },
      ],
    };
  }

  return null;
}

function listPrompts() {
  return [
    {
      name: "architecture-review",
      description: "Generate a context-pack-safe architecture review prompt.",
      arguments: [
        {
          name: "change",
          description: "Short description of the proposed change.",
          required: true,
        },
      ],
    },
  ];
}

function emit(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function logError(...args) {
  process.stderr.write(args.map(String).join(" ") + "\n");
}

function hasRequestId(request) {
  return request && Object.prototype.hasOwnProperty.call(request, "id");
}

function sendResult(id, result) {
  if (id === undefined) return;
  emit({ jsonrpc: "2.0", id, result });
}

function sendError(id, code, message, data) {
  if (id === undefined) return;
  const error = { code, message };
  if (data !== undefined) error.data = data;
  emit({ jsonrpc: "2.0", id, error });
}

async function handleRequest(request) {
  if (!request || typeof request !== "object") {
    return;
  }

  const method = request.method;
  const params = request.params || {};
  const id = request.id;

  // JSON-RPC notifications do not require responses.
  if (method === "notifications/initialized") {
    return;
  }

  if (method === "notifications/cancelled") {
    const reason = params && params.reason ? String(params.reason) : "no reason";
    logError(`[mcp] request cancelled: ${reason}`);
    return;
  }

  if (method && method.startsWith("notifications/")) {
    return;
  }

  if (method === "initialize") {
    return sendResult(id, {
      protocolVersion: params.protocolVersion || "2025-06-18",
      capabilities: {
        tools: {},
        resources: {},
        prompts: {},
      },
      serverInfo: {
        name: "gmail-agent-repo-assistant",
        version: "0.1.0",
      },
    });
  }

  if (method === "tools/list") {
    return sendResult(id, { tools: listTools() });
  }

  if (method === "resources/list") {
    return sendResult(id, { resources: listResources() });
  }

  if (method === "resources/read") {
    return sendResult(id, {
      contents: [
        {
          uri: params.uri,
          mimeType: "text/markdown",
          text: readResource(params.uri),
        },
      ],
    });
  }

  if (method === "tools/call") {
    return sendResult(id, {
      content: [
        {
          type: "text",
          text: callTool(params.name, params.arguments || {}),
        },
      ],
    });
  }

  if (method === "prompts/list") {
    return sendResult(id, { prompts: listPrompts() });
  }

  if (method === "prompts/get") {
    const prompt = getPrompt(params.name, params.arguments || []);
    if (!prompt) {
      return sendError(id, -32602, `Unknown prompt: ${params.name}`);
    }
    return sendResult(id, prompt);
  }

  return sendError(id, -32601, `Unsupported method: ${method}`);
}

let inputBuffer = "";

process.stdin.setEncoding("utf8");

process.stdin.on("data", (chunk) => {
  inputBuffer += chunk;

  const lines = inputBuffer.split(/\r?\n/);
  inputBuffer = lines.pop() || "";

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;

    let request;
    try {
      request = JSON.parse(trimmed);
    } catch (error) {
      logError(`[mcp] invalid JSON-RPC line: ${error.message}`);
      emit({
        jsonrpc: "2.0",
        id: null,
        error: {
          code: -32700,
          message: "Parse error",
        },
      });
      continue;
    }

    Promise.resolve(handleRequest(request)).catch((error) => {
      logError(`[mcp] request handler failed: ${error && error.stack ? error.stack : error}`);
      if (hasRequestId(request)) {
        sendError(request.id, -32603, "Internal error");
      }
    });
  }
});

process.stdin.on("end", () => {
  const trimmed = inputBuffer.trim();
  if (!trimmed) return;

  let request;
  try {
    request = JSON.parse(trimmed);
  } catch (error) {
    logError(`[mcp] invalid trailing JSON-RPC line: ${error.message}`);
    return;
  }

  Promise.resolve(handleRequest(request)).catch((error) => {
    logError(`[mcp] trailing request handler failed: ${error && error.stack ? error.stack : error}`);
    if (hasRequestId(request)) {
      sendError(request.id, -32603, "Internal error");
    }
  });
});

process.on("uncaughtException", (error) => {
  logError(`[mcp] uncaught exception: ${error && error.stack ? error.stack : error}`);
});

process.on("unhandledRejection", (error) => {
  logError(`[mcp] unhandled rejection: ${error && error.stack ? error.stack : error}`);
});
