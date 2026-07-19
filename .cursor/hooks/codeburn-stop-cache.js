/**
 * stop hook: refresh CodeBurn status with TTL cache.
 *
 * CodeBurn can scan local AI session stores and may take seconds. This hook is
 * fail-open and cached so token/cost observability stays default-on without
 * slowing every agent turn.
 */
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const TTL_MS = Number(process.env.CODEBURN_HOOK_TTL_MS || 60 * 60 * 1000);
const TIMEOUT_MS = Number(process.env.CODEBURN_HOOK_TIMEOUT_MS || 20 * 1000);

const chunks = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  let cachePath = "";
  if (process.env.CODEBURN_HOOK_DISABLED === "1") {
    process.stdout.write("{}\n");
    return;
  }

  let payload = {};
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    payload = {};
  }

  const roots = Array.isArray(payload.workspace_roots) ? payload.workspace_roots : [];
  const root = roots[0] ? path.resolve(roots[0]) : process.cwd();
  const cacheDir = path.join(root, ".agent-tooling-cache");
  cachePath = path.join(cacheDir, "codeburn-status.json");

  try {
    const existing = readJson(cachePath);
    if (process.env.CODEBURN_HOOK_FORCE !== "1" && existing && Date.now() - Number(existing.updated_at || 0) < TTL_MS) {
      process.stdout.write("{}\n");
      return;
    }

    const command = process.platform === "win32" ? "codeburn.cmd" : "codeburn";
    const result = spawnSync(command, ["status", "--format", "json"], {
      cwd: root,
      encoding: "utf8",
      timeout: TIMEOUT_MS,
      maxBuffer: 1024 * 1024,
      windowsHide: true,
      shell: process.platform === "win32",
    });

    if (result.error || result.status !== 0 || !result.stdout) {
      writeJson(cachePath, {
        updated_at: Date.now(),
        ok: false,
        error: result.error ? String(result.error.message || result.error) : `exit ${result.status}`,
      });
      process.stdout.write("{}\n");
      return;
    }

    const status = JSON.parse(firstJsonLine(result.stdout));
    writeJson(cachePath, {
      updated_at: Date.now(),
      ok: true,
      status,
      warnings: String(result.stderr || "").slice(0, 2000),
    });

    const today = status.today || {};
    const month = status.month || {};
    const msg = `CodeBurn active: today $${fmt(today.cost)} / ${today.calls || 0} calls; month $${fmt(month.cost)} / ${month.calls || 0} calls. Keep responses compact.`;
    process.stdout.write(JSON.stringify({ followup_message: msg }) + "\n");
  } catch (error) {
    if (cachePath) {
      try {
        writeJson(cachePath, {
          updated_at: Date.now(),
          ok: false,
          error: String(error && error.message ? error.message : error),
        });
      } catch {
        // Ignore cache write failures; hook must fail open.
      }
    }
    process.stdout.write("{}\n");
  }
});

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

function writeJson(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(data, null, 2), "utf8");
}

function firstJsonLine(text) {
  const line = String(text)
    .split(/\r?\n/)
    .find((x) => x.trim().startsWith("{"));
  if (!line) throw new Error("No JSON line in codeburn output");
  return line;
}

function fmt(value) {
  const n = Number(value || 0);
  return n.toFixed(2);
}
