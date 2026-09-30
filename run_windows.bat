@echo off
cd /d "%~dp0"
set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py"
if not defined PYTHON_CMD where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  echo Python was not found. Install Python 3.10+ and try again.
  exit /b 1
)
if not exist .venv %PYTHON_CMD% -m venv .venv
call .venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py
