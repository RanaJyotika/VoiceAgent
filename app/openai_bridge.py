"""OpenAI Realtime API as a second voice agent, shaped like the Gemini Live session.

Why an adapter and not a second call loop: live_bridge.run_call only ever touches four methods
on a session (send_realtime_input, send_client_content, send_tool_response, receive) and reads
three fields off each message (server_content, tool_call, usage_metadata). Presenting exactly
that surface means pump_in/pump_out, the manual speech detection, the tools, the transcript and
the outcome handling all work unchanged, and the Gemini path is not touched at all.

Mapping, Gemini -> OpenAI Realtime:
  ACTIVITY_START          -> nothing (or response.cancel, when the agent is mid-sentence)
  audio chunk             -> input_audio_buffer.append (resampled 16 kHz -> 24 kHz)
  ACTIVITY_END            -> input_audio_buffer.commit + response.create
  audio_stream_end=True   -> the same commit + response.create (the reply watchdog)
  text turn               -> conversation.item.create + response.create
  FunctionResponse        -> conversation.item.create(function_call_output) + response.create

Audio: OpenAI takes and returns 24 kHz 16-bit mono PCM. The phone bridge already resamples the
agent's 24 kHz down to 16 kHz for Plivo, so only the input direction needs new resampling.

The Realtime API exists in two wire formats (the newer "GA" shape, where audio settings are
nested under session.audio, and the older beta shape with flat fields). Which one an account
gets is not something we can know from here, so the session is opened by trying them in order,
the same way live_bridge already drops Gemini options a model refuses.
"""
import asyncio
import audioop
import base64
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from .config import (OPENAI_API_KEY, OPENAI_REALTIME_MODEL, OPENAI_TRANSCRIBE_MODEL, OPENAI_VOICES,
                     VOICES)
from .tools import TOOLS

log = logging.getLogger("openai")

WS_URL = "wss://api.openai.com/v1/realtime?model={model}"
RATE = 24000          # what the Realtime API takes and returns
MIN_COMMIT_MS = 120   # the API rejects a commit with less than ~100 ms of audio in the buffer


def is_openai_model(model: str) -> bool:
    return model.startswith("gpt-") or model.startswith("o1-") or "realtime" in model


def voice_for(gemini_voice: str) -> str:
    """An OpenAI voice of the same gender as the Gemini voice chosen on the dashboard, so the
    agent stays Gauri (female) or Arjun (male) whichever provider answers the call."""
    want = VOICES.get(gemini_voice, "female")
    for name, gender in OPENAI_VOICES.items():
        if gender == want:
            return name
    return "marin"


# --- tool schemas -------------------------------------------------------------------------

def _json_schema(node: Any) -> Any:
    """One google-genai Schema as plain JSON Schema: the type names are upper case there
    (STRING, OBJECT) and lower case here, and None fields have to go."""
    if node is None:
        return None
    out: dict[str, Any] = {}
    kind = getattr(node, "type", None)
    if kind is not None:
        out["type"] = str(getattr(kind, "value", kind)).lower()
    if getattr(node, "description", None):
        out["description"] = node.description
    if getattr(node, "enum", None):
        out["enum"] = list(node.enum)
    props = getattr(node, "properties", None)
    if props:
        out["properties"] = {k: _json_schema(v) for k, v in props.items()}
    if getattr(node, "items", None) is not None:
        out["items"] = _json_schema(node.items)
    if getattr(node, "required", None):
        out["required"] = list(node.required)
    return out


def openai_tools() -> list[dict]:
    """The same three tools the Gemini agent has, in OpenAI's shape."""
    out = []
    for tool in TOOLS:
        for fd in tool.function_declarations or []:
            out.append({"type": "function", "name": fd.name, "description": fd.description or "",
                        "parameters": _json_schema(fd.parameters) or {"type": "object", "properties": {}}})
    return out


# --- messages shaped like google-genai's ------------------------------------------------------

@dataclass
class _Blob:
    data: bytes


@dataclass
class _Part:
    inline_data: _Blob | None = None


@dataclass
class _ModelTurn:
    parts: list = field(default_factory=list)


@dataclass
class _Text:
    text: str


