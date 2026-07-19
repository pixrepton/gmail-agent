# Trigger operational feed push on VPS (Node B -> production WordPress).
param(
    [string]$VpsHost = "178.104.171.104",
    [string]$Key = "$env:USERPROFILE\.ssh\gmail-agent_vps_id_ed25519",
    [int]$CaseLimit = 50,
    # B1: local path to desk_notes.json (copied to VPS /tmp/desk_notes.json before push).
    [string]$DeskNotesJson = ""
)
$ErrorActionPreference = "Stop"
$deskEnv = ""
if ($DeskNotesJson -ne "") {
    if (-not (Test-Path -LiteralPath $DeskNotesJson)) {
        throw "DeskNotesJson not found: $DeskNotesJson"
    }
    scp -i $Key -o BatchMode=yes $DeskNotesJson "root@${VpsHost}:/tmp/desk_notes.json"
    $deskEnv = "DESK_NOTES_JSON=/tmp/desk_notes.json "
}
$remote = "${deskEnv}cd /opt/gmail-agent/current && sed -i 's/\r$//' deploy/push_daszek_operational_feed_prod.sh && chmod +x deploy/push_daszek_operational_feed_prod.sh && CASE_LIMIT=$CaseLimit bash deploy/push_daszek_operational_feed_prod.sh"
ssh -i $Key -o BatchMode=yes "root@${VpsHost}" $remote
