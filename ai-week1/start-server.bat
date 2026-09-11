@echo off
REM ============================================================
REM  Start the sensor ingest server and open the dashboard
REM  Double-click this file to run.
REM  (Kept ASCII-only on purpose: Windows .bat + non-ASCII text
REM   is a classic encoding trap that garbles messages.)
REM ============================================================
cd /d "%~dp0server"

python --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found.
  echo Install Python 3 and tick "Add Python to PATH" during setup.
  pause
  exit /b 1
)

echo Starting sensor ingest server...
start "IMU Ingest Server" python server.py

REM give the server a moment to bind port 8000
timeout /t 3 >nul

start "" http://127.0.0.1:8000/

echo.
echo Done.
echo  - Server is running in the new "IMU Ingest Server" window.
echo  - That window prints the LAN IP (http://x.x.x.x:8000) which
echo    must match SERVER_URL in firmware/main/app_config.h.
echo  - To stop the server: close that window, or press Ctrl+C in it.
echo.
pause
