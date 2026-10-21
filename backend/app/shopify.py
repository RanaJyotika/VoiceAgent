"""Shopify: new orders and abandoned carts become contacts on their own, and what the customer said
goes back onto the order.

- An order (webhook orders/create) is added to the "Shopify orders" campaign for an order
  confirmation call; by default only cash-on-delivery orders, as prepaid ones need no check.
- A checkout (checkouts/create, checkouts/update) is held here. When it has sat unfinished for the
  wait set on the dashboard (an hour by default), it is added to the "Shopify abandoned carts"
  campaign. An order from the same checkout or the same phone number before then means no call,
  and a cart call not yet made when its order arrives is taken off the list.
- An order cancelled in Shopify (orders/cancelled) before its call is taken off the list.
- Once an order's call has a result, the order gets tags (voice-call, voice-confirmed...) and a
  line in its notes, so the team sees it in Shopify.
- A cash-on-delivery customer who chose to pay online on the call (the WhatsApp coupon): the order's
  payment is set to Prepaid here, and in Shopify it gets the prepaid tags and a note (mark_prepaid).
  This works for any order the call knows the number of, not only the ones that came by webhook.

Both campaigns are made the first time they are needed and are marked for automatic calling;
nothing is rung until automatic calling (autodial.py) is switched on. Without the .env settings the
webhook refuses everything and nothing is sent to Shopify.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import db
from .config import (PUBLIC_BASE_URL, SHOPIFY_ADMIN_TOKEN, SHOPIFY_API_VERSION, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET,
                     SHOPIFY_PREPAID_TAGS_ADD, SHOPIFY_PREPAID_TAGS_REMOVE, SHOPIFY_STORE, SHOPIFY_WEBHOOK_SECRET)
from .contacts import DETAIL_LABELS, RESPONSES, is_valid_phone, normalise_phone

log = logging.getLogger("shopify")
router = APIRouter()

WEBHOOK_PATH = "/webhooks/shopify"
TOPICS = {"ORDERS_CREATE": "orders/create", "ORDERS_CANCELLED": "orders/cancelled",
          "CHECKOUTS_CREATE": "checkouts/create", "CHECKOUTS_UPDATE": "checkouts/update"}
CAMPAIGNS = {"order": ("Store orders", "order_confirmation"), "cart": ("Store abandoned carts", "abandoned_cart")}
# Dashboard settings, with what they are before the owner changes anything.
DEFAULTS = {
    "orders": "cod",              # which new orders get a confirmation call: "cod", "all" or "off"
    "carts": True,                # call abandoned carts
    "cart_wait_minutes": 60,      # how long a checkout sits unfinished before it counts as abandoned
    "cart_max_hours": 24,         # older than this when found: too late to call
    "writeback": True,            # tag and note the order in Shopify with the call's result
    "order_wait_minutes": 5,      # an order is called this long after it was placed (time to cancel or edit it)
    "order_max_hours": 24,        # an order not called by then is taken off the list (it has likely shipped)
    # Owner, 13 Oct 2026: call only some orders. At most this many new orders a day by automatic calling, from that
    # day's orders (0 = every order), picked "newest" first, "oldest" first or at "random" (store_orders.next_order).
    "order_daily_limit": 0,
    "order_pick": "newest",
    # Owner, 19 Oct 2026: which orders' days automatic calling picks new orders from (store_orders.ORDER_DAYS):
    # "today", "yesterday", "last_3", "last_7", "earlier" or "all". "" = as before: today's with a limit, else all.
    "order_days": "",
}
SYNC_TRIES = 5                    # write-back attempts for one result before it is left for a person
TICK_SECONDS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS shopify_checkouts (
    token TEXT PRIMARY KEY,     -- Shopify's checkout token
    phone TEXT,
    row TEXT,                   -- JSON: the contact this cart becomes
    first_seen TEXT,
    last_update TEXT,           -- our clock, when Shopify last told us about it
    completed_at TEXT,          -- it became an order (from Shopify, or an order with its token or phone)
    contact_id INTEGER,         -- the contact made from it
    skipped TEXT                -- why it was never turned into a call
);
CREATE TABLE IF NOT EXISTS shopify_links (
    contact_id INTEGER PRIMARY KEY,
    kind TEXT,                  -- 'order' or 'cart'
    shopify_id TEXT,            -- an order's admin_graphql_api_id, or a cart's checkout token
    synced_status TEXT,         -- the contact status last written to Shopify
    synced_at TEXT,
    sync_error TEXT,
    sync_tries INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS shopify_links_id ON shopify_links (shopify_id);
-- Changes to one order that a call asked for (the payment switched to prepaid), sent by the worker.
CREATE TABLE IF NOT EXISTS shopify_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER,
    call_id INTEGER,
    order_name TEXT,            -- the order number as the store shows it ("#1234"): found in Shopify by it
    shopify_id TEXT,            -- the order's id, once known
    tags_add TEXT,              -- JSON lists
    tags_remove TEXT,
    note TEXT,                  -- the line added to the order's notes ("" = none)
    status TEXT DEFAULT 'pending',   -- pending, done, failed
    tries INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS shopify_events (
    webhook_id TEXT PRIMARY KEY,
    topic TEXT,
    received_at TEXT,
    result TEXT                 -- what was done with it, for the dashboard
);
"""


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)


