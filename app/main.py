"""FastAPI app: contact upload, browser test call over WebSocket, call history."""
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from .campaign import router as campaign_router
from .config import DATA_DIR, GEMINI_LIVE_MODEL, GEMINI_VOICE, OPENAI_REALTIME_MODEL, RECORDINGS_DIR, VOICES
from .contacts import build_results, build_template, check_rows, inspect_excel, is_valid_phone, normalise_phone
from .live_bridge import live_snapshot, run_call
from .plivo_bridge import router as plivo_router
from .prompts import LANGUAGES, get_prompt, with_language

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Keep call events on disk too, so a call can be diagnosed after the console window is closed.
_file_log = RotatingFileHandler(DATA_DIR / "server.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_file_log.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_file_log)
log = logging.getLogger("app")


class _QuietPolling(logging.Filter):
    """The dashboard polls these every 1 to 5 seconds; logging each hit buries the call events."""
    NOISY = ("/api/live", "/api/calls", "/api/stats")

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn access records carry (client, method, path, http_version, status). Failures still log.
        a = record.args
        if not isinstance(a, tuple) or len(a) < 5:
            return True
        return not (a[1] == "GET" and str(a[2]).split("?")[0] in self.NOISY and a[4] == 200)


logging.getLogger("uvicorn.access").addFilter(_QuietPolling())

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Voice Agent MVP")
db.init_db()
app.include_router(plivo_router)
app.include_router(campaign_router)


async def stall_monitor() -> None:
    """Logs whenever this server itself freezes for more than 0.3 s. If a call goes bad and this
    stayed quiet, the cause was the network or the providers, not this PC."""
    import time as _t
    while True:
        t0 = _t.monotonic()
        await asyncio.sleep(0.1)
        lag = _t.monotonic() - t0 - 0.1
        if lag > 0.3:
            log.warning("server stalled for %.1f s (PC busy or a slow operation)", lag)


@app.on_event("startup")
async def _start_monitor() -> None:
    asyncio.create_task(stall_monitor())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def get_config() -> dict:
    # While the OpenAI agent is being tested it is the only one offered, so a call cannot go to
    # Gemini by accident (calls 61 and 62 did exactly that). Put the two Gemini lines back to
    # have the choice again; nothing else needs changing.
    return {"model": OPENAI_REALTIME_MODEL, "voice": GEMINI_VOICE, "voices": VOICES, "languages": LANGUAGES,
            "models": [OPENAI_REALTIME_MODEL,
                       # "gemini-2.5-flash-native-audio-preview-12-2025",
                       # "gemini-3.8-live",
                       ]}


@app.get("/api/contacts")
async def contacts() -> list[dict]:
    return db.list_contacts()


def _read_sheet(file: UploadFile, content: bytes) -> dict:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Upload an .xlsx file")
    try:
        sheet = inspect_excel(content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that file as Excel: {exc}")
    if not sheet["rows"]:
        raise HTTPException(400, "No rows found. Check the header row matches the template.")
    existing = {c["phone"] for c in db.list_contacts() if c.get("phone")}
    sheet["rows"] = check_rows(sheet["rows"], existing)
    sheet["counts"] = {k: sum(1 for r in sheet["rows"] if r["_issue"] == k) for k in ("ok", "no_phone", "duplicate", "on_list")}
    sheet["missing"] = [c for c in ("customer_name", "phone", "product", "amount", "payment_mode") if c not in sheet["columns"]]
    return sheet


@app.post("/api/contacts/preview")
async def preview_contacts(file: UploadFile = File(...)) -> dict:
    """What the sheet contains, before anything is added: rows, columns understood, problems."""
    sheet = _read_sheet(file, await file.read())
    rows = [{k: v for k, v in r.items() if k != "_row"} | {"row": r["_row"], "issue": r["_issue"]} for r in sheet["rows"]]
    for r in rows:
        r.pop("_issue", None)
    return {"rows": rows[:500], "total": len(rows), "columns": sheet["columns"], "ignored": sheet["ignored"],
            "missing": sheet["missing"], "counts": sheet["counts"]}


@app.post("/api/contacts/upload")
async def upload_contacts(file: UploadFile = File(...), skip_duplicates: bool = True, skip_no_phone: bool = False) -> dict:
    """Add the sheet's rows to the call list. By default rows whose number is already on the list
    (or earlier in the same sheet) are left out; rows without a valid phone are kept so they show
    up as 'wrong number' for the team to fix, unless skip_no_phone is set."""
    sheet = _read_sheet(file, await file.read())
    keep = []
    for r in sheet["rows"]:
        if skip_duplicates and r["_issue"] in ("duplicate", "on_list"):
            continue
        if skip_no_phone and r["_issue"] == "no_phone":
            continue
        keep.append({k: v for k, v in r.items() if not k.startswith("_")})
    n = db.insert_contacts(keep) if keep else 0
    return {"imported": n, "skipped": len(sheet["rows"]) - n, "counts": sheet["counts"]}


@app.post("/api/contacts")
async def add_contact(row: dict) -> dict:
    """Add one contact typed on the screen."""
    clean = {k: str(row.get(k, "") or "").strip() for k in db.CONTACT_COLUMNS}
    clean["phone"] = normalise_phone(clean["phone"])
    if not clean["customer_name"] or not is_valid_phone(clean["phone"]):
        raise HTTPException(400, "Name and a valid 10-digit phone number are required.")
    db.insert_contacts([clean])
    return {"ok": True}


@app.delete("/api/contacts")
async def delete_contacts() -> dict:
    db.clear_contacts()
    return {"ok": True}


@app.get("/api/contacts/export")
async def export_results() -> Response:
    """The contact sheet with every call's result beside it, for the team."""
    contacts = sorted(db.list_contacts(), key=lambda c: c["id"])
    stamp = db.datetime.now(db.IST).strftime("%Y-%m-%d_%H%M")
    return Response(
        content=build_results(contacts, db.latest_call_for_contact),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=call_results_{stamp}.xlsx"},
    )


@app.get("/api/contacts/template")
async def template() -> Response:
    return Response(
        content=build_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=contacts_template.xlsx"},
    )


@app.get("/api/calls")
async def calls() -> list[dict]:
    return db.list_calls()


@app.get("/api/calls/{call_id}")
async def one_call(call_id: int) -> dict:
    """One call with its transcript, for the board (which may point at calls older than the history table shows)."""
    row = db.get_call(call_id)
    if not row:
        raise HTTPException(404, "No such call.")
    return row


@app.get("/api/calls/{call_id}/recording")
async def recording(call_id: int) -> FileResponse:
    """The customer's side of a phone call as a wav file (see PHONE_RECORD)."""
    name = db.get_recording(call_id)
    path = RECORDINGS_DIR / name if name else None
    if not path or not path.is_file():
        raise HTTPException(404, "No recording for this call.")
    return FileResponse(path, media_type="audio/wav", filename=name)


@app.get("/api/live")
async def live() -> list[dict]:
    return live_snapshot()


@app.get("/api/stats")
async def get_stats(period: str = "today") -> dict:
    return db.stats("all" if period == "all" else "today")


@app.get("/api/spend")
async def get_spend(days: int = 7) -> dict:
    return db.spend(max(1, min(90, days)))


@app.delete("/api/calls")
async def delete_calls() -> dict:
    db.clear_calls()
    return {"ok": True}


@app.websocket("/ws/call/{contact_id}")
async def browser_call(ws: WebSocket, contact_id: int) -> None:
    """Browser sends binary 16 kHz PCM frames; we send binary 24 kHz PCM frames and JSON text events."""
    await ws.accept()
    voice = ws.query_params.get("voice") or GEMINI_VOICE
    if voice not in VOICES:
        voice = GEMINI_VOICE
    model = ws.query_params.get("model") or GEMINI_LIVE_MODEL
    contact = db.get_contact(contact_id)
    if not contact:
        await ws.send_text(json.dumps({"type": "error", "text": "contact not found"}))
        await ws.close()
        return
    contact = with_language(contact, ws.query_params.get("language") or "")

    incoming: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send_audio(pcm: bytes) -> None:
        await ws.send_bytes(pcm)

    async def send_event(evt: dict) -> None:
        try:
            await ws.send_text(json.dumps(evt, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    async def reader() -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    await incoming.put(msg["bytes"])
                elif msg.get("text"):
                    data = json.loads(msg["text"])
                    if data.get("type") == "hangup":
                        break
                    if data.get("type") == "text" and str(data.get("text", "")).strip():
                        await incoming.put(str(data["text"]).strip())
        except WebSocketDisconnect:
            pass
        finally:
            await incoming.put(None)

    reader_task = asyncio.create_task(reader())
    try:
        await run_call(
            contact=contact,
            system_prompt=get_prompt(contact, voice),
            channel="browser",
            incoming_audio=incoming,
            send_audio=send_audio,
            send_event=send_event,
            model=model,
            voice=voice,
        )
    finally:
        reader_task.cancel()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


app.mount("/static", StaticFiles(directory=STATIC), name="static")
