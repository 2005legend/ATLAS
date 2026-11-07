# Wake both InstaBox VMs (VM 2 first, so VM 1's check of the bridge finds it awake), check every service's real health, and pull the latest backups to this laptop.
# Usage (PowerShell, from this folder):  .\wake-vms.ps1          (add -NoBackup to skip the backup copy)
# An SSH connection wakes a paused InstaBox VM; a pause keeps memory and processes, so services resume as they were.
param([switch]$NoBackup)

# --- edit these two lines for your VMs (see docs/03 and docs/04) ---
$vm1 = @('-i', "$env:USERPROFILE\.ssh\atlas_vm1_ed25519", '-p', '2222', 'VM1_SSH_TARGET')
$vm2 = @('-p', '2222', 'VM2_SSH_TARGET')
$sshOpts = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=60')
$backupDir = Join-Path $env:USERPROFILE 'hermes-backups'   # outside the project folder: archives contain keys

function Show($name, $ok, $detail) {
    $color = if ($ok) { 'Green' } else { 'Yellow' }
    Write-Host ("  {0,-22} {1}" -f $name, $detail) -ForegroundColor $color
}

Write-Host "VM 2 (DSH worker)"
$c2 = 'curl -s -m 8 http://127.0.0.1:8000/health'
$r2 = & ssh @sshOpts @vm2 $c2 2>$null
if ($LASTEXITCODE -ne 0 -or -not $r2) { Show 'reachable' $false 'NO (may be in its wake cooldown; try later)' }
else {
    try {
        $h = $r2 | ConvertFrom-Json
        Show 'delegation bridge' ($h.bridge -eq 'ok') $h.bridge
        Show 'freellmapi' ($h.freellmapi -eq 'ok') $h.freellmapi
        Show 'Ollama (offline model)' ($h.ollama -notlike 'unreachable*') $h.ollama
        Show 'DSH installed' ([bool]$h.dsh) $h.dsh
    } catch { Show 'bridge health' $false "unexpected reply: $r2" }
}

Write-Host "VM 1 (Hermes)"
# No double quotes inside these remote commands: Windows PowerShell 5.1 strips them when calling ssh.
$c1 = 'gw=$(systemctl --user is-active HERMES_GATEWAY_UNIT); tg=$(journalctl --user -u HERMES_GATEWAY_UNIT -b --no-pager | grep -c Connected.to.Telegram); br=$(curl -s -m 8 -o /dev/null -w %{http_code} http://VM2_TAILSCALE_IP:8000/health); echo $gw\|$tg\|$br'
$r1 = & ssh @sshOpts @vm1 $c1 2>$null
if ($LASTEXITCODE -ne 0 -or -not $r1) { Show 'reachable' $false 'NO (may be in its 5-20 min cooldown after the 8-hour cap; try later)' }
else {
    $gw, $tg, $br = $r1.Split('|')
    Show 'Hermes gateway' ($gw -eq 'active') $gw
    Show 'Telegram' ([int]$tg -gt 0) $(if ([int]$tg -gt 0) { 'connected' } else { 'not connected yet' })
    Show 'can reach VM 2 bridge' ($br -eq '200') "http $br"
}

Write-Host "Laptop"
try { $v = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:11434/api/version).Content; Show 'Ollama (fallback model)' $true $v }
catch { Show 'Ollama (fallback model)' $false 'not running (optional): start "Ollama" from the Start menu (it also autostarts at login)' }

if (-not $NoBackup) {
    Write-Host "Backups -> $backupDir"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $pulls = @(
        @{ Name = 'VM 1'; Ssh = $vm1; Dir = '/data/backups/critical'; Prefix = 'vm1-critical-'; Sudo = '' },
        @{ Name = 'VM 2'; Ssh = $vm2; Dir = '/data/backups'; Prefix = 'vm2-critical-'; Sudo = 'sudo ' }
    )
    foreach ($p in $pulls) {
        $name = (& ssh @sshOpts @($p.Ssh) "$($p.Sudo)ls -1t $($p.Dir) | grep -m1 ^$($p.Prefix)" 2>$null | Select-Object -First 1)
        if (-not $name -or $name -notmatch '^vm[12]-critical-[0-9-]+\.tgz$') { Show $p.Name $false 'no backup found'; continue }
        $remote = "$($p.Dir)/$name"
        $p.Cat = "$($p.Sudo)cat"
        $local = Join-Path $backupDir (Split-Path $remote -Leaf)
        if (Test-Path $local) { Show $p.Name $true "already have $(Split-Path $remote -Leaf)"; continue }
        # cmd.exe redirection keeps the bytes intact (PowerShell 5.1 pipes would re-encode binary data)
        cmd /c "ssh -o BatchMode=yes -o ConnectTimeout=60 $($p.Ssh -join ' ') ""$($p.Cat) $remote"" > ""$local"""
        if ($LASTEXITCODE -eq 0 -and (Get-Item $local).Length -gt 0) { Show $p.Name $true "saved $(Split-Path $local -Leaf) ($([math]::Round((Get-Item $local).Length/1KB)) KB)" }
        else { Remove-Item $local -ErrorAction SilentlyContinue; Show $p.Name $false 'copy failed' }
    }
    # reminders live in Cloudflare D1: export them too (needs `npx wrangler login` on this laptop)
    $workerDir = Join-Path $PSScriptRoot 'assistant\reminder_worker'
    $d1Out = Join-Path $backupDir ("reminders-d1-{0}.sql" -f (Get-Date -Format 'yyyyMMdd'))
    if ((Test-Path $workerDir) -and -not (Test-Path $d1Out)) {
        Push-Location $workerDir
        cmd /c "npx --yes wrangler@4 d1 export pa-reminders --remote --output ""$d1Out"" >nul 2>&1"
        Pop-Location
        if (Test-Path $d1Out) { Show 'Reminders (Cloudflare)' $true "saved $(Split-Path $d1Out -Leaf)" }
        else { Show 'Reminders (Cloudflare)' $false 'export failed (run: npx wrangler login)' }
    }
    # keep the newest 14 archives per VM / per kind
    foreach ($pat in 'vm1-critical-*.tgz', 'vm2-critical-*.tgz', 'reminders-d1-*.sql') {
        Get-ChildItem $backupDir -Filter $pat | Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 | Remove-Item -ErrorAction SilentlyContinue
    }
}
