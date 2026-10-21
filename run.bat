@echo off
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
echo Voice Agent running at http://127.0.0.1:8000  (close this window to stop)
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
