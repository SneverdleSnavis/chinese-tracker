@echo off
cd /d "%~dp0"
start "Chinese Tracker Server" "%~dp0venv\Scripts\python.exe" -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8000"
