@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Stopping Animal Restaurant Bot ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
echo.
pause
