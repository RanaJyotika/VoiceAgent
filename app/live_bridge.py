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
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types

from . import db
from .config import (AFFECTIVE_DIALOG, GEMINI_LIVE_MODEL, GEMINI_THINKING_BUDGET, GEMINI_VOICE,
                     GOOGLE_API_KEY, REPLY_WATCHDOG_SECONDS)
from .tools import TOOLS

log = logging.getLogger("bridge")

# Calls that are ringing or in progress right now, for the dashboard's live panel.
ACTIVE: dict[int, dict] = {}

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
    ended: bool = False
    speaking: bool = False
    idle_since: float | None = None     # when the agent last finished a turn
    loud_since: float | None = None     # first clear customer sound after that
    nudges: int = 0
    reconnects: int = 0
    error: str | None = None
    _in_buf: str = ""
    _out_buf: str = ""

    def add_input_text(self, text: str) -> None:
        self._in_buf += text

    def add_output_text(self, text: str) -> None:
        self._out_buf += text

    def flush_turn(self) -> None:
        if self._in_buf.strip():
            self.transcript.append({"role": "customer", "text": self._in_buf.strip()})
        if self._out_buf.strip():
            self.transcript.append({"role": "agent", "text": self._out_buf.strip()})
        self._in_buf = ""
        self._out_buf = ""


