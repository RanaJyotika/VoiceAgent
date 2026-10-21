"""FastAPI app: contact upload, browser test call over WebSocket, call history."""
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import catalog, coupons, db, messaging, mongo_sync, orders_view, shopify, store_orders
from .autodial import resume_after_restart, router as autodial_router
from .campaign import CAMPAIGN, router as campaign_router
from .config import (AGENT_LABELS, DATA_DIR, GEMINI_LIVE_MODEL, GEMINI_VOICE, OFFERED_MODELS, PAGE_BUILD, RECORDINGS_DIR,
                     ROOT, SARVAM_VOICES, STALE_PAGE, VOICES)
from .contacts import (build_results, build_template, call_type_named, check_rows, inspect_excel, is_valid_phone,
                       missing_columns, normalise_phone)
from .live_bridge import live_snapshot, run_call
from .intake import router as intake_router
from .overview import router as overview_router
from .plivo_bridge import router as plivo_router
from .call_types import CALL_TYPES, get_prompt
from .prompts import LANGUAGES, with_language

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Keep call events on disk too, so a call can be diagnosed after the console window is closed.
_file_log = RotatingFileHandler(DATA_DIR / "server.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_file_log.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logging.getLogger().addHandler(_file_log)
log = logging.getLogger("app")


class _QuietPolling(logging.Filter):
    """The dashboard polls these every 1 to 5 seconds; logging each hit buries the call events. The Coupons page
    polls its lists every 10 s while open, and every page asks for the waiting messages every 30 s."""
    NOISY = ("/api/live", "/api/calls", "/api/stats", "/api/coupons", "/api/messages", "/api/messages/status")

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
shopify.init_tables()
orders_view.init_tables()
store_orders.init_tables()  # batches of automatic calling: what each one reached and cost
coupons.init_tables()       # coupon codes; DIWALIDILSE10 is added the first time
messaging.init_tables()     # the outbox for coupon messages
app.include_router(plivo_router)
app.include_router(campaign_router)
app.include_router(autodial_router)
app.include_router(shopify.router)
# Before the contacts intake: its /api/intake/{campaign_id} would otherwise take /api/intake/coupons.
app.include_router(coupons.router)
app.include_router(intake_router)
app.include_router(messaging.router)
app.include_router(catalog.router)
app.include_router(mongo_sync.router)
app.include_router(store_orders.router)
app.include_router(orders_view.router)
app.include_router(overview_router)


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
    asyncio.create_task(shopify.worker())
    asyncio.create_task(catalog.worker())   # the product catalogue, read from MongoDB every few minutes
    asyncio.create_task(mongo_sync.worker())   # every finished call, copied to MongoDB
    asyncio.create_task(store_orders.worker())   # new store orders from the main database become confirmation calls
    asyncio.create_task(messaging.worker())   # coupon messages, once a message service is set up
    resume_after_restart()   # automatic calling always starts off; only the Switch on button starts it


@app.get("/classic")
async def classic() -> Response:
    """The original dashboard (static/). The new one (frontend/) links here for the pages it does not have yet."""
    # The browser kept running yesterday's scripts after a restart (new filters with nothing behind
    # them). Never cache the page, and tag each script with this server's build so a restart fetches them fresh.
    page = (STATIC / "index.html").read_text(encoding="utf-8").replace('.js"></script>', f'.js?v={PAGE_BUILD}"></script>')
    return Response(page, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
async def get_config() -> dict:
    # Only OFFERED_MODELS (config.py). A browser that remembered another model falls back to the first.
    return {"model": OFFERED_MODELS[0], "voice": GEMINI_VOICE, "voices": VOICES, "sarvam_voices": SARVAM_VOICES,
            "languages": LANGUAGES,
            "models": OFFERED_MODELS, "model_labels": AGENT_LABELS, "build": PAGE_BUILD}


# --- call types and campaigns ----------------------------------------------------------------------

@app.get("/api/call-types")
async def call_types() -> list[dict]:
    """What each kind of call does, its sheet columns and the campaign facts it asks for."""
    return [t.public() for t in CALL_TYPES.values()]


def _campaign_or_404(campaign_id: int) -> dict:
    camp = db.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "No such campaign.")
    return camp


def _clean_campaign(body: dict, call_type_key: str) -> dict:
    """Name, facts and last call date from the dashboard form, checked against the call type."""
    ct = CALL_TYPES.get(call_type_key)
    if not ct:
        raise HTTPException(400, "Pick a call type.")
    name = str(body.get("name") or "").strip()[:120]
    if not name:
        raise HTTPException(400, "Give the campaign a name.")
    given = body.get("facts") or {}
    facts = {f.key: str(given.get(f.key) or "").strip()[:2000] for f in ct.facts}
    missing = [f.label for f in ct.facts if f.required and not facts[f.key]]
    if missing:
        raise HTTPException(400, "Missing: " + ", ".join(missing))
    last = str(body.get("last_call_date") or "").strip()[:10]
    return {"name": name, "facts": facts, "last_call_date": last}


@app.get("/api/campaigns")
async def campaigns() -> list[dict]:
    running = CAMPAIGN.settings.campaign_id if CAMPAIGN.running else None
    out = db.list_campaigns()
    for c in out:
        c["running"] = c["id"] == running
        ct = CALL_TYPES.get(c["call_type"])
        c["call_type_name"] = ct.name if ct else c["call_type"]
    return out


@app.post("/api/campaigns")
async def create_campaign(body: dict) -> dict:
    key = str(body.get("call_type") or "")
    clean = _clean_campaign(body, key)
    campaign_id = db.create_campaign(clean["name"], key, clean["facts"], clean["last_call_date"])
    return db.get_campaign(campaign_id)


@app.get("/api/campaigns/{campaign_id}")
async def one_campaign(campaign_id: int) -> dict:
    return _campaign_or_404(campaign_id)


@app.put("/api/campaigns/{campaign_id}")
async def edit_campaign(campaign_id: int, body: dict) -> dict:
    camp = _campaign_or_404(campaign_id)
    clean = _clean_campaign(body, camp["call_type"])
    db.update_campaign(campaign_id, **clean)
    return db.get_campaign(campaign_id)


@app.delete("/api/campaigns/{campaign_id}")
async def remove_campaign(campaign_id: int) -> dict:
    _campaign_or_404(campaign_id)
    if CAMPAIGN.running and CAMPAIGN.settings.campaign_id == campaign_id:
        raise HTTPException(400, "Stop this campaign before deleting it.")
    db.delete_campaign(campaign_id)
    return {"ok": True}


# --- contacts ------------------------------------------------------------------------------------------

@app.get("/api/contacts")
async def contacts(campaign_id: int | None = None) -> list[dict]:
    return db.list_contacts(campaign_id)


def _read_sheet(file: UploadFile, content: bytes, camp: dict) -> dict:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Upload an .xlsx file")
    try:
        sheet = inspect_excel(content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that file as Excel: {exc}")
    if not sheet["rows"]:
        raise HTTPException(400, "No rows found. Check the first row holds the column names, as in the template.")
    # Duplicates are judged within this campaign: the same customer may well be in two campaigns.
    existing = {c["phone"] for c in db.list_contacts(camp["id"]) if c.get("phone")}
    blocked = db.do_not_call_phones()
    rows = check_rows(sheet["rows"], existing)
    for r in rows:
        if r["_issue"] == "ok" and r.get("phone") in blocked:
            r["_issue"] = "do_not_call"
    sheet["rows"] = rows
    sheet["counts"] = {k: sum(1 for r in rows if r["_issue"] == k) for k in ("ok", "no_phone", "duplicate", "on_list", "do_not_call")}
    sheet["missing"] = missing_columns(call_type_named(camp["call_type"]), sheet["columns"], sheet["extras"])
    return sheet


@app.post("/api/campaigns/{campaign_id}/preview")
async def preview_contacts(campaign_id: int, file: UploadFile = File(...)) -> dict:
    """What the sheet contains, before anything is added: rows, columns understood, problems."""
    camp = _campaign_or_404(campaign_id)
    sheet = _read_sheet(file, await file.read(), camp)
    rows = []
    for r in sheet["rows"]:
        row = {k: v for k, v in r.items() if not k.startswith("_")}
        row["row"], row["issue"] = r["_row"], r["_issue"]
        rows.append(row)
    return {"rows": rows[:500], "total": len(rows), "columns": sheet["columns"], "extras": sheet["extras"],
            "missing": sheet["missing"], "counts": sheet["counts"]}


@app.post("/api/campaigns/{campaign_id}/upload")
async def upload_contacts(campaign_id: int, file: UploadFile = File(...), skip_duplicates: bool = True,
                          skip_no_phone: bool = False) -> dict:
    """Add the sheet's rows to this campaign. By default rows whose number is already in the
    campaign (or earlier in the same sheet) are left out, and so are numbers that asked not to be
    called; rows without a valid phone are kept so the team can fix them, unless skip_no_phone."""
    camp = _campaign_or_404(campaign_id)
    sheet = _read_sheet(file, await file.read(), camp)
    keep = []
    for r in sheet["rows"]:
        if r["_issue"] == "do_not_call":
            continue
        if skip_duplicates and r["_issue"] in ("duplicate", "on_list"):
            continue
        if skip_no_phone and r["_issue"] == "no_phone":
            continue
        keep.append({k: v for k, v in r.items() if not k.startswith("_")})
    n = db.insert_contacts(keep, camp["id"], camp["call_type"]) if keep else 0
    return {"imported": n, "skipped": len(sheet["rows"]) - n, "counts": sheet["counts"]}


@app.post("/api/campaigns/{campaign_id}/contacts")
async def add_contact(campaign_id: int, row: dict) -> dict:
    """Add one contact typed on the screen. Fields the call type reads beyond the fixed columns
    (temple, puja date...) go to its extra details."""
    camp = _campaign_or_404(campaign_id)
    ct = call_type_named(camp["call_type"])
    clean = {k: str(row.get(k, "") or "").strip() for k in db.CONTACT_COLUMNS}
    clean["phone"] = normalise_phone(clean["phone"])
    clean["extra"] = {c.key: {"label": c.label, "value": str(row.get(c.key) or "").strip()}
                      for c in ct.columns if c.key not in db.CONTACT_COLUMNS and str(row.get(c.key) or "").strip()}
    if not clean["customer_name"] or not is_valid_phone(clean["phone"]):
        raise HTTPException(400, "Name and a valid 10-digit phone number are required.")
    if clean["phone"] in db.do_not_call_phones():
        raise HTTPException(400, "This number asked not to be called again.")
    db.insert_contacts([clean], camp["id"], camp["call_type"])
    return {"ok": True}


@app.delete("/api/campaigns/{campaign_id}/contacts")
async def delete_contacts(campaign_id: int) -> dict:
    _campaign_or_404(campaign_id)
    if CAMPAIGN.running and CAMPAIGN.settings.campaign_id == campaign_id:
        raise HTTPException(400, "Stop this campaign before clearing its list.")
    db.clear_contacts(campaign_id)
    return {"ok": True}


@app.get("/api/campaigns/{campaign_id}/export")
async def export_results(campaign_id: int) -> Response:
    """Each contact's details with why we called, whether they picked up and what they said, for the team."""
    camp = _campaign_or_404(campaign_id)
    contacts = sorted(db.list_contacts(campaign_id), key=lambda c: c["id"])
    stamp = db.datetime.now(db.IST).strftime("%Y-%m-%d_%H%M")
    safe = "".join(ch if ch.isalnum() else "_" for ch in camp["name"])[:40] or "campaign"
    return Response(
        content=build_results(contacts, db.latest_call_for_contact, call_type_named(camp["call_type"]), camp["name"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={safe}_results_{stamp}.xlsx"},
    )


@app.get("/api/call-types/{key}/template")
async def template(key: str) -> Response:
    ct = call_type_named(key)
    return Response(
        content=build_template(ct),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={ct.key}_template.xlsx"},
    )


@app.get("/api/calls")
async def calls(campaign_id: int | None = None, day_from: str | None = None, day_to: str | None = None) -> list[dict]:
    """day_from / day_to: Indian dates (YYYY-MM-DD), both included. A date range shows up to 1000 calls."""
    try:
        return db.list_calls(limit=1000 if day_from or day_to else 100,
                             campaign_id=campaign_id, day_from=day_from, day_to=day_to)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-10-05.")


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
    if ws.query_params.get("build") != PAGE_BUILD:
        await ws.send_text(json.dumps({"type": "error", "text": STALE_PAGE}))
        await ws.close()
        return
    voice = ws.query_params.get("voice") or GEMINI_VOICE
    if voice not in VOICES:
        voice = GEMINI_VOICE
    model = ws.query_params.get("model") or GEMINI_LIVE_MODEL
    if model not in OFFERED_MODELS:
        model = OFFERED_MODELS[0]
    contact = db.get_contact(contact_id)
    if not contact:
        await ws.send_text(json.dumps({"type": "error", "text": "contact not found"}))
        await ws.close()
        return
    contact = with_language(contact, ws.query_params.get("language") or "")
    contact = await catalog.enrich(contact)   # the order and its products, fresh from the main database
    contact = coupons.attach(contact)   # the coupon an abandoned-cart call may offer, fixed for the whole call

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


class _Pages(StaticFiles):
    """The built dashboard. Its pages are never cached, so a rebuild shows at once; its scripts carry a hash in the name."""
    async def get_response(self, path: str, scope) -> Response:
        resp = await super().get_response(path, scope)
        if not path.startswith("_next/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


# The new dashboard, built by `npm run build` in frontend/. Mounted last: every route above comes first.
# Until it has been built, / shows the original dashboard.
FRONTEND = ROOT / "frontend" / "out"
if (FRONTEND / "index.html").is_file():
    app.mount("/", _Pages(directory=FRONTEND, html=True), name="frontend")
else:
    app.add_api_route("/", classic, methods=["GET"])
