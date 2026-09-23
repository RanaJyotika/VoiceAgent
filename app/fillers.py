"""Filler sounds that cover the wait for the agent's reply.

On a phone call about 3 seconds pass between the customer finishing and Gemini's first audio
(measured on calls 54 to 58; thinking is already off). A short "hmm" or "achha" in the agent's own
voice, played the moment the customer's turn closes, makes that wait feel like a breath.

The clips are made once per voice with Gemini text-to-speech, which offers the same prebuilt
voices as the Live model (Aoede, Puck, ...), and cached in data/fillers/<voice>/. Switching the
agent to another voice therefore needs no recording: the first call on that voice generates its
clips (a few paise, FILLER_GENERATE=false to forbid it). When no clips exist, a soft keyboard
tap, which fits any voice, is used instead.
"""
import asyncio
import audioop
import logging
import math
import random
import struct
import wave
from pathlib import Path

from .config import DATA_DIR, FILLERS, FILLER_GENERATE, GEMINI_TTS_MODEL, GOOGLE_API_KEY

log = logging.getLogger("fillers")

FILLER_DIR = DATA_DIR / "fillers"
RATE = 16000   # what the phone line takes

# What each clip says. Devanagari so the text-to-speech model says them the Hindi way.
TEXTS = {
    "hmm": "हम्म",
    "achha": "अच्छा",
    "ji": "जी",
    "ek_second": "एक सेकंड",
}

_generating: set[str] = set()


def _dir(voice: str) -> Path:
    return FILLER_DIR / voice


def load(voice: str) -> list[bytes]:
    """The cached clips for a voice as 16 kHz 16-bit mono PCM, or [] when none were made yet."""
    clips = []
    for name in TEXTS:
        path = _dir(voice) / f"{name}.wav"
        if not path.exists():
            continue
        try:
            with wave.open(str(path), "rb") as w:
                if w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2:
                    clips.append(w.readframes(w.getnframes()))
        except Exception as exc:  # noqa: BLE001
            log.warning("filler %s unreadable: %s", path, exc)
    return clips


def tap() -> bytes:
    """A soft keyboard tap, two clicks 120 ms apart, about 0.35 s in all. Voice-neutral fallback."""
    rnd = random.Random(7)   # the same tap every time
    n = int(RATE * 0.35)
    out = [0.0] * n
    for start in (0.0, 0.12):
        s0 = int(start * RATE)
        for i in range(int(RATE * 0.04)):
            out[s0 + i] += (rnd.random() * 2 - 1) * math.exp(-i / (RATE * 0.006))
    peak = max(abs(x) for x in out) or 1.0
    return struct.pack(f"<{n}h", *(int(x / peak * 2500) for x in out))


def _trim(pcm: bytes, threshold: int = 300) -> bytes:
    """Cut leading and trailing silence so the clip starts the instant it is played."""
    frame = 320   # 10 ms
    frames = [pcm[i:i + frame] for i in range(0, len(pcm) - frame + 1, frame)]
    loud = [i for i, f in enumerate(frames) if audioop.rms(f, 2) > threshold]
    if not loud:
        return pcm
    a, b = max(0, loud[0] - 2), min(len(frames), loud[-1] + 8)
    return b"".join(frames[a:b])


def _generate_sync(voice: str) -> int:
    """Make every clip for a voice with Gemini text-to-speech. Returns how many were written.
    This is a paid call (a few paise per voice); only FILLER_GENERATE=true allows it."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GOOGLE_API_KEY)
    _dir(voice).mkdir(parents=True, exist_ok=True)
    made = 0
    for name, text in TEXTS.items():
        path = _dir(voice) / f"{name}.wav"
        if path.exists():
            made += 1
            continue
        resp = client.models.generate_content(
            model=GEMINI_TTS_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
                ),
            ),
        )
        data = b""
        for part in (resp.candidates[0].content.parts or []) if resp.candidates else []:
            if part.inline_data is not None and part.inline_data.data:
                data += part.inline_data.data
        if not data:
            log.warning("filler %s for %s: text-to-speech returned no audio", name, voice)
            continue
        # The model answers with 24 kHz 16-bit mono PCM.
        pcm16k, _ = audioop.ratecv(data, 2, 1, 24000, RATE, None)
        pcm16k = _trim(pcm16k)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm16k)
        made += 1
        log.info("filler %s for %s: %.2f s", name, voice, len(pcm16k) / (RATE * 2))
    return made


async def ensure(voice: str) -> None:
    """Have the clips for this voice ready, generating them once if allowed. Never raises."""
    if not FILLERS or not FILLER_GENERATE or voice in _generating:
        return
    if len(load(voice)) == len(TEXTS):
        return
    _generating.add(voice)
    try:
        made = await asyncio.to_thread(_generate_sync, voice)
        log.info("filler clips for voice %s: %s of %s ready (model %s)", voice, made, len(TEXTS), GEMINI_TTS_MODEL)
    except Exception as exc:  # noqa: BLE001
        log.warning("filler clips for voice %s could not be made (%s); using the keyboard tap instead", voice, exc)
    finally:
        _generating.discard(voice)


class Picker:
    """Hands out one clip at a time for a call, never the same one twice in a row."""

    def __init__(self, voice: str) -> None:
        self.clips = load(voice) if FILLERS else []
        self.tap = tap()
        self.last = -1

    def next(self) -> bytes:
        if not self.clips:
            return self.tap
        i = random.randrange(len(self.clips))
        if len(self.clips) > 1 and i == self.last:
            i = (i + 1) % len(self.clips)
        self.last = i
        return self.clips[i]
