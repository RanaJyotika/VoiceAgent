"""Excel import for contacts. Header row must match CONTACT_COLUMNS (case-insensitive)."""
from io import BytesIO

from openpyxl import Workbook, load_workbook

from .db import CONTACT_COLUMNS

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


def parse_excel(content: bytes) -> list[dict]:
    wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if not header:
        return []
    cols = [_normalise(h) for h in header]
    rows: list[dict] = []
    for raw in rows_iter:
        if raw is None or all(v in (None, "") for v in raw):
            continue
        row = {c: (v if v is not None else "") for c, v in zip(cols, raw) if c in CONTACT_COLUMNS}
        row["phone"] = normalise_phone(row.get("phone", ""))
        if isinstance(row.get("delivery_date"), object) and hasattr(row.get("delivery_date"), "strftime"):
            row["delivery_date"] = row["delivery_date"].strftime("%d %B %Y")
        rows.append(row)
    return rows


def build_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "contacts"
    ws.append(CONTACT_COLUMNS)
    ws.append([
        "VV-1001", "Ramesh Kumar", "PUT 10 DIGIT NUMBER", "Rudraksha Mala 108 beads", "1", "1499",
        "COD", "12 MG Road, Indore, MP 452001", "22 September 2026", "hinglish",
        "First order",
    ])
    ws.append([
        "VV-1002", "Sunita Sharma", "PUT 10 DIGIT NUMBER", "Shri Yantra (brass)", "1", "2299",
        "Prepaid", "Flat 4B, Sector 21, Noida, UP 201301", "23 September 2026", "hindi", "",
    ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
