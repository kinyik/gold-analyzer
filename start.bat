@echo off
rem Double-click to start the analyzer (runner) and the dashboard.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
set PYTHONIOENCODING=utf-8
start "Gold Runner" cmd /k ".venv\Scripts\python.exe -m app.runner"
timeout /t 5 /nobreak >nul
start "Gold Dashboard" cmd /k ".venv\Scripts\python.exe -m streamlit run dashboard.py"
timeout /t 5 /nobreak >nul
start "" http://localhost:8501
