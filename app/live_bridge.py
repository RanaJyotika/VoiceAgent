"""Bridge between an audio source (browser now, Plivo later) and a Gemini Live session.

The source pushes 16 kHz 16-bit PCM chunks in; we push 24 kHz PCM chunks out, plus
JSON events (transcripts, interruptions, outcome, end). Both directions run as
independent tasks so neither blocks the other.
"""
import asyncio
import audioop
import contextlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types

from . import db, openai_bridge
from .db import IST
from .config import (AFFECTIVE_DIALOG, GEMINI_LIVE_MODEL, GEMINI_PRICE_PER_M, GEMINI_THINKING_BUDGET,
                     GEMINI_VERTEX, GEMINI_VOICE, GOOGLE_API_KEY, OUTPUT_SAMPLE_RATE,
                     OPENAI_FALLBACK, OPENAI_PRICE_PER_M,
                     OPENAI_REALTIME_MODEL, PROVIDER_COOLDOWN_SECONDS, REPLY_WATCHDOG_SECONDS,
                     TRANSCRIPT_LANGUAGES)
from .tools import TOOLS

log = logging.getLogger("bridge")

# Calls that are ringing or in progress right now, for the dashboard's live panel.
ACTIVE: dict[int, dict] = {}

# Markers an audio source can put on the incoming queue when it does its own speech detection
# (manual_vad): "the customer started talking" / "the customer stopped talking".
# With affective dialog on, the model's private mood notes sometimes leak into the written
# transcript: "emotion_user\ncalmness<ctrl95>emotion_model\ncalmness<ctrl95>". They are not spoken.
_MOOD_NOTE = re.compile(r"emotion_(?:user|model)\s*[a-zA-Z_]*\s*(?:<ctrl\d+>)?")


def clean_agent_text(text: str) -> str:
    return re.sub(r"<ctrl\d+>", "", _MOOD_NOTE.sub("", text)).strip()


_NOTHING = re.compile(r"^[\s.,!?…\-–—'\"()\[\]]*$")
_REPEAT_WORDS = re.compile(r"sorry|phir|dobara|dubara|awaaz|awaz|sun(?:ai|a)? nahi|फिर|दोबारा|दुबारा|आवाज़|आवाज|सुनाई नहीं|repeat", re.IGNORECASE)
# Letters that can carry a customer's words on these calls: Latin, digits, Devanagari and the other
# Indian scripts, Arabic (Urdu). Anything else on a Hindi call is the transcriber guessing at noise:
# call 53 transcribed a fragment as Thai.
_OUR_LETTERS = re.compile(r"[A-Za-z0-9ऀ-෿؀-ۿ٠-٩०-९]")
_ANY_LETTER = re.compile(r"[^\W\d_]")
_YES = re.compile(r"\b(haan|han|ha|haa|hanji|ji|jee|sahi|sahee|theek|thik|ok|okay|yes|yeah|confirm|bilkul|kar do|kar dijiye|"
                  r"kardo|karo|done|right|correct|हाँ|हां|हा|जी|सही|ठीक|बिल्कुल|कर दो|कर दीजिए)\b", re.IGNORECASE)
_NO = re.compile(r"\b(nahi|nahin|nai|nhi|no|not|galat|wrong|cancel|mat|नहीं|नही|गलत|मत)\b", re.IGNORECASE)


def heard_nothing(customer_text: str) -> bool:
    """A customer turn happened but its transcript carries no words: '.', '<noise>', a dash, or
    a script that cannot be this customer's language."""
    t = customer_text.strip()
    if not t:
        return False
    bare = t.replace("<noise>", "").replace("[noise]", "")
    if _NOTHING.match(bare):
        return True
    return _ANY_LETTER.search(bare) is not None and _OUR_LETTERS.search(bare) is None


def said_yes(customer_text: str) -> bool:
    """A clear agreement with no refusal in the same breath."""
    t = customer_text.strip()
    return bool(t) and not heard_nothing(t) and _YES.search(t) is not None and _NO.search(t) is None


# Digits a customer may say as words. Used to check that a number the model wants to record was
# actually spoken: on call 54 it called record_address with "Sector 45, Gurgaon, 122003" and later
# "641002" when the customer had only said "house number 302".
_DIGIT_WORDS = {
    "zero": "0", "shunya": "0", "शून्य": "0", "ek": "1", "एक": "1", "do": "2", "दो": "2", "teen": "3", "tin": "3", "तीन": "3",
    "char": "4", "chaar": "4", "चार": "4", "paanch": "5", "panch": "5", "पांच": "5", "पाँच": "5", "chhe": "6", "che": "6",
    "chah": "6", "chhah": "6", "छह": "6", "छे": "6", "छः": "6", "saat": "7", "sat": "7", "सात": "7", "aath": "8", "ath": "8",
    "आठ": "8", "nau": "9", "नौ": "9", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
}
# A run of digits, or a word: anything between spaces and punctuation (Devanagari vowel signs are not
# "word characters" to the regex engine, so \w+ would cut "पांच" into pieces).
_DIGIT_RUN = re.compile(r"\d+|[^\s\d.,;:!?()\[\]/\-]+")


def spoken_digits(text: str) -> str:
    """Every digit in the text, in order: numerals in any script plus digit words ("ek saat chhe")."""
    out = []
    for tok in _DIGIT_RUN.findall(text or ""):
        if tok[0].isdigit():
            out.append("".join(str(unicodedata.digit(ch)) for ch in tok))
        else:
            out.append(_DIGIT_WORDS.get(tok.lower(), ""))
    return "".join(out)


def spoken_digits_spaced(text: str) -> str:
    """Like spoken_digits, but each number stays separate: "Tower 10, 502, 5th floor" -> "10 502 5"."""
    out = []
    for tok in _DIGIT_RUN.findall(text or ""):
        if tok[0].isdigit():
            out.append("".join(str(unicodedata.digit(ch)) for ch in tok))
        elif tok.lower() in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok.lower()])
    return " ".join(out)


