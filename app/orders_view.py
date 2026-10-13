"""The Store orders page: every order from the main database for a date range, with where its call stands
and tags that say something about the order and the customer.

Read from the database, never from Shopify: the main site copies each order within seconds
(VV-Ecom.shopifyorders), and its fuller copy (shopifyOrders) adds the store's own tags, the delivery stage
and every earlier order of the same phone number. Our side (contacts and calls, in SQLite) is joined in
by the order number. Writing anything back to Shopify would need the Shopify API; nothing here does.

Tags, worked out per order:
- Payment: COD, Prepaid, Part-paid COD.
- Customer: New customer (no earlier order on this phone), Repeat customer, VIP customer (VIP_ORDERS or
  more earlier orders).
- Return risk: an earlier order on this phone was cancelled or came back (NDR & RTO).
- High value: HIGH_VALUE_RS or more.
- The store's own: GoKwik's High / Medium / Low Risk, Bad Address; COD confirmed / cancelled elsewhere.
- Has a question: the last call ended with something only the team can answer.
"""
import asyncio
import csv
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from . import catalog, db, shopify, store_orders
from .config import MONGODB_DB
from .db import IST

log = logging.getLogger("orders_view")
router = APIRouter()

HIGH_VALUE_RS = 2000
VIP_ORDERS = 3
MAX_DAYS = 62
CACHE_SECONDS = 15
STORE_TAGS = {"High Risk": "critical", "Medium Risk": "warning", "Low Risk": "good", "Bad Address": "critical",
              "COD_CONFIRMED": "good", "COD_CANCELLED": "critical"}
TAG_LABELS = {"COD_CONFIRMED": "COD confirmed (store)", "COD_CANCELLED": "COD cancelled (store)"}
PROJECTION = {"name": 1, "shopifyOrderId": 1, "createdAt": 1, "customer": 1, "phone": 1, "email": 1, "totalPrice": 1,
              "financialStatus": 1, "paymentGatewayName": 1, "lineItems": 1, "shippingAddress": 1}

# What the page calls each state of a call, and its colour.
STATES = {
    "yet_to_call": ("Yet to call", "warning"), "calling": ("Calling now", "accent"), "confirmed": ("Confirmed", "good"),
    "cancelled": ("Cancelled", "critical"), "no_pick": ("Didn't pick", "serious"), "busy": ("Busy", "serious"),
    "scheduled": ("Scheduled", "accent"), "review": ("Needs a look", "serious"), "not_calling": ("Not calling", ""),
}
TABS = ["all", "yet_to_call", "calling", "confirmed", "cancelled", "no_pick", "busy", "scheduled", "review", "not_calling"]
# The page's cards; each is one or more of the states above. "Called" = someone answered and the call has a result.
VIEWS = {"left": ("yet_to_call", "calling"), "called": ("confirmed", "cancelled", "review"), "no_pick": ("no_pick",),
         "busy": ("busy",), "scheduled": ("scheduled",),
         "to_call": ("yet_to_call", "calling", "no_pick", "busy", "scheduled"), "confirmed": ("confirmed",),
         "cancelled": ("cancelled",), "attention": ("review",), "all": tuple(TABS[1:])}

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_name TEXT,            -- '#71432'
    text TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS order_notes_name ON order_notes (order_name);
"""

_CACHE: dict = {}


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)


def _db():
    return store_orders._client()[MONGODB_DB]


def _last10(phone) -> str:
    return re.sub(r"\D", "", str(phone or ""))[-10:]


def _range(day_from: str, day_to: str) -> tuple[datetime, datetime]:
    """Indian dates (both included) as the UTC times without a zone that the database keeps."""
    try:
        start = datetime.strptime(day_from, "%Y-%m-%d").replace(tzinfo=IST)
        end = datetime.strptime(day_to, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-10-12.")
    if end <= start or (end - start).days > MAX_DAYS:
        raise HTTPException(400, f"Pick a range of 1 to {MAX_DAYS} days.")
    return (start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None))


async def _cached(key, seconds, make):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < seconds:
        return hit[1]
    value = await make()
    _CACHE[key] = (time.monotonic(), value)
    return value


async def _orders(start: datetime, end: datetime) -> list[dict]:
    async def make():
        cursor = _db()[store_orders.ORDERS_COLLECTION].find({"createdAt": {"$gte": start, "$lt": end}}, PROJECTION)
        return [o async for o in cursor.sort("createdAt", -1).limit(25000)]
    return await _cached(("orders", start, end), CACHE_SECONDS, make)


async def _store_side(names: list[str]) -> dict[str, dict]:
    """The fuller copy of each order: the store's tags, delivery stage, cancellation."""
    async def make():
        out = {}
        for i in range(0, len(names), 2000):
            async for o in _db()[store_orders.STATUS_COLLECTION].find(
                    {"name": {"$in": names[i:i + 2000]}},
                    {"name": 1, "tags": 1, "shopifyGroupedStatus": 1, "deliveryStatus": 1, "courier": 1, "trackingNumber": 1,
                     "cancelledAt": 1, "fulfillmentStatus": 1, "deliveredAt": 1, "shopifyCreatedAt": 1}):
                out[o["name"]] = o
        return out
    return await _cached(("store", hash(tuple(names))), 60, make)


