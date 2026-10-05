@echo off
rem One-shot check of all due tasks. Schedule this with Windows Task Scheduler
rem (see README "Monitoring without keeping the window open").
cd /d "%~dp0"
if not exist logs mkdir logs
"venv\Scripts\python.exe" main.py check-due --log logs\check-due.log >> logs\check-due-console.log 2>&1
