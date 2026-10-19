"""Sarvam as a third voice agent, shaped like the Gemini Live session (as app/openai_bridge.py is).

Sarvam has no single voice model, so each customer turn goes through three of its services:
  Saaras (realtime speech-to-text, WebSocket)   -> the customer's words
  Sarvam-105B (chat completions, streamed)      -> the reply, and any tool calls
  Bulbul (text-to-speech, WebSocket)            -> the reply's sound, 24 kHz 16-bit PCM like Gemini's

Mapping, Gemini -> here:
  ACTIVITY_START   -> a barge-in when a reply is being made; speech_start goes to Saaras with the turn's first audio
  audio chunk      -> audio_input to Saaras
  ACTIVITY_END     -> speech_end; once its transcript comes, the chat call, each sentence spoken as it is written
  text             -> a note added to the customer's turn, or a turn of its own (typed reply, opening)
  FunctionResponse -> tool messages, then the chat call again

Bulbul cannot be stopped mid-reply, so a barge-in closes its socket and opens a fresh one (Sarvam's own advice).
A reply's tool calls are handed over only once its sound has all arrived, so a goodbye before end_call is whole.
"""
import asyncio
import base64
import contextlib
import json
import logging
import random
import re
import time
import urllib.parse

import httpx
from websockets.asyncio.client import connect as ws_connect

from .config import (OUTPUT_SAMPLE_RATE, SARVAM_API_KEY, SARVAM_LANGUAGE, SARVAM_MODEL, SARVAM_PACE, SARVAM_STT_MODE,
                     SARVAM_STT_MODEL, SARVAM_TTS_MODEL, SARVAM_VOICE_FEMALE, SARVAM_VOICE_MALE, SARVAM_VOICES, VOICES)
from .openai_bridge import (OPENING_ONLY, _FunctionCall, _Message, _ModelTurn, _Part, _Blob,
                            _ServerContent, _Text, _TokenDetail, _ToolCall, _Usage, openai_tools)
from .prompts import about_hint, openai_accent
from .tools import TOOLS

log = logging.getLogger("sarvam")

CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"
STT_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
TTS_URL = "wss://api.sarvam.ai/text-to-speech/ws"
MAX_TOKENS = 300            # a reply is one or two sentences; this only stops a runaway
STT_FINAL_WAIT = 4.0        # seconds to wait for the customer's words after speech_end
TTS_QUIET_SECONDS = 3.0     # no sound and no "final" for this long after the last text: the reply is taken as spoken
KEEPALIVE_SECONDS = 20      # both sockets close after about a minute with nothing on them

# Lookups that wait on our server (and then on a second chat call) before the agent can answer. Owner, 12 Oct
# 2026: while one runs the line must not go quiet, so a short "let me check" is spoken the moment the reply turns
# to the lookup with nothing said yet, and "one moment more" if the answer is still not there after a while.
LOOKUP_TOOLS = {"product_info"}
STILL_CHECKING_SECONDS = 3.5
_HELPER = {
    "female": ["जी, एक सेकंड, मैं अभी चेक करके बताती हूँ।", "जी, बस एक पल, मैं देख कर बताती हूँ।",
               "अच्छा जी, एक सेकंड, मैं चेक कर लेती हूँ।"],
    "male": ["जी, एक सेकंड, मैं अभी चेक करके बताता हूँ।", "जी, बस एक पल, मैं देख कर बताता हूँ।",
             "अच्छा जी, एक सेकंड, मैं चेक कर लेता हूँ।"],
}
_STILL = {"female": ["बस एक पल और जी।", "जी, बस देख रही हूँ।"], "male": ["बस एक पल और जी।", "जी, बस देख रहा हूँ।"]}
# A sentence ends at a danda, "?" or "!", or at a full stop with a space after it ("2." may be "2.5" in the making).
_SENTENCE_END = re.compile(r"[।?!]|\.(?=\s)")
_MARKUP = re.compile(r"[*#`_~>\[\]]|[\U0001F300-\U0001FAFF☀-➿]")


