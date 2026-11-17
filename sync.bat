@echo off
rem Sync code between the edit source and the runnable copy.
rem   Run from the workspace copy -> pushes to D:\AnimalBot
rem   Run from D:\AnimalBot       -> pulls from the workspace copy
rem Excludes the python runtime (large, already present) and screenshots.
setlocal
set "RUN=D:\AnimalBot"
set "WS=D:\Users\Documents\deepseek-harness\default-workspace\game-bot"

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

if /i "%HERE%"=="%RUN%" (
    set "SRC=%WS%"
    echo Pulling from workspace: %WS%
) else (
    set "SRC=%HERE%"
    echo Pushing to runtime: %RUN%
)

if not exist "%RUN%" (
    echo [ERROR] %RUN% not found.
    pause
    exit /b 1
)
if not exist "%SRC%" (
    echo [ERROR] source not found: %SRC%
    pause
    exit /b 1
)

robocopy "%SRC%" "%RUN%" /E /XD python shots __pycache__ /XF *.log *.pid /NFL /NDL /NJH /NJS /NP
if errorlevel 8 (
    echo [ERROR] Sync failed.
    pause
    exit /b 1
)
echo.
echo [OK] Code files in both places are now identical.