async def _history(phones: list[str]) -> dict[str, list]:
    """Every order of these phone numbers (when, and whether it went wrong), for the customer tags."""
    async def make():
        out: dict[str, list] = {}
        for i in range(0, len(phones), 2000):
            async for o in _db()[store_orders.STATUS_COLLECTION].find(
                    {"shippingAddress.phone": {"$in": phones[i:i + 2000]}},
                    {"shippingAddress.phone": 1, "shopifyCreatedAt": 1, "cancelledAt": 1, "shopifyGroupedStatus": 1, "name": 1}):
                p = _last10((o.get("shippingAddress") or {}).get("phone"))
                bad = bool(o.get("cancelledAt")) or o.get("shopifyGroupedStatus") == "NDR & RTO"
                out.setdefault(p, []).append((o.get("shopifyCreatedAt"), bad, o.get("name")))
        return out
    return await _cached(("history", hash(tuple(phones))), 60, make)


def _our_side(names: list[str]) -> tuple[dict[str, dict], dict[int, dict]]:
    """Our contact for each order (the latest, if it was listed twice) and that contact's latest call."""
    contacts: dict[str, dict] = {}
    with db.connect() as conn:
        for i in range(0, len(names), 900):
            chunk = names[i:i + 900]
            for r in conn.execute(f"SELECT * FROM contacts WHERE order_id IN ({','.join('?' for _ in chunk)}) ORDER BY id", chunk):
                contacts[r["order_id"]] = dict(r)
        ids = [c["id"] for c in contacts.values()]
        calls: dict[int, dict] = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in conn.execute(
                    f"SELECT id, contact_id, started_at, duration_sec, outcome, outcome_details FROM calls "
                    f"WHERE contact_id IN ({','.join('?' for _ in chunk)}) ORDER BY id", chunk):
                calls[r["contact_id"]] = dict(r)
    return contacts, calls


def _state(contact: dict | None, cod: bool) -> str:
    if not contact:
        return "not_calling"
    status, tries = contact.get("status") or "pending", contact.get("attempts") or 0
    if status == "pending":
        return "yet_to_call"
    if status == "calling":
        return "calling"
    if status in ("confirmed", "changed"):
        return "confirmed"
    if status == "cancelled":
        return "cancelled"
    if status == "busy":
        return "busy"
    if status in ("no_answer", "dropped"):
        return "no_pick"
    if status == "call_later":
        return "scheduled"
    return "review"


def _payment(o: dict) -> str:
    fin = str(o.get("financialStatus") or "").lower()
    return "Part-paid COD" if fin == "partially_paid" else "COD" if store_orders._is_cod(o) else "Prepaid"


def _row(o: dict, store: dict, history: dict, contacts: dict, calls: dict) -> dict:
    name = str(o.get("name") or "")
    contact = contacts.get(name)
    call = calls.get(contact["id"]) if contact else None
    payment = _payment(o)
    amount = shopify._money(o.get("totalPrice"))
    phone = _last10(o.get("phone"))
    tags = [{"label": payment, "tone": "" if payment == "Prepaid" else "warning"}]
    earlier = [h for h in history.get(phone, []) if h[2] != name and (not o.get("createdAt") or not h[0] or h[0] < o["createdAt"])]
    if phone:
        n = len(earlier)
        tags.append({"label": "New customer", "tone": "accent"} if n == 0 else
                     {"label": "VIP customer", "tone": "accent"} if n >= VIP_ORDERS else {"label": "Repeat customer", "tone": "good"})
        if any(h[1] for h in earlier):
            tags.append({"label": "Return risk", "tone": "critical"})
    try:
        if float(amount or 0) >= HIGH_VALUE_RS:
            tags.append({"label": "High value", "tone": "good"})
    except ValueError:
        pass
    s = store.get(name) or {}
    for t in s.get("tags") or []:
        if t in STORE_TAGS:
            tags.append({"label": TAG_LABELS.get(t, t), "tone": STORE_TAGS[t]})
    if call and call.get("outcome") == "needs_help":
        tags.append({"label": "Has a question", "tone": "serious"})
    items = []
    for li in o.get("lineItems") or []:
        if not isinstance(li, dict) or not li.get("title"):
            continue
        p = catalog.STATE["by_title"].get(catalog._key(li["title"]))
        variant = str(li.get("variant_title") or "").strip()
        items.append({"title": li["title"], "variant": "" if variant.lower() == "default title" else variant,
                      "qty": int(li.get("quantity") or 1), "image": (p or {}).get("image", "")})
    state = _state(contact, payment != "Prepaid")
    label, tone = STATES[state]
    if state == "not_calling":
        label = "No call · prepaid" if payment == "Prepaid" and shopify.settings()["orders"] == "cod" else "Not on call list"
    cust = o.get("customer") or {}
    return {
        "name": name, "created_at": o["createdAt"].replace(tzinfo=timezone.utc).isoformat() if o.get("createdAt") else "",
        "customer": " ".join(x for x in (str(cust.get("first_name") or "").strip(), str(cust.get("last_name") or "").strip()) if x),
        "phone": phone, "items": items, "amount": amount, "payment": payment, "tags": tags,
        "state": state, "state_label": label, "state_tone": tone,
        "contact_id": contact["id"] if contact else None, "attempts": (contact or {}).get("attempts") or 0,
        "result": (contact or {}).get("last_result") or "", "next_try_at": (contact or {}).get("next_try_at"),
        "last_call": {"id": call["id"], "at": call["started_at"], "seconds": round(call.get("duration_sec") or 0)} if call else None,
        "delivery": s.get("shopifyGroupedStatus") or "",
    }


