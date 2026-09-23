"""SQLite storage for contacts (uploaded from Excel) and call records."""
import json
import sqlite3
from contextlib import contextmanager
import math
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR, DB_PATH, RECORDINGS_DIR, UPLOAD_DIR, USD_INR

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
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Columns added later; older databases need them.
        _add_columns(conn, "calls", {
            "recording": "TEXT",        # file name under data/recordings
            "hangup_cause": "TEXT",     # Plivo's reason: USER_BUSY, NO_ANSWER, NORMAL_CLEARING...
        })
        _add_columns(conn, "contacts", {
            "attempts": "INTEGER DEFAULT 0",   # phone calls placed to this contact so far
            "last_call_at": "TEXT",
            "next_try_at": "TEXT",             # set by the campaign for no-answer / busy retries
            "last_result": "TEXT",             # one line for the team: "busy", "confirmed", "customer hung up"
        })
        # A server restart mid-campaign must not leave contacts stuck on "calling".
        conn.execute("UPDATE contacts SET status='pending' WHERE status='calling'")


def _add_columns(conn, table: str, columns: dict[str, str]) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# Contact statuses. The first group means "the campaign is done with this contact"; the second
# group is the call-back queue (rung again when next_try_at comes, up to the campaign's tries);
# "calling" is set while a call is ringing or in progress.
FINAL_STATUSES = ("confirmed", "changed", "cancelled", "wrong_number", "do_not_call", "unclear")
RETRY_STATUSES = ("no_answer", "busy", "dropped", "call_later")