def settings() -> dict:
    return {**DEFAULTS, **(db.get_setting("shopify", {}) or {})}


def _save(**changes) -> None:
    db.set_setting("shopify", {**(db.get_setting("shopify", {}) or {}), **changes})


def admin_ready() -> bool:
    return bool(SHOPIFY_STORE and (SHOPIFY_ADMIN_TOKEN or (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET)))


def _utc(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


# --- turning Shopify's records into contact rows ------------------------------------------------------

def _first_phone(*candidates) -> str:
    for p in candidates:
        phone = normalise_phone(p)
        if is_valid_phone(phone):
            return phone
    return ""


def _name(*people) -> str:
    for p in people:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip() or " ".join(
            x for x in (str(p.get("first_name") or "").strip(), str(p.get("last_name") or "").strip()) if x)
        if name:
            return name
    return ""


def _address(a: dict | None) -> str:
    a = a or {}
    if not str(a.get("address1") or "").strip():
        return ""
    parts = [a.get("address1"), a.get("address2"), a.get("city"), a.get("province"), a.get("zip")]
    return ", ".join(str(p).strip() for p in parts if str(p or "").strip())


def _money(value) -> str:
    text = str(value or "").strip()
    return text[:-3] if text.endswith(".00") else text


def _product_ids(line_items: list) -> str:
    """Shopify's product id of each item, comma separated: how the catalogue finds the exact product."""
    ids = [str(li.get("product_id")) for li in line_items or [] if isinstance(li, dict) and li.get("product_id")]
    return ",".join(dict.fromkeys(ids))


def _items(line_items: list) -> tuple[str, str]:
    """(what was bought, quantity) as the call types read them."""
    items = [li for li in line_items or [] if isinstance(li, dict)]
    names = []
    for li in items:
        title = str(li.get("title") or li.get("presentment_title") or li.get("name") or "").strip()
        variant = str(li.get("variant_title") or "").strip()
        if variant and variant.lower() != "default title":
            title = f"{title} ({variant})"
        qty = int(li.get("quantity") or 1)
        names.append(f"{title} x{qty}" if len(items) > 1 and qty > 1 else title)
    total = sum(int(li.get("quantity") or 1) for li in items)
    return ", ".join(n for n in names if n), (str(total) if items else "")


def is_cod(order: dict) -> bool:
    gateways = " ".join(str(g) for g in order.get("payment_gateway_names") or []).lower()
    return "cash on delivery" in gateways or "cod" in gateways.replace("(", " ").replace(")", " ").split()


def order_row(order: dict) -> dict:
    ship, bill, cust = order.get("shipping_address") or {}, order.get("billing_address") or {}, order.get("customer") or {}
    product, quantity = _items(order.get("line_items"))
    return {
        "order_id": str(order.get("name") or order.get("order_number") or order.get("id") or ""),
        "customer_name": _name(ship, cust, bill),
        "phone": _first_phone(ship.get("phone"), order.get("phone"), cust.get("phone"), bill.get("phone"),
                              (cust.get("default_address") or {}).get("phone")),
        "product": product, "quantity": quantity, "amount": _money(order.get("total_price")),
        "payment_mode": "COD" if is_cod(order) else "Prepaid",
        "address": _address(ship) or _address(bill), "notes": str(order.get("note") or "").strip()[:300],
        "product_ids": _product_ids(order.get("line_items")),
    }


def cart_row(checkout: dict) -> dict:
    ship, bill, cust = checkout.get("shipping_address") or {}, checkout.get("billing_address") or {}, checkout.get("customer") or {}
    product, quantity = _items(checkout.get("line_items"))
    row = {
        "customer_name": _name(ship, cust, bill),
        "phone": _first_phone(checkout.get("phone"), ship.get("phone"), bill.get("phone"), cust.get("phone"),
                              (cust.get("default_address") or {}).get("phone")),
        "product": product, "quantity": quantity, "amount": _money(checkout.get("total_price")),
        "address": _address(ship) or _address(bill), "product_ids": _product_ids(checkout.get("line_items")),
    }
    # Owner, 19 Oct 2026: the coupon message links the customer's own cart (messaging.link_for); never read out on the call.
    url = str(checkout.get("abandoned_checkout_url") or "").strip()
    if url.lower().startswith(("http://", "https://")):
        row["extra"] = {"cart_url": {"label": "Cart link", "value": url}}
    return row


# --- campaigns and links ----------------------------------------------------------------------------

def _campaign_for(kind: str) -> int:
    """The Shopify campaign for orders or carts, made (and marked automatic) the first time."""
    key = f"{kind}s_campaign_id"
    cid = int(settings().get(key) or 0)
    if cid and db.get_campaign(cid):
        return cid
    name, call_type = CAMPAIGNS[kind]
    cid = db.create_campaign(name, call_type, {}, source="store")
    db.set_campaign_auto(cid, True)
    _save(**{key: cid})
    log.info("made campaign %s (%s) for Shopify %ss", cid, name, kind)
    return cid


def _add_contact(kind: str, row: dict, shopify_id: str) -> int:
    name, call_type = CAMPAIGNS[kind]
    contact_id = db.insert_contact(row, _campaign_for(kind), call_type)
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO shopify_links (contact_id, kind, shopify_id) VALUES (?, ?, ?)",
                     (contact_id, kind, shopify_id))
    return contact_id


