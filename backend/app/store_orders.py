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

from fastapi import APIRouter, HTTPException

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


# --- orders the owner does not want called (owner, 19 Oct 2026) ---------------------------------------------
# Switches on the Store orders page, any number at once. An order whose payment or store tag matches a switch that is
# on stays on the list as "skipped" (the page shows Not calling, with the reason) and is never rung. The store's tags
# often come after the order (COD confirmed on WhatsApp, GoKwik's risk), so every waiting order is checked again at
# each look, and turning a switch off puts its orders back in the queue. The values are the owner's for now.
SKIP_RULES = {   # a payment kind or one of the store's own tags -> how the page names it
    "Prepaid": "Prepaid", "Part-paid COD": "Part-paid COD", "COD_CONFIRMED": "COD confirmed (WhatsApp)",
    "Low Risk": "Low Risk", "Medium Risk": "Medium Risk", "High Risk": "High Risk", "Bad Address": "Bad Address",
    "Fake Order": "Fake Order", "COD_CANCELLED": "COD cancelled (store)",
}
SKIP_DEFAULT = ("Prepaid", "Part-paid COD", "Low Risk", "COD_CONFIRMED")
_SKIP_SETTING = "store_order_skip"   # app setting: {"on": [keys of SKIP_RULES]}
SKIPPED = "skipped"                  # the contact status of an order a switch keeps from being called


def skip_on() -> list[str]:
    saved = db.get_setting(_SKIP_SETTING, None)
    on = saved.get("on") if isinstance(saved, dict) and isinstance(saved.get("on"), list) else SKIP_DEFAULT
    return [k for k in SKIP_RULES if k in on]


def payment_kind(o: dict) -> str:
    """Prepaid, Part-paid COD (GoKwik PPCOD: "partially_paid") or COD."""
    return "Part-paid COD" if str(o.get("financialStatus") or "").lower() == "partially_paid" else "COD" if _is_cod(o) else "Prepaid"


def skip_reason(payment: str, tags, on: list[str] | None = None) -> str:
    """Why an order is not called ("Part-paid COD, Low Risk"), or "" when no switch that is on matches it."""
    tags = set(tags or [])
    return ", ".join(SKIP_RULES[k] for k in (skip_on() if on is None else on) if k == payment or k in tags)


def _placed(o: dict) -> str:
    """When the order was placed, as the UTC ISO time contacts are stamped with; "" if the record has no time."""
    at = o.get("createdAt")
    return at.replace(tzinfo=timezone.utc).isoformat() if hasattr(at, "replace") and hasattr(at, "isoformat") else ""


async def _date_by_order(database) -> None:
    """Once per server run: waiting store orders take the time they were placed (those listed before 19 Oct 2026 carry
    the time they reached the list). Orders older than the rules allow are then dropped by _expire_old as usual."""
    if STATE.get("dated"):
        return
    with db.connect() as conn:
        waiting = conn.execute(
            "SELECT k.id, k.order_id, k.created_at FROM shopify_links l JOIN contacts k ON k.id = l.contact_id "
            f"WHERE l.kind='order' AND k.order_id != '' AND COALESCE(k.attempts, 0)=0 AND k.status IN ('pending', '{SKIPPED}')"
        ).fetchall()
    by_name = {r["order_id"]: r for r in waiting}
    fixed = 0
    names = list(by_name)
    for i in range(0, len(names), 500):
        async for o in database[ORDERS_COLLECTION].find({"name": {"$in": names[i:i + 500]}}, {"name": 1, "createdAt": 1}):
            r, placed = by_name.get(o.get("name")), _placed(o)
            if r and placed and placed != r["created_at"]:
                with db.connect() as conn:
                    conn.execute("UPDATE contacts SET created_at=? WHERE id=?", (placed, r["id"]))
                fixed += 1
    STATE["dated"] = True
    if fixed:
        log.info("store orders: %s waiting order(s) now dated by when they were placed", fixed)


