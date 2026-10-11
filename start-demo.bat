@echo off
rem Presentation mode: monitors with 30 days of price history (demo.db), live Find.
rem start-demo.bat reset   rebuilds demo.db (history ends today again).
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Run start.bat once first to set up the Python environment.
  pause
  exit /b 1
)
if /i "%~1"=="reset" (
  "venv\Scripts\python.exe" -m demo.seed_demo
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
if not exist demo.db "venv\Scripts\python.exe" -m demo.seed_demo
if not exist demo.db (
  echo Could not build demo.db.
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8765/"
"venv\Scripts\python.exe" webapp.py --port 8765 --db-path demo.db --demo
pause
