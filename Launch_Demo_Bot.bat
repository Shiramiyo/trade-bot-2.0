@echo off
setlocal
cd /d "%~dp0"
title Donchian 1H Demo Bot (NQ + XAUUSD)

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Python virtual environment not found at "%PY%".
  pause
  exit /b 1
)

powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine -match 'live_mt5_bot\.py' }; if ($p) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
  echo Donchian 1H Demo Bot is already running.
  pause
  exit /b 0
)

echo Starting Donchian 1H Live Demo Bot...
echo Make sure MetaTrader 5 is open and "Algo Trading" is enabled.
echo.
"%PY%" "%~dp0live_mt5_bot.py"
if errorlevel 1 (
  echo.
  echo Bot stopped with an error. Check the message above.
  pause
)