def take(o: dict, tags=()) -> str:
    """One order from the database: added to the call list, or why not. tags: the store's own, if known yet."""
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
    if (placed := _placed(o)):
        # Dated by when the order was placed, not when it reached the list (owner, 19 Oct 2026: a server off for three
        # days read 1,400 old orders at once, and "today's orders" and the 24 h rule took them all for today's).
        with db.connect() as conn:
            conn.execute("UPDATE contacts SET created_at=? WHERE id=?", (placed, contact_id))
    if (reason := skip_reason(payment_kind(o), tags)):
        # Listed but not called, so a switch turned off later puts it back in the queue.
        with db.connect() as conn:
            shopify._take_off_list(conn, contact_id, SKIPPED, f"not calling: {reason}")
        _skip(f"not calling: {reason}")
        return f"added {row['order_id']} as contact {contact_id}, not called ({reason})"
    return f"added {row['order_id']} as contact {contact_id}" + ("" if is_valid_phone(row["phone"]) else " (no valid phone)")


# --- which orders automatic calling rings (owner, 13 Oct 2026: "call the first 10, or randomly selected ones") ---
# Settings on the Store orders page: at most order_daily_limit new orders a day (0 = every order), picked by
# order_pick. With a limit, only that day's orders (Indian date) are picked from; the rest are never called.
# Retries of an order already called (no answer, busy, call later) still go ahead, and orders called by hand
# with the Call button do not count.
PICKS = ("newest", "oldest", "random")
_PICKED = "order_picks"     # app setting: {"day": "2026-10-13", "ids": [orders automatic calling first rang that day]}
# Owner, 19 Oct 2026: which days' orders the new calls come from, chosen on the page with the number of calls a day
# and which ones first. An order inside the chosen days is not dropped for its age ("Stop trying after" counts only
# outside them), so "Earlier days" really reaches orders of earlier days.
ORDER_DAYS = {"today": "Today's orders", "yesterday": "Yesterday's orders", "last_3": "Last 3 days", "last_7": "Last 7 days",
              "earlier": "Earlier days (not today, up to 7 days back)", "all": "Any waiting order"}


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def day_start_utc() -> str:
    """Today's Indian midnight, as the UTC time contacts are stamped with."""
    return datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def orders_campaign_id() -> int:
    return int(shopify.settings().get("orders_campaign_id") or 0)


# --- batches (owner, 19 Oct 2026: "pehle 100 logo ko phone kiya aur uska cost ye aaya") -----------------------------
# The orders automatic calling picks under the "Calls a day" number form a batch: each day starts one by itself, and
# "Start a new batch" starts another the same day (the next 100). The day's number counts the current batch. Each
# batch's results and cost (every call to its customers, retries too) are on the Store orders page.
BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT,                   -- Indian date it ran on
    size INTEGER,               -- the Calls a day number while it ran (0 = no limit)
    started_at TEXT
);
CREATE TABLE IF NOT EXISTS order_batch_calls (
    batch_id INTEGER,
    contact_id INTEGER,
    picked_at TEXT,             -- when automatic calling first rang it
    PRIMARY KEY (batch_id, contact_id)
);
"""


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(BATCH_SCHEMA)
    saved = db.get_setting(_PICKED, {}) or {}   # today's picks from before batches existed become today's batch
    if saved.get("day") == _today() and saved.get("ids") and not current_batch(create=False):
        b = current_batch()
        with db.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO order_batch_calls (batch_id, contact_id, picked_at) VALUES (?, ?, ?)",
                             [(b["id"], i, db.now_iso()) for i in saved["ids"]])


def _limit() -> int:
    return max(0, int(shopify.settings().get("order_daily_limit") or 0))


def current_batch(create: bool = True) -> dict | None:
    """Today's latest batch, made when there is none yet (create)."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM order_batches WHERE day=? ORDER BY id DESC LIMIT 1", (_today(),)).fetchone()
        if row or not create:
            return dict(row) if row else None
        cur = conn.execute("INSERT INTO order_batches (day, size, started_at) VALUES (?, ?, ?)",
                           (_today(), _limit(), db.now_iso()))
        return {"id": cur.lastrowid, "day": _today(), "size": _limit(), "started_at": db.now_iso()}


