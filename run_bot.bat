@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Bot environment not installed yet.
  echo Run setup_bot.bat first.
  pause
  exit /b 1
)
echo Starting Ragnarok bot...
echo F10 = debug screenshot ^| F11 = pause ^| F12 = stop
".venv\Scripts\python.exe" main.py
echo.
echo Bot closed.
pause
