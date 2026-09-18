@echo off
rem Starts the server WITH a public address so Plivo can reach it (needed for real phone calls).
rem For browser-only testing, run.bat is enough.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe scripts\start_with_tunnel.py
pause