def _in_view(r: dict, tab: str) -> bool:
    """A page view (left, called...) or a single state (yet_to_call...). A name this server does not know shows
    everything rather than nothing: a page newer than the server once asked for "left" and got an empty list."""
    if tab not in VIEWS and tab not in TABS:
        return True
    return tab == "all" or r["state"] == tab or r["state"] in VIEWS.get(tab, ())


def _matches(r: dict, q: str, tag: str, payment: str) -> bool:
    if payment and r["payment"] != payment:
        return False
    if tag and not any(t["label"] == tag for t in r["tags"]):
        return False
    if q:
        hay = " ".join([r["name"], r["customer"], r["phone"], *[i["title"] for i in r["items"]]]).lower()
        digits = re.sub(r"\D", "", q)
        return q.lower() in hay or (len(digits) >= 4 and digits in r["phone"] + re.sub(r"\D", "", r["name"]))
    return True


async def _rows(day_from: str, day_to: str) -> list[dict]:
    if not store_orders.enabled():
        raise HTTPException(400, "The main database is not set up (MONGODB_URI in .env).")
    start, end = _range(day_from, day_to)
    orders = await _orders(start, end)
    names = [str(o.get("name") or "") for o in orders if o.get("name")]
    phones = sorted({_last10(o.get("phone")) for o in orders if _last10(o.get("phone"))})
    store, history = await asyncio.gather(_store_side(names), _history(phones))
    contacts, calls = _our_side(names)
    return [_row(o, store, history, contacts, calls) for o in orders]


