/**
 * beforeShellExecution — deterministic gating (no jq; Node only).
 * Deny clearly catastrophic patterns; "ask" on suspicious network-in-shell pipes.
 */
const chunks = [];

process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  let payload = {};
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    process.stdout.write(JSON.stringify({ permission: "allow" }) + "\n");
    return;
  }

  const cmd = String(payload.command || payload.shell_command || "").trim();
  const lower = cmd.toLowerCase();

  const deny = (userMessage) => {
    process.stdout.write(
      JSON.stringify({
        permission: "deny",
        user_message: userMessage,
        agent_message: userMessage,
      }) + "\n",
    );
  };

  const ask = (userMessage) => {
    process.stdout.write(
      JSON.stringify({
        permission: "ask",
        user_message: userMessage,
        agent_message: userMessage,
      }) + "\n",
    );
  };

  if (!cmd) {
    process.stdout.write(JSON.stringify({ permission: "allow" }) + "\n");
    return;
  }

  if (/\bformat-?volume\b/i.test(cmd) || /\bdiskpart\b/i.test(cmd)) {
    return deny("Blocked disk/volume destructive command.");
  }

  if (/\bdd\s+if=/i.test(cmd) || /\bmksfs\b/i.test(cmd) || /\bmkfs\b/i.test(cmd)) {
    return deny("Blocked disk image / mkfs style command.");
  }

  if (/\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}/.test(cmd)) {
    return deny("Blocked fork-bomb style pattern.");
  }

  if (/\brm\s+/.test(lower) && /-rf|-fr\b/.test(lower) && (/\s\/(\s|$)/.test(cmd) || /\\system32/i.test(cmd))) {
    return deny("Blocked rm -rf on system root / protected path pattern.");
  }

  if (/\bcurl\b[^|]*\|\s*(ba)?sh\b/i.test(cmd) || /\bwget\b[^|]*\|\s*(ba)?sh\b/i.test(cmd)) {
    return ask("Shell pipe from curl/wget into shell detected. Confirm you trust the source before running.");
  }

  if (/\bpowershell\b.*\b-enc(odedcommand)?\b/i.test(lower)) {
    return ask("Encoded PowerShell command — review before running.");
  }

  process.stdout.write(JSON.stringify({ permission: "allow" }) + "\n");
});
