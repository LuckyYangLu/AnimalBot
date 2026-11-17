@echo off
rem Resolve a usable Python interpreter for the other .bat files.
rem   1) Prefer the portable runtime shipped next to this folder (python\python.exe)
rem   2) Then the runtime under DSH_HOME
rem   3) Finally python.exe on PATH, skipping the Microsoft Store stub
rem Usage:  call "%~dp0_python.bat" || exit /b 1
set "PY="
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
if defined PY goto done

if defined DSH_HOME if exist "%DSH_HOME%\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%DSH_HOME%\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if defined PY goto done

for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY (
        echo %%i | findstr /i /c:WindowsApps >nul
        if errorlevel 1 set "PY=%%i"
    )
)

:done
if not defined PY exit /b 1
exit /b 0
