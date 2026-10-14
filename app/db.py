"""SQLite storage for contacts (uploaded from Excel) and call records."""
import json
import sqlite3
from contextlib import contextmanager
import math
from datetime import datetime, timedelta, timezone

from .config import DATA_DIR, DB_PATH, RECORDINGS_DIR, UPLOAD_DIR, USD_INR

CONTACT_COLUMNS = [
    "order_id", "customer_name", "phone", "product", "quantity", "amount",
    "payment_mode", "address", "delivery_date", "language", "notes", "product_ids",
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
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    call_type TEXT,             -- a key of call_types.CALL_TYPES
    facts TEXT,                 -- JSON: what the agent is told about this campaign (campaign invites)
    settings TEXT,              -- JSON: voice, language, hours, tries... as last started
    last_call_date TEXT,        -- YYYY-MM-DD: no calls are placed after this day (optional)
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,       -- 'autodial', 'shopify'
    value TEXT                  -- JSON
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
            "mongo_synced_at": "TEXT",  # when mongo_sync.py copied this call to MongoDB
        })
        _add_columns(conn, "contacts", {
            "attempts": "INTEGER DEFAULT 0",   # phone calls placed to this contact so far
            "last_call_at": "TEXT",
            "next_try_at": "TEXT",             # set by the campaign for no-answer / busy retries
            "last_result": "TEXT",             # one line for the team: "busy", "confirmed", "customer hung up"
            "campaign_id": "INTEGER",          # the campaign this contact belongs to
            "extra": "TEXT",                   # JSON: the sheet's own columns beyond CONTACT_COLUMNS
            "product_ids": "TEXT",             # Shopify product ids of the order or cart, comma separated (catalog.py)
        })
        _add_columns(conn, "campaigns", {
            "auto": "INTEGER DEFAULT 0",       # 1: automatic calling (autodial.py) rings this list
            "source": "TEXT DEFAULT ''",       # '' = made by hand (Excel, typed numbers); 'store' = filled from the store
        })
        # A server restart mid-campaign must not leave contacts stuck on "calling".
        conn.execute("UPDATE contacts SET status='pending' WHERE status='calling'")
        # Contacts from before campaigns existed go into one campaign of their own call type, so
        # the list, its results and its history stay exactly as they were.
        loose = conn.execute("SELECT DISTINCT COALESCE(NULLIF(campaign, ''), 'order_confirmation') AS t "
                             "FROM contacts WHERE campaign_id IS NULL").fetchall()
        for r in loose:
            cur = conn.execute("INSERT INTO campaigns (name, call_type, facts, settings, created_at) VALUES (?, ?, '{}', '{}', ?)",
                               ("Order confirmation" if r["t"] == "order_confirmation" else r["t"].replace("_", " ").capitalize(),
                                r["t"], now_iso()))
            conn.execute("UPDATE contacts SET campaign_id=? WHERE campaign_id IS NULL "
                         "AND COALESCE(NULLIF(campaign, ''), 'order_confirmation')=?", (cur.lastrowid, r["t"]))


def _add_columns(conn, table: str, columns: dict[str, str]) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# Contact statuses. The first group means "the campaign is done with this contact"; the second
# group is the call-back queue (rung again when next_try_at comes, up to the campaign's tries);
# "calling" is set while a call is ringing or in progress.
FINAL_STATUSES = ("confirmed", "changed", "cancelled", "not_ordered", "wrong_number", "do_not_call", "unclear",
                  "interested", "not_interested", "already_booked", "needs_help", "wants_to_order", "already_ordered")
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


def insert_contacts(rows: list[dict], campaign_id: int, call_type: str) -> int:
    """Add rows to a campaign. A row's "extra" (the sheet's other columns) is kept as JSON."""
    with connect() as conn:
        for r in rows:
            conn.execute(
                f"INSERT INTO contacts ({', '.join(CONTACT_COLUMNS)}, campaign, campaign_id, extra, created_at) "
                f"VALUES ({', '.join('?' for _ in CONTACT_COLUMNS)}, ?, ?, ?, ?)",
                [str(r.get(c, "") or "") for c in CONTACT_COLUMNS]
                + [call_type, campaign_id, json.dumps(r.get("extra") or {}, ensure_ascii=False), now_iso()],
            )
    return len(rows)


