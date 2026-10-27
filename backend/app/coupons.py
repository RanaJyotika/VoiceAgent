"""Coupon codes: the ones the owner adds on the dashboard (Coupons page), or another system sends
(POST /api/intake/coupons, e.g. the Vedic Vaibhav admin dashboard).

The one marked "offer on cart calls" is what the abandoned-cart agent offers, once, to a customer who
says no (call_types.py); when they agree, messaging.py queues the coupon message. A coupon that is
switched off, not started or past its last day is never offered and never sent. Its days are Indian
dates: one valid until 10 November works all of 10 November in India.
"""
import hmac
import logging
import math
import re
import sqlite3
from datetime import date, datetime

from fastapi import APIRouter, Header, HTTPException

from . import db
from .config import INTAKE_API_KEY
from .db import IST

log = logging.getLogger("coupons")
router = APIRouter()

SCHEMA = """
CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- as the customer types it at checkout, e.g. DIWALIDILSE10
    discount_type TEXT DEFAULT 'percent',      -- 'percent' or 'amount' (rupees off)
    discount_value REAL DEFAULT 0,             -- 10 = 10% (or Rs 10 for 'amount')
    prepaid_only INTEGER DEFAULT 1,            -- 1: only with online (prepaid) payment
    min_order REAL DEFAULT 0,                  -- cart value it needs; 0 = any
    valid_from TEXT,                           -- YYYY-MM-DD, Indian date; empty = from now
    valid_until TEXT,                          -- YYYY-MM-DD, the last day it works; empty = no end
    active INTEGER DEFAULT 1,                  -- 0: switched off; never offered, never sent
    offer_on_calls INTEGER DEFAULT 0,          -- 1: the abandoned-cart agent offers this one (one coupon at most)
    description TEXT DEFAULT '',               -- for the team: what it is
    message TEXT DEFAULT '',                   -- the message it is sent in; '' = messaging.DEFAULT_TEMPLATE
    created_at TEXT,
    updated_at TEXT
);
"""

# Owner, 19 Oct 2026: DIWALIDILSE10, 10% off on the website for prepaid orders, offered on abandoned-cart calls.
# Added once, the first time the server starts with coupons; deleted on the dashboard, it stays deleted.
SEED = {"code": "DIWALIDILSE10", "discount_type": "percent", "discount_value": 10, "prepaid_only": True, "active": True,
        "offer_on_calls": True, "description": "Diwali offer: 10% off on prepaid orders at Vedic Shop"}

CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,39}$")
FIELDS = ("code", "discount_type", "discount_value", "prepaid_only", "min_order", "valid_from", "valid_until", "active",
          "offer_on_calls", "description", "message")
FLAGS = {"prepaid_only": 1, "active": 1, "offer_on_calls": 0}     # yes/no fields, and what they are when not given
MAX_DESCRIPTION = 200
MAX_MESSAGE = 2000
# What another system may call the two kinds of discount.
_PERCENT = ("percent", "percentage", "%", "pct")
_AMOUNT = ("amount", "rupees", "rs", "inr", "₹", "flat", "fixed", "fixed_amount")
_MONEY = re.compile(r"rs\.?|inr|rupees?|₹|%|,|/-|\s", re.IGNORECASE)
_YES = ("1", "true", "on", "yes", "y")
_NO = ("0", "false", "off", "no", "n")


def init_tables() -> None:
    with db.connect() as conn:
        conn.executescript(SCHEMA)
    if not db.get_setting("coupons_seeded"):
        if not get_by_code(SEED["code"]):
            create_coupon(SEED)
        db.set_setting("coupons_seeded", True)


# --- numbers, dates and words ---------------------------------------------------------------------

def _today() -> date:
    return datetime.now(IST).date()


def _day(text) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


def _num(value) -> int | float:
    """A stored number as the API gives it: 10, not 10.0."""
    n = float(value or 0)
    return int(n) if n.is_integer() else n


