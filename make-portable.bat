@echo off
setlocal
cd /d "%~dp0"

set "SRC=%DSH_HOME%\dsh-runtimes\dsh-primary-runtime\dependencies\python"
if not exist "%SRC%\python.exe" (
    echo [ERROR] Bundled python not found at:
    echo         %SRC%
    echo         Run this from the machine that has DeepSeek Harness installed.
    pause
    exit /b 1
)

if exist "%~dp0python\python.exe" (
    echo [INFO] Existing .\python found, removing old copy...
    rmdir /s /q "%~dp0python"
)

echo Copying the python runtime into .\python  ^(about 150 MB, 1-3 minutes^)
robocopy "%SRC%" "%~dp0python" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo [ERROR] Copy failed.
    pause
    exit /b 1
)

echo.
echo [OK] Portable runtime is ready: .\python\python.exe
echo.
echo Self test:
"%~dp0python\python.exe" -c "import numpy, PIL; print('  numpy', numpy.__version__, '/ PIL', PIL.__version__)"
if errorlevel 1 (
    echo [ERROR] Self test failed - do not ship this copy.
    pause
    exit /b 1
)

echo.
echo Now zip this whole folder (game-bot) and copy it to the other Windows PC.
echo On that PC: open the mini program, then double-click start-daemon.bat.
pause
