"""Campaign: call every contact on the list, one after another, and keep the records straight.

The owner presses Start on the dashboard. From then on, while the campaign is running:
- contacts that were never called are dialled in sheet order;
- no-answer and busy contacts are dialled again after RETRY_MINUTES, up to MAX_ATTEMPTS times;
- at most MAX_CONCURRENT calls run at once (default 1: one agent, one customer at a time);
- calls are only placed inside the calling hours (Indian time);
- the campaign pauses itself when a call fails for a reason on our side (Gemini billing, Plivo
  refusing the number, the tunnel gone), so a bad key cannot burn through the whole list.

Every call goes through the same place_call as the Call button, so the records (transcript,
outcome, notes, recording, cost) are exactly the same. What the customer said is written by the
agent through mark_outcome; this module only decides who to ring next.
"""
import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from . import db, usage
from .config import (DIAL_ONLY_MY_NUMBER, GEMINI_VOICE, MAX_CONCURRENT_CALLS, OFFERED_MODELS, PAGE_BUILD, PHONE_NUMBER,
                     STALE_PAGE, VOICES, plivo_missing)
from .call_types import missing_facts
from .db import IST
from .plivo_bridge import PENDING, line_problem, place_call
from .prompts import LANGUAGES

log = logging.getLogger("campaign")
router = APIRouter()

GAP_SECONDS = 4          # breathing space between one call ending and the next being placed
STOP_ON_ERRORS = 2       # consecutive system failures that pause the campaign

# Contacts being dialled right now, before Plivo has taken the call (place_call adds a call to
# PENDING only once Plivo answers). Two dialers run side by side, this one and automatic calling
# (autodial.py): picking a contact and adding it here happen with no await in between, so the
# other dialer never rings the same contact, and lines in use counts calls still being placed.
DIALING: set[int] = set()


def lines_in_use() -> int:
    return len(PENDING) + len(DIALING)


async def dial(contact: dict, s: "Settings"):
    """place_call for either dialer, with the contact held in DIALING while Plivo is asked."""
    DIALING.add(contact["id"])
    try:
        return await place_call(contact, s.voice, s.model, language=s.language)
    finally:
        DIALING.discard(contact["id"])


@dataclass
class Settings:
    campaign_id: int = 0           # the campaign being called (one runs at a time)
    voice: str = GEMINI_VOICE
    model: str = ""                # the agent every call of this run starts on ("" = the first offered)
    language: str = "hinglish"     # for rows whose sheet has no language column
    max_concurrent: int = 1
    max_attempts: int = 3          # calls to one contact before giving up on no answer / busy
    retry_minutes: int = 30        # wait this long before ringing a no-answer / busy contact again
    start_hour: int = 10           # calling window, Indian time, inclusive start
    end_hour: int = 19             # exclusive end: 19 means calls stop at 7 pm
    limit: int = 0                 # stop after this many calls this run (0 = whole list)
    budget_rs: int = 0             # pause when today's estimated cost (all calls) reaches this (0 = no cap)
    days: str = "0123456"          # weekdays calls may be placed on, Monday = 0 (owner, 21 Oct 2026: e.g. Mon to Sat)


@dataclass
class Campaign:
    settings: Settings = field(default_factory=Settings)
    running: bool = False
    reason: str = ""               # why it is idle: "finished", "stopped by you", "paused: ..."
    started_at: str = ""
    calls_made: int = 0
    errors_in_a_row: int = 0
    task: asyncio.Task | None = None
    watchers: set = field(default_factory=set)

    def snapshot(self, campaign_id: int | None = None) -> dict:
        """How the running campaign stands, or (when another one is asked for) how that one's list
        stands; its saved settings are returned so the form shows what it last ran with."""
        s = self.settings
        cid = s.campaign_id if self.running or not campaign_id else campaign_id
        viewed = campaign_id or cid
        camp = db.get_campaign(viewed) if viewed else None
        saved = Settings(**{**asdict(Settings()), **((camp or {}).get("settings") or {}), "campaign_id": viewed or 0})             if camp and not (self.running and viewed == s.campaign_id) else s
        counts = db.contact_counts(saved.max_attempts, viewed) if viewed else db.contact_counts(saved.max_attempts, -1)
        return {
            "running": self.running, "running_campaign_id": s.campaign_id if self.running else None,
            "campaign_id": viewed, "last_call_date": (camp or {}).get("last_call_date") or "",
            "past_last_date": past_last_date(camp),
            "reason": self.reason if (self.running or viewed == s.campaign_id) else "",
            "started_at": self.started_at,
            "calls_made": self.calls_made, "settings": asdict(saved), "in_progress": len(PENDING),
            "within_hours": within_hours(saved), "now_ist": datetime.now(IST).strftime("%H:%M"),
            "waiting": "" if within_hours(saved) else waiting_text(saved),
            "dial_only_my_number": DIAL_ONLY_MY_NUMBER, "my_number": PHONE_NUMBER if DIAL_ONLY_MY_NUMBER else "",
            "max_concurrent_allowed": MAX_CONCURRENT_CALLS,
            **counts,
        }


