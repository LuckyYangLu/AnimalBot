@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
call "%~dp0_python.bat"
if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)
echo === One-shot action test (safe actions only) ===
echo Do not touch the mouse until it finishes.
"%PY%" oneclick.py
pause
