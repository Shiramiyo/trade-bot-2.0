@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "MT5_EXE=C:\Program Files\MetaTrader 5\terminal64.exe"

if not exist "%PY%" (
  echo Python virtual environment not found at "%PY%".
  pause
  exit /b 1
)

REM 1. Ensure MetaTrader 5 is running and logged in
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'terminal64.exe' -and $_.ExecutablePath -eq $env:MT5_EXE }; if ($p) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  if exist "%MT5_EXE%" (
    echo Starting MetaTrader 5...
    start "" "%MT5_EXE%"
  )
)

echo Waiting for MetaTrader 5 connection...
"%PY%" -c "import time, MetaTrader5 as mt5; ok=False; [((ok:=True) if (mt5.initialize() and mt5.account_info() is not None) else time.sleep(0.5)) for _ in range(30) if not ok]; (mt5.shutdown() if ok else None); raise SystemExit(0 if ok else 1)" >nul 2>&1
if errorlevel 1 (
  echo MetaTrader 5 is not logged in yet. Check MT5 and try again.
  pause
  exit /b 1
)

REM 2. Start Demo Trading Bot (skip if already running to prevent duplicate bots)
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine -match 'live_mt5_bot\.py' }; if ($p) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo Starting Donchian 1H Demo Bot...
  start "Donchian 1H Demo Bot" /min cmd /c ""%PY%" "%~dp0live_mt5_bot.py" || pause"
) else (
  echo Donchian 1H Demo Bot is already running.
)

REM 3. Start Web Terminal Server on port 8080 (skip if already listening)
powershell -NoProfile -Command "try { $c = [Net.Sockets.TcpClient]::new(); $ar = $c.BeginConnect('127.0.0.1', 8080, $null, $null); $ok = $ar.AsyncWaitHandle.WaitOne(250); if ($ok -and $c.Connected) { $c.EndConnect($ar); $c.Close(); exit 0 } else { $c.Close(); exit 1 } } catch { exit 1 }"
if errorlevel 1 (
  echo Starting TradeTerminal Server on port 8080...
  start "TradeTerminal Server" /min cmd /c ""%PY%" "%~dp0terminal_server.py" || pause"
  powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { try { $c = [Net.Sockets.TcpClient]::new(); $ar = $c.BeginConnect('127.0.0.1', 8080, $null, $null); $ok = $ar.AsyncWaitHandle.WaitOne(250); if ($ok -and $c.Connected) { $c.EndConnect($ar); $c.Close(); exit 0 }; $c.Close() } catch {}; Start-Sleep -Milliseconds 250 }; exit 1"
  if errorlevel 1 (
    echo Could not verify TradeTerminal Server on port 8080. Check the "TradeTerminal Server" window.
    pause
    exit /b 1
  )
)

REM 4. Open standalone desktop app window
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
  start "" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" --app=http://127.0.0.1:8080
) else if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" (
  start "" "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" --app=http://127.0.0.1:8080
) else if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
  start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" --app=http://127.0.0.1:8080
) else (
  start "" "http://127.0.0.1:8080"
)
