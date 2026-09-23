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
import wave
from collections import deque

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from . import db, fillers, vad
from .config import (DIAL_ONLY_MY_NUMBER, FILLERS, GEMINI_LIVE_MODEL, GEMINI_VOICE, NOISE_GATE, MAX_CALL_SECONDS,
                     OPENAI_REALTIME_MODEL,
                     PHONE_END_SILENCE_MS, PHONE_LONG_ANSWER_MS, PHONE_NUMBER, PHONE_RECORD, PHONE_SILENT_SECONDS,
                     PHONE_STUCK_SECONDS, PHONE_VAD, PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_FROM_NUMBER,
                     PUBLIC_BASE_URL, RECORDINGS_DIR, VOICES, plivo_missing)
from .contacts import is_valid_phone, normalise_phone
from .live_bridge import ACTIVE, ACTIVITY_END, ACTIVITY_START, Nudge, run_call
from .openai_bridge import is_openai_model
from .prompts import get_prompt, with_language

log = logging.getLogger("plivo")
router = APIRouter()

PLIVO_API = "https://api.plivo.com/v1/Account"
RING_SECONDS = 30
SEND_LEAD_SECONDS = 1.2   # how far ahead of real time we feed audio to the phone
ALLOWED_MODELS = {"gemini-2.5-flash-native-audio-preview-12-2025", "gemini-3.8-live",
                  OPENAI_REALTIME_MODEL, "gpt-realtime-2.1", "gpt-realtime-2.1-mini"}
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

    # Loudness report every ~5 seconds: shows from the log whether the caller's voice arrives,
    # and how voice-like the detector found it (filled in by PendingCall.is_speech). On call 45
    # the line was loud for 30 s at a time while nothing was recognised; this line tells why.
    fmt["peak"] = max(fmt.get("peak", 0), audioop.rms(pcm, 2))
    if fmt["frames"] % 250 == 0:
        log.info("plivo inbound audio: %s frames so far, loudest recent level %s (speech is usually above 500), "
                 "most voice-like frame %.2f, %s loud frames not taken as speech",
                 fmt["frames"], fmt["peak"], fmt.get("vad_peak", 0.0), fmt.get("loud_dropped", 0))
        fmt["peak"], fmt["vad_peak"], fmt["loud_dropped"] = 0, 0.0, 0
    return pcm


@router.get("/api/plivo/status")
async def plivo_status() -> dict:
    missing = plivo_missing()
    return {"ready": not missing, "missing": missing, "dial_only_my_number": DIAL_ONLY_MY_NUMBER,
            "from_number": PLIVO_FROM_NUMBER, "public_base_url": PUBLIC_BASE_URL}


