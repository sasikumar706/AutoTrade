@echo off
rem Double-click to install the Kite Algo server on this PC (for LIVE orders).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\deploy\windows_setup.ps1"
echo.
echo Done. Next time just double-click START_SERVER.bat
pause
