# ZWFN - start the Finishing app over HTTPS on a clean port 8000.
#
# It first stops any lingering `server/app.py` processes, so the browser can
# never land on a stale plain-HTTP instance (which blocks the iPad camera with
# "The camera needs a secure connection"). Then it starts ONE HTTPS server.
#
# Usage:  right-click -> Run with PowerShell,  or:  .\run-https.ps1
# Stop the server with Ctrl+C in this window.

Set-Location -LiteralPath $PSScriptRoot

Write-Host "Stopping any running app.py servers..." -ForegroundColor Yellow
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'app\.py|app\.app\.run' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host ("  stopped PID {0}" -f $_.ProcessId)
    }
Start-Sleep -Seconds 2

# Show the current LAN IP so you know exactly what to open on the iPad.
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Select-Object -First 1).IPAddress
if (-not $ip) { $ip = "<this-pc-ip>" }

$env:USE_HTTPS = "true"
Write-Host ""
Write-Host "Starting ZWFN over HTTPS..." -ForegroundColor Green
Write-Host ("On the iPad open:  https://{0}:8000   (accept the certificate warning once)" -f $ip) -ForegroundColor Cyan
Write-Host "Look for 'Running on https://...' below. Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

python server/app.py
