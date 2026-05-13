@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto :error
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :error
echo.
echo Virtual environment ready.
echo Use run_dry_run.bat first, then run_full_once.bat.
exit /b 0
:error
echo setup_venv failed.
exit /b 1
