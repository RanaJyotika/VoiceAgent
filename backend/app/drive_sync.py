"""Every finished call's recording, both sides in one MP3, copied to a Google Drive folder (owner, 24 Oct 2026).

Like mongo_sync: the recording on this computer stays the working copy, and this copies each call once it is over
(GDRIVE_FOLDER_ID, in a folder for the day, "24 Oct 2026", named "2026-10-24_1227_call5347_Jyotika_confirmed.mp3"). The file's link is kept with the call
(calls.drive_link), shown on the dashboard and copied to MongoDB. Drive being slow or down never touches a call:
the copy simply catches up, and a file that keeps failing is tried MAX_TRIES times.

Nothing happens until scripts/drive_login.py has saved a login (GDRIVE_TOKEN_FILE). Plain HTTPS calls to Google's
Drive API, no Google client library: the access token is renewed from the saved refresh token when it runs out.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter

from . import db, recordings
from .config import (GDRIVE_FOLDER_ID, GDRIVE_FROM_CALL, GDRIVE_LOCAL_DIR, GDRIVE_SCRIPT_SECRET, GDRIVE_SCRIPT_URL,
                     GDRIVE_TOKEN_FILE, RECORDINGS_DIR)
from .db import IST

log = logging.getLogger("drive_sync")
router = APIRouter()

SETTLE_SECONDS = 30     # the recording files are closed a moment after the call ends (owner: upload soon after)
TICK_SECONDS = 15
BATCH = 10
MAX_TRIES = 5
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true"
              "&fields=id,webViewLink")
FROM_SETTING = "gdrive_from_call"   # app_settings: the first call to copy when GDRIVE_FROM_CALL is 0

STATE: dict = {"last_run": "", "uploaded": 0, "error": "", "folder": GDRIVE_FOLDER_ID}


def local_dir() -> Path | None:
    """The Drive folder as Google Drive for desktop shows it on this computer (GDRIVE_LOCAL_DIR), when it is there."""
    path = Path(GDRIVE_LOCAL_DIR) if GDRIVE_LOCAL_DIR else None
    return path if path and path.is_dir() else None


def via_script() -> bool:
    """The owner's Apps Script web app takes the recordings (no app, no API): GDRIVE_SCRIPT_URL and its password."""
    return bool(GDRIVE_SCRIPT_URL and GDRIVE_SCRIPT_SECRET)


def enabled() -> bool:
    """On with the Apps Script, a synced Drive folder on this computer, or the API login (drive_login.py)."""
    return via_script() or local_dir() is not None or (bool(GDRIVE_FOLDER_ID) and GDRIVE_TOKEN_FILE.is_file())


async def _send_to_script(http: httpx.AsyncClient, call: dict, name: str, data: bytes, mime: str) -> tuple[str, str]:
    """The recording to the Apps Script, which makes the day's folder ("24 Oct 2026") if needed and saves the file."""
    import base64   # here: only this way needs it
    r = await http.post(GDRIVE_SCRIPT_URL, follow_redirects=True, json={
        "secret": GDRIVE_SCRIPT_SECRET, "day": day_name(call), "name": name, "mime": mime,
        "data": base64.b64encode(data).decode("ascii")})
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"the Apps Script did not answer as expected ({r.status_code}): {r.text[:200]}. Is it deployed "
                           "as a Web app with access 'Anyone'?") from None
    if not j.get("ok"):
        raise RuntimeError(f"the Apps Script refused it: {j.get('error')}")
    return j["id"], j["url"]


def _save_local(folder: Path, call: dict, name: str, data: bytes) -> str:
    """The recording written into that day's folder of the synced Drive folder; Google Drive for desktop uploads it."""
    day = folder / day_name(call)
    day.mkdir(exist_ok=True)
    tmp = day / (name + ".part")
    tmp.write_bytes(data)
    tmp.replace(day / name)   # whole, so Drive never syncs half a file
    return f"local:{day_name(call)}/{name}"


def _login() -> dict:
    return json.loads(GDRIVE_TOKEN_FILE.read_text(encoding="utf-8"))


