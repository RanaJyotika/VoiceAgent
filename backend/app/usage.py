"""The new dashboard's Usage page: what calling cost by day, provider, campaign and agent, and the workspace budget
(monthly and daily limits, alert levels, and whether campaigns pause at a limit). Reads the database; calls nothing.

The budget is enforced where calls are placed by themselves: a campaign run (campaign.py) and automatic calling
(autodial.py) stop at a limit when "pause" is on. A single call placed by hand is never blocked."""
from calendar import monthrange
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from . import db
from .config import AGENT_LABELS

router = APIRouter()
IST = db.IST
_SETTING = "budget"   # app setting: {"monthly_rs", "daily_rs", "alert_at": [80, 90], "pause": true}
DEFAULT = {"monthly_rs": 0, "daily_rs": 0, "alert_at": [80, 90], "pause": True}

# How each part of a call's cost is named on the page: the agent's provider, and the phone line.
PROVIDERS = {"sarvam": "Sarvam · agent", "openai": "OpenAI · agent", "gemini": "Gemini · agent", "unknown": "Agent (not recorded)",
             "plivo": "Plivo · phone line"}


def budget() -> dict:
    saved = db.get_setting(_SETTING, {}) or {}
    return {**DEFAULT, **{k: v for k, v in saved.items() if k in DEFAULT}}


def _midnight(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _spent(start: datetime, end: datetime | None = None) -> float:
    """Rupees spent on every call (agent and line, tests included) from start to end (Indian time)."""
    where, args = "started_at >= ?", [start.astimezone(timezone.utc).isoformat()]
    if end:
        where += " AND started_at < ?"
        args.append(end.astimezone(timezone.utc).isoformat())
    with db.connect() as conn:
        rows = conn.execute(f"SELECT channel, duration_sec, usage FROM calls WHERE {where}", args).fetchall()
    return sum(db.call_cost(r["channel"], r["duration_sec"], r["usage"]) for r in rows)


def spent_now() -> tuple[float, float]:
    """(today, this month) in rupees."""
    today = _midnight(datetime.now(IST))
    return _spent(today), _spent(today.replace(day=1))


def limit_reached() -> str:
    """Why calling placed by itself must stop now ("" = it may go on): a limit is reached and "pause" is on."""
    b = budget()
    if not b["pause"] or not (b["daily_rs"] or b["monthly_rs"]):
        return ""
    day, month = spent_now()
    if b["monthly_rs"] and month >= b["monthly_rs"]:
        return f"this month's spend Rs {month:.0f} reached the monthly limit of Rs {b['monthly_rs']} (Usage page)"
    if b["daily_rs"] and day >= b["daily_rs"]:
        return f"today's spend Rs {day:.0f} reached the daily limit of Rs {b['daily_rs']} (Usage page)"
    return ""


def alerts() -> list[dict]:
    """Limits whose alert level is passed, for the Overview's "Attention required"."""
    b = budget()
    if not (b["daily_rs"] or b["monthly_rs"]):
        return []
    day, month = spent_now()
    out = []
    for name, spent, limit in (("Monthly", month, b["monthly_rs"]), ("Daily", day, b["daily_rs"])):
        if not limit:
            continue
        share = 100 * spent / limit
        passed = [a for a in sorted(b["alert_at"]) if share >= a]
        if passed:
            stop = " Campaigns are paused." if share >= 100 and b["pause"] else ""
            out.append({"level": "critical" if share >= 100 else "warning",
                        "title": f"{name} budget {share:.0f}% used",
                        "detail": f"Rs {spent:,.0f} of Rs {limit:,.0f}.{stop}", "page": "spend"})
    return out


@router.get("/api/budget")
async def get_budget() -> dict:
    day, month = spent_now()
    return {**budget(), "spent_today_rs": round(day, 2), "spent_month_rs": round(month, 2)}


@router.put("/api/budget")
async def save_budget(body: dict) -> dict:
    try:
        monthly = max(0, int(body.get("monthly_rs") or 0))
        daily = max(0, int(body.get("daily_rs") or 0))
        alert_at = sorted({int(a) for a in body.get("alert_at") or [] if 1 <= int(a) <= 100})
    except (TypeError, ValueError):
        raise HTTPException(400, "Limits and alert levels must be whole numbers.")
    db.set_setting(_SETTING, {"monthly_rs": monthly, "daily_rs": daily, "alert_at": alert_at, "pause": bool(body.get("pause"))})
    return await get_budget()


def _period(range_: str) -> tuple[datetime, datetime, str]:
    today = _midnight(datetime.now(IST))
    if range_ == "month":
        return today.replace(day=1), today + timedelta(days=1), "this month"
    if range_ == "last_month":
        end = today.replace(day=1)
        return (end - timedelta(days=1)).replace(day=1), end, "last month"
    if range_ in ("7d", "30d"):
        n = int(range_[:-1])
        return today - timedelta(days=n - 1), today + timedelta(days=1), f"last {n} days"
    raise HTTPException(400, "range must be month, last_month, 7d or 30d.")


def _rows(start: datetime, end: datetime) -> list:
    with db.connect() as conn:
        return conn.execute(
            "SELECT c.started_at, c.channel, c.duration_sec, c.usage, k.campaign_id, g.name AS campaign_name FROM calls c "
            "LEFT JOIN contacts k ON k.id = c.contact_id LEFT JOIN campaigns g ON g.id = k.campaign_id "
            "WHERE c.started_at >= ? AND c.started_at < ?",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())).fetchall()