def parse_amount(value) -> float | None:
    """A number as people write money or a discount: 1499, "1,499.00", "Rs. 1499", "₹1,499/-", "10%". None if it
    is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        try:
            n = float(_MONEY.sub("", str(value or "")))
        except ValueError:
            return None
    return n if math.isfinite(n) else None


def rupees(value) -> str:
    """Rupees with Indian digit grouping: 999, 1,499, 12,500, 1,25,000 (paise only when there are some)."""
    whole, paise = f"{abs(float(value or 0)):.2f}".split(".")
    head, groups = whole[:-3], [whole[-3:]]
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    text = ",".join(groups)
    return text if paise == "00" else f"{text}.{paise}"


def discount_words(coupon: dict) -> str:
    """The discount as the message and the dashboard write it: "10%" or "₹100"."""
    value = _num(coupon.get("discount_value"))
    return f"₹{rupees(value)}" if coupon.get("discount_type") == "amount" else f"{value}%"


def offer_text(coupon: dict) -> str:
    """The offer in one line of English, for the dashboard and the cart call's details:
    "10% off on online (prepaid) payment"."""
    text = f"{discount_words(coupon)} off"
    if coupon.get("prepaid_only"):
        text += " on online (prepaid) payment"
    if _num(coupon.get("min_order")) > 0:
        text += f" on orders of ₹{rupees(coupon['min_order'])} or more"
    return text


def state_of(coupon: dict, today: date | None = None) -> str:
    """live, off (switched off), expired (past its last day) or not_started (before its first day)."""
    today = today or _today()
    if not coupon.get("active"):
        return "off"
    until, start = _day(coupon.get("valid_until")), _day(coupon.get("valid_from"))
    if until and until < today:
        return "expired"
    if start and start > today:
        return "not_started"
    return "live"


def is_live(coupon: dict | None, today: date | None = None) -> bool:
    """Switched on and inside its days: it may be offered and sent."""
    return bool(coupon) and state_of(coupon, today) == "live"


# --- reading -------------------------------------------------------------------------------------

def _public(row) -> dict:
    """A coupon as the API and the call code see it."""
    c = dict(row)
    for key in FLAGS:
        c[key] = bool(c.get(key))
    for key in ("discount_value", "min_order"):
        c[key] = _num(c.get(key))
    for key in ("valid_from", "valid_until", "description", "message"):
        c[key] = c.get(key) or ""
    c["discount"], c["offer_text"], c["state"] = discount_words(c), offer_text(c), state_of(c)
    return c


def _has_messages(conn) -> bool:
    """messaging.init_tables() makes the messages table; a coupon list may be read before that."""
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone())


def list_coupons() -> list[dict]:
    """Every coupon, newest first, with how many messages carry it: waiting (for the service or a person) and sent."""
    counts: dict[str, dict[str, int]] = {}
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM coupons ORDER BY id DESC").fetchall()
        if _has_messages(conn):
            for r in conn.execute("SELECT UPPER(coupon_code) AS code, status, COUNT(*) AS n FROM messages "
                                  "WHERE coupon_code IS NOT NULL GROUP BY UPPER(coupon_code), status"):
                counts.setdefault(r["code"], {})[r["status"]] = r["n"]
    out = []
    for r in rows:
        c = _public(r)
        by = counts.get(c["code"].upper(), {})
        c["messages"] = {"waiting": sum(by.get(s, 0) for s in ("queued", "sending", "held")), "sent": by.get("sent", 0)}
        out.append(c)
    return out


def get_coupon(coupon_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM coupons WHERE id=?", (coupon_id,)).fetchone()
    return _public(row) if row else None


def get_by_code(code: str) -> dict | None:
    """The coupon with this code, whatever its case or spaces ("diwalidilse10" finds DIWALIDILSE10)."""
    code = re.sub(r"\s+", "", str(code or ""))
    if not code:
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM coupons WHERE code=?", (code,)).fetchone()   # the column ignores case
    return _public(row) if row else None


def call_coupon() -> dict | None:
    """The coupon the abandoned-cart agent offers: the one marked for calls, while it is live."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM coupons WHERE offer_on_calls=1 ORDER BY id DESC LIMIT 1").fetchone()
    coupon = _public(row) if row else None
    return coupon if coupon and coupon["state"] == "live" else None


def offer_for(contact: dict | None) -> dict | None:
    """The coupon this contact's call may offer, or None: only a call type that offers one (abandoned carts), the
    campaign's WhatsApp coupon (Facts & offers, as for order confirmations: owner, 22 Oct 2026; it was this page's call
    coupon), and never to a customer who already has this code. Shape: {title, code, max_discount, valid_until, details}."""
    if not contact:
        return None
    from .call_types import call_type_for, offers_of, whatsapp_offer   # here, not at the top: the call types read this module
    if not getattr(call_type_for(contact), "offers_coupon", False):
        return None
    campaign = db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None
    facts = (campaign or {}).get("facts") or {}
    coupon = whatsapp_offer(facts)
    if not coupon and not offers_of(facts):
        # Owner, 26 Oct 2026: a campaign with no offer of its own (the 26 Oct cart lists) still shares the live coupon
        # marked for calls on the Coupons page, with every customer who picked up, "not interested" too. A campaign
        # that has offers but its WhatsApp switch off stays off.
        coupon = _as_offer(call_coupon())
    if not coupon:
        return None
    from . import messaging   # here: messaging reads this module
    if messaging.already_sent(contact.get("phone") or "", coupon["code"]):
        return None
    return coupon


