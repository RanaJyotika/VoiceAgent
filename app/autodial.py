"""Automatic calling: one always-on dialer for every campaign marked "call automatically".

A campaign (campaign.py) calls one list after the owner presses Start, and stops when the list is
done. Automatic calling never finishes: contacts that arrive later (a Shopify order, an abandoned
cart, a sheet uploaded into an automatic campaign, a row sent by another system) are rung as they
come, inside the calling hours, most urgent kind of call first.

It is off until the owner switches it on, and stays as switched: on survives a server restart, so
orders that come in overnight are called in the morning. Same safety as a campaign: it switches
itself off after calls fail on our side, and waits for the next day once today's budget is spent.
Both dialers can run at once; they share the phone lines through campaign.DIALING.
"""
import asyncio
import logging
from dataclasses import asdict, fields
from datetime import datetime

from fastapi import APIRouter, HTTPException

from . import db, store_orders
from .call_types import CALL_TYPES
from .campaign import (DIALING, GAP_SECONDS, STOP_ON_ERRORS, Campaign, Settings, _after_call, dial, lines_in_use,
                       past_last_date, within_hours)
from .config import (DIAL_ONLY_MY_NUMBER, GEMINI_VOICE, MAX_CONCURRENT_CALLS, OFFERED_MODELS, PAGE_BUILD, PHONE_NUMBER,
                     STALE_PAGE, VOICES, plivo_missing)
from .db import IST
from .plivo_bridge import PENDING
from .prompts import LANGUAGES

log = logging.getLogger("autodial")
router = APIRouter()

# Most urgent first: an order or booking must be confirmed before it is packed; a cart goes cold in
# hours; an invite can wait. Within one kind, the older campaign first.
PRIORITY = {"order_confirmation": 0, "booking_confirmation": 1, "abandoned_cart": 2, "campaign_invite": 3}

AUTO = Campaign()          # its settings.campaign_id stays 0: it serves every automatic campaign


def _saved() -> dict:
    return db.get_setting("autodial", {}) or {}


def _settings_from(saved: dict) -> Settings:
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in saved.items() if k in known and k != "campaign_id"})


def _campaigns() -> list[dict]:
    """Automatic campaigns still inside their calling days, most urgent first."""
    camps = [c for c in db.auto_campaigns() if not past_last_date(c)]
    return sorted(camps, key=lambda c: (PRIORITY.get(c["call_type"], 9), c["id"]))


def _next_contact(max_attempts: int) -> dict | None:
    orders_id = store_orders.orders_campaign_id()
    for camp in _campaigns():
        if camp["id"] == orders_id:
            # Store orders: as set on the Store orders page (how many a day, which ones; newest first by default).
            contact = store_orders.next_order(camp["id"], max_attempts)
        else:
            # Store carts: the newest first (owner, 12 Oct 2026); a sheet keeps its own order.
            contact = db.next_to_call(max_attempts, camp["id"], pick="newest" if camp.get("source") == "store" else "oldest")
        if contact and contact["id"] not in DIALING:
            return contact
    return None


def _set_reason(text: str) -> None:
    if AUTO.reason != text:
        AUTO.reason = text
        log.info("automatic calling: %s", text)


async def _run() -> None:
    c = AUTO
    s = c.settings
    log.info("automatic calling on: %s", asdict(s))
    try:
        while c.running:
            if c.errors_in_a_row >= STOP_ON_ERRORS:
                c.reason = "switched off: the last calls failed on our side (see the call history); fix that, then switch on again"
                db.set_setting("autodial", {**_saved(), "enabled": False})
                break
            if not within_hours(s):
                _set_reason(f"waiting for calling hours ({s.start_hour}:00 to {s.end_hour}:00 IST)")
                await asyncio.sleep(30)
                continue
            if s.budget_rs:
                spent = db.stats("today")["cost_rs"]
                if spent >= s.budget_rs:
                    _set_reason(f"waiting for tomorrow: today's estimated cost is Rs {spent:.0f}, the budget is Rs {s.budget_rs}")
                    await asyncio.sleep(60)
                    continue
            if lines_in_use() >= s.max_concurrent:
                await asyncio.sleep(1)
                continue
            contact = _next_contact(s.max_attempts)
            if contact is None:
                _set_reason("waiting for new contacts")
                await asyncio.sleep(5)
                continue
            _set_reason("calling")
            try:
                pc = await dial(contact, s)
            except HTTPException as exc:
                if exc.status_code == 400:
                    db.finish_contact_without_call(contact["id"], "skipped: " + exc.detail)
                    log.warning("automatic calling skipped contact %s: %s", contact["id"], exc.detail)
                    continue
                c.errors_in_a_row += 1
                log.warning("automatic calling: could not place a call (%s)", exc.detail)
                await asyncio.sleep(30)
                continue
            except Exception as exc:  # noqa: BLE001
                c.errors_in_a_row += 1
                log.exception("automatic calling: could not place a call")
                await asyncio.sleep(30)
                continue
            c.calls_made += 1
            store_orders.note_dialed(contact)   # a store order's first call counts towards today's limit
            w = asyncio.create_task(_after_call(pc, contact, c))
            c.watchers.add(w)
            w.add_done_callback(c.watchers.discard)
            await asyncio.sleep(GAP_SECONDS)
    except asyncio.CancelledError:
        c.reason = c.reason if c.reason.startswith("switched off") else "switched off by you"
    finally:
        c.running = False
        c.task = None
        log.info("automatic calling off: %s (%s calls this run)", c.reason, c.calls_made)


