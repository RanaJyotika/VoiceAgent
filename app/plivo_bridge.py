"""Plivo phone line: place an outbound call and bridge its audio to the Gemini Live agent.

Flow: POST /api/calls/dial/{contact_id} -> Plivo rings the phone -> on answer Plivo fetches
/plivo/answer/... -> we return <Stream> XML -> Plivo opens a WebSocket to /ws/plivo/... ->
audio flows both ways through live_bridge.run_call, the same agent the browser test uses.

Audio: we ask Plivo for 16 kHz 16-bit PCM, which is exactly what Gemini wants as input.
Gemini replies at 24 kHz, so we resample that to 16 kHz before sending it to the phone.
"""
import asyncio
import audioop  # stdlib in Python 3.12
import base64
import json
import logging
import time
import uuid
from collections import deque

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from . import db, vad
from .config import (DIAL_ONLY_MY_NUMBER, GEMINI_LIVE_MODEL, GEMINI_VOICE, NOISE_GATE, MAX_CALL_SECONDS,
                     PHONE_END_SILENCE_MS, PHONE_NUMBER, PHONE_VAD,
                     PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_FROM_NUMBER, PUBLIC_BASE_URL,
                     VOICES, plivo_missing)
from .contacts import is_valid_phone, normalise_phone
from .live_bridge import ACTIVE, ACTIVITY_END, ACTIVITY_START, run_call
from .prompts import get_prompt

log = logging.getLogger("plivo")
router = APIRouter()

PLIVO_API = "https://api.plivo.com/v1/Account"
RING_SECONDS = 30
SEND_LEAD_SECONDS = 1.2   # how far ahead of real time we feed audio to the phone
ALLOWED_MODELS = {"gemini-2.5-flash-native-audio-preview-12-2025", "gemini-3.8-live"}
CHUNK_BYTES = 6400  # 200 ms of 16 kHz 16-bit audio; well under Plivo's 16 KB base64 advice


def _auth() -> tuple[str, str]:
    return (PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)


def to_gemini_pcm(raw: bytes, media: dict, fmt: dict) -> bytes:
    """Convert one Plivo audio frame to what Gemini needs: 16 kHz, 16-bit, little-endian PCM.

    Plivo may send mu-law or linear PCM, at 8 or 16 kHz, and linear PCM may arrive in either
    byte order. We read the format from the frame, detect the byte order once from the first
    second of audio, and log what we found so problems can be diagnosed from the log.
    """
    if not raw:
        return b""
    ctype = str(media.get("contentType", "")).lower()
    rate = int(media.get("sampleRate") or 0) or (8000 if "mulaw" in ctype else 16000)
    fmt["frames"] += 1
    if fmt["frames"] == 1:
        log.info("plivo first audio frame: contentType=%s sampleRate=%s bytes=%s", ctype, rate, len(raw))

    if "mulaw" in ctype or "ulaw" in ctype:
        pcm = audioop.ulaw2lin(raw, 2)
    else:
        if len(raw) % 2:
            raw = raw[:-1]
        if "swap" not in fmt:
            # Audio read with the WRONG byte order looks like loud noise,
            # so the right order is the one with the smaller loudness.
            fmt["probe"] = fmt.get("probe", b"") + raw
            if len(fmt["probe"]) >= rate * 2:          # about one second
                as_is = audioop.rms(fmt["probe"], 2)
                swapped = audioop.rms(audioop.byteswap(fmt["probe"], 2), 2)
                fmt["swap"] = swapped < as_is
                log.info("plivo audio byte order: loudness as-is=%s swapped=%s -> %s", as_is, swapped,
                         "big-endian, swapping bytes" if fmt["swap"] else "little-endian, no change")
                fmt.pop("probe", None)
        pcm = audioop.byteswap(raw, 2) if fmt.get("swap") else raw

    if rate != 16000:
        pcm, fmt["rs"] = audioop.ratecv(pcm, 2, 1, rate, 16000, fmt.get("rs"))

    # Loudness report every ~5 seconds: shows from the log whether the caller's voice arrives.
    fmt["peak"] = max(fmt.get("peak", 0), audioop.rms(pcm, 2))
    if fmt["frames"] % 250 == 0:
        log.info("plivo inbound audio: %s frames so far, loudest recent level %s (speech is usually above 500)",
                 fmt["frames"], fmt["peak"])
        fmt["peak"] = 0
    return pcm