class PendingCall:
    """One phone call from dial to hang-up. The Gemini session is opened at dial time."""

    BACKGROUND_GATE = True   # ignore voices much fainter than the customer's own (scripts/replay.py can switch it off)
    BG_RATIO = 0.25          # ...fainter than this share of the customer's usual level...
    BG_PROB = 0.6            # ...unless the voice model is at least this sure. (0.4/0.9 lost 40% of a
                             # fragmenting customer's own speech on call 53; 0.25/0.6 lost none.)

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
        self.stats = {"frames": 0, "silenced": 0, "bargein": 0, "loud_dropped": 0}
        # --- safety nets while waiting for the customer (see PHONE_STUCK_SECONDS) ---
        self.lenient = False                  # after a stuck line: trust quieter voice scores
        self.wait_since = 0.0                 # the agent finished and we started waiting
        self.loud_wait = 0                    # loud frames seen while waiting, none taken as speech
        self.nudged = False                   # one nudge per agent turn
        self.silent_nudges = 0                # silence nudges so far this call
        self.stuck_nudges = 0
        self.last_loud = False                # the latest frame was well above the noise floor
        self.recorder = None                  # wave file of the customer's audio (PHONE_RECORD)
        self.meta: dict = {}                  # shared with run_call (see _start_agent)
        self.end_silence = PHONE_END_SILENCE_MS / 1000   # pause that closes the customer's turn; longer after "what is your address?"
        self.gated_line = False               # the customer's phone sends digital silence between bursts (call 53)
        self.filler: fillers.Picker | None = None   # "hmm" / "achha" clips in this voice, loaded when the line opens
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
            # A quiet line (call 49: voice level under 400) scores low on the voice model, so
            # it is judged on a brought-up copy, the same as the agent would hear.
            probe = pcm
            ref = self.voice_level or level
            if level >= 60 and ref < 1500:
                probe = audioop.mul(pcm, 2, min(6.0, 2000.0 / max(ref, 200.0)))
            prob = self.vad.feed(probe)
            self.fmt["vad_peak"] = max(self.fmt.get("vad_peak", 0.0), prob)
            # Well above the room's noise floor. On a hot, noisy speakerphone line (call 45: floor
            # over 1000, voice peaks 10000+) the voice model is only half sure of distorted speech,
            # so a clearly loud sound is accepted on a lower score. Lower still once a call has
            # already been caught stuck (lenient).
            loud = level >= max(300.0, floor * 3.0)
            if agent_talking:
                # The agent's own echo IS a voice, so over the agent loudness still has to clear it.
                speech = prob >= 0.6 and level >= max(echo * 2.5, 500.0)
            else:
                speech = (prob >= 0.5 and level >= 60) or (prob >= (0.2 if self.lenient else 0.3) and loud)
                # Someone else talking in the room is a voice too, but a fainter one than the customer's
                # own. Once the customer's level is known, a voice at under 40% of it is background,
                # unless the voice model is all but certain.
                if (speech and self.BACKGROUND_GATE and self.voice_level >= 300 and not self.in_speech
                        and level < self.BG_RATIO * self.voice_level and prob < self.BG_PROB):
                    speech = False
                    self.stats["background"] = self.stats.get("background", 0) + 1
            if loud and not speech and not agent_talking:
                self.stats["loud_dropped"] += 1
                self.fmt["loud_dropped"] = self.fmt.get("loud_dropped", 0) + 1
        else:
            if agent_talking:
                # To interrupt the agent, the customer must be clearly louder than that echo.
                threshold = max(threshold, echo * 2.5, 900.0)
            speech = level >= threshold

        if speech:
            self.voice_level = level if not self.voice_level else 0.9 * self.voice_level + 0.1 * level
            if agent_talking:
                self.stats["bargein"] += 1
        self.last_loud = level >= max(300.0, floor * 3.0)
        return speech

    def waiting_check(self, now: float) -> list:
        """While the agent waits for an answer: is the line stuck (loud but nothing recognised)
        or dead quiet? Either way the agent hears nothing and would wait forever, so tell it."""
        if self.agent_talking(now) or self.reply_pending:
            self.wait_since = 0.0
            return []
        if not self.wait_since:
            self.wait_since, self.loud_wait, self.nudged = now, 0, False
            return []
        if self.nudged:
            return []
        if self.last_loud:
            self.loud_wait += 1
        waited = now - self.wait_since
        note = ""
        if self.loud_wait >= 50 and waited >= PHONE_STUCK_SECONDS and self.stuck_nudges < 3:
            # A second or more of loud sound and still no speech: almost certainly the customer
            # answering on a bad line. Repeat the question, and trust the line's voice scores more.
            self.stuck_nudges += 1
            self.lenient = True
            log.info("line loud for %.0fs (%s loud frames) but no speech recognised; asking the agent to repeat "
                     "(stuck nudge %s); listening more leniently from now on", waited, self.loud_wait, self.stuck_nudges)
            note = (f"[Note from the phone line: for the last {waited:.0f} seconds there has been loud sound from the "
                    "customer's side but no words could be made out; they are probably answering you on a noisy line. "
                    "Do not wait. Say the voice is not clear ('sorry ji, awaaz clear nahi aa rahi') and repeat your "
                    "last question in fewer words.]")
        elif self.loud_wait < 10 and waited >= PHONE_SILENT_SECONDS and self.silent_nudges < 2:
            self.silent_nudges += 1
            log.info("line quiet for %.0fs; asking the agent to check (silence nudge %s)", waited, self.silent_nudges)
            if self.silent_nudges == 1:
                note = (f"[Note from the phone line: nothing has been heard from the customer for {waited:.0f} seconds. "
                        "Ask once, briefly: 'hello ji, meri awaaz aa rahi hai?']")
            else:
                note = (f"[Note from the phone line: still nothing from the customer after another {waited:.0f} seconds. "
                        "Say a short goodbye, call mark_outcome with outcome 'unclear' and notes 'no response on the "
                        "line', then end_call.]")
        if not note:
            return []
        self.nudged = True
        self.reply_pending, self.reply_due = True, now + 8   # the agent's reply is on its way
        return [Nudge(note)]

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
            return self.boost(pcm)
        if now < self.gate_open_until:
            return self.boost(pcm)
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
                return self.waiting_check(now)
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
        # How long a pause closes the turn: the base value, longer when the agent asked for an
        # address or a number, and longer again on a phone that sends digital silence between
        # bursts of speech (its own noise suppression cutting the mic; the customer has not
        # finished, the phone just went quiet). 20 s cap: a line that never goes quiet must not
        # keep the agent waiting forever.
        zeros = sum(1 for lv in self.levels if lv <= 2)
        self.gated_line = zeros >= len(self.levels) * 0.5 and self.seg_voiced > 0
        # The gated-line bonus only after a question with a long answer (an address, a number): a
        # "haan ji" has no second burst to wait for, and the bonus cost 0.6 s on every turn of call 54.
        long_answer = self.end_silence > PHONE_END_SILENCE_MS / 1000
        pause = self.end_silence + (0.6 if self.gated_line and long_answer else 0.0)
        if now - self.last_voiced >= pause or now - self.speech_started > 20:
            self.in_speech = False
            self.reply_pending, self.reply_due = True, now + 8   # cleared when the agent's audio arrives
            # Real replies are long and loud; a knock or a clatter is short. Used to tune the detector.
            log.info("customer sound: %.1fs of voice, loudest level %s, usual voice level %.0f, over the agent: %s",
                     self.seg_voiced * 0.02, self.seg_peak, self.voice_level, "yes" if self.seg_over_agent else "no")
            self.play_filler()
            return [self.boost(pcm), ACTIVITY_END]
        if self.vad is not None and now - self.last_voiced > 0.5:
            # A real gap between the customer's words: turn the room noise down so the agent
            # hears mostly voice. Gently, and after half a second: on call 49 a 3-second answer
            # on a quiet line was mostly "gap" to the detector, and turning it down to 15% (and
            # unboosted) left Gemini with nothing to transcribe but ".".
            return [audioop.mul(self.boost(pcm), 2, 0.4)]
        return [self.boost(pcm)]

    async def attach(self, ws: WebSocket) -> None:
        self.ws = ws
        waiting = self.outbox.qsize()
        self.sender = asyncio.create_task(self._send_loop(ws))
        if FILLERS and is_openai_model(self.model):
            # The filler exists to cover Gemini's ~3 s pause. OpenAI answers quickly and in its
            # own voice, so a synthetic click would be noise rather than cover.
            log.info("filler sounds: off (this agent answers without the pause they cover)")
        elif FILLERS:
            # Loaded now, not at dial time: the clips may still be generating while the phone rings.
            self.filler = fillers.Picker(self.voice)
            log.info("filler sounds: %s", f"{len(self.filler.clips)} clips in voice {self.voice}" if self.filler.clips
                     else "no clips for this voice yet; using the keyboard tap")
        log.info("audio line attached; %s prepared audio chunks waiting", waiting)

    def play_filler(self) -> None:
        """A short "hmm" in the agent's voice while Gemini prepares its reply. Not after the customer
        talked over the agent (they want an answer, not a noise) and never twice in a row."""
        if self.filler is None or self.seg_over_agent or not self.outbox.empty():
            return
        clip = self.filler.next()
        for i in range(0, len(clip), CHUNK_BYTES):
            self.outbox.put_nowait(clip[i:i + CHUNK_BYTES])

    def call_id(self) -> int | None:
        return next((cid for cid, a in ACTIVE.items() if a["meta"].get("token") == self.token), None)

    def start_recording(self) -> None:
        """Keep the customer's side of the call as a 16 kHz mono wav, for listening back later."""
        call_id = self.call_id()
        if not PHONE_RECORD or self.recorder is not None or call_id is None:
            return
        try:
            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            name = f"call_{call_id}.wav"
            self.recorder = wave.open(str(RECORDINGS_DIR / name), "wb")
            self.recorder.setnchannels(1)
            self.recorder.setsampwidth(2)
            self.recorder.setframerate(16000)
            db.set_recording(call_id, name)
            log.info("recording the customer's audio to data/recordings/%s", name)
        except Exception as exc:  # noqa: BLE001
            self.recorder = None
            log.warning("could not start the recording: %s", exc)

    def record(self, pcm: bytes) -> None:
        if self.recorder is not None:
            try:
                self.recorder.writeframes(pcm)
            except Exception as exc:  # noqa: BLE001
                log.warning("recording stopped: %s", exc)
                self.recorder = None

    def stop_recording(self) -> None:
        if self.recorder is not None:
            try:
                self.recorder.close()
            except Exception:  # noqa: BLE001
                pass
            self.recorder = None

    async def send_event(self, evt: dict) -> None:
        if evt.get("type") == "turn_complete":
            self.greeting_generated = True
            # After "address batayein?" the answer is long and comes with pauses; give it time.
            self.end_silence = (PHONE_LONG_ANSWER_MS if evt.get("long_answer") else PHONE_END_SILENCE_MS) / 1000
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
    pc.meta = {"to": pc.to, "token": pc.token}   # shared with the agent; the hang-up cause is added later
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
        meta=pc.meta,
    ))
    pc.task.add_done_callback(lambda t: asyncio.ensure_future(_agent_finished(pc, t)))