def new_batch() -> dict:
    """Start another batch now: the day's number counts afresh from it."""
    with db.connect() as conn:
        cur = conn.execute("INSERT INTO order_batches (day, size, started_at) VALUES (?, ?, ?)",
                           (_today(), _limit(), db.now_iso()))
    log.info("store orders: batch %s started by hand (%s calls)", cur.lastrowid, _limit() or "no limit")
    return current_batch(create=False)


def picked_today() -> list[int]:
    """The orders in the current batch (today's latest)."""
    b = current_batch(create=False)
    if not b:
        return []
    with db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT contact_id FROM order_batch_calls WHERE batch_id=?", (b["id"],))]


def window(days: str) -> tuple[str, str]:
    """(since, before) as UTC ISO times for the chosen days, by Indian date; "" = open on that side."""
    midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    utc = lambda d: (midnight - timedelta(days=d)).astimezone(timezone.utc).isoformat()   # noqa: E731
    return {"today": (utc(0), ""), "yesterday": (utc(1), utc(0)), "last_3": (utc(2), ""), "last_7": (utc(6), ""),
            "earlier": (utc(6), utc(0))}.get(days, ("", ""))


def quota() -> dict:
    """The day's limit: limit (0 = none), pick, orders picked today, room left (None without a limit), and the days the
    new calls come from (days, days_label, since, before). Unset days keep the old rule: today's with a limit, else all."""
    s = shopify.settings()
    limit = max(0, int(s.get("order_daily_limit") or 0))
    picked = len(picked_today())   # in the current batch
    days = s.get("order_days") if s.get("order_days") in ORDER_DAYS else ("today" if limit else "all")
    since, before = window(days)
    return {"limit": limit, "pick": s.get("order_pick") if s.get("order_pick") in PICKS else "newest",
            "picked_today": picked, "room": max(0, limit - picked) if limit else None, "day_start": day_start_utc(),
            "days": days, "days_label": ORDER_DAYS[days], "since": since, "before": before}


def in_window(created_at, q: dict) -> bool:
    created = str(created_at or "")
    return (not q["since"] or created >= q["since"]) and (not q["before"] or created < q["before"])


def next_order(campaign_id: int, max_attempts: int) -> dict | None:
    """The next store order for automatic calling: a new one from the chosen days, by the pick, while today's limit has
    room; else a retry."""
    q = quota()
    return db.next_to_call(max_attempts, campaign_id, pick=q["pick"], new_since=q["since"], new_before=q["before"],
                           new_ok=not q["limit"] or q["room"] > 0)


def note_dialed(contact: dict) -> None:
    """Automatic calling has just rung this contact: a store order's first call joins the current batch."""
    if contact.get("status") != "pending" or not contact.get("campaign_id") or contact["campaign_id"] != orders_campaign_id():
        return
    b = current_batch()
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO order_batch_calls (batch_id, contact_id, picked_at) VALUES (?, ?, ?)",
                     (b["id"], contact["id"], db.now_iso()))
        conn.execute("UPDATE order_batches SET size=? WHERE id=?", (_limit(), b["id"]))   # the number as it is now


# What the team wants to know of a batch's customers, by their contact status now.
_BATCH_GROUPS = {"confirmed": ("confirmed", "changed"), "cancelled": ("cancelled",),
                 "not_reached": ("no_answer", "busy"), "hung_up": ("dropped",),   # hung up: picked up, gone before an answer
                 "call_later": ("call_later",),
                 "waiting": ("pending", "calling", SKIPPED)}


