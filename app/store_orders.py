"""New store orders, read from the main site's database (VV-Ecom.shopifyorders), become confirmation calls.

The main site copies every Shopify order into its database within seconds, so the orders are read
from there rather than from Shopify: no Shopify keys and no fixed public address for webhooks. Every
ORDERS_CHECK_SECONDS the orders added since the last look are read in the order they arrived
(by _id), and each one old enough (the wait set on the Store page, so a customer can still cancel
or edit) goes into the "Store orders" campaign, by the same rules as a Shopify webhook
(shopify.on_order): cash on delivery only unless set otherwise, never twice, never a number that
asked not to be called. The first look starts from that moment; older orders are never called.

An order cancelled before its call is taken off the list: the main site's richer copy
(shopifyOrders) carries cancelledAt, a few minutes behind.
Products: these copies carry the items' titles and variants, rarely Shopify's product id, so the
catalogue matches them by name (catalog.py); a product id is used whenever one is there.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from . import db, shopify
from .config import MONGODB_DB, MONGODB_URI
from .contacts import is_valid_phone, normalise_phone
from .db import IST

log = logging.getLogger("store_orders")
router = APIRouter()

ORDERS_COLLECTION = "shopifyorders"     # the copy that is filled within seconds of the order
STATUS_COLLECTION = "shopifyOrders"     # the richer copy: cancellations, delivery
ORDERS_CHECK_SECONDS = 30
BATCH = 200

STATE: dict = {"last_check": "", "error": "", "added": 0, "skipped": {}, "since": ""}


def enabled() -> bool:
    return bool(MONGODB_URI)


def _is_cod(o: dict) -> bool:
    """Gokwik and the other gateways mark a paid order "paid"; cash on delivery (gateway empty) stays "pending",
    and a part-paid COD ("Gokwik PPCOD") is "partially_paid": the rest is still paid at the door."""
    gateway = str(o.get("paymentGatewayName") or "").lower()
    return str(o.get("financialStatus") or "").lower() in ("pending", "partially_paid") or "cod" in gateway or "cash" in gateway


def order_row(o: dict) -> dict:
    cust, ship = o.get("customer") or {}, o.get("shippingAddress") or {}
    items = [li for li in o.get("lineItems") or [] if isinstance(li, dict) and li.get("title")]
    names = []
    for li in items:
        title = str(li["title"]).strip()
        variant = str(li.get("variant_title") or "").strip()
        if variant and variant.lower() != "default title":
            title = f"{title} ({variant})"
        qty = int(li.get("quantity") or 1)
        names.append(f"{title} x{qty}" if len(items) > 1 and qty > 1 else title)
    ids = [str(li.get("shopifyProductId") or li.get("product_id") or "").rsplit("/", 1)[-1] for li in items]
    return {
        "order_id": str(o.get("name") or o.get("orderNumber") or ""),
        "customer_name": " ".join(x for x in (str(cust.get("first_name") or "").strip(), str(cust.get("last_name") or "").strip()) if x),
        "phone": normalise_phone(o.get("phone") or ship.get("phone") or ""),
        "product": ", ".join(names), "quantity": str(sum(int(li.get("quantity") or 1) for li in items)) if items else "",
        "amount": shopify._money(o.get("totalPrice")), "payment_mode": "COD" if _is_cod(o) else "Prepaid",
        "address": ", ".join(str(ship[k]).strip() for k in ("address1", "address2", "city", "province", "zip")
                             if str(ship.get(k) or "").strip()),
        "product_ids": ",".join(dict.fromkeys(i for i in ids if i)),
    }


def _skip(why: str) -> str:
    STATE["skipped"][why] = STATE["skipped"].get(why, 0) + 1
    return why


def take(o: dict) -> str:
    """One order from the database: added to the call list, or why not."""
    s = shopify.settings()
    if s["orders"] == "off":
        return _skip("order calls are off")
    gid = f"gid://shopify/Order/{o.get('shopifyOrderId')}" if o.get("shopifyOrderId") else f"db:{o.get('_id')}"
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM shopify_links WHERE shopify_id=? AND kind='order'", (gid,)).fetchone():
            return _skip("already on the list")
    row = order_row(o)
    if s["orders"] == "cod" and row["payment_mode"] != "COD":
        return _skip("prepaid")
    if row["phone"] and row["phone"] in db.do_not_call_phones():
        return _skip("number asked not to be called")
    contact_id = shopify._add_contact("order", row, gid)
    STATE["added"] += 1
    return f"added {row['order_id']} as contact {contact_id}" + ("" if is_valid_phone(row["phone"]) else " (no valid phone)")


# --- which orders automatic calling rings (owner, 13 Oct 2026: "call the first 10, or randomly selected ones") ---
# Settings on the Store orders page: at most order_daily_limit new orders a day (0 = every order), picked by
# order_pick. With a limit, only that day's orders (Indian date) are picked from; the rest are never called.
# Retries of an order already called (no answer, busy, call later) still go ahead, and orders called by hand
# with the Call button do not count.
PICKS = ("newest", "oldest", "random")
_PICKED = "order_picks"     # app setting: {"day": "2026-10-13", "ids": [orders automatic calling first rang that day]}


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def day_start_utc() -> str:
    """Today's Indian midnight, as the UTC time contacts are stamped with."""
    return datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def orders_campaign_id() -> int:
    return int(shopify.settings().get("orders_campaign_id") or 0)