@router.get("/api/usage")
async def usage(range: str = "month") -> dict:  # noqa: A002
    """Spend, minutes and cost per minute against the period before; cost per day by provider; and the splits by
    provider, campaign and agent. Costs are the same figures as everywhere else (db.cost_parts)."""
    start, end, label = _period(range)
    prev_start = start - (end - start) if range != "month" and range != "last_month" else \
        (start - timedelta(days=1)).replace(day=1)
    prev_end = start
    rows, prev = _rows(start, end), _rows(prev_start, prev_end)

    def totals(rs) -> tuple[float, float]:
        cost = sum(db.call_cost(r["channel"], r["duration_sec"], r["usage"]) for r in rs)
        return cost, sum((r["duration_sec"] or 0) for r in rs) / 60

    spend, minutes = totals(rows)
    prev_spend, prev_minutes = totals(prev)
    per_min = spend / minutes if minutes else 0.0
    prev_per_min = prev_spend / prev_minutes if prev_minutes else 0.0

    days, providers, campaigns, agents = {}, {}, {}, {}
    day = start
    while day < end:
        days[day.strftime("%Y-%m-%d")] = {"day": day.strftime("%Y-%m-%d"), "label": day.strftime("%d %b"), **{k: 0.0 for k in PROVIDERS}}
        day += timedelta(days=1)
    for r in rows:
        agent_rs, line_rs = db.cost_parts(r["channel"], r["duration_sec"], r["usage"])
        model, provider = db.call_agent(r["usage"])
        provider = provider if provider in PROVIDERS else "unknown"
        seconds = r["duration_sec"] or 0
        key = datetime.fromisoformat(r["started_at"]).astimezone(IST).strftime("%Y-%m-%d")
        if key in days:
            days[key][provider] += agent_rs
            days[key]["plivo"] += line_rs
        p = providers.setdefault(provider, {"key": provider, "calls": 0, "minutes": 0.0, "cost_rs": 0.0})
        p["calls"] += 1
        p["minutes"] += seconds / 60
        p["cost_rs"] += agent_rs
        if r["channel"] == "plivo":
            line = providers.setdefault("plivo", {"key": "plivo", "calls": 0, "minutes": 0.0, "cost_rs": 0.0})
            line["calls"] += 1
            line["minutes"] += seconds / 60
            line["cost_rs"] += line_rs
        c = campaigns.setdefault(r["campaign_id"] or 0, {"id": r["campaign_id"] or 0,
                                                        "name": r["campaign_name"] or ("Single calls" if r["campaign_id"] is None else "—"),
                                                        "calls": 0, "cost_rs": 0.0})
        c["calls"] += 1
        c["cost_rs"] += agent_rs + line_rs
        a = agents.setdefault(model, {"name": AGENT_LABELS.get(model, model if model != "not recorded" else "Not recorded"),
                                      "calls": 0, "cost_rs": 0.0})
        a["calls"] += 1
        a["cost_rs"] += agent_rs + line_rs

    def rounded(items, key="cost_rs"):
        return sorted(({**x, key: round(x[key], 2), **({"minutes": round(x["minutes"], 1)} if "minutes" in x else {})} for x in items),
                      key=lambda x: -x[key])

    projected = None
    if range == "month":
        today = datetime.now(IST)
        elapsed = today.day - 1 + (today.hour * 60 + today.minute) / 1440
        projected = round(spend / max(elapsed, 1 / 24) * monthrange(today.year, today.month)[1], 2) if spend else 0.0
    change = lambda now, before: round(100 * (now - before) / before, 1) if before else None  # noqa: E731
    return {
        "range": range, "label": label, "from": start.strftime("%Y-%m-%d"), "to": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "spend_rs": round(spend, 2), "spend_change": change(spend, prev_spend), "projected_rs": projected,
        "minutes": round(minutes, 1), "minutes_change": change(minutes, prev_minutes),
        "per_min_rs": round(per_min, 2), "per_min_change": change(per_min, prev_per_min), "calls": len(rows),
        "series": [{**d, **{k: round(d[k], 2) for k in PROVIDERS}} for d in days.values()],
        "providers": [{**p, "label": PROVIDERS[p["key"]]} for p in rounded(providers.values())],
        "provider_labels": PROVIDERS,
        "campaigns": rounded(campaigns.values()), "agents": rounded(agents.values()),
        "budget": await get_budget(),
    }