def _as_offer(c: dict | None) -> dict | None:
    """A Coupons page coupon in a campaign offer's shape (call_types.offers_of), as the cart call and its message read it."""
    if not c:
        return None
    return {"title": c["description"] or c["offer_text"], "code": c["code"].upper(), "max_discount": c["discount"],
            "valid_until": c["valid_until"], "details": c["offer_text"]}


def offer_by_code(contact: dict, code: str) -> dict | None:
    """The offer a code stands for, in offer_for's shape: the campaign's own offer with that code, else the Coupons
    page coupon. For a cart message already sent: the call names the discount it carried."""
    from .call_types import offers_of   # here: the call types read this module
    code = str(code or "").strip().upper()
    if not code:
        return None
    campaign = db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None
    for o in offers_of((campaign or {}).get("facts") or {}):
        if o["code"].upper() == code:
            return {**o, "code": code}
    return _as_offer(get_by_code(code))


def attach(contact: dict) -> dict:
    """The contact with "_coupon": the coupon its call may offer, or None; and, on a cart call, "_cart_sent": the cart
    message this customer already has ({"id", "code"}), or None. Fixed when the call is placed, so the prompt and the
    call's checks agree on it. Never raises: a call never fails for a coupon."""
    try:
        coupon = offer_for(contact)
    except Exception:  # noqa: BLE001
        log.exception("coupon lookup failed for contact %s; no offer on this call", (contact or {}).get("id"))
        coupon = None
    sent = None
    try:
        from .call_types import call_type_for   # here: the call types read this module
        if contact and call_type_for(contact).key == "abandoned_cart":
            from . import messaging   # here: messaging reads this module
            sent = messaging.sent_cart_message(contact)
    except Exception:  # noqa: BLE001
        log.exception("cart message lookup failed for contact %s", (contact or {}).get("id"))
    return {**(contact or {}), "_coupon": coupon, "_cart_sent": sent}


# --- checking and saving ----------------------------------------------------------------------------

def clean_code(raw) -> str:
    """A code as the customer types it at checkout: capitals, no spaces."""
    code = re.sub(r"\s+", "", str(raw or "")).upper()
    if not code:
        raise ValueError("Give the coupon a code, as the customer types it at checkout (for example DIWALIDILSE10).")
    if not CODE.match(code):
        raise ValueError(f'"{code[:50]}" cannot be a code: use 3 to 40 letters and digits (a dash or underscore may '
                         "follow the first), for example DIWALIDILSE10.")
    return code


def _flag(value, key: str) -> int:
    if value is None or str(value).strip() == "":
        return FLAGS[key]
    if isinstance(value, bool) or (isinstance(value, (int, float)) and value in (0, 1)):
        return int(value)
    text = str(value).strip().lower()
    if text in _YES:
        return 1
    if text in _NO:
        return 0
    raise ValueError(f"{key} must be true or false.")


def _date_text(value, what: str) -> str:
    """"" or YYYY-MM-DD; a date and time from another system keeps only its date."""
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", text):
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
    raise ValueError(f"{what} must be a date like 2026-11-10, or empty.")


def _message(value) -> str:
    """The coupon's own message; "" (the default message) when it is empty or the default word for word, so a later
    change to the default reaches it."""
    from .messaging import DEFAULT_TEMPLATE
    text = str(value or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"The message is at most {MAX_MESSAGE} characters (this one has {len(text)}).")
    if text == DEFAULT_TEMPLATE.strip():
        return ""
    if text and "{code}" not in text:
        raise ValueError("The message must have {code} in it, or the customer never gets the code.")
    return text


def check(coupon: dict) -> None:
    """The rules between fields, on a whole coupon: a new one, or an edit together with its other fields."""
    value = float(coupon.get("discount_value") or 0)
    if value <= 0:
        raise ValueError("The discount must be more than 0.")
    if coupon.get("discount_type") == "percent" and value > 100:
        raise ValueError("A percent discount cannot be more than 100.")
    if float(coupon.get("min_order") or 0) < 0:
        raise ValueError("The minimum order cannot be below 0 (0 means any order).")
    if coupon.get("valid_from") and coupon.get("valid_until") and coupon["valid_until"] < coupon["valid_from"]:
        raise ValueError("The last day cannot be before the first day.")


