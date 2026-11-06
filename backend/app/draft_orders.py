"""Draft orders: abandoned-cart customers who chose cash on delivery on a keypad call and confirmed their address
(owner, 3 Nov 2026). The call books it here instead of sending the cart link on WhatsApp; the team creates the real
order in the store by hand and ticks it off on the dashboard's Draft orders page. Nothing is sent to the store."""
import json
import logging

from fastapi import APIRouter, HTTPException

from . import db

log = logging.getLogger("draft_orders")
router = APIRouter()

SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER,
    call_id INTEGER UNIQUE,      -- one draft per call
    campaign_id INTEGER,
    customer_name TEXT,
    phone TEXT,
    product TEXT,
    product_ids TEXT,
    quantity TEXT,
    amount TEXT,
    payment TEXT,                -- COD
    address TEXT,
    address_source TEXT,         -- on_record (confirmed on the call), taken_on_call, missing
    cart_url TEXT,
    notes TEXT,
    created_at TEXT,
    order_created INTEGER DEFAULT 0,   -- ticked by the team once the order is made in the store
    order_ref TEXT,              -- the store's order number, if they type it
    order_created_at TEXT
);
"""
_ready = False


def _ensure() -> None:
    global _ready
    if not _ready:
        with db.connect() as conn:
            conn.executescript(SCHEMA)
        _ready = True


def _extra(contact: dict, key: str) -> str:
    extra = contact.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except ValueError:
            extra = {}
    return str((extra or {}).get(key) or "").strip() if isinstance(extra, dict) else ""


def create(*, contact: dict, call_id: int | None, address: str, address_source: str, notes: str = "") -> int | None:
    """Save the draft order a call booked; its id. Never raises (a call is saved whatever happens here)."""
    try:
        _ensure()
        with db.connect() as conn:
            if call_id is not None:
                row = conn.execute("SELECT id FROM draft_orders WHERE call_id=?", (call_id,)).fetchone()
                if row:
                    return row["id"]
            cur = conn.execute(
                "INSERT INTO draft_orders (contact_id, call_id, campaign_id, customer_name, phone, product, product_ids, "
                "quantity, amount, payment, address, address_source, cart_url, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'COD', ?, ?, ?, ?, ?)",
                (contact.get("id"), call_id, contact.get("campaign_id"), str(contact.get("customer_name") or "").strip(),
                 str(contact.get("phone") or "").strip(), str(contact.get("product") or "").strip(),
                 str(contact.get("product_ids") or "").strip(), str(contact.get("quantity") or "1").strip() or "1",
                 str(contact.get("amount") or "").strip(), address.strip(), address_source,
                 _extra(contact, "cart_url") or str(contact.get("cart_url") or "").strip(), notes, db.now_iso()))
            draft_id = cur.lastrowid
        log.info("draft order %s booked on call %s for contact %s (address %s)", draft_id, call_id, contact.get("id"),
                 address_source)
        return draft_id
    except Exception:  # noqa: BLE001
        log.exception("call %s: could not save the draft order", call_id)
        return None


@router.get("/api/draft-orders")
async def list_drafts(show: str = "all") -> dict:
    """The page's list: every draft order, newest first, with how many are still to be created."""
    _ensure()
    where = {"pending": "WHERE d.order_created=0", "created": "WHERE d.order_created=1"}.get(show, "")
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT d.*, c.name AS campaign_name FROM draft_orders d LEFT JOIN campaigns c ON c.id=d.campaign_id "
            f"{where} ORDER BY d.id DESC LIMIT 2000")]
        counts = conn.execute("SELECT COUNT(*) AS total, COALESCE(SUM(order_created=0), 0) AS pending "
                              "FROM draft_orders").fetchone()
    return {"rows": rows, "total": counts["total"], "pending": counts["pending"]}


@router.post("/api/draft-orders/{draft_id}")
async def update_draft(draft_id: int, body: dict) -> dict:
    """Tick or untick "order created" (and the store's order number, if typed)."""
    _ensure()
    created = bool(body.get("order_created"))
    ref = str(body.get("order_ref") or "").strip()[:60]
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM draft_orders WHERE id=?", (draft_id,)).fetchone():
            raise HTTPException(404, "No such draft order.")
        conn.execute("UPDATE draft_orders SET order_created=?, order_ref=?, order_created_at=? WHERE id=?",
                     (int(created), ref or None, db.now_iso() if created else None, draft_id))
    log.info("draft order %s marked %s%s", draft_id, "created" if created else "not created", f" ({ref})" if ref else "")
    return {"ok": True}
