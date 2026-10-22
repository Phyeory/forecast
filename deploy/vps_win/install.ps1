# install.ps1 — Windows VPS setup for the FTMO MT5 bridge.
#
# Run as Administrator on the Windows VPS (PowerShell 5+):
#   Set-ExecutionPolicy -Scope Process Bypass; .\install.ps1 -Server "FTMO-Demo"
#
# What it does:
#   1) copies mt5_bridge_server.py into C:\mt5bridge\
#   2) creates a venv there and installs MetaTrader5
#   3) asks for MT5 login/password/server and writes C:\mt5bridge\creds.env
#      (LOCAL ONLY — never in any repo, never printed)
#   4) generates start_bridge.cmd from those creds
#   5) registers a Scheduled Task "MT5Bridge" that runs at every logon
#
# The MT5 terminal itself must still be installed, started at logon and
# logged into the FTMO account (demo first, challenge after the rehearsal).
param([int]$Port = 8765, [int]$Magic = 77001, [string]$Server = "")
$ErrorActionPreference = "Stop"
$root = "C:\mt5bridge"
New-Item -ItemType Directory -Force -Path $root | Out-Null

# 1) bridge server (deploy/vps_win/../.. = repo root)
$src = Join-Path $PSScriptRoot "..\..\backend\market_live\mt5_bridge_server.py"
Copy-Item -Force $src "$root\mt5_bridge_server.py"

# 2) python + venv + dependency
$py = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $py) {
    Write-Host "installing Python 3.12 via winget (64-bit; required by MetaTrader5)…"
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    $py = "py"
}
& $py -3 -m venv "$root\venv"
& "$root\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$root\venv\Scripts\python.exe" -m pip install --quiet MetaTrader5

# 3) credentials — local file only
$login = Read-Host "MT5 login (e.g. 5201123)"
$pwd   = Read-Host "MT5 password"   # plain text on purpose; bridge needs it for --login
if (-not $Server) { $Server = Read-Host "MT5 server name (e.g. FTMO-Demo)" }
"MT5_LOGIN=$login`nMT5_PASSWORD=$pwd`nMT5_SERVER=$Server" | Out-File -FilePath "$root\creds.env" -Encoding ascii

# 4) launcher
@"
@echo off
set /p _=<nul
for /f "usebackq tokens=1,* delims==" %%a in ("%root%\creds.env") do (
  if "%%a"=="MT5_LOGIN"    set MT5_LOGIN=%%b
  if "%%a"=="MT5_PASSWORD" set MT5_PASSWORD=%%b
  if "%%a"=="MT5_SERVER"   set MT5_SERVER=%%b
)
"%root%\venv\Scripts\python.exe" "%root%\mt5_bridge_server.py" --port $Port --magic $Magic --login %MT5_LOGIN% --password %MT5_PASSWORD% --server %MT5_SERVER% >> "%root%\bridge.log" 2>&1
"@ | Out-File -FilePath "$root\start_bridge.cmd" -Encoding ascii

# 5) scheduled task at logon (SURVIVES RDP logoff only if "Run whether user is logged on or not" — schtasks ONLOGON keeps it in the session; use /RU SYSTEM preferred for terminal apps requiring user session, therefore keep ONLOGON + autologon on the VPS)
schtasks /Create /TN "MT5Bridge" /TR "\"$root\start_bridge.cmd\"" /SC ONLOGON /RL HIGHEST /F
schtasks /Run /TN "MT5Bridge"

# 6) sanity
Start-Sleep -Seconds 3
try {
  $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5
  Write-Host "bridge health: $($h | ConvertTo-Json -Compress)"
} catch {
  Write-Host "bridge not answering yet — check $root\bridge.log"
}
Write-Host ""
Write-Host "REHEARSAL (no MT5, no MT5 creds needed):"
Write-Host "  $root\venv\Scripts\python.exe $root\mt5_bridge_server.py --port $Port --simulate"
Write-Host "MT5 terminal must be running + logged in before the real bridge can connect."
Write-Host "REMEMBER: MT5 terminal autostart at logon + auto-login (Options -> Server)."
Write-Host "Enable Windows autologon (netplwiz) so ONLOGON tasks run after a VPS reboot while unattended."