def clean(body: dict, partial: bool = False) -> dict:
    """A coupon from the dashboard form or another system, checked and tidied into what is stored. partial: an edit,
    only the fields the body has (the caller checks them with the rest). Raises ValueError saying what to fix."""
    if not isinstance(body, dict):
        raise ValueError("Send the coupon's fields as a JSON object.")
    out: dict = {}

    def given(key: str) -> bool:
        return not partial or key in body

    if given("code"):
        out["code"] = clean_code(body.get("code"))
    if given("discount_type"):
        kind = str(body.get("discount_type") or "percent").strip().lower()
        if kind not in _PERCENT + _AMOUNT:
            raise ValueError('The discount type is "percent" (% off) or "amount" (rupees off).')
        out["discount_type"] = "percent" if kind in _PERCENT else "amount"
    if given("discount_value"):
        value = parse_amount(body.get("discount_value"))
        if value is None:
            raise ValueError("Give the discount as a number: 10 for 10% off, or the rupees off.")
        out["discount_value"] = value
    if given("min_order"):
        value = parse_amount(body.get("min_order")) if str(body.get("min_order") or "").strip() else 0.0
        if value is None:
            raise ValueError("The minimum order must be a number of rupees (0 for any order).")
        out["min_order"] = value
    for key, what in (("valid_from", "The first day"), ("valid_until", "The last day")):
        if given(key):
            out[key] = _date_text(body.get(key), what)
    for key in FLAGS:
        if given(key):
            out[key] = _flag(body.get(key), key)
    if given("description"):
        text = " ".join(str(body.get("description") or "").split())
        if len(text) > MAX_DESCRIPTION:
            raise ValueError(f"The description is at most {MAX_DESCRIPTION} characters (this one has {len(text)}).")
        out["description"] = text
    if given("message"):
        out["message"] = _message(body.get("message"))
    if not partial:
        check(out)
    return out


def _code_free(conn, code: str, own_id: int = 0) -> None:
    if conn.execute("SELECT 1 FROM coupons WHERE code=? AND id!=?", (code, own_id)).fetchone():
        raise ValueError(f"A coupon with the code {code} already exists.")


def _only_one_on_calls(conn, coupon_id: int, now: str) -> None:
    """The agent offers one coupon at most: marking this one takes the mark off any other, in the same transaction."""
    n = conn.execute("UPDATE coupons SET offer_on_calls=0, updated_at=? WHERE offer_on_calls=1 AND id!=?",
                     (now, coupon_id)).rowcount
    if n:
        log.info("coupon %s is now the one offered on cart calls (taken off %s other)", coupon_id, n)


def create_coupon(body: dict) -> dict:
    """A new coupon. Raises ValueError saying what to fix."""
    c = clean(body)
    now = db.now_iso()
    try:
        with db.connect() as conn:
            _code_free(conn, c["code"])
            cur = conn.execute(f"INSERT INTO coupons ({', '.join(FIELDS)}, created_at, updated_at) "
                               f"VALUES ({', '.join('?' for _ in FIELDS)}, ?, ?)", (*(c[k] for k in FIELDS), now, now))
            if c["offer_on_calls"]:
                _only_one_on_calls(conn, cur.lastrowid, now)
    except sqlite3.IntegrityError:   # the same code added at the same moment
        raise ValueError(f"A coupon with the code {c['code']} already exists.")
    log.info("coupon %s added: %s%s", c["code"], offer_text(c), "; offered on cart calls" if c["offer_on_calls"] else "")
    return get_coupon(cur.lastrowid)


def update_coupon(coupon_id: int, body: dict) -> dict | None:
    """Change the fields the body has: an edit, or {"active": false} from the On/Off switch. None if there is no such
    coupon; ValueError saying what to fix."""
    old = get_coupon(coupon_id)
    if not old:
        return None
    changes = clean(body, partial=True)
    check({**{k: old[k] for k in FIELDS}, **changes})
    if not changes:
        return old
    now = db.now_iso()
    try:
        with db.connect() as conn:
            if "code" in changes:
                _code_free(conn, changes["code"], coupon_id)
            conn.execute(f"UPDATE coupons SET {', '.join(f'{k}=?' for k in changes)}, updated_at=? WHERE id=?",
                         (*changes.values(), now, coupon_id))
            if changes.get("offer_on_calls"):
                _only_one_on_calls(conn, coupon_id, now)
    except sqlite3.IntegrityError:
        raise ValueError(f"A coupon with the code {changes.get('code')} already exists.")
    log.info("coupon %s changed: %s", changes.get("code") or old["code"], ", ".join(changes))
    return get_coupon(coupon_id)