_PLACEHOLDER = re.compile(r"^(unknown|not (given|provided|known|available)|n/?a|none|nil|null|-+|\?+)$", re.IGNORECASE)
_PART_WORDS = re.compile(r"house|हाउस|flat|फ्लैट|makaan|मकान|area|एरिया|sector|सेक्टर|society|सोसाइटी|colony|कॉलोनी|street|"
                         r"स्ट्रीट|gali|गली|city|सिटी|shehar|शहर|pin|पिन|landmark|लैंडमार्क", re.IGNORECASE)


def asks_for_address_part(agent_text: str) -> str:
    """The short question the agent has just asked for an address part, if its current turn ends in
    one ("aur area ya society?", "PIN code?"), else "". A long read-back or recap question ("...PIN
    1 4 0 6 0 3, sahi hai?", "sab sahi hai?") is not an ask, so it returns ""."""
    text = clean_agent_text(agent_text)
    if not text.rstrip().endswith("?"):
        return ""
    sentences = [x.strip() for x in re.split(r"(?<=[?।.!])\s+", text) if x.strip()]
    last = sentences[-1] if sentences else ""
    if last.endswith("?") and len(last.split()) <= 7 and _PART_WORDS.search(last) and not re.search(r"\d", last):
        return last
    return ""


def asked_to_repeat(agent_text: str) -> bool:
    return _REPEAT_WORDS.search(clean_agent_text(agent_text)) is not None


# Questions whose answer is long (an address, a number, "what did you order") get a longer pause
# before the turn is closed; a yes/no gets a short one. Decided from the agent's own words.
_LONG_ANSWER = re.compile(r"address|एड्रेस|पता|house|हाउस|flat|फ्लैट|makaan|मकान|area|एरिया|sector|सेक्टर|society|सोसाइटी|"
                          r"colony|कॉलोनी|city|सिटी|शहर|pin|पिन|landmark|number|नंबर|kya order|क्या ऑर्डर|kaun ?sa|कौन ?सा|"
                          r"kitne|कितने|kab |कब |time|टाइम|naam|नाम|bata|बता", re.IGNORECASE)


def expects_long_answer(agent_text: str) -> bool:
    return _LONG_ANSWER.search(clean_agent_text(agent_text)) is not None


# What to tell the agent after the customer's reply came through as nothing, by how many times in a
# row it has happened for the same question. Escalation lives here, not in the prompt.
INAUDIBLE_NOTES = {
    1: "[Note from the phone line: the customer's last reply was not audible at all; nothing they said reached you, so "
       "you have not had an answer to your previous question. Do not go on. Say 'sorry ji, aapki awaaz nahi aayi' and "
       "ask that previous question again, in fewer words.]",
    2: "[Note from the phone line: the customer's reply was inaudible again, twice now. Ask them once, warmly, to hold the "
       "phone a little closer and speak slowly ('ji, phone thoda paas rakh ke dheere se boliye'), then ask the same "
       "question again. Do not move on.]",
    3: "[Note from the phone line: three replies in a row could not be heard. Do not ask this question again. If it was "
       "about the address or another detail: say 'koi baat nahi, team aapse le legi', put that in mark_outcome notes, and "
       "carry on with the next step. If it was the final confirmation or the opening: say the line is bad and you will "
       "call back a little later ('awaaz nahi aa rahi, hum thodi der mein dobara call karte hain'), then call end_call "
       "without mark_outcome.]",
}


MAX_RECONNECTS = 3
_refused_models: set[str] = set()   # models that closed a session with "1007 invalid argument"

_hints_ok = True # set to False once a model refuses the transcript language hint

ACTIVITY_START = object()
ACTIVITY_END = object()


class Nudge(str):
    """A note from the audio source for the agent's ears only, e.g. "the line has been noisy for
    8 seconds". Goes to Gemini as a text turn; never spoken and never in the transcript."""


def is_billing_error(exc: BaseException) -> bool:
    """Out of credits, billing off, quota gone: retrying cannot help, so fail fast and say why."""
    text = str(exc).lower()
    return any(w in text for w in ("credits", "billing", "quota", "exhausted", "payment"))


def make_client() -> genai.Client:
    if GEMINI_VERTEX:
        return genai.Client(vertexai=True, api_key=GOOGLE_API_KEY)
    return genai.Client(api_key=GOOGLE_API_KEY)


def turn_usage(um: Any) -> dict[str, int]:
    """Token counts of one model turn, split by modality, from a usage_metadata message."""
    out = {"prompt_tokens": um.prompt_token_count or 0, "response_tokens": um.response_token_count or 0,
           "text_in": 0, "audio_in": 0, "text_out": 0, "audio_out": 0}
    for details, kind in ((um.prompt_tokens_details, "in"), (um.response_tokens_details, "out")):
        for d in details or []:
            modality = str(getattr(d, "modality", "") or "").upper()
            key = ("audio" if "AUDIO" in modality else "text") + "_" + kind
            out[key] += d.token_count or 0
    # Repeated prompt text that the provider billed at its cheap cached rate (OpenAI only;
    # Gemini Live has no such discount, which is why its text input dominates a call's cost).
    out["cached_text_in"] = int(getattr(um, "cached_text_in", 0) or 0)
    out["cached_audio_in"] = int(getattr(um, "cached_audio_in", 0) or 0)
    return out


def usage_cost_usd(usage: dict[str, Any], prices: dict[str, float] | None = None) -> float:
    return sum(usage.get(k, 0) * price / 1_000_000 for k, price in (prices or GEMINI_PRICE_PER_M).items())


# Providers that just failed for a reason retrying cannot fix, and the moment they may be tried
# again. Shared by every call on this server run.
_provider_down: dict[str, float] = {}


