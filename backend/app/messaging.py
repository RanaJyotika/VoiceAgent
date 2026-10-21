"""The outbox for messages to customers: the coupon after an abandoned-cart call, and the prepaid coupon after an
order-confirmation call on a cash-on-delivery order (kind "prepaid_coupon": the campaign's "Share a coupon on
WhatsApp", owner 21 Oct 2026).

A message is written when the customer agreed on the call (queue_coupon, from live_bridge). It is sent by
the message service set in .env (MESSAGE_PROVIDER: pinnacle, whatsapp, plivo_sms or webhook), or by a person from
the dashboard (Coupons page: Open in WhatsApp, then Mark as sent) until one is set up. Nothing is ever
sent while MESSAGE_PROVIDER is empty.

Statuses:
- queued: the worker sends it once a service is set up (again after a failure that may pass, up to MESSAGE_RETRIES).
- sending: on its way. A server restart in the middle turns it into held: it may or may not have gone.
- sent.
- failed: given up after MESSAGE_RETRIES tries, or refused by the service for good.
- held: a person decides. Too old by the time a service was ready, a browser test with no PHONE_NUMBER, no valid
  number, or back after a restart.
- cancelled: by a person, or its coupon was switched off or deleted.
A browser test call, or test mode (DIAL_ONLY_MY_NUMBER), writes a test message to PHONE_NUMBER; a test message never
counts as the customer having the coupon.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException

from . import catalog, coupons, db
from .call_types import extra_of
from .config import (DIAL_ONLY_MY_NUMBER, MESSAGE_FROM_NUMBER, MESSAGE_MAX_AGE_HOURS, MESSAGE_PROVIDER, MESSAGE_RETRIES,
                     MESSAGE_SMS_TEMPLATE, MESSAGE_WEBHOOK_TOKEN, MESSAGE_WEBHOOK_URL, PHONE_NUMBER, PLIVO_AUTH_ID,
                     PINBOT_PHONE_NUMBER_ID, PINNACLE_API_KEY, PINNACLE_BASE_URL, PINNACLE_CART_PARAMS, PINNACLE_CART_TEMPLATE,
                     PINNACLE_DEFAULT_IMAGE_URL, PINNACLE_PREPAID_BUTTON, PINNACLE_PREPAID_IMAGE, PINNACLE_PREPAID_PARAMS,
                     PINNACLE_PREPAID_PARAMS_SET, PINNACLE_PREPAID_TEMPLATE, PINNACLE_TEMPLATE_LANGUAGE,
                     PLIVO_AUTH_TOKEN, PLIVO_DLT_ENTITY_ID, PLIVO_DLT_TEMPLATE_CATEGORY, PLIVO_DLT_TEMPLATE_ID,
                     PLIVO_FROM_NUMBER, STORE_NAME, STORE_URL, WHATSAPP_API_VERSION, WHATSAPP_PHONE_NUMBER_ID,
                     WHATSAPP_TEMPLATE, WHATSAPP_TEMPLATE_LANGUAGE, WHATSAPP_TEMPLATE_PARAMS, WHATSAPP_TOKEN)
from .contacts import is_valid_phone, normalise_phone

log = logging.getLogger("messaging")
router = APIRouter()

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT DEFAULT 'coupon',
    contact_id INTEGER,
    call_id INTEGER,
    to_phone TEXT,                -- +91XXXXXXXXXX
    customer_name TEXT,
    coupon_code TEXT,
    body TEXT,                    -- the full message (dashboard, sending by hand, SMS and webhook use it)
    params TEXT,                  -- JSON: the values that went into it (a WhatsApp template's variables)
    status TEXT DEFAULT 'queued', -- queued, sending, sent, failed, held, cancelled
    test INTEGER DEFAULT 0,       -- a browser test call or test mode: never counts as the customer having it
    provider TEXT,                -- whatsapp, plivo_sms, webhook, manual
    provider_id TEXT,
    error TEXT,                   -- the last failure, or why it waits
    attempts INTEGER DEFAULT 0,
    next_try_at TEXT,
    created_at TEXT,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_status ON messages (status, next_try_at);
"""

STATUSES = ("queued", "sending", "sent", "failed", "held", "cancelled")
HAS_IT = ("queued", "sending", "sent", "held")   # the customer has the coupon, or is getting it: never queued again
TICK_SECONDS = 30
BATCH = 20                                       # messages one round of the worker sends at most
PLIVO_API = "https://api.plivo.com/v1/Account"
RESTARTED = "the server restarted while sending it; check before sending again"
BROWSER_TEST = ("browser test call: not sent automatically (set PHONE_NUMBER in .env to get test messages on your "
                "own phone)")
NO_PHONE = "no valid phone number"
NOT_SET_UP = "No message service is set up yet. Use Open in WhatsApp to send it by hand, then Mark as sent."

# What a message may say, in this order; the dashboard lists them for the owner.
PLACEHOLDERS = ("name", "product", "amount", "offer", "discount", "code", "link", "store", "min_order", "pay", "order", "until",
                "link_path", "order_line", "offer_line", "pay_line")

