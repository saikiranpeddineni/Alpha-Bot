# AlphaDesk watchdog — keeps exactly one server alive on ALPHADESK_PORT.
# Usage: run in its own window / scheduled task:
#   powershell -ExecutionPolicy Bypass -File scripts\watchdog.ps1
# Checks /api/status every 30s. If the port is dead it starts ONE server via
# .venv python (never the store-Python shim path). Exits are never blocked by
# this script — it only (re)starts the API process; the engine's own
# STALE-POSITION GUARD + EOD latch own position safety.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$port = "8744"
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env") {
        if ($line -match "^\s*ALPHADESK_PORT\s*=\s*(.+)$") { $port = $Matches[1].Trim() }
    }
}
$venvPy = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$log = Join-Path (Get-Location) "logs\watchdog.log"

function Write-Watchdog($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $log -Value "[$ts] $msg"
    Write-Host "[$ts] $msg"
}

Write-Watchdog "Watchdog started (port $port). Press Ctrl+C to stop."
while ($true) {
    $alive = $false
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 8
        $alive = $true
    } catch { $alive = $false }

    if (-not $alive) {
        # Port dead: make sure no orphan run_server is half-alive, then start one.
        $orphans = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
            Where-Object { $_.CommandLine -like "*run_server.py*" }
        foreach ($o in $orphans) {
            try { Stop-Process -Id $o.ProcessId -Force; Write-Watchdog "Killed orphan run_server PID $($o.ProcessId)" } catch {}
        }
        if (-not (Test-Path $venvPy)) {
            Write-Watchdog "ERROR: venv python missing at $venvPy -- fix install first"
        } else {
            $p = Start-Process -FilePath $venvPy -ArgumentList "backend\run_server.py" `
                -WorkingDirectory (Get-Location) -PassThru
            Write-Watchdog "Server dead -- restarted run_server PID $($p.Id)"
            Start-Sleep -Seconds 30
        }
    }
    Start-Sleep -Seconds 30
}