def picked_today() -> list[int]:
    saved = db.get_setting(_PICKED, {}) or {}
    return list(saved.get("ids") or []) if saved.get("day") == _today() else []


def quota() -> dict:
    """The day's limit: limit (0 = none), pick, orders picked today, room left (None without a limit)."""
    s = shopify.settings()
    limit = max(0, int(s.get("order_daily_limit") or 0))
    picked = len(picked_today())
    return {"limit": limit, "pick": s.get("order_pick") if s.get("order_pick") in PICKS else "newest",
            "picked_today": picked, "room": max(0, limit - picked) if limit else None, "day_start": day_start_utc()}


def next_order(campaign_id: int, max_attempts: int) -> dict | None:
    """The next store order for automatic calling: a new one by the pick while today's limit has room, else a retry."""
    q = quota()
    if not q["limit"]:
        return db.next_to_call(max_attempts, campaign_id, pick=q["pick"])
    return db.next_to_call(max_attempts, campaign_id, pick=q["pick"], new_since=q["day_start"], new_ok=q["room"] > 0)


def note_dialed(contact: dict) -> None:
    """Automatic calling has just rung this contact: a store order's first call counts towards today's limit."""
    if contact.get("status") != "pending" or not contact.get("campaign_id") or contact["campaign_id"] != orders_campaign_id():
        return
    ids = picked_today()
    if contact["id"] not in ids:
        db.set_setting(_PICKED, {"day": _today(), "ids": [*ids, contact["id"]]})


def _client():
    from pymongo import AsyncMongoClient
    loop = asyncio.get_running_loop()
    if STATE.get("_loop") is not loop:
        STATE["_loop"], STATE["_client"] = loop, AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000, appname="voice-agent")
    return STATE["_client"]


async def check_once() -> int:
    """Read the orders added since the last look. Returns how many were added to the call list."""
    from bson import ObjectId
    database = _client()[MONGODB_DB]
    orders = database[ORDERS_COLLECTION]
    mark = db.get_setting("store_orders", {}) or {}
    if not mark.get("last_id"):
        newest = await orders.find_one({}, {"_id": 1}, sort=[("_id", -1)])
        mark = {"last_id": str(newest["_id"]) if newest else "", "since": db.now_iso()}
        db.set_setting("store_orders", mark)
        log.info("store orders: starting from now; orders placed before this are never called")
        return 0
    STATE["since"] = mark.get("since", "")
    wait = float(shopify.settings()["order_wait_minutes"])
    ripe = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=wait)   # the database keeps UTC without a zone
    added = 0
    async for o in orders.find({"_id": {"$gt": ObjectId(mark["last_id"])}}).sort("_id", 1).limit(BATCH):
        if o.get("createdAt") and o["createdAt"] > ripe:
            break       # this one and the ones after it are still inside the wait; next look
        result = take(o)
        added += result.startswith("added")
        log.info("store order %s: %s", o.get("name"), result)
        mark["last_id"] = str(o["_id"])
        db.set_setting("store_orders", mark)
    await _drop_cancelled(database)
    _expire_old()
    STATE["last_check"], STATE["error"] = db.now_iso(), ""
    return added


