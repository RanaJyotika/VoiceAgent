"""SQLite storage for contacts (uploaded from Excel) and call records."""
import json
import sqlite3
from contextlib import contextmanager
import math
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR, DB_PATH, UPLOAD_DIR

CONTACT_COLUMNS = [
    "order_id", "customer_name", "phone", "product", "quantity", "amount",
    "payment_mode", "address", "delivery_date", "language", "notes",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    customer_name TEXT,
    phone TEXT,
    product TEXT,
    quantity TEXT,
    amount TEXT,
    payment_mode TEXT,
    address TEXT,
    delivery_date TEXT,
    language TEXT,
    notes TEXT,
    campaign TEXT DEFAULT 'order_confirmation',
    status TEXT DEFAULT 'pending',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER,
    channel TEXT,               -- 'browser' now, 'plivo' later
    started_at TEXT,
    ended_at TEXT,
    duration_sec REAL,
    outcome TEXT,
    outcome_details TEXT,       -- JSON from the mark_outcome tool
    transcript TEXT,            -- JSON list of {role, text}
    usage TEXT,                 -- JSON token usage if reported
    error TEXT
);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_contacts(rows: list[dict], campaign: str = "order_confirmation") -> int:
    with connect() as conn:
        for r in rows:
            conn.execute(
                f"INSERT INTO contacts ({', '.join(CONTACT_COLUMNS)}, campaign, created_at) "
                f"VALUES ({', '.join('?' for _ in CONTACT_COLUMNS)}, ?, ?)",
                [str(r.get(c, "") or "") for c in CONTACT_COLUMNS] + [campaign, now_iso()],
            )
    return len(rows)


def list_contacts() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM contacts ORDER BY id DESC")]


def get_contact(contact_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row) if row else None


def clear_contacts() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM contacts")


def clear_calls() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM calls")
        conn.execute("UPDATE contacts SET status='pending'")


def start_call(contact_id: int | None, channel: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (contact_id, channel, started_at) VALUES (?, ?, ?)",
            (contact_id, channel, now_iso()),
        )
        return cur.lastrowid


def finish_call(call_id: int, *, outcome: str | None, outcome_details: dict | None,
                transcript: list[dict], usage: dict | None, error: str | None,
                duration_sec: float) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE calls SET ended_at=?, duration_sec=?, outcome=?, outcome_details=?, "
            "transcript=?, usage=?, error=? WHERE id=?",
            (now_iso(), duration_sec, outcome, json.dumps(outcome_details or {}),
             json.dumps(transcript), json.dumps(usage or {}), error, call_id),
        )
        if outcome:
            conn.execute(
                "UPDATE contacts SET status=? WHERE id=(SELECT contact_id FROM calls WHERE id=?)",
                (outcome, call_id),
            )


def list_calls(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, k.customer_name, k.order_id, k.phone FROM calls c "
            "LEFT JOIN contacts k ON k.id = c.contact_id ORDER BY c.id DESC LIMIT ?",
            (limit,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["cost_rs"] = round(call_cost(d.get("channel") or "", d.get("duration_sec") or 0), 2)
            for key in ("outcome_details", "transcript", "usage"):
                try:
                    d[key] = json.loads(d[key]) if d[key] else None
                except (TypeError, json.JSONDecodeError):
                    pass
            out.append(d)
        return out


IST = timezone(timedelta(hours=5, minutes=30))
GEMINI_RS_PER_MIN = 1.30     # audio in + audio out, agent speaking about half the time
PLIVO_RS_PER_PULSE = 0.19    # Rs 0.38 per minute, billed in 30-second pulses


def call_cost(channel: str, seconds: float) -> float:
    cost = (seconds or 0) / 60 * GEMINI_RS_PER_MIN
    if channel == "plivo" and seconds:
        cost += math.ceil(seconds / 30) * PLIVO_RS_PER_PULSE
    return cost


def stats(period: str = "today") -> dict:
    """Headline numbers for the dashboard. period: 'today' (Indian time) or 'all'."""
    where, args = "", ()
    if period == "today":
        midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        where, args = "WHERE started_at >= ?", (midnight.astimezone(timezone.utc).isoformat(),)
    with connect() as conn:
        rows = conn.execute(f"SELECT channel, duration_sec, outcome, error FROM calls {where}", args).fetchall()
    total = len(rows)
    answered = [r for r in rows if (r["duration_sec"] or 0) > 0 and r["outcome"] != "no_answer"]
    by = {}
    for r in rows:
        key = r["outcome"] or ("error" if r["error"] else "none")
        by[key] = by.get(key, 0) + 1
    talk = sum(r["duration_sec"] or 0 for r in answered)
    return {
        "period": period, "calls": total, "answered": len(answered),
        "answer_rate": round(100 * len(answered) / total) if total else None,
        "confirmed": by.get("confirmed", 0), "changed": by.get("changed", 0),
        "cancelled": by.get("cancelled", 0), "no_answer": by.get("no_answer", 0),
        "needs_review": by.get("unclear", 0) + by.get("error", 0) + by.get("wrong_number", 0)
                        + by.get("do_not_call", 0) + by.get("call_later", 0),
        "talk_seconds": round(talk),
        "avg_seconds": round(talk / len(answered)) if answered else None,
        "cost_rs": round(sum(call_cost(r["channel"], r["duration_sec"]) for r in rows), 2),
        "phone_calls": sum(1 for r in rows if r["channel"] == "plivo"),
    }