def delete_coupon(coupon_id: int) -> bool:
    """Gone for good. Its messages stay as a record; the ones not sent yet are cancelled, as the code is gone."""
    old = get_coupon(coupon_id)
    if not old:
        return False
    cancelled = 0
    with db.connect() as conn:
        conn.execute("DELETE FROM coupons WHERE id=?", (coupon_id,))
        if _has_messages(conn):
            cancelled = conn.execute("UPDATE messages SET status='cancelled', error=?, next_try_at=NULL "
                                     "WHERE coupon_code=? COLLATE NOCASE AND status IN ('queued', 'held')",
                                     (f"coupon {old['code']} was deleted", old["code"])).rowcount
    log.info("coupon %s deleted%s", old["code"], f"; {cancelled} message(s) not sent yet cancelled" if cancelled else "")
    return True


# --- the dashboard (Coupons page) and other systems ------------------------------------------------------

def _loose(body: dict) -> dict:
    """The form as it stands while it is being typed: whatever does not make sense yet takes a stand-in, never an error."""
    kind = str(body.get("discount_type") or "").strip().lower()
    value = parse_amount(body.get("discount_value"))
    try:
        prepaid = bool(_flag(body.get("prepaid_only"), "prepaid_only"))
    except ValueError:
        prepaid = True
    return {"code": re.sub(r"\s+", "", str(body.get("code") or "")).upper()[:40] or "YOURCODE",
            "discount_type": "amount" if kind in _AMOUNT else "percent",
            "discount_value": value if value and value > 0 else 0, "prepaid_only": prepaid,
            "min_order": max(0.0, parse_amount(body.get("min_order")) or 0.0),
            "message": str(body.get("message") or "").replace("\r\n", "\n").strip()[:MAX_MESSAGE]}


@router.get("/api/coupons")
async def coupons_page() -> dict:
    from . import messaging
    live = call_coupon()
    return {"coupons": list_coupons(), "call_coupon": live["code"] if live else None,
            "default_message": messaging.DEFAULT_TEMPLATE, "placeholders": list(messaging.PLACEHOLDERS)}


@router.post("/api/coupons")
async def add_coupon(body: dict) -> dict:
    try:
        return create_coupon(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/coupons/preview")
async def preview_message(body: dict) -> dict:
    """The message as a customer would get it, with a made-up customer, for the form while it is being typed."""
    from . import messaging
    coupon = _loose(body)
    values = messaging.sample_values(coupon)
    return {"text": messaging.render(coupon["message"] or messaging.DEFAULT_TEMPLATE, values),
            "sms": messaging.sms_text(values),
            # {words} that are not values stay as typed in the message: likely a typo ({naam} for {name})
            "unknown": sorted({k for k in re.findall(r"\{(\w+)\}", coupon["message"]) if k not in values})}


@router.put("/api/coupons/{coupon_id}")
async def edit_coupon(coupon_id: int, body: dict) -> dict:
    try:
        coupon = update_coupon(coupon_id, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if coupon is None:
        raise HTTPException(404, "No such coupon.")
    return coupon


@router.delete("/api/coupons/{coupon_id}")
async def remove_coupon(coupon_id: int) -> dict:
    if not delete_coupon(coupon_id):
        raise HTTPException(404, "No such coupon.")
    return {"ok": True}


@router.post("/api/intake/coupons")
async def intake_coupon(body: dict, x_api_key: str = Header(default="")) -> dict:
    """Another system sending a coupon code, with the same key as the contacts intake (X-Api-Key: INTAKE_API_KEY).
    The fields are the dashboard's ({"code": ..., "discount_value": ...}, or inside {"coupon": {...}}); a code
    already here is updated with the fields sent."""
    if not INTAKE_API_KEY:
        raise HTTPException(503, "The intake is closed: set INTAKE_API_KEY in .env.")
    if not hmac.compare_digest(x_api_key.encode(), INTAKE_API_KEY.encode()):
        raise HTTPException(401, "Wrong or missing X-Api-Key.")
    fields = body.get("coupon") if isinstance(body.get("coupon"), dict) else body
    try:
        old = get_by_code(clean_code(fields.get("code")))
        if old:
            return {"coupon": update_coupon(old["id"], fields), "created": False}
        return {"coupon": create_coupon(fields), "created": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
