# gmail-agent — root thin command surface (Stage 3)

# Aliases only: no secrets, no broad shell passthrough, no deploy/restart/migrate.

# Requires: `python` on PATH; Docker recipes need `.env.vps` + Docker (see messages below).

# Discover: `just --list` or `just` (default). Recipe command lines must be indented (Just 1.x).

set dotenv-load := false

# Windows: use PowerShell so recipes never require POSIX `sh` (fixes "could not find the shell sh").

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

# Interpreter: override with `just py=python3 …` if needed.

py := "python"

# =============================================================================

# Local validation (safe offline / repo-only)

# =============================================================================

# Default: show available recipes (same as `just --list`).

default:
@just --list

list:
@just --list

# Docs link hygiene (repo-relative paths).

audit-context:
{{py}} support/context_pack_audit.py --root .

audit-harness:
{{py}} tools/scripts/agent_harness_audit.py

docs-audit:
{{py}} tools/docs_audit.py --root docs --out docs/\_audit
{{py}} tools/scripts/agent_harness_audit.py
{{py}} support/context_pack_audit.py --root .

agent-start:
{{py}} tools/scripts/agent_context_preflight.py

agent-context:
{{py}} tools/scripts/agent_context_preflight.py

compile:
{{py}} -m compileall tools/gmail_audit scripts -q

test:
{{py}} -m pytest tools/gmail_audit/tests -q

validate: agent-start audit-context audit-harness compile test

# Matches docs/core/PACKAGE_VALIDATION.md recommended local gates.

package-verify: audit-context compile test
php -l wp-adapter/mail-ingress/WorkflowConfig.php
node --check ../daszek/public/app.js

# =============================================================================

# Environment-dependent doctor (may call remote APIs; uses tools/gmail_audit/.env)

# =============================================================================

doctor-skip-gmail:
{{py}} -c "print('[doctor-skip-gmail] Uses local .env; may call Postgres/Neo4j/Ollama if configured.')"
{{py}} tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose

doctor-drive:
{{py}} -c "print('[doctor-drive] WARN: touches Google Drive API readiness if OAuth configured.')"
{{py}} tools/gmail_audit/gmail_intake.py doctor --skip-gmail --check-drive --verbose

doctor-daszek:
{{py}} -c "print('[doctor-daszek] WARN: touches Daszek live-push login/listing if Daszek env configured.')"
{{py}} tools/gmail_audit/gmail_intake.py doctor --skip-gmail --check-daszek --verbose

# Full operational doctor (includes Gmail mailbox check unless you change flags below).

doctor-live:
{{py}} -c "print('[doctor-live] WARN: environment-dependent; may invoke Gmail/Drive/Daszek per .env and flags.')"
{{py}} tools/gmail_audit/gmail_intake.py doctor --verbose

# =============================================================================

# Docker — canonical compose: docker-compose.vps.yml (requires .env.vps)

# Inspect only: ps / logs. No up/down/build here.

# =============================================================================

docker-ps:
{{py}} -c "import pathlib,sys; p=pathlib.Path('.env.vps'); (not p.is_file()) and (print('ERROR: missing .env.vps (copy from .env.vps.example). See docker-compose.vps.yml.', file=sys.stderr) or sys.exit(1))"
docker compose --env-file .env.vps -f docker-compose.vps.yml ps

# `just docker-logs gmail-agent-worker 200`

docker-logs service lines="200":
{{py}} -c "import pathlib,sys; p=pathlib.Path('.env.vps'); (not p.is_file()) and (print('ERROR: missing .env.vps', file=sys.stderr) or sys.exit(1))"
docker compose --env-file .env.vps -f docker-compose.vps.yml logs --tail {{lines}} {{service}}

# =============================================================================

# Bounded runtime inspection (still can touch configured backends — not a mock)

# =============================================================================

bounded-signal-worker:
{{py}} -c "print('[bounded-signal-worker] Bounded: --max-iterations 1 --dry-run (no explicit --push-daszek).')"
{{py}} tools/gmail_audit/gmail_intake.py signal-worker --max-iterations 1 --dry-run --verbose

# =============================================================================

# Proof / context helpers (read-focused; may require DB for case commands)

# =============================================================================

proof-status:
{{py}} -c "print('Proof / state — inspect manually (no network I/O from this recipe):'); print(' docs/runbooks/LAST_PROVEN_STATE.md'); print(' docs/proof-packs/\*/README.md')"

# `just case-context <case_id>`

case-context case_id:
{{py}} tools/gmail_audit/gmail_intake.py case-context --case-id {{case_id}}

# `just drive-case-context <case_id>`

drive-case-context case_id:
{{py}} tools/gmail_audit/gmail_intake.py drive-case-context --case-id {{case_id}}

# vNext contract (additive JSON); legacy `case-context` / `drive-case-context` unchanged.

case-context-vnext case_id:
{{py}} tools/gmail_audit/gmail_intake.py case-context --case-id {{case_id}} --vnext

drive-case-context-vnext case_id:
{{py}} tools/gmail_audit/gmail_intake.py drive-case-context --case-id {{case_id}} --vnext

# `just signal-replay <signal_id>`

signal-replay signal_id:
{{py}} -c "print('[signal-replay] WARN: may mutate mailbox-memory / journal depending on env; operator-scoped.')"
{{py}} tools/gmail_audit/gmail_intake.py signal-replay --signal-id {{signal_id}}

# `just signal-rebuild-case <case_id>`

signal-rebuild-case case_id:
{{py}} -c "print('[signal-rebuild-case] WARN: rebuilds case lineage from journal; operator-scoped.')"
{{py}} tools/gmail_audit/gmail_intake.py signal-rebuild-case --case-id {{case_id}}