# Owner, 19 Oct 2026: the coupon code and the product in a beautifully drafted message (WhatsApp *bold*, Hinglish).
# A line whose value is empty is left out (no cart value on record, no minimum order).
DEFAULT_TEMPLATE = """🙏 Namaste {name},

Aapne {store} par jo pasand kiya tha, woh abhi bhi aapke cart mein hai:
🛍️ *{product}*
💰 Cart value: {amount}

🎁 *Aapke liye khaas offer: {offer}*
🏷️ Coupon code: *{code}*
{min_order}

Kaise use karein:
1️⃣ Neeche diye link par jaayein
2️⃣ Checkout par coupon code *{code}* daalein
3️⃣ {pay} aur {discount} ki chhoot paayein

👉 {link}



Dhanyavaad 🙏
Team {store}"""

# Owner, 21 Oct 2026: the coupon for paying online instead of cash on delivery, after the customer confirmed their order
# on the call and said yes to getting the code on WhatsApp. A line whose value is empty is left out (no order id, no
# last day for the offer).
DEFAULT_PREPAID_TEMPLATE = """🙏 Namaste {name},

{store} par order karne ke liye dil se dhanyavaad! ✨
🛍️ Aapka order: *{product}*
🧾 Order ID: {order}

🎁 *Aapke liye ek khaas tohfa*
Cash on Delivery ki jagah *online payment* karein aur paayein *{discount} OFF* 🎉

🏷️ Use coupon code: *{code}*
⏳ Offer {until} tak valid hai

Kaise use karein:
1️⃣ Neeche diye link par jaayein
2️⃣ Checkout par coupon code *{code}* daalein
3️⃣ Online payment karein aur chhoot ka laabh uthayein

👉 {link}



Dhanyavaad 🙏
Team {store}"""

# The same message as a WhatsApp template, to submit for approval in Pinnacle's console (Meta approves every message a
# business starts). Its {{1}}..{{5}} are filled in the order of PINNACLE_PREPAID_PARAMS: name, product, discount, code,
# link. A template cannot drop a line, so it has no order id or last day.
PREPAID_WHATSAPP_TEMPLATE = """🙏 Namaste {{1}},

Vedic Shop par order karne ke liye dil se dhanyavaad! ✨
🛍️ Aapka order: *{{2}}*

🎁 *Aapke liye ek khaas tohfa*
Cash on Delivery ki jagah *online payment* karein aur paayein *{{3}} OFF* 🎉

🏷️ Use coupon code: *{{4}}*

Kaise use karein:
1️⃣ Neeche diye link par jaayein
2️⃣ Checkout par upar diya coupon code daalein
3️⃣ Online payment karein aur chhoot ka laabh uthayein

👉 {{5}}


Dhanyavaad 🙏
Team Vedic Shop"""

# Indian SMS must match a DLT-registered template word for word, so MESSAGE_SMS_TEMPLATE replaces this one.
DEFAULT_SMS_TEMPLATE = ("Namaste {name}, {store} par aapke cart ka {product} aapka intezaar kar raha hai. {offer}: coupon "
                        "{code}. Order karein: {link} - Team {store}")


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)
        held = conn.execute("UPDATE messages SET status='held', error=? WHERE status='sending'", (RESTARTED,)).rowcount
    if held:
        log.warning("%s message(s) were being sent when the server stopped; held for a person to check", held)


# --- writing a message ------------------------------------------------------------------------------

_KEY = re.compile(r"\{(\w+)\}")