def is_sarvam_model(model: str) -> bool:
    return model.startswith("sarvam")


def voice_for(gemini_voice: str) -> str:
    """The Bulbul speaker chosen on the dashboard (Priya or Varun); for a Gemini voice, the one of its gender."""
    if gemini_voice in SARVAM_VOICES:
        return gemini_voice.lower()
    return SARVAM_VOICE_MALE if VOICES.get(gemini_voice, "female") == "male" else SARVAM_VOICE_FEMALE


def chat_tools(tools: list | None = None) -> list[dict]:
    """The call's tools in the chat-completions shape (OpenAI's realtime shape, one level deeper)."""
    return [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}}
            for t in openai_tools(tools if tools is not None else TOOLS)]


def _pcm(data: bytes) -> bytes:
    """Bulbul's linear16 chunk as bare PCM: a chunk that comes as a WAV file loses its header."""
    if data[:4] == b"RIFF":
        at = data.find(b"data")
        return data[at + 8:] if at >= 0 else b""
    return data


class SarvamSession:
    """One call's conversation, presenting the handful of methods live_bridge calls."""

    def __init__(self, *, prompt: str, speaker: str, label: str, manual_vad: bool, tools: list[dict],
                 greeting: str = "") -> None:
        self.prompt = prompt
        self.speaker = speaker
        self.label = label
        self.manual_vad = manual_vad    # the phone line marks the turns; on the browser test Saaras finds them
        self.tools = tools
        self.greeting = greeting
        self.next_hint = ""             # extra instructions for the reply to the next customer turn only (live_bridge)
        self.language = SARVAM_LANGUAGE   # what Saaras listens for and Bulbul speaks; the customer may ask for another
        self._stt_switch: asyncio.Task | None = None
        self.history: list[dict] = []   # the conversation, without the system prompt
        self._q: asyncio.Queue = asyncio.Queue()
        self._closed: BaseException | None = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._headers = {"api-subscription-key": SARVAM_API_KEY}
        # Saaras
        self._stt = None
        self._stt_reader: asyncio.Task | None = None
        self._stt_lock = asyncio.Lock()
        self._in_turn = False           # between ACTIVITY_START and ACTIVITY_END
        self._stt_started = False       # speech_start sent for this turn (at its first audio)
        self._utterances = 0            # speech_start sent this session
        self._skip_below = 0            # transcripts of utterances before this one came too late; dropped
        self._turn_ms = 0.0             # audio sent this turn
        self._heard: list[str] = []     # transcripts not yet used in a turn
        self._owed = 0                  # speech_end sent, transcript not come yet
        self._final = asyncio.Event()
        self._notes: list[str] = []     # notes from the phone line inside this turn
        self._carry = ""                # words of a turn whose reply was cut before it made a sound
        self._ending: asyncio.Task | None = None   # the turn's end, waiting for its transcript
        self._ended_at = 0.0            # when the customer's turn ended (for the timing line)
        # Bulbul
        self._tts = None
        self._tts_reader: asyncio.Task | None = None
        self._tts_lock = asyncio.Lock()
        self._tts_opening: asyncio.Task | None = None
        self._flushes = 0               # flushes sent whose "final" has not come
        self._last_tts = 0.0            # last time Bulbul sent anything, or we sent it text
        self._odd = b""                 # a half sample left over from the last chunk
        self._warned_codec = False
        # the reply
        self._reply: asyncio.Task | None = None
        self._responding = False        # a reply is being written or spoken right now
        self._said = ""                 # this reply's words sent to Bulbul
        self._got_audio = False         # this reply has produced sound
        self._tools_spoke = False       # the reply that called the tools waiting for answers had words in it
        self._gender = SARVAM_VOICES.get(speaker.capitalize(), "female")
        self._helped = False            # this reply already said "let me check"
        self._after_lookup = False      # the next reply answers a lookup: say "one moment more" if it is slow
        self._ready: tuple[list, list] = ([], [])   # tool calls of a reply fully written, not handed over yet
        self._warned_usage = False
        self._acc: dict[str, float] = {}   # this agent turn's usage so far
        self._keepalive = asyncio.create_task(self._keep_alive())

    # ---- opening the sockets ----

    async def open(self) -> None:
        await self._open_stt()
        await self._open_tts()

    async def _open_stt(self) -> None:
        params = {"language_code": self.language, "model": SARVAM_STT_MODEL, "mode": SARVAM_STT_MODE,
                  "encoding": "linear16", "sample_rate": "16000",
                  "endpointing": "manual" if self.manual_vad else "vad"}
        ws = await ws_connect(STT_URL + "?" + urllib.parse.urlencode(params), additional_headers=self._headers,
                              max_size=None, open_timeout=15, ping_interval=20)
        while True:   # session.begin, or an error saying why not
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if evt.get("event") == "session.begin":
                break
            if evt.get("event") == "error":
                raise RuntimeError(f"Sarvam speech-to-text: {evt.get('code')}: {evt.get('message')}")
        self._stt = ws
        self._stt_reader = asyncio.create_task(self._read_stt(ws))

    def _tts_config(self) -> str:
        return json.dumps({"type": "config", "data": {
            "speaker": self.speaker, "language_code": self.language, "pace": SARVAM_PACE,
            "speech_sample_rate": OUTPUT_SAMPLE_RATE, "output_audio_codec": "linear16"}})

    async def _open_tts(self) -> None:
        params = {"model": SARVAM_TTS_MODEL, "send_completion_event": "true"}
        ws = await ws_connect(TTS_URL + "?" + urllib.parse.urlencode(params), additional_headers=self._headers,
                              max_size=None, open_timeout=15, ping_interval=20)
        await ws.send(self._tts_config())
        self._tts, self._flushes, self._odd = ws, 0, b""
        self._tts_reader = asyncio.create_task(self._read_tts(ws))

    def _language_note(self) -> str:
        from .live_bridge import language_note   # here: live_bridge imports this module
        return language_note(self.language)

    async def _switch_language(self, code: str) -> None:
        """The customer asked for another language (owner, 19 Oct 2026): Bulbul speaks it from the next reply on (a new
        config is taken at any point), and Saaras listens for it on a fresh socket, opened while the reply is made."""
        log.info("%s: the customer asked for %s; voice and speech-to-text switch to it", self.label, code)
        self.language = code
        ws = await self._ready_tts()
        async with self._tts_lock:
            await ws.send(self._tts_config())
        self._stt_switch = asyncio.create_task(self._reopen_stt())

    async def _reopen_stt(self) -> None:
        async with self._stt_lock:   # no audio goes out while the sockets change over
            old, reader = self._stt, self._stt_reader
            try:
                await self._open_stt()
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: speech-to-text could not switch language (%s); still listening as before", self.label, exc)
                self._stt, self._stt_reader = old, reader
                return
            self._utterances = self._skip_below = 0   # the new socket counts its utterances from 0
            self._stt_started = False
        if reader is not None:
            reader.cancel()   # first, or its socket closing would end the session
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()

    async def _fresh_tts(self) -> None:
        """Drop Bulbul's socket, with whatever it was still saying, and open a new one in the background."""
        old, reader, self._tts = self._tts, self._tts_reader, None
        if reader is not None:
            reader.cancel()
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()
        self._tts_opening = asyncio.create_task(self._open_tts())

    async def _ready_tts(self):
        if self._tts_opening is not None:
            task, self._tts_opening = self._tts_opening, None
            await task
        if self._tts is None:
            await self._open_tts()
        return self._tts

    async def _keep_alive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            with contextlib.suppress(Exception):
                if self._stt is not None:
                    async with self._stt_lock:
                        await self._stt.send(json.dumps({"event": "ping"}))
                if self._tts is not None:
                    async with self._tts_lock:
                        await self._tts.send(json.dumps({"type": "ping"}))

    # ---- what live_bridge sends ----

    async def send_realtime_input(self, *, activity_start=None, activity_end=None, text=None,
                                  audio=None, audio_stream_end=None) -> None:
        if activity_start is not None:
            # The customer started talking (or a note from the line begins). Over a reply being made, a barge-in.
            if self._responding:
                await self._interrupt()
            if self._ending is not None and not self._ending.done():
                # They carried on talking before the last turn's words came back: one turn, not two.
                self._ending.cancel()
            self._in_turn, self._stt_started, self._turn_ms = True, False, 0.0
            return
        if activity_end is not None:
            if self._in_turn:
                self._in_turn = False
                self._ended_at = time.monotonic()
                if self._stt_started:
                    async with self._stt_lock:
                        await self._stt.send(json.dumps({"event": "speech_end"}))
                    self._owed += 1
                self._ending = asyncio.create_task(self._end_turn(self._stt_started))
            return
        if audio_stream_end:
            return   # the browser's reply watchdog; Saaras finds the pauses itself there
        if text is not None:
            if self.manual_vad and self._in_turn:
                self._notes.append(str(text))   # a note from the phone line, inside a start/end pair
            else:
                # A typed reply on the test page (live_bridge has written it down already).
                if self._responding:
                    await self._interrupt()
                self._user(str(text))
                self._start_reply()
            return
        if audio is not None:
            pcm = getattr(audio, "data", audio)
            if not pcm or self._stt is None:
                return
            async with self._stt_lock:
                if self.manual_vad and not self._stt_started:
                    self._stt_started = True
                    self._utterances += 1
                    await self._stt.send(json.dumps({"event": "speech_start"}))
                await self._stt.send(json.dumps({"event": "audio_input", "audio": base64.b64encode(pcm).decode()}))
            ms = len(pcm) / 32.0   # 16 kHz 16-bit: 32 bytes per millisecond
            self._turn_ms += ms
            self._add("stt_seconds", ms / 1000)

    async def send_client_content(self, turns=None, turn_complete: bool = True) -> None:
        """How live_bridge opens the call (and reopens it after a drop)."""
        text = "".join(getattr(p, "text", "") or "" for p in getattr(turns, "parts", None) or [])
        self._user(text)
        opening = "first turn" in text
        if opening and self.greeting:
            # The call type gives the exact first line: spoken at once, no chat call in front of it.
            self._start_reply(fixed=self.greeting)
            return
        self._start_reply(extra=OPENING_ONLY if opening else "")

    async def send_tool_response(self, *, function_responses) -> None:
        ending = False
        for fr in function_responses or []:
            result = getattr(fr, "response", {}) or {}
            self.history.append({"role": "tool", "tool_call_id": getattr(fr, "id", "") or "",
                                 "content": json.dumps(result, ensure_ascii=False)})
            ending = ending or (getattr(fr, "name", "") == "end_call" and result.get("status") == "ending")
        if ending:
            return   # the goodbye has been spoken and the line is closing: no further reply
        self._after_lookup = any(getattr(fr, "name", "") in LOOKUP_TOOLS for fr in function_responses or [])
        # A hang-up sent back for want of a goodbye comes with the goodbye to say (live_bridge.closing_line). Owner, 12 Oct
        # 2026 (call 217: the model asked to hang up twice with nothing said): it is spoken as given, with no chat call,
        # and live_bridge hangs up once it has played. So is the coupon offer, when a "not interested" came back because
        # it was never made (owner, 19 Oct 2026). Only these two tools: product_info's "say" is an instruction.
        say = next((str((getattr(fr, "response", {}) or {}).get("say") or "") for fr in function_responses or []
                    if getattr(fr, "name", "") in ("end_call", "mark_outcome")
                    and (getattr(fr, "response", {}) or {}).get("say")), "")
        if say:
            log.info("%s: a tool came back with the line to say; saying it as given: %s", self.label, say)
            self._start_reply(fixed=say)
            return
        plain = all(set(getattr(fr, "response", {}) or {}) <= {"status"}
                    and (getattr(fr, "response", {}) or {}).get("status") == "recorded" for fr in function_responses or [])
        if plain and self._tools_spoke:
            # Simply written down by a reply that already said its piece (call 159 on ChatGPT: it said it twice).
            await self._turn_done()
            return
        self._start_reply()

    # ---- the customer's turn ----

    def _user(self, content: str) -> None:
        if self._carry:
            content, self._carry = self._carry + "\n" + content, ""
        self.history.append({"role": "user", "content": content})

    async def _end_turn(self, had_audio: bool) -> None:
        """The customer stopped: wait for their words from Saaras, then reply."""
        deadline = time.monotonic() + STT_FINAL_WAIT
        while self._owed > 0 and time.monotonic() < deadline:
            self._final.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._final.wait(), timeout=max(0.01, deadline - time.monotonic()))
        if self._owed > 0:
            log.warning("%s: the customer's words did not come back within %.0f s; replying without them",
                        self.label, STT_FINAL_WAIT)
            self._owed, self._skip_below = 0, self._utterances
        words, self._heard = " ".join(self._heard).strip(), []
        notes, self._notes = self._notes, []
        heard_ms = (time.monotonic() - self._ended_at) * 1000
        from .live_bridge import asked_language, machine_message   # here: live_bridge imports this module
        if words and self.manual_vad and machine_message(words):
            # The network's recorded voice (owner, 19 Oct 2026, call 294: "...has put your call on hold" for minutes, each
            # one answered). live_bridge sees the words and waits or hangs up; no reply is written, nothing joins history.
            log.info("%s: a recorded message on the line, not the customer; no reply to it", self.label)
            await self._q.put(_Message(server_content=_ServerContent(input_transcription=_Text(" " + words))))
            await self._q.put(_Message(server_content=_ServerContent(output_transcription=_Text("…"))))
            await self._turn_done()
            return
        if words and (code := asked_language(words)) and code != self.language:
            await self._switch_language(code)   # before the reply, so it is already in the new language
        if words:
            await self._q.put(_Message(server_content=_ServerContent(input_transcription=_Text(" " + words))))
        elif had_audio and self.manual_vad:
            # Sound with no words in it. Written "<noise>" as Gemini writes it, so live_bridge's own checks hold
            # the reply back and tell the agent; the reply itself is an empty stand-in, with no chat call.
            log.info("%s: no words in that turn (%.0f ms of audio)", self.label, self._turn_ms)
            await self._q.put(_Message(server_content=_ServerContent(input_transcription=_Text(" <noise>"))))
            await self._q.put(_Message(server_content=_ServerContent(output_transcription=_Text("…"))))
            await self._turn_done()
            return
        elif not notes:
            return   # a start/end pair with nothing in it
        log.info("%s: customer's words back %.0f ms after they stopped", self.label, heard_ms)
        self._user("\n".join(notes + ([words] if words else [])))
        hint, self.next_hint = self.next_hint, ""
        if (about := about_hint(words)):
            log.info("%s: company question likely; the matching ABOUT US lines go with this reply", self.label)
        self._start_reply(extra=hint + about)

    # ---- the reply ----

    async def _hand_over_tools(self) -> None:
        calls, raw = self._ready
        self._ready = ([], [])
        self.history.append({"role": "assistant", "content": self._said or None, "tool_calls": raw})
        self._tools_spoke = bool(self._said)
        await self._q.put(_Message(tool_call=_ToolCall(function_calls=calls)))

    def _start_reply(self, extra: str = "", fixed: str = "") -> None:
        if self._reply is not None and not self._reply.done():
            self._reply.cancel()
        self._responding, self._said, self._got_audio, self._ready = True, "", False, ([], [])
        self._helped = False
        self._reply = asyncio.create_task(self._make_reply(extra, fixed))

    async def _still_checking(self) -> None:
        """After a lookup: if the answer has not started within a few seconds, a short "one moment more"."""
        await asyncio.sleep(STILL_CHECKING_SECONDS)
        if not self._said:
            log.info("%s: the answer to a lookup is slow; saying one moment more", self.label)
            await self._speak(random.choice(_STILL[self._gender]))

    async def _make_reply(self, extra: str, fixed: str) -> None:
        still = asyncio.create_task(self._still_checking()) if self._after_lookup else None
        self._after_lookup = False
        try:
            started = time.monotonic()
            calls: list[_FunctionCall] = []
            raw_calls: list[dict] = []
            if fixed:
                await self._speak(fixed)
            else:
                for attempt in range(2):
                    calls, raw_calls = await self._chat(extra, started)
                    if self._said or calls:
                        break
                    log.warning("%s: the reply came back empty%s", self.label, "; asking again" if attempt == 0 else "")
            self._ready = (calls, raw_calls)   # written in full; only its sound may still be coming
            await self._spoken()
            if calls:
                self._responding = False
                await self._q.put(_Message(usage_metadata=self._usage()))
                await self._hand_over_tools()
                return
            if self._said:
                self.history.append({"role": "assistant", "content": self._said})
            await self._turn_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # The chat call or Bulbul failed: close the session, so live_bridge reconnects (or ends the call) as it
            # does for a Gemini drop, rather than the agent sitting silent.
            log.warning("%s: reply failed: %s: %s", self.label, type(exc).__name__, exc)
            self._closed = exc
            await self._q.put(None)
        finally:
            if still is not None:
                still.cancel()

    async def _chat(self, extra: str, started: float) -> tuple[list[_FunctionCall], list[dict]]:
        """One streamed chat call. Each sentence goes to Bulbul the moment it is complete."""
        payload = {"model": SARVAM_MODEL, "stream": True, "max_tokens": MAX_TOKENS,
                   "reasoning_effort": None,   # thinking is on by default and costs seconds on every reply
                   "messages": [{"role": "system", "content": self.prompt + self._language_note() + extra}] + self.history}
        if self.tools:
            payload["tools"], payload["tool_choice"] = self.tools, "auto"
        buf, parts, usage, first = "", {}, None, 0.0
        async with self._http.stream("POST", CHAT_URL, json=payload, headers=self._headers) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="replace")[:300]
                raise RuntimeError(f"Sarvam chat {r.status_code}: {body}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        buf += delta["content"]
                        while (m := _SENTENCE_END.search(buf)):
                            if not first:
                                first = time.monotonic()
                            await self._speak(buf[:m.end()])
                            buf = buf[m.end():]
                    for tc in delta.get("tool_calls") or []:
                        slot = parts.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        fn = tc.get("function") or {}
                        slot["id"] = tc.get("id") or slot["id"]
                        slot["name"] += fn.get("name") or ""
                        slot["args"] += fn.get("arguments") or ""
                        if slot["name"] in LOOKUP_TOOLS and not self._helped and not self._said and not buf.strip():
                            # The reply is turning to a lookup with nothing said: fill the wait at once.
                            self._helped = True
                            log.info("%s: %s with nothing said; saying a let-me-check line", self.label, slot["name"])
                            await self._speak(random.choice(_HELPER[self._gender]))
        if buf.strip():
            first = first or time.monotonic()
            await self._speak(buf)
        if first:
            log.info("%s: first sentence written %.0f ms after the chat call began", self.label, (first - started) * 1000)
        if usage:
            cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
            self._add("prompt", usage.get("prompt_tokens") or 0)
            self._add("cached_text_in", cached)
            self._add("text_in", max(0, int(usage.get("prompt_tokens") or 0) - cached))
            self._add("text_out", usage.get("completion_tokens") or 0)
        elif not self._warned_usage:
            self._warned_usage = True
            log.warning("%s: the chat reply carried no token counts; the Spending tab will miss its cost", self.label)
        calls, raw = [], []
        for n, (_, slot) in enumerate(sorted(parts.items())):
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                log.warning("%s: %s came with arguments that are not JSON: %r", self.label, slot["name"], slot["args"][:200])
                args = {}
            cid = slot["id"] or f"call_{len(self.history)}_{n}"
            calls.append(_FunctionCall(id=cid, name=slot["name"], args=args))
            raw.append({"id": cid, "type": "function",
                        "function": {"name": slot["name"], "arguments": slot["args"] or "{}"}})
        return calls, raw

    async def _speak(self, text: str) -> None:
        """One sentence: its words to live_bridge (the transcript and its checks), its text to Bulbul."""
        text = re.sub(r"\s+", " ", _MARKUP.sub("", text)).strip()
        if not text:
            return
        if self._said:
            text = " " + text
        await self._q.put(_Message(server_content=_ServerContent(output_transcription=_Text(text))))
        ws = await self._ready_tts()
        async with self._tts_lock:
            await ws.send(json.dumps({"type": "text", "data": {"text": text.strip()}}))
            await ws.send(json.dumps({"type": "flush"}))   # speak it now, not when Bulbul's buffer fills
        self._flushes += 1
        self._said += text
        self._last_tts = time.monotonic()
        self._add("tts_chars", len(text.strip()))

    async def _spoken(self) -> None:
        """Wait until Bulbul has sent all of this reply's sound."""
        while self._flushes > 0:
            if time.monotonic() - self._last_tts > TTS_QUIET_SECONDS:
                log.info("%s: no word from the voice for %.0f s; taking the reply as spoken", self.label, TTS_QUIET_SECONDS)
                self._flushes = 0
                break
            await asyncio.sleep(0.05)

    async def _turn_done(self) -> None:
        self._responding = False
        usage = self._usage()
        if usage is not None:
            await self._q.put(_Message(usage_metadata=usage))
        self._acc = {}
        await self._q.put(_Message(server_content=_ServerContent(turn_complete=True)))

    async def _interrupt(self) -> None:
        """The customer talked over a reply being made: stop writing it and stop the voice."""
        if self._reply is not None and not self._reply.done():
            self._reply.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reply
        self._responding = False
        await self._fresh_tts()
        calls = self._ready[0]   # with tool calls, its words go into history with them (below)
        if not calls and self._said and self._got_audio:
            self.history.append({"role": "assistant", "content": self._said})   # some of it was heard
        elif not calls and not self._got_audio and self.history and self.history[-1]["role"] == "user":
            self._carry = self.history.pop()["content"]   # nothing was heard: their next words join these
        usage = self._usage()
        if usage is not None:
            await self._q.put(_Message(usage_metadata=usage))
        self._acc = {}
        await self._q.put(_Message(server_content=_ServerContent(interrupted=True)))
        if calls:
            # Talked over while its sound was still arriving (a "ji" over the goodbye): the result it recorded
            # and the hang-up it asked for still stand.
            await self._hand_over_tools()

    # ---- what the sockets send ----

    async def _read_stt(self, ws) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                evt = json.loads(raw)
                kind = evt.get("event", "")
                if kind == "transcript.final":
                    idx = evt.get("utterance_idx")
                    if self.manual_vad and idx is not None and int(idx) < self._skip_below:
                        continue   # its turn already went ahead without it (counted from 0, ours from 1)
                    text = (evt.get("text") or "").strip()
                    if self.manual_vad:
                        if text:
                            self._heard.append(text)
                        self._owed = max(0, self._owed - 1)
                        self._final.set()
                    elif text:
                        # Browser test: Saaras found the turn itself.
                        await self._q.put(_Message(server_content=_ServerContent(input_transcription=_Text(" " + text))))
                        self._user(text)
                        self._start_reply()
                elif kind == "vad.speech_start" and not self.manual_vad and self._responding:
                    await self._interrupt()
                elif kind == "error":
                    log.warning("%s: speech-to-text error %s: %s", self.label, evt.get("code"), evt.get("message"))
                    if evt.get("is_fatal"):
                        raise RuntimeError(f"Sarvam speech-to-text {evt.get('code')}: {evt.get('message')}")
            raise ConnectionError("the Sarvam speech-to-text socket closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._closed is None:
                self._closed = exc
                await self._q.put(None)

    async def _read_tts(self, ws) -> None:
        try:
            async for raw in ws:
                evt = json.loads(raw) if isinstance(raw, str) else {}
                kind = evt.get("type", "")
                data = evt.get("data") or {}
                if kind == "audio":
                    self._last_tts = time.monotonic()
                    ctype = data.get("content_type") or ""
                    if "mpeg" in ctype or "mp3" in ctype:
                        if not self._warned_codec:
                            self._warned_codec = True
                            log.error("%s: the voice sent MP3 (%s), which the phone line cannot play", self.label, ctype)
                        continue
                    pcm = self._odd + _pcm(base64.b64decode(data.get("audio") or ""))
                    pcm, self._odd = (pcm[:-1], pcm[-1:]) if len(pcm) % 2 else (pcm, b"")
                    if pcm:
                        self._got_audio = True
                        await self._q.put(_Message(server_content=_ServerContent(
                            model_turn=_ModelTurn(parts=[_Part(inline_data=_Blob(data=pcm))]))))
                elif kind == "event" and data.get("event_type") == "final":
                    self._last_tts = time.monotonic()
                    self._flushes = max(0, self._flushes - 1)
                elif kind == "error":
                    msg = data.get("message") or json.dumps(evt, ensure_ascii=False)[:200]
                    log.warning("%s: voice error %s: %s", self.label, data.get("code"), msg)
                    if any(w in str(msg).lower() for w in ("credit", "quota", "subscription", "unauthor", "invalid api")):
                        raise RuntimeError(f"Sarvam voice: {msg}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._closed is None:
                self._closed = exc
                await self._q.put(None)
            return
        if self._tts is ws:
            self._tts = None   # closed on its own (idle); the next sentence opens a new one

    # ---- usage ----

    def _add(self, key: str, value: float) -> None:
        self._acc[key] = self._acc.get(key, 0) + value

    def _usage(self) -> _Usage | None:
        """This agent turn so far, as a running total (Gemini reports usage the same way)."""
        if not self._acc:
            return None
        u = _Usage(prompt_token_count=int(self._acc.get("prompt", 0)),
                   response_token_count=int(self._acc.get("text_out", 0)),
                   prompt_tokens_details=[_TokenDetail("TEXT", int(self._acc.get("text_in", 0)))],
                   response_tokens_details=[_TokenDetail("TEXT", int(self._acc.get("text_out", 0)))],
                   cached_text_in=int(self._acc.get("cached_text_in", 0)))
        u.stt_seconds = round(self._acc.get("stt_seconds", 0.0), 2)
        u.tts_chars = int(self._acc.get("tts_chars", 0))
        return u

    # ---- receiving and closing ----

    async def receive(self):
        """One agent turn, then stop, exactly as google-genai's session.receive() does."""
        while True:
            msg = await self._q.get()
            if msg is None:
                raise self._closed or ConnectionError("the Sarvam session closed")
            yield msg
            if msg.server_content is not None and msg.server_content.turn_complete:
                return

    async def aclose(self) -> None:
        tasks = [t for t in (self._reply, self._ending, self._keepalive, self._stt_reader, self._tts_reader, self._stt_switch,
                             self._tts_opening) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        for ws in (self._stt, self._tts):
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
        with contextlib.suppress(Exception):
            await self._http.aclose()


@contextlib.asynccontextmanager
async def connect(*, prompt: str, voice: str, label: str = "", manual_vad: bool = True,
                  tools: list | None = None, greeting: str = ""):
    """A live Sarvam session, entered the way live_bridge enters a Gemini one. Both sockets open here,
    while the phone is still ringing."""
    if not SARVAM_API_KEY:
        raise RuntimeError("SARVAM_API is not set in .env")
    speaker = voice_for(voice)
    session = SarvamSession(prompt=prompt + openai_accent(voice), speaker=speaker, label=label, manual_vad=manual_vad,
                            tools=chat_tools(tools), greeting=greeting)
    try:
        await session.open()
        log.info("%s: connected to Sarvam (chat %s, words %s/%s, voice %s %s, turns marked %s)", label, SARVAM_MODEL,
                 SARVAM_STT_MODEL, SARVAM_STT_MODE, SARVAM_TTS_MODEL, speaker, "by us" if manual_vad else "by Saaras")
        yield session
    finally:
        await session.aclose()
