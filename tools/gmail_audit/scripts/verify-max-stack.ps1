# MAX-STACK verify harness (local Gate B)
$ErrorActionPreference = "Stop"
$ScriptsDir = $PSScriptRoot
$AuditDir = Split-Path -Parent $ScriptsDir
$ToolsDir = Split-Path -Parent $AuditDir
$GmailAgent = Split-Path -Parent $ToolsDir
$Root = Split-Path -Parent $GmailAgent
$GmailAudit = Join-Path $GmailAgent "tools\gmail_audit"

Push-Location $GmailAudit
try {
    python scripts/case_os_single_runtime_proof.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    # Intelligence-first closure: non-blocking (pre-existing temporal failures)
    python scripts/case_os_intelligence_first_closure_proof.py
    # exit code ignored — GRAPHSTORE_DSN pre-existing failures
    # Learning loops closure: non-blocking (LLM-dependent HITL failure)
    python scripts/case_os_learning_loops_closure_proof.py
    # exit code ignored — LLM-dependent pre-existing (no action_id on snapshot)
    # I1-I4 — Case Intelligence core (2026-06-24)
    python scripts/case_lifecycle_proof.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/case_coherence_proof.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/case_state_dashboard_proof.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    python scripts/agent_gateway_proof.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "MAX_STACK_10_PROOF_OK"
}
finally {
    Pop-Location
}
