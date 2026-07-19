# Gmail Audit Runtime

Status: compact router for the Node B Python runtime under `tools/gmail_audit`. Detailed historical command prose from the old README is archived at `C:\Users\compg\Desktop\gmail-agent-offloaded-archive\`.

## Authority

- This directory is Node B runtime code.
- It does not prove live Gmail, Drive, Daszek, Groq, Postgres, or Gate status by existing locally.
- Runtime/proof truth is recorded in `../../docs/runbooks/LAST_PROVEN_STATE.md` and proof artifacts.
- For runtime/proof work, use `../../docs/runbooks/LAST_PROVEN_STATE.md` and targeted tests.

## Main Entry Point

Use:

```powershell
python tools/gmail_audit/gmail_intake.py --help
```

Common command families:

- `doctor` - config/runtime readiness checks.
- `message`, `batch`, `period` - bounded Gmail analysis/intake.
- `signal-run`, `signal-worker`, `signal-replay`, `signal-rebuild-case` - signal runtime.
- `memory-backfill`, `case-context` - mailbox memory and case context.
- `drive-ingest`, `drive-case-context`, `drive-graph-rebuild` - bounded Drive/document intelligence.
- `operator-feedback`, `daszek-bridge-drain` - operator feedback/adjudication bridge.
- `action-proposal-*` - supervised proposal workflows.
- `maintain-desk` and operational feed exporters - Daszek projection support.

Always inspect current CLI help before using flags in a proof.

## Environment (`.env`)

`load_settings()` (`config.py`) loads the **first existing** file (no override of already-set keys):

1. `GMAIL_AGENT_ENV_FILE` if set
2. `tools/gmail_audit/.env` (optional)
3. **Repository root `.env`** (typical dev — Gmail, Groq, Daszek, Postgres URL)

`.env.local` is never loaded. `.env.vps` is for Docker Compose on VPS, not loaded by default. Details: `../../docs/dev/ENV_LOADING.md`.

## Context Projection API (read-only)

FastAPI app: `api_app.py` — context trays + Skrzat ask. Fixture smoke: `python tools/scripts/context_projection_smoke.py`.

## Local Baseline

From repo root (requires root `.env` or paths above):

```powershell
python -m compileall tools/gmail_audit scripts -q
python -m pytest tools/gmail_audit/tests -q
python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose
```

`doctor` may report `failed` for Postgres/Neo4j when local stacks on `:54129` / `:7687` are down — `checks.config` should still be `ok`.

For Daszek UI JS changes:

```powershell
node --check ../daszek/public/app.js
```

## Runtime / VPS Rule

Local success is implementation readiness, not live proof. For Node B/VPS:

1. Back up/sync only the scoped runtime files.
2. Rebuild/recreate the worker if code is image-baked.
3. Verify host/container hashes for changed files.
4. Run compile/tests/doctor inside the worker container.
5. Store artifacts under a dated `runs/<run-id>/` directory.
6. Report local, VPS, Node A, and operator proof separately.

## Safety Boundaries

- No autonomous customer email send by default.
- No Calendar live write by default.
- No CRM write by default.
- No OfferDTO/HVAC generation in `gmail-agent`.
- No raw private customer content in docs or chat.
- Daszek remains projection-only.

## LLM Routing

Structured stages can use configured provider routing:

- primary/fallback via `LLM_PRIMARY_PROVIDER` and `LLM_FALLBACK_PROVIDERS`;
- structured Groq/Cerebras alternation is **on by default** when both API keys are configured (`LLM_STRUCTURED_PROVIDER_ALTERNATION=0` to disable).

This does not imply multi-provider HA for Gmail connector reads, Google APIs, Drive/Calendar, Daszek bridge, policy gates, outbound actions, embeddings, or the whole app.

## Documentation

- Gate/proof truth: `../../docs/runbooks/LAST_PROVEN_STATE.md`.
- Deep manual: `../../docs/core/PROJECT_README.md`.
