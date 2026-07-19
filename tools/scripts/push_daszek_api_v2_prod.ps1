# Upload Daszek/includes/api-v2.php to production WordPress (Node A).
# Requires SSH to Hostido: DASZEK_DEPLOY_SSH=user@host (optional DASZEK_DEPLOY_SSH_KEY).
param(
  [string]$SshTarget = $env:DASZEK_DEPLOY_SSH,
  [string]$SshKey = $env:DASZEK_DEPLOY_SSH_KEY,
  [string]$RemotePluginDir = $env:DASZEK_WP_PLUGIN_DIR
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$localFile = Join-Path $repoRoot "Daszek\includes\api-v2.php"
if (-not (Test-Path $localFile)) { throw "Missing $localFile" }

if (-not $SshTarget) {
  Write-Host @"
DASZEK_DEPLOY_SSH not set. Manual upload required:
  Local:  $localFile
  Remote: wp-content/plugins/Daszek/includes/api-v2.php
  Host:   topinstal.com.pl (Hostido) — SSH port 22 refused from agent network; use Hostido panel / SFTP.

After upload, from Node B:
  bash /opt/gmail-agent/current/deploy/p2-nodeb-proof-bundle.sh
"@
  exit 2
}

if (-not $RemotePluginDir) {
  $RemotePluginDir = "/var/www/html/topinstal.com.pl/wp-content/plugins/Daszek"
}

$sshArgs = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
if ($SshKey) { $sshArgs = @("-i", $SshKey) + $sshArgs }

$remotePath = "$RemotePluginDir/includes/api-v2.php"
Write-Host "==> scp api-v2.php -> ${SshTarget}:${remotePath}"
& scp @sshArgs $localFile "${SshTarget}:${remotePath}"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> verify remote file"
& ssh @sshArgs $SshTarget "test -f '$remotePath' && grep -q operational_feed_snapshot_write '$remotePath' && echo DASZEK_API_V2_OK"
exit $LASTEXITCODE
