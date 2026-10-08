@echo off
rem Start the Bargain Agent web UI and open it in the browser.
rem First run: creates the Python environment and installs the requirements.
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo First run: setting up the Python environment ^(a few minutes^)...
  py -3.13 -m venv venv 2>nul || py -3 -m venv venv 2>nul || python -m venv venv
  if not exist "venv\Scripts\python.exe" (
    echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH"^), then run start.bat again.
    pause
    exit /b 1
  )
  "venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo Installing the requirements failed; check the internet connection and run start.bat again.
    rmdir /s /q venv
    pause
    exit /b 1
  )
)
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env from .env.example. Fill in the LLM and SERPER keys in .env, then run start.bat again.
  notepad ".env"
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8765/"
"venv\Scripts\python.exe" webapp.py --port 8765
pause
