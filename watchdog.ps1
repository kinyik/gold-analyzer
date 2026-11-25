# Gold Analyzer watchdog. Run every 5 minutes by Task Scheduler ("Gold Analyzer Watchdog") via watchdog.vbs.
# - Analyzer not running            -> start it
# - Analyzer running but no heartbeat (data\heartbeat.txt) for 15 min -> it is stuck: kill and restart
# - Dashboard or MT5 not running    -> start it
# To pause it (e.g. you closed the analyzer on purpose), create an empty file data\pause_watchdog.
# Actions are logged to data\watchdog.log.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $root 'data'
$logFile = Join-Path $data 'watchdog.log'
$heartbeat = Join-Path $data 'heartbeat.txt'
$mt5Exe = 'C:\Program Files\MetaTrader 5\terminal64.exe'
$staleMinutes = 15

function Write-WatchLog([string]$msg) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $msg
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

if (Test-Path (Join-Path $data 'pause_watchdog')) { exit 0 }
if (-not (Test-Path (Join-Path $root '.venv\Scripts\python.exe'))) { exit 0 }

# Right after logon autostart.bat is still starting things; leave it alone.
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
if (((Get-Date) - $boot).TotalMinutes -lt 5) { exit 0 }

function Get-AppProcesses([string]$pattern) {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe', 'cmd.exe') -and $_.CommandLine -like $pattern
    }
}

function Start-AppWindow([string]$title, [string]$command) {
    $arg = 'title ' + $title + ' && set PYTHONIOENCODING=utf-8 && ' + $command
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $arg -WorkingDirectory $root -WindowStyle Minimized
}

# MT5
if (-not (Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue) -and (Test-Path $mt5Exe)) {
    Start-Process -FilePath $mt5Exe
    Write-WatchLog 'MT5 was not running: started it'
}

# Analyzer
$runner = @(Get-AppProcesses '*-m app.runner*')
$runnerPython = @($runner | Where-Object { $_.Name -eq 'python.exe' })
if ($runnerPython.Count -eq 0) {
    foreach ($p in $runner) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-AppWindow 'Gold Runner' '.venv\Scripts\python.exe -m app.runner'
    Write-WatchLog 'Analyzer was not running: started it'
} else {
    $oldest = ($runnerPython | Sort-Object CreationDate | Select-Object -First 1).CreationDate
    $runningMinutes = ((Get-Date) - $oldest).TotalMinutes
    $beatAge = if (Test-Path $heartbeat) { ((Get-Date) - (Get-Item $heartbeat).LastWriteTime).TotalMinutes } else { 1e9 }
    if ($beatAge -gt $staleMinutes -and $runningMinutes -gt $staleMinutes) {
        foreach ($p in $runner) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 3
        Start-AppWindow 'Gold Runner' '.venv\Scripts\python.exe -m app.runner'
        Write-WatchLog ('Analyzer stuck (no heartbeat for {0:N0} min): restarted it' -f $beatAge)
    }
}

# Dashboard
if (@(Get-AppProcesses '*streamlit run dashboard.py*' | Where-Object { $_.Name -eq 'python.exe' }).Count -eq 0) {
    Start-AppWindow 'Gold Dashboard' '.venv\Scripts\python.exe -m streamlit run dashboard.py'
    Write-WatchLog 'Dashboard was not running: started it'
}