CAMPAIGN = Campaign()


def past_last_date(camp: dict | None) -> bool:
    """The campaign's last calling day has gone by (Indian date)."""
    last = (camp or {}).get("last_call_date") or ""
    return bool(last) and datetime.now(IST).strftime("%Y-%m-%d") > last[:10]


def within_hours(s: Settings) -> bool:
    """Inside the calling window, on one of the calling days (Indian time)."""
    now = datetime.now(IST)
    return s.start_hour <= now.hour < s.end_hour and str(now.weekday()) in (s.days or "0123456")


DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def days_text(days: str) -> str:
    """"0123456" -> "every day"; "01234" -> "Mon to Fri"; "0" -> "Mon"."""
    on = sorted(int(d) for d in set(days or "0123456"))
    if len(on) == 7:
        return "every day"
    if len(on) > 2 and on == list(range(on[0], on[-1] + 1)):
        return f"{DAY_LABELS[on[0]]} to {DAY_LABELS[on[-1]]}"
    return ", ".join(DAY_LABELS[d] for d in on)


def next_window(s: Settings) -> datetime | None:
    """When calling may next happen under the window and days (now, if inside them). Indian time."""
    now = datetime.now(IST)
    if within_hours(s):
        return now
    for add in range(8):
        at = (now + timedelta(days=add)).replace(hour=s.start_hour, minute=0, second=0, microsecond=0)
        if at > now and str(at.weekday()) in (s.days or "0123456"):
            return at
    return None


def waiting_text(s: Settings) -> str:
    """Why a running campaign places no call right now, and when the first one goes."""
    nxt = next_window(s)
    when = nxt.strftime("%a %d %b at %H:%M") if nxt else "never (no calling day is set)"
    days = days_text(s.days)
    return (f"Waiting: this campaign calls {'' if days == 'every day' else 'only '}{days}, {s.start_hour:02d}:00 to {s.end_hour:02d}:00 IST. "
            f"It is {datetime.now(IST):%a %H:%M} now, so the next call goes {when}.")


