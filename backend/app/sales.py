"""What the calls really sold: store orders matched back to the calls before them (owner, 26 Oct 2026).

A campaign's "Converted" only says the customer said yes on the call. Here every store order (VV-Ecom.shopifyorders,
the copy filled within seconds) is checked against the calls to the same phone number in the SALE_WINDOW_DAYS before
it. The orders carry no coupon code and no cart id, so the phone is the only link. Only selling calls count (abandoned
cart, campaign invite...), never an order confirmation call: that one is about an order already placed. Calls made in
test mode (they rang PHONE_NUMBER, not the customer) never count.

Some people order again on their own, so a campaign shows two groups side by side: customers who talked with the agent
and customers who were called but never talked (no answer, busy, hung up at once). What the calls added is the
difference between the two.

Each matched order, and each order an order confirmation call was about, also gets its outcome from the richer copy
(shopifyOrders.dashStatus.bucket: New, In Transit, Delivered, Cancellation, NDR & RTO...), refreshed every
STATUS_EVERY_SECONDS until it is final.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from . import db, store_orders
from .config import MONGODB_DB, PHONE_NUMBER
from .contacts import normalise_phone

log = logging.getLogger("sales")
router = APIRouter()

SALE_WINDOW_DAYS = int(os.getenv("SALE_WINDOW_DAYS", "7"))
CHECK_SECONDS = 120
STATUS_EVERY_SECONDS = 1800
STATUS_DAYS = 60                 # an order older than this keeps the outcome it last had
BATCH = 1000
_MARK = "call_sales"
# Did the customer talk with the agent? Not these, nor a call with no result written.
NO_TALK = ("no_answer", "busy", "dropped")
FINAL = ("Delivered", "Cancellation", "NDR & RTO")
STATE: dict = {"last_check": "", "error": "", "matched": 0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_sales (
    order_name TEXT PRIMARY KEY,   -- the store's order number, "#123456"
    phone TEXT NOT NULL,
    placed_at TEXT NOT NULL,       -- UTC ISO
    amount REAL,
    payment TEXT,                  -- COD, Prepaid, Part-paid COD
    product TEXT,
    contact_id INTEGER,            -- the contact whose call it is matched to
    call_id INTEGER,               -- the last call they talked on, else the last call
    campaign_id INTEGER,
    talked INTEGER NOT NULL,       -- 1: talked with the agent on a call in the window
    hours_after REAL,              -- from that call to the order
    matched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS call_sales_campaign ON call_sales(campaign_id);
CREATE INDEX IF NOT EXISTS call_sales_call ON call_sales(call_id);
CREATE TABLE IF NOT EXISTS order_status (
    order_name TEXT PRIMARY KEY,
    bucket TEXT,                   -- dashStatus.bucket, else shopifyGroupedStatus
    delivery_status TEXT,
    updated_at TEXT NOT NULL
);
"""


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)


def _talked(outcome, seconds) -> bool:
    return bool(outcome) and outcome not in NO_TALK and (seconds or 0) > 0