def _start(s: Settings) -> None:
    c = AUTO
    c.settings = s
    c.running, c.reason, c.calls_made, c.errors_in_a_row = True, "", 0, 0
    c.started_at = db.now_iso()
    c.task = asyncio.create_task(_run())


def resume_after_restart() -> None:
    """Called at server start: switched on before the restart means switched on now."""
    saved = _saved()
    if not saved.get("enabled"):
        return
    missing = plivo_missing()
    if missing:
        AUTO.reason = f"switched on, but the phone line is not ready (missing {', '.join(missing)})"
        log.warning("automatic calling not resumed: %s", AUTO.reason)
        return
    _start(_settings_from(saved))


def snapshot() -> dict:
    c = AUTO
    s = c.settings if c.running else _settings_from(_saved())
    camps = []
    orders_id, q = store_orders.orders_campaign_id(), store_orders.quota()
    for camp in _campaigns():
        counts = db.contact_counts(s.max_attempts, camp["id"])
        to_call = counts["to_call"]
        if camp["id"] == orders_id and q["limit"]:
            # With a daily limit only the day's room is rung (plus retries), not every order waiting.
            to_call = min(q["room"], db.count_new(camp["id"], q["day_start"])) + counts["retry_left"]
        ct = CALL_TYPES.get(camp["call_type"])
        camps.append({"id": camp["id"], "name": camp["name"], "call_type": camp["call_type"],
                      "call_type_name": ct.name if ct else camp["call_type"],
                      "to_call": to_call, "pending": counts["pending"], "retry_due": counts["retry_due"],
                      "total": counts["total"]})
    return {
        "enabled": bool(_saved().get("enabled")), "running": c.running, "reason": c.reason,
        "started_at": c.started_at, "calls_made": c.calls_made, "in_progress": len(PENDING),
        "settings": asdict(s), "campaigns": camps, "order_quota": q,
        "within_hours": within_hours(s), "now_ist": datetime.now(IST).strftime("%H:%M"),
        "dial_only_my_number": DIAL_ONLY_MY_NUMBER, "my_number": PHONE_NUMBER if DIAL_ONLY_MY_NUMBER else "",
        "max_concurrent_allowed": MAX_CONCURRENT_CALLS, "phone_missing": plivo_missing(),
    }


@router.get("/api/auto")
async def auto_status() -> dict:
    return snapshot()


@router.post("/api/auto/start")
async def auto_start(body: dict) -> dict:
    """Switch automatic calling on. From then on every new contact in an automatic campaign is a
    real, paid phone call, without anyone pressing a button."""
    if str(body.get("build") or "") != PAGE_BUILD:
        raise HTTPException(409, STALE_PAGE)
    if AUTO.running:
        raise HTTPException(400, "Automatic calling is already on.")
    missing = plivo_missing()
    if missing:
        raise HTTPException(400, f"Phone line not ready. Missing: {', '.join(missing)}")
    s = Settings(
        voice=body.get("voice") if body.get("voice") in VOICES else GEMINI_VOICE,
        model=body.get("model") if body.get("model") in OFFERED_MODELS else OFFERED_MODELS[0],
        language=body.get("language") if body.get("language") in LANGUAGES else "hinglish",
        max_concurrent=max(1, min(MAX_CONCURRENT_CALLS, int(body.get("max_concurrent") or 1))),
        max_attempts=max(1, min(6, int(body.get("max_attempts") or 3))),
        retry_minutes=max(1, min(24 * 60, int(body.get("retry_minutes") or 30))),
        start_hour=max(0, min(23, int(body.get("start_hour") or 10))),
        end_hour=max(1, min(24, int(body.get("end_hour") or 19))),
        budget_rs=max(0, int(body.get("budget_rs") or 0)),
    )
    if s.end_hour <= s.start_hour:
        raise HTTPException(400, "Calling hours: the end must be after the start.")
    db.set_setting("autodial", {**{k: v for k, v in asdict(s).items() if k != "campaign_id"}, "enabled": True})
    _start(s)
    return snapshot()


@router.post("/api/auto/stop")
async def auto_stop() -> dict:
    """Switch automatic calling off. A call already in progress is left to finish."""
    db.set_setting("autodial", {**_saved(), "enabled": False})
    c = AUTO
    if c.task and not c.task.done():
        c.reason = "switched off by you"
        c.running = False
        c.task.cancel()
    return snapshot()


@router.post("/api/campaigns/{campaign_id}/auto")
async def campaign_auto(campaign_id: int, body: dict) -> dict:
    """Mark a campaign for automatic calling ({"auto": true}) or take it off ({"auto": false})."""
    if not db.get_campaign(campaign_id):
        raise HTTPException(404, "No such campaign.")
    db.set_campaign_auto(campaign_id, bool(body.get("auto")))
    return {"ok": True, "auto": bool(body.get("auto"))}
