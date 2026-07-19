# Stop Mailbox Memory Postgres (repo root).

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$EnvFile = Join-Path $Root ".env.mailbox-memory"
$ComposeFile = Join-Path $Root "docker-compose.mailbox-memory.yml"

if (Test-Path $EnvFile) {
    docker compose --env-file $EnvFile -f $ComposeFile down
}
else {
    docker compose -f $ComposeFile down
}
