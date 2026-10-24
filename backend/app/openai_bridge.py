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
import re
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from .config import (OPENAI_API_KEY, OPENAI_REALTIME_MODEL, OPENAI_TRANSCRIBE_LANGUAGE,
                     OPENAI_TRANSCRIBE_MODEL, OPENAI_VOICE_FEMALE, OPENAI_VOICE_MALE, OPENAI_VOICES, VOICES)
from .prompts import openai_accent, prompt_stats
from .tools import TOOLS

log = logging.getLogger("openai")

WS_URL = "wss://api.openai.com/v1/realtime?model={model}"
RATE = 24000          # what the Realtime API takes and returns
MIN_COMMIT_MS = 120   # the API rejects a commit with less than ~100 ms of audio in the buffer
# Calls 150 and 151 (6 Oct 2026): the opening ran straight on into step 2 ("...ek minute baat ho sakti hai?
# Ji, aapne ... order kiya tha") without waiting for "haan ji, boliye". Added to the opening reply's instructions.
OPENING_ONLY = ("\n\nTHIS TURN ONLY: the call has just been answered. Say step 1 of THE USUAL ORDER and nothing more, "
                "then stop right after its question and wait for the customer to answer (for example 'haan ji, boliye'). "
                "Nothing from step 2 in this turn. Say it ONCE, written only in Devanagari: never in English letters, "
                "never twice. Say 'namaste ji' in a normal, even tone, with no stress on it.")   # call 172: said the line in English letters, then again in Devanagari
# Call 177: answering the opening note itself, it said "ठीक है, मैं सबसे पहले अभिवादन के साथ शुरू करता हूँ..." first.
# When the call type gives the exact line, no note is sent: the reply's own instructions hold the line.
OPENING_EXACT = ("\n\nTHE CALL HAS JUST BEEN ANSWERED. Your first words are exactly this line, word for word, in Devanagari, "
                 "in a normal, even tone, and nothing before or after it (the name the Hindi way): {line}")