@router.get("/api/plivo/status")
async def plivo_status() -> dict:
    missing = plivo_missing()
    return {"ready": not missing, "missing": missing, "dial_only_my_number": DIAL_ONLY_MY_NUMBER,
            "from_number": PLIVO_FROM_NUMBER, "public_base_url": PUBLIC_BASE_URL}


class PendingCall:
    """One phone call from dial to hang-up. The Gemini session is opened at dial time."""

    def __init__(self, contact: dict, voice: str, model: str = GEMINI_LIVE_MODEL, to: str = "") -> None:
        self.to = to
        self.token = ""
        self.request_uuid = ""
        self.contact = contact
        self.voice = voice
        self.model = model
        # --- line clean-up state ---
        self.play_until = 0.0                 # the phone is playing agent audio until this moment
        self.levels: deque = deque(maxlen=150)  # last ~3 s of frame loudness, for the noise floor
        self.echo_sum = 0.0                   # loudness of the line while ONLY the agent talks
        self.echo_n = 0
        self.gate_open_until = 0.0
        self.vad = vad.SpeechDetector() if vad.available() else None   # None = judge by loudness
        self.voice_level = 0.0                # running loudness of this customer's voice, for boost()
        # --- manual speech detection state (PHONE_VAD=manual) ---
        self.greeting_generated = False       # the agent's first turn is complete (still playing)
        self.greeted = False                  # ...and has finished playing on the phone
        self.listen_from = 0.0                # safety: listen anyway from this moment
        self.in_speech = False
        self.voiced_run = 0                   # loud frames in a row while waiting for speech
        self.quiet_run = 0                    # quiet frames since the last loud one
        self.reply_pending = False            # the customer finished; the agent's reply is on its way
        self.reply_due = 0.0
        self.preroll: deque = deque(maxlen=30)  # last 600 ms, replayed when speech starts
        self.speech_started = 0.0
        self.last_voiced = 0.0
        self.seg_peak, self.seg_voiced, self.seg_over_agent = 0, 0, False   # for the log line per reply
        self.stats = {"frames": 0, "silenced": 0, "bargein": 0}
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.answered = asyncio.Event()
        self.ws: WebSocket | None = None
        self.outbox: asyncio.Queue = asyncio.Queue()   # agent audio waiting to go to the phone
        self.sender: asyncio.Task | None = None
        self.silence: asyncio.Task | None = None
        self.fmt: dict = {"frames": 0}
        self.ids = {"stream": "", "call": ""}
        self.resample_state = None
        self.task: asyncio.Task | None = None

    async def send_audio(self, pcm24k: bytes) -> None:
        """Called by the agent. Only queues: it must never wait on the phone network, because a
        slow phone line would then stall the Gemini connection until it times out."""
        self.reply_pending = False
        pcm16k, self.resample_state = audioop.ratecv(pcm24k, 2, 1, 24000, 16000, self.resample_state)
        for i in range(0, len(pcm16k), CHUNK_BYTES):
            self.outbox.put_nowait(pcm16k[i:i + CHUNK_BYTES])

    async def _send_loop(self, ws: WebSocket) -> None:
        """Feeds the phone at speaking speed plus a small lead. Gemini produces audio much faster
        than it is spoken; pushing it all at once floods the line and makes interruptions slow."""
        try:
            while True:
                chunk = await self.outbox.get()
                lead = self.play_until - time.monotonic()
                if lead > SEND_LEAD_SECONDS:
                    await asyncio.sleep(lead - SEND_LEAD_SECONDS)
                await ws.send_text(json.dumps({
                    "event": "playAudio",
                    "media": {"contentType": "audio/x-l16", "sampleRate": 16000,
                              "payload": base64.b64encode(chunk).decode()},
                }))
                self.play_until = max(self.play_until, time.monotonic()) + len(chunk) / 32000.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("sending audio to the phone stopped: %s", exc)

    async def _silence_loop(self) -> None:
        """Between pick-up and the audio line opening, keep a steady stream of silence going to
        Gemini so the session never sits without input."""
        frame = b"\x00" * 640
        try:
            while self.ws is None:
                await self.incoming.put(frame)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise

    def on_answered(self) -> None:
        if not self.answered.is_set():
            self.answered.set()
            if PHONE_VAD != "manual":      # manual mode sends audio only while the customer talks
                self.silence = asyncio.create_task(self._silence_loop())

    def stop_tasks(self) -> None:
        for t in (self.sender, self.silence):
            if t:
                t.cancel()

    def is_speech(self, pcm: bytes) -> bool:
        """Is this frame the customer talking, as opposed to line noise or the agent's own echo?"""
        now = time.monotonic()
        level = audioop.rms(pcm, 2)
        self.levels.append(level)
        self.stats["frames"] += 1
        ordered = sorted(self.levels)
        floor = ordered[len(ordered) // 5]                 # 20th percentile = the background level
        threshold = max(300.0, floor * 2.2)

        echo = self.echo_sum / max(1, self.echo_n)
        agent_talking = self.agent_talking(now)
        if agent_talking and self.echo_n < 150:
            # Learn how loud the agent's own voice comes back on this line (first ~3 s of it).
            self.echo_sum += level
            self.echo_n += 1

        if self.vad is not None:
            # Voice pattern, not loudness: a soft voice counts, a loud knock does not.
            prob = self.vad.feed(pcm)
            if agent_talking:
                # The agent's own echo IS a voice, so over the agent loudness still has to clear it.
                speech = prob >= 0.6 and level >= max(echo * 2.5, 500.0)
            else:
                speech = prob >= 0.5 and level >= 60
        else:
            if agent_talking:
                # To interrupt the agent, the customer must be clearly louder than that echo.
                threshold = max(threshold, echo * 2.5, 900.0)
            speech = level >= threshold

        if speech:
            self.voice_level = level if not self.voice_level else 0.9 * self.voice_level + 0.1 * level
            if agent_talking:
                self.stats["bargein"] += 1
        return speech

    def agent_talking(self, now: float) -> bool:
        """Is the agent's voice still coming out of the customer's phone? On a line with echo
        (speakerphone) the tail arrives late, so allow longer before trusting the line again."""
        echo = self.echo_sum / max(1, self.echo_n)
        return now < self.play_until + (0.8 if echo > 100 else 0.3)

    def boost(self, pcm: bytes) -> bytes:
        """Bring a soft or distant voice up to a normal level before the agent hears it."""
        if not self.voice_level or self.voice_level >= 2000:
            return pcm
        return audioop.mul(pcm, 2, min(6.0, 2000 / self.voice_level))

    def clean(self, pcm: bytes) -> bytes:
        """Turn line noise and the agent's own echo into true silence; let real speech through."""
        if not NOISE_GATE or not pcm:
            return pcm
        now = time.monotonic()
        if self.is_speech(pcm):
            self.gate_open_until = now + 0.35              # keep word endings
            return pcm
        if now < self.gate_open_until:
            return pcm
        self.stats["silenced"] += 1
        return b"\x00" * len(pcm)

    def listen(self, pcm: bytes) -> list:
        """What to pass to the agent for one frame from the phone.

        Manual mode: nothing while the line is quiet. When the customer starts talking we send a
        start marker plus the last moments of audio (so the first syllable is not lost), then the
        live audio, then an end marker once they have been quiet for PHONE_END_SILENCE_MS.
        """
        if PHONE_VAD != "manual":
            return [self.clean(pcm)]
        now = time.monotonic()
        voiced = self.is_speech(pcm)
        if not self.greeted:
            # People say "hello" as they pick up. Passing that on cut the greeting off before it
            # began, and the agent then hung up thinking nobody was there. So the customer is only
            # listened to once the greeting has been fully played (or 20 s, if it never comes).
            self.listen_from = self.listen_from or now + 20
            played = self.greeting_generated and self.outbox.empty() and now >= self.play_until
            if played or now >= self.listen_from:
                self.greeted = True
                log.info("greeting finished; now listening to the customer")
            else:
                return []
        if not self.in_speech:
            self.preroll.append(pcm)
            # Count loud frames, forgiving the tiny dips inside a word (up to 60 ms).
            if voiced:
                self.voiced_run, self.quiet_run = self.voiced_run + 1, 0
            else:
                self.quiet_run += 1
                if self.quiet_run > 3:
                    self.voiced_run = 0
            # How much sound counts as "the customer is talking":
            # - 120 ms normally: rules out clicks, pops and knocks.
            # - 240 ms over the agent: a cough or a "hmm" must not cut the agent off mid-sentence.
            # - 400 ms while the agent is preparing its reply. Any start marker in that gap throws
            #   the reply away; on real calls a breath or a bump there cost 4 to 8 s of silence.
            #   A customer who really carries on talking easily passes 400 ms.
            # The pre-roll is replayed when speech starts, so waiting loses no audio.
            self.seg_over_agent = self.agent_talking(now)
            if now > self.reply_due:
                self.reply_pending = False
            need = 12 if self.seg_over_agent else 20 if self.reply_pending else 6
            if self.voiced_run < need:
                self.stats["silenced"] += 1
                return []
            self.in_speech, self.voiced_run, self.quiet_run = True, 0, 0
            self.speech_started = self.last_voiced = now
            self.seg_peak, self.seg_voiced = 0, 0
            out = [ACTIVITY_START, *(self.boost(f) for f in self.preroll)]
            self.preroll.clear()
            return out
        if voiced:
            self.last_voiced = now
            self.seg_voiced += 1
            self.seg_peak = max(self.seg_peak, self.levels[-1])
        # 20 s cap: a line that never goes quiet must not keep the agent waiting forever.
        if now - self.last_voiced >= PHONE_END_SILENCE_MS / 1000 or now - self.speech_started > 20:
            self.in_speech = False
            self.reply_pending, self.reply_due = True, now + 8   # cleared when the agent's audio arrives
            # Real replies are long and loud; a knock or a clatter is short. Used to tune the detector.
            log.info("customer sound: %.1fs of voice, loudest level %s, usual voice level %.0f, over the agent: %s",
                     self.seg_voiced * 0.02, self.seg_peak, self.voice_level, "yes" if self.seg_over_agent else "no")
            return [self.boost(pcm), ACTIVITY_END]
        if self.vad is not None and now - self.last_voiced > 0.2:
            # A gap between the customer's words: turn the room noise down so the agent hears
            # mostly voice. (Turned down, not cut, so the audio still sounds natural to it.)
            return [audioop.mul(pcm, 2, 0.15)]
        return [self.boost(pcm)]

    async def attach(self, ws: WebSocket) -> None:
        self.ws = ws
        waiting = self.outbox.qsize()
        self.sender = asyncio.create_task(self._send_loop(ws))
        log.info("audio line attached; %s prepared audio chunks waiting", waiting)

    async def send_event(self, evt: dict) -> None:
        if evt.get("type") == "turn_complete":
            self.greeting_generated = True
        # The customer talked over the agent: drop whatever audio Plivo still has queued.
        if evt.get("type") == "interrupted":
            while not self.outbox.empty():
                self.outbox.get_nowait()
            self.play_until = time.monotonic()
            if self.ws is not None and self.ids["stream"]:
                try:
                    await self.ws.send_text(json.dumps({"event": "clearAudio", "streamId": self.ids["stream"]}))
                except Exception:  # noqa: BLE001
                    pass


PENDING: dict[str, PendingCall] = {}


def _start_agent(pc: PendingCall) -> None:
    pc.task = asyncio.create_task(run_call(
        contact=pc.contact,
        system_prompt=get_prompt(pc.contact, pc.voice),
        channel="plivo",
        incoming_audio=pc.incoming,
        send_audio=pc.send_audio,
        send_event=pc.send_event,
        voice=pc.voice,
        model=pc.model,
        sensitive_listening=True,   # phone channel tuning
        manual_vad=PHONE_VAD == "manual",
        start_gate=pc.answered,
        meta={"to": pc.to, "token": pc.token},
    ))


async def _expire(token: str, seconds: float) -> None:
    """Nobody picked up: close the waiting Gemini session so nothing is left running."""
    await asyncio.sleep(seconds)
    pc = PENDING.get(token)
    if pc and not pc.answered.is_set():
        PENDING.pop(token, None)
        if pc.task:
            pc.task.cancel()
        pc.stop_tasks()
        log.info("call %s was not answered; agent released", token)


@router.post("/api/calls/dial/{contact_id}")
async def dial(contact_id: int, voice: str = GEMINI_VOICE, to: str = "", model: str = "") -> dict:
    """Place one real phone call. This costs money: Plivo per minute plus Gemini per minute."""
    missing = plivo_missing()
    if missing:
        raise HTTPException(400, f"Plivo is not set up yet. Missing: {', '.join(missing)}")
    contact = db.get_contact(contact_id)
    if not contact:
        raise HTTPException(404, "contact not found")
    if voice not in VOICES:
        voice = GEMINI_VOICE
    # Number typed on the screen wins; otherwise the number from the Excel row.
    to_number = normalise_phone(to) or normalise_phone(contact.get("phone", ""))
    if DIAL_ONLY_MY_NUMBER:
        to_number = normalise_phone(PHONE_NUMBER)
    if not is_valid_phone(to_number):
        raise HTTPException(400, "No valid phone number. Type a 10-digit mobile number in the box.")

    token = uuid.uuid4().hex[:16]
    payload = {
        "from": PLIVO_FROM_NUMBER.lstrip("+"),
        "to": to_number.lstrip("+"),
        "answer_url": f"{PUBLIC_BASE_URL}/plivo/answer/{token}",
        "answer_method": "POST",
        "hangup_url": f"{PUBLIC_BASE_URL}/plivo/hangup/{token}",
        "hangup_method": "POST",
        "ring_timeout": RING_SECONDS,
        "time_limit": MAX_CALL_SECONDS,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{PLIVO_API}/{PLIVO_AUTH_ID}/Call/", json=payload, auth=_auth())
    if r.status_code >= 300:
        log.error("plivo dial failed %s %s", r.status_code, r.text[:500])
        raise HTTPException(502, f"Plivo refused the call: {r.text[:300]}")

    # Get the agent ready while the phone rings, so it can greet the moment they pick up.
    pc = PendingCall(contact, voice, model if model in ALLOWED_MODELS else GEMINI_LIVE_MODEL, to_number)
    pc.token = token
    pc.request_uuid = str(r.json().get("request_uuid") or "")   # lets us cancel while it is still ringing
    PENDING[token] = pc
    _start_agent(pc)
    asyncio.create_task(_expire(token, RING_SECONDS + 20))
    log.info("dialing %s for contact %s (voice %s) token=%s", to_number, contact_id, voice, token)
    return {"ok": True, "dialed": to_number, "plivo": r.json()}


@router.api_route("/plivo/answer/{token}", methods=["GET", "POST"])
async def answer(token: str) -> Response:
    """Plivo tells us the phone was picked up. Start the greeting and ask for the audio line."""
    pc = PENDING.get(token)
    if pc:
        pc.on_answered()
        log.info("call %s answered; greeting started", token)
    ws_base = PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-l16;rate=16000">{ws_base}/ws/plivo/{token}</Stream>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@router.api_route("/plivo/hangup/{token}", methods=["GET", "POST"])
async def hangup_webhook(token: str, request: Request) -> dict:
    try:
        form = dict(await request.form())
    except Exception:  # noqa: BLE001
        form = {}
    log.info("plivo hangup: status=%s cause=%s duration=%s bill=%s to=%s",
             form.get("CallStatus"), form.get("HangupCause"), form.get("Duration"),
             form.get("TotalCost"), form.get("To"))
    pc = PENDING.pop(token, None)
    if pc:
        if not pc.answered.is_set() and pc.task:
            pc.task.cancel()              # busy, rejected or no answer
        else:
            await pc.incoming.put(None)   # the customer hung up: end the conversation
    return {"ok": True}


async def _hangup_call(call_uuid: str) -> None:
    if not call_uuid:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(f"{PLIVO_API}/{PLIVO_AUTH_ID}/Call/{call_uuid}/", auth=_auth())
            if r.status_code == 404:
                log.info("call %s had already ended", call_uuid)
    except Exception:  # noqa: BLE001
        log.exception("failed to hang up %s", call_uuid)


@router.post("/api/calls/{call_id}/hangup")
async def hangup_from_dashboard(call_id: int) -> dict:
    """The owner pressed Hang up on a live phone call. Works while ringing and while talking."""
    active = ACTIVE.get(call_id)
    if not active:
        raise HTTPException(404, "That call has already ended.")
    if active["channel"] != "plivo":
        raise HTTPException(400, "Browser test calls are ended from the browser test itself.")
    pc = PENDING.get(active["meta"].get("token", ""))
    if pc is None:
        raise HTTPException(404, "That call has already ended.")
    state = active["state"]
    if not pc.answered.is_set():
        # Still ringing: cancel the dial request, then release the waiting agent.
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(f"{PLIVO_API}/{PLIVO_AUTH_ID}/Request/{pc.request_uuid}/", auth=_auth())
        except Exception:  # noqa: BLE001
            log.exception("failed to cancel ringing call %s", call_id)
        PENDING.pop(pc.token, None)
        if pc.task:
            pc.task.cancel()
        pc.stop_tasks()
        log.info("call %s cancelled from the dashboard while ringing", call_id)
        return {"ok": True, "was": "ringing"}
    if not state.outcome:
        state.outcome_details = {"notes": "Hung up from the dashboard."}
    log.info("call %s hung up from the dashboard", call_id)
    await _hangup_call(pc.ids["call"])
    await pc.incoming.put(None)   # ends the conversation; the audio line then closes itself
    return {"ok": True, "was": "in progress"}


@router.websocket("/ws/plivo/{token}")
async def plivo_stream(ws: WebSocket, token: str) -> None:
    await ws.accept()
    pc = PENDING.get(token)
    if pc is None:
        log.error("audio line opened for unknown call %s", token)
        await ws.close()
        return
    pc.on_answered()

    async def reader() -> None:
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                event = msg.get("event")
                if event == "start":
                    pc.ids["stream"] = msg.get("start", {}).get("streamId", "")
                    pc.ids["call"] = msg.get("start", {}).get("callId", "")
                    log.info("plivo stream started call=%s stream=%s", pc.ids["call"], pc.ids["stream"])
                    await pc.attach(ws)
                elif event == "media":
                    media = msg.get("media", {})
                    pcm = to_gemini_pcm(base64.b64decode(media.get("payload", "")), media, pc.fmt)
                    if pcm:
                        for item in pc.listen(pcm):
                            await pc.incoming.put(item)
                elif event == "stop":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await pc.incoming.put(None)

    reader_task = asyncio.create_task(reader())
    try:
        if pc.task:
            await pc.task
        # Let the goodbye finish playing on the phone, then hang up so billing stops.
        for _ in range(100):
            if pc.outbox.empty() and time.monotonic() >= pc.play_until:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.6)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("phone call %s failed", token)
    finally:
        reader_task.cancel()
        pc.stop_tasks()
        PENDING.pop(token, None)
        st = pc.stats
        if st["frames"]:
            ordered = sorted(pc.levels) or [0]
            log.info("line report: background level %s, agent echo level %.0f, %d%% of frames silenced, "
                     "%s loud-enough-to-interrupt frames", ordered[len(ordered) // 5],
                     pc.echo_sum / max(1, pc.echo_n), 100 * st["silenced"] // st["frames"], st["bargein"])
        await _hangup_call(pc.ids["call"])
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
