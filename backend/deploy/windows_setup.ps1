# Kite Algo server on a Windows PC (for LIVE orders from a whitelisted static IP).
# The phone (Termux) server keeps running backtests / paper; the app sends LIVE work here.
#
# Run in PowerShell from the repo folder:
#   powershell -ExecutionPolicy Bypass -File backend\deploy\windows_setup.ps1
$ErrorActionPreference = "Stop"
$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend

function Find-Python {
    foreach ($c in @("py -3.12", "py -3", "python")) {
        try { $v = & cmd /c "$c --version 2>&1"; if ($v -match "Python 3\.(1[0-9])") { return $c } } catch {}
    }
    return $null
}

Write-Host ">> Checking Python 3.10+"
$py = Find-Python
if (-not $py) {
    Write-Host "Python 3 not found - installing Python 3.12 with winget (Windows may ask for permission)..." -ForegroundColor Yellow
    try {
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    } catch { Write-Host "winget failed: $_" }
    # pick up the new PATH without reopening the window
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = Find-Python
    if (-not $py) {
        Write-Host "Python still not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH'), then run INSTALL_PC.bat again." -ForegroundColor Red
        exit 1
    }
}
Write-Host "   using: $py"

if (-not (Test-Path ".venv")) {
    Write-Host ">> Creating virtual environment (.venv)"
    cmd /c "$py -m venv .venv"
}
$vpy = Join-Path $backend ".venv\Scripts\python.exe"
Write-Host ">> Installing packages (first time takes a few minutes)"
& $vpy -m pip install --upgrade pip | Out-Null
& $vpy -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "Use the SAME Kite app (API key/secret) as on your phone, so one daily login works for both." -ForegroundColor Cyan
    $key = Read-Host "Kite API key"
    $secret = Read-Host "Kite API secret"
    $tok = Read-Host "App token for this PC server (Enter = generate a new one)"
    if (-not $tok) { $tok = & $vpy -c "import secrets; print(secrets.token_urlsafe(32))" }
    $live = Read-Host "Allow REAL-MONEY orders from this PC? (y/N)"
    $liveVal = if ($live -match "^[yY]") { "true" } else { "false" }
    $lines = Get-Content ".env"
    $lines = $lines -replace "^KITE_API_KEY=.*", "KITE_API_KEY=$key"
    $lines = $lines -replace "^KITE_API_SECRET=.*", "KITE_API_SECRET=$secret"
    $lines = $lines -replace "^APP_TOKEN=.*", "APP_TOKEN=$tok"
    $lines = $lines -replace "^LIVE_TRADING_ENABLED=.*", "LIVE_TRADING_ENABLED=$liveVal"
    Set-Content ".env" $lines -Encoding ascii
}

Write-Host ">> Running tests"
& $vpy -m pytest -q tests

Write-Host ">> Allowing port 8000 through Windows Firewall (private networks)"
try {
    if (-not (Get-NetFirewallRule -DisplayName "Kite Algo 8000" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "Kite Algo 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private | Out-Null
    }
    Write-Host "   done"
} catch {
    Write-Host "   Needs admin. Run once in an ADMIN PowerShell:" -ForegroundColor Yellow
    Write-Host '   New-NetFirewallRule -DisplayName "Kite Algo 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private'
}

$lan = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch "^(127|169\.254)\." -and $_.PrefixOrigin -ne "WellKnown" } |
        Select-Object -First 1).IPAddress
try { $pub = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 8) } catch { $pub = "(could not check)" }
$tokNow = (Select-String -Path ".env" -Pattern "^APP_TOKEN=(.*)").Matches[0].Groups[1].Value
$liveNow = (Select-String -Path ".env" -Pattern "^LIVE_TRADING_ENABLED=(.*)").Matches[0].Groups[1].Value

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host " PC server ready.  Start it with:  backend\deploy\start_server.bat"
Write-Host ""
Write-Host " In the Kite Algo app -> Settings -> PC server (live):"
Write-Host "   URL        : http://$($lan):8000      (same Wi-Fi as the phone)"
Write-Host "   App token  : $tokNow"
Write-Host ""
Write-Host " Real-money orders on this PC : $liveNow   (change LIVE_TRADING_ENABLED in backend\.env)"
Write-Host " Public IP Zerodha will see   : $pub"
Write-Host "   -> Kite developer console -> your app -> add this IP to the IP whitelist."
Write-Host "      It must be a STATIC IP (ask your ISP), or orders stop working when it changes."
Write-Host "======================================================================" -ForegroundColor Green