def _not_called_yet(conn, contact_id: int) -> bool:
    row = conn.execute("SELECT status FROM contacts WHERE id=?", (contact_id,)).fetchone()
    return bool(row) and row["status"] in ("pending", *db.RETRY_STATUSES)


def _take_off_list(conn, contact_id: int, status: str, note: str) -> None:
    """A contact that no longer needs its call. Its status is also marked as already in Shopify, so
    the write-back does not tag the order with something nobody said on a call."""
    conn.execute("UPDATE contacts SET status=?, next_try_at=NULL, last_result=? WHERE id=?", (status, note, contact_id))
    conn.execute("UPDATE shopify_links SET synced_status=? WHERE contact_id=?", (status, contact_id))


# --- webhook events -----------------------------------------------------------------------------------

def on_order(order: dict) -> str:
    gid = str(order.get("admin_graphql_api_id") or f"gid://shopify/Order/{order.get('id')}")
    row = order_row(order)
    token = str(order.get("checkout_token") or "")
    with db.connect() as conn:
        # The cart this order came from (or any cart of this number) is no longer abandoned.
        conn.execute("UPDATE shopify_checkouts SET completed_at=? WHERE completed_at IS NULL AND (token=? OR (phone=? AND phone!=''))",
                     (db.now_iso(), token, row["phone"]))
        carts = conn.execute(
            "SELECT l.contact_id FROM shopify_links l JOIN contacts k ON k.id=l.contact_id "
            "WHERE l.kind='cart' AND (l.shopify_id=? OR (k.phone=? AND k.phone!=''))", (token, row["phone"])).fetchall()
        for c in carts:
            if _not_called_yet(conn, c["contact_id"]):
                _take_off_list(conn, c["contact_id"], "already_ordered", f"ordered on the store ({row['order_id']}) before the cart call")
        if conn.execute("SELECT 1 FROM shopify_links WHERE shopify_id=? AND kind='order'", (gid,)).fetchone():
            return "already on the list"
    mode = settings()["orders"]
    if mode == "off":
        return "not added: order calls are off"
    if mode == "cod" and row["payment_mode"] != "COD":
        return "not added: prepaid (only cash-on-delivery orders are called)"
    if order.get("cancelled_at"):
        return "not added: already cancelled"
    if row["phone"] and row["phone"] in db.do_not_call_phones():
        return "not added: this number asked not to be called"
    contact_id = _add_contact("order", row, gid)
    return f"added {row['order_id']} as contact {contact_id}" + ("" if row["phone"] else " (no valid phone: needs review)")