def gemini_is_down() -> bool:
    return time.monotonic() < _provider_down.get("gemini", 0.0)


def can_fall_back(model: str, exc: BaseException) -> bool:
    """Should this call leave Gemini for OpenAI? Only when Gemini cannot take the call at all:
    out of credit, key refused, or it has already failed to reconnect the allowed number of
    times. A single internal error is a blip and reconnecting to Gemini fixes it."""
    if not OPENAI_FALLBACK or openai_bridge.is_openai_model(model):
        return False
    return is_billing_error(exc) or "api key" in str(exc).lower() or "permission" in str(exc).lower()


SendAudio = Callable[[bytes], Awaitable[None]]
SendEvent = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class CallState:
    call_id: int
    started: float = field(default_factory=time.monotonic)
    transcript: list[dict[str, str]] = field(default_factory=list)
    outcome: str | None = None
    outcome_details: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    _turn_usage: dict[str, int] | None = None   # latest usage report for the turn in progress
    ended: bool = False
    speaking: bool = False
    idle_since: float | None = None     # when the agent last finished a turn
    loud_since: float | None = None     # first clear customer sound after that
    nudges: int = 0
    address_checked: bool = False       # mark_outcome was already sent back once for a missing PIN code
    address_refusals: int = 0           # record_address sent back because the digits did not match
    address: dict[str, str] | None = None   # what record_address accepted
    inaudible_streak: int = 0           # customer replies in a row that carried no words
    inaudible_total: int = 0
    confirm_refusals: int = 0           # mark_outcome "confirmed" sent back for want of a clear yes
    reconnects: int = 0
    error: str | None = None
    _in_buf: str = ""
    _out_buf: str = ""
    speech_until: float = 0.0                       # when the audio already sent finishes playing
    provider: str = "gemini"                        # which voice agent answered this call
    prices: dict[str, float] = field(default_factory=lambda: GEMINI_PRICE_PER_M)

    def add_input_text(self, text: str) -> None:
        self._in_buf += text

    def add_output_text(self, text: str) -> None:
        self._out_buf += text

    def note_usage(self, um: Any) -> None:
        # Gemini reports a turn's usage as a running total, so keep the latest and add it up
        # once the turn is over (flush_turn). Each turn's prompt count is the whole conversation
        # so far, and each turn is billed, which is why long calls cost far more than short ones.
        self._turn_usage = turn_usage(um)

    def close_usage_turn(self) -> None:
        if not self._turn_usage:
            return
        total = self.usage or {"turns": 0}
        total["turns"] = total.get("turns", 0) + 1
        for k, v in self._turn_usage.items():
            total[k] = total.get(k, 0) + v
        total["total_tokens"] = total["prompt_tokens"] + total["response_tokens"]
        total["cost_usd"] = round(usage_cost_usd(total, self.prices), 5)
        self.usage = total
        self._turn_usage = None

    def recent_customer_turns(self, n: int = 2) -> list[str]:
        """The last n things the customer said, latest last (the unfinished turn included)."""
        said = [t["text"] for t in self.transcript if t["role"] == "customer"]
        if self._in_buf.strip():
            said.append(self._in_buf.strip())
        return said[-n:]

    def flush_turn(self) -> None:
        self.close_usage_turn()
        if self._in_buf.strip():
            self.transcript.append({"role": "customer", "text": self._in_buf.strip()})
        if clean_agent_text(self._out_buf):
            self.transcript.append({"role": "agent", "text": clean_agent_text(self._out_buf)})
        self._in_buf = ""
        self._out_buf = ""


def build_config(system_prompt: str, voice: str = GEMINI_VOICE,
                 sensitive_listening: bool = False, fast: bool = True,
                 manual_vad: bool = False, language_hints: bool = False) -> types.LiveConnectConfig:
    extra: dict[str, Any] = {}
    heard = types.AudioTranscriptionConfig(language_codes=TRANSCRIPT_LANGUAGES) if language_hints \
        else types.AudioTranscriptionConfig()
    if manual_vad:
        # The audio source marks speech itself with ACTIVITY_START / ACTIVITY_END.
        detection = types.AutomaticActivityDetection(disabled=True)
    else:
        detection = types.AutomaticActivityDetection(
            disabled=False,
            # LOW = a sound must be clearly speech before it counts as the customer talking.
            # HIGH made line noise interrupt the agent mid-sentence on real phone calls.
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            prefix_padding_ms=200,
            silence_duration_ms=500 if sensitive_listening else 600,
        )
    if AFFECTIVE_DIALOG:
        # Lets the model match the caller's tone and emotion instead of a flat read.
        extra["enable_affective_dialog"] = True
    if fast and GEMINI_THINKING_BUDGET >= 0:
        # The model otherwise "thinks" for about 3 seconds before every reply. On a phone call
        # that silence is worse than a slightly less considered sentence.
        extra["thinking_config"] = types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET)
    return types.LiveConnectConfig(
        **extra,
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        tools=TOOLS,
        input_audio_transcription=heard,
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=detection),
    )