def _expire_old() -> None:
    """An order never rung within the set hours is taken off the list: a confirmation call days later, about
    an order that has likely shipped, helps nobody (automatic calling may have been off, or the list long)."""
    hours = float(shopify.settings()["order_max_hours"])
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db.connect() as conn:
        old = conn.execute("SELECT k.id FROM shopify_links l JOIN contacts k ON k.id = l.contact_id "
                           "WHERE l.kind='order' AND k.status='pending' AND COALESCE(k.attempts, 0)=0 AND k.created_at < ?",
                           (cutoff,)).fetchall()
        for r in old:
            shopify._take_off_list(conn, r["id"], "unclear", f"not called within {hours:g} h of the order; taken off the list")
    if old:
        log.info("store orders: %s order(s) older than %g h taken off the list without a call", len(old), hours)


async def _drop_cancelled(database) -> None:
    """Orders cancelled in the store before their call are taken off the list."""
    with db.connect() as conn:
        waiting = conn.execute(
            "SELECT k.id, k.order_id FROM shopify_links l JOIN contacts k ON k.id = l.contact_id "
            f"WHERE l.kind='order' AND k.status IN ('pending', {','.join('?' for _ in db.RETRY_STATUSES)}) AND k.order_id != ''",
            db.RETRY_STATUSES).fetchall()
    if not waiting:
        return
    by_name = {r["order_id"]: r["id"] for r in waiting}
    async for o in database[STATUS_COLLECTION].find({"name": {"$in": list(by_name)}, "cancelledAt": {"$ne": None}}, {"name": 1}):
        contact_id = by_name.get(o["name"])
        with db.connect() as conn:
            if contact_id and shopify._not_called_yet(conn, contact_id):
                shopify._take_off_list(conn, contact_id, "cancelled", "order cancelled in the store before the call")
                log.info("store order %s cancelled before its call; contact %s taken off the list", o["name"], contact_id)


async def worker() -> None:
    if not enabled():
        return
    while True:
        try:
            await check_once()
        except Exception as exc:  # noqa: BLE001
            STATE["error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("store orders not read: %s", STATE["error"])
        await asyncio.sleep(ORDERS_CHECK_SECONDS)


@router.get("/api/store")
async def store_status() -> dict:
    """The Store page: where orders come from, how the reading goes, and the orders on the call list."""
    s = shopify.settings()
    cid = int(s.get("orders_campaign_id") or 0)
    rows = []
    if cid:
        with db.connect() as conn:
            for r in conn.execute(
                    "SELECT k.id, k.order_id, k.customer_name, k.phone, k.product, k.amount, k.payment_mode, k.status, k.attempts, "
                    "k.last_result, k.created_at, k.next_try_at, c.id AS call_id, c.outcome, c.duration_sec "
                    "FROM contacts k LEFT JOIN calls c ON c.id = (SELECT id FROM calls WHERE contact_id = k.id ORDER BY id DESC LIMIT 1) "
                    "WHERE k.campaign_id=? ORDER BY k.id DESC LIMIT 200", (cid,)):
                rows.append(dict(r))
        counts = db.contact_counts(3, cid)
    else:
        counts = {"total": 0, "to_call": 0, "by_status": {}}
    return {
        "source": f"{MONGODB_DB}.{ORDERS_COLLECTION}" if enabled() else "", "enabled": enabled(),
        "settings": {k: s[k] for k in ("orders", "order_wait_minutes", "order_max_hours", "order_daily_limit", "order_pick")},
        "quota": quota(), "campaign_id": cid or None,
        "last_check": STATE["last_check"], "since": STATE["since"], "error": STATE["error"],
        "added_since_start": STATE["added"], "skipped_since_start": STATE["skipped"],
        "counts": counts, "orders": rows,
    }
