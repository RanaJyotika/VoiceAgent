# Voice Agent MVP

AI outbound calling agent on the Gemini Live API. Stage 1: order-confirmation agent tested in the browser with contacts uploaded from Excel. Stage 2 adds real phone calls through Plivo.

## Run

1. Put your keys in `.env` (see `.env.example`).
2. Double-click `run.bat`, or run:

```bash
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
```

3. Open http://127.0.0.1:8000 in Chrome or Edge. Use headphones.

## Use

1. Download the Excel template from the page, fill one row per order, upload it.
2. Pick a contact and press Start call. The agent speaks first, like a real call. Answer it as the customer would.
3. When the agent says goodbye it records the outcome and ends the call. The call history table shows outcome, duration, token usage and the transcript.

## Calling the whole list

The "Call the list" card on the dashboard rings every contact in the sheet one after another (start it with `run_phone.bat` running, since these are real calls):

- never-called contacts go first, in sheet order; no-answer and busy contacts are rung again after "Retry after" minutes, up to "Tries per contact" times;
- calls are placed only inside the calling hours (Indian time), and "Stop after" caps a run (use it for tests);
- the campaign pauses itself if calls start failing on our side (Gemini billing, Plivo, the tunnel) so a bad key cannot burn through the list;
- every contact ends up with a status: confirmed, change requested, cancelled, call later (callback time in the notes), no answer, busy, wrong number, do not call, or needs review (customer hung up, line dropped, or the agent could not make it out);
- "Download results" gives the uploaded sheet back with the status, tries, the agent's notes (mismatch, new address, cancel reason, callback time) and the transcript beside each row.

With `DIAL_ONLY_MY_NUMBER=true` every campaign call rings your own phone: the dashboard warns about this.

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
backend/            the Python server: FastAPI, calls, database (run from the project folder)
frontend/           the dashboard: Next.js, built by `npm run build` to frontend/out, served by Python at /
.env, data/, company.txt, tools/   settings, database and recordings, company facts, cloudflared
```

The original dashboard is at http://127.0.0.1:8000/classic until each page is rebuilt in `frontend/`.

Working on the dashboard: start the Python server (`run.bat`), then in `frontend/` run `npm run dev` and open
http://localhost:3000. It forwards `/api` to the Python server on port 8000. `npm run build` makes the version
the Python server shows; `run.bat` does this on every start.

Inside `backend/`:

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

Each call row stores the token usage Gemini reports, added up over every model turn and split by modality (audio in, text in, audio out, text out). The cost column is worked out from those tokens at the prices in `config.py` (`GEMINI_PRICE_PER_M`) and the `USD_INR` rate, plus Plivo's per-30-second pulse. A `~` in front of the figure means Gemini reported nothing for that call and it is a per-minute guess instead.

Gemini Live bills every model turn for the whole conversation so far, so the cost of a call grows faster than its length: a 3-minute call with 8 turns costs several times a 1-minute call with 3 turns. Keep calls short. On 21 Sep 2026 Google billed about Rs 700 for 22 minutes of calls (about Rs 32 a minute) while the old dashboard estimate said Rs 30.

Phone calls are recorded (customer's side only) to `data/recordings/call_<id>.wav`; the transcript view has a Listen link. Set `PHONE_RECORD=false` to turn this off.

## Two voice agents

Calls normally run on Gemini Live. OpenAI's Realtime API is wired in as a second agent
(`app/openai_bridge.py`) and can run a call on its own, so the two can be compared.

- Pick the model per call from the dashboard dropdown, or set `GEMINI_LIVE_MODEL`.
- Set `OPENAI_API_KEY` in `.env` to switch it on. Without it the fallback stays off.
- A call falls back to OpenAI automatically only when Gemini cannot take the call at all:
  credits gone, key refused. A transient error still just reconnects to Gemini, because that
  works and a mid-call provider switch changes the agent's voice.
- After such a failure, later calls skip Gemini for `PROVIDER_COOLDOWN_SECONDS` (15 minutes by
  default) instead of losing three seconds each rediscovering it.
- The voice is matched by gender, so Gauri stays a woman and Arjun a man on either provider.
- Each call's stored usage records which `provider` and `model` answered it, and the cost is
  priced with that provider's own rates.

`gpt-realtime-2.1-mini` costs about the same per call as Gemini 2.5 Flash native audio, because
OpenAI bills repeated prompt text at a tenth. The full `gpt-realtime-2.1` is roughly four times
dearer. Both numbers come from replaying real calls 53 to 58 through each price list.

While Gemini prepares each reply (about 3 s) the phone plays a short "hmm" or "achha" in the agent's voice. The clips are made once per voice with Gemini text-to-speech on the first call that uses the voice (a few paise) and kept in `data/fillers/<voice>/`; until they exist a soft keyboard tap is played. `FILLERS=false` turns the sounds off, `FILLER_GENERATE=false` forbids the text-to-speech call.
