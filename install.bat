@echo off
REM Creates the Python virtual environment and installs dependencies (once per computer)
cd /d "%~dp0"
py -3 -m venv .venv || python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo.
echo Installation finished. First run: sync.bat --dry-run
pause