async def access_token(http: httpx.AsyncClient) -> str:
    """A valid access token, renewed from the saved refresh token (and saved again) when it is about to run out."""
    login = _login()
    if login.get("access_token") and float(login.get("expires_at") or 0) > time.time() + 120:
        return login["access_token"]
    r = await http.post(TOKEN_URL, data={"client_id": login["client_id"], "client_secret": login["client_secret"],
                                         "refresh_token": login["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise RuntimeError(f"Google refused the saved login ({r.status_code}): {r.text[:200]}. "
                           "Run scripts/drive_login.py again.")
    j = r.json()
    login.update(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in") or 3600))
    GDRIVE_TOKEN_FILE.write_text(json.dumps(login), encoding="utf-8")
    return login["access_token"]


def _first_call() -> int:
    """Copy calls from this number on: GDRIVE_FROM_CALL, else the first call after the copy was first switched on."""
    if GDRIVE_FROM_CALL:
        return GDRIVE_FROM_CALL
    start = db.get_setting(FROM_SETTING)
    if not start:
        with db.connect() as conn:
            start = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()[0]) + 1
        db.set_setting(FROM_SETTING, start)
        log.info("Google Drive copy switched on: recordings from call %s on are copied", start)
    return int(start)


def _pending(limit: int) -> list[dict]:
    settled = (datetime.now(timezone.utc) - timedelta(seconds=SETTLE_SECONDS)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.started_at, c.outcome, c.recording, k.customer_name FROM calls c "
            "LEFT JOIN contacts k ON k.id = c.contact_id WHERE c.recording IS NOT NULL AND c.drive_file_id IS NULL "
            "AND c.ended_at IS NOT NULL AND c.ended_at <= ? AND c.id >= ? AND COALESCE(c.drive_tries, 0) < ? "
            "ORDER BY c.id LIMIT ?", (settled, _first_call(), MAX_TRIES, limit)).fetchall()
    return [dict(r) for r in rows]


def file_name(call: dict, ext: str) -> str:
    """"2026-10-24_1227_call5347_Jyotika_confirmed.mp3": sorts by time in the folder, and says whose call it was."""
    try:
        when = datetime.fromisoformat(call["started_at"]).astimezone(IST).strftime("%Y-%m-%d_%H%M")
    except (TypeError, ValueError):
        when = "unknown-time"
    name = re.sub(r"[^\w-]+", "-", str(call.get("customer_name") or "").strip(), flags=re.UNICODE).strip("-")[:40]
    parts = [when, f"call{call['id']}", name or "customer", str(call.get("outcome") or "no-result")]
    return "_".join(parts) + ext


FOLDER_MIME = "application/vnd.google-apps.folder"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
DAYS_SETTING = "gdrive_day_folders"   # app_settings: {"24 Oct 2026": folder id}, so a day's folder is looked up once
_days: dict[str, str] = {}


def day_name(call: dict) -> str:
    """The folder a call's recording goes in: the day the call was made, Indian time ("24 Oct 2026"). Owner, 24 Oct
    2026: one folder a day. With the year, so next October's calls do not land in this October's folders."""
    try:
        return datetime.fromisoformat(call["started_at"]).astimezone(IST).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return "No date"


async def _day_folder(http: httpx.AsyncClient, day: str) -> str:
    """The id of that day's folder inside GDRIVE_FOLDER_ID: found (made by us earlier, or by hand), else made now."""
    if not _days:
        _days.update(db.get_setting(DAYS_SETTING, {}) or {})
    if day in _days:
        return _days[day]
    headers = {"Authorization": f"Bearer {await access_token(http)}"}
    q = (f"name = '{day}' and '{GDRIVE_FOLDER_ID}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false")
    r = await http.get(FILES_URL, headers=headers, params={"q": q, "fields": "files(id)", "supportsAllDrives": "true",
                                                           "includeItemsFromAllDrives": "true"})
    if r.status_code != 200:
        raise RuntimeError(f"Drive refused the folder look-up ({r.status_code}): {r.text[:300]}")
    found = r.json().get("files") or []
    if found:
        folder = found[0]["id"]
    else:
        r = await http.post(FILES_URL, headers=headers, params={"supportsAllDrives": "true", "fields": "id"},
                            json={"name": day, "mimeType": FOLDER_MIME, "parents": [GDRIVE_FOLDER_ID]})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Drive refused to make the folder {day!r} ({r.status_code}): {r.text[:300]}")
        folder = r.json()["id"]
        log.info("Google Drive: made the folder %r for that day's recordings", day)
    _days[day] = folder
    db.set_setting(DAYS_SETTING, dict(_days))
    return folder


async def _upload(http: httpx.AsyncClient, name: str, data: bytes, mime: str, parent: str) -> tuple[str, str]:
    boundary = "vedic-voice-agent-recording"
    meta = json.dumps({"name": name, "parents": [parent]})
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    token = await access_token(http)
    r = await http.post(UPLOAD_URL, content=body, headers={"Authorization": f"Bearer {token}",
                                                           "Content-Type": f"multipart/related; boundary={boundary}"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Drive refused the upload ({r.status_code}): {r.text[:300]}")
    j = r.json()
    return j["id"], j.get("webViewLink") or f"https://drive.google.com/file/d/{j['id']}/view"


async def copy_once(limit: int = BATCH) -> int:
    """Copy the recordings waiting; returns how many were tried."""
    calls = await asyncio.to_thread(_pending, limit)
    if not calls:
        STATE["last_run"] = datetime.now(timezone.utc).isoformat()
        return 0
    async with httpx.AsyncClient(timeout=120) as http:
        for call in calls:
            path = RECORDINGS_DIR / call["recording"]
            try:
                if not path.is_file():
                    raise FileNotFoundError(f"{call['recording']} is not on this computer")
                # The MP3 the day's download makes too (kept next to the recordings), else the wav when there is no encoder.
                audio = await asyncio.to_thread(recordings.conversation_mp3, path)
                if audio is None:
                    audio = await asyncio.to_thread(recordings.conversation, path)
                data = audio.read_bytes()
                ext, mime = (".mp3", "audio/mpeg") if audio.suffix == ".mp3" else (".wav", "audio/wav")
                if via_script():
                    file_id, link = await _send_to_script(http, call, file_name(call, ext), data, mime)
                elif (synced := local_dir()) is not None:
                    file_id = await asyncio.to_thread(_save_local, synced, call, file_name(call, ext), data)
                    link = f"https://drive.google.com/drive/folders/{GDRIVE_FOLDER_ID}"   # no file link without the API
                else:
                    folder = await _day_folder(http, day_name(call))
                    file_id, link = await _upload(http, file_name(call, ext), data, mime, folder)
                with db.connect() as conn:
                    # mongo_synced_at cleared: the MongoDB copy is written again, now with the link.
                    conn.execute("UPDATE calls SET drive_file_id=?, drive_link=?, mongo_synced_at=NULL WHERE id=?",
                                 (file_id, link, call["id"]))
                STATE["uploaded"] += 1
                STATE["error"] = ""
                log.info("call %s: recording copied to Google Drive (%d KB): %s", call["id"], len(data) // 1024, link)
            except Exception as exc:  # noqa: BLE001
                STATE["error"] = f"call {call['id']}: {type(exc).__name__}: {exc}"[:400]
                log.warning("recording not copied to Google Drive: %s", STATE["error"])
                with db.connect() as conn:
                    conn.execute("UPDATE calls SET drive_tries = COALESCE(drive_tries, 0) + 1 WHERE id=?", (call["id"],))
                if "refused the saved login" in str(exc):
                    break   # every other upload would fail the same way
    STATE["last_run"] = datetime.now(timezone.utc).isoformat()
    return len(calls)


async def worker() -> None:
    """Runs for the whole life of the server; does nothing while there is no login."""
    while True:
        if enabled():
            try:
                while await copy_once() == BATCH:
                    pass   # a backlog: keep going
            except Exception as exc:  # noqa: BLE001
                STATE["error"] = f"{type(exc).__name__}: {exc}"[:400]
                log.warning("recordings not copied to Google Drive: %s", STATE["error"])
        await asyncio.sleep(TICK_SECONDS)


@router.get("/api/drive")
async def drive_status() -> dict:
    """Whether recordings are being copied to Google Drive, and how far along."""
    if not enabled():
        return {**STATE, "enabled": False,
                "why": ("The synced Drive folder is not on this computer: " + GDRIVE_LOCAL_DIR + " (is Google Drive for "
                        "desktop running and signed in?)") if GDRIVE_LOCAL_DIR else
                       "Not set up: deploy backend/scripts/drive_upload_apps_script.gs and set GDRIVE_SCRIPT_URL and "
                       "GDRIVE_SCRIPT_SECRET in .env."}
    with db.connect() as conn:
        start = _first_call()
        waiting = conn.execute("SELECT COUNT(*) FROM calls WHERE recording IS NOT NULL AND drive_file_id IS NULL "
                               "AND id >= ? AND COALESCE(drive_tries, 0) < ?", (start, MAX_TRIES)).fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM calls WHERE recording IS NOT NULL AND drive_file_id IS NULL "
                              "AND id >= ? AND COALESCE(drive_tries, 0) >= ?", (start, MAX_TRIES)).fetchone()[0]
        copied = conn.execute("SELECT COUNT(*) FROM calls WHERE drive_file_id IS NOT NULL").fetchone()[0]
    return {**STATE, "enabled": True,
            "mode": "Apps Script" if via_script() else f"synced folder {local_dir()}" if local_dir() else "Drive API",
            "from_call": start, "copied": copied, "waiting": waiting, "gave_up": failed}
