"""The numbers behind the new dashboard's Overview page (frontend/src/app/page.tsx): headline cards with the
change against the period before, calls per hour or day for the chart, live calls and what needs attention.
Reads the SQLite database only; makes no calls to any paid service."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from . import autodial, db
from .call_types import CALL_TYPES
from .config import DIAL_ONLY_MY_NUMBER
from .live_bridge import live_snapshot

router = APIRouter()
IST = db.IST

# A conversion is a result the call type itself counts as a success, plus a yes to the coupon offer.
CONVERSIONS = {o for t in CALL_TYPES.values() for o in t.positive} | {"wants_coupon"}
NOT_CONNECTED = ("no_answer", "busy")


def _window(range_: str, day_from: str, day_to: str) -> tuple[datetime, datetime, str]:
    """(start, end, bucket) in Indian time for a period; end is exclusive. Hours for one day, days otherwise."""
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    if range_ == "today":
        return today, today + timedelta(days=1), "hour"
    if range_ == "yesterday":
        return today - timedelta(days=1), today, "hour"
    if range_ in ("7d", "30d"):
        days = int(range_[:-1])
        return today - timedelta(days=days - 1), today + timedelta(days=1), "day"
    if range_ == "custom":
        try:
            start = datetime.strptime(day_from, "%Y-%m-%d").replace(tzinfo=IST)
            end = datetime.strptime(day_to or day_from, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)
        except ValueError:
            raise HTTPException(400, "Custom dates must look like 2026-10-21.")
        if end <= start:
            raise HTTPException(400, "The end date must not be before the start date.")
        if end - start > timedelta(days=366):
            raise HTTPException(400, "Pick at most a year.")
        return start, end, "hour" if end - start <= timedelta(days=1) else "day"
    raise HTTPException(400, "range must be today, yesterday, 7d, 30d or custom.")


def _utc(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


def _rows(start: datetime, end: datetime, campaign_id: int | None) -> list:
    where, args = ["c.started_at >= ?", "c.started_at < ?"], [_utc(start), _utc(end)]
    if campaign_id:
        where.append("k.campaign_id = ?")
        args.append(campaign_id)
    with db.connect() as conn:
        return conn.execute(
            "SELECT c.started_at, c.channel, c.duration_sec, c.outcome, c.error, c.usage FROM calls c "
            "LEFT JOIN contacts k ON k.id = c.contact_id WHERE " + " AND ".join(where), args).fetchall()


def _connected(r) -> bool:
    # The same rule as db.stats: someone picked up and we heard them for a moment at least.
    return (r["duration_sec"] or 0) > 0 and r["outcome"] not in NOT_CONNECTED


def _failed(r) -> bool:
    """The call broke: the line dropped, or something failed on our side before any result."""
    return r["outcome"] == "dropped" or (not r["outcome"] and bool(r["error"]))


def _totals(rows: list) -> dict:
    return {
        "calls": len(rows),
        "connected": sum(1 for r in rows if _connected(r)),
        "conversions": sum(1 for r in rows if r["outcome"] in CONVERSIONS),
        "failed": sum(1 for r in rows if _failed(r)),
        "spend_rs": round(sum(db.call_cost(r["channel"], r["duration_sec"], r["usage"]) for r in rows), 2),
    }


def _series(rows: list, start: datetime, end: datetime, bucket: str) -> list[dict]:
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    fmt = "%H:00" if bucket == "hour" else "%d %b"
    points, at = {}, start
    while at < end:
        points[at] = {"label": at.strftime(fmt), "calls": 0, "connected": 0, "conversions": 0, "failed": 0}
        at += step
    for r in rows:
        t = datetime.fromisoformat(r["started_at"]).astimezone(IST)
        key = t.replace(minute=0, second=0, microsecond=0) if bucket == "hour" else t.replace(hour=0, minute=0, second=0, microsecond=0)
        p = points.get(key)
        if not p:
            continue
        p["calls"] += 1
        p["connected"] += _connected(r)
        p["conversions"] += r["outcome"] in CONVERSIONS
        p["failed"] += _failed(r)
    return list(points.values())


def _attention(rows: list, auto: dict, spend_today: float) -> list[dict]:
    """Things the owner should look at, most serious first. Each: level (critical/warning/info), title, detail, page."""
    items = []
    errors = [r for r in rows if r["error"]]
    if errors:
        last = max(errors, key=lambda r: r["started_at"])
        items.append({"level": "critical", "title": f"{len(errors)} call{'s' if len(errors) != 1 else ''} failed with an error",
                      "detail": str(last["error"])[:160], "page": "history"})
    missing = auto.get("phone_missing") or []
    if missing == ["PUBLIC_BASE_URL"]:   # started with run.bat: fine for browser tests, no phone calls
        items.append({"level": "info", "title": "Phone calls are off",
                      "detail": "The server was started without a public address. Use run_phone.bat to place phone calls.",
                      "page": "store"})
    elif missing:
        items.append({"level": "critical", "title": "Phone line not set up",
                      "detail": "Missing in .env: " + ", ".join(missing), "page": "store"})
    budget = (auto.get("settings") or {}).get("budget_rs") or 0
    if auto.get("running") and budget and spend_today >= 0.8 * budget:
        items.append({"level": "warning", "title": f"Daily budget {round(100 * spend_today / budget)}% used",
                      "detail": f"₹{spend_today:,.0f} of ₹{budget:,.0f} spent today", "page": "spend"})
    if auto.get("enabled") and not auto.get("running") and auto.get("reason"):
        items.append({"level": "warning", "title": "Automatic calling has stopped", "detail": auto["reason"], "page": "store"})
    if DIAL_ONLY_MY_NUMBER:
        items.append({"level": "info", "title": "Test mode is on",
                      "detail": "Every phone call rings your own number (DIAL_ONLY_MY_NUMBER).", "page": "store"})
    review = sum(1 for r in rows if r["outcome"] in ("unclear", "wrong_number", "not_ordered"))
    if review:
        items.append({"level": "info", "title": f"{review} call{'s' if review != 1 else ''} need a look",
                      "detail": "Unclear answers, wrong numbers or orders the customer did not place.", "page": "history"})
    return items


@router.get("/api/overview")
async def overview(range: str = "today", day_from: str = "", day_to: str = "", campaign_id: int = 0) -> dict:  # noqa: A002
    start, end, bucket = _window(range, day_from, day_to)
    prev_start = start - (end - start)
    camp = campaign_id or None
    rows, prev = _rows(start, end, camp), _rows(prev_start, start, camp)
    now, before = _totals(rows), _totals(prev)

    names = {c["id"]: c["name"] for c in db.list_campaigns()}
    live = []
    for c in live_snapshot():
        if camp and c.get("campaign_id") != camp:
            continue
        live.append({**c, "campaign_name": names.get(c.get("campaign_id"), "Single call" if c.get("channel") == "browser" else "")})

    auto = autodial.snapshot()
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    spend_today = _totals(_rows(today, today + timedelta(days=1), None))["spend_rs"]
    queued = sum(c["to_call"] for c in auto.get("campaigns", []) if not camp or c["id"] == camp)
    return {
        "range": range, "from": start.strftime("%Y-%m-%d"), "to": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "bucket": bucket, "now": now, "before": before,
        "live": live, "queued": queued if auto.get("running") else 0, "waiting": queued,
        "series": _series(rows, start, end, bucket),
        "attention": _attention(rows, auto, spend_today),
        "campaigns": [{"id": i, "name": n} for i, n in names.items()],
        "spend_today_rs": spend_today, "budget_rs": (auto.get("settings") or {}).get("budget_rs") or 0,
        "auto": {"enabled": auto.get("enabled"), "running": auto.get("running"), "test_mode": DIAL_ONLY_MY_NUMBER},
    }