def render(template: str, values: dict) -> str:
    """The template with {name}, {code}... filled in. A line with a value that is empty is left out whole (no
    "Cart value:" with nothing after it); a {word} that is not one of the values stays as typed."""
    lines = []
    for line in str(template or "").replace("\r\n", "\n").split("\n"):
        if any(not str(values[k] or "").strip() for k in _KEY.findall(line) if k in values):
            continue
        lines.append(_KEY.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), line).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _short(text, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,;") + "…"


def _product(contact: dict) -> dict:
    """The catalogue product behind the contact's order or cart ({} when it cannot be found)."""
    try:
        found = catalog.find_for_contact(contact)
    except Exception:  # noqa: BLE001
        log.exception("no catalogue product for contact %s", contact.get("id"))
        found = []
    return found[0] if found else {}


def link_for(contact: dict) -> str:
    """Where the message sends the customer: their own cart when the store gave its link (Shopify's
    abandoned_checkout_url), else the product's page, else the store."""
    extra = extra_of(contact)
    for key in ("cart_url", "cart_link", "checkout_url"):
        v = extra.get(key)
        url = str((v.get("value") if isinstance(v, dict) else v) or "").strip()
        if url.lower().startswith(("http://", "https://")):
            return url
    product = _product(contact)
    return f"{STORE_URL}/products/{product['handle']}" if product.get("handle") else STORE_URL


def image_for(contact: dict) -> str:
    """The product's first photo for the top of the WhatsApp message, else the default image ("" = none)."""
    image = str(_product(contact).get("image") or "")
    return image if image.lower().startswith("https://") else PINNACLE_DEFAULT_IMAGE_URL


def _values(contact: dict, coupon: dict, link: str) -> dict[str, str]:
    name = " ".join(str(contact.get("customer_name") or "").split())
    amount = coupons.parse_amount(contact.get("amount"))
    min_order = coupons.parse_amount(coupon.get("min_order")) or 0
    discount = coupons.discount_words(coupon)
    prepaid = bool(coupon.get("prepaid_only"))
    return {
        "name": name if name.lower().endswith(" ji") else f"{name} ji" if name else "ji",
        "product": _short(contact.get("product")) or "aapka product",
        "amount": f"₹{coupons.rupees(amount)}" if amount and amount > 0 else "",
        "offer": f"online payment (prepaid) par {discount} OFF" if prepaid else f"{discount} OFF",
        "discount": discount,
        "code": str(coupon.get("code") or ""),
        "link": link,
        "store": STORE_NAME,
        "min_order": f"(₹{coupons.rupees(min_order)} ya usse zyada ke order par)" if min_order > 0 else "",
        "pay": "Online payment karein" if prepaid else "Order place karein",
    }


def values_for(contact: dict, coupon: dict) -> dict[str, str]:
    """What fills the message's {name}, {product}, {code}... for this customer and coupon."""
    return _values(contact, coupon, link_for(contact))


def sample_values(coupon: dict) -> dict[str, str]:
    """The same with a made-up customer, for the preview on the Coupons page."""
    sample = {"customer_name": "Ramesh", "product": "Rudraksha Mala 108 beads", "amount": "1499"}
    return _values(sample, coupon, STORE_URL)


def sms_text(values: dict) -> str:
    return render(MESSAGE_SMS_TEMPLATE or DEFAULT_SMS_TEMPLATE, values)


def queue_coupon(*, contact: dict, coupon: dict, call_id: int | None, to: str = "", channel: str = "plivo") -> int | None:
    """Write the coupon message for a customer who said yes to it on a call. Returns its id (or the id of the one
    this customer already has for this code), or None when nothing was written (no coupon, or a number that
    asked not to be called). to: the number the phone call went to."""
    if not coupon or not coupon.get("code"):
        return None
    coupon = coupons.get_by_code(coupon["code"]) or coupon   # its message as it reads now
    values = values_for(contact, coupon)
    return _queue("coupon", contact=contact, code=coupon["code"], body=render(coupon.get("message") or DEFAULT_TEMPLATE, values),
                  values=values, call_id=call_id, to=to, channel=channel)


def prepaid_values(contact: dict, offer: dict, link: str = "", image: str | None = None) -> dict[str, str]:
    """What fills the prepaid coupon message: the customer, their order, and the campaign's offer (call_types.offers_of)."""
    name = " ".join(str(contact.get("customer_name") or "").split())
    until = ""
    if offer.get("valid_until"):
        try:
            until = datetime.fromisoformat(offer["valid_until"][:10]).strftime("%d %b %Y").lstrip("0")
        except ValueError:
            until = ""
    discount = " ".join(str(offer.get("max_discount") or "").split()) or _short(offer.get("title"), 60) or "special"
    name = name if name.lower().endswith(" ji") else f"{name} ji" if name else "ji"
    product = _short(contact.get("product")) or "aapka product"
    amount = coupons.parse_amount(contact.get("amount"))
    price = f" (₹{coupons.rupees(amount)})" if amount and amount > 0 else ""
    code = str(offer.get("code") or "").strip().upper()
    link = link or link_for(contact)
    return {
        # Whole sentences for a template whose {{1}}, {{2}}, {{3}} each take one (owner, 21 Oct 2026): a template value
        # cannot hold a line break, so each is one line. The name and the order; the offer and its code; the link.
        "order_line": f"{name}, aapka order *{product}*{price} confirm ho gaya hai ✅",
        "offer_line": f"🎁 Cash on Delivery ki jagah *online payment* karein aur paayein *{discount} OFF*! Checkout par coupon "
                      f"code *{code}* lagayein" + (f" (offer {until} tak valid)." if until else "."),
        "pay_line": f"👉 Abhi online payment karein: {link}",
        "name": name,
        "product": product,
        "order": str(contact.get("order_id") or "").strip(),
        "discount": discount,
        "offer": f"online payment par {discount} OFF",
        "code": code,
        "link": link,
        "store": STORE_NAME,
        "until": until,
        "link_path": "",   # filled below: the link without the store's address, for a button's dynamic link
        "image": image if image is not None else image_for(contact),   # the template's image, not a line of the text
    }


def _with_path(values: dict) -> dict:
    link = values.get("link") or ""
    return {**values, "link_path": link[len(STORE_URL) + 1:] if link.startswith(STORE_URL + "/") else ""}


def queue_prepaid_coupon(*, contact: dict, offer: dict, call_id: int | None, to: str = "", channel: str = "plivo") -> int | None:
    """Write the prepaid coupon message for a cash-on-delivery customer who said yes on the call to getting the code on
    WhatsApp. Returns its id (or the one they already have for this code), or None when nothing was written."""
    if not offer or not str(offer.get("code") or "").strip():
        return None
    values = _with_path(prepaid_values(contact, offer))
    return _queue("prepaid_coupon", contact=contact, code=values["code"], body=render(DEFAULT_PREPAID_TEMPLATE, values),
                  values=values, call_id=call_id, to=to, channel=channel)


def _queue(kind: str, *, contact: dict, code: str, body: str, values: dict, call_id: int | None, to: str,
           channel: str) -> int | None:
    """The message written for the number the call rang (the owner's own in test mode), once per number and code."""
    test = channel != "plivo" or DIAL_ONLY_MY_NUMBER
    status, error = "queued", None
    if channel == "plivo":
        recipient = normalise_phone(to) or normalise_phone(contact.get("phone"))
        if DIAL_ONLY_MY_NUMBER:
            recipient = normalise_phone(PHONE_NUMBER)
    elif PHONE_NUMBER:
        recipient = normalise_phone(PHONE_NUMBER)
    else:
        recipient, status, error = normalise_phone(contact.get("phone")), "held", BROWSER_TEST
    if not is_valid_phone(recipient):
        status, error = "held", NO_PHONE
    # A call to the owner's own number (Call my phone, or the number typed in the box) is a test too: it never counts as
    # a customer having the coupon, and the next test still gets its message.
    test = test or bool(PHONE_NUMBER and recipient == normalise_phone(PHONE_NUMBER))
    if recipient and recipient in db.do_not_call_phones():
        log.info("coupon %s for contact %s not queued: %s asked not to be called", code, contact.get("id"), recipient)
        return None
    if not test and is_valid_phone(recipient):
        with db.connect() as conn:
            row = conn.execute(
                f"SELECT id FROM messages WHERE test=0 AND to_phone=? AND coupon_code=? COLLATE NOCASE "
                f"AND status IN ({','.join('?' for _ in HAS_IT)}) ORDER BY id LIMIT 1",
                (recipient, code, *HAS_IT)).fetchone()
        if row:
            log.info("coupon %s for contact %s not queued again: %s already has it (message %s)",
                     code, contact.get("id"), recipient, row["id"])
            return row["id"]
    with db.connect() as conn:
        message_id = conn.execute(
            "INSERT INTO messages (kind, contact_id, call_id, to_phone, customer_name, coupon_code, body, params, status, "
            "test, error, attempts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (kind, contact.get("id"), call_id, recipient, str(contact.get("customer_name") or ""), code,
             body, json.dumps(values, ensure_ascii=False), status, 1 if test else 0, error, db.now_iso())).lastrowid
    log.info("%s %s for contact %s (call %s) queued as message %s to %s: %s%s", kind.replace("_", " "), code,
             contact.get("id"), call_id, message_id, recipient or "no number", status, " (test)" if test else "")
    return message_id


