@echo off
rem Start the Bargain Agent web UI and open it in the browser.
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Python environment not found. Run:  py -3.13 -m venv venv  then  venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
if not exist ".env" (
  echo .env not found. Copy .env.example to .env and fill in the keys first.
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8765/"
"venv\Scripts\python.exe" webapp.py --port 8765
pause
