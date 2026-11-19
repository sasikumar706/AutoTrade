# Start the Kite Algo server on this PC. Keep this window open while trading.
$backend = Split-Path -Parent $PSScriptRoot
Set-Location $backend
$vpy = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { Write-Host "Run windows_setup.ps1 first." -ForegroundColor Yellow; exit 1 }
if ((Get-TimeZone).Id -ne "India Standard Time") {
    Write-Host "WARNING: Windows time zone is $((Get-TimeZone).Id). Set it to (UTC+05:30) India Standard Time - market hours use the PC clock." -ForegroundColor Yellow
}
# keep the PC awake while the server runs
powercfg /change standby-timeout-ac 0 2>$null
Write-Host "Kite Algo server on http://0.0.0.0:8000  (Ctrl+C to stop)"
& $vpy -m uvicorn app.main:app --host 0.0.0.0 --port 8000