def already_sent(phone: str, code: str) -> bool:
    """This customer has this code already: a real (not test) message to them that is waiting, going or gone."""
    to = normalise_phone(phone)
    if not to or not code:
        return False
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
            return False
        return bool(conn.execute(
            f"SELECT 1 FROM messages WHERE test=0 AND to_phone=? AND coupon_code=? COLLATE NOCASE "
            f"AND status IN ({','.join('?' for _ in HAS_IT)}) LIMIT 1", (to, code, *HAS_IT)).fetchone())


# --- reading and changing messages ---------------------------------------------------------------------

def _public(row) -> dict:
    m = dict(row)
    m["test"] = bool(m.get("test"))
    m["params"] = db._json(m.get("params"), {})
    # For sending by hand: WhatsApp opens on this number with the text typed in (no number: pick the chat).
    digits = re.sub(r"\D", "", m.get("to_phone") or "")
    m["wa_link"] = f"https://wa.me/{digits}?text={quote(m.get('body') or '', safe='')}"
    return m


def get_message(message_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
    return _public(row) if row else None


def list_messages(status: str = "all", limit: int = 200) -> list[dict]:
    """Newest first; status "all" or one of STATUSES."""
    where, args = ("WHERE status=?", (status,)) if status != "all" else ("", ())
    with db.connect() as conn:
        return [_public(r) for r in conn.execute(f"SELECT * FROM messages {where} ORDER BY id DESC LIMIT ?", (*args, limit))]


def counts() -> dict[str, int]:
    """Messages by status, every status listed, and "all"."""
    with db.connect() as conn:
        by = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM messages GROUP BY status")}
    out = {s: by.get(s, 0) for s in STATUSES}
    out["all"] = sum(by.values())
    return out


def _set(message_id: int, **fields) -> None:
    with db.connect() as conn:
        conn.execute(f"UPDATE messages SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), message_id))


def _too_old(m: dict) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MESSAGE_MAX_AGE_HOURS)).isoformat()
    return (m.get("created_at") or "") < cutoff


def _too_old_note() -> str:
    return (f"older than {MESSAGE_MAX_AGE_HOURS:g} hours when the message service was ready; send it by hand if it is "
            "still wanted")


# --- the message services ---------------------------------------------------------------------------------

class SendError(Exception):
    """The service did not take the message. retry: a failure that may pass (down, busy, slow to answer)."""

    def __init__(self, text: str, retry: bool):
        super().__init__(text)
        self.retry = retry


