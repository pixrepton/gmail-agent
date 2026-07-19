param(
  [switch]$Api,
  [switch]$Worker,
  [switch]$Setup
)

$ErrorActionPreference = "Stop"

function Ensure-Copied([string]$src, [string]$dst) {
  if (-not (Test-Path $dst)) {
    Copy-Item $src $dst
    Write-Host "Created $dst (edit it, do not commit secrets)." -ForegroundColor Yellow
  }
}

Push-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
Pop-Location

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repoRoot

Ensure-Copied ".\.env.vps.example" ".\.env.vps"
Ensure-Copied ".\.env.local-vps.example" ".\.env.local-vps"
Ensure-Copied ".\tools\gmail_audit\.env.example" ".\tools\gmail_audit\.env"

docker version | Out-Null
docker compose version | Out-Null

Write-Host ""
Write-Host "Starting local VPS-like services..." -ForegroundColor Cyan
docker compose --env-file .env.vps -f docker-compose.local-vps.yml up -d mailbox-memory-db neo4j ollama

if ($Setup) {
  Write-Host "Pulling Ollama embedding model..." -ForegroundColor Cyan
  docker compose --env-file .env.vps -f docker-compose.local-vps.yml --profile setup run --rm ollama-pull
}

if ($Api) {
  Write-Host "Starting Node B API (local) on 127.0.0.1:8765..." -ForegroundColor Cyan
  docker compose --env-file .env.vps -f docker-compose.local-vps.yml --profile api up -d gmail-agent-nodeb-api
}

if ($Worker) {
  Write-Host "Starting gmail-agent worker (local)..." -ForegroundColor Cyan
  docker compose --env-file .env.vps -f docker-compose.local-vps.yml --profile worker up -d --build gmail-agent-worker
}

Write-Host ""
Write-Host "Next checks:" -ForegroundColor Green
Write-Host "  - API health (if -Api):  curl.exe http://127.0.0.1:8765/health"
Write-Host "  - Postgres port:         127.0.0.1:54329"
Write-Host "  - Ollama:                http://127.0.0.1:11434"
