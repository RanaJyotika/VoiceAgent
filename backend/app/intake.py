"""Contacts sent by another system (the Vedic Vaibhav main database, a booking site...), as JSON
instead of an Excel sheet.

POST /api/intake/{campaign_id} with the header X-Api-Key: <INTAKE_API_KEY from .env> and a body of
{"rows": [{...}, ...]}. Each row uses the same names as the sheet's headers ("Customer name",
"phone", "booking_id", "temple", "puja_date"...; contacts.py lists every name it understands), and
anything else is kept as an extra detail, exactly as a sheet's other columns are.

A row whose order or booking id is already in the campaign is left out, so sending the same row
twice is harmless; rows without an id are matched on the phone number instead. Numbers that asked
not to be called are left out. In a campaign marked for automatic calling, the rows are rung
without anyone pressing Start.
"""
import hmac

from fastapi import APIRouter, Header, HTTPException

from . import db
from .config import INTAKE_API_KEY
from .contacts import _cell_text, _normalise, call_type_named, is_valid_phone, normalise_phone

router = APIRouter()
MAX_ROWS = 1000


def _row(raw: dict) -> dict:
    row, extra = {}, {}
    for header, value in raw.items():
        key, text = _normalise(header), _cell_text(value)
        if not key or not text:
            continue
        if key in db.CONTACT_COLUMNS:
            row[key] = text
        else:
            extra[key] = {"label": str(header), "value": text}
    row["phone"] = normalise_phone(row.get("phone", ""))
    row["extra"] = extra
    return row


@router.post("/api/intake/{campaign_id}")
async def intake(campaign_id: int, body: dict, x_api_key: str = Header(default="")) -> dict:
    if not INTAKE_API_KEY:
        raise HTTPException(503, "The intake is closed: set INTAKE_API_KEY in .env.")
    if not hmac.compare_digest(x_api_key, INTAKE_API_KEY):
        raise HTTPException(401, "Wrong or missing X-Api-Key.")
    camp = db.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "No such campaign.")
    raw_rows = body.get("rows")
    if not isinstance(raw_rows, list) or not all(isinstance(r, dict) for r in raw_rows):
        raise HTTPException(400, 'Send {"rows": [{...}, ...]}.')
    if len(raw_rows) > MAX_ROWS:
        raise HTTPException(400, f"At most {MAX_ROWS} rows at a time.")
    existing = db.list_contacts(campaign_id)
    have_ids = {c["order_id"] for c in existing if c.get("order_id")}
    have_phones = {c["phone"] for c in existing if c.get("phone")}
    blocked = db.do_not_call_phones()
    call_type = call_type_named(camp["call_type"]).key
    added, skipped = [], []
    for n, raw in enumerate(raw_rows):
        row = _row(raw)
        oid, phone = row.get("order_id", ""), row["phone"]
        if phone and phone in blocked:
            skipped.append({"row": n, "why": "number asked not to be called"})
        elif oid and oid in have_ids:
            skipped.append({"row": n, "why": f"{oid} is already in this campaign"})
        elif not oid and phone and phone in have_phones:
            skipped.append({"row": n, "why": "number already in this campaign"})
        else:
            contact_id = db.insert_contact(row, campaign_id, call_type)
            added.append({"row": n, "contact_id": contact_id, **({} if is_valid_phone(phone) else {"note": "no valid phone: needs review"})})
            have_ids.add(oid)
            have_phones.add(phone)
    return {"added": len(added), "skipped": len(skipped), "rows": added, "skipped_rows": skipped}
