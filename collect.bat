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
echo === Collect ground items + phone promo, 20 clicks total ===
echo Do not touch the mouse while running. Ctrl+C to stop.
"%PY%" collect.py --clicks 20 --click --phone --wait 120 %*
pause