def batches(limit: int = 30) -> list[dict]:
    """The latest batches, newest first: how many were called, what they said, and what all their calls cost."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM order_batches ORDER BY id DESC LIMIT ?", (limit,))]
        out = []
        for b in rows:
            ids = [r[0] for r in conn.execute("SELECT contact_id FROM order_batch_calls WHERE batch_id=?", (b["id"],))]
            if not ids:
                continue   # a batch nothing was rung in (started by hand, or a day with calling off)
            marks = ",".join("?" for _ in ids)
            status = [r[0] for r in conn.execute(f"SELECT status FROM contacts WHERE id IN ({marks})", ids)]
            calls = conn.execute(f"SELECT contact_id, channel, duration_sec, usage FROM calls WHERE contact_id IN ({marks}) "
                                 "AND started_at >= ?", (*ids, b["started_at"])).fetchall()
            cost = sum(db.call_cost(c["channel"] or "", c["duration_sec"] or 0, c["usage"]) for c in calls)
            groups = {k: sum(1 for s in status if s in v) for k, v in _BATCH_GROUPS.items()}
            answered = len({c["contact_id"] for c in calls if (c["duration_sec"] or 0) > 0})
            out.append({**b, "orders": len(ids), "calls": len(calls), "answered": answered, **groups,
                        "other": len(status) - sum(groups.values()),
                        "minutes": round(sum((c["duration_sec"] or 0) for c in calls) / 60, 1),
                        "cost_rs": round(cost, 2), "per_order_rs": round(cost / len(ids), 2),
                        "per_confirmed_rs": round(cost / groups["confirmed"], 2) if groups["confirmed"] else None})
    return out


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
        rich = await database[STATUS_COLLECTION].find_one({"name": o.get("name")}, {"tags": 1}) if o.get("name") else None
        result = take(o, (rich or {}).get("tags") or [])
        added += result.startswith("added")
        log.info("store order %s: %s", o.get("name"), result)
        mark["last_id"] = str(o["_id"])
        db.set_setting("store_orders", mark)
    await _date_by_order(database)   # once per run, before anything goes by an order's date
    _revive_in_window()   # first, so the cancelled and don't-call checks below see what came back
    await _drop_cancelled(database)
    await apply_skips(database)
    _expire_old()
    STATE["last_check"], STATE["error"] = db.now_iso(), ""
    return added


async def apply_skips(database) -> dict:
    """Every store order not called yet, against the switches as they are now: a match is taken off the queue (its tag
    may have come after the order), and a skipped order nothing matches any more goes back to it."""
    with db.connect() as conn:
        waiting = conn.execute(
            "SELECT k.id, k.order_id, k.status, k.payment_mode, k.last_result FROM shopify_links l "
            "JOIN contacts k ON k.id = l.contact_id WHERE l.kind='order' AND k.order_id != '' "
            f"AND k.status IN ('pending', '{SKIPPED}', {','.join('?' for _ in db.RETRY_STATUSES)})", db.RETRY_STATUSES).fetchall()
    if not waiting:
        return {"skipped": 0, "back": 0}
    names = list({r["order_id"] for r in waiting})
    payment, tags = {}, {}
    for i in range(0, len(names), 500):
        chunk = names[i:i + 500]
        async for o in database[ORDERS_COLLECTION].find({"name": {"$in": chunk}}, {"name": 1, "financialStatus": 1,
                                                                                  "paymentGatewayName": 1}):
            payment[o["name"]] = payment_kind(o)
        async for o in database[STATUS_COLLECTION].find({"name": {"$in": chunk}}, {"name": 1, "tags": 1}):
            tags[o["name"]] = o.get("tags") or []
    on, skipped, back = skip_on(), 0, 0
    with db.connect() as conn:
        for r in waiting:
            kind = payment.get(r["order_id"]) or ("COD" if r["payment_mode"] == "COD" else "Prepaid")
            reason = skip_reason(kind, tags.get(r["order_id"]), on)
            if reason and r["status"] != SKIPPED:
                if shopify._not_called_yet(conn, r["id"]):   # never one being rung right now
                    shopify._take_off_list(conn, r["id"], SKIPPED, f"not calling: {reason}")
                    skipped += 1
            elif reason and r["last_result"] != f"not calling: {reason}":
                conn.execute("UPDATE contacts SET last_result=? WHERE id=?", (f"not calling: {reason}", r["id"]))
            elif not reason and r["status"] == SKIPPED:
                conn.execute("UPDATE contacts SET status='pending', last_result=? WHERE id=?",
                             ("back on the list: no switch on the Store orders page matches it now", r["id"]))
                back += 1
    if skipped or back:
        log.info("store orders: %s order(s) set not to be called, %s put back on the list", skipped, back)
    return {"skipped": skipped, "back": back}


def _cutoff() -> tuple[str, float]:
    """Orders made before this are too old to call: the set hours back, or the start of the chosen days if earlier."""
    hours = float(shopify.settings()["order_max_hours"])
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    since = quota()["since"]
    return (min(cutoff, since) if since else cutoff), hours


def _revive_in_window() -> None:
    """Orders dropped for their age that the chosen days now reach go back on the list (owner, 19 Oct 2026)."""
    cutoff, _ = _cutoff()
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE contacts SET status='pending', last_result='back on the list: inside the days chosen for calling' "
            "WHERE id IN (SELECT k.id FROM shopify_links l JOIN contacts k ON k.id = l.contact_id WHERE l.kind='order' "
            "AND k.status='unclear' AND COALESCE(k.attempts, 0)=0 AND k.last_result LIKE 'not called within %' "
            "AND k.created_at >= ?)", (cutoff,))
    if cur.rowcount:
        log.info("store orders: %s order(s) back on the list, inside the days chosen for calling", cur.rowcount)


def _expire_old() -> None:
    """An order never rung within the set hours is taken off the list: a confirmation call days later, about
    an order that has likely shipped, helps nobody (automatic calling may have been off, or the list long).
    Orders inside the days chosen for calling are kept."""
    cutoff, hours = _cutoff()
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
        "settings": {**{k: s[k] for k in ("orders", "order_wait_minutes", "order_max_hours", "order_daily_limit", "order_pick")},
                     "order_days": quota()["days"]},
        "order_days": ORDER_DAYS,
        "quota": quota(), "campaign_id": cid or None,
        "last_check": STATE["last_check"], "since": STATE["since"], "error": STATE["error"],
        "added_since_start": STATE["added"], "skipped_since_start": STATE["skipped"],
        "counts": counts, "orders": rows,
    }


def _skip_view(changed: dict | None = None) -> dict:
    on = skip_on()
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM shopify_links l JOIN contacts k ON k.id = l.contact_id "
                         "WHERE l.kind='order' AND k.status=?", (SKIPPED,)).fetchone()[0]
    return {"rules": [{"key": k, "label": label, "on": k in on} for k, label in SKIP_RULES.items()],
            "not_calling": n, **({"changed": changed} if changed is not None else {})}


@router.get("/api/store/skip")
async def skip_settings() -> dict:
    """The "don't call orders that are..." switches, and how many orders they keep off the queue."""
    return _skip_view()


@router.put("/api/store/skip")
async def save_skip(body: dict) -> dict:
    """Body {"on": [keys]}: the switches that are on (any number). Applied to the waiting orders at once."""
    on = body.get("on")
    if not isinstance(on, list) or any(k not in SKIP_RULES for k in on):
        raise HTTPException(400, "Send the switches to turn on as a list, e.g. {\"on\": [\"Low Risk\", \"COD_CONFIRMED\"]}.")
    db.set_setting(_SKIP_SETTING, {"on": [k for k in SKIP_RULES if k in on]})
    changed = None
    if enabled():
        try:
            changed = await apply_skips(_client()[MONGODB_DB])
        except Exception as exc:  # noqa: BLE001
            log.warning("store orders: switches saved, applied at the next look (%s)", exc)
    return _skip_view(changed)


@router.get("/api/store/batches")
async def batch_report() -> dict:
    """Each batch of automatic calling: orders, answers, results and cost; and the batch running now."""
    b, q = current_batch(create=False), quota()
    return {"batches": batches(), "current": {"id": b["id"] if b else None, "started_at": b["started_at"] if b else "",
                                              "picked": q["picked_today"], "limit": q["limit"], "room": q["room"]}}


@router.post("/api/store/batches/new")
async def start_batch() -> dict:
    """Start the next batch now (e.g. the next 100 the same day): the day's number counts afresh."""
    new_batch()
    return await batch_report()
