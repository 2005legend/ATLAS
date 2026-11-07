# Start the local assistant dashboard and open it in your browser.
# Usage (PowerShell, from this folder):  .\dashboard.ps1
# If scripts are blocked:                 powershell -ExecutionPolicy Bypass -File .\dashboard.ps1
# Needs: Tailscale connected, VM 1 awake (run .\wake-vms.ps1 first if the page says VM 1 is unreachable).
$server = Join-Path $PSScriptRoot 'dashboard\server.py'
$token = Join-Path $env:USERPROFILE '.hermes-dashboard\token'
if (-not (Test-Path $token)) { Write-Host "Token missing: $token" -ForegroundColor Yellow; exit 1 }
$running = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($running) { Start-Process 'http://127.0.0.1:8765'; Write-Host 'Dashboard already running: http://127.0.0.1:8765'; exit 0 }
python $server
