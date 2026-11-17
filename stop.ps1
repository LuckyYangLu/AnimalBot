# 停止挂机进程：优先按 daemon.pid，找不到就扫描 daemon.py 进程
$ErrorActionPreference = 'SilentlyContinue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $here 'daemon.pid'
$killed = $false

if (Test-Path $pidFile) {
    $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($procId) {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p) {
            Stop-Process -Id $procId -Force
            Write-Host "[OK] stopped pid $procId"
            $killed = $true
        } else {
            Write-Host "[WARN] pid $procId is not running"
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

if (-not $killed) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
             Where-Object { $_.CommandLine -like '*daemon.py*' }
    if ($procs) {
        foreach ($p in $procs) {
            Write-Host "  killing pid $($p.ProcessId)"
            Stop-Process -Id $p.ProcessId -Force
        }
    } else {
        Write-Host "no daemon.py process found"
    }
}