def _utc(value) -> datetime:
    """A time from either database as an aware UTC datetime: MongoDB keeps UTC without a zone."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc)


def _selling_calls(since: str) -> dict[str, list[dict]]:
    """Every selling call to a real customer since `since`, by phone, oldest first."""
    mine = normalise_phone(PHONE_NUMBER)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.contact_id, c.started_at, c.duration_sec, c.outcome, k.phone, k.campaign_id "
            "FROM calls c JOIN contacts k ON k.id = c.contact_id JOIN campaigns m ON m.id = k.campaign_id "
            "WHERE c.channel = 'plivo' AND COALESCE(c.test_mode, 0) = 0 AND m.call_type != 'order_confirmation' "
            "AND c.started_at >= ? ORDER BY c.started_at", (since,)).fetchall()
    by: dict[str, list[dict]] = {}
    for r in rows:
        phone = normalise_phone(r["phone"])
        if phone and phone != mine:
            by.setdefault(phone, []).append({**dict(r), "at": _utc(r["started_at"])})
    return by


def match(order: dict, calls: list[dict]) -> dict | None:
    """The call an order is put down to: of the calls in the window before it, the last one they talked on, else the
    last one. None when no selling call came before it."""
    placed = _utc(order["createdAt"])
    window = [c for c in calls if placed - timedelta(days=SALE_WINDOW_DAYS) <= c["at"] < placed]
    if not window:
        return None
    talked = [c for c in window if _talked(c["outcome"], c["duration_sec"])]
    call = (talked or window)[-1]
    return {"call": call, "talked": bool(talked), "hours_after": round((placed - call["at"]).total_seconds() / 3600, 1)}


def _amount(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


async def check_once() -> int:
    """Read the orders added since the last look and match them. Returns how many were matched."""
    from bson import ObjectId
    with db.connect() as conn:
        first = conn.execute(
            "SELECT MIN(c.started_at) FROM calls c JOIN contacts k ON k.id = c.contact_id "
            "JOIN campaigns m ON m.id = k.campaign_id WHERE c.channel = 'plivo' AND m.call_type != 'order_confirmation'"
        ).fetchone()[0]
    if not first:
        return 0
    orders = store_orders._client()[MONGODB_DB][store_orders.ORDERS_COLLECTION]
    mark = db.get_setting(_MARK, {}) or {}
    # The first look goes back to the first selling call, so every order since then is checked once.
    after = ObjectId(mark["last_id"]) if mark.get("last_id") else ObjectId.from_datetime(_utc(first))
    calls = _selling_calls((_utc(first) - timedelta(days=1)).isoformat())
    matched = 0
    while True:
        seen = 0
        async for o in orders.find({"_id": {"$gt": after}}).sort("_id", 1).limit(BATCH):
            seen += 1
            after = o["_id"]
            phone = normalise_phone(o.get("phone") or (o.get("shippingAddress") or {}).get("phone") or "")
            name = str(o.get("name") or "")
            hit = match(o, calls.get(phone, [])) if phone and name and o.get("createdAt") else None
            if hit:
                row = store_orders.order_row(o)
                c = hit["call"]
                with db.connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO call_sales (order_name, phone, placed_at, amount, payment, product, contact_id, "
                        "call_id, campaign_id, talked, hours_after, matched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (name, phone, _utc(o["createdAt"]).isoformat(), _amount(o.get("totalPrice")), store_orders.payment_kind(o),
                         row["product"], c["contact_id"], c["id"], c["campaign_id"], int(hit["talked"]), hit["hours_after"],
                         db.now_iso()))
                matched += 1
                log.info("order %s matched to call %s (%s, %.1f h after)", name, c["id"],
                         "talked" if hit["talked"] else "did not talk", hit["hours_after"])
        db.set_setting(_MARK, {"last_id": str(after)})
        if seen < BATCH:
            break
    return matched


async def refresh_status() -> int:
    """The outcome of each matched order and each order an order confirmation call was about, from the richer copy."""
    since = (datetime.now(timezone.utc) - timedelta(days=STATUS_DAYS)).isoformat()
    final = ",".join("?" for _ in FINAL)
    with db.connect() as conn:
        names = {r[0] for r in conn.execute("SELECT order_name FROM call_sales WHERE placed_at >= ?", (since,))}
        names |= {r[0] for r in conn.execute(
            "SELECT DISTINCT k.order_id FROM contacts k JOIN campaigns m ON m.id = k.campaign_id "
            "WHERE m.call_type = 'order_confirmation' AND k.order_id != '' AND k.created_at >= ?", (since,))}
        names -= {r[0] for r in conn.execute(f"SELECT order_name FROM order_status WHERE bucket IN ({final})", FINAL)}
    if not names:
        return 0
    rich = store_orders._client()[MONGODB_DB][store_orders.STATUS_COLLECTION]
    names, updated = sorted(names), 0
    for i in range(0, len(names), 500):
        found = []
        async for o in rich.find({"name": {"$in": names[i:i + 500]}},
                                 {"name": 1, "dashStatus.bucket": 1, "shopifyGroupedStatus": 1, "deliveryStatus": 1}):
            bucket = (o.get("dashStatus") or {}).get("bucket") or o.get("shopifyGroupedStatus") or ""
            found.append((o["name"], bucket, o.get("deliveryStatus") or "", db.now_iso()))
        with db.connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO order_status (order_name, bucket, delivery_status, updated_at) "
                             "VALUES (?, ?, ?, ?)", found)
        updated += len(found)
    return updated


async def worker() -> None:
    if not store_orders.enabled():
        return
    last_status = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            STATE["matched"] += await check_once()
            if loop.time() - last_status >= STATUS_EVERY_SECONDS:
                n = await refresh_status()
                last_status = loop.time()
                log.info("order outcomes read for %s order(s)", n)
            STATE["last_check"], STATE["error"] = db.now_iso(), ""
        except Exception as exc:  # noqa: BLE001
            STATE["error"] = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("sales not read: %s", STATE["error"])
        await asyncio.sleep(CHECK_SECONDS)


# --- what the pages show -------------------------------------------------------------------------------------------

def _outcomes(rows) -> dict:
    """Counts of the store's final outcome over orders (rows with a bucket)."""
    out = {"delivered": 0, "cancelled": 0, "returned": 0, "open": 0}
    for r in rows:
        b = r["bucket"] or ""
        key = "delivered" if b == "Delivered" else "cancelled" if b == "Cancellation" else "returned" if b == "NDR & RTO" else "open"
        out[key] += 1
    return out


