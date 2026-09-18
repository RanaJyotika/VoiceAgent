"""FastAPI app: contact upload, browser test call over WebSocket, call history."""
import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from .config import GEMINI_LIVE_MODEL, GEMINI_VOICE, VOICES
from .contacts import build_template, is_valid_phone, normalise_phone, parse_excel
from .live_bridge import live_snapshot, run_call
from .plivo_bridge import router as plivo_router
from .prompts import get_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Voice Agent MVP")
db.init_db()
app.include_router(plivo_router)


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
    return {"model": GEMINI_LIVE_MODEL, "voice": GEMINI_VOICE, "voices": VOICES,
            "models": ["gemini-2.5-flash-native-audio-preview-12-2025", "gemini-3.8-live"]}


@app.get("/api/contacts")
async def contacts() -> list[dict]:
    return db.list_contacts()


@app.post("/api/contacts/upload")
async def upload_contacts(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Upload an .xlsx file")
    rows = parse_excel(await file.read())
    if not rows:
        raise HTTPException(400, "No rows found. Check the header row matches the template.")
    n = db.insert_contacts(rows)
    return {"imported": n}


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


@app.get("/api/live")
async def live() -> list[dict]:
    return live_snapshot()


@app.get("/api/stats")
async def get_stats(period: str = "today") -> dict:
    return db.stats("all" if period == "all" else "today")


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
