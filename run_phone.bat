@echo off
rem Starts the server WITH a public address so Plivo can reach it (needed for real phone calls).
rem For browser-only testing, run.bat is enough.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
rem Build the dashboard (frontend/, Next.js) so the server shows the latest version. Needs Node.js;
rem if the build fails the server still starts, with the last build or the original dashboard.
if exist frontend\package.json (
  pushd frontend
  if not exist node_modules call npm install
  call npm run build
  popd
)
.venv\Scripts\python.exe scripts\start_with_tunnel.py
pause
