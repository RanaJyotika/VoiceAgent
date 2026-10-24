"""A phone call's audio as one conversation: the customer's and the agent's sides mixed, and as an MP3 for keeping.

The phone line writes each side to its own file, sample for sample (plivo_bridge: call_<id>.wav and, with echo
cancellation on, call_<id>_agent.wav). Owner, 24 Oct 2026: hear both in one file, like the call itself, and keep a
copy on Google Drive (drive_sync.py).
"""
import logging
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("recordings")

TARGET_LEVEL = 4000.0   # both sides to this speaking loudness (16-bit samples)
MAX_GAIN = 16.0         # a side that is mostly line noise is not blown up past this
MP3_KBPS = 32           # mono speech: clear, about a tenth of the wav's size


def _level(v: np.ndarray) -> float:
    """How loud a side is while someone speaks: the middle loudness of its 20 ms frames with speech in them."""
    frames = v[: len(v) // 320 * 320].reshape(-1, 320).astype(np.float64)
    if not len(frames):
        return 0.0
    rms = np.sqrt((frames ** 2).mean(axis=1))
    loud = rms[rms > max(rms.max() * 0.1, 50)]
    return float(np.median(loud)) if len(loud) else 0.0


def both_sides(customer: Path) -> Path | None:
    """The customer's and the agent's audio mixed into one wav, made the first time it is asked for and kept as
    call_<id>_both.wav. Each side is brought to the same speaking level first: the customer's was 1x to 13x quieter
    than the agent's (calls 5306, 5347). None when there is no agent side to mix in."""
    agent = customer.with_name(customer.stem + "_agent.wav")
    both = customer.with_name(customer.stem + "_both.wav")
    if both.is_file() and both.stat().st_mtime >= customer.stat().st_mtime:
        return both
    if not agent.is_file():
        return None
    try:
        with wave.open(str(customer)) as c, wave.open(str(agent)) as a:
            rate = c.getframerate()
            if (c.getsampwidth(), c.getnchannels(), a.getsampwidth(), a.getnchannels(), a.getframerate()) != (2, 1, 2, 1, rate):
                return None
            x = np.frombuffer(c.readframes(c.getnframes()), dtype=np.int16)
            y = np.frombuffer(a.readframes(a.getnframes()), dtype=np.int16)
        mix = np.zeros(max(len(x), len(y)), dtype=np.float64)
        for side in (x, y):
            if (lv := _level(side)) > 0:
                mix[:len(side)] += side * min(TARGET_LEVEL / lv, MAX_GAIN)
        tmp = both.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(np.clip(mix, -32768, 32767).astype(np.int16).tobytes())
        tmp.replace(both)
        return both
    except Exception:  # noqa: BLE001
        log.exception("could not mix the two sides of %s", customer.name)
        return None


def conversation(customer: Path) -> Path:
    """The file that holds the whole conversation: both sides mixed when the agent's side exists, else the customer's."""
    return both_sides(customer) or customer


def conversation_mp3(customer: Path) -> Path | None:
    """The whole conversation as an MP3 file next to the recordings (call_<id>_both.mp3, or call_<id>.mp3 with only
    the customer's side), made once and kept, so a day's download or the Drive copy does not encode it again."""
    audio = conversation(customer)
    out = audio.with_suffix(".mp3")
    if out.is_file() and out.stat().st_mtime >= audio.stat().st_mtime:
        return out
    data = mp3(audio)
    if data is None:
        return None
    tmp = out.with_suffix(".mp3.part")
    tmp.write_bytes(data)
    tmp.replace(out)
    return out


def mp3(path: Path) -> bytes | None:
    """A mono 16-bit wav as MP3 bytes; None when the encoder (lameenc) is missing or the file is not that kind."""
    try:
        import lameenc   # here: only the Drive copy needs it
    except ImportError:
        return None
    try:
        with wave.open(str(path)) as w:
            if (w.getsampwidth(), w.getnchannels()) != (2, 1):
                return None
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        enc = lameenc.Encoder()
        enc.set_bit_rate(MP3_KBPS)
        enc.set_in_sample_rate(rate)
        enc.set_channels(1)
        enc.set_quality(5)
        return bytes(enc.encode(pcm) + enc.flush())
    except Exception:  # noqa: BLE001
        log.exception("could not make an MP3 of %s", path.name)
        return None