def on_order_cancelled(order: dict) -> str:
    gid = str(order.get("admin_graphql_api_id") or f"gid://shopify/Order/{order.get('id')}")
    with db.connect() as conn:
        link = conn.execute("SELECT contact_id FROM shopify_links WHERE shopify_id=? AND kind='order'", (gid,)).fetchone()
        if not link:
            return "not on the list"
        if not _not_called_yet(conn, link["contact_id"]):
            return "already called; left as it is"
        _take_off_list(conn, link["contact_id"], "cancelled", "order cancelled in Shopify before the call")
    return f"contact {link['contact_id']} taken off the list"


def on_checkout(checkout: dict) -> str:
    token = str(checkout.get("token") or "")
    if not token:
        return "ignored: no checkout token"
    row = cart_row(checkout)
    now = db.now_iso()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO shopify_checkouts (token, phone, row, first_seen, last_update, completed_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(token) DO UPDATE SET phone=excluded.phone, row=excluded.row, last_update=excluded.last_update, "
            "completed_at=COALESCE(shopify_checkouts.completed_at, excluded.completed_at)",
            (token, row["phone"], json.dumps(row, ensure_ascii=False), now, now, checkout.get("completed_at") or None))
    return "checkout completed" if checkout.get("completed_at") else "cart held" + ("" if row["phone"] else " (no phone yet)")


HANDLERS = {"orders/create": on_order, "orders/cancelled": on_order_cancelled,
            "checkouts/create": on_checkout, "checkouts/update": on_checkout}


def valid_signature(raw: bytes, signature: str) -> bool:
    if not SHOPIFY_WEBHOOK_SECRET or not signature:
        return False
    digest = base64.b64encode(hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, signature.strip())


@router.post(WEBHOOK_PATH)
async def webhook(request: Request) -> dict:
    """Shopify calls this. Answer within five seconds with a 2xx, or Shopify sends it again."""
    raw = await request.body()
    if not SHOPIFY_WEBHOOK_SECRET:
        raise HTTPException(503, "Shopify is not set up on this server (SHOPIFY_CLIENT_SECRET or SHOPIFY_WEBHOOK_SECRET).")
    if not valid_signature(raw, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        log.warning("webhook refused: bad signature (topic %s)", request.headers.get("X-Shopify-Topic"))
        raise HTTPException(401, "bad signature")
    topic = request.headers.get("X-Shopify-Topic", "")
    webhook_id = request.headers.get("X-Shopify-Webhook-Id", "") or hashlib.sha1(raw).hexdigest()
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM shopify_events WHERE webhook_id=?", (webhook_id,)).fetchone():
            return {"ok": True, "result": "duplicate"}
    handler = HANDLERS.get(topic)
    try:
        result = handler(json.loads(raw)) if handler else "ignored: topic not used"
    except Exception as exc:  # noqa: BLE001
        log.exception("webhook %s failed", topic)
        result = f"failed: {type(exc).__name__}: {exc}"
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO shopify_events (webhook_id, topic, received_at, result) VALUES (?, ?, ?, ?)",
                     (webhook_id, topic, db.now_iso(), result[:300]))
    log.info("webhook %s: %s", topic, result)
    return {"ok": True, "result": result}


# --- abandoned carts --------------------------------------------------------------------------------