def build_config(system_prompt: str, voice: str = GEMINI_VOICE,
                 sensitive_listening: bool = False, fast: bool = True) -> types.LiveConnectConfig:
    extra: dict[str, Any] = {}
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
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                # LOW = a sound must be clearly speech before it counts as the customer talking.
                # HIGH made line noise interrupt the agent mid-sentence on real phone calls.
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=200,
                silence_duration_ms=500 if sensitive_listening else 600,
            )
        ),
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
    start_gate: "asyncio.Event | None" = None,
    meta: dict | None = None,
) -> CallState:
    """Run one full conversation. Returns when the agent ends the call or the source closes.

    start_gate: for phone calls. We connect to Gemini while the phone is still ringing and
    only start the greeting when the gate opens (the customer picked up), so the greeting
    is not delayed by connection setup. Waiting connected costs nothing.
    """
    call_id = db.start_call(contact["id"] if contact else None, channel)
    state = CallState(call_id=call_id)
    ACTIVE[call_id] = {"state": state, "contact": contact or {}, "channel": channel, "gate": start_gate,
                       "meta": meta or {}, "dialed_at": time.monotonic(), "voice": voice}
    client = genai.Client(api_key=GOOGLE_API_KEY)
    fast_modes = [True, False] if GEMINI_THINKING_BUDGET >= 0 else [False]
    await send_event({"type": "status", "text": f"connecting to {model}", "call_id": call_id})

    OPENING = "[The customer has answered the call. Greet them now, then wait for their reply.]"

    async def session_once(prompt: str, opening: str, first: bool) -> None:
        async with contextlib.AsyncExitStack() as stack:
            session = None
            for fast in fast_modes:
                try:
                    session = await stack.enter_async_context(client.aio.live.connect(
                        model=model, config=build_config(prompt, voice, sensitive_listening, fast)))
                    log.info("call %s: connected to %s (fast replies %s)", call_id, model, "on" if fast else "off")
                    break
                except Exception as exc:  # noqa: BLE001
                    if fast is False:
                        raise
                    log.warning("call %s: fast mode was refused (%s); retrying with model defaults", call_id, exc)
            await send_event({"type": "status", "text": "connected"})

            if start_gate is not None and first:
                await start_gate.wait()
                state.started = time.monotonic()   # measure the call from pick-up, not from dialing

            # Ask the agent to open the call. Phone calls start with the agent speaking.
            # A completed turn makes the agent greet at once. (A realtime text message would
            # wait for the microphone to go quiet first, which never happens in a noisy room.)
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=opening)]),
                turn_complete=True,
            )
            log.info("call %s: opening sent, voice=%s model=%s", call_id, voice, model)

            async def pump_in() -> None:
                while True:
                    chunk = await incoming_audio.get()
                    if chunk is None or state.ended:
                        return
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
                            um = msg.usage_metadata
                            state.usage = {
                                "prompt_tokens": getattr(um, "prompt_token_count", None),
                                "response_tokens": getattr(um, "response_token_count", None),
                                "total_tokens": getattr(um, "total_token_count", None),
                            }
                        sc = msg.server_content
                        if sc is not None:
                            if sc.interrupted:
                                state.speaking = False
                                state.idle_since = time.monotonic()
                                state.loud_since = None
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
                                        await send_audio(part.inline_data.data)
                            if sc.turn_complete:
                                state.speaking = False
                                state.idle_since = time.monotonic()
                                state.loud_since = None
                                log.info("call %s: agent turn complete, now waiting for the customer", call_id)
                                state.flush_turn()
                                await send_event({"type": "turn_complete"})
                        if msg.tool_call is not None:
                            responses = []
                            for fc in msg.tool_call.function_calls or []:
                                result = await handle_tool(fc.name, dict(fc.args or {}), state, send_event)
                                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                            await session.send_tool_response(function_responses=responses)
                            if state.ended:
                                # Give the final audio a moment to drain, then stop.
                                await asyncio.sleep(1.5)
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
        try:
            await session_once(system_prompt, OPENING, True)
        except Exception as exc:  # noqa: BLE001
            # The connection to Gemini dropped (network blip, server reset). The customer is still
            # on the line, so reconnect once and carry on instead of going silent and hanging up.
            if state.ended or not drain_stale_audio():
                raise
            state.flush_turn()
            state.reconnects += 1
            log.warning("call %s: connection to Gemini dropped (%s: %s); reconnecting once", call_id, type(exc).__name__, exc)
            so_far = "\n".join(f"{'You' if t['role'] == 'agent' else 'Customer'}: {t['text']}" for t in state.transcript) or "(nothing yet)"
            resume_prompt = (system_prompt + "\n\nTHE LINE DROPPED FOR A MOMENT AND IS BACK. Conversation so far:\n" + so_far +
                             "\nDo not greet or introduce yourself again. Say one short apology for the break "
                             "(for example: 'sorry ji, awaaz cut gayi thi') and continue from where you stopped.")
            await session_once(resume_prompt, "[The line is back. Apologise briefly for the break and continue.]", False)
    except Exception as exc:  # noqa: BLE001
        log.exception("call %s failed", call_id)
        state.error = f"{type(exc).__name__}: {exc}"
        await send_event({"type": "error", "text": state.error})
    finally:
        ACTIVE.pop(call_id, None)
        state.flush_turn()
        duration = time.monotonic() - state.started
        log.info("call %s ended: %.0fs, outcome=%s, watchdog nudges=%s, reconnects=%s", call_id, duration, state.outcome, state.nudges, state.reconnects)
        if start_gate is not None and not start_gate.is_set():
            state.outcome = state.outcome or "no_answer"
            duration = 0.0
        db.finish_call(
            call_id,
            outcome=state.outcome,
            outcome_details=state.outcome_details,
            transcript=state.transcript,
            usage=state.usage,
            error=state.error,
            duration_sec=round(duration, 1),
        )
        await send_event({"type": "ended", "call_id": call_id, "outcome": state.outcome,
                          "duration_sec": round(duration, 1)})
    return state


async def handle_tool(name: str, args: dict[str, Any], state: CallState, send_event: SendEvent) -> dict[str, Any]:
    log.info("tool %s %s", name, json.dumps(args, ensure_ascii=False))
    if name == "mark_outcome":
        state.outcome = args.get("outcome", "unclear")
        state.outcome_details = args
        await send_event({"type": "outcome", "outcome": state.outcome, "details": args})
        return {"status": "recorded"}
    if name == "end_call":
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
        if state._out_buf.strip():
            lines.append({"role": "agent", "text": state._out_buf.strip()})
        c = a["contact"]
        out.append({
            "call_id": call_id, "channel": a["channel"], "status": status, "seconds": round(seconds),
            "customer_name": c.get("customer_name", ""), "order_id": c.get("order_id", ""),
            "product": c.get("product", ""), "to": a["meta"].get("to") or c.get("phone", ""),
            "voice": a["voice"], "outcome": state.outcome, "lines": lines[-3:],
        })
    return out