def campaign_sales(campaign_id: int, cost_rs: float) -> dict:
    """A selling campaign's real orders: how many, their value and outcome, and the two groups to compare."""
    with db.connect() as conn:
        orders = conn.execute(
            "SELECT s.*, o.bucket FROM call_sales s LEFT JOIN order_status o ON o.order_name = s.order_name "
            "WHERE s.campaign_id = ?", (campaign_id,)).fetchall()
        # Per contact: called at all, and talked on any call. Test-mode calls never reached the customer.
        people = conn.execute(
            "SELECT k.id, MAX(CASE WHEN c.outcome IS NOT NULL AND c.outcome NOT IN (?, ?, ?) AND COALESCE(c.duration_sec, 0) > 0 "
            "THEN 1 ELSE 0 END) AS talked FROM contacts k JOIN calls c ON c.contact_id = k.id "
            "WHERE k.campaign_id = ? AND c.channel = 'plivo' AND COALESCE(c.test_mode, 0) = 0 GROUP BY k.id",
            (*NO_TALK, campaign_id)).fetchall()
    talked_people = {r["id"] for r in people if r["talked"]}
    quiet_people = {r["id"] for r in people if not r["talked"]}
    bought_talked = {r["contact_id"] for r in orders if r["talked"]}
    bought_quiet = {r["contact_id"] for r in orders if not r["talked"]}
    talked_orders = [r for r in orders if r["talked"]]
    return {
        "window_days": SALE_WINDOW_DAYS,
        "orders": len(orders), "sales_rs": round(sum(r["amount"] or 0 for r in orders), 2),
        "talked_orders": len(talked_orders), "talked_sales_rs": round(sum(r["amount"] or 0 for r in talked_orders), 2),
        "cod": sum(1 for r in orders if r["payment"] != "Prepaid"),
        "cost_per_order_rs": round(cost_rs / len(talked_orders), 2) if talked_orders else None,
        "talked": {"customers": len(talked_people), "bought": len(bought_talked & talked_people)},
        "not_talked": {"customers": len(quiet_people), "bought": len(bought_quiet & quiet_people)},
        **_outcomes(talked_orders),
        "last_check": STATE["last_check"], "error": STATE["error"],
    }


# What the customer said on the order confirmation call, in the groups the outcomes are compared by.
CONFIRM_GROUPS = (("confirmed", "Confirmed on the call", ("confirmed", "changed")),
                  ("cancelled", "Cancelled on the call", ("cancelled", "not_ordered")),
                  ("other", "Talked, other result", ()),
                  ("not_reached", "Called, never talked", ()),
                  ("not_called", "Not called", ()))


def confirmation_outcomes(campaign_id: int) -> dict:
    """An order confirmation campaign: what became of the orders, by what happened on their calls."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT k.id, k.order_id, o.bucket, "
            "(SELECT c.outcome FROM calls c WHERE c.contact_id = k.id AND c.outcome IS NOT NULL AND c.outcome NOT IN (?, ?, ?) "
            " AND COALESCE(c.duration_sec, 0) > 0 ORDER BY c.id DESC LIMIT 1) AS said, "
            "(SELECT COUNT(*) FROM calls c WHERE c.contact_id = k.id) AS calls "
            "FROM contacts k LEFT JOIN order_status o ON o.order_name = k.order_id "
            "WHERE k.campaign_id = ? AND k.order_id != ''", (*NO_TALK, campaign_id)).fetchall()
    groups = {key: [] for key, _, _ in CONFIRM_GROUPS}
    for r in rows:
        if r["said"]:
            key = next((k for k, _, outs in CONFIRM_GROUPS if r["said"] in outs), "other")
        else:
            key = "not_reached" if r["calls"] else "not_called"
        groups[key].append(r)
    return {"groups": [{"key": key, "label": label, "orders": len(groups[key]),
                        "known": sum(1 for r in groups[key] if r["bucket"]), **_outcomes(r for r in groups[key] if r["bucket"])}
                       for key, label, _ in CONFIRM_GROUPS],
            "last_check": STATE["last_check"], "error": STATE["error"]}


def call_orders(call_id: int) -> list[dict]:
    """The orders put down to one call, for its panel."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT s.order_name, s.placed_at, s.amount, s.payment, s.product, s.talked, s.hours_after, o.bucket "
            "FROM call_sales s LEFT JOIN order_status o ON o.order_name = s.order_name WHERE s.call_id = ? "
            "ORDER BY s.placed_at", (call_id,))]


def week_sales() -> dict:
    """Overview: orders after a talk with the agent in the last 7 days, and their value."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db.connect() as conn:
        r = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS rs FROM call_sales "
                         "WHERE talked = 1 AND placed_at >= ?", (since,)).fetchone()
    return {"orders": r["n"], "sales_rs": round(r["rs"], 2), "window_days": SALE_WINDOW_DAYS}


@router.get("/api/sales")
async def sales_status() -> dict:
    return {**STATE, "window_days": SALE_WINDOW_DAYS, **week_sales()}


@router.get("/api/sales/confirmations")
async def confirmations(campaign_id: int) -> dict:
    """The Store orders page: what became of the orders, by what happened on their confirmation calls."""
    return confirmation_outcomes(campaign_id)
