const chunks = [];

process.stdin.on("data", (chunk) => chunks.push(chunk));
process.stdin.on("end", () => {
  try {
    const payload = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    const status = payload.status;
    const loopCount = Number(payload.loop_count || 0);

    if (status === "completed" && loopCount === 0) {
      process.stdout.write(
        JSON.stringify({
          followup_message:
            "Before finishing: (1) Confirm the session followed the current canon: AGENTS.md -> docs/README.md -> docs/core/PROJECT_README.md; read docs/runbooks/LAST_PROVEN_STATE.md for proof claims. (2) If you changed code, run the minimal checks from AGENTS.md or `/verify-pack`. (3) Update only canonical workspace memory when stable context changed. (4) State clearly what was confirmed locally vs external vs historical.",
        }) + "\n",
      );
      return;
    }

    process.stdout.write("{}\n");
  } catch (error) {
    process.stdout.write("{}\n");
  }
});
