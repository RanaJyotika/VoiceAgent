"""A campaign's contacts fetched from the store: orders from the main database (read only), between two days, as
contact rows (store_orders.order_row). The new dashboard's campaign wizard, Audience step, "Fetch from store".
Nothing is called here; the orders only join the campaign's list."""
from fastapi import APIRouter, HTTPException

from . import db, orders_view, store_orders
from .contacts import is_valid_phone

router = APIRouter()
PAYMENTS = ("cod", "prepaid", "all")
TAG_MODES = ("only", "except")   # only orders that are any of the tags, or every order except those
SHOWN = 50   # rows shown in the preview; all of them are added
MAX_SHOWN = 3000   # orders listed back after a fetch (all of them are counted)


def _campaign(campaign_id: int) -> dict:
    camp = db.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "No such campaign.")
    if not store_orders.enabled():
        raise HTTPException(400, "The store database is not set up (MONGODB_URI in .env).")
    return camp


def _tags_of(o: dict, store: dict) -> list[str]:
    """An order's tags as the Store orders page names them (store_orders.SKIP_RULES): its payment kind, if one of
    those, and the store's own tags (risk, address, WhatsApp COD confirmation...) from the fuller copy."""
    keys = [store_orders.payment_kind(o), *((store.get(o.get("name")) or {}).get("tags") or [])]
    return [k for k in store_orders.SKIP_RULES if k in keys]


async def _rows(camp: dict, day_from: str, day_to: str, payment: str, include_listed: bool,
                tags: list[str] | None = None, tag_mode: str = "only") -> list[dict]:
    """Every order of those days that passes the payment and tag filters, as a contact row, with why it would be
    left out ("" = it is added)."""
    if payment not in PAYMENTS:
        raise HTTPException(400, "payment must be cod, prepaid or all.")
    if tag_mode not in TAG_MODES:
        raise HTTPException(400, "tag_mode must be only or except.")
    wanted = {t for t in (tags or []) if t in store_orders.SKIP_RULES}
    start, end = orders_view._range(day_from, day_to)
    orders = await orders_view._orders(start, end)
    store = await orders_view._store_side([str(o.get("name")) for o in orders if o.get("name")]) if orders else {}
    with db.connect() as conn:
        here = {r[0] for r in conn.execute("SELECT order_id FROM contacts WHERE campaign_id=? AND order_id != ''", (camp["id"],))}
        elsewhere = {r[0] for r in conn.execute("SELECT order_id FROM contacts WHERE campaign_id IS NOT ? AND order_id != ''",
                                                 (camp["id"],))}
    blocked = db.do_not_call_phones()
    out, seen = [], set()
    for o in orders:
        row = store_orders.order_row(o)
        kind = store_orders.payment_kind(o)
        if payment == "cod" and row["payment_mode"] != "COD":
            continue
        if payment == "prepaid" and row["payment_mode"] != "Prepaid":
            continue
        order_tags = _tags_of(o, store)
        if wanted and (tag_mode == "only") != bool(wanted & set(order_tags)):
            continue
        oid = row["order_id"]
        if not is_valid_phone(row["phone"]):
            issue = "no_phone"
        elif oid in here or oid in seen:
            issue = "on_list"
        elif oid in elsewhere and not include_listed:
            issue = "elsewhere"
        elif row["phone"] in blocked:
            issue = "do_not_call"
        else:
            issue = ""
        seen.add(oid)
        out.append({**row, "payment_kind": kind, "placed": store_orders._placed(o), "issue": issue,
                    "tags": [store_orders.SKIP_RULES[k] for k in order_tags]})
    return out


@router.get("/api/store/order-tags")
async def order_tags() -> list[dict]:
    """The tags an order can carry, as the Store orders page names them: the "Fetch from store" filter chips."""
    return [{"key": k, "label": v} for k, v in store_orders.SKIP_RULES.items()]


def _tag_list(raw) -> list[str]:
    return [t.strip() for t in (raw.split(",") if isinstance(raw, str) else raw or []) if str(t).strip()]


@router.get("/api/campaigns/{campaign_id}/store-preview")
async def store_preview(campaign_id: int, day_from: str, day_to: str, payment: str = "cod", include_listed: bool = False,
                        tags: str = "", tag_mode: str = "only") -> dict:
    """What "Fetch from store" would add: how many orders, how many are left out and why, and the first few.
    tags: comma-separated keys of /api/store/order-tags; tag_mode only (any of them) or except (none of them)."""
    camp = _campaign(campaign_id)
    rows = await _rows(camp, day_from, day_to, payment, include_listed, _tag_list(tags), tag_mode)
    counts = {k: sum(1 for r in rows if r["issue"] == k) for k in ("", "no_phone", "on_list", "elsewhere", "do_not_call")}
    return {"total": len(rows), "add": counts.pop(""), "left_out": counts,
            "rows": [{k: r[k] for k in ("order_id", "customer_name", "phone", "product", "amount", "payment_kind", "placed", "issue", "tags")}
                     for r in rows[:SHOWN]]}


@router.post("/api/campaigns/{campaign_id}/from-store")
async def from_store(campaign_id: int, body: dict) -> dict:
    """Add those orders to the campaign's list: these are the customers the campaign calls. Nothing is rung until the
    campaign starts. Says how many were added, and why the rest were left out."""
    camp = _campaign(campaign_id)
    rows = await _rows(camp, str(body.get("day_from") or ""), str(body.get("day_to") or ""),
                       str(body.get("payment") or "cod"), bool(body.get("include_listed")),
                       _tag_list(body.get("tags")), str(body.get("tag_mode") or "only"))
    keep = [{k: v for k, v in r.items() if k not in ("issue", "payment_kind", "placed", "tags")} for r in rows if not r["issue"]]
    n = db.insert_contacts(keep, camp["id"], camp["call_type"]) if keep else 0
    left = {k: sum(1 for r in rows if r["issue"] == k) for k in ("no_phone", "on_list", "elsewhere", "do_not_call")}
    # Every order the filters matched, added or not, so the page can show them all with what happened to each.
    shown = [{**{k: r[k] for k in ("order_id", "customer_name", "phone", "product", "amount", "payment_kind", "tags")},
              "result": "added" if not r["issue"] else r["issue"]} for r in rows[:MAX_SHOWN]]
    return {"imported": n, "skipped": len(rows) - n, "total": len(rows), "left_out": left, "rows": shown}
