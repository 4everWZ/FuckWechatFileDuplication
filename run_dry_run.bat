@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0fuck_wechat_file_duplication.py" --config "%~dp0config.json" --dry-run >> "%~dp0logs\manual_dry_run.log" 2>&1
) else (
  py -3 "%~dp0fuck_wechat_file_duplication.py" --config "%~dp0config.json" --dry-run >> "%~dp0logs\manual_dry_run.log" 2>&1
)
type "%~dp0logs\manual_dry_run.log"