def provider_status() -> dict:
    """Which service sends the messages, whether it has what it needs from .env, and a note for the Coupons page."""
    p = MESSAGE_PROVIDER
    if not p:
        return {"provider": "", "ready": False, "missing": [], "label": "Not set up",
                "note": "Messages wait here until a message service is set in .env (MESSAGE_PROVIDER). You can send them "
                        "by hand meanwhile."}
    notes = []
    if p == "pinnacle":
        label = "WhatsApp (Pinnacle)"
        need = {"API_Key": PINNACLE_API_KEY, "PINBOT_PHONE_NUMBER_ID (the sending number's id in Pinnacle)": PINBOT_PHONE_NUMBER_ID}
        if (why := template_missing({"kind": "prepaid_coupon"}, "pinnacle")):
            notes.append(f"Prepaid coupon messages wait: {why}.")
        if not PINNACLE_CART_TEMPLATE:
            notes.append("Abandoned-cart coupon messages wait until their template name is in .env (PINNACLE_CART_TEMPLATE).")
        for name, keys in (("PINNACLE_PREPAID_PARAMS", PINNACLE_PREPAID_PARAMS), ("PINNACLE_CART_PARAMS", PINNACLE_CART_PARAMS)):
            if unknown := [k for k in keys if k not in PLACEHOLDERS]:
                need[f"{name} (not message values: {', '.join(unknown)})"] = ""
    elif p == "whatsapp":
        label = "WhatsApp"
        need = {"WHATSAPP_TOKEN": WHATSAPP_TOKEN, "WHATSAPP_PHONE_NUMBER_ID": WHATSAPP_PHONE_NUMBER_ID}
        if not WHATSAPP_TEMPLATE:
            notes.append("Without WHATSAPP_TEMPLATE only customers who messaged you in the last 24 hours can receive it.")
        unknown = [k for k in WHATSAPP_TEMPLATE_PARAMS if k not in PLACEHOLDERS]
        if WHATSAPP_TEMPLATE and unknown:
            need[f"WHATSAPP_TEMPLATE_PARAMS (not message values: {', '.join(unknown)})"] = ""
    elif p == "plivo_sms":
        label = "Plivo SMS"
        need = {"PLIVO_AUTH_ID": PLIVO_AUTH_ID, "PLIVO_AUTH_TOKEN": PLIVO_AUTH_TOKEN,
                "MESSAGE_FROM_NUMBER or PLIVO_FROM_NUMBER": MESSAGE_FROM_NUMBER or PLIVO_FROM_NUMBER}
        if not PLIVO_DLT_TEMPLATE_ID:
            notes.append("Indian SMS needs a DLT-registered template: set PLIVO_DLT_ENTITY_ID and PLIVO_DLT_TEMPLATE_ID, "
                         "and MESSAGE_SMS_TEMPLATE to its exact text.")
    elif p == "webhook":
        label = "Webhook"
        need = {"MESSAGE_WEBHOOK_URL": MESSAGE_WEBHOOK_URL}
        if MESSAGE_WEBHOOK_URL and not MESSAGE_WEBHOOK_URL.lower().startswith(("http://", "https://")):
            need = {"MESSAGE_WEBHOOK_URL (an address starting with https://)": ""}
    else:
        return {"provider": p, "ready": False, "missing": ["MESSAGE_PROVIDER (pinnacle, whatsapp, plivo_sms or webhook)"],
                "label": "Unknown service", "note": f'MESSAGE_PROVIDER is "{p}", which this server does not know.'}
    missing = [k for k, v in need.items() if not v]
    if missing:
        notes.insert(0, "Messages wait here until the missing settings are in .env.")
    return {"provider": p, "ready": not missing, "missing": missing, "label": label, "note": " ".join(notes)}


def _client() -> httpx.AsyncClient:
    """Every request to a message service is made with this (an offline test swaps in a fake service)."""
    return httpx.AsyncClient(timeout=20)


def _error_text(r: httpx.Response) -> str:
    """The service's own words for a refusal: WhatsApp's error.message, Plivo's error, or the body."""
    try:
        j = r.json()
    except ValueError:
        return r.text[:300]
    if isinstance(j, dict):
        e = j.get("error")
        if isinstance(e, dict) and e.get("message"):
            return str(e["message"])[:300]
        if isinstance(e, str) and e:
            return e[:300]
        if j.get("message"):
            return str(j["message"])[:300]
    return r.text[:300]


async def _post(url: str, body: dict, headers: dict | None = None, auth: tuple[str, str] | None = None) -> dict:
    """POST JSON to the service and return its JSON answer ({} if none). SendError when it did not take it:
    5xx, 429, 408 and no answer may pass, so they are tried again; any other refusal is final."""
    try:
        async with _client() as client:
            r = await client.post(url, json=body, headers=headers or {}, auth=auth)
    except httpx.TimeoutException as exc:
        raise SendError(f"no answer in time ({type(exc).__name__})", retry=True)
    except httpx.HTTPError as exc:
        raise SendError(f"could not reach the service ({type(exc).__name__}: {exc})"[:300], retry=True)
    if not 200 <= r.status_code < 300:
        raise SendError(f"{r.status_code}: {_error_text(r)}"[:300], retry=r.status_code >= 500 or r.status_code in (408, 429))
    try:
        j = r.json()
    except ValueError:
        return {}
    return j if isinstance(j, dict) else {}


def _param(value) -> str:
    """One WhatsApp template value: no newlines, tabs or runs of spaces (Meta refuses them), never empty."""
    return " ".join(str(value or "").split()) or "-"