# Whisper writes the customer's words down after the model has already answered. On call 150 the cancel check
# ran 0.2 s before "कैंसिल कर दो" arrived and refused a real cancellation, so a tool call waits this long at most.
TRANSCRIPT_WAIT_SECONDS = 2.0
# The agent narrating what it is about to do: every lead-in dropped so far, and the narration lines owners heard on
# calls 70-193, match; none of the other 300 agent lines on ChatGPT calls do (checked 8 Oct 2026).
NARRATION = re.compile(r"(बताऊंगी|बताऊँगी|बताऊंगा|बताऊँगा|पूछूंगी|पूछूँगी|पूछूंगा|पूछूँगा|करूँगी|करूंगी|करूँगा|करूंगा|चेक कर|"
                       r"समझ रही हूँ|समझ रहा हूँ|समझ रही हूं|समझ रहा हूं|आगे बढ़|देखती हूँ|देखता हूँ|कर लेती हूँ|कर लेता हूँ|"
                       r"संभाल|सम्भाल|एक पल|एक मिनट|शुरू करती|शुरू करता|recap|bataungi|bataunga|karungi|karunga|"
                       r"aage badh|dekhti hoon|dekhta hoon|samajh rahi|samajh raha|ek pal)", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[।.!?](?=\s|$)")


def is_openai_model(model: str) -> bool:
    return model.startswith("gpt-") or model.startswith("o1-") or "realtime" in model


def voice_for(gemini_voice: str) -> str:
    """An OpenAI voice of the same gender as the Gemini voice chosen on the dashboard, so the
    agent stays Gauri (female) or Arjun (male) whichever provider answers the call."""
    want = VOICES.get(gemini_voice, "female")
    chosen = OPENAI_VOICE_FEMALE if want == "female" else OPENAI_VOICE_MALE
    if chosen:
        return chosen
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


def openai_tools(tools: list | None = None) -> list[dict]:
    """The same tools the Gemini agent has on this call, in OpenAI's shape."""
    out = []
    for tool in tools if tools is not None else TOOLS:
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

    def __init__(self, ws, dialect: str, call_label: str = "", manual_vad: bool = True, prompt: str = "",
                 greeting: str = "") -> None:
        self.ws = ws
        self.dialect = dialect          # "ga" or "beta"
        self.label = call_label
        self.manual_vad = manual_vad    # we mark the turns (phone) rather than the server (browser)
        self.prompt = prompt            # the session instructions, repeated in the opening reply's own
        self.greeting = greeting        # the exact first line, when the call type gives one
        self._awaiting_tx: set = set()  # customer turns committed whose transcript has not arrived yet
        self._held_tool: _Message | None = None   # a tool call waiting for that transcript
        self._release: asyncio.Task | None = None
        self._got_audio = False         # the reply being made has produced sound
        # A reply's first spoken part is held until we know whether a second one follows it. Calls 167, 173, 177
        # and 179: the model spoke a lead-in of its own ("aaiye, ab main ise saaf-saaf khatm kar deta hoon") as a
        # separate part, then the real reply. When a second spoken part comes, the first is never played.
        self._first_item = ""           # the reply's first spoken part
        self._held: list = []           # its messages (sound and words), in order, not yet passed on
        self._holding = False
        self._held_text = ""
        # Only one spoken part of a reply is ever played. Call 180: the greeting came as two identical parts
        # ("...ho rahi hai?नमस्ते जी...") and both were played.
        self._played_item = ""
        self._dropped_text = ""
        self._tools_spoke = False       # the reply that called the tools waiting for answers had sound in it
        self._retried = False           # an empty reply was already asked for again this customer turn
        self._retry = False
        self._tool_buf: list = []       # function calls of the response being read
        self._warned_vad = False
        self._delta_items: set = set()  # customer turns whose transcript already arrived in pieces
        self._q: asyncio.Queue = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._resample = None           # 16 kHz -> 24 kHz state, kept across chunks
        self._appended_ms = 0.0         # audio in the input buffer since the last commit
        self._responding = False        # the model is producing a reply right now
        self._pending_tools = 0         # tool calls we owe an answer to
        self.next_hint = ""             # extra instructions for the reply to the next customer turn only (live_bridge)
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
        if self._responding:
            # The reply watchdog fired while the answer was already on its way. Asking for a
            # second one is an error ("conversation already has an active response").
            log.info("%s: a reply is already coming; not asking for another", self.label)
            return
        if self._appended_ms < MIN_COMMIT_MS:
            if self._appended_ms:
                log.info("%s: only %.0f ms of audio, not committing a turn", self.label, self._appended_ms)
            self._appended_ms = 0.0
            return
        self._appended_ms = 0.0
        self._retried = False   # a new customer turn: an empty reply to it may be asked for again once
        hint, self.next_hint = self.next_hint, ""
        await self._send({"type": "input_audio_buffer.commit"})
        await self._reply(self.prompt + hint if hint and self.prompt else "")

    async def _reply(self, instructions: str = "") -> None:
        payload: dict = {"type": "response.create"}
        if instructions:
            payload["response"] = {"instructions": instructions}   # replaces the session's for this reply only
        await self._send(payload)
        self._responding, self._got_audio = True, False

    async def _say_user_text(self, text: str, instructions: str = "") -> None:
        """A text turn: the opening instruction, a typed test reply, or a note from the phone line."""
        await self._send({"type": "conversation.item.create",
                          "item": {"type": "message", "role": "user",
                                   "content": [{"type": "input_text", "text": text}]}})
        await self._reply(instructions)

    async def send_client_content(self, turns=None, turn_complete: bool = True) -> None:
        """Gemini's way of starting the call. Same thing here: one user turn, then a reply."""
        text = ""
        for part in getattr(turns, "parts", None) or []:
            text += getattr(part, "text", "") or ""
        # live_bridge's OPENING ("Say your first turn now"), not a reconnect's "[The line is back...]".
        opening = "first turn" in text and self.prompt
        if opening and self.greeting:
            await self._reply(self.prompt + OPENING_EXACT.format(line=self.greeting))
            return
        await self._say_user_text(text, self.prompt + OPENING_ONLY if opening else "")

    async def send_tool_response(self, *, function_responses) -> None:
        ending = False
        for fr in function_responses or []:
            await self._send({"type": "conversation.item.create",
                              "item": {"type": "function_call_output",
                                       "call_id": getattr(fr, "id", "") or "",
                                       "output": json.dumps(getattr(fr, "response", {}) or {}, ensure_ascii=False)}})
            self._pending_tools = max(0, self._pending_tools - 1)
            ending = ending or (getattr(fr, "name", "") == "end_call"
                                and (getattr(fr, "response", {}) or {}).get("status") == "ending")
        if ending:
            # The goodbye has already been spoken and the line is about to close. Asking for
            # another reply here makes the agent start talking over its own hang-up.
            return
        plain = all(set((getattr(fr, "response", {}) or {})) <= {"status"}
                    and (getattr(fr, "response", {}) or {}).get("status") == "recorded" for fr in function_responses or [])
        if plain and self._tools_spoke:
            # A result simply written down, by a reply that already said its piece: asking for more makes it say the
            # same thing again (call 159: "...team tak pahuncha dungi" then "ji, main team ko bata dungi...").
            await self._q.put(_Message(server_content=_ServerContent(turn_complete=True)))
            return
        # The model has something to act on (a refusal, a next step) or has not spoken yet: let it carry on.
        await self._reply()

    # ---- receiving ----

    async def _read_loop(self) -> None:
        """Reads the socket for the whole call and turns each event into a Gemini-shaped message.
        A task of its own so nothing is missed between two receive() generators."""
        try:
            async for raw in self.ws:
                evt = json.loads(raw)
                for msg in self._translate(evt):
                    if msg.tool_call is not None and self._awaiting_tx:
                        # The checks on this tool call read the customer's words; let them arrive first.
                        self._held_tool = msg
                        self._release = asyncio.create_task(self._release_tool_later())
                        continue
                    await self._q.put(msg)
                if self._held_tool is not None and not self._awaiting_tx:
                    await self._release_tool()
                if self._retry:
                    # Call 151: a reply came back with no sound in it, and the agent sat silent for 11 s
                    # until the silence check asked "hello ji?". Ask for it again, once per customer turn.
                    self._retry, self._retried = False, True
                    await self._reply()
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

    async def _release_tool(self) -> None:
        msg, self._held_tool = self._held_tool, None
        if self._release is not None and self._release is not asyncio.current_task():
            self._release.cancel()
        self._release = None
        if msg is not None:
            await self._q.put(msg)

    async def _release_tool_later(self) -> None:
        await asyncio.sleep(TRANSCRIPT_WAIT_SECONDS)
        if self._held_tool is not None:
            log.info("%s: the customer's transcript did not come within %.0f s; checking the tool call without it",
                     self.label, TRANSCRIPT_WAIT_SECONDS)
            self._awaiting_tx.clear()   # a transcript that never comes must not hold up later tool calls
            await self._release_tool()

    def _translate(self, evt: dict) -> list[_Message]:
        kind = evt.get("type", "")
        out: list[_Message] = []

        if kind in ("response.audio.delta", "response.output_audio.delta"):
            data = base64.b64decode(evt.get("delta") or "")
            if data:
                self._got_audio = True
                self._spoken(evt, _Message(server_content=_ServerContent(
                    model_turn=_ModelTurn(parts=[_Part(inline_data=_Blob(data=data))]))), out)

        elif kind in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
            if evt.get("delta"):
                self._spoken(evt, _Message(server_content=_ServerContent(output_transcription=_Text(evt["delta"]))), out)
                if self._holding and evt.get("item_id") == self._first_item:
                    self._held_text += evt["delta"]
                    # A question is the reply itself, never a lead-in: play it now. Unless it is in English letters
                    # (call 172: the greeting in English letters, then again in Devanagari): wait for a second part.
                    deva = sum(1 for ch in self._held_text if "ऀ" <= ch <= "ॿ")
                    latin = sum(1 for ch in self._held_text if ch.isascii() and ch.isalpha())
                    # Call 195: a reply was held until its "?" at the very end, so a 13 s answer started 1.75 s late.
                    # A first sentence that is not narration is the reply itself too: play it as soon as it is complete.
                    done = self._held_text[:max((m.end() for m in _SENTENCE_END.finditer(self._held_text)), default=0)]
                    if (deva >= latin and not NARRATION.search(self._held_text)
                            and ("?" in self._held_text or len(done.split()) >= 4)):
                        out.extend(self._held)
                        self._held, self._holding, self._played_item = [], False, self._first_item

        elif kind == "response.created":
            self._first_item, self._held, self._holding, self._held_text = "", [], True, ""
            self._played_item, self._dropped_text = "", ""

        elif kind == "response.output_item.added":
            item = evt.get("item") or {}
            if (item.get("type") == "message" and self._holding and self._first_item
                    and item.get("id") != self._first_item):
                log.info("%s: not played (a lead-in before the reply): %s", self.label, self._held_text.strip()[:200])
                self._held, self._holding = [], False

        elif kind == "conversation.item.input_audio_transcription.delta":
            if evt.get("delta"):
                item, text = evt.get("item_id") or "", evt["delta"]
                if item not in self._delta_items:
                    # Each stretch of speech is transcribed on its own, and the first piece has no space in
                    # front: call 153 wrote "site par" + "nahi" as "परनहीं", and the check never saw the no.
                    text = " " + text.lstrip()
                self._delta_items.add(item)
                out.append(_Message(server_content=_ServerContent(input_transcription=_Text(text))))

        elif kind == "input_audio_buffer.committed":
            if evt.get("item_id"):
                self._awaiting_tx.add(evt["item_id"])

        elif kind == "conversation.item.input_audio_transcription.failed":
            self._awaiting_tx.discard(evt.get("item_id") or "")

        elif kind == "conversation.item.input_audio_transcription.completed":
            # The finished transcript repeats everything the deltas already carried. Adding both
            # wrote every customer turn down twice ("160036160036" on call 60), which doubled the
            # tokens and left the agent reading digits that were never said.
            item = evt.get("item_id") or ""
            self._awaiting_tx.discard(item)
            if item in self._delta_items:
                self._delta_items.discard(item)
            elif evt.get("transcript"):
                out.append(_Message(server_content=_ServerContent(input_transcription=_Text(" " + evt["transcript"].strip()))))

        elif kind == "input_audio_buffer.speech_started":
            if self.manual_vad:
                # We asked for turn_detection null, so this event should never arrive. It means
                # the server is still listening for pauses itself and will answer on its own,
                # on top of our replies. Worth saying plainly once.
                if not self._warned_vad:
                    self._warned_vad = True
                    log.warning("%s: the server is detecting turns even though we asked it not to; "
                                "replies may double up. Check the wire format in the connect line.", self.label)
            elif self._responding:
                # Browser test: the customer talked over the agent.
                self._responding = False
                out.append(_Message(server_content=_ServerContent(interrupted=True)))

        elif kind == "response.function_call_arguments.done":
            try:
                args = json.loads(evt.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # Held until the response ends so that all of a turn's tool calls arrive together, as
            # Gemini delivers them. One at a time meant one extra reply request per tool, and the
            # agent said a second goodbye after mark_outcome.
            self._tool_buf.append(_FunctionCall(id=evt.get("call_id") or "",
                                                name=evt.get("name") or "", args=args))

        elif kind == "response.done":
            self._responding = False
            resp = evt.get("response") or {}
            status = resp.get("status") or ""
            if self._holding and status != "cancelled":
                out.extend(self._held)   # only one spoken part: it was the reply
            self._held, self._holding = [], False
            if self._dropped_text.strip():
                log.info("%s: not played (a second spoken part of the same reply): %s", self.label,
                         self._dropped_text.strip()[:200])
            if not self._got_audio and not self._tool_buf and status != "cancelled":
                log.warning("%s: the reply came back with no sound (status %s, %s)%s", self.label, status,
                            json.dumps(resp.get("status_details") or {}, ensure_ascii=False)[:200],
                            "; already asked again once" if self._retried else "; asking for it again")
                self._retry = not self._retried
            usage = self._usage(resp.get("usage") or {})
            if usage is not None:
                out.append(_Message(usage_metadata=usage))
            if self._tool_buf:
                self._pending_tools = len(self._tool_buf)
                self._tools_spoke = self._got_audio
                out.append(_Message(tool_call=_ToolCall(function_calls=self._tool_buf)))
                self._tool_buf = []
            else:
                # A reply that called a tool is not the end of the agent's turn: the spoken
                # follow-up comes after we hand the tool's answer back.
                out.append(_Message(server_content=_ServerContent(turn_complete=True)))

        elif kind == "error":
            err = evt.get("error") or {}
            log.warning("%s: realtime error %s: %s", self.label, err.get("code"), err.get("message"))
            if str(err.get("code") or "") in ("insufficient_quota", "billing_hard_limit_reached"):
                # Out of credit is not a blip; _read_loop closes the session so the call can
                # fall back rather than sit silent (Gemini did exactly this on 22 Sep, calls 46-48).
                self._closed = RuntimeError(f"OpenAI billing: {err.get('message')}")

        return out

    def _spoken(self, evt: dict, msg: _Message, out: list) -> None:
        """Pass on a piece of the reply's sound or words, or hold it while it belongs to the first spoken part."""
        item = evt.get("item_id") or ""
        if self._holding and not self._first_item:
            self._first_item = item
        if self._holding and item == self._first_item:
            self._held.append(msg)
        elif self._played_item and item != self._played_item:
            sc = msg.server_content
            if sc is not None and sc.output_transcription is not None:
                self._dropped_text += sc.output_transcription.text
        else:
            self._played_item = item
            out.append(msg)

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

def _transcription() -> dict:
    """How the customer's words get written down. Everything that guards the call reads this
    text, so the language hint is worth having when a call is known to be in one language."""
    cfg = {"model": OPENAI_TRANSCRIBE_MODEL}
    if OPENAI_TRANSCRIBE_LANGUAGE:
        cfg["language"] = OPENAI_TRANSCRIBE_LANGUAGE
    return cfg


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
                          "transcription": _transcription()},
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
        "input_audio_transcription": _transcription(),
        "turn_detection": _turn_detection(manual_vad),
        "tools": tools, "tool_choice": "auto",
    }}


