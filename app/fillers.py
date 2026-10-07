"""Filler sounds that cover the wait for the agent's reply.

Gemini takes about 3 s from the customer's last word to its first audio, ChatGPT 1.3-2 s. A short
"hmm" / "achha" / "ji" in the agent's own voice breaks that silence.

On call 72 (25 Sep 2026) the owner found a "hmm" after every reply unnecessary, and it also played after
sounds that were not an answer, so fillers were switched off. Back on 6 Oct 2026 (owner), played only when
the reply is late, only after a real answer, and at most every other reply (plivo_bridge.plan_filler).

The clips are made ONCE by scripts/make_fillers.py (owner: "only once"), never during a call, and kept in
data/fillers/<voice>/: Gemini voices with Gemini text-to-speech (the same prebuilt voices as the Live
model), ChatGPT voices with OpenAI text-to-speech in data/fillers/openai-<voice>/, so the filler is always
in the voice the agent is speaking in. A voice with no clips simply gets no filler.
"""
import audioop
import logging
import random
import wave
from pathlib import Path

from .config import DATA_DIR, FILLER_WORDS, GEMINI_TTS_MODEL, GOOGLE_API_KEY, OPENAI_API_KEY

log = logging.getLogger("fillers")

FILLER_DIR = DATA_DIR / "fillers"
RATE = 16000   # what the phone line takes
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"

# What each clip says. Devanagari so the text-to-speech model says them the Hindi way.
TEXTS = {
    "hmm": "हम्म",
    "achha": "अच्छा",
    "ji": "जी",
    "ek_second": "एक सेकंड",
}


def openai_key(voice: str) -> str:
    """The folder name for an OpenAI voice's clips."""
    return f"openai-{voice}"


def _dir(voice: str) -> Path:
    return FILLER_DIR / voice


def load(voice: str) -> dict[str, bytes]:
    """The clips made for a voice (folder name) as 16 kHz 16-bit mono PCM, by name; {} when none were made."""
    clips = {}
    for name in TEXTS:
        path = _dir(voice) / f"{name}.wav"
        if not path.exists():
            continue
        try:
            with wave.open(str(path), "rb") as w:
                if w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2:
                    clips[name] = w.readframes(w.getnframes())
        except Exception as exc:  # noqa: BLE001
            log.warning("filler %s unreadable: %s", path, exc)
    return clips


def _trim(pcm: bytes, threshold: int = 300) -> bytes:
    """Cut leading and trailing silence so the clip starts the instant it is played."""
    frame = 320   # 10 ms
    frames = [pcm[i:i + frame] for i in range(0, len(pcm) - frame + 1, frame)]
    loud = [i for i, f in enumerate(frames) if audioop.rms(f, 2) > threshold]
    if not loud:
        return pcm
    a, b = max(0, loud[0] - 2), min(len(frames), loud[-1] + 8)
    return b"".join(frames[a:b])


def _save(folder: str, name: str, pcm24k: bytes) -> float:
    pcm16k, _ = audioop.ratecv(pcm24k, 2, 1, 24000, RATE, None)   # both TTS services answer at 24 kHz
    pcm16k = _trim(pcm16k)
    _dir(folder).mkdir(parents=True, exist_ok=True)
    with wave.open(str(_dir(folder) / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm16k)
    return len(pcm16k) / (RATE * 2)


def make_gemini(voice: str, female: bool) -> list[str]:
    """PAID (a few paise): the missing clips for a Gemini voice, with Gemini text-to-speech. A bare word is refused
    ("Model tried to generate text"), so it goes inside a plain instruction to say it."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GOOGLE_API_KEY)
    done = []
    for name, text in TEXTS.items():
        if (_dir(voice) / f"{name}.wav").exists():
            continue
        resp = client.models.generate_content(
            model=GEMINI_TTS_MODEL,
            contents=(f"Say softly, like a {'woman' if female else 'man'} from Delhi thinking for a moment on a phone "
                      f"call: {text}"),
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
        content = resp.candidates[0].content if resp.candidates else None   # sometimes empty: the preview model
        data = b"".join(p.inline_data.data for p in ((content.parts or []) if content else [])
                        if p.inline_data is not None and p.inline_data.data)
        if data:
            done.append(f"{name} {_save(voice, name, data):.2f}s")
        else:
            log.warning("filler %s for %s: text-to-speech returned no audio", name, voice)
    return done


def make_openai(voice: str, female: bool) -> list[str]:
    """PAID (a few paise): the missing clips for an OpenAI voice, with OpenAI text-to-speech."""
    import httpx
    done = []
    how = (f"A {'woman' if female else 'man'} from Delhi on a phone call, saying this one short Hindi word softly, "
           "the way people do while they think before answering. North Indian Hindi pronunciation.")
    for name, text in TEXTS.items():
        if (_dir(openai_key(voice)) / f"{name}.wav").exists():
            continue
        if name == "hmm":
            # Owner, 7 Oct 2026: OpenAI read "हम्म" as a "Hindi word", i.e. "हम" (we). A hum is no word.
            text, how_ = "Hmmm.", (f"A {'woman' if female else 'man'} on a phone call making a soft, short "
                                   "closed-mouth hum while thinking, lips closed. Not a word: no vowel, no 'hum'.")
        else:
            how_ = how
        r = httpx.post("https://api.openai.com/v1/audio/speech", timeout=60,
                       headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                       json={"model": OPENAI_TTS_MODEL, "voice": voice, "input": text, "instructions": how_,
                             "response_format": "pcm"})
        if r.status_code != 200:
            log.warning("filler %s for %s: OpenAI text-to-speech said %s %s", name, voice, r.status_code, r.text[:200])
            continue
        done.append(f"{name} {_save(openai_key(voice), name, r.content):.2f}s")
    return done


# A filler sits just under the agent's own speech (about 1500-3000 on the line): the clips came out between
# 1125 and 9246, and Aoede's "hmm" would have been a jolt.
SOFT_LEVEL = 1400
# Small speed changes of a clip (pitch moves with it), so one "hmm" does not sound the same every time.
# Made here for free instead of more paid clips (owner: clips made only once).
VARIANTS = {"": 1.0, " higher": 1.05, " lower": 0.95}


def _level(pcm: bytes) -> bytes:
    rms = audioop.rms(pcm, 2)
    return audioop.mul(pcm, 2, min(4.0, SOFT_LEVEL / rms)) if rms else pcm


def _speed(pcm: bytes, factor: float) -> bytes:
    """Played at the same rate, fewer samples sound faster and higher; more sound slower and lower."""
    if factor == 1.0:
        return pcm
    out, _ = audioop.ratecv(pcm, 2, 1, RATE, int(RATE / factor), None)
    return out


class Picker:
    """Hands out one clip at a time for a call, never the same one twice in a row. Only the words in
    FILLER_WORDS: by default just "hmm", which carries no accent (owner, 6 Oct 2026: the ChatGPT voices say
    words like "achha" with a southern sound)."""

    def __init__(self, folder: str) -> None:
        made = {n: c for n, c in load(folder).items() if n in FILLER_WORDS}
        self.clips = {f"{n}{tag}": _speed(_level(c), f) for n, c in made.items() for tag, f in VARIANTS.items()}
        self.names = list(self.clips)
        self.last = ""

    def next(self) -> tuple[str, bytes]:
        choices = [n for n in self.names if n != self.last] or self.names
        self.last = random.choice(choices)
        return self.last, self.clips[self.last]

