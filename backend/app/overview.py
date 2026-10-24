"""The numbers behind the new dashboard's Overview page (frontend/src/app/page.tsx): headline cards with the
change against the period before, calls per hour or day for the chart, live calls and what needs attention.
Reads the SQLite database only; makes no calls to any paid service."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from . import autodial, campaign, db, usage
from .call_types import CALL_TYPES
from .config import AGENT_LABELS, DIAL_ONLY_MY_NUMBER
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
    items += usage.alerts()   # the workspace budget (Usage page): an alert level passed
    review = sum(1 for r in rows if r["outcome"] in ("unclear", "wrong_number", "not_ordered"))
    if review:
        items.append({"level": "info", "title": f"{review} call{'s' if review != 1 else ''} need a look",
                      "detail": "Unclear answers, wrong numbers or orders the customer did not place.", "page": "history"})
    return items


# --- the Overview's "Today" cards (owner, 21 Oct 2026) ---------------------------------------------------------
# Per customer, by the result of their latest call today: a customer rung twice (no answer, then confirmed) is one
# customer, convinced. Phone calls only: browser tests are not customers. Costs count every call, tests included.
REVIEW_OUTCOMES = ("unclear", "wrong_number", "do_not_call", "not_ordered", "complaint")   # as db.stats' "needs review"


@router.get("/api/overview/today")
async def overview_today() -> dict:
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    with db.connect() as conn:
        rows = conn.execute("SELECT id, contact_id, channel, duration_sec, outcome, error, usage FROM calls "
                            "WHERE started_at >= ? ORDER BY id", (_utc(today),)).fetchall()
    latest: dict[int, dict] = {}
    agent_rs = line_rs = 0.0
    for r in rows:
        agent, line = db.cost_parts(r["channel"], r["duration_sec"], r["usage"])
        agent_rs += agent
        line_rs += line
        if r["channel"] == "plivo" and r["contact_id"]:
            latest[r["contact_id"]] = r   # ordered by id: the last one wins
    last = list(latest.values())
    outcome = lambda r: r["outcome"] or ("error" if r["error"] else "")  # noqa: E731
    return {
        "called": len(last),
        "convinced": sum(1 for r in last if outcome(r) in CONVERSIONS),
        "not_answered": sum(1 for r in last if outcome(r) == "no_answer"),
        "busy": sum(1 for r in last if outcome(r) == "busy"),
        "needs_review": sum(1 for r in last if outcome(r) in (*REVIEW_OUTCOMES, "error")),
        # The rest of the customers called, so the cards add up: hung up before a result, or asked for a later call.
        "hung_up": sum(1 for r in last if outcome(r) == "dropped"),
        "call_later": sum(1 for r in last if outcome(r) == "call_later"),
        "agent_rs": round(agent_rs, 2), "line_rs": round(line_rs, 2),
        "calls": sum(1 for r in rows if r["channel"] == "plivo"),   # phone calls placed today
    }


# --- the Campaigns page: each campaign's status, how its list went and what it cost -----------------

NOT_CONNECTED_STATUSES = ("no_answer", "busy")
HUNG_UP_STATUSES = ("dropped",)   # the call ended before any result: the customer hung up or the line dropped
NOT_CALLED_STATUSES = ("pending",)


def _status(c: dict, counts: dict, running: bool) -> str:
    """running · scheduled (set to start at a time) · completed (every customer has had a first call, or past its last
    day; call-backs may still be waiting, shown beside it) · paused (started, some never called yet) · draft (never
    started: no contacts yet, or not launched). Owner, 24 Oct 2026: a list rung end to end showed "Paused" because
    its no-answers had been put back on the list."""
    if running:
        return "running"
    if c.get("start_at"):
        return "scheduled"
    if not counts["total"]:
        return "draft"
    if campaign.past_last_date(c) or counts["to_call"] == 0:
        return "completed"
    if not counts["reached"]:
        return "draft"
    return "paused" if counts["new_left"] else "completed"


ENGAGED_LESS = ("unclear", "wrong_number")   # picked up, but no real conversation to an answer
STATS_DAYS = 60                              # the calls-per-day chart covers at most this many days


@router.get("/api/campaigns/{campaign_id}/stats")
async def campaign_stats(campaign_id: int) -> dict:
    """One campaign's page, Overview tab: its list from audience to converted (per contact, as the Campaigns page counts
    them), what it cost, calls per day, and how many contacts each result has."""
    c = db.get_campaign(campaign_id)
    if not c:
        raise HTTPException(404, "No such campaign.")
    s = c.get("settings") or {}
    counts = db.contact_counts(int(s.get("max_attempts") or 3), campaign_id)
    by, total = counts["by_status"], counts["total"]
    run = campaign.CAMPAIGN
    running = (run.running and run.settings.campaign_id == campaign_id) or (bool(c.get("auto")) and autodial.AUTO.running)
    # Contacts whose status a call has set (a call-back put back on the list is pending again); "called" itself counts
    # everyone rung at least once.
    settled, called = total - sum(by.get(k, 0) for k in NOT_CALLED_STATUSES), counts["reached"]
    not_connected = sum(by.get(k, 0) for k in NOT_CONNECTED_STATUSES)
    hung_up = sum(by.get(k, 0) for k in HUNG_UP_STATUSES)
    connected = max(0, settled - not_connected - hung_up - by.get("calling", 0))
    converted = sum(n for k, n in by.items() if k in CONVERSIONS)
    engaged = max(converted, connected - sum(by.get(k, 0) for k in ENGAGED_LESS))
    # Owner, 24 Oct 2026: the contacts still to be rung again (no answer, busy, cut off or "call later", tries left, as
    # db.next_to_call picks them), and the repeat calls they have had so far (every call after their first).
    max_attempts = int(s.get("max_attempts") or 3)
    marks = ",".join("?" for _ in db.RETRY_STATUSES)
    with db.connect() as conn:
        retry = conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(MAX(COALESCE(attempts, 0) - 1, 0)), 0) AS done FROM contacts "
            f"WHERE campaign_id=? AND status IN ({marks}) AND (status='call_later' OR COALESCE(attempts, 0) < ?)",
            (campaign_id, *db.RETRY_STATUSES, max_attempts)).fetchone()
        # The rest of the Call back tab: all tries used, so no campaign rings them again by itself.
        out_of_tries = conn.execute(
            f"SELECT COUNT(*) FROM contacts WHERE campaign_id=? AND status IN ({marks}) AND status != 'call_later' "
            "AND COALESCE(attempts, 0) >= ?", (campaign_id, *db.RETRY_STATUSES, max_attempts)).fetchone()[0]

    with db.connect() as conn:
        calls = conn.execute("SELECT c.started_at, c.channel, c.duration_sec, c.outcome, c.usage FROM calls c "
                             "JOIN contacts k ON k.id = c.contact_id WHERE k.campaign_id = ? ORDER BY c.id", (campaign_id,)).fetchall()
    agent_rs = line_rs = 0.0
    per_day: dict[str, list[int]] = {}
    for r in calls:
        a, l = db.cost_parts(r["channel"], r["duration_sec"], r["usage"])
        agent_rs, line_rs = agent_rs + a, line_rs + l
        day = datetime.fromisoformat(r["started_at"]).astimezone(IST).strftime("%Y-%m-%d")
        d = per_day.setdefault(day, [0, 0])
        d[0] += 1
        d[1] += (r["duration_sec"] or 0) > 0 and r["outcome"] not in NOT_CONNECTED_STATUSES
    series = []
    if per_day:
        today = datetime.now(IST).date()
        start = max(datetime.strptime(min(per_day), "%Y-%m-%d").date(), today - timedelta(days=STATS_DAYS - 1))
        day = start
        while day <= today:
            key = day.strftime("%Y-%m-%d")
            series.append({"day": key, "label": day.strftime("%d %b"), "calls": per_day.get(key, [0, 0])[0],
                           "connected": per_day.get(key, [0, 0])[1]})
            day += timedelta(days=1)

    ct = CALL_TYPES.get(c["call_type"])
    keys = [*(ct.outcomes if ct else ()), "no_answer", "busy", "dropped"]
    return {
        "id": campaign_id, "status": _status(c, counts, running),
        # Owner, 24 Oct 2026: whether the sheet's customers have all had their first call, apart from call-backs.
        "reached": counts["reached"], "new_left": counts["new_left"], "callbacks_left": counts["to_call"] - counts["new_left"],
        "running": running, "agent": AGENT_LABELS.get(s.get("model") or "", s.get("model") or ""), "voice": s.get("voice") or "",
        "budget_rs": int(s.get("budget_rs") or 0),
        "audience": total, "called": called, "connected": connected, "engaged": engaged, "converted": converted,
        "not_connected": not_connected, "hung_up": hung_up, "to_call": counts["to_call"],
        "retry_waiting": retry["n"], "retries_done": retry["done"], "out_of_tries": out_of_tries,
        "agent_rs": round(agent_rs, 2), "line_rs": round(line_rs, 2), "cost_rs": round(agent_rs + line_rs, 2),
        "calls": len(calls), "series": series,
        "outcomes": [{"key": k, "n": by.get(k, 0)} for k in dict.fromkeys(keys)],
    }


@router.get("/api/campaigns/board")
async def campaigns_board() -> dict:
    """The new dashboard's Campaigns page. The Store orders campaign has its own page and is left out, as before."""
    camps = [c for c in db.list_campaigns() if c.get("source") != "store"]
    run = campaign.CAMPAIGN
    costs: dict[int, list[float]] = {}
    with db.connect() as conn:
        for r in conn.execute("SELECT k.campaign_id, c.channel, c.duration_sec, c.usage FROM calls c "
                              "JOIN contacts k ON k.id = c.contact_id WHERE k.campaign_id IS NOT NULL"):
            agent, line = db.cost_parts(r["channel"], r["duration_sec"], r["usage"])
            acc = costs.setdefault(r["campaign_id"], [0.0, 0.0])
            acc[0] += agent
            acc[1] += line
    rows = []
    for c in camps:
        s = c.get("settings") or {}
        counts = (db.contact_counts(int(s.get("max_attempts") or 3), c["id"]) if c["total"]
                  else {"total": 0, "to_call": 0, "new_left": 0, "reached": 0})
        to_call = counts["to_call"]
        running = (run.running and run.settings.campaign_id == c["id"]) or (bool(c.get("auto")) and autodial.AUTO.running)
        by = c["by_status"]
        settled, called = c["total"] - sum(by.get(k, 0) for k in NOT_CALLED_STATUSES), counts["reached"]
        not_connected = sum(by.get(k, 0) for k in NOT_CONNECTED_STATUSES)
        hung_up = sum(by.get(k, 0) for k in HUNG_UP_STATUSES)
        agent_rs, line_rs = costs.get(c["id"], [0.0, 0.0])
        rows.append({
            "id": c["id"], "name": c["name"], "call_type": c["call_type"], "auto": bool(c.get("auto")),
            "created_at": c["created_at"], "last_call_at": c.get("last_call_at"),
            "status": _status(c, counts, running),
            "new_left": counts["new_left"], "callbacks_left": to_call - counts["new_left"],
            "model": s.get("model") or "", "agent": AGENT_LABELS.get(s.get("model") or "", s.get("model") or ""),
            "voice": s.get("voice") or "",
            "contacts": c["total"], "called": called, "to_call": to_call,
            # A call in progress is called but has no result yet: it is in none of these three.
            "connected": max(0, settled - not_connected - hung_up - by.get("calling", 0)),
            "not_connected": not_connected, "hung_up": hung_up,
            "agent_rs": round(agent_rs, 2), "line_rs": round(line_rs, 2), "cost_rs": round(agent_rs + line_rs, 2),
            "start_at": c.get("start_at"), "start_note": c.get("start_note"),
        })
    count = lambda st: sum(1 for r in rows if r["status"] == st)  # noqa: E731
    return {"campaigns": rows,
            "counts": {st: count(st) for st in ("running", "scheduled", "paused", "completed", "draft")}}


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
