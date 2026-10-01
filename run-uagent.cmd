@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo UAgent environment is missing. Run setup first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe launcher.py
