@echo off
rem Double-click to start the Kite Algo server on this PC. Keep this window open while trading.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\deploy\windows_start.ps1"
pause