async def run_call(
    *,
    contact: dict | None,
    system_prompt: str,
    channel: str,
    incoming_audio: "asyncio.Queue[bytes | None]",
    send_audio: SendAudio,
    send_event: SendEvent,
    model: str = GEMINI_LIVE_MODEL,
    voice: str = GEMINI_VOICE,
    sensitive_listening: bool = False,
    manual_vad: bool = False,
    start_gate: "asyncio.Event | None" = None,
    meta: dict | None = None,
) -> CallState:
    """Run one full conversation. Returns when the agent ends the call or the source closes.

    start_gate: for phone calls. We connect to Gemini while the phone is still ringing and
    only start the greeting when the gate opens (the customer picked up), so the greeting
    is not delayed by connection setup. Waiting connected costs nothing.
    """
    if model in _refused_models:
        log.warning("%s refused an earlier session on this run; using %s for this call instead", model, GEMINI_LIVE_MODEL)
        model = GEMINI_LIVE_MODEL
    if gemini_is_down() and not openai_bridge.is_openai_model(model):
        left = _provider_down["gemini"] - time.monotonic()
        log.warning("Gemini failed recently; this call goes straight to %s (retrying Gemini in %.0f min)",
                    OPENAI_REALTIME_MODEL, left / 60)
        model = OPENAI_REALTIME_MODEL
    call_id = db.start_call(contact["id"] if contact else None, channel)
    state = CallState(call_id=call_id)
    if openai_bridge.is_openai_model(model):
        state.provider, state.prices = "openai", OPENAI_PRICE_PER_M
    ACTIVE[call_id] = {"state": state, "contact": contact or {}, "channel": channel, "gate": start_gate,
                       "meta": meta or {}, "dialed_at": time.monotonic(), "voice": voice}
    client = make_client()
    fast_modes = [True, False] if GEMINI_THINKING_BUDGET >= 0 else [False]
    await send_event({"type": "status", "text": f"connecting to {model}", "call_id": call_id})

    OPENING = "[The customer has answered the call. Greet them now, then wait for their reply.]"

    async def session_once(prompt: str, opening: str, first: bool) -> None:
        async with contextlib.AsyncExitStack() as stack:
            global _hints_ok
            session = None
            # Optional settings a model may refuse. Try the full set first, then drop them one by one.
            attempts = [(fast, hints) for fast in fast_modes
                        for hints in ([True, False] if TRANSCRIPT_LANGUAGES and _hints_ok else [False])]
            if openai_bridge.is_openai_model(model):
                # OpenAI Realtime wearing the same session interface (app/openai_bridge.py), so
                # everything below this line is shared with Gemini.
                session = await stack.enter_async_context(openai_bridge.connect(
                    model=model, prompt=prompt, voice=voice, label=f"call {call_id}", manual_vad=manual_vad))
                attempts = []
            for n, (fast, hints) in enumerate(attempts):
                try:
                    session = await stack.enter_async_context(client.aio.live.connect(
                        model=model, config=build_config(prompt, voice, sensitive_listening, fast, manual_vad, hints)))
                    log.info("call %s: connected to %s via %s (fast replies %s, transcript language hint %s)",
                             call_id, model, "Vertex AI" if GEMINI_VERTEX else "Gemini API",
                             "on" if fast else "off", "on" if hints else "off")
                    break
                except Exception as exc:  # noqa: BLE001
                    if n == len(attempts) - 1 or is_billing_error(exc):
                        raise
                    if hints:
                        _hints_ok = False   # do not waste a connection attempt on every later call
                        log.warning("call %s: transcript language hint was refused (%s); retrying without it", call_id, exc)
                    else:
                        log.warning("call %s: fast mode was refused (%s); retrying with model defaults", call_id, exc)
            await send_event({"type": "status", "text": "connected"})

            if start_gate is not None and first:
                await start_gate.wait()
                state.started = time.monotonic()   # measure the call from pick-up, not from dialing

            # Ask the agent to open the call. Phone calls start with the agent speaking.
            # A completed turn makes the agent greet at once. (A realtime text message would
            # wait for the microphone to go quiet first, which never happens in a noisy room.)
            if model.startswith("gemini-2.") or openai_bridge.is_openai_model(model):
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=opening)]),
                    turn_complete=True,
                )
            else:
                # Newer live models close the session with "1007 invalid argument" on a
                # client_content turn. They take text on the realtime channel instead; with our own
                # speech detection the text has to sit inside a start/end pair to count as a turn.
                if manual_vad:
                    await session.send_realtime_input(activity_start=types.ActivityStart())
                await session.send_realtime_input(text=opening)
                if manual_vad:
                    await session.send_realtime_input(activity_end=types.ActivityEnd())
            log.info("call %s: opening sent, voice=%s model=%s", call_id, voice, model)

            async def pump_in() -> None:
                in_activity = False   # manual_vad: between ACTIVITY_START and ACTIVITY_END
                while True:
                    chunk = await incoming_audio.get()
                    if chunk is None or state.ended:
                        return
                    if chunk is ACTIVITY_START:
                        in_activity = True
                        log.info("call %s: customer started talking at %.1fs", call_id, time.monotonic() - state.started)
                        await session.send_realtime_input(activity_start=types.ActivityStart())
                        continue
                    if chunk is ACTIVITY_END:
                        if in_activity:   # a marker left over from before a reconnect has no start
                            in_activity = False
                            log.info("call %s: customer stopped talking at %.1fs", call_id, time.monotonic() - state.started)
                            await session.send_realtime_input(activity_end=types.ActivityEnd())
                        continue
                    if isinstance(chunk, Nudge):
                        # A note from the phone line ("noisy for 8 s, nothing understood"). With
                        # our own speech detection a text turn has to sit inside start/end marks.
                        if in_activity:
                            continue   # the customer is mid-sentence; that audio matters more
                        log.info("call %s: nudging the agent: %s", call_id, chunk)
                        if manual_vad:
                            await session.send_realtime_input(activity_start=types.ActivityStart())
                        await session.send_realtime_input(text=str(chunk))
                        if manual_vad:
                            await session.send_realtime_input(activity_end=types.ActivityEnd())
                        continue
                    if manual_vad and isinstance(chunk, bytes):
                        if in_activity:
                            await session.send_realtime_input(
                                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
                        continue
                    if isinstance(chunk, str):
                        # Typed customer reply from the test page (no microphone needed).
                        state.transcript.append({"role": "customer", "text": chunk})
                        # Text goes on the realtime channel; mixing client_content turns with a
                        # live audio stream makes native-audio models reject the session.
                        await session.send_realtime_input(audio_stream_end=True)
                        await session.send_realtime_input(text=chunk)
                        log.info("call %s: typed reply sent", call_id)
                        continue
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
                    # Reply watchdog. Gemini answers only after it hears the customer pause. On a
                    # noisy line that pause may never be detected, and the agent sits silent while
                    # the customer keeps repeating themselves. If the customer has clearly spoken
                    # and nothing has come back for a few seconds, mark a pause ourselves.
                    if state.idle_since is not None and not state.speaking:
                        now = time.monotonic()
                        if state.loud_since is None:
                            if audioop.rms(chunk, 2) > 700:
                                state.loud_since = now
                        elif now - state.loud_since > REPLY_WATCHDOG_SECONDS:
                            state.nudges += 1
                            state.loud_since = None
                            log.info("call %s: customer spoke but no reply for %.0fs; marking a pause (nudge %s)",
                                     call_id, REPLY_WATCHDOG_SECONDS, state.nudges)
                            await session.send_realtime_input(audio_stream_end=True)

            async def pump_out() -> None:
                # session.receive() yields ONE model turn and then stops,
                # so keep calling it until the call is over.
                while not state.ended:
                    async for msg in session.receive():
                        if state.ended:
                            return
                        if msg.usage_metadata is not None:
                            state.note_usage(msg.usage_metadata)
                        sc = msg.server_content
                        if sc is not None:
                            if sc.interrupted:
                                state.speaking = False
                                state.idle_since = time.monotonic()
                                state.loud_since = None
                                state.speech_until = 0.0   # that audio is dropped, not played
                                state.close_usage_turn()   # a cut-off turn is still billed
                                log.info("call %s: customer interrupted", call_id)
                                await send_event({"type": "interrupted"})
                            if sc.input_transcription is not None and sc.input_transcription.text:
                                if not state._in_buf:
                                    log.info("call %s: customer speech detected", call_id)
                                state.add_input_text(sc.input_transcription.text)
                                await send_event({"type": "transcript", "role": "customer",
                                                  "text": sc.input_transcription.text, "partial": True})
                            if sc.output_transcription is not None and sc.output_transcription.text:
                                state.add_output_text(sc.output_transcription.text)
                                await send_event({"type": "transcript", "role": "agent",
                                                  "text": sc.output_transcription.text, "partial": True})
                            if sc.model_turn is not None:
                                for part in sc.model_turn.parts or []:
                                    if part.inline_data is not None and part.inline_data.data:
                                        if not state.speaking:
                                            state.speaking = True
                                            state.idle_since = None
                                            state.loud_since = None
                                            log.info("call %s: agent started speaking at %.1fs", call_id, time.monotonic() - state.started)
                                        # How long this audio takes to speak. OpenAI hands over a
                                        # whole sentence in a fraction of the time it takes to say
                                        # it, so "the model has stopped" is not "the customer has
                                        # heard it" and the goodbye was being cut off.
                                        state.speech_until = max(state.speech_until, time.monotonic()) + \
                                            len(part.inline_data.data) / (OUTPUT_SAMPLE_RATE * 2)
                                        await send_audio(part.inline_data.data)
                            if sc.turn_complete:
                                state.speaking = False
                                state.idle_since = time.monotonic()
                                state.loud_since = None
                                log.info("call %s: agent turn complete, now waiting for the customer", call_id)
                                # Safety net: the customer's turn transcribed to nothing ("." / "<noise>" /
                                # a foreign script). Count it, and escalate: ask again, then ask them to
                                # speak closer, then stop asking. The agent is told, not left to guess.
                                if state._in_buf.strip():
                                    if heard_nothing(state._in_buf):
                                        state.inaudible_streak += 1
                                        state.inaudible_total += 1
                                    else:
                                        state.inaudible_streak = 0
                                streak = state.inaudible_streak
                                if streak and not state.ended and (streak >= 2 or not asked_to_repeat(state._out_buf)):
                                    log.info("call %s: customer turn inaudible (%r), %s in a row; nudging the agent",
                                             call_id, state._in_buf.strip()[:40], streak)
                                    incoming_audio.put_nowait(Nudge(INAUDIBLE_NOTES[min(streak, 3)]))
                                long_answer = expects_long_answer(state._out_buf)
                                state.flush_turn()
                                await send_event({"type": "turn_complete", "long_answer": long_answer})
                        if msg.tool_call is not None:
                            responses = []
                            for fc in msg.tool_call.function_calls or []:
                                result = await handle_tool(fc.name, dict(fc.args or {}), state, send_event)
                                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                            await session.send_tool_response(function_responses=responses)
                            if state.ended:
                                # Wait for the goodbye to actually finish in the customer's ear,
                                # not merely for the model to stop producing it. Capped so a
                                # runaway reply cannot hold the line open.
                                left = min(20.0, max(0.0, state.speech_until - time.monotonic()) + 0.4)
                                log.info("call %s: waiting %.1fs for the goodbye to finish", call_id, left)
                                await asyncio.sleep(left)
                                return

            in_task = asyncio.create_task(pump_in())
            out_task = asyncio.create_task(pump_out())
            done, pending = await asyncio.wait({in_task, out_task}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc

    def drain_stale_audio() -> bool:
        """Drop audio that queued up while we were reconnecting. Returns False if the line has closed."""
        alive = True
        while not incoming_audio.empty():
            if incoming_audio.get_nowait() is None:
                alive = False
        if not alive:
            incoming_audio.put_nowait(None)
        return alive

    try:
        prompt, opening = system_prompt, OPENING
        while True:
            try:
                await session_once(prompt, opening, state.reconnects == 0)
                break
            except Exception as exc:  # noqa: BLE001
                # The connection to Gemini dropped (network blip, Gemini "1011 internal error"). The
                # customer is still on the line, so reconnect and carry on instead of going silent
                # and hanging up. Gemini has failed twice in one call, so allow a few tries.
                alive = drain_stale_audio()
                switching = alive and not state.ended and can_fall_back(model, exc)
                if switching:
                    # Gemini cannot take this call at all. Rather than hang up on a customer who
                    # is still holding the phone, carry on with the other provider.
                    _provider_down["gemini"] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS
                    log.warning("call %s: Gemini cannot take this call (%s); switching to %s",
                                call_id, exc, OPENAI_REALTIME_MODEL)
                    model = OPENAI_REALTIME_MODEL
                    state.provider, state.prices = "openai", OPENAI_PRICE_PER_M
                elif state.ended or state.reconnects >= MAX_RECONNECTS or is_billing_error(exc) or not alive:
                    raise
                else:
                    state.reconnects += 1
                    log.warning("call %s: connection to Gemini dropped (%s: %s); reconnecting (%s of %s)",
                                call_id, type(exc).__name__, exc, state.reconnects, MAX_RECONNECTS)
                    if "1007" in str(exc) and model != GEMINI_LIVE_MODEL:
                        # "Invalid argument" is not a blip: this model refuses how we talk to it, and
                        # would refuse again. Carry the call on the default model instead of dropping it.
                        log.warning("call %s: %s refused the session; switching to %s", call_id, model, GEMINI_LIVE_MODEL)
                        _refused_models.add(model)   # later calls skip it at once instead of greeting 3 s late
                        model = GEMINI_LIVE_MODEL
                state.flush_turn()
                if not state.transcript:
                    continue   # nothing was said yet: greet normally, there is no break to apologise for
                so_far = "\n".join(f"{'You' if t['role'] == 'agent' else 'Customer'}: {t['text']}" for t in state.transcript) or "(nothing yet)"
                prompt = (system_prompt + "\n\nTHE LINE DROPPED FOR A MOMENT AND IS BACK. Conversation so far:\n" + so_far +
                          "\nDo not greet or introduce yourself again. Say one short apology for the break "
                          "(for example: 'sorry ji, awaaz cut gayi thi') and continue from where you stopped. "
                          "If the conversation above was already finished, just say a short goodbye, then call mark_outcome and end_call.")
                opening = "[The line is back. Apologise briefly for the break and continue.]"
    except Exception as exc:  # noqa: BLE001
        if is_billing_error(exc):
            log.error("call %s failed: Gemini refused for billing reasons: %s", call_id, exc)
            state.error = f"Gemini billing: {exc}"
        else:
            log.exception("call %s failed", call_id)
            state.error = f"{type(exc).__name__}: {exc}"
        await send_event({"type": "error", "text": state.error})
    finally:
        ACTIVE.pop(call_id, None)
        state.flush_turn()
        duration = time.monotonic() - state.started
        log.info("call %s ended: %.0fs, outcome=%s, agent=%s (%s), watchdog nudges=%s, reconnects=%s",
                 call_id, duration, state.outcome, state.provider, model, state.nudges, state.reconnects)
        if state.usage:
            # Kept with the call so the two agents can be compared later from the database
            # alone, without digging through the log.
            state.usage["provider"], state.usage["model"] = state.provider, model
            u = state.usage
            log.info("call %s usage: %s turns, in: %s audio + %s text tokens, out: %s audio + %s text tokens, about $%.4f",
                     call_id, u.get("turns"), u.get("audio_in"), u.get("text_in"), u.get("audio_out"), u.get("text_out"),
                     u.get("cost_usd", 0))
        cause = str((meta or {}).get("hangup_cause") or "")
        result = ""
        if start_gate is not None and not start_gate.is_set():
            duration = 0.0
            if cause == "NO_CALLBACK" and not state.error:
                state.error = "Plivo never called back: the tunnel is probably down or PUBLIC_BASE_URL is stale"
            if state.error:
                result = f"not called: {state.error}"     # our failure; the customer was never rung
            else:
                state.outcome, result = classify_unanswered(cause)
        elif not state.outcome:
            # Someone answered but the call ended with nothing recorded: the customer hung up or the
            # line dropped -> call back later. If the agent itself failed mid-way, a person looks first.
            state.outcome = "unclear" if state.error else "dropped"
            notes = (state.outcome_details or {}).get("notes") or (
                f"call failed mid-way: {state.error}" if state.error else "call ended without a result (customer hung up or line dropped)")
            state.outcome_details = {**(state.outcome_details or {}), "notes": notes}
            result = notes
        if not result:
            result = (state.outcome or "").replace("_", " ")
            for key in ("notes", "cancel_reason", "callback_time", "mismatch", "new_address"):
                if (state.outcome_details or {}).get(key):
                    result += f": {state.outcome_details[key]}"
                    break
        db.finish_call(
            call_id,
            outcome=state.outcome,
            outcome_details=state.outcome_details,
            transcript=state.transcript,
            usage=state.usage,
            error=state.error,
            duration_sec=round(duration, 1),
            hangup_cause=cause or None,
            result=result[:200],
        )
        if contact and state.outcome in db.RETRY_STATUSES:
            # Into the call-back queue with a time. The campaign may overwrite it with its own
            # retry gap; a single call from the dashboard keeps this default.
            when = None
            if state.outcome == "call_later":
                when = parse_callback_time((state.outcome_details or {}).get("callback_time") or "")
            db.set_next_try(contact["id"], when or datetime.now(timezone.utc) + timedelta(minutes=DEFAULT_RETRY_MINUTES))
        await send_event({"type": "ended", "call_id": call_id, "outcome": state.outcome,
                          "duration_sec": round(duration, 1)})
    return state


DEFAULT_RETRY_MINUTES = 30


def parse_callback_time(text: str, now: datetime | None = None) -> datetime | None:
    """A time from what the customer said ("kal subah", "shaam ko 6 baje", "after 2 hours",
    "tomorrow evening"). None when nothing usable is in it."""
    t = (text or "").lower().strip()
    if not t:
        return None
    now = now or datetime.now(IST)
    now = now.astimezone(IST)
    day = now.date()
    hour: int | None = None
    if re.search(r"\bparso\b|day after", t):
        day += timedelta(days=2)
    elif re.search(r"\bkal\b|\bkl\b|tomorrow|अगले दिन|कल", t):
        day += timedelta(days=1)
    m = re.search(r"(\d+)\s*(?:ghante|ghanta|hour|hrs?|घंटे|घंटा)", t)
    if m and re.search(r"baad|after|later|में|mein|me\b", t):
        return now + timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*(?:minute|min|मिनट)", t)
    if m and re.search(r"baad|after|later|में|mein|me\b", t):
        return now + timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(?:baje|बजे|o'?clock|am|pm|a\.m|p\.m)?", t)
    if m and (re.search(r"baje|बजे|o'?clock|am|pm|a\.m|p\.m|:\d{2}", t) or re.search(r"\b" + m.group(1) + r"\b", t)):
        h = int(m.group(1))
        if h <= 24 and (re.search(r"baje|बजे|o'?clock|am|pm|a\.m|p\.m|:\d{2}", t)):
            if re.search(r"pm|p\.m|shaam|शाम|evening|raat|रात|night|dopahar|दोपहर|afternoon", t) and h < 12:
                h += 12
            hour = h % 24
    if hour is None:
        if re.search(r"subah|सुबह|morning", t):
            hour = 10
        elif re.search(r"dopahar|दोपहर|afternoon|lunch", t):
            hour = 13
        elif re.search(r"shaam|शाम|evening", t):
            hour = 18
        elif re.search(r"raat|रात|night", t):
            hour = 20
    if hour is None and day == now.date():
        return None
    if hour is None:
        hour = 11
    when = datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)
    if when <= now:
        when += timedelta(days=1)
    return when


def classify_unanswered(cause: str) -> tuple[str, str]:
    """Outcome and a one-line note for a call nobody picked up, from Plivo's hangup cause."""
    c = cause.upper()
    if c in ("USER_BUSY", "CALL_REJECTED"):
        return "busy", "busy" if c == "USER_BUSY" else "call rejected"
    if c in ("UNALLOCATED_NUMBER", "INVALID_NUMBER_FORMAT", "NO_ROUTE_DESTINATION", "NUMBER_CHANGED"):
        return "wrong_number", "number does not exist"
    if c in ("SUBSCRIBER_ABSENT", "NORMAL_UNSPECIFIED", "NORMAL_TEMPORARY_FAILURE"):
        return "no_answer", "switched off or not reachable"
    return "no_answer", "no answer"


async def handle_tool(name: str, args: dict[str, Any], state: CallState, send_event: SendEvent) -> dict[str, Any]:
    log.info("tool %s %s", name, json.dumps(args, ensure_ascii=False))
    if name == "record_address":
        # The address must come from the customer, not be assembled by the model out of fragments.
        recent = state.recent_customer_turns(4)
        heard = [t for t in recent if not heard_nothing(t)]
        if not heard or sum(len(t.split()) for t in heard) < 3:
            log.info("call %s: record_address refused; nothing intelligible from the customer in the last turns", state.call_id)
            return {"status": "not recorded", "reason": "You have not heard an address from the customer: their last replies were "
                    "inaudible. Do not read any address back. Ask them to hold the phone closer and say the address again, "
                    "slowly, starting with the house number. If it still cannot be heard, say 'koi baat nahi, team aapse "
                    "address le legi' and carry on."}
        asked = asks_for_address_part(state._out_buf)
        if asked:
            # The model asked for a part and, in the same breath, filled it in itself (call 54).
            log.info("call %s: record_address refused; the agent asked %r in this same turn and has no answer yet", state.call_id, asked)
            return {"status": "not recorded", "reason": "You just asked the customer a question in this same turn and have not heard "
                    "their answer. Do not read any address back and do not fill in the answer yourself. Stop talking and wait "
                    "for their reply; call record_address only after they have said every part."}
        pin = spoken_digits(str(args.get("pin") or ""))
        if len(pin) != 6:
            return {"status": "not recorded", "reason": f"The PIN code must be exactly six digits; you gave '{args.get('pin')}'. "
                    "Ask for the PIN code again, digit by digit, then call record_address again with all four parts."}
        parts = {k: str(args.get(k) or "").strip() for k in ("house", "area", "city", "pin", "landmark")}
        parts["pin"] = pin
        if not all(parts[k] and not _PLACEHOLDER.match(parts[k]) for k in ("house", "area", "city")):
            # "Unknown" as the city (call 58) is a gap the model papered over, not something the customer said.
            return {"status": "not recorded", "reason": "House number, area and city are all needed, as the customer said them; never write "
                    "'unknown' or 'not given'. Ask for the missing part, then call record_address again."}
        # The numbers must have come out of the customer's mouth on this call, not out of the model.
        # Only a transcript we actually have can say that. OpenAI writes the customer's words down
        # in a separate pass that often lands after the reply, so an empty result here means "not
        # transcribed yet", not "never said" (call 60 refused a correct PIN three times on that).
        said = spoken_digits(" ".join(state.recent_customer_turns(10)))
        wrong = []
        if said:
            if pin not in said:
                wrong.append(f"PIN code {pin}")
            # House numbers of two digits or more; a lone digit is a floor or a wing, not a number
            # the customer had to say ("Tower No 10, 502, 5th Floor" on call 58).
            wrong += [f"house number {n}" for n in
                      (n for n in re.findall(r"\d+", spoken_digits_spaced(parts["house"])) if len(n) >= 2)
                      if n not in said]
        if wrong:
            state.address_refusals += 1
            log.info("call %s: record_address refused (%s of 3); %s never said (customer digits heard: %r)",
                     state.call_id, state.address_refusals, " and ".join(wrong), said)
            if state.address_refusals >= 3:
                # Three refusals in a row is a loop the customer is sitting through. Stop asking.
                return {"status": "not recorded", "reason": "The address still does not match what the customer said, and they have "
                        "been asked enough times. Do not ask again and do not read any address back. Say 'koi baat nahi ji, team "
                        "aapse address le legi', put that in mark_outcome notes, and carry on to the close."}
            return {"status": "not recorded", "reason": f"The customer has NOT said {' and '.join(wrong)} on this call. The digits they "
                    f"were actually heard to say, in order, are '{said}'. Never fill a number in yourself. Ask for the part you are "
                    "missing, wait for their answer, then call record_address again."}
        if not said:
            log.info("call %s: record_address taken unchecked; nothing of the customer's has been transcribed yet", state.call_id)
        state.address = parts
        state.address_refusals = 0   # a good one clears the slate; a later correction gets its own tries
        log.info("call %s: address recorded %s", state.call_id, json.dumps(parts, ensure_ascii=False))
        return {"status": "recorded", "next": f"Now read it back ONCE and get a yes: 'toh address hai: house number {parts['house']}, "
                f"{parts['area']}, {parts['city']}, PIN {' '.join(pin)}. Sahi hai?' If they correct a part, call record_address again with the corrected part."}
    if name == "mark_outcome":
        outcome = args.get("outcome", "unclear")
        if state.address and not args.get("new_address"):
            a = state.address
            args["new_address"] = ", ".join(x for x in (f"house number {a['house']}", a["area"], a.get("landmark", ""), a["city"], f"PIN {a['pin']}") if x)
        address = str(args.get("new_address") or "")
        if address and not re.search(r"(?<!\d)\d{3}\s?\d{3}(?!\d)", address) and not state.address_checked:
            # Refused once only, so a customer who truly cannot give a PIN code does not trap the call.
            state.address_checked = True
            log.info("call %s: new address has no 6-digit PIN code; asking the agent to complete it", state.call_id)
            return {"status": "not recorded", "reason": "new_address has no valid 6-digit PIN code. Do not end the call yet. "
                    "Ask the customer for the PIN code digit by digit, read the full address back, then call mark_outcome again. "
                    "If they cannot give it, call mark_outcome again and say in notes that the team must take the address."}
        if outcome in ("confirmed", "changed") and not any(said_yes(t) for t in state.recent_customer_turns(2)):
            # No clear yes on record: the model is confirming on silence or noise (call 53 did exactly this).
            state.confirm_refusals += 1
            if state.confirm_refusals <= 2:
                log.info("call %s: mark_outcome %s refused; no clear yes from the customer (last: %r)",
                         state.call_id, outcome, state.recent_customer_turns(1))
                return {"status": "not recorded", "reason": "The customer has NOT clearly said yes: their last replies were inaudible "
                        "or not an agreement. Do not confirm. Ask once more, in a few words, 'sab sahi hai? confirm kar doon?' and "
                        "wait for a clear haan. If you still cannot hear them, say 'awaaz nahi aa rahi, team aapse baat kar legi', call "
                        "mark_outcome with outcome 'unclear' and notes 'could not hear the customer's confirmation', and end_call."}
            outcome = "unclear"
            args = {**args, "outcome": outcome, "notes": ((args.get("notes") or "") + " (confirmation was not clearly heard)").strip()}
        if state.inaudible_total >= 2:
            args = {**args, "notes": ((args.get("notes") or "") + f" (customer was inaudible {state.inaudible_total} times on this call)").strip()}
        state.outcome = outcome
        state.outcome_details = args
        await send_event({"type": "outcome", "outcome": state.outcome, "details": args})
        return {"status": "recorded"}
    if name == "end_call":
        # A call that the agent ends within seconds of pick-up, before the customer has said
        # anything we could write down, is a mistake (it once hung up on a customer's "hello").
        heard = any(t["role"] == "customer" for t in state.transcript) or state._in_buf.strip()
        if time.monotonic() - state.started < 12 and not heard:
            log.warning("call %s: agent tried to hang up %.1fs after pick-up; refused", state.call_id, time.monotonic() - state.started)
            state.outcome = state.outcome_details = None
            return {"status": "refused", "reason": "The customer has only just picked up and is on the line. "
                    "Do not hang up. Greet them and run the call."}
        state.ended = True
        return {"status": "ending"}
    return {"error": f"unknown tool {name}"}


def live_snapshot() -> list[dict]:
    """What the dashboard shows for calls that are ringing or in progress."""
    now = time.monotonic()
    out = []
    for call_id, a in list(ACTIVE.items()):
        state: CallState = a["state"]
        gate = a["gate"]
        if gate is not None and not gate.is_set():
            status, seconds = "ringing", now - a["dialed_at"]
        elif state.ended:
            status, seconds = "ending", now - state.started
        elif state.speaking:
            status, seconds = "agent_speaking", now - state.started
        else:
            status, seconds = "listening", now - state.started
        lines = list(state.transcript[-3:])
        if state._in_buf.strip():
            lines.append({"role": "customer", "text": state._in_buf.strip()})
        if clean_agent_text(state._out_buf):
            lines.append({"role": "agent", "text": clean_agent_text(state._out_buf)})
        c = a["contact"]
        out.append({
            "call_id": call_id, "channel": a["channel"], "status": status, "seconds": round(seconds),
            "customer_name": c.get("customer_name", ""), "order_id": c.get("order_id", ""),
            "product": c.get("product", ""), "to": a["meta"].get("to") or c.get("phone", ""),
            "voice": a["voice"], "outcome": state.outcome, "lines": lines[-3:],
        })
    return out
