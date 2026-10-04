@echo off
cd /d "%~dp0"
echo.
echo === Ragnarok Bot Setup ===
python --version
if errorlevel 1 (
  echo Python was not found. Install Python 3.12 and enable Add Python to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
)
echo Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo Setup completed.
echo You can now start the bot with run_bot.bat
pause