async def _cancel_ringing(pc: PendingCall) -> None:
    """Stop the phone before anyone picks up (Gemini refused: out of credits, bad key...).
    Otherwise the customer answers to silence and is hung up on, and Plivo bills a pulse."""
    if not pc.request_uuid:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(f"{PLIVO_API}/{PLIVO_AUTH_ID}/Request/{pc.request_uuid}/", auth=_auth())
        log.info("call %s cancelled while ringing", pc.token)
    except Exception:  # noqa: BLE001
        log.exception("failed to cancel ringing call %s", pc.token)


async def _agent_finished(pc: PendingCall, task: asyncio.Task) -> None:
    if task.cancelled() or pc.answered.is_set():
        return
    # The agent gave up before the customer picked up (it either failed or refused to run).
    PENDING.pop(pc.token, None)
    pc.stop_tasks()
    await _cancel_ringing(pc)


async def _expire(token: str, seconds: float) -> None:
    """Plivo has said nothing for longer than a ring can last. Normally its hang-up webhook
    arrives within RING_SECONDS and pops the call; if we are still here, Plivo could not reach
    this server at all (tunnel down, PUBLIC_BASE_URL stale). That is our failure, not a customer
    who did not pick up, so it is recorded as an error and the campaign will pause on it."""
    await asyncio.sleep(seconds)
    pc = PENDING.get(token)
    if pc and not pc.answered.is_set():
        PENDING.pop(token, None)
        pc.meta["hangup_cause"] = "NO_CALLBACK"
        if pc.task:
            pc.task.cancel()
        pc.stop_tasks()
        log.warning("call %s: no answer or hang-up webhook from Plivo in %.0fs; is the tunnel up? agent released", token, seconds)


