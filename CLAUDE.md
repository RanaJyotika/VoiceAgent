# Voice Agent project rules

## Strict: no paid API calls without permission
The Gemini API key in `.env` is billed to the owner. Never run anything that calls it: not `scripts/smoke_test.py`, `scripts/ws_test.py`, `scripts/convo_test.py`, `scripts/voice_check.py`, `scripts/list_models.py`, and not a browser call on the test page. The owner tests manually and reports results. If a test seems necessary, ask first, state what it calls and the rough cost, and wait for a yes.

Free checks are fine: syntax and import checks, starting the server, loading the page, reading `data/server.log` and the SQLite database.

## Running
`run.bat` starts the server at http://127.0.0.1:8000 without auto-reload. Restart it after code changes.

## Phone calls (Plivo)
`run_phone.bat` starts a Cloudflare tunnel plus the server and passes the tunnel address as PUBLIC_BASE_URL. The address changes on every start. The "Call my phone" button and `POST /api/calls/dial/{id}` place REAL PAID calls (Plivo + Gemini). Never trigger them; only the owner does. `DIAL_ONLY_MY_NUMBER=true` (default) sends every call to PHONE_NUMBER in `.env`.