def set_recording(call_id: int, filename: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE calls SET recording=? WHERE id=?", (filename, call_id))


def get_recording(call_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT recording FROM calls WHERE id = ?", (call_id,)).fetchone()
        return row["recording"] if row else None


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
    """Every contact with a summary of its latest call (for the board on the dashboard)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT k.*, c.id AS last_call_id, c.outcome_details AS last_details, c.recording AS last_recording, "
            "c.duration_sec AS last_duration, c.error AS last_error "
            "FROM contacts k LEFT JOIN calls c ON c.id = (SELECT id FROM calls WHERE contact_id = k.id ORDER BY id DESC LIMIT 1) "
            "ORDER BY k.id DESC")
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["last_details"] = json.loads(d["last_details"]) if d["last_details"] else {}
            except (TypeError, json.JSONDecodeError):
                d["last_details"] = {}
            out.append(d)
        return out


def get_call(call_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT c.*, k.customer_name, k.order_id, k.phone FROM calls c "
            "LEFT JOIN contacts k ON k.id = c.contact_id WHERE c.id = ?", (call_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for key in ("outcome_details", "transcript", "usage"):
        try:
            d[key] = json.loads(d[key]) if d[key] else None
        except (TypeError, json.JSONDecodeError):
            pass
    d["cost_rs"] = round(call_cost(d.get("channel") or "", d.get("duration_sec") or 0, d.get("usage")), 2)
    d["cost_estimated"] = not _has_cost(d.get("usage"))
    return d


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
        conn.execute("UPDATE contacts SET status='pending', attempts=0, last_call_at=NULL, next_try_at=NULL, last_result=NULL")


def requeue_contacts(ids: list[int] | None = None, statuses: tuple[str, ...] | None = None) -> int:
    """Put contacts back in the queue to be called again (chosen by id, or by current status)."""
    with connect() as conn:
        if ids:
            marks = ",".join("?" for _ in ids)
            cur = conn.execute(f"UPDATE contacts SET status='pending', attempts=0, next_try_at=NULL WHERE id IN ({marks})", ids)
        elif statuses:
            marks = ",".join("?" for _ in statuses)
            cur = conn.execute(f"UPDATE contacts SET status='pending', attempts=0, next_try_at=NULL WHERE status IN ({marks})", statuses)
        else:
            return 0
        return cur.rowcount


def mark_calling(contact_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE contacts SET status='calling', last_call_at=?, attempts=attempts+1 WHERE id=?",
                     (now_iso(), contact_id))


def finish_contact_without_call(contact_id: int, note: str) -> None:
    """A row the campaign cannot ring at all (no usable phone number): out of the queue, for review."""
    with connect() as conn:
        conn.execute("UPDATE contacts SET status='wrong_number', last_result=? WHERE id=?", (note[:200], contact_id))


def schedule_retry(contact_id: int, minutes: float) -> str:
    due = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    with connect() as conn:
        conn.execute("UPDATE contacts SET next_try_at=? WHERE id=?", (due, contact_id))
    return due


def set_next_try(contact_id: int, when: datetime | None) -> None:
    """The owner set (or cleared) the call-back time by hand. Clearing means 'call as soon as possible'."""
    with connect() as conn:
        conn.execute("UPDATE contacts SET next_try_at=? WHERE id=?",
                     (when.astimezone(timezone.utc).isoformat() if when else None, contact_id))


def give_up(contact_id: int, note: str = "") -> None:
    """Take a contact out of the call-back queue: it goes to Needs review for a person to decide."""
    with connect() as conn:
        conn.execute("UPDATE contacts SET status='unclear', next_try_at=NULL, last_result=? WHERE id=?",
                     ((note or "taken off the call-back list")[:200], contact_id))


def callbacks() -> list[dict]:
    """The call-back queue: everyone waiting for another call, soonest first."""
    marks = ",".join("?" for _ in RETRY_STATUSES)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT k.*, c.outcome_details AS last_details, c.id AS last_call_id FROM contacts k "
            "LEFT JOIN calls c ON c.id = (SELECT id FROM calls WHERE contact_id = k.id ORDER BY id DESC LIMIT 1) "
            f"WHERE k.status IN ({marks}) ORDER BY (k.next_try_at IS NULL) DESC, k.next_try_at, k.id", RETRY_STATUSES)
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["last_details"] = json.loads(d["last_details"]) if d["last_details"] else {}
            except (TypeError, json.JSONDecodeError):
                d["last_details"] = {}
            out.append(d)
        return out


def next_to_call(max_attempts: int) -> dict | None:
    """The next contact the campaign should dial: never-called contacts first (in sheet order),
    then no-answer / busy retries whose time has come."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if row:
            return dict(row)
        marks = ",".join("?" for _ in RETRY_STATUSES)
        # A customer who asked to be called later is called later however many tries it took.
        row = conn.execute(
            f"SELECT * FROM contacts WHERE status IN ({marks}) AND (status='call_later' OR attempts < ?) "
            "AND (next_try_at IS NULL OR next_try_at <= ?) ORDER BY next_try_at, id LIMIT 1",
            (*RETRY_STATUSES, max_attempts, now_iso()),
        ).fetchone()
        return dict(row) if row else None


def contact_counts(max_attempts: int) -> dict:
    """How the list stands: by status, plus what the campaign still has to do."""
    with connect() as conn:
        by = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM contacts GROUP BY status")}
        marks = ",".join("?" for _ in RETRY_STATUSES)
        retry_left = conn.execute(
            f"SELECT COUNT(*) AS n FROM contacts WHERE status IN ({marks}) AND (status='call_later' OR attempts < ?)",
            (*RETRY_STATUSES, max_attempts)).fetchone()["n"]
        retry_due = conn.execute(
            f"SELECT COUNT(*) AS n FROM contacts WHERE status IN ({marks}) AND (status='call_later' OR attempts < ?) "
            "AND (next_try_at IS NULL OR next_try_at <= ?)",
            (*RETRY_STATUSES, max_attempts, now_iso())).fetchone()["n"]
    return {"by_status": by, "total": sum(by.values()), "pending": by.get("pending", 0),
            "retry_left": retry_left, "retry_due": retry_due,
            "to_call": by.get("pending", 0) + retry_left}


def latest_call_for_contact(contact_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM calls WHERE contact_id=? ORDER BY id DESC LIMIT 1", (contact_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for key in ("outcome_details", "transcript", "usage"):
            try:
                d[key] = json.loads(d[key]) if d[key] else None
            except (TypeError, json.JSONDecodeError):
                pass
        return d


def start_call(contact_id: int | None, channel: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (contact_id, channel, started_at) VALUES (?, ?, ?)",
            (contact_id, channel, now_iso()),
        )
        return cur.lastrowid


def finish_call(call_id: int, *, outcome: str | None, outcome_details: dict | None,
                transcript: list[dict], usage: dict | None, error: str | None,
                duration_sec: float, hangup_cause: str | None = None, result: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE calls SET ended_at=?, duration_sec=?, outcome=?, outcome_details=?, "
            "transcript=?, usage=?, error=?, hangup_cause=? WHERE id=?",
            (now_iso(), duration_sec, outcome, json.dumps(outcome_details or {}),
             json.dumps(transcript), json.dumps(usage or {}), error, hangup_cause, call_id),
        )
        row = conn.execute("SELECT contact_id, channel FROM calls WHERE id=?", (call_id,)).fetchone()
        if not row or not row["contact_id"]:
            return
        if outcome:
            status = outcome
        elif error and duration_sec > 0:
            status = "unclear"        # the line failed mid-conversation: a person should look
        else:
            status = "pending"        # our own failure before anyone answered: try again later
        sets, args = ["status=?", "last_result=?"], [status, result or error or outcome or ""]
        if error and duration_sec == 0 and row["channel"] == "plivo":
            sets.append("attempts=MAX(attempts-1, 0)")   # not the customer's missed call
        conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id=?", (*args, row["contact_id"]))


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
            for key in ("outcome_details", "transcript", "usage"):
                try:
                    d[key] = json.loads(d[key]) if d[key] else None
                except (TypeError, json.JSONDecodeError):
                    pass
            d["cost_rs"] = round(call_cost(d.get("channel") or "", d.get("duration_sec") or 0, d.get("usage")), 2)
            d["cost_estimated"] = not _has_cost(d.get("usage"))
            out.append(d)
        return out


IST = timezone(timedelta(hours=5, minutes=30))
# Fallback for calls that have no token report (older calls, calls that failed early). Gemini
# bills every turn for the whole conversation so far, so the real figure grows with call length;
# on 21 Sep 2026 Google billed about Rs 700 for 22 minutes of calls, roughly Rs 32 a minute,
# against the Rs 1.30 a minute this used to assume.
GEMINI_RS_PER_MIN = 32.0
PLIVO_RS_PER_PULSE = 0.19    # Rs 0.38 per minute, billed in 30-second pulses


def _has_cost(usage) -> bool:
    return isinstance(usage, dict) and usage.get("cost_usd") is not None


def call_cost(channel: str, seconds: float, usage=None) -> float:
    """Rupees for one call. Real token cost when Gemini reported usage, else a per-minute guess."""
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            usage = None
    if _has_cost(usage):
        cost = usage["cost_usd"] * USD_INR
    else:
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
        rows = conn.execute(f"SELECT channel, duration_sec, outcome, error, usage FROM calls {where}", args).fetchall()
    total = len(rows)
    answered = [r for r in rows if (r["duration_sec"] or 0) > 0 and r["outcome"] not in ("no_answer", "busy")]
    call_back = sum(1 for r in rows if r["outcome"] in ("no_answer", "busy", "dropped", "call_later"))
    by = {}
    for r in rows:
        key = r["outcome"] or ("error" if r["error"] else "none")
        by[key] = by.get(key, 0) + 1
    talk = sum(r["duration_sec"] or 0 for r in answered)
    return {
        "period": period, "calls": total, "answered": len(answered),
        "answer_rate": round(100 * len(answered) / total) if total else None,
        "confirmed": by.get("confirmed", 0), "changed": by.get("changed", 0),
        "cancelled": by.get("cancelled", 0), "no_answer": by.get("no_answer", 0) + by.get("busy", 0),
        "busy": by.get("busy", 0), "dropped": by.get("dropped", 0), "call_later": by.get("call_later", 0),
        "call_back": call_back,
        "needs_review": by.get("unclear", 0) + by.get("error", 0) + by.get("wrong_number", 0) + by.get("do_not_call", 0),
        "talk_seconds": round(talk),
        "avg_seconds": round(talk / len(answered)) if answered else None,
        "cost_rs": round(sum(call_cost(r["channel"], r["duration_sec"], r["usage"]) for r in rows), 2),
        "phone_calls": sum(1 for r in rows if r["channel"] == "plivo"),
    }