def process_carts() -> int:
    """Turn checkouts unfinished for longer than the wait into cart calls. Returns how many."""
    s = settings()
    if not s["carts"]:
        return 0
    added = 0
    blocked = db.do_not_call_phones()
    with db.connect() as conn:
        due = conn.execute(
            "SELECT * FROM shopify_checkouts WHERE completed_at IS NULL AND contact_id IS NULL AND skipped IS NULL "
            "AND last_update <= ? ORDER BY last_update", (_utc(float(s["cart_wait_minutes"])),)).fetchall()
    too_old = _utc(float(s["cart_max_hours"]) * 60)
    for r in due:
        row = json.loads(r["row"] or "{}")
        phone = row.get("phone") or ""
        skip = ""
        if r["last_update"] < too_old:
            skip = "too old when found"
        elif not phone:
            skip = "no phone number"
        elif phone in blocked:
            skip = "number asked not to be called"
        else:
            with db.connect() as conn:
                recent = conn.execute(
                    "SELECT 1 FROM shopify_links l JOIN contacts k ON k.id=l.contact_id "
                    "WHERE l.kind='cart' AND k.phone=? AND k.created_at >= ?", (phone, _utc(3 * 24 * 60))).fetchone()
            if recent:
                skip = "already called about a cart in the last 3 days"
        with db.connect() as conn:
            if skip:
                conn.execute("UPDATE shopify_checkouts SET skipped=? WHERE token=?", (skip, r["token"]))
                continue
        contact_id = _add_contact("cart", row, r["token"])
        with db.connect() as conn:
            conn.execute("UPDATE shopify_checkouts SET contact_id=? WHERE token=?", (contact_id, r["token"]))
        added += 1
        log.info("abandoned cart %s added as contact %s", r["token"][:8], contact_id)
    return added


# --- the Admin API: write-back and webhook set-up -------------------------------------------------------

class ShopifyError(Exception):
    pass


_TOKEN = {"value": "", "until": 0.0}


async def _access_token() -> str:
    if SHOPIFY_ADMIN_TOKEN:
        return SHOPIFY_ADMIN_TOKEN
    if not admin_ready():
        raise ShopifyError("Shopify is not set up: SHOPIFY_STORE plus SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET in .env")
    if _TOKEN["value"] and time.time() < _TOKEN["until"]:
        return _TOKEN["value"]
    # Client credentials grant (Dev Dashboard app on our own store): a token for 24 hours.
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
                              data={"grant_type": "client_credentials", "client_id": SHOPIFY_CLIENT_ID,
                                    "client_secret": SHOPIFY_CLIENT_SECRET})
    if r.status_code >= 300:
        raise ShopifyError(f"Shopify refused the app's credentials ({r.status_code}): {r.text[:200]}")
    j = r.json()
    _TOKEN["value"] = j["access_token"]
    _TOKEN["until"] = time.time() + int(j.get("expires_in") or 86400) - 300
    return _TOKEN["value"]


async def graphql(query: str, variables: dict | None = None) -> dict:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
                              json={"query": query, "variables": variables or {}},
                              headers={"X-Shopify-Access-Token": token})
    if r.status_code == 401:
        _TOKEN["value"] = ""
    if r.status_code >= 300:
        raise ShopifyError(f"Shopify answered {r.status_code}: {r.text[:200]}")
    j = r.json()
    if j.get("errors"):
        raise ShopifyError(f"Shopify: {json.dumps(j['errors'])[:300]}")
    return j.get("data") or {}


def _user_errors(data: dict, field: str) -> None:
    errors = (data.get(field) or {}).get("userErrors") or []
    if errors:
        raise ShopifyError("; ".join(e.get("message", "") for e in errors))


TAGS_ADD = "mutation tagsAdd($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }"
TAGS_REMOVE = "mutation tagsRemove($id: ID!, $tags: [String!]!) { tagsRemove(id: $id, tags: $tags) { userErrors { field message } } }"
ORDER_NOTE = "query orderNote($id: ID!) { order(id: $id) { note } }"
ORDER_UPDATE = "mutation orderUpdate($input: OrderInput!) { orderUpdate(input: $input) { userErrors { field message } } }"


def _tag(status: str) -> str:
    return "voice-" + status.replace("_", "-")


def _result_line(contact_id: int, status: str, last_result: str) -> str:
    call = db.latest_call_for_contact(contact_id) or {}
    details = call.get("outcome_details") if isinstance(call.get("outcome_details"), dict) else {}
    labels = {**DETAIL_LABELS, "new_address": "New address"}
    parts = [f"{labels[k]}: {str(v).strip()}" for k, v in details.items() if k in labels and str(v or "").strip()]
    when = datetime.now(db.IST).strftime("%d %b %H:%M")
    line = f"Voice call {when}: {RESPONSES.get(status, status.replace('_', ' '))}"
    extra = "; ".join(parts) or (last_result if last_result and last_result != status else "")
    return f"{line}. {extra}".strip().rstrip(".")[:600]


