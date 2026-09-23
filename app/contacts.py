"""Excel import for contacts. Header row must match CONTACT_COLUMNS (case-insensitive)."""
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook, load_workbook

from .db import CONTACT_COLUMNS, IST

ALIASES = {
    "name": "customer_name",
    "customer": "customer_name",
    "mobile": "phone",
    "phone_number": "phone",
    "contact": "phone",
    "item": "product",
    "qty": "quantity",
    "price": "amount",
    "total": "amount",
    "payment": "payment_mode",
    "cod": "payment_mode",
    "delivery": "delivery_date",
    "lang": "language",
    "order": "order_id",
    "order_no": "order_id",
    "order_number": "order_id",
    "customer_name": "customer_name",
    "full_name": "customer_name",
    "phone_no": "phone",
    "mobile_number": "phone",
    "mobile_no": "phone",
    "whatsapp": "phone",
    "product_name": "product",
    "items": "product",
    "quantity": "quantity",
    "amount": "amount",
    "order_amount": "amount",
    "order_total": "amount",
    "payment_mode": "payment_mode",
    "payment_method": "payment_mode",
    "mode": "payment_mode",
    "shipping_address": "address",
    "delivery_address": "address",
    "delivery_date": "delivery_date",
    "expected_delivery": "delivery_date",
    "remarks": "notes",
    "remark": "notes",
    "comment": "notes",
    "comments": "notes",
    "note": "notes",
}


def _normalise(header: str) -> str:
    key = str(header or "").strip().lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(key, key)


def normalise_phone(raw) -> str:
    """Return +91XXXXXXXXXX style. Accepts 10 digits, 0-prefixed, 91-prefixed, spaces, dashes."""
    text = str(raw or "").strip()
    if text.endswith(".0"):          # Excel turns numbers into floats
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    return "+" + digits


def is_valid_phone(phone: str) -> bool:
    digits = phone.lstrip("+")
    return phone.startswith("+") and digits.isdigit() and 10 <= len(digits) <= 15


def inspect_excel(content: bytes) -> dict:
    """Read the sheet and say what is in it: the rows as contacts, which headers were understood,
    which were ignored, and the sheet's own row number for each contact (for the preview)."""
    wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if not header:
        return {"rows": [], "columns": [], "ignored": []}
    raw_headers = [str(h).strip() if h not in (None, "") else "" for h in header]
    cols = [_normalise(h) for h in raw_headers]
    columns = [c for c in cols if c in CONTACT_COLUMNS]
    ignored = [h for h, c in zip(raw_headers, cols) if h and c not in CONTACT_COLUMNS]
    rows: list[dict] = []
    for n, raw in enumerate(rows_iter, start=2):
        if raw is None or all(v in (None, "") for v in raw):
            continue
        row = {c: (v if v is not None else "") for c, v in zip(cols, raw) if c in CONTACT_COLUMNS}
        row["phone"] = normalise_phone(row.get("phone", ""))
        if hasattr(row.get("delivery_date"), "strftime"):
            row["delivery_date"] = row["delivery_date"].strftime("%d %B %Y")
        for k, v in list(row.items()):
            if isinstance(v, float) and v.is_integer():
                row[k] = str(int(v))      # Excel turns 1499 into 1499.0
            elif not isinstance(v, str):
                row[k] = str(v)
        row["_row"] = n
        rows.append(row)
    return {"rows": rows, "columns": columns, "ignored": ignored}


def parse_excel(content: bytes) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in inspect_excel(content)["rows"]]


def check_rows(rows: list[dict], existing_phones: set[str]) -> list[dict]:
    """Attach a verdict to each row: ok, no_phone, duplicate (same number earlier in the sheet)
    or on_list (same number already on the call list)."""
    seen: set[str] = set()
    out = []
    for r in rows:
        p = r.get("phone", "")
        if not is_valid_phone(p):
            issue = "no_phone"
        elif p in seen:
            issue = "duplicate"
        elif p in existing_phones:
            issue = "on_list"
        else:
            issue = "ok"
        seen.add(p)
        out.append({**r, "_issue": issue})
    return out


RESULT_COLUMNS = [
    "status", "attempts", "last_call", "result", "mismatch", "new_product", "new_address",
    "new_delivery_date", "cancel_reason", "callback_time", "agent_notes", "call_length_sec", "transcript",
]


def build_results(contacts: list[dict], latest_call) -> bytes:
    """The uploaded sheet with the call results added on the right, one row per contact.
    latest_call(contact_id) -> the most recent call row for that contact, or None."""
    wb = Workbook()
    ws = wb.active
    ws.title = "results"
    ws.append(CONTACT_COLUMNS + RESULT_COLUMNS)
    for c in contacts:
        call = latest_call(c["id"]) or {}
        details = call.get("outcome_details") or {}
        lines = call.get("transcript") or []
        transcript = "\n".join(f"{'Agent' if t.get('role') == 'agent' else 'Customer'}: {t.get('text', '')}" for t in lines)
        when = c.get("last_call_at") or ""
        if when:
            try:
                when = datetime.fromisoformat(when).astimezone(IST).strftime("%d %b %Y %H:%M")
            except ValueError:
                pass
        ws.append([c.get(k, "") for k in CONTACT_COLUMNS] + [
            c.get("status", ""), c.get("attempts", 0), when, c.get("last_result", ""),
            details.get("mismatch", ""), details.get("new_product", ""), details.get("new_address", ""),
            details.get("new_delivery_date", ""), details.get("cancel_reason", ""), details.get("callback_time", ""),
            details.get("notes", ""), round(call.get("duration_sec") or 0), transcript[:32000],
        ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


TEMPLATE_COLUMNS = [c for c in CONTACT_COLUMNS if c != "language"]   # language is chosen on the dashboard


def build_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "contacts"
    ws.append(TEMPLATE_COLUMNS)
    ws.append([
        "VV-1001", "Ramesh Kumar", "PUT 10 DIGIT NUMBER", "Rudraksha Mala 108 beads", "1", "1499",
        "COD", "12 MG Road, Indore, MP 452001", "22 September 2026", "First order",
    ])
    ws.append([
        "VV-1002", "Sunita Sharma", "PUT 10 DIGIT NUMBER", "Shri Yantra (brass)", "1", "2299",
        "Prepaid", "Flat 4B, Sector 21, Noida, UP 201301", "23 September 2026", "",
    ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
