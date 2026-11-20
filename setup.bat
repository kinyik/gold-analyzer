@echo off
rem First-time setup: create virtual env and install packages.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3.12 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if not exist .env (
    copy .env.example .env >nul
    echo Created .env from .env.example - please edit it.
)
echo.
echo Setup done. Next: edit .env, then double-click start.bat
pause