async def _push_result(gid: str, contact_id: int, status: str, previous: str | None, last_result: str) -> None:
    _user_errors(await graphql(TAGS_ADD, {"id": gid, "tags": ["voice-call", _tag(status)]}), "tagsAdd")
    if previous and previous != status:
        _user_errors(await graphql(TAGS_REMOVE, {"id": gid, "tags": [_tag(previous)]}), "tagsRemove")
    note = ((await graphql(ORDER_NOTE, {"id": gid})).get("order") or {}).get("note") or ""
    line = _result_line(contact_id, status, last_result)
    _user_errors(await graphql(ORDER_UPDATE, {"input": {"id": gid, "note": f"{note}\n{line}".strip()}}), "orderUpdate")


ORDER_BY_NAME = "query orderByName($q: String!) { orders(first: 5, query: $q) { nodes { id name } } }"


def mark_prepaid(contact: dict, code: str, call_id: int | None) -> str:
    """A cash-on-delivery customer chose on the call to pay online (the WhatsApp coupon): the order's payment is Prepaid
    in our records, and its Shopify order gets the prepaid tags and a note line (sent by the worker). Returns what
    changed, for the call's notes. Never raises."""
    what = "COD to Prepaid: customer chose online payment on the call" + (f"; coupon {code} sent on WhatsApp" if code else "")
    try:
        with db.connect() as conn:
            conn.execute("UPDATE contacts SET payment_mode='Prepaid' WHERE id=?", (contact["id"],))
            link = conn.execute("SELECT shopify_id FROM shopify_links WHERE contact_id=? AND kind='order'", (contact["id"],)).fetchone()
            gid = link["shopify_id"] if link and str(link["shopify_id"]).startswith("gid://") else ""
            when = datetime.now(db.IST).strftime("%d %b %H:%M")
            # An order linked here gets the call's result line (sync_results), which carries the change already.
            note = "" if gid else f"Voice call {when}: payment changed from COD to Prepaid (customer chose online payment" + \
                (f", coupon {code} sent on WhatsApp)" if code else ")")
            conn.execute("INSERT INTO shopify_updates (contact_id, call_id, order_name, shopify_id, tags_add, tags_remove, note, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (contact["id"], call_id, str(contact.get("order_id") or "").strip(), gid,
                          json.dumps(SHOPIFY_PREPAID_TAGS_ADD), json.dumps(SHOPIFY_PREPAID_TAGS_REMOVE), note, db.now_iso()))
        log.info("contact %s (order %s): payment COD -> Prepaid; Shopify update queued", contact["id"], contact.get("order_id"))
    except Exception:  # noqa: BLE001
        log.exception("contact %s: could not record the switch to prepaid", contact.get("id"))
    return what


async def _order_gid(name: str) -> str:
    """The Shopify order with this number ("#1234"), found by search; ShopifyError when there is not exactly that one."""
    if not name:
        raise ShopifyError("the contact has no order number to find the order by")
    nodes = (((await graphql(ORDER_BY_NAME, {"q": f'name:"{name}"'})).get("orders") or {}).get("nodes")) or []
    found = [n for n in nodes if str(n.get("name") or "").strip().lower() == name.strip().lower()]
    if len(found) != 1:
        raise ShopifyError(f"order {name} not found in Shopify" if not found else f"more than one order named {name}")
    return found[0]["id"]


