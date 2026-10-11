@echo off
rem Record the lead's lines in your own voice, then build the narrated video.
cd /d "%~dp0"
"venv\Scripts\python.exe" presentation\record_voice.py
pause
