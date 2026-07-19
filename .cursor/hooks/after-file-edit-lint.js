/**
 * afterFileEdit — optional fast syntax checks for PHP / Daszek JS.
 * Fails open if php/node missing. Logs nothing to model unless hook host forwards stderr.
 */
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const chunks = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  let payload = {};
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    process.stdout.write("{}\n");
    return;
  }

  const filePath = String(payload.file_path || payload.path || "").trim();
  const roots = Array.isArray(payload.workspace_roots) ? payload.workspace_roots : [];
  const root = roots[0] ? path.resolve(roots[0]) : process.cwd();

  if (!filePath) {
    process.stdout.write("{}\n");
    return;
  }

  const abs = path.resolve(filePath);
  const rootResolved = path.resolve(root);
  const absLow = abs.toLowerCase();
  const rootLow = rootResolved.toLowerCase();
  if (absLow !== rootLow && !absLow.startsWith(rootLow + path.sep)) {
    process.stdout.write("{}\n");
    return;
  }

  const norm = filePath.replace(/\\/g, "/");
  let extra = "";

  const relNorm = path.relative(root, abs).replace(/\\/g, "/");
  const touchesAgentGovernance =
    relNorm === "AGENTS.md" ||
    relNorm === "justfile" ||
    relNorm.startsWith("memory-bank/") ||
    relNorm.startsWith(".cursor/rules/") ||
    relNorm.startsWith(".cursor/hooks/") ||
    relNorm.startsWith(".codex/") ||
    relNorm === "tools/scripts/agent_context_preflight.py" ||
    relNorm === "tools/scripts/agent_harness_audit.py";

  if (touchesAgentGovernance) {
    const r = spawnSync("python", ["tools/scripts/agent_context_preflight.py"], {
      cwd: root,
      encoding: "utf8",
      timeout: 20000,
      maxBuffer: 2 * 1024 * 1024,
    });
    if (!r.error && r.status !== 0) {
      extra = `Knowledge Spine preflight failed after editing ${relNorm}:\n${(r.stdout || r.stderr || "").slice(0, 6000)}`;
    }
  }

  if (filePath.endsWith(".php") && (norm.includes("/wp-adapter/") || norm.includes("/Daszek/"))) {
    const r = spawnSync("php", ["-l", abs], { encoding: "utf8", timeout: 20000, maxBuffer: 2 * 1024 * 1024 });
    if (r.error) {
      process.stdout.write("{}\n");
      return;
    }
    if (r.status !== 0) {
      extra = `php -l failed (${path.relative(root, abs)}):\n${(r.stderr || r.stdout || "").slice(0, 6000)}`;
    }
  }

  if (filePath.endsWith(".js") && norm.includes("/Daszek/public/")) {
    const r = spawnSync("node", ["--check", abs], { encoding: "utf8", timeout: 20000, maxBuffer: 2 * 1024 * 1024 });
    if (r.error) {
      process.stdout.write("{}\n");
      return;
    }
    if (r.status !== 0) {
      extra = `node --check failed (${path.relative(root, abs)}):\n${(r.stderr || r.stdout || "").slice(0, 6000)}`;
    }
  }

  if (extra) {
    process.stdout.write(JSON.stringify({ additional_context: extra }) + "\n");
    return;
  }

  process.stdout.write("{}\n");
});