async def sync_updates() -> int:
    """Send the order changes calls asked for (switched to prepaid). Returns how many went through."""
    if not settings()["writeback"] or not admin_ready():
        return 0
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM shopify_updates WHERE status='pending' AND tries < ? ORDER BY id LIMIT 20",
                            (SYNC_TRIES,)).fetchall()
    done = 0
    for r in rows:
        try:
            gid = r["shopify_id"] or await _order_gid(r["order_name"] or "")
            add, remove = json.loads(r["tags_add"] or "[]"), json.loads(r["tags_remove"] or "[]")
            if add:
                _user_errors(await graphql(TAGS_ADD, {"id": gid, "tags": add}), "tagsAdd")
            if remove:
                _user_errors(await graphql(TAGS_REMOVE, {"id": gid, "tags": remove}), "tagsRemove")
            if r["note"]:
                note = ((await graphql(ORDER_NOTE, {"id": gid})).get("order") or {}).get("note") or ""
                _user_errors(await graphql(ORDER_UPDATE, {"input": {"id": gid, "note": f"{note}\n{r['note']}".strip()}}), "orderUpdate")
        except Exception as exc:  # noqa: BLE001
            tries = int(r["tries"] or 0) + 1
            with db.connect() as conn:
                conn.execute("UPDATE shopify_updates SET tries=?, error=?, status=? WHERE id=?",
                             (tries, str(exc)[:300], "failed" if tries >= SYNC_TRIES else "pending", r["id"]))
            log.warning("order %s: Shopify update %s not sent (try %s): %s", r["order_name"], r["id"], tries, exc)
            if isinstance(exc, ShopifyError) and "not set up" in str(exc):
                break
            continue
        with db.connect() as conn:
            conn.execute("UPDATE shopify_updates SET status='done', shopify_id=?, error=NULL, done_at=? WHERE id=?",
                         (gid, db.now_iso(), r["id"]))
        log.info("order %s in Shopify: tags +%s -%s%s", r["order_name"], add, remove, ", note added" if r["note"] else "")
        done += 1
    return done


async def sync_results() -> int:
    """Write finished order calls back to Shopify. Returns how many orders were updated."""
    if not settings()["writeback"] or not admin_ready():
        return 0
    marks = ",".join("?" for _ in db.FINAL_STATUSES)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT l.*, k.status, k.last_result FROM shopify_links l JOIN contacts k ON k.id=l.contact_id "
            f"WHERE l.kind='order' AND k.status IN ({marks}) AND (l.synced_status IS NULL OR l.synced_status != k.status) "
            "AND COALESCE(l.sync_tries, 0) < ?", (*db.FINAL_STATUSES, SYNC_TRIES)).fetchall()
    done = 0
    for r in rows:
        try:
            await _push_result(r["shopify_id"], r["contact_id"], r["status"], r["synced_status"], r["last_result"] or "")
        except Exception as exc:  # noqa: BLE001
            with db.connect() as conn:
                conn.execute("UPDATE shopify_links SET sync_error=?, sync_tries=COALESCE(sync_tries, 0)+1 WHERE contact_id=?",
                             (str(exc)[:300], r["contact_id"]))
            log.warning("could not write contact %s back to Shopify: %s", r["contact_id"], exc)
            if isinstance(exc, ShopifyError) and "not set up" in str(exc):
                break
            continue
        with db.connect() as conn:
            conn.execute("UPDATE shopify_links SET synced_status=?, synced_at=?, sync_error=NULL, sync_tries=0 WHERE contact_id=?",
                         (r["status"], db.now_iso(), r["contact_id"]))
        done += 1
    return done


async def worker() -> None:
    """Runs for as long as the server does: cart timer and write-back, once a minute."""
    while True:
        try:
            n = process_carts()
            if n:
                log.info("%s abandoned cart(s) added", n)
            await sync_results()
            await sync_updates()
        except Exception:  # noqa: BLE001
            log.exception("shopify worker")
        await asyncio.sleep(TICK_SECONDS)


# --- dashboard ----------------------------------------------------------------------------------------

def webhook_url() -> str:
    return f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}" if PUBLIC_BASE_URL else ""


@router.get("/api/shopify")
async def status() -> dict:
    with db.connect() as conn:
        def n(sql: str, *args) -> int:
            return conn.execute(sql, args).fetchone()[0]
        counts = {
            "orders_added": n("SELECT COUNT(*) FROM shopify_links WHERE kind='order'"),
            "carts_held": n("SELECT COUNT(*) FROM shopify_checkouts WHERE completed_at IS NULL AND contact_id IS NULL AND skipped IS NULL"),
            "carts_completed": n("SELECT COUNT(*) FROM shopify_checkouts WHERE completed_at IS NOT NULL AND contact_id IS NULL"),
            "carts_added": n("SELECT COUNT(*) FROM shopify_links WHERE kind='cart'"),
            "carts_skipped": n("SELECT COUNT(*) FROM shopify_checkouts WHERE skipped IS NOT NULL"),
            "synced": n("SELECT COUNT(*) FROM shopify_links WHERE kind='order' AND synced_at IS NOT NULL"),
            "sync_failed": n(f"SELECT COUNT(*) FROM shopify_links WHERE kind='order' AND sync_tries >= {SYNC_TRIES}"),
        }
        events = [dict(r) for r in conn.execute(
            "SELECT topic, received_at, result FROM shopify_events ORDER BY received_at DESC LIMIT 15")]
        last_error = conn.execute("SELECT sync_error FROM shopify_links WHERE sync_error IS NOT NULL "
                                  "ORDER BY contact_id DESC LIMIT 1").fetchone()
    s = settings()
    return {
        "store": SHOPIFY_STORE, "webhooks_ready": bool(SHOPIFY_WEBHOOK_SECRET), "admin_ready": admin_ready(),
        "webhook_url": webhook_url(), "settings": {k: s[k] for k in DEFAULTS},
        "campaigns": {k: s.get(f"{k}s_campaign_id") for k in CAMPAIGNS},
        "counts": counts, "events": events, "last_sync_error": last_error[0] if last_error else "",
    }