async def _send_whatsapp(m: dict) -> str:
    to = re.sub(r"\D", "", m["to_phone"])
    if WHATSAPP_TEMPLATE:
        body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": WHATSAPP_TEMPLATE, "language": {"code": WHATSAPP_TEMPLATE_LANGUAGE},
                             "components": [{"type": "body", "parameters": [
                                 {"type": "text", "text": _param(m["params"].get(k))} for k in WHATSAPP_TEMPLATE_PARAMS]}]}}
    else:
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": True, "body": m["body"]}}
    j = await _post(f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages", body,
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
    sent = j.get("messages") if isinstance(j.get("messages"), list) else []
    return str((sent[0] if sent and isinstance(sent[0], dict) else {}).get("id") or "")


# Pinnacle's template for each kind of message, and what fills its {{1}}, {{2}}...
PINNACLE_TEMPLATES = {"prepaid_coupon": (PINNACLE_PREPAID_TEMPLATE, PINNACLE_PREPAID_PARAMS),
                      "coupon": (PINNACLE_CART_TEMPLATE, PINNACLE_CART_PARAMS)}


def template_missing(m: dict, provider: str | None = None) -> str:
    """Why this message cannot go by the service in .env yet ("" = it can): Pinnacle sends only approved templates,
    each with exactly the values its text has."""
    if (provider or MESSAGE_PROVIDER) != "pinnacle":
        return ""
    if m.get("kind") == "prepaid_coupon":
        if not PINNACLE_PREPAID_TEMPLATE:
            return "no approved WhatsApp template named in .env (PINNACLE_PREPAID_TEMPLATE); send it by hand meanwhile"
        if not PINNACLE_PREPAID_PARAMS_SET:
            return ("what fills the template's {{1}}, {{2}}... is not set (PINNACLE_PREPAID_PARAMS in .env); send it by "
                    "hand meanwhile")
    elif not PINNACLE_TEMPLATES.get(m.get("kind") or "", ("", []))[0]:
        return "no approved WhatsApp template named in .env (PINNACLE_CART_TEMPLATE); send it by hand meanwhile"
    return ""


async def _send_pinnacle(m: dict) -> str:
    """A template message by Pinnacle (Pinbot), as the owner's other projects send it: the template by name, the product
    photo as its image, the values for its {{1}}, {{2}}..., and the button's dynamic link when it has one. Pinbot answers
    200 even when it refuses, with "status": "FAILED" or a "WA0..." code."""
    name, keys = PINNACLE_TEMPLATES[m["kind"]]
    components: list[dict] = []
    if m["kind"] == "prepaid_coupon" and PINNACLE_PREPAID_IMAGE:
        # A template with an image at the top needs one on every message.
        image = PINNACLE_PREPAID_IMAGE if PINNACLE_PREPAID_IMAGE.startswith("https://") else (m["params"].get("image") or PINNACLE_DEFAULT_IMAGE_URL)
        if not image:
            raise SendError("no product photo for this order and no PINNACLE_DEFAULT_IMAGE_URL in .env", retry=False)
        components.append({"type": "header", "parameters": [{"type": "image", "image": {"link": image}}]})
    components.append({"type": "body", "parameters": [{"type": "text", "text": _param(m["params"].get(k))} for k in keys]})
    if m["kind"] == "prepaid_coupon" and PINNACLE_PREPAID_BUTTON:
        components.append({"type": "button", "sub_type": "url", "index": "0",
                           "parameters": [{"type": "text", "text": _param(m["params"].get(PINNACLE_PREPAID_BUTTON))}]})
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": re.sub(r"\D", "", m["to_phone"]),
            "type": "template", "template": {"name": name, "language": {"code": PINNACLE_TEMPLATE_LANGUAGE}, "components": components}}
    j = await _post(f"{PINNACLE_BASE_URL}/v3/{PINBOT_PHONE_NUMBER_ID}/messages", body, headers={"apikey": PINNACLE_API_KEY})
    if str(j.get("status") or "").upper() == "FAILED" or str(j.get("code") or "").startswith("WA0"):
        raise SendError(f"Pinnacle: {j.get('code') or ''} {j.get('message') or 'refused'}".strip()[:300], retry=False)
    sent = j.get("messages") if isinstance(j.get("messages"), list) else []
    data = j.get("data") if isinstance(j.get("data"), dict) else {}
    return str((sent[0] if sent and isinstance(sent[0], dict) else {}).get("id") or data.get("messageid") or "")


async def _send_plivo_sms(m: dict) -> str:
    body = {"src": (MESSAGE_FROM_NUMBER or PLIVO_FROM_NUMBER).lstrip("+"), "dst": re.sub(r"\D", "", m["to_phone"]),
            "text": sms_text(m["params"])}
    for key, value in (("dlt_entity_id", PLIVO_DLT_ENTITY_ID), ("dlt_template_id", PLIVO_DLT_TEMPLATE_ID),
                       ("dlt_template_category", PLIVO_DLT_TEMPLATE_CATEGORY)):
        if value:
            body[key] = value
    j = await _post(f"{PLIVO_API}/{PLIVO_AUTH_ID}/Message/", body, auth=(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN))
    uuids = j.get("message_uuid") if isinstance(j.get("message_uuid"), list) else []
    return str(uuids[0]) if uuids else ""


async def _send_webhook(m: dict) -> str:
    params = m["params"]
    body = {"id": m["id"], "kind": m["kind"], "to": m["to_phone"], "text": m["body"], "coupon_code": m["coupon_code"],
            "customer_name": m["customer_name"] or "", "product": params.get("product", ""), "link": params.get("link", ""),
            "params": params}
    j = await _post(MESSAGE_WEBHOOK_URL, body,
                    headers={"Authorization": f"Bearer {MESSAGE_WEBHOOK_TOKEN}"} if MESSAGE_WEBHOOK_TOKEN else None)
    return str(j.get("id") or j.get("message_id") or "")


SENDERS = {"pinnacle": _send_pinnacle, "whatsapp": _send_whatsapp, "plivo_sms": _send_plivo_sms, "webhook": _send_webhook}


