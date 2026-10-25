@echo off
REM garmin-health-import: Renpho weight and Google Fit blood pressure. See --help.
cd /d "%~dp0"
.venv\Scripts\python garmin_health_import.py %*
pause