@router.get("/api/orders")
async def orders(day_from: str, day_to: str, tab: str = "all", q: str = "", tag: str = "", payment: str = "",
                 page: int = 1, per_page: int = 25) -> dict:
    rows = await _rows(day_from, day_to)
    counts = {t: 0 for t in TABS}
    counts["all"] = len(rows)
    for r in rows:
        counts[r["state"]] += 1
    for v, states in VIEWS.items():
        if v not in TABS:
            counts[v] = sum(counts[st] for st in states)
    tags = sorted({t["label"] for r in rows for t in r["tags"]})
    shown = [r for r in rows if _in_view(r, tab) and _matches(r, q.strip(), tag, payment)]
    per_page = max(5, min(100, per_page))
    pages = max(1, -(-len(shown) // per_page))
    page = max(1, min(pages, page))
    return {"counts": counts, "tags": tags, "total": len(shown), "page": page, "pages": pages,
            "rows": shown[(page - 1) * per_page: page * per_page],
            "settings": {"orders": shopify.settings()["orders"]}}


@router.get("/api/orders/export")
async def export(day_from: str, day_to: str, tab: str = "all", q: str = "", tag: str = "", payment: str = "") -> Response:
    rows = [r for r in await _rows(day_from, day_to) if _in_view(r, tab) and _matches(r, q.strip(), tag, payment)]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Order", "Placed", "Customer", "Phone", "Products", "Amount", "Payment", "Tags", "Call status", "Tries",
                "Result", "Delivery"])
    for r in rows:
        placed = datetime.fromisoformat(r["created_at"]).astimezone(IST).strftime("%d %b %Y %H:%M") if r["created_at"] else ""
        w.writerow([r["name"], placed, r["customer"], r["phone"],
                    "; ".join(f"{i['title']}{' (' + i['variant'] + ')' if i['variant'] else ''} x{i['qty']}" for i in r["items"]),
                    r["amount"], r["payment"], ", ".join(t["label"] for t in r["tags"]), r["state_label"], r["attempts"],
                    r["result"], r["delivery"]])
    return Response("﻿" + out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=store_orders_{day_from}_to_{day_to}.csv"})


async def _find(name: str) -> dict:
    name = "#" + re.sub(r"\D", "", name)
    o = await _db()[store_orders.ORDERS_COLLECTION].find_one({"name": name}, PROJECTION, sort=[("_id", -1)])
    if not o:
        raise HTTPException(404, f"No order {name} in the database.")
    return o


@router.get("/api/order")
async def order(name: str) -> dict:
    """One order for the side panel: everything on the row, plus address, email, delivery, every call and the notes."""
    o = await _find(name)
    store = {}
    full = await _db()[store_orders.STATUS_COLLECTION].find_one({"name": o["name"]})
    if full:
        store[o["name"]] = full
    history = await _history([_last10(o.get("phone"))]) if _last10(o.get("phone")) else {}
    contacts, calls = _our_side([o["name"]])
    row = _row(o, store, history, contacts, calls)
    ship = o.get("shippingAddress") or {}
    with db.connect() as conn:
        call_rows = [dict(r) for r in conn.execute(
            "SELECT c.id, c.started_at, c.duration_sec, c.outcome, c.error, c.channel FROM calls c JOIN contacts k ON k.id = c.contact_id "
            "WHERE k.order_id = ? ORDER BY c.id DESC", (o["name"],))]
        notes = [dict(r) for r in conn.execute("SELECT text, created_at FROM order_notes WHERE order_name=? ORDER BY id DESC", (o["name"],))]
    full = full or {}
    return {**row, "email": o.get("email") or "",
            "address": ", ".join(str(ship[k]).strip() for k in ("address1", "address2", "city", "province", "zip")
                                 if str(ship.get(k) or "").strip()),
            "store_status": {"delivery": full.get("shopifyGroupedStatus") or "", "courier": full.get("courier") or "",
                             "tracking": full.get("trackingNumber") or "", "fulfillment": full.get("fulfillmentStatus") or ""},
            "earlier_orders": max(0, len(history.get(_last10(o.get("phone")), [])) - (1 if full else 0)),
            "calls": calls_out(call_rows), "notes": notes}


def calls_out(rows: list[dict]) -> list[dict]:
    return [{"id": r["id"], "at": r["started_at"], "seconds": round(r.get("duration_sec") or 0), "outcome": r.get("outcome") or "",
             "error": r.get("error") or "", "channel": r.get("channel") or ""} for r in rows]


async def ensure_contact(name: str) -> dict:
    """The order's contact on the Store orders list, made now if the order was never listed (a prepaid order, or
    one placed before the reading started): the owner chose to call it."""
    o = await _find(name)
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM contacts WHERE order_id=? ORDER BY id DESC LIMIT 1", (o["name"],)).fetchone()
    if r:
        return dict(r)
    row = store_orders.order_row(o)
    if row["phone"] and row["phone"] in db.do_not_call_phones():
        raise HTTPException(400, "This number asked not to be called again.")
    gid = f"gid://shopify/Order/{o.get('shopifyOrderId')}" if o.get("shopifyOrderId") else f"db:{o.get('_id')}"
    contact_id = shopify._add_contact("order", row, gid)
    return db.get_contact(contact_id)


@router.post("/api/order/contact")
async def order_contact(body: dict) -> dict:
    return await ensure_contact(str(body.get("name") or ""))


@router.post("/api/order/schedule")
async def order_schedule(body: dict) -> dict:
    """Call this order at a set time (Indian time, "2026-10-12T17:30"), by automatic calling."""
    contact = await ensure_contact(str(body.get("name") or ""))
    try:
        when = datetime.fromisoformat(str(body.get("at") or "")).replace(tzinfo=IST)
    except ValueError:
        raise HTTPException(400, "Pick a date and time.")
    with db.connect() as conn:
        conn.execute("UPDATE contacts SET status='call_later', next_try_at=?, last_result=? WHERE id=?",
                     (when.astimezone(timezone.utc).isoformat(), f"scheduled for {when:%d %b %H:%M}", contact["id"]))
    _CACHE.clear()
    return {"ok": True, "contact_id": contact["id"]}


@router.post("/api/order/note")
async def order_note(body: dict) -> dict:
    name = "#" + re.sub(r"\D", "", str(body.get("name") or ""))
    text = str(body.get("text") or "").strip()[:1000]
    if name == "#" or not text:
        raise HTTPException(400, "Write a note first.")
    with db.connect() as conn:
        conn.execute("INSERT INTO order_notes (order_name, text, created_at) VALUES (?, ?, ?)", (name, text, db.now_iso()))
    return {"ok": True}
