"""Every finished call, copied to MongoDB (MONGODB_CALLS_DB.MONGODB_CALLS_COLLECTION, "ai-calling.calling").

The local SQLite file stays the working copy: calls read and write it with no network in the way. This
copies each call once it is over, with the customer, the campaign, what was said and what it cost, as
one document per call (_id = the call's number here). Calls made before this existed are copied too,
oldest first. A MongoDB that is slow or down never touches a call; the copy simply catches up.

Nothing is written unless the collection already exists. MongoDB makes a database the moment
something is written to it, so a URI for the wrong cluster would otherwise quietly start a second
"ai-calling" there.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from . import db
from .call_types import CALL_TYPES
from .config import MONGODB_CALLS_COLLECTION, MONGODB_CALLS_DB, MONGODB_URI

log = logging.getLogger("mongo_sync")
router = APIRouter()

SETTLE_SECONDS = 60     # the recording is saved a moment after the call ends; copy once it is in
BATCH = 200
TICK_SECONDS = 30

STATE: dict = {"last_run": "", "last_copied": 0, "error": "", "collection": ""}


def enabled() -> bool:
    return bool(MONGODB_URI and MONGODB_CALLS_DB and MONGODB_CALLS_COLLECTION)


def _when(iso: str | None):
    """Stored as real dates in MongoDB, so they sort and filter as dates there."""
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _document(r) -> dict:
    """One call as the document MongoDB keeps."""
    d = dict(r)
    usage = db._json(d.get("usage"), {})
    agent_rs, phone_rs = db.cost_parts(d.get("channel") or "", d.get("duration_sec") or 0, usage)
    model, provider = db.call_agent(usage)
    ct = CALL_TYPES.get(d.get("campaign_call_type") or d.get("k_campaign") or "")
    doc = {
        "_id": d["id"], "call_id": d["id"],
        "channel": d.get("channel"),          # "plivo" = a real phone call, "browser" = a test on the dashboard
        "started_at": _when(d.get("started_at")), "ended_at": _when(d.get("ended_at")),
        "duration_sec": d.get("duration_sec"),
        "outcome": d.get("outcome"), "outcome_details": db._json(d.get("outcome_details"), {}),
        "error": d.get("error"), "hangup_cause": d.get("hangup_cause"), "recording": d.get("recording"),
        "transcript": db._json(d.get("transcript"), []),
        "agent": {"model": model, "provider": provider, "turns": usage.get("turns")},
        "cost_rs": {"agent": round(agent_rs, 2), "phone": round(phone_rs, 2), "total": round(agent_rs + phone_rs, 2),
                    "estimated": not db._has_cost(usage)},
        "copied_at": datetime.now(timezone.utc),
    }
    if d.get("contact_id") and d.get("contact_status") is None:
        doc["contact"] = {"id": d["contact_id"], "deleted": True}   # its campaign or list was cleared since
    elif d.get("contact_id"):
        doc["contact"] = {
            "id": d["contact_id"], "name": d.get("customer_name"), "phone": d.get("phone"), "order_id": d.get("order_id"),
            "product": d.get("product"), "product_ids": d.get("product_ids"), "quantity": d.get("quantity"), "amount": d.get("amount"),
            "payment_mode": d.get("payment_mode"), "address": d.get("address"), "delivery_date": d.get("delivery_date"),
            "language": d.get("language"), "notes": d.get("contact_notes"), "extra": db._json(d.get("extra"), {}),
            "status": d.get("contact_status"), "attempts": d.get("attempts"), "result": d.get("last_result"),
        }
    if d.get("campaign_id"):
        doc["campaign"] = {"id": d["campaign_id"], "name": d.get("campaign_name"), "call_type": d.get("campaign_call_type"),
                           "call_type_name": ct.name if ct else None}
    if d.get("shopify_id"):
        doc["shopify"] = {"kind": d.get("shopify_kind"), "id": d.get("shopify_id")}
    return doc


def _due(limit: int) -> list:
    settled = (datetime.now(timezone.utc) - timedelta(seconds=SETTLE_SECONDS)).isoformat()
    with db.connect() as conn:
        has_links = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shopify_links'").fetchone()
        links = ("LEFT JOIN shopify_links l ON l.contact_id = k.id", "l.kind AS shopify_kind, l.shopify_id")
        if not has_links:
            links = ("", "NULL AS shopify_kind, NULL AS shopify_id")
        return conn.execute(
            "SELECT c.*, k.customer_name, k.phone, k.order_id, k.product, k.product_ids, k.quantity, k.amount, k.payment_mode, k.address, "
            "k.delivery_date, k.language, k.notes AS contact_notes, k.extra, k.status AS contact_status, k.attempts, "
            "k.last_result, k.campaign AS k_campaign, k.campaign_id, g.name AS campaign_name, g.call_type AS campaign_call_type, "
            f"{links[1]} FROM calls c LEFT JOIN contacts k ON k.id = c.contact_id LEFT JOIN campaigns g ON g.id = k.campaign_id "
            f"{links[0]} WHERE c.ended_at IS NOT NULL AND c.ended_at <= ? AND c.mongo_synced_at IS NULL ORDER BY c.id LIMIT ?",
            (settled, limit)).fetchall()


async def _collection(client):
    """The calls collection, or None (and why) when it is not there to be written to."""
    database = client[MONGODB_CALLS_DB]
    names = await database.list_collection_names()
    # Atlas lets a name be typed with a stray space ("calling "): take it as the same collection.
    name = MONGODB_CALLS_COLLECTION if MONGODB_CALLS_COLLECTION in names else next(
        (n for n in names if n.strip() == MONGODB_CALLS_COLLECTION), "")
    if not name:
        return None, (f'no collection "{MONGODB_CALLS_COLLECTION}" in database "{MONGODB_CALLS_DB}" on this cluster '
                      f'(it has: {", ".join(repr(n) for n in names) or "nothing"}); is MONGODB_URI for the right cluster? Nothing written.')
    return database[name], ""


async def copy_once() -> int:
    """Copy the calls that are due. Returns how many were copied."""
    if not enabled():
        return 0
    rows = _due(BATCH)
    STATE["last_run"] = db.now_iso()
    if not rows:
        STATE["error"] = ""
        return 0
    from pymongo import AsyncMongoClient, ReplaceOne   # here: the server starts without it when MongoDB is not set
    client = AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000, appname="voice-agent")
    try:
        coll, why = await _collection(client)
        if coll is None:
            if STATE["error"] != why:
                log.warning("calls not copied to MongoDB: %s", why)
            STATE["error"] = why
            return 0
        STATE["collection"] = f"{MONGODB_CALLS_DB}.{coll.name}"
        await coll.bulk_write([ReplaceOne({"_id": r["id"]}, _document(r), upsert=True) for r in rows], ordered=False)
    finally:
        await client.close()
    now = db.now_iso()
    with db.connect() as conn:
        conn.executemany("UPDATE calls SET mongo_synced_at=? WHERE id=?", [(now, r["id"]) for r in rows])
    STATE["last_copied"], STATE["error"] = len(rows), ""
    log.info("copied %s call(s) to MongoDB %s", len(rows), STATE["collection"])
    return len(rows)


async def worker() -> None:
    if not enabled():
        return
    while True:
        try:
            while await copy_once() == BATCH:
                pass            # a backlog (the first run copies every earlier call): keep going
        except Exception as exc:  # noqa: BLE001
            STATE["error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("calls not copied to MongoDB: %s", STATE["error"])
        await asyncio.sleep(TICK_SECONDS)


@router.get("/api/mongo")
async def mongo_status() -> dict:
    with db.connect() as conn:
        waiting = conn.execute("SELECT COUNT(*) FROM calls WHERE ended_at IS NOT NULL AND mongo_synced_at IS NULL").fetchone()[0]
        copied = conn.execute("SELECT COUNT(*) FROM calls WHERE mongo_synced_at IS NOT NULL").fetchone()[0]
    return {"enabled": enabled(), "target": f"{MONGODB_CALLS_DB}.{MONGODB_CALLS_COLLECTION}", "copied": copied,
            "waiting": waiting, **STATE}