@dataclass
class _ServerContent:
    interrupted: bool = False
    input_transcription: _Text | None = None
    output_transcription: _Text | None = None
    model_turn: _ModelTurn | None = None
    turn_complete: bool = False


@dataclass
class _FunctionCall:
    id: str
    name: str
    args: dict


@dataclass
class _ToolCall:
    function_calls: list


@dataclass
class _TokenDetail:
    modality: str
    token_count: int


@dataclass
class _Usage:
    """Shaped for live_bridge.turn_usage, plus the cached counts OpenAI bills at a tenth."""
    prompt_token_count: int = 0
    response_token_count: int = 0
    prompt_tokens_details: list = field(default_factory=list)
    response_tokens_details: list = field(default_factory=list)
    cached_text_in: int = 0
    cached_audio_in: int = 0


@dataclass
class _Message:
    server_content: _ServerContent | None = None
    tool_call: _ToolCall | None = None
    usage_metadata: _Usage | None = None


# --- the session ------------------------------------------------------------------------------

class OpenAISession:
    """One Realtime conversation, presenting the handful of methods live_bridge calls."""

    def __init__(self, ws, dialect: str, call_label: str = "") -> None:
        self.ws = ws
        self.dialect = dialect          # "ga" or "beta"
        self.label = call_label
        self._q: asyncio.Queue = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._resample = None           # 16 kHz -> 24 kHz state, kept across chunks
        self._appended_ms = 0.0         # audio in the input buffer since the last commit
        self._responding = False        # the model is producing a reply right now
        self._pending_tools = 0         # tool calls we owe an answer to
        self._closed: BaseException | None = None
        self._reader = asyncio.create_task(self._read_loop())

    # ---- sending ----

    async def _send(self, payload: dict) -> None:
        async with self._send_lock:
            await self.ws.send(json.dumps(payload))

    async def send_realtime_input(self, *, activity_start=None, activity_end=None, text=None,
                                  audio=None, audio_stream_end=None) -> None:
        if activity_start is not None:
            # The customer started talking. If the agent is mid-sentence this is a barge-in:
            # Gemini detects that itself and reports it, so here we have to say it out loud.
            if self._responding:
                await self._send({"type": "response.cancel"})
                self._responding = False
                await self._q.put(_Message(server_content=_ServerContent(interrupted=True)))
            return
        if activity_end is not None or audio_stream_end:
            await self._commit_and_reply()
            return
        if text is not None:
            await self._say_user_text(str(text))
            return
        if audio is not None:
            pcm = getattr(audio, "data", audio)
            if pcm:
                up, self._resample = audioop.ratecv(pcm, 2, 1, 16000, RATE, self._resample)
                self._appended_ms += len(pcm) / 32.0   # 16 kHz 16-bit: 32 bytes per millisecond
                await self._send({"type": "input_audio_buffer.append",
                                  "audio": base64.b64encode(up).decode()})

    async def _commit_and_reply(self) -> None:
        """Close the customer's turn and ask for the reply. A commit with almost nothing in the
        buffer is rejected by the API, so a stray end marker is dropped instead of raising."""
        if self._appended_ms < MIN_COMMIT_MS:
            if self._appended_ms:
                log.info("%s: only %.0f ms of audio, not committing a turn", self.label, self._appended_ms)
            self._appended_ms = 0.0
            return
        self._appended_ms = 0.0
        await self._send({"type": "input_audio_buffer.commit"})
        await self._send({"type": "response.create"})
        self._responding = True

    async def _say_user_text(self, text: str) -> None:
        """A text turn: the opening instruction, a typed test reply, or a note from the phone line."""
        await self._send({"type": "conversation.item.create",
                          "item": {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": text}]}})
        await self._send({"type": "response.create"})
        self._responding = True

    async def send_client_content(self, turns=None, turn_complete: bool = True) -> None:
        """Gemini's way of starting the call. Same thing here: one user turn, then a reply."""
        text = ""
        for part in getattr(turns, "parts", None) or []:
            text += getattr(part, "text", "") or ""
        await self._say_user_text(text)

    async def send_tool_response(self, *, function_responses) -> None:
        for fr in function_responses or []:
            await self._send({"type": "conversation.item.create",
                              "item": {"type": "function_call_output",
                                       "call_id": getattr(fr, "id", "") or "",
                                       "output": json.dumps(getattr(fr, "response", {}) or {}, ensure_ascii=False)}})
            self._pending_tools = max(0, self._pending_tools - 1)
        # The model has its answer; let it carry on speaking.
        await self._send({"type": "response.create"})
        self._responding = True

    # ---- receiving ----

    async def _read_loop(self) -> None:
        """Reads the socket for the whole call and turns each event into a Gemini-shaped message.
        A task of its own so nothing is missed between two receive() generators."""
        try:
            async for raw in self.ws:
                evt = json.loads(raw)
                for msg in self._translate(evt):
                    await self._q.put(msg)
                if self._closed is not None:
                    await self._q.put(None)      # a fatal error event: wake receive() with it
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._closed = exc
            await self._q.put(None)
        else:
            self._closed = ConnectionError("the OpenAI realtime socket closed")
            await self._q.put(None)

    def _translate(self, evt: dict) -> list[_Message]:
        kind = evt.get("type", "")
        out: list[_Message] = []

        if kind in ("response.audio.delta", "response.output_audio.delta"):
            data = base64.b64decode(evt.get("delta") or "")
            if data:
                out.append(_Message(server_content=_ServerContent(
                    model_turn=_ModelTurn(parts=[_Part(inline_data=_Blob(data=data))]))))

        elif kind in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
            if evt.get("delta"):
                out.append(_Message(server_content=_ServerContent(output_transcription=_Text(evt["delta"]))))

        elif kind == "conversation.item.input_audio_transcription.delta":
            if evt.get("delta"):
                out.append(_Message(server_content=_ServerContent(input_transcription=_Text(evt["delta"]))))

        elif kind == "conversation.item.input_audio_transcription.completed":
            # Some builds send only the finished transcript, with no deltas at all.
            if evt.get("transcript"):
                out.append(_Message(server_content=_ServerContent(input_transcription=_Text(evt["transcript"]))))

        elif kind == "input_audio_buffer.speech_started":
            # Server turn detection only (the browser test): the customer talked over the agent.
            if self._responding:
                self._responding = False
                out.append(_Message(server_content=_ServerContent(interrupted=True)))

        elif kind == "response.function_call_arguments.done":
            try:
                args = json.loads(evt.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            self._pending_tools += 1
            out.append(_Message(tool_call=_ToolCall(function_calls=[
                _FunctionCall(id=evt.get("call_id") or "", name=evt.get("name") or "", args=args)])))

        elif kind == "response.done":
            self._responding = False
            resp = evt.get("response") or {}
            usage = self._usage(resp.get("usage") or {})
            if usage is not None:
                out.append(_Message(usage_metadata=usage))
            # A reply that only called a tool is not the end of the agent's turn: the spoken
            # follow-up comes after we hand the tool's answer back.
            if not self._pending_tools:
                out.append(_Message(server_content=_ServerContent(turn_complete=True)))

        elif kind == "error":
            err = evt.get("error") or {}
            log.warning("%s: realtime error %s: %s", self.label, err.get("code"), err.get("message"))
            if str(err.get("code") or "") in ("insufficient_quota", "billing_hard_limit_reached"):
                # Out of credit is not a blip; _read_loop closes the session so the call can
                # fall back rather than sit silent (Gemini did exactly this on 22 Sep, calls 46-48).
                self._closed = RuntimeError(f"OpenAI billing: {err.get('message')}")

        return out

    def _usage(self, u: dict) -> _Usage | None:
        if not u:
            return None
        ind = u.get("input_token_details") or {}
        outd = u.get("output_token_details") or {}
        cached = ind.get("cached_tokens_details") or {}
        c_text, c_audio = int(cached.get("text_tokens") or 0), int(cached.get("audio_tokens") or 0)
        # OpenAI counts cached tokens inside the input totals; they are billed at a tenth, so
        # they are taken out here and priced separately (see OPENAI_PRICE_PER_M).
        text_in = max(0, int(ind.get("text_tokens") or 0) - c_text)
        audio_in = max(0, int(ind.get("audio_tokens") or 0) - c_audio)
        return _Usage(
            prompt_token_count=int(u.get("input_tokens") or 0),
            response_token_count=int(u.get("output_tokens") or 0),
            prompt_tokens_details=[_TokenDetail("TEXT", text_in), _TokenDetail("AUDIO", audio_in)],
            response_tokens_details=[_TokenDetail("TEXT", int(outd.get("text_tokens") or 0)),
                                     _TokenDetail("AUDIO", int(outd.get("audio_tokens") or 0))],
            cached_text_in=c_text, cached_audio_in=c_audio,
        )

    async def receive(self):
        """One agent turn, then stop, exactly as google-genai's session.receive() does."""
        while True:
            msg = await self._q.get()
            if msg is None:
                raise self._closed or ConnectionError("the OpenAI realtime socket closed")
            yield msg
            if msg.server_content is not None and msg.server_content.turn_complete:
                return

    async def aclose(self) -> None:
        self._reader.cancel()
        # CancelledError is a BaseException, so suppress(Exception) alone lets it escape and
        # every call would end on a traceback instead of hanging up cleanly.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        with contextlib.suppress(Exception):
            await self.ws.close()


# --- opening a session --------------------------------------------------------------------

def _turn_detection(manual_vad: bool) -> dict | None:
    """None when we mark the turns ourselves (phone calls). On the browser test there is no such
    marking, so the server listens for the pause itself, as Gemini does there."""
    if manual_vad:
        return None
    return {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300,
            "silence_duration_ms": 600, "create_response": True}


def _session_payload(dialect: str, prompt: str, voice: str, tools: list[dict], manual_vad: bool) -> dict:
    """The session settings in one of the two wire formats."""
    if dialect == "ga":
        return {"type": "session.update", "session": {
            "type": "realtime",
            "instructions": prompt,
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": RATE},
                          "turn_detection": _turn_detection(manual_vad),
                          "transcription": {"model": OPENAI_TRANSCRIBE_MODEL}},
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": voice},
            },
            "tools": tools, "tool_choice": "auto",
        }}
    return {"type": "session.update", "session": {
        "modalities": ["audio", "text"],
        "instructions": prompt,
        "voice": voice,
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "input_audio_transcription": {"model": OPENAI_TRANSCRIBE_MODEL},
        "turn_detection": _turn_detection(manual_vad),
        "tools": tools, "tool_choice": "auto",
    }}


async def _try_dialect(model: str, dialect: str, prompt: str, voice: str, label: str, manual_vad: bool):
    """Open the socket and settle the session in one wire format. Returns a live session or
    raises, so the caller can try the other format."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    if dialect == "beta":
        headers["OpenAI-Beta"] = "realtime=v1"
    ws = await ws_connect(WS_URL.format(model=model), additional_headers=headers,
                          max_size=None, open_timeout=15, ping_interval=20)
    try:
        await ws.send(json.dumps(_session_payload(dialect, prompt, voice, openai_tools(), manual_vad)))
        # The server answers session.updated when it liked the settings, or error when it did not.
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            kind = evt.get("type", "")
            if kind in ("session.updated", "session.created"):
                if kind == "session.created":
                    continue   # the greeting event; the answer to our update is still coming
                return OpenAISession(ws, dialect, label)
            if kind == "error":
                err = evt.get("error") or {}
                raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
    except BaseException:
        with contextlib.suppress(Exception):
            await ws.close()
        raise


@contextlib.asynccontextmanager
async def connect(*, model: str = "", prompt: str, voice: str, label: str = "", manual_vad: bool = True):
    """A live OpenAI session, as an async context manager so live_bridge can enter it exactly
    the way it enters a Gemini one."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model = model or OPENAI_REALTIME_MODEL
    oa_voice = voice_for(voice)
    last: Exception | None = None
    session = None
    for dialect in ("ga", "beta"):
        try:
            session = await _try_dialect(model, dialect, prompt, voice=oa_voice, label=label, manual_vad=manual_vad)
            log.info("%s: connected to %s (%s wire format, voice %s, transcriber %s)",
                     label, model, dialect, oa_voice, OPENAI_TRANSCRIBE_MODEL)
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("%s: %s wire format refused (%s)", label, dialect, exc)
            if "insufficient_quota" in str(exc) or "billing" in str(exc).lower():
                raise
    if session is None:
        raise last or RuntimeError("could not open an OpenAI realtime session")
    try:
        yield session
    finally:
        await session.aclose()
