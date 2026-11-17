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
title Animal Restaurant Bot
echo ============================================
echo  Stop: Ctrl+C, or close this window, or run stop.bat
echo  Items : wait 1-5 min, then collect 3 min
echo  Phone : 40 clicks every 15 minutes
echo  Tips  : auto collect at 290000+ (ad, then claim)
echo  NPC   : only "refuse" buttons are clicked
echo ============================================
echo.
"%PY%" daemon.py --click --phone-now --phone-interval 900 --phone-clicks 40 %*
echo.
echo daemon exited with code %errorlevel%
pause