@router.put("/api/shopify/settings")
async def save_settings(body: dict) -> dict:
    clean = {}
    if body.get("orders") in ("cod", "all", "off"):
        clean["orders"] = body["orders"]
    for key in ("carts", "writeback"):
        if key in body:
            clean[key] = bool(body[key])
    if "cart_wait_minutes" in body:
        clean["cart_wait_minutes"] = max(15, min(24 * 60, int(body["cart_wait_minutes"] or 60)))
    if "order_max_hours" in body:
        clean["order_max_hours"] = max(1, min(7 * 24, int(body["order_max_hours"] or 24)))
    if "order_wait_minutes" in body:
        clean["order_wait_minutes"] = max(0, min(24 * 60, int(body["order_wait_minutes"] or 0)))
    if "order_daily_limit" in body:
        clean["order_daily_limit"] = max(0, min(5000, int(body["order_daily_limit"] or 0)))
    if body.get("order_pick") in ("newest", "oldest", "random"):
        clean["order_pick"] = body["order_pick"]
    if body.get("order_days") in ("today", "yesterday", "last_3", "last_7", "earlier", "all"):
        clean["order_days"] = body["order_days"]
    if "cart_max_hours" in body:
        clean["cart_max_hours"] = max(1, min(7 * 24, int(body["cart_max_hours"] or 24)))
    _save(**clean)
    return await status()


@router.post("/api/shopify/check")
async def check() -> dict:
    """Ask Shopify who we are: proves the store address and the app's credentials work."""
    try:
        data = await graphql("query { shop { name myshopifyDomain } }")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return {"ok": True, "shop": data.get("shop")}


WEBHOOKS_LIST = "query { webhookSubscriptions(first: 100) { nodes { id topic uri } } }"
WEBHOOK_CREATE = ("mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) "
                  "{ webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) { webhookSubscription { id } "
                  "userErrors { field message } } }")
WEBHOOK_DELETE = ("mutation webhookSubscriptionDelete($id: ID!) { webhookSubscriptionDelete(id: $id) "
                  "{ deletedWebhookSubscriptionId userErrors { field message } } }")


@router.post("/api/shopify/webhooks")
async def connect_webhooks() -> dict:
    """Point the store's order and checkout webhooks at this server's public address. Old ones for an
    earlier address of ours (a tunnel that changed) are removed; anyone else's are left alone."""
    url = webhook_url()
    if not url:
        raise HTTPException(400, "This server has no public address (PUBLIC_BASE_URL). Start it with run_phone.bat.")
    try:
        existing = (await graphql(WEBHOOKS_LIST)).get("webhookSubscriptions", {}).get("nodes") or []
        created, removed = [], []
        for w in existing:
            if w.get("topic") in TOPICS and str(w.get("uri") or "").endswith(WEBHOOK_PATH) and w.get("uri") != url:
                _user_errors(await graphql(WEBHOOK_DELETE, {"id": w["id"]}), "webhookSubscriptionDelete")
                removed.append(w["topic"])
        have = {w.get("topic") for w in existing if w.get("uri") == url}
        for topic in TOPICS:
            if topic not in have:
                _user_errors(await graphql(WEBHOOK_CREATE, {"topic": topic, "sub": {"uri": url, "format": "JSON"}}),
                             "webhookSubscriptionCreate")
                created.append(topic)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return {"ok": True, "url": url, "created": created, "removed": removed}
