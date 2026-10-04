@echo off
cd /d "%~dp0"

:: Relaunch this script elevated if needed.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting Administrator rights for the Ragnarok bot...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

if not exist ".venv\Scripts\python.exe" (
  echo Bot environment not installed yet.
  echo Run setup_bot.bat first.
  pause
  exit /b 1
)

echo Starting Ragnarok bot as Administrator...
echo F10 = debug screenshot ^| F11 = pause ^| F12 = stop
".venv\Scripts\python.exe" main.py
echo.
echo Bot closed.
pause