async def send_message(message_id: int, manual: bool = False) -> dict:
    """Send one message now by the service in .env and return it as it then stands. manual: a person pressed Send now,
    so a held or failed one may go too; the worker sends only queued ones. Raises ValueError, in plain English, when
    it cannot be sent at all."""
    service = provider_status()
    if not service["ready"]:
        raise ValueError(NOT_SET_UP)
    m = get_message(message_id)
    if not m:
        raise ValueError("No such message.")
    if m["status"] in ("sent", "sending", "cancelled"):
        raise ValueError({"sent": "This message has already been sent.", "sending": "This message is being sent right now.",
                          "cancelled": "This message was cancelled."}[m["status"]])
    if not manual and m["status"] != "queued":
        raise ValueError("Only a waiting message goes out on its own; this one needs a person.")
    if m["kind"] == "coupon" and not coupons.is_live(coupons.get_by_code(m["coupon_code"] or "")):
        _set(message_id, status="cancelled", error=f"coupon {m['coupon_code']} is switched off or has expired", next_try_at=None)
        log.info("message %s cancelled: coupon %s is switched off or has expired", message_id, m["coupon_code"])
        return get_message(message_id)
    if not is_valid_phone(m["to_phone"] or ""):
        raise ValueError("This message has no valid phone number.")
    if (why := template_missing(m)):
        raise ValueError(why[0].upper() + why[1:] + ".")
    if m["to_phone"] in db.do_not_call_phones():   # they asked on a later call, after the message was written
        _set(message_id, status="cancelled", error="this number asked not to be called", next_try_at=None)
        log.info("message %s cancelled: %s asked not to be called", message_id, m["to_phone"])
        return get_message(message_id)
    with db.connect() as conn:
        claimed = conn.execute("UPDATE messages SET status='sending' WHERE id=? AND status=?", (message_id, m["status"])).rowcount
    if not claimed:
        raise ValueError("This message changed in the meantime; look again.")
    provider, attempts = service["provider"], int(m["attempts"] or 0) + 1
    try:
        provider_id = await SENDERS[provider](m)
    except SendError as exc:
        again = exc.retry and attempts < MESSAGE_RETRIES
        due = (datetime.now(timezone.utc) + timedelta(minutes=5 * attempts)).isoformat() if again else None
        _set(message_id, status="queued" if again else "failed", attempts=attempts, provider=provider, error=str(exc)[:300],
             next_try_at=due)
        log.warning("message %s to %s not sent by %s (try %s): %s; %s", message_id, m["to_phone"], provider, attempts, exc,
                    "tried again later" if again else "given up")
        return get_message(message_id)
    except Exception as exc:  # noqa: BLE001
        _set(message_id, status="failed", attempts=attempts, provider=provider, error=f"{type(exc).__name__}: {exc}"[:300],
             next_try_at=None)
        log.exception("message %s: sending failed", message_id)
        return get_message(message_id)
    _set(message_id, status="sent", attempts=attempts, provider=provider, provider_id=provider_id or None, error=None,
         next_try_at=None, sent_at=db.now_iso())
    log.info("message %s (%s %s) sent to %s by %s%s", message_id, m["kind"].replace("_", " "), m["coupon_code"], m["to_phone"], provider,
             " (Send now on the dashboard)" if manual else "")
    return get_message(message_id)


def _hold(message_id: int, error: str) -> bool:
    """Held for a person, only while it is still queued: a round sends one message at a time, and a person may cancel
    or send the next one in the meantime."""
    with db.connect() as conn:
        return bool(conn.execute("UPDATE messages SET status='held', error=?, next_try_at=NULL WHERE id=? AND status='queued'",
                                 (error, message_id)).rowcount)


async def send_due() -> int:
    """One round of the worker: the queued messages whose time has come, oldest first. Returns how many went out."""
    with db.connect() as conn:
        rows = [_public(r) for r in conn.execute(
            "SELECT * FROM messages WHERE status='queued' AND (next_try_at IS NULL OR next_try_at='' OR next_try_at<=?) "
            "ORDER BY created_at, id LIMIT ?", (db.now_iso(), BATCH))]
    sent = 0
    for m in rows:
        if _too_old(m):
            if _hold(m["id"], _too_old_note()):
                log.info("message %s held: %s", m["id"], _too_old_note())
            continue
        if not is_valid_phone(m["to_phone"] or ""):
            _hold(m["id"], NO_PHONE)
            continue
        if template_missing(m):
            continue
        if m["test"] and m["to_phone"] != normalise_phone(PHONE_NUMBER):   # a test reaches only the owner's own phone
            _hold(m["id"], "a test call's message to a number that is not PHONE_NUMBER: not sent automatically")
            continue
        try:
            sent += (await send_message(m["id"]))["status"] == "sent"
        except ValueError as exc:
            log.info("message %s not sent: %s", m["id"], exc)
    return sent


async def worker() -> None:
    """Runs for as long as the server does: every TICK_SECONDS the waiting messages go out by the service in .env.
    With no service set up it only says so, once."""
    told = False
    while True:
        try:
            if provider_status()["ready"]:
                told = False
                await send_due()
            elif not told:
                log.info("no message service set up; coupon messages wait on the Coupons page")
                told = True
        except Exception:  # noqa: BLE001
            log.exception("message worker")
        await asyncio.sleep(TICK_SECONDS)


# --- the dashboard (Coupons page) ---------------------------------------------------------------------------