@router.post("/api/calls/dial/{contact_id}")
async def dial(contact_id: int, voice: str = GEMINI_VOICE, to: str = "", model: str = "", language: str = "") -> dict:
    """Place one real phone call. This costs money: Plivo per minute plus Gemini per minute."""
    missing = plivo_missing()
    if missing:
        raise HTTPException(400, f"Plivo is not set up yet. Missing: {', '.join(missing)}")
    contact = db.get_contact(contact_id)
    if not contact:
        raise HTTPException(404, "contact not found")
    pc = await place_call(contact, voice, model, to, language)
    return {"ok": True, "dialed": pc.to, "plivo": {"request_uuid": pc.request_uuid}}


async def place_call(contact: dict, voice: str = GEMINI_VOICE, model: str = "", to: str = "", language: str = "") -> PendingCall:
    """Ring one contact and get the agent ready. Used by the Call button and by the campaign.
    Raises HTTPException with a plain-English reason when the call cannot be placed."""
    if voice not in VOICES:
        voice = GEMINI_VOICE
    contact = with_language(contact, language)
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

    db.mark_calling(contact["id"])
    # Get the agent ready while the phone rings, so it can greet the moment they pick up.
    pc = PendingCall(contact, voice, model if model in ALLOWED_MODELS else GEMINI_LIVE_MODEL, to_number)
    pc.token = token
    pc.request_uuid = str(r.json().get("request_uuid") or "")   # lets us cancel while it is still ringing
    PENDING[token] = pc
    _start_agent(pc)
    if FILLERS and not is_openai_model(pc.model):
        asyncio.create_task(fillers.ensure(voice))   # clips for this voice, made once while the phone rings
    asyncio.create_task(_expire(token, RING_SECONDS + 20))
    log.info("dialing %s for contact %s (voice %s) token=%s", to_number, contact["id"], voice, token)
    return pc


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
        pc.meta["hangup_cause"] = str(form.get("HangupCause") or "")   # busy / no answer / normal
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
        await _cancel_ringing(pc)
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
                    pc.start_recording()
                elif event == "media":
                    media = msg.get("media", {})
                    pcm = to_gemini_pcm(base64.b64decode(media.get("payload", "")), media, pc.fmt)
                    if pcm:
                        pc.record(pcm)
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
        pc.stop_recording()
        PENDING.pop(token, None)
        st = pc.stats
        if st["frames"]:
            ordered = sorted(pc.levels) or [0]
            log.info("line report: background level %s, agent echo level %.0f, customer voice level %.0f, "
                     "%d%% of frames silenced, %s loud frames not taken as speech, %s background-voice frames ignored, "
                     "%s loud-enough-to-interrupt frames, gated line: %s, stuck nudges %s, silence nudges %s",
                     ordered[len(ordered) // 5], pc.echo_sum / max(1, pc.echo_n), pc.voice_level,
                     100 * st["silenced"] // st["frames"], st["loud_dropped"], st.get("background", 0), st["bargein"],
                     "yes" if pc.gated_line else "no", pc.stuck_nudges, pc.silent_nudges)
        await _hangup_call(pc.ids["call"])
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
