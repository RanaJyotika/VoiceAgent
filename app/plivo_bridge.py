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

from . import catalog, db, fillers, line_audio, vad, voiceprint
from .config import (AUDIO_CLEANUP, DIAL_ONLY_MY_NUMBER, ECHO_CANCEL, FILLERS, FILLER_DELAY_MS, FILLER_LATE_MS,
                     FILLER_MAX_PER_CALL, FILLER_MIN_VOICE_MS, FILLER_SLOW_MS, NOISE_SUPPRESSION, VOICEPRINT,
                     VOICEPRINT_CALLER, VOICEPRINT_OTHER, GEMINI_LIVE_MODEL, GEMINI_VOICE, NOISE_GATE, MAX_CALL_SECONDS,
                     OFFERED_MODELS, PAGE_BUILD, STALE_PAGE, CALLER_GATE, CALLER_GATE_HIGH, CALLER_GATE_LOW, CALLER_GATE_MID_MS, PHONE_AFTER_LOST_REPLY_MS, PHONE_BARGEIN_MS, PHONE_PREPARING_MS,
                     PHONE_OUT_CHUNK_MS, PHONE_SEND_LEAD_MS, PHONE_END_SILENCE_MS, PHONE_LONG_ANSWER_MS, PHONE_LONG_TURN_SECONDS, PHONE_NUMBER, PHONE_RECORD, PHONE_SILENT_SECONDS,
                     PHONE_SILENT_GOODBYE_SECONDS, PHONE_STUCK_SECONDS, PHONE_REPLY_LATE_SECONDS, PHONE_UNCLEAR_SECONDS, PHONE_NOISE_ASK_SECONDS,
                     PHONE_SHORT_END_MS, PHONE_SHORT_TURN_MS, PHONE_VAD, PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_FROM_NUMBER,
                     PUBLIC_BASE_URL, RECORDINGS_DIR, RING_SECONDS, VOICES, gemini_voice, plivo_missing)
from .contacts import is_valid_phone, normalise_phone
from .live_bridge import ACTIVE, ACTIVITY_END, ACTIVITY_START, NOT_CALLER, SILENT_HANGUP, Nudge, run_call
from .openai_bridge import is_openai_model, voice_for
from .sarvam_bridge import is_sarvam_model
from .call_types import get_prompt
from .prompts import with_language

log = logging.getLogger("plivo")
router = APIRouter()
if VOICEPRINT in ("log", "on"):
    voiceprint.available()   # starts loading the speaker model now, so the first call has it

