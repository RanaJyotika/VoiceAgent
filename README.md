# Voice Agent MVP

AI outbound calling agent on the Gemini Live API. Stage 1: order-confirmation agent tested in the browser with contacts uploaded from Excel. Stage 2 adds real phone calls through Plivo.

## Run

1. Put your keys in `.env` (see `.env.example`).
2. Double-click `run.bat`, or run:

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

3. Open http://127.0.0.1:8000 in Chrome or Edge. Use headphones.

## Use

1. Download the Excel template from the page, fill one row per order, upload it.
2. Pick a contact and press Start call. The agent speaks first, like a real call. Answer it as the customer would.
3. When the agent says goodbye it records the outcome and ends the call. The call history table shows outcome, duration, token usage and the transcript.

## Excel columns

| Column | Example | Notes |
|---|---|---|
| order_id | VV-1001 | |
| customer_name | Ramesh Kumar | |
| phone | 9876543210 | 10 digits or +91 form |
| product | Rudraksha Mala 108 beads | |
| quantity | 1 | |
| amount | 1499 | rupees |
| payment_mode | COD or Prepaid | |
| address | full address | read back on the call |
| delivery_date | 22 September 2026 | |
| language | hindi, hinglish or english | how the agent opens the call |
| notes | anything the agent should know | |

## Layout

```
app/
  main.py         FastAPI routes and the browser WebSocket
  live_bridge.py  Gemini Live session: audio in, audio out, tools, transcript
  prompts.py      system prompt per campaign, built from a contact row
  tools.py        mark_outcome and end_call tool definitions
  contacts.py     Excel parsing and template
  db.py           SQLite: contacts and calls
  config.py       .env loading
  static/         test page, mic worklet, client JS
scripts/
  list_models.py  which Live models your key can use
  smoke_test.py   one text turn in, audio out
data/             SQLite database and uploads (git-ignored)
```

## Settings (optional, in .env)

- `GEMINI_LIVE_MODEL` default `gemini-2.5-flash-native-audio-preview-12-2025`. `gemini-3.8-live` also works on this key.
- `GEMINI_VOICE` default `Aoede`.
- `AGENT_NAME`, `BUSINESS_NAME`.

## Cost check

Each call row stores the token usage Gemini reports. Audio input is about $0.005 per minute and audio output about $0.018 per minute on the Live API, so a one-minute confirmation call costs roughly Rs 1 to 1.5 in AI before telephony.
