@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  首次使用：准备便携版 Python 运行时（装到 .\python）
rem  已经有 python\python.exe 就跳过，只检查依赖
rem ============================================================

if exist "%~dp0python\python.exe" goto deps

echo.
echo ============================================
echo   首次运行：正在准备 Python 运行环境
echo   （约 25MB 下载 + 安装，1~3 分钟）
echo ============================================
echo.

set "PYVER=3.11.9"
set "EXE=%TEMP%\python-%PYVER%-amd64.exe"
set "URL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
set "MIRROR=https://mirrors.huaweicloud.com/python/%PYVER%/python-%PYVER%-amd64.exe"

if not exist "%EXE%" (
    echo [1/3] 下载 Python %PYVER% ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%URL%' -OutFile '%EXE%' -UseBasicParsing -TimeoutSec 600; exit 0 } catch { exit 1 }"
    if errorlevel 1 (
        echo       官方源失败，改用华为云镜像 ...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%MIRROR%' -OutFile '%EXE%' -UseBasicParsing -TimeoutSec 600; exit 0 } catch { exit 1 }"
        if errorlevel 1 (
            echo.
            echo [ERROR] 下载失败。请手动安装 Python 3.10+ 后重试，
            echo         或者把旧机器上的 python 文件夹整个拷过来。
            pause
            exit /b 1
        )
    )
) else (
    echo [1/3] 已有安装包缓存，跳过下载
)

echo [2/3] 安装到 .\python ...
start /wait "" "%EXE%" /quiet InstallAllUsers=0 TargetDir="%~dp0python" Include_pip=1 Include_test=0 Include_launcher=0 PrependPath=0 Shortcuts=0
if not exist "%~dp0python\python.exe" (
    echo.
    echo [ERROR] 安装失败：找不到 python\python.exe
    pause
    exit /b 1
)
echo       安装完成

:deps
echo [3/3] 检查依赖 pillow / numpy ...
"%~dp0python\python.exe" -c "import numpy, PIL" >nul 2>&1
if errorlevel 1 (
    echo       正在安装 pillow 和 numpy ...
    "%~dp0python\python.exe" -m pip install --upgrade pip >nul 2>&1
    "%~dp0python\python.exe" -m pip install pillow numpy
    if errorlevel 1 (
        echo.
        echo [ERROR] 依赖安装失败，请检查网络后重新运行 setup.bat
        pause
        exit /b 1
    )
)

"%~dp0python\python.exe" -c "import numpy, PIL; print('      numpy', numpy.__version__, '/ pillow', PIL.__version__)"
if errorlevel 1 (
    echo [ERROR] 环境自检失败
    pause
    exit /b 1
)

echo.
echo ============================================
echo   [OK] 环境就绪，现在可以双击 start-daemon.bat
echo ============================================
ping -n 3 127.0.0.1 >nul
exit /b 0