PLIVO_API = "https://api.plivo.com/v1/Account"
SEND_LEAD_SECONDS = PHONE_SEND_LEAD_MS / 1000   # how far ahead of real time we feed audio to the phone
ALLOWED_MODELS = set(OFFERED_MODELS)   # a dial asking for any other model gets the first of these
CHUNK_BYTES = PHONE_OUT_CHUNK_MS * 32   # 16 kHz 16-bit audio is 32 bytes per ms; under Plivo's 16 KB base64 advice
# Inbound phone frames are 20 ms each; these are the voice runs listen() needs, in frames.
BARGEIN_FRAMES = max(1, round(PHONE_BARGEIN_MS / 20))
AFTER_LOST_REPLY_FRAMES = max(1, round(PHONE_AFTER_LOST_REPLY_MS / 20))
PREPARING_FRAMES = max(1, round(PHONE_PREPARING_MS / 20))


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
        self.echo_near_sum = 0.0              # ...and what is left of it after echo cancellation
        self.gate_open_until = 0.0
        self.vad = vad.SpeechDetector() if vad.available() else None   # None = judge by loudness
        # WebRTC noise suppression + echo cancellation (line_audio); None when off or not installed.
        self.cleaner = line_audio.make_cleaner(AUDIO_CLEANUP, NOISE_SUPPRESSION, ECHO_CANCEL)
        # The caller's voice fingerprint (voiceprint, VOICEPRINT=log|on); None when off or the model is not loaded.
        self.voiceprint = (voiceprint.CallerVoice(VOICEPRINT_OTHER, VOICEPRINT_CALLER)
                           if VOICEPRINT in ("log", "on") and voiceprint.available() else None)
        self.run_audio = bytearray()          # voiced frames of the voice run being counted, for its fingerprint
        self.run_verdict: str | None = None   # that run's fingerprint verdict, checked once
        self.seg_audio = bytearray()          # voiced frames of the customer's current turn
        self.voice_level = 0.0                # running loudness of this customer's voice, for boost()
        # --- the caller's own level (CALLER_GATE): locked from their first clear speech ---
        self.caller_gate = CALLER_GATE and self.vad is not None
        self.caller_level = 0.0               # 0 until locked
        self.caller_sample: list[int] = []    # levels of confident voice frames in the caller's first turns
        self.last_prob = 0.0                  # voice score of the latest frame
        self.run_peak = 0                     # loudest frame of the voice run being counted while waiting
        self.seg_levels: list[int] = []       # levels of the voiced frames in the current turn
        # --- manual speech detection state (PHONE_VAD=manual) ---
        self.greeting_generated = False       # the agent's first turn is complete (still playing)
        self.greeted = False                  # ...and has finished playing on the phone
        self.listen_from = 0.0                # safety: listen anyway from this moment
        self.in_speech = False
        self.voiced_run = 0                   # loud frames in a row while waiting for speech
        self.quiet_run = 0                    # quiet frames since the last loud one
        self.reply_pending = False            # the customer finished; the agent's reply is on its way
        self.reply_due = 0.0
        self.reply_late = 0.0                 # when a reply passed its deadline with no sound of it (call 194)
        self.late_nudges = 0                  # "reply now" notes so far this call
        self.lost_replies = 0                 # replies in a row thrown away before a sound of them was played
        self.preroll: deque = deque(maxlen=50)  # last second, replayed when speech starts
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
        self.unclear = False                  # the customer's last sound had no words and nobody has asked them to repeat
        self.stuck_nudges = 0
        self.last_loud = False                # the latest frame was well above the noise floor
        self.recorder = None                  # wave file of the customer's audio (PHONE_RECORD)
        self.agent_recorder = None            # ...and of the agent's audio as the echo canceller lined it up
        self.meta: dict = {}                  # shared with run_call (see _start_agent)
        self.long_question = False            # the agent's last question has a long answer (an address, a number)
        self.pending_nudge = False            # the reply on its way answers a note from the line, not the customer
        self.noise_wait = 0                   # loud frames with no voice in them while waiting for an answer
        self.last_noise_at = 0.0
        self.last_loud_at = 0.0               # the latest loud frame while waiting for an answer
        self.faint_nudges = 0                 # "please repeat" after a sound too short or faint to count (call 175)
        self.gated_line = False               # the customer's phone sends digital silence between bursts (call 53)
        self.filler: fillers.Picker | None = None   # "hmm" / "achha" clips in this voice, loaded when the line opens
        self.filler_due = 0.0                 # when to play a filler if the reply has not started by then (0: none)
        self.filler_until = 0.0               # a filler is playing until then
        self.filler_skip = False              # the last reply had a filler: the next one goes without
        self.fillers_played = 0
        self.reply_delays: list[float] = []   # seconds from the end of each customer turn to the agent's first audio
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.answered = asyncio.Event()
        self.ws: WebSocket | None = None
        self.outbox: asyncio.Queue = asyncio.Queue()   # agent audio waiting to go to the phone
        self.sender: asyncio.Task | None = None
        self.silence: asyncio.Task | None = None
        self.fmt: dict = {"frames": 0}
        self.ids = {"stream": "", "call": ""}
        self.clear_task: asyncio.Task | None = None   # cutting the agent's audio on a barge-in
        self.resample_state = None
        self.task: asyncio.Task | None = None
        self.turn_t: dict = {}                # one turn's timing, from the customer's last voice to the phone (log_turn_timing)

    async def send_audio(self, pcm24k: bytes) -> None:
        """Called by the agent. Only queues: it must never wait on the phone network, because a
        slow phone line would then stall the Gemini connection until it times out."""
        if self.reply_pending and not self.in_speech:
            # The first sound of a reply. A customer sound that began before it is not talking over the
            # agent: counting it made a 0.1 s noise cut "850 rupaye" off as it began (call 90).
            self.voiced_run = 0
        self.reply_pending = False
        self.reply_late = 0.0
        self.lost_replies = 0
        self.filler_due = 0.0   # the reply has come
        if self.turn_t and "first_audio" not in self.turn_t:
            self.turn_t["first_audio"] = time.monotonic()
            self.reply_delays = (self.reply_delays + [self.turn_t["first_audio"] - self.turn_t["end"]])[-5:]
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
                start = max(self.play_until, time.monotonic())   # when this piece plays on the phone
                if self.cleaner is not None:
                    self.cleaner.played(chunk, start)   # the echo canceller's reference
                self.play_until = start + len(chunk) / 32000.0
                if "first_audio" in self.turn_t:
                    self.log_turn_timing()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("sending audio to the phone stopped: %s", exc)

    def log_turn_timing(self) -> None:
        """One line per reply: where the time goes between the customer's last voice and the agent's
        first audio leaving for the phone. Plivo and the phone network add their own delay after that."""
        t, self.turn_t = self.turn_t, {}
        sent_at = time.monotonic()
        meta = self.meta
        started = meta.get("started") or t["last_voice"]

        def mark(at: float | None) -> str:
            return "?" if not at else f"{at - started:.2f}s (+{(at - t['last_voice']) * 1000:.0f} ms)"

        turn_sent = meta.get("turn_sent") if (meta.get("turn_sent") or 0) >= t["end"] else None
        first_event = meta.get("first_event") if turn_sent else None
        log.info("turn timing: customer last voice %s | end of turn detected %s | sent to agent %s | first agent "
                 "event %s | first agent audio %s | first audio sent to phone %s | agent's own time %s",
                 mark(t["last_voice"]), mark(t["end"]), mark(turn_sent), mark(first_event), mark(t["first_audio"]),
                 mark(sent_at), f"{t['first_audio'] - turn_sent:.2f}s" if turn_sent else "?")

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

    def is_speech(self, pcm: bytes, cleaned: bytes | None = None) -> bool:
        """Is this frame the customer talking, as opposed to line noise or the agent's own echo?

        `cleaned` is the same frame after noise suppression and echo cancellation (line_audio). It decides
        how voice-like the frame is and, over the agent, how loud it is next to the echo left after
        cancelling. Every other loudness (noise floor, caller level, background) stays on the raw frame:
        those thresholds were tuned on raw audio, and suppression turns quiet speech down more than loud
        speech (replayed call 149: a soft "nahi aa rahi" fell under the caller gate and was lost)."""
        now = time.monotonic()
        cleaned = pcm if cleaned is None else cleaned
        level = audioop.rms(pcm, 2)
        near = audioop.rms(cleaned, 2) if cleaned is not pcm else level   # what is left after the echo is taken out
        self.levels.append(level)
        self.stats["frames"] += 1
        ordered = sorted(self.levels)
        floor = ordered[len(ordered) // 5]                 # 20th percentile = the background level
        threshold = max(300.0, floor * 2.2)

        echo = self.echo_sum / max(1, self.echo_n)
        echo_near = self.echo_near_sum / max(1, self.echo_n)
        agent_talking = self.agent_talking(now)
        if agent_talking and self.echo_n < 150:
            # Learn how loud the agent's own voice comes back on this line (first ~3 s of it).
            self.echo_sum += level
            self.echo_near_sum += near
            self.echo_n += 1
        # Over the agent, the customer is judged on what echo cancellation leaves. But when the line's delay
        # shifts the canceller lets the echo through for about 2 s while it re-learns (tested 6 Oct 2026), so
        # the raw frame must still be clearly above the line's usual echo: a looser bar than the 2.5x used
        # without cancelling, and one the echo alone cannot pass.
        over_echo = level >= echo * 1.5 if cleaned is not pcm else True

        if self.vad is not None:
            # Voice pattern, not loudness: a soft voice counts, a loud knock does not.
            # A quiet line (call 49: voice level under 400) scores low on the voice model, so
            # it is judged on a brought-up copy, the same as the agent would hear.
            probe = cleaned
            ref = self.voice_level or level
            if level >= 60 and ref < 1500:
                probe = audioop.mul(cleaned, 2, min(6.0, 2000.0 / max(ref, 200.0)))
            prob = self.vad.feed(probe)
            self.last_prob = prob
            self.fmt["vad_peak"] = max(self.fmt.get("vad_peak", 0.0), prob)
            # Well above the room's noise floor. On a hot, noisy speakerphone line (call 45: floor
            # over 1000, voice peaks 10000+) the voice model is only half sure of distorted speech,
            # so a clearly loud sound is accepted on a lower score. Lower still once a call has
            # already been caught stuck (lenient).
            loud = level >= max(300.0, floor * 3.0)
            if agent_talking:
                # The agent's own echo IS a voice, so over the agent loudness still has to clear it.
                speech = prob >= 0.6 and near >= max(echo_near * 2.5, 500.0) and over_echo
            else:
                speech = (prob >= 0.5 and level >= 60) or (prob >= (0.2 if self.lenient else 0.3) and loud)
                # Someone else talking in the room is a voice too, but a fainter one than the customer's
                # own. Once the customer's level is known, a voice at under 40% of it is background,
                # unless the voice model is all but certain.
                # Until the caller's level is locked; after that the caller gate in listen() decides.
                if (speech and self.BACKGROUND_GATE and not self.caller_level and self.voice_level >= 300 and not self.in_speech
                        and level < self.BG_RATIO * self.voice_level and prob < self.BG_PROB):
                    speech = False
                    self.stats["background"] = self.stats.get("background", 0) + 1
            if loud and not speech and not agent_talking:
                self.stats["loud_dropped"] += 1
                self.fmt["loud_dropped"] = self.fmt.get("loud_dropped", 0) + 1
        else:
            if agent_talking:
                # To interrupt the agent, the customer must be clearly louder than that echo.
                threshold = max(threshold, echo_near * 2.5, 900.0)
            speech = (near >= threshold and over_echo) if agent_talking else level >= threshold

        if speech:
            self.voice_level = level if not self.voice_level else 0.9 * self.voice_level + 0.1 * level
            if agent_talking:
                self.stats["bargein"] += 1
        self.last_loud = level >= max(300.0, floor * 3.0)
        return speech

    def end_pause(self, now: float) -> float:
        """How long a quiet gap closes the customer's turn, by what kind of answer it is (owner, 1 Oct 2026):
        - a short answer ("haan", "nahi", "hmm"): PHONE_SHORT_END_MS. Nothing more is coming; answer quickly.
        - a sentence: PHONE_END_SILENCE_MS, so a breath between two clauses does not cut it in half.
        - after a question with a long answer (an address, a number), for its first PHONE_LONG_TURN_SECONDS:
          PHONE_LONG_ANSWER_MS, plus 0.6 s on a phone that sends digital silence between bursts (call 53).
          Past that they are repeating themselves, not thinking between parts (call 101).
        A turn longer than 20 s is closed whatever happens (listen)."""
        zeros = sum(1 for lv in self.levels if lv <= 2)
        self.gated_line = zeros >= len(self.levels) * 0.5 and self.seg_voiced > 0
        if self.long_question and now - self.speech_started <= PHONE_LONG_TURN_SECONDS:
            return PHONE_LONG_ANSWER_MS / 1000 + (0.6 if self.gated_line else 0.0)
        if self.seg_voiced * 20 < PHONE_SHORT_TURN_MS:
            return PHONE_SHORT_END_MS / 1000
        return PHONE_END_SILENCE_MS / 1000

    def caller_band(self) -> str:
        """How the voice run being counted compares with the caller's locked level: "caller", "mid" or
        "background". Judged on the run's loudest frame: single frames inside a word can be very quiet."""
        if not self.caller_level:
            return "caller"
        ratio = self.run_peak / self.caller_level
        if ratio >= CALLER_GATE_HIGH:
            return "caller"
        # After a stuck line (the caller may have moved away or gone on speakerphone) nothing is dropped outright.
        return "mid" if ratio >= CALLER_GATE_LOW or self.lenient else "background"

    def waiting_check(self, now: float) -> list:
        """While the agent waits for an answer: is the line stuck (loud but nothing recognised)
        or dead quiet? Either way the agent hears nothing and would wait forever, so tell it."""
        if self.agent_talking(now) or self.reply_pending:
            self.wait_since, self.unclear = 0.0, False
            return []
        if not self.wait_since:
            self.wait_since, self.loud_wait, self.nudged, self.noise_wait = now, 0, False, 0
            return []
        if self.nudged:
            return []
        if self.last_loud:
            self.loud_wait += 1
            self.last_loud_at = now
            if self.last_prob < 0.5:
                self.noise_wait, self.last_noise_at = self.noise_wait + 1, now
        waited = now - self.wait_since
        note = ""
        if self.reply_late and not self.last_loud and self.late_nudges < 2:
            # Call 194: the customer said "haan ji bolo", the reply never came, and they heard 13 s of nothing and
            # then "hello ji, meri awaaz aa rahi hai?". They did answer: what is missing is the agent's reply.
            late = now - (self.turn_t.get("end") or self.reply_late) if self.turn_t else PHONE_REPLY_LATE_SECONDS
            self.reply_late, self.late_nudges = 0.0, self.late_nudges + 1
            log.info("no sound of the agent's reply %.1fs after the customer's turn; asking it to reply now (late nudge %s)",
                     late, self.late_nudges)
            note = (f"[Note from the phone line: the customer has heard nothing from you for {late:.0f} seconds and is waiting "
                    "for your reply. Say it now, starting straight with the reply itself: no line about what you are doing. "
                    "Do not ask whether they can hear you.]")
        elif self.reply_late:
            self.reply_late = 0.0
        if note:
            pass   # the missing reply comes first; the checks below are about the customer
        elif (self.noise_wait >= 15 and now - self.last_noise_at >= PHONE_NOISE_ASK_SECONDS
                and self.loud_wait < 50 and not self.last_loud):
            # "No speech": 0.3 s or more of loud sound with no voice in it (a muffled or distorted answer),
            # then quiet. They answered and are waiting; nothing reached the agent, so ask them to repeat.
            log.info("loud sound with no voice in it (%.1fs), then quiet for %.1fs; asking them to repeat",
                     self.noise_wait * 0.02, now - self.last_noise_at)
            self.noise_wait = 0
            note = ("[Note from the phone line: the customer made a sound but no voice could be made out, and they are "
                    "now waiting for you. Say 'sorry ji, aapki awaaz clear nahi aayi, ek baar repeat kar dijiye' and ask "
                    "your last question again in fewer words.]")
        elif self.loud_wait >= 50 and waited >= PHONE_STUCK_SECONDS and self.stuck_nudges < 3:
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
        elif self.unclear and self.loud_wait < 10 and waited >= PHONE_UNCLEAR_SECONDS:
            log.info("customer's last sound had no words and the line is quiet for %.1fs; asking them to repeat", waited)
            note = ("[Note from the phone line: the customer said something but no words could be made out, and they are "
                    "now waiting for you. Say 'sorry ji, aapki awaaz clear nahi aayi, ek baar repeat kar dijiye' and ask "
                    "your last question again in fewer words.]")
        elif (10 <= self.loud_wait < 50 and not self.last_loud and now - self.last_loud_at >= PHONE_SILENT_SECONDS
              and self.faint_nudges < 2):
            # Call 175: a short, quiet "nahi" was dropped as a background voice. Its loud frames kept the silence
            # check below from firing, and too few for the stuck check: a minute of nothing. Ask them to repeat.
            self.faint_nudges += 1
            log.info("a short or faint sound (%s loud frames) not taken as speech, then quiet for %.1fs; asking them to repeat",
                     self.loud_wait, now - self.last_loud_at)
            note = ("[Note from the phone line: the customer said something but it was too faint to make out, and they are "
                    "now waiting for you. Say 'sorry ji, aapki awaaz clear nahi aayi, ek baar repeat kar dijiye' and ask "
                    "your last question again in fewer words.]")
        elif (self.loud_wait < 10 or (not self.last_loud and now - self.last_loud_at >= PHONE_SILENT_SECONDS)) \
                and waited >= (PHONE_SILENT_GOODBYE_SECONDS if self.silent_nudges else PHONE_SILENT_SECONDS) \
                and self.silent_nudges < 3:
            self.silent_nudges += 1
            if self.silent_nudges == 3:
                # Owner, 13 Oct 2026 (call 228): asked twice and still nothing, so they cannot hear us. Hang up with
                # no goodbye (it used to be told to say one, and call 228's customer said "hello" over it).
                log.info("line quiet for %.0fs after asking twice; hanging up with no goodbye", waited)
                self.nudged = True
                return [SILENT_HANGUP]
            log.info("line quiet for %.0fs; asking the agent to check (silence nudge %s)", waited, self.silent_nudges)
            if self.silent_nudges == 1:
                note = (f"[Note from the phone line: nothing has been heard from the customer for {waited:.0f} seconds. "
                        "Ask once, briefly: 'hello ji, meri awaaz aa rahi hai?']")
            else:
                note = (f"[Note from the phone line: still nothing from the customer after another {waited:.0f} seconds. "
                        "Ask once more, briefly: 'hello? hello ji, meri awaaz aa rahi hai?' No goodbye.]")
        if not note:
            return []
        self.nudged = True
        self.reply_pending, self.reply_due = True, now + PHONE_REPLY_LATE_SECONDS   # the agent's reply is on its way
        self.pending_nudge = True
        return [Nudge(note)]

    def agent_talking(self, now: float) -> bool:
        """Is the agent's voice still coming out of the customer's phone? On a line with echo
        (speakerphone) the tail arrives late, so allow longer before trusting the line again."""
        echo = self.echo_sum / max(1, self.echo_n)
        # Audio still queued counts: it is about to play, though play_until has not caught up with it yet.
        return not self.outbox.empty() or now < self.play_until + (0.8 if echo > 100 else 0.3)

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
        raw = heard = pcm   # `heard` is what the agent gets; `pcm` is what our own listening judges
        if self.cleaner is not None:
            pcm = self.cleaner.capture(pcm, time.monotonic())
            if self.cleaner.echo:
                self.record_agent(self.cleaner.last_render if not self.cleaner.failed else b"\x00" * len(heard))
            if AUDIO_CLEANUP == "all":
                heard = pcm
        if PHONE_VAD != "manual":
            return [self.clean(heard)]
        now = time.monotonic()
        voiced = self.is_speech(raw, pcm)
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
        self.maybe_play_filler(now)
        if not self.in_speech:
            self.preroll.append(heard)
            # Count loud frames, forgiving the tiny dips inside a word (up to 60 ms).
            if voiced:
                self.voiced_run, self.quiet_run = self.voiced_run + 1, 0
                self.run_peak = max(self.run_peak, self.levels[-1])
                self.run_audio += raw
                if len(self.run_audio) > 4 * 32000:   # a long voice that keeps being ignored: keep its last 4 s
                    del self.run_audio[:len(self.run_audio) - 4 * 32000]
            else:
                self.quiet_run += 1
                if self.quiet_run > 3:
                    self.voiced_run, self.run_peak = 0, 0
                    self.run_audio.clear()
                    self.run_verdict = None
            # How much sound counts as "the customer is talking":
            # - 120 ms normally: rules out clicks, pops and knocks.
            # - PHONE_BARGEIN_MS over the agent: a cough, a "hmm" or a knock must not cut the agent off
            #   mid-sentence (history in config.py).
            # - PHONE_PREPARING_MS while the agent is preparing its reply. Any start marker in that gap throws
            #   the reply away; on real calls a breath or a bump there cost 4 to 8 s of silence.
            #   A customer who really carries on talking easily passes it.
            # - PHONE_AFTER_LOST_REPLY_MS while the agent is preparing its reply, once a reply has already
            #   been thrown away like that (call 90: each "hello?" threw away the reply to the one before).
            # The pre-roll is replayed when speech starts, so waiting loses no audio.
            # A filler is not the agent answering: someone carrying on talking under it still faces the bar that
            # protects the reply being prepared, not the lower barge-in one.
            self.seg_over_agent = self.agent_talking(now) and not (self.reply_pending and now < self.filler_until)
            if now > self.reply_due:
                if self.reply_pending:
                    self.reply_late = now   # its deadline passed with no sound of it: waiting_check says so
                self.reply_pending = False
            need = (BARGEIN_FRAMES if self.seg_over_agent
                    else (AFTER_LOST_REPLY_FRAMES if self.lost_replies else PREPARING_FRAMES) if self.reply_pending else 6)
            band = self.caller_band()
            verdict = self.check_run(band, need)
            if verdict == "caller" and band == "background" and VOICEPRINT == "on":
                # Quiet, but clearly the caller's own voice: they moved away from the phone or went on speaker.
                band = "caller"
            if verdict == "other" and VOICEPRINT == "on":
                # Clearly someone else near the phone, however loud: not the customer talking.
                self.stats["silenced"] += 1
                return self.waiting_check(now)
            if band == "mid":
                # Quieter than the caller: them only if it lasts (someone in the room speaks in short bursts).
                need = max(need, round(CALLER_GATE_MID_MS / 20))
            if band == "background" or (band == "mid" and self.reply_pending and self.lost_replies >= 2):
                # Far fainter than the caller: someone else in the room. And once two replies in a row have
                # been thrown away, only the caller's own voice may throw away a third (call 123: 12 in a row).
                if self.voiced_run == need:
                    self.stats["background"] = self.stats.get("background", 0) + 1
                self.stats["silenced"] += 1
                return self.waiting_check(now)
            if self.voiced_run < need:
                self.stats["silenced"] += 1
                return self.waiting_check(now)
            if self.reply_pending and not self.seg_over_agent and not self.pending_nudge:
                # This start marker throws away the reply being prepared. Not counted when that reply only
                # answered our own "hello ji?" note: the customer answering it late is not a stray sound.
                self.lost_replies += 1
            self.in_speech, self.voiced_run, self.quiet_run, self.run_peak = True, 0, 0, 0
            self.turn_t = {}
            self.filler_due = 0.0
            if now < self.filler_until:   # they carried on talking: stop the filler
                self.filler_until = 0.0
                self.clear_task = asyncio.get_running_loop().create_task(self.send_event({"type": "interrupted"}))
            self.speech_started = self.last_voiced = now
            self.seg_peak, self.seg_voiced, self.seg_levels = 0, 0, []
            self.seg_audio, self.run_audio, self.run_verdict = bytearray(self.run_audio), bytearray(), None
            self.silent_nudges = 0   # they spoke: a later silence gets its two "hello?" checks again
            out = [ACTIVITY_START, *(self.boost(f) for f in self.preroll)]
            self.preroll.clear()
            if self.seg_over_agent and self.meta.get("provider") in ("openai", "sarvam"):
                # OpenAI finishes writing a reply in about a second, long before the phone has
                # played it, and only reports a barge-in while it is still writing. So the agent
                # talked straight over the customer (call 70). Cut the queued audio here instead.
                # Sarvam's voice is just as far ahead of the phone.
                log.info("customer talked over the agent; stopping its audio")
                self.clear_task = asyncio.get_running_loop().create_task(self.send_event({"type": "interrupted"}))
            return out
        if voiced:
            self.last_voiced = now
            self.seg_voiced += 1
            if len(self.seg_audio) < 10 * 32000:
                self.seg_audio += raw
            self.seg_peak = max(self.seg_peak, self.levels[-1])
            self.seg_levels.append(self.levels[-1])
            if (self.caller_gate and not self.caller_level and self.last_prob >= 0.8
                    and not self.seg_over_agent and not self.agent_talking(now)):
                self.caller_sample.append(self.levels[-1])
                if len(self.caller_sample) >= 75:   # 1.5 s of clear voice
                    ordered = sorted(self.caller_sample)
                    self.caller_level = float(ordered[int(len(ordered) * 0.9)])
                    log.info("caller level locked at %.0f (from %s clear voice frames)", self.caller_level, len(ordered))
        pause = self.end_pause(now)
        if now - self.last_voiced >= pause or now - self.speech_started > 20:
            self.in_speech = False
            self.reply_pending, self.reply_due, self.pending_nudge = True, now + PHONE_REPLY_LATE_SECONDS, False   # cleared when the agent's audio arrives
            self.turn_t = {"last_voice": self.last_voiced, "end": now}
            if self.caller_level and len(self.seg_levels) >= 25:
                # The caller came through louder than the locked level (moved closer): follow them up,
                # gently. Never down: quieter voices are exactly what the gate has to tell apart.
                p90 = sorted(self.seg_levels)[int(len(self.seg_levels) * 0.9)]
                if p90 > self.caller_level:
                    self.caller_level = 0.7 * self.caller_level + 0.3 * p90
            # Real replies are long and loud; a knock or a clatter is short. Used to tune the detector.
            log.info("customer sound: %.1fs of voice, loudest level %s, usual voice level %.0f, over the agent: %s",
                     self.seg_voiced * 0.02, self.seg_peak, self.voice_level, "yes" if self.seg_over_agent else "no")
            if self.check_turn():
                # Someone else near the phone: the agent holds back its reply (live_bridge), and no reply is on its way.
                self.reply_pending, self.turn_t = False, {}
                return [self.boost(heard), NOT_CALLER, ACTIVITY_END]
            self.plan_filler(now)
            return [self.boost(heard), ACTIVITY_END]
        if self.vad is not None and now - self.last_voiced > 0.5:
            # A real gap between the customer's words: turn the room noise down so the agent
            # hears mostly voice. Gently, and after half a second: on call 49 a 3-second answer
            # on a quiet line was mostly "gap" to the detector, and turning it down to 15% (and
            # unboosted) left Gemini with nothing to transcribe but ".".
            return [audioop.mul(self.boost(heard), 2, 0.4)]
        return [self.boost(heard)]

    def check_run(self, band: str, need: int) -> str | None:
        """The fingerprint verdict on the voice run being counted, worked out once it has enough voice."""
        vp = self.voiceprint
        if vp is None or not vp.ready or self.run_verdict is not None:
            return self.run_verdict
        if len(self.run_audio) < int(voiceprint.CHECK_SECONDS * 32000):
            return None
        sim = vp.similarity(bytes(self.run_audio))
        self.run_verdict = vp.verdict(sim, len(self.run_audio) / 32000)
        if sim is not None and (self.run_verdict == "other" or (self.run_verdict == "caller" and band == "background")):
            when = ("over the agent" if self.seg_over_agent else "while the agent prepares a reply" if self.reply_pending
                    else "while waiting for the customer")
            act = "ignored as someone else" if self.run_verdict == "other" else "accepted as the caller though quiet"
            if self.run_verdict == "caller":
                vp.stats["rescued"] += 1
            log.info("voice check: a voice %s, similarity %.2f to the caller (loudness says %s): %s%s", when, sim, band,
                     act, "" if VOICEPRINT == "on" else " (log only, nothing changed)")
        return self.run_verdict

    def check_turn(self) -> bool:
        """A finished turn: learn the caller's voice from it, or check it against them. True when the turn was
        clearly someone else and VOICEPRINT=on, so the reply to it must be held back."""
        vp = self.voiceprint
        if vp is None:
            return False
        audio, self.seg_audio = bytes(self.seg_audio), bytearray()
        clean = not self.seg_over_agent and (not self.caller_level or self.seg_peak >= 0.4 * self.caller_level)
        sim, what = vp.turn(audio, clean)
        secs = len(audio) / 32000
        if sim is None:
            log.info("voice check: turn of %.1fs of voice: %s", secs, "too short to check" if what == "unknown" else what)
            return False
        hold = what == "other"
        meaning = {"other": "someone else", "caller": "the caller", "unsure": "not sure, treated as the caller",
                   "handover": "a new voice for the second time running; taking it as the phone handed over and learning it"}
        log.info("voice check: turn of %.1fs of voice, similarity %.2f to the caller: %s%s", secs, sim, meaning[what],
                 (", reply held back" if VOICEPRINT == "on" else ", reply would be held back (log only)") if hold else "")
        return hold and VOICEPRINT == "on"

    async def attach(self, ws: WebSocket) -> None:
        self.ws = ws
        waiting = self.outbox.qsize()
        self.sender = asyncio.create_task(self._send_loop(ws))
        if FILLERS:
            # Always in the voice the agent speaks in: a ChatGPT call speaks in an OpenAI voice.
            # Sarvam has no clips made, so its calls get none rather than another agent's voice.
            folder = (fillers.openai_key(voice_for(self.voice)) if is_openai_model(self.model)
                      else f"sarvam-{self.voice}" if is_sarvam_model(self.model) else gemini_voice(self.voice))
            self.filler = fillers.Picker(folder)
            log.info("filler sounds: %s", f"{len(self.filler.clips)} clips in voice {folder}, played when the wait is "
                     "long or longer than usual" if self.filler.clips else
                     f"none (no clips made for voice {folder}; scripts/make_fillers.py)")
        log.info("audio line attached; %s prepared audio chunks waiting", waiting)
        log.info("timing: end pause %s ms (short answer %s ms, long answer %s ms), send lead %s ms, chunk %s ms, "
                 "barge-in %s ms, while preparing %s ms, after a lost reply %s ms", PHONE_END_SILENCE_MS, PHONE_SHORT_END_MS,
                 PHONE_LONG_ANSWER_MS, PHONE_SEND_LEAD_MS, PHONE_OUT_CHUNK_MS, BARGEIN_FRAMES * 20,
                 PREPARING_FRAMES * 20, AFTER_LOST_REPLY_FRAMES * 20)
        log.info("audio clean-up: %s", "off" if self.cleaner is None else
                 f"noise suppression {'on' if NOISE_SUPPRESSION else 'off'}, echo cancellation "
                 f"{'on' if self.cleaner.echo else 'off'}; the agent hears the "
                 f"{'cleaned' if AUDIO_CLEANUP == 'all' else 'original'} audio")

    def plan_filler(self, now: float) -> None:
        """A finished answer: plan a filler for if the reply is late (owner, 6 Oct 2026). Not after a sound shorter
        than a real answer, not after talking over the agent, and at most every other reply (call 72: a "hmm"
        after every reply, and after sounds that were not answers)."""
        if (self.filler is None or not self.filler.clips or self.seg_over_agent
                or (FILLER_MAX_PER_CALL and self.fillers_played >= FILLER_MAX_PER_CALL)):
            return
        if self.seg_voiced * 20 < FILLER_MIN_VOICE_MS and not self.short_clear_answer():
            return
        if self.filler_skip:
            self.filler_skip = False   # the last reply had one
            return
        self.filler_due = now + self.filler_wait()

    def short_clear_answer(self) -> bool:
        """A short answer ("haan ji", "no") said clearly, to a slow agent. Call 164: after a 0.2 s "haan ji" to "kya meri
        baat Jyoti ji se ho rahi hai?" Gemini was silent 4.6 s, the longest and most awkward gap of the call, and the
        0.4 s rule kept the filler out. At the customer's own loudness, not a faint sound, and never to a quick agent."""
        return (self.seg_voiced * 20 >= 160 and self.usual_wait() >= FILLER_SLOW_MS / 1000
                and self.seg_peak >= 0.4 * (self.caller_level or self.voice_level or 1500))

    def usual_wait(self) -> float:
        """This call's usual time from the end of the customer's turn to the agent's first audio."""
        return (sorted(self.reply_delays)[len(self.reply_delays) // 2] if self.reply_delays
                else 1.6 if is_openai_model(self.model) else 2.0 if is_sarvam_model(self.model)   # Sarvam: a guess, not measured
                else 3.5)   # measured on calls 145-160 until this call has its own

    def filler_wait(self) -> float:
        """How far into the wait a filler goes: early in a gap that is always long (Gemini), and for a quick agent
        (ChatGPT) only once this reply is clearly later than its usual (the gap growing, owner 6 Oct 2026)."""
        usual = self.usual_wait()
        if usual >= FILLER_SLOW_MS / 1000:
            return FILLER_DELAY_MS / 1000
        return usual + FILLER_LATE_MS / 1000

    def maybe_play_filler(self, now: float) -> None:
        """The planned filler, once its time comes, if the reply still has not started."""
        if not self.filler_due or now < self.filler_due:
            return
        self.filler_due = 0.0
        if not self.reply_pending or self.in_speech or not self.outbox.empty() or now < self.play_until:
            return
        name, clip = self.filler.next()
        for i in range(0, len(clip), CHUNK_BYTES):
            self.outbox.put_nowait(clip[i:i + CHUNK_BYTES])
        self.filler_until = now + len(clip) / 32000 + 0.1
        self.fillers_played += 1
        self.filler_skip = True
        log.info("filler %r: the reply had not started %.1fs after the customer's answer (usual wait on this call %s)", name,
                 now - self.turn_t.get("end", now) if self.turn_t else 0.0,
                 f"{sorted(self.reply_delays)[len(self.reply_delays) // 2]:.1f}s" if self.reply_delays else "not known yet")

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
            if self.cleaner is not None and self.cleaner.echo:
                # Same length as the customer's file, frame for frame, so echo cancellation can be
                # replayed offline (scripts/replay.py picks it up).
                self.agent_recorder = wave.open(str(RECORDINGS_DIR / f"call_{call_id}_agent.wav"), "wb")
                self.agent_recorder.setnchannels(1)
                self.agent_recorder.setsampwidth(2)
                self.agent_recorder.setframerate(16000)
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

    def record_agent(self, pcm: bytes) -> None:
        if self.agent_recorder is not None:
            try:
                self.agent_recorder.writeframes(pcm)
            except Exception:  # noqa: BLE001
                self.agent_recorder = None

    def stop_recording(self) -> None:
        for rec in (self.recorder, self.agent_recorder):
            if rec is not None:
                try:
                    rec.close()
                except Exception:  # noqa: BLE001
                    pass
        self.recorder = self.agent_recorder = None

    async def send_event(self, evt: dict) -> None:
        if evt.get("type") == "heard_nothing":
            # Gemini heard no words in that sound (a "hmm", a breath) and its reply will be held back
            # (live_bridge), so no reply is on its way: listen for the real answer as usual, not with
            # the stricter bar that protects a reply in preparation.
            self.reply_pending = False
            self.turn_t = {}   # the reply to it is held back, never played
            self.filler_due = 0.0
            # On the first such sound in a row the agent is told nothing (live_bridge), so if the
            # customer now waits, waiting_check asks them to repeat. Later ones get live_bridge's own note.
            self.unclear = bool(evt.get("first"))
        if evt.get("type") == "turn_complete":
            self.greeting_generated = True
            # After "address batayein?" the answer is long and comes with pauses; give it time.
            self.long_question = bool(evt.get("long_answer"))
        # The customer talked over the agent: drop whatever audio Plivo still has queued.
        if evt.get("type") == "interrupted":
            while not self.outbox.empty():
                self.outbox.get_nowait()
            self.play_until = time.monotonic()
            self.filler_due = self.filler_until = 0.0
            if self.cleaner is not None:
                self.cleaner.clear_played()
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
async def dial(contact_id: int, voice: str = GEMINI_VOICE, to: str = "", model: str = "", language: str = "",
               build: str = "") -> dict:
    """Place one real phone call. This costs money: Plivo per minute plus Gemini per minute."""
    if build != PAGE_BUILD:
        raise HTTPException(409, STALE_PAGE)
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
    # The order and its products, fresh from the main database (at most a few seconds; never stops the call).
    contact = await catalog.enrich(contact)
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
    pc = PendingCall(contact, voice, model if model in ALLOWED_MODELS else OFFERED_MODELS[0], to_number)
    pc.token = token
    pc.request_uuid = str(r.json().get("request_uuid") or "")   # lets us cancel while it is still ringing
    PENDING[token] = pc
    _start_agent(pc)
    # Plivo's hang-up webhook normally lands a second after the ring times out. The margin is for
    # a slow tunnel, not for a slow carrier, so it is short: a call stuck on "Ringing" with the
    # customer long gone is worse than releasing the agent a little early.
    asyncio.create_task(_expire(token, RING_SECONDS + 10))
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
        # Let the goodbye finish playing on the phone, then hang up at once (owner, 7 Oct 2026: a fixed 0.6 s
        # here plus 0.4 s in live_bridge left a second of silence). The hang-up request itself takes ~0.4 s to
        # reach Plivo, which covers the phone network's playback lag.
        for _ in range(400):
            if pc.outbox.empty() and time.monotonic() >= pc.play_until:
                break
            await asyncio.sleep(0.025)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("phone call %s failed", token)
    finally:
        # First, before the recording is saved and the line report written: nothing should delay the hang-up.
        hangup = asyncio.create_task(_hangup_call(pc.ids["call"]))
        reader_task.cancel()
        pc.stop_tasks()
        pc.stop_recording()
        PENDING.pop(token, None)
        st = pc.stats
        if st["frames"]:
            ordered = sorted(pc.levels) or [0]
            log.info("line report: background level %s, agent echo level %.0f (%.0f after echo cancellation), customer voice "
                     "level %.0f, caller level %.0f, "
                     "%d%% of frames silenced, %s loud frames not taken as speech, %s background voices ignored, "
                     "%s loud-enough-to-interrupt frames, gated line: %s, stuck nudges %s, silence nudges %s, voice checks: %s",
                     ordered[len(ordered) // 5], pc.echo_sum / max(1, pc.echo_n),
                     pc.echo_near_sum / max(1, pc.echo_n), pc.voice_level, pc.caller_level,
                     100 * st["silenced"] // st["frames"], st["loud_dropped"], st.get("background", 0), st["bargein"],
                     "yes" if pc.gated_line else "no", pc.stuck_nudges, pc.silent_nudges,
                     "off" if pc.voiceprint is None else
                     (f"profile {'ready' if pc.voiceprint.ready else 'never ready'}, {pc.voiceprint.stats['checked']} checked, "
                      f"{pc.voiceprint.stats['other']} other voices, {pc.voiceprint.stats['rescued']} quiet caller rescued, "
                      f"{pc.voiceprint.stats['handovers']} handovers ({VOICEPRINT})"))
            log.info("fillers played: %s", pc.fillers_played if pc.filler is not None else "off")
        await hangup
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