async def _try_dialect(model: str, dialect: str, prompt: str, voice: str, label: str, manual_vad: bool,
                      tools: list | None = None, greeting: str = ""):
    """Open the socket and settle the session in one wire format. Returns a live session or
    raises, so the caller can try the other format."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    if dialect == "beta":
        headers["OpenAI-Beta"] = "realtime=v1"
    ws = await ws_connect(WS_URL.format(model=model), additional_headers=headers,
                          max_size=None, open_timeout=15, ping_interval=20)
    try:
        await ws.send(json.dumps(_session_payload(dialect, prompt, voice, openai_tools(tools), manual_vad)))
        # The server answers session.updated when it liked the settings, or error when it did not.
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            kind = evt.get("type", "")
            if kind in ("session.updated", "session.created"):
                if kind == "session.created":
                    continue   # the greeting event; the answer to our update is still coming
                return OpenAISession(ws, dialect, label, manual_vad=manual_vad, prompt=prompt, greeting=greeting)
            if kind == "error":
                err = evt.get("error") or {}
                raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
    except BaseException:
        with contextlib.suppress(Exception):
            await ws.close()
        raise


@contextlib.asynccontextmanager
async def connect(*, model: str = "", prompt: str, voice: str, label: str = "", manual_vad: bool = True,
                  tools: list | None = None, greeting: str = ""):
    """A live OpenAI session, as an async context manager so live_bridge can enter it exactly
    the way it enters a Gemini one."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    model = model or OPENAI_REALTIME_MODEL
    oa_voice = voice_for(voice)
    prompt = prompt + openai_accent(voice)   # same script as Gemini, plus which Hindi to speak, in the agent's gender
    log.info("%s: system prompt %s", label, prompt_stats(prompt))
    last: Exception | None = None
    session = None
    for dialect in ("ga", "beta"):
        try:
            session = await _try_dialect(model, dialect, prompt, voice=oa_voice, label=label, manual_vad=manual_vad,
                                            tools=tools, greeting=greeting)
            log.info("%s: connected to %s (%s wire format, voice %s, transcriber %s%s, turns marked %s)",
                     label, model, dialect, oa_voice, OPENAI_TRANSCRIBE_MODEL,
                     f"/{OPENAI_TRANSCRIBE_LANGUAGE}" if OPENAI_TRANSCRIBE_LANGUAGE else "",
                     "by us" if manual_vad else "by the server")
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