async def _run() -> None:
    c = CAMPAIGN
    s = c.settings
    log.info("campaign started: %s", asdict(s))
    try:
        waiting_logged = False
        while c.running:
            if c.errors_in_a_row >= STOP_ON_ERRORS:
                c.reason = "paused: the last calls failed on our side (see the call history); fix that, then start again"
                break
            if s.limit and c.calls_made >= s.limit:
                c.reason = f"finished: reached the limit of {s.limit} calls"
                break
            if s.budget_rs:
                spent = db.stats("today")["cost_rs"]
                if spent >= s.budget_rs:
                    c.reason = f"paused: today's estimated cost is Rs {spent:.0f}, the budget was Rs {s.budget_rs}"
                    break
            if past_last_date(db.get_campaign(s.campaign_id)):
                c.reason = "finished: the campaign's last calling day has passed"
                break
            if (over := usage.limit_reached()):
                c.reason = f"paused: {over}"
                break
            if not within_hours(s):
                # Shown on the campaign page (owner, 21 Oct 2026: a run "started immediately" waited silently for Monday).
                c.reason = waiting_text(s)
                if not waiting_logged:
                    log.info("campaign waiting: calls only %s, %02d:00 to %02d:00 IST", days_text(s.days), s.start_hour, s.end_hour)
                    waiting_logged = True
                await asyncio.sleep(30)
                continue
            if waiting_logged:
                c.reason = ""
            waiting_logged = False
            if lines_in_use() >= s.max_concurrent:
                await asyncio.sleep(1)
                continue
            contact = db.next_to_call(s.max_attempts, s.campaign_id)
            if contact is not None and contact["id"] in DIALING:
                await asyncio.sleep(1)     # automatic calling is placing this very call
                continue
            if contact is None:
                if PENDING or c.watchers:
                    await asyncio.sleep(2)     # last calls still running; their retries may come
                    continue
                counts = db.contact_counts(s.max_attempts, s.campaign_id)
                if counts["retry_left"]:
                    # Only retries are left and none is due yet: wait for the next one.
                    await asyncio.sleep(15)
                    continue
                c.reason = "finished: everyone on the list has been called"
                break
            try:
                pc = await dial(contact, s)
            except HTTPException as exc:
                # A bad row (no phone number) is that row's problem; anything else is ours.
                if exc.status_code == 400:
                    db.finish_contact_without_call(contact["id"], "skipped: " + exc.detail)
                    log.warning("campaign skipped contact %s: %s", contact["id"], exc.detail)
                    continue
                c.reason = f"paused: {exc.detail}"
                break
            except Exception as exc:  # noqa: BLE001
                c.reason = f"paused: {type(exc).__name__}: {exc}"
                break
            c.calls_made += 1
            w = asyncio.create_task(_after_call(pc, contact))
            c.watchers.add(w)
            w.add_done_callback(c.watchers.discard)
            await asyncio.sleep(GAP_SECONDS)
    except asyncio.CancelledError:
        c.reason = "stopped by you" if not c.reason or c.reason.startswith("Waiting") else c.reason
    finally:
        c.running = False
        c.task = None
        log.info("campaign ended: %s (%s calls this run)", c.reason, c.calls_made)


async def _after_call(pc, contact: dict, c: Campaign = CAMPAIGN) -> None:
    """Once a campaign call is over, decide whether that contact gets another try. c is the
    dialer that placed it: this campaign, or automatic calling."""
    s = c.settings
    try:
        await pc.task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass   # the call's own record was written by run_call regardless
    row = db.latest_call_for_contact(contact["id"]) or {}
    outcome, error, answered = row.get("outcome"), row.get("error"), (row.get("duration_sec") or 0) > 0
    if error and not answered:
        c.errors_in_a_row += 1
        log.warning("campaign: call to contact %s failed before pick-up (%s)", contact["id"], error)
        return
    c.errors_in_a_row = 0
    if outcome == "call_later":
        return   # the time the customer asked for was already set from their words
    if outcome in db.RETRY_STATUSES:
        attempts = (db.get_contact(contact["id"]) or {}).get("attempts", 0)
        if attempts < s.max_attempts:
            due = db.schedule_retry(contact["id"], s.retry_minutes)
            log.info("campaign: contact %s %s (attempt %s of %s); next try at %s", contact["id"], outcome, attempts, s.max_attempts, due)
        else:
            log.info("campaign: contact %s %s after %s attempts; giving up", contact["id"], outcome, attempts)


def clean_settings(body: dict, campaign_id: int = 0) -> Settings:
    """A campaign's run settings from the dashboard, each kept inside its limits. Used by Start, and by the new
    dashboard's campaign wizard, which saves them on the campaign before it ever runs."""
    try:
        s = Settings(
            campaign_id=campaign_id,
            voice=body.get("voice") if body.get("voice") in VOICES else GEMINI_VOICE,
            model=body.get("model") if body.get("model") in OFFERED_MODELS else OFFERED_MODELS[0],
            language=body.get("language") if body.get("language") in LANGUAGES else "hinglish",
            max_concurrent=max(1, min(MAX_CONCURRENT_CALLS, int(body.get("max_concurrent") or 1))),
            max_attempts=max(1, min(6, int(body.get("max_attempts") or 3))),
            retry_minutes=max(1, min(24 * 60, int(body.get("retry_minutes") or 30))),
            start_hour=max(0, min(23, int(body.get("start_hour") or 10))),
            end_hour=max(1, min(24, int(body.get("end_hour") or 19))),
            limit=max(0, int(body.get("limit") or 0)),
            budget_rs=max(0, int(body.get("budget_rs") or 0)),
            days="".join(d for d in "0123456" if d in str(body.get("days") or "0123456")) or "0123456",
        )
    except (TypeError, ValueError):
        raise HTTPException(400, "Settings must be whole numbers.")
    if s.end_hour <= s.start_hour:
        raise HTTPException(400, "Calling hours: the end must be after the start.")
    return s


