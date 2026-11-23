@echo off
rem Run at Windows logon (Task Scheduler "Gold Analyzer"): start MT5, wait, then the analyzer and dashboard minimized.
rem Change MT5_EXE if MetaTrader 5 is installed somewhere else.
set "MT5_EXE=C:\Program Files\MetaTrader 5\terminal64.exe"
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist .venv\Scripts\python.exe (
    echo Please run setup.bat first.
    exit /b 1
)

rem Start MT5 if it is not running yet
"%SystemRoot%\System32\tasklist.exe" /FI "IMAGENAME eq terminal64.exe" 2>nul | "%SystemRoot%\System32\find.exe" /I "terminal64.exe" >nul
if errorlevel 1 (
    if exist "%MT5_EXE%" start "" "%MT5_EXE%"
)

rem Give MT5 time to log in (the analyzer also keeps retrying until MT5 is ready)
"%SystemRoot%\System32\PING.EXE" -n 61 127.0.0.1 >nul

rem Do not start a second copy if a python process for it is already running
call :running "app.runner"
if errorlevel 1 start "Gold Runner" /min cmd /k ".venv\Scripts\python.exe -m app.runner"
call :running "streamlit run dashboard.py"
if errorlevel 1 start "Gold Dashboard" /min cmd /k ".venv\Scripts\python.exe -m streamlit run dashboard.py"
exit /b 0

:running
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*%~1*' }) { exit 0 } else { exit 1 }"
exit /b %errorlevel%
