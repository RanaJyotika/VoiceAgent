@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Voice Agent running at http://127.0.0.1:8000  (close this window to stop)
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
