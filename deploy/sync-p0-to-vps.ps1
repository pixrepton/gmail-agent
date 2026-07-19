# Sync P0 code to Node B VPS (excludes secrets / local .env)
param(
    [string]$VpsHost = "178.104.171.104",
    [string]$Key = "$env:USERPROFILE\.ssh\gmail-agent_vps_id_ed25519"
)
$ErrorActionPreference = "Stop"
$ga = Split-Path $PSScriptRoot -Parent
$orch = Join-Path (Split-Path $ga -Parent) "topinstal-cieplo-orchestrator"
$tmp = Join-Path $env:TEMP "topinstal-p0-sync"

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $tmp | Out-Null

Push-Location $ga
tar -czf "$tmp\gmail-agent-p0.tgz" --exclude="tools/gmail_audit/.env" --exclude="tools/gmail_audit/.env.*" --exclude="tools/gmail_audit/runs" --exclude="tools/gmail_audit/data" docker-compose.vps.yml docker deploy tools/gmail_audit
Pop-Location

Push-Location $orch
tar -czf "$tmp\orch-p0.tgz" src deploy pyproject.toml tests tools
Pop-Location

scp -i $Key -o BatchMode=yes "$tmp\gmail-agent-p0.tgz" "$tmp\orch-p0.tgz" "root@${VpsHost}:/tmp/"
$remote = @(
    'set -e',
    'cd /opt/gmail-agent/current && tar -xzf /tmp/gmail-agent-p0.tgz',
    'find /opt/gmail-agent/current/deploy -name "*.sh" -exec sed -i "s/\\r$//" {} +',
    'chmod +x /opt/gmail-agent/current/deploy/*.sh 2>/dev/null || true',
    'cd /opt/topinstal-cieplo-worker && tar -xzf /tmp/orch-p0.tgz',
    'rm -f /opt/topinstal-cieplo-worker/src/topinstal_cieplo_worker/integrations/email/email_personalizer.py',
    'rm -f /opt/topinstal-cieplo-worker/src/topinstal_cieplo_worker/integrations/cieplo/cieplo_llm_enrichment.py',
    'rm -rf /opt/topinstal-cieplo-worker/src/topinstal_cieplo_worker/integrations/central_llm',
    'rm -f /opt/topinstal-cieplo-worker/tests/test_email_personalizer.py',
    'if [ -f /opt/topinstal-cieplo-worker/pyproject.toml ]; then cd /opt/topinstal-cieplo-worker && sudo -u topinstal .venv/bin/pip install -q -e . || true; fi',
    'systemctl restart cieplo-worker-api.service 2>/dev/null || true',
    'echo SYNC_OK'
) -join '; '
ssh -i $Key -o BatchMode=yes "root@$VpsHost" $remote

Write-Host "Done. Run: ssh root@$VpsHost bash /opt/gmail-agent/current/deploy/p0-vps-rollout.sh"