@router.get("/api/campaign")
async def campaign_status(campaign_id: int | None = None) -> dict:
    return CAMPAIGN.snapshot(campaign_id)


@router.post("/api/campaign/start")
async def campaign_start(body: dict) -> dict:
    """Start (or resume) calling the list. Every call is a real, paid phone call."""
    if str(body.get("build") or "") != PAGE_BUILD:
        raise HTTPException(409, STALE_PAGE)
    camp = db.get_campaign(int(body.get("campaign_id") or 0))
    if not camp:
        raise HTTPException(400, "Pick a campaign to start.")
    if not plivo_missing() and not CAMPAIGN.running and (why := await line_problem()):
        raise HTTPException(400, why)   # owner, 22 Oct 2026: a dead tunnel paused every start with Plivo's own error
    return begin(camp, clean_settings(body, camp["id"]))


def begin(camp: dict, s: Settings) -> dict:
    """Start calling a campaign, by the Start button or at its scheduled time: every check, then the run."""
    c = CAMPAIGN
    if c.running:
        raise HTTPException(400, "A campaign is already running. Stop it first; one campaign calls at a time.")
    if past_last_date(camp):
        raise HTTPException(400, "This campaign's last calling day has passed. Change it on the campaign to call again.")
    if missing_facts(camp):
        raise HTTPException(400, "Fill in the campaign details first (Edit details): " + ", ".join(missing_facts(camp)))
    missing = plivo_missing()
    if missing:
        raise HTTPException(400, f"Phone line not ready. Missing: {', '.join(missing)}")
    if (over := usage.limit_reached()):
        raise HTTPException(400, f"Budget limit reached: {over}.")
    counts = db.contact_counts(s.max_attempts, s.campaign_id)
    if not counts["to_call"]:
        raise HTTPException(400, "Nobody to call: every contact is done. Re-queue some contacts or upload a new sheet.")
    c.settings = s
    db.update_campaign(s.campaign_id, settings={k: v for k, v in asdict(s).items() if k != "campaign_id"})
    c.running, c.reason, c.calls_made, c.errors_in_a_row = True, "", 0, 0
    c.started_at = db.now_iso()
    c.task = asyncio.create_task(_run())
    db.update_campaign(s.campaign_id, start_at=None)   # a scheduled start is used up by any start
    return c.snapshot()


# --- scheduled starts (owner, 21 Oct 2026: "Start: Immediately or Schedule") -------------------------------------
SCHEDULE_TICK = 20                 # seconds between looks at the scheduled campaigns
SCHEDULE_GRACE = 60 * 60           # a start missed by more than this (server off) is not made late: it is marked missed


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(IST).strftime("%d %b %H:%M")


def run_due_schedules() -> None:
    """Start every campaign whose scheduled time has come, with the settings saved on it. One campaign calls at a time:
    a start that finds another one calling waits (up to SCHEDULE_GRACE). Never a start long after the time set."""
    now = datetime.now(IST)
    for camp in db.list_campaigns():
        at = camp.get("start_at")
        if not at:
            continue
        due = datetime.fromisoformat(at)
        if due > now:
            continue
        if (now - due).total_seconds() > SCHEDULE_GRACE:
            db.update_campaign(camp["id"], start_at=None,
                               start_note=f"missed: it was due {_when(at)}, but the server was off or another campaign was calling")
            log.warning("scheduled start of campaign %s missed (due %s)", camp["id"], at)
            continue
        if CAMPAIGN.running:
            continue   # one campaign calls at a time: try again on the next tick
        saved = {**asdict(Settings()), **(camp.get("settings") or {})}
        try:
            begin(camp, clean_settings(saved, camp["id"]))
        except HTTPException as exc:
            db.update_campaign(camp["id"], start_at=None, start_note=f"not started at {_when(at)}: {exc.detail}")
            log.warning("scheduled start of campaign %s refused: %s", camp["id"], exc.detail)
            continue
        db.update_campaign(camp["id"], start_note=f"started by its schedule at {datetime.now(IST).strftime('%d %b %H:%M')}")
        log.info("campaign %s started by its schedule (due %s)", camp["id"], at)


