@echo off
REM Syncs Renpho -^> Garmin. Options: --dry-run, --since YYYY-MM-DD, --reset-credentials, --verbose
cd /d "%~dp0"
.venv\Scripts\python renpho_garmin_sync.py %*
pause
