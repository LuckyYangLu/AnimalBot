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
echo === CLICK MODE: the mouse WILL be moved ===
echo Do not touch the mouse while running. Ctrl+C to stop.
"%PY%" bot.py --click --max-actions 6 %*
pause
