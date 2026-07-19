# Start Mailbox Memory Postgres via Docker Compose (repo root).
# Creates .env.mailbox-memory from example if missing, then fails closed until the placeholder password is replaced.

$ErrorActionPreference = "Stop"

# scripts/ -> repo root
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$EnvFile = Join-Path $Root ".env.mailbox-memory"
$Example = Join-Path $Root ".env.mailbox-memory.example"
if (-not (Test-Path $EnvFile)) {
    if (Test-Path $Example) {
        Copy-Item $Example $EnvFile
        Write-Host "Created $EnvFile from example - replace MAILBOX_MEMORY_POSTGRES_PASSWORD before first docker compose up." -ForegroundColor Yellow
    }
}

if (-not (Test-Path $EnvFile)) {
    throw "Missing $EnvFile and no example available."
}

$passwordLine = Get-Content $EnvFile | Where-Object { $_ -match '^MAILBOX_MEMORY_POSTGRES_PASSWORD=' } | Select-Object -First 1
$passwordValue = if ($passwordLine) { ($passwordLine -split '=', 2)[1].Trim() } else { "" }
if ([string]::IsNullOrWhiteSpace($passwordValue) -or $passwordValue -eq 'CHANGE_ME_LOCAL_ONLY') {
    throw "MAILBOX_MEMORY_POSTGRES_PASSWORD must be set to a non-placeholder value in $EnvFile before starting Docker."
}

$ComposeFile = Join-Path $Root "docker-compose.mailbox-memory.yml"
if (-not (Test-Path $ComposeFile)) {
    throw "Missing docker-compose.mailbox-memory.yml at $Root"
}

docker compose --env-file $EnvFile -f $ComposeFile up -d
docker compose --env-file $EnvFile -f $ComposeFile ps

Write-Host ""
Write-Host "Next: set MAILBOX_MEMORY_DATABASE_URL in tools/gmail_audit/.env, e.g." -ForegroundColor Cyan
Write-Host "  postgresql://USER:PASSWORD@127.0.0.1:PORT/mailbox_memory" -ForegroundColor Gray
Write-Host "Use the same USER, PASSWORD, PORT as in .env.mailbox-memory" -ForegroundColor Gray
