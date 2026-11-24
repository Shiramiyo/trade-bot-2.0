@echo off
setlocal
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine -match 'terminal_server\.py|live_mt5_bot\.py' }; if (-not $procs) { Write-Host 'No TradeTerminal or Demo Bot processes are running.'; exit 0 }; foreach ($p in $procs) { Write-Host ('Stopping PID ' + $p.ProcessId + ': ' + $p.CommandLine); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host 'Stopped cleanly. Note: any open MT5 positions remain in MetaTrader 5 with their current SL.'"
pause