def insert_contact(row: dict, campaign_id: int, call_type: str) -> int:
    """Add one contact and return its id (for the systems that add contacts on their own)."""
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO contacts ({', '.join(CONTACT_COLUMNS)}, campaign, campaign_id, extra, created_at) "
            f"VALUES ({', '.join('?' for _ in CONTACT_COLUMNS)}, ?, ?, ?, ?)",
            [str(row.get(c, "") or "") for c in CONTACT_COLUMNS]
            + [call_type, campaign_id, json.dumps(row.get("extra") or {}, ensure_ascii=False), now_iso()],
        )
        return cur.lastrowid


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def list_contacts(campaign_id: int | None = None) -> list[dict]:
    """Contacts with a summary of their latest call (for the call list), one campaign or all."""
    where, args = ("WHERE k.campaign_id = ?", (campaign_id,)) if campaign_id else ("", ())
    with connect() as conn:
        rows = conn.execute(
            "SELECT k.*, c.id AS last_call_id, c.outcome_details AS last_details, c.recording AS last_recording, "
            "c.duration_sec AS last_duration, c.error AS last_error, g.name AS campaign_name "
            "FROM contacts k LEFT JOIN calls c ON c.id = (SELECT id FROM calls WHERE contact_id = k.id ORDER BY id DESC LIMIT 1) "
            f"LEFT JOIN campaigns g ON g.id = k.campaign_id {where} ORDER BY k.id DESC", args)
        out = []
        for r in rows:
            d = dict(r)
            d["last_details"] = _json(d["last_details"], {})
            d["extra"] = _json(d.get("extra"), {})
            out.append(d)
        return out


# --- campaigns -------------------------------------------------------------------------------------

def _campaign_row(r) -> dict:
    d = dict(r)
    d["facts"] = _json(d.get("facts"), {})
    d["settings"] = _json(d.get("settings"), {})
    return d


def create_campaign(name: str, call_type: str, facts: dict, last_call_date: str = "", source: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, call_type, facts, settings, last_call_date, created_at, source) VALUES (?, ?, ?, '{}', ?, ?, ?)",
            (name, call_type, json.dumps(facts, ensure_ascii=False), last_call_date or None, now_iso(), source))
        return cur.lastrowid