async def schedule_worker() -> None:
    """Runs for as long as the server does."""
    while True:
        try:
            run_due_schedules()
        except Exception:  # noqa: BLE001
            log.exception("scheduled starts: check failed")
        await asyncio.sleep(SCHEDULE_TICK)


@router.post("/api/campaigns/{campaign_id}/schedule")
async def schedule_campaign(campaign_id: int, body: dict) -> dict:
    """{"at": "2026-10-22T10:00"} (Indian time) makes the campaign start calling by itself then; {"at": null} cancels.
    Starting then is a real start: real, paid calls, with the settings saved on the campaign."""
    camp = db.get_campaign(campaign_id)
    if not camp:
        raise HTTPException(404, "No such campaign.")
    raw = str(body.get("at") or "").strip()
    if not raw:
        db.update_campaign(campaign_id, start_at=None, start_note=None)
        return {"start_at": None}
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "The start time must look like 2026-10-22T10:00.")
    when = when.replace(tzinfo=IST) if when.tzinfo is None else when.astimezone(IST)
    if when <= datetime.now(IST):
        raise HTTPException(400, "Pick a start time in the future.")
    if missing_facts(camp):
        raise HTTPException(400, "Fill in the campaign details first: " + ", ".join(missing_facts(camp)))
    db.update_campaign(campaign_id, start_at=when.isoformat(), start_note=None)
    return {"start_at": when.isoformat()}


@router.post("/api/campaign/stop")
async def campaign_stop() -> dict:
    """Stop placing new calls. A call already in progress is left to finish."""
    c = CAMPAIGN
    if c.task and not c.task.done():
        c.reason = "stopped by you"
        c.running = False
        c.task.cancel()
    return c.snapshot()


@router.get("/api/callbacks")
async def list_callbacks() -> dict:
    """Everyone waiting for another call, with when."""
    return {"rows": db.callbacks(), "max_attempts": CAMPAIGN.settings.max_attempts, "running": CAMPAIGN.running}


@router.post("/api/contacts/{contact_id}/schedule")
async def schedule_contact(contact_id: int, body: dict) -> dict:
    """Set when to call back: {"at": "2026-09-23T11:00"} in Indian time, or {"at": null} for as soon as possible."""
    if not db.get_contact(contact_id):
        raise HTTPException(404, "contact not found")
    at = body.get("at")
    when = None
    if at:
        try:
            when = datetime.fromisoformat(str(at))
        except ValueError:
            raise HTTPException(400, "Time must look like 2026-09-23T11:00")
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
    db.set_next_try(contact_id, when)
    return {"ok": True, "next_try_at": when.isoformat() if when else None}


@router.post("/api/contacts/{contact_id}/giveup")
async def give_up_contact(contact_id: int) -> dict:
    if not db.get_contact(contact_id):
        raise HTTPException(404, "contact not found")
    db.give_up(contact_id)
    return {"ok": True}


@router.post("/api/callbacks/giveup")
async def give_up_many(body: dict) -> dict:
    """Give up on several people of the call back list at once ({"ids": [...]}): each leaves the list for Needs review.
    Only contacts still waiting for a call back are touched; a contact that has moved on since is left alone."""
    ids = {int(i) for i in body.get("ids") or [] if str(i).isdigit()}
    waiting = {c["id"] for c in db.callbacks()}
    done = 0
    for contact_id in ids & waiting:
        db.give_up(contact_id)
        done += 1
    return {"given_up": done}


@router.post("/api/contacts/requeue")
async def requeue(body: dict) -> dict:
    """Put contacts back on the list: by ids, or every contact with one of the given statuses."""
    ids = [int(i) for i in body.get("ids") or []]
    statuses = tuple(str(x) for x in body.get("statuses") or [])
    n = db.requeue_contacts(ids=ids or None, statuses=statuses or None, campaign_id=int(body.get("campaign_id") or 0) or None)
    return {"requeued": n}