def _or_404(message_id: int) -> dict:
    m = get_message(message_id)
    if not m:
        raise HTTPException(404, "No such message.")
    return m


@router.get("/api/messages")
async def messages_page(status: str = "all", limit: int = 200) -> dict:
    if status != "all" and status not in STATUSES:
        raise HTTPException(400, f"status is all or one of: {', '.join(STATUSES)}.")
    return {"messages": list_messages(status, max(1, min(1000, limit))), "counts": counts(), "service": provider_status()}


@router.get("/api/messages/status")
async def messages_status() -> dict:
    """For the badge on the Coupons button: messages waiting for the service or a person."""
    c = counts()
    return {"counts": c, "waiting": c["queued"] + c["held"], "service": provider_status()}


@router.post("/api/messages/prepaid-preview")
async def prepaid_preview(body: dict) -> dict:
    """The prepaid coupon message as a sample customer gets it for this offer ({title, code, max_discount, valid_until}),
    the same as a template to submit in Pinnacle's console, and whether the service can send it."""
    offer = body.get("offer") if isinstance(body.get("offer"), dict) else {}
    # A real product from the catalogue when it is loaded, so the preview shows the product link a customer gets.
    product = next((x for x in catalog.STATE["products"] if x.get("handle") and x.get("title")), None)
    sample = {"customer_name": "Ramesh", "product": product["title"] if product else "Rudraksha Mala 108 beads", "order_id": "#1001"}
    link = f"{STORE_URL}/products/{product['handle']}" if product else STORE_URL
    values = _with_path(prepaid_values(sample, {**offer, "code": str(offer.get("code") or "COUPON").strip().upper()}, link=link,
                                       image=str((product or {}).get("image") or "") or PINNACLE_DEFAULT_IMAGE_URL))
    service = provider_status()
    waiting = template_missing({"kind": "prepaid_coupon"}) if service["provider"] == "pinnacle" else ""
    return {"body": render(DEFAULT_PREPAID_TEMPLATE, values), "image": values["image"] if PINNACLE_PREPAID_IMAGE else "",
            "template": PREPAID_WHATSAPP_TEMPLATE,
            "template_params": PINNACLE_PREPAID_PARAMS, "service": service, "waiting": waiting,
            # What the template named in .env gets, value by value, so it can be checked against its text.
            "template_name": PINNACLE_PREPAID_TEMPLATE,
            "sends": [{"slot": f"{{{{{i + 1}}}}}", "value": _param(values.get(k)), "from": k} for i, k in enumerate(PINNACLE_PREPAID_PARAMS)]
            + ([{"slot": "button link", "value": _param(values.get(PINNACLE_PREPAID_BUTTON)), "from": PINNACLE_PREPAID_BUTTON}]
               if PINNACLE_PREPAID_BUTTON else [])}


@router.post("/api/messages/{message_id}/send")
async def send_now(message_id: int) -> dict:
    _or_404(message_id)
    try:
        return await send_message(message_id, manual=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/messages/{message_id}/mark-sent")
async def mark_sent(message_id: int) -> dict:
    """A person sent it by hand (Open in WhatsApp)."""
    m = _or_404(message_id)
    if m["status"] == "cancelled":
        raise HTTPException(400, "This message was cancelled.")
    if m["status"] == "sending":
        raise HTTPException(400, "This message is being sent right now.")
    if m["status"] != "sent":
        _set(message_id, status="sent", provider="manual", sent_at=db.now_iso(), error=None, next_try_at=None)
        log.info("message %s (coupon %s) marked as sent by hand", message_id, m["coupon_code"])
    return get_message(message_id)


@router.post("/api/messages/{message_id}/cancel")
async def cancel(message_id: int) -> dict:
    m = _or_404(message_id)
    if m["status"] == "sent":
        raise HTTPException(400, "This message has already been sent.")
    if m["status"] == "sending":
        raise HTTPException(400, "This message is being sent right now; it cannot be stopped.")
    if m["status"] != "cancelled":
        _set(message_id, status="cancelled", error="cancelled by hand", next_try_at=None)
        log.info("message %s (coupon %s) cancelled by hand", message_id, m["coupon_code"])
    return get_message(message_id)


@router.post("/api/messages/{message_id}/retry")
async def retry(message_id: int) -> dict:
    """A failed or held message back in the queue, its tries counted from 0. One too old to go out on its own would
    only be held again: that one needs Send now, or sending by hand."""
    m = _or_404(message_id)
    if m["status"] not in ("failed", "held"):
        raise HTTPException(400, "Only a failed or held message can be tried again.")
    if not is_valid_phone(m["to_phone"] or ""):
        raise HTTPException(400, "This message has no valid phone number.")
    if m["test"] and m["to_phone"] != normalise_phone(PHONE_NUMBER):
        # A browser test call with no PHONE_NUMBER kept the customer's own number: it never goes out on its own.
        raise HTTPException(400, "This is a test call's message to the customer's own number, so it does not go out on "
                                 "its own. Use Send now only if it is really meant for them.")
    if _too_old(m):
        raise HTTPException(400, f"This message is older than {MESSAGE_MAX_AGE_HOURS:g} hours, so it does not go out on "
                                 "its own. Use Send now, or send it by hand and Mark as sent.")
    _set(message_id, status="queued", attempts=0, next_try_at=None, error=None)
    return get_message(message_id)
