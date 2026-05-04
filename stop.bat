@echo off
REM Stop the hub process recorded in hub.pid. Safe — only the PID written by
REM `python -m mcp_hub` is targeted; nothing else is touched.
setlocal
cd /d "%~dp0"

if not exist hub.pid (
  echo No hub.pid found. Nothing to stop.
  endlocal
  exit /b 0
)

set /p PID=<hub.pid

REM Make sure the process still exists before killing.
tasklist /FI "PID eq %PID%" 2>nul | findstr /R "^python\.exe " >nul
if errorlevel 1 (
  echo PID %PID% is not a running python process. Removing stale hub.pid.
  del /q hub.pid
  endlocal
  exit /b 0
)

echo Stopping hub PID %PID%...
taskkill /F /PID %PID% >nul
del /q hub.pid 2>nul
endlocal
