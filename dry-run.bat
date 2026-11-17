@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
call "%~dp0_python.bat"
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3 or set DSH_HOME.
    pause
    exit /b 1
)
echo === DRY RUN: recognize only, no clicking ===
"%PY%" bot.py --duration 30 %*
pause