def get_campaign(campaign_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    return _campaign_row(row) if row else None


def update_campaign(campaign_id: int, **fields) -> None:
    """Change name, facts, settings or last_call_date."""
    sets, args = [], []
    for key in ("name", "facts", "settings", "last_call_date"):
        if key in fields:
            value = fields[key]
            sets.append(f"{key}=?")
            args.append(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else (value or None))
    if sets:
        with connect() as conn:
            conn.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id=?", (*args, campaign_id))


def set_campaign_auto(campaign_id: int, auto: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE campaigns SET auto=? WHERE id=?", (1 if auto else 0, campaign_id))


def auto_campaigns() -> list[dict]:
    """The campaigns automatic calling rings, oldest first."""
    with connect() as conn:
        return [_campaign_row(r) for r in conn.execute("SELECT * FROM campaigns WHERE auto=1 ORDER BY id")]


def get_setting(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return _json(row["value"], default) if row else default


def set_setting(key: str, value) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def delete_campaign(campaign_id: int) -> None:
    """The campaign and its contacts. Call records stay (history and spending still count them)."""
    with connect() as conn:
        conn.execute("DELETE FROM contacts WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def list_campaigns() -> list[dict]:
    """Every campaign, newest first, with how its list stands."""
    with connect() as conn:
        camps = [_campaign_row(r) for r in conn.execute("SELECT * FROM campaigns ORDER BY id DESC")]
        counts: dict[int, dict] = {}
        for r in conn.execute("SELECT campaign_id, status, COUNT(*) AS n FROM contacts GROUP BY campaign_id, status"):
            counts.setdefault(r["campaign_id"], {})[r["status"]] = r["n"]
        last = {r["campaign_id"]: r["at"] for r in conn.execute(
            "SELECT k.campaign_id, MAX(c.started_at) AS at FROM calls c JOIN contacts k ON k.id = c.contact_id GROUP BY k.campaign_id")}
    for c in camps:
        by = counts.get(c["id"], {})
        c["by_status"] = by
        c["total"] = sum(by.values())
        c["pending"] = by.get("pending", 0)
        c["last_call_at"] = last.get(c["id"])
    return camps


def do_not_call_phones() -> set[str]:
    """Numbers that asked not to be called, in any campaign. No campaign rings them again."""
    with connect() as conn:
        return {r["phone"] for r in conn.execute("SELECT DISTINCT phone FROM contacts WHERE status='do_not_call' AND phone != ''")}


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


def clear_contacts(campaign_id: int | None = None) -> None:
    with connect() as conn:
        if campaign_id:
            conn.execute("DELETE FROM contacts WHERE campaign_id=?", (campaign_id,))
        else:
            conn.execute("DELETE FROM contacts")


def clear_calls() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM calls")
        conn.execute("UPDATE contacts SET status='pending', attempts=0, last_call_at=NULL, next_try_at=NULL, last_result=NULL")


def requeue_contacts(ids: list[int] | None = None, statuses: tuple[str, ...] | None = None,
                     campaign_id: int | None = None) -> int:
    """Put contacts back in the queue to be called again (chosen by id, or by current status,
    within one campaign when campaign_id is given)."""
    with connect() as conn:
        if ids:
            marks = ",".join("?" for _ in ids)
            cur = conn.execute(f"UPDATE contacts SET status='pending', attempts=0, next_try_at=NULL WHERE id IN ({marks})", ids)
        elif statuses:
            marks = ",".join("?" for _ in statuses)
            scope, extra = (" AND campaign_id=?", (campaign_id,)) if campaign_id else ("", ())
            cur = conn.execute(f"UPDATE contacts SET status='pending', attempts=0, next_try_at=NULL "
                               f"WHERE status IN ({marks}){scope}", (*statuses, *extra))
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
            f"SELECT k.*, c.outcome_details AS last_details, c.id AS last_call_id, g.name AS campaign_name FROM contacts k "
            "LEFT JOIN calls c ON c.id = (SELECT id FROM calls WHERE contact_id = k.id ORDER BY id DESC LIMIT 1) "
            "LEFT JOIN campaigns g ON g.id = k.campaign_id "
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


# A number that asked not to be called in ANY campaign is skipped by every campaign.
_NOT_BLOCKED = "phone NOT IN (SELECT phone FROM contacts WHERE status='do_not_call' AND phone != '')"


_PICK_ORDER = {"oldest": "id", "newest": "id DESC", "random": "RANDOM()"}


def next_to_call(max_attempts: int, campaign_id: int | None = None, pick: str = "oldest", new_since: str = "",
                 new_ok: bool = True) -> dict | None:
    """The next contact the campaign should dial: never-called contacts first, then no-answer / busy retries whose
    time has come. pick: which never-called contact goes first, "oldest" (sheet order), "newest" (a fresh store
    order is the one worth confirming) or "random". new_since: only never-called contacts added from then on
    (UTC ISO time). new_ok=False: retries only (a store order limit reached for the day)."""
    scope, sargs = (" AND campaign_id=?", (campaign_id,)) if campaign_id else ("", ())
    with connect() as conn:
        if new_ok:
            since, args = (" AND created_at >= ?", (new_since,)) if new_since else ("", ())
            row = conn.execute(f"SELECT * FROM contacts WHERE status='pending' AND {_NOT_BLOCKED}{scope}{since} "
                               f"ORDER BY {_PICK_ORDER.get(pick, 'id')} LIMIT 1", (*sargs, *args)).fetchone()
            if row:
                return dict(row)
        marks = ",".join("?" for _ in RETRY_STATUSES)
        # A customer who asked to be called later is called later however many tries it took.
        row = conn.execute(
            f"SELECT * FROM contacts WHERE status IN ({marks}) AND (status='call_later' OR attempts < ?) "
            f"AND (next_try_at IS NULL OR next_try_at <= ?) AND {_NOT_BLOCKED}{scope} ORDER BY next_try_at, id LIMIT 1",
            (*RETRY_STATUSES, max_attempts, now_iso(), *sargs),
        ).fetchone()
        return dict(row) if row else None


def contact_counts(max_attempts: int, campaign_id: int | None = None) -> dict:
    """How the list stands: by status, plus what the campaign still has to do."""
    scope, sargs = (" AND campaign_id=?", (campaign_id,)) if campaign_id else ("", ())
    with connect() as conn:
        by = {r["status"]: r["n"] for r in conn.execute(
            f"SELECT status, COUNT(*) AS n FROM contacts WHERE 1=1{scope} GROUP BY status", sargs)}
        marks = ",".join("?" for _ in RETRY_STATUSES)
        retry_left = conn.execute(
            f"SELECT COUNT(*) AS n FROM contacts WHERE status IN ({marks}) AND (status='call_later' OR attempts < ?){scope}",
            (*RETRY_STATUSES, max_attempts, *sargs)).fetchone()["n"]
        retry_due = conn.execute(
            f"SELECT COUNT(*) AS n FROM contacts WHERE status IN ({marks}) AND (status='call_later' OR attempts < ?) "
            f"AND (next_try_at IS NULL OR next_try_at <= ?){scope}",
            (*RETRY_STATUSES, max_attempts, now_iso(), *sargs)).fetchone()["n"]
    return {"by_status": by, "total": sum(by.values()), "pending": by.get("pending", 0),
            "retry_left": retry_left, "retry_due": retry_due,
            "to_call": by.get("pending", 0) + retry_left}


def count_new(campaign_id: int, since: str = "") -> int:
    """Never-called contacts of a campaign, added from `since` (UTC ISO time) on."""
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND status='pending' AND created_at >= ?",
                            (campaign_id, since)).fetchone()[0]


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


def _ist_midnight(day: str, plus_days: int = 0) -> str:
    """Start of an Indian day ('YYYY-MM-DD') as the UTC text started_at is stored in."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=plus_days)
    return start.astimezone(timezone.utc).isoformat()


def list_calls(limit: int = 100, campaign_id: int | None = None,
               day_from: str | None = None, day_to: str | None = None) -> list[dict]:
    """Newest first. day_from and day_to are Indian dates, both days included."""
    where, args = [], []
    if campaign_id:
        where.append("k.campaign_id = ?")
        args.append(campaign_id)
    if day_from:
        where.append("c.started_at >= ?")
        args.append(_ist_midnight(day_from))
    if day_to:
        where.append("c.started_at < ?")
        args.append(_ist_midnight(day_to, 1))
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, k.customer_name, k.order_id, k.phone, k.campaign_id, g.name AS campaign_name FROM calls c "
            "LEFT JOIN contacts k ON k.id = c.contact_id LEFT JOIN campaigns g ON g.id = k.campaign_id "
            f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY c.id DESC LIMIT ?",
            (*args, limit),
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


def _as_usage(usage):
    if isinstance(usage, str):
        try:
            return json.loads(usage)
        except json.JSONDecodeError:
            return None
    return usage


def cost_parts(channel: str, seconds: float, usage=None) -> tuple[float, float]:
    """Rupees for one call, split into the voice agent and the phone line. The agent's figure is
    the real token cost when the model reported one, else a per-minute guess."""
    usage = _as_usage(usage)
    agent = usage["cost_usd"] * USD_INR if _has_cost(usage) else (seconds or 0) / 60 * GEMINI_RS_PER_MIN
    phone = math.ceil(seconds / 30) * PLIVO_RS_PER_PULSE if channel == "plivo" and seconds else 0.0
    return agent, phone


def call_cost(channel: str, seconds: float, usage=None) -> float:
    """Rupees for one call, agent plus phone line."""
    return sum(cost_parts(channel, seconds, usage))


def call_agent(usage) -> tuple[str, str]:
    """(model, provider) that answered a call. Calls made before this was recorded say so."""
    usage = _as_usage(usage)
    if not isinstance(usage, dict):
        return "not recorded", "unknown"
    model = usage.get("model") or ""
    provider = usage.get("provider") or ("openai" if "gpt" in model else "gemini" if "gemini" in model else "")
    return model or "not recorded", provider or "unknown"


def spend(days: int = 7) -> dict:
    """What the calling actually cost: today's total, a day by day list, a split per model, and
    every call with the agent that answered it and its own price."""
    start = (datetime.now(IST) - timedelta(days=max(1, days) - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.channel, c.started_at, c.duration_sec, c.outcome, c.usage, k.customer_name "
            "FROM calls c LEFT JOIN contacts k ON k.id = c.contact_id "
            "WHERE c.started_at >= ? ORDER BY c.id DESC",
            (start.astimezone(timezone.utc).isoformat(),)).fetchall()

    today_key = datetime.now(IST).strftime("%Y-%m-%d")
    calls, per_day, per_model = [], {}, {}
    for r in rows:
        started = datetime.fromisoformat(r["started_at"]).astimezone(IST) if r["started_at"] else None
        seconds = r["duration_sec"] or 0
        agent_rs, phone_rs = cost_parts(r["channel"] or "", seconds, r["usage"])
        model, provider = call_agent(r["usage"])
        usage = _as_usage(r["usage"]) or {}
        calls.append({
            "id": r["id"], "day": started.strftime("%Y-%m-%d") if started else "",
            "time": started.strftime("%H:%M") if started else "", "date": started.strftime("%d %b") if started else "",
            "customer": r["customer_name"] or "", "channel": r["channel"] or "",
            "seconds": round(seconds), "outcome": r["outcome"] or "",
            "model": model, "provider": provider, "turns": usage.get("turns"),
            "agent_rs": round(agent_rs, 2), "phone_rs": round(phone_rs, 2),
            "total_rs": round(agent_rs + phone_rs, 2), "estimated": not _has_cost(usage),
        })
        key = started.strftime("%Y-%m-%d") if started else "unknown"
        day = per_day.setdefault(key, {"day": key, "label": started.strftime("%a %d %b") if started else "unknown",
                                       "calls": 0, "answered": 0, "seconds": 0,
                                       "agent_rs": 0.0, "phone_rs": 0.0, "total_rs": 0.0})
        day["calls"] += 1
        day["seconds"] += seconds
        day["answered"] += 1 if seconds > 0 else 0
        day["agent_rs"] += agent_rs
        day["phone_rs"] += phone_rs
        day["total_rs"] += agent_rs + phone_rs

        m = per_model.setdefault(model, {"model": model, "provider": provider, "calls": 0, "seconds": 0,
                                         "agent_rs": 0.0, "phone_rs": 0.0, "total_rs": 0.0})
        m["calls"] += 1
        m["seconds"] += seconds
        m["agent_rs"] += agent_rs
        m["phone_rs"] += phone_rs
        m["total_rs"] += agent_rs + phone_rs

    def tidy(d):
        out = dict(d)
        for k in ("agent_rs", "phone_rs", "total_rs"):
            out[k] = round(out[k], 2)
        out["minutes"] = round(out["seconds"] / 60, 1)
        if out.get("calls"):
            out["per_call_rs"] = round(out["total_rs"] / out["calls"], 2)
        return out

    days_list = [tidy(d) for d in sorted(per_day.values(), key=lambda x: x["day"], reverse=True)]
    models_list = [tidy(m) for m in sorted(per_model.values(), key=lambda x: -x["total_rs"])]
    today = next((d for d in days_list if d["day"] == today_key), None) or {
        "day": today_key, "label": "today", "calls": 0, "answered": 0, "seconds": 0, "minutes": 0.0,
        "agent_rs": 0.0, "phone_rs": 0.0, "total_rs": 0.0}
    return {"today": today, "days": days_list, "models": models_list, "calls": calls,
            "window_days": days, "usd_inr": USD_INR}


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
        "interested": by.get("interested", 0) + by.get("already_booked", 0) + by.get("wants_to_order", 0) + by.get("already_ordered", 0),
        "not_interested": by.get("not_interested", 0),
        "cancelled": by.get("cancelled", 0), "no_answer": by.get("no_answer", 0) + by.get("busy", 0),
        "busy": by.get("busy", 0), "dropped": by.get("dropped", 0), "call_later": by.get("call_later", 0),
        "call_back": call_back,
        "needs_review": by.get("unclear", 0) + by.get("error", 0) + by.get("wrong_number", 0) + by.get("do_not_call", 0)
        + by.get("not_ordered", 0),
        "talk_seconds": round(talk),
        "avg_seconds": round(talk / len(answered)) if answered else None,
        "cost_rs": round(sum(call_cost(r["channel"], r["duration_sec"], r["usage"]) for r in rows), 2),
        "phone_calls": sum(1 for r in rows if r["channel"] == "plivo"),
    }
