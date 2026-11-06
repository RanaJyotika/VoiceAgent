"""Phone keypad tones (DTMF) in the customer's audio.

A key press is two pure tones at once, one from each group below. Plivo sends key presses as their own "dtmf" message,
but the tone may also come through in the audio: there it must not count as the customer talking (it would cut the
agent off and send a beep to speech-to-text), and if Plivo's message never comes, the digit is read from the tone here.

Each 20 ms frame is checked at the eight key frequencies only (Goertzel), as one small matrix product.
"""
import numpy as np

RATE = 16000
ROWS = (697, 770, 852, 941)
COLS = (1209, 1336, 1477, 1633)
KEYS = ("123A", "456B", "789C", "*0#D")

MIN_LEVEL = 150        # RMS; quieter than this is not a key press
# Checked on 14,000 recorded calls (2 Nov 2026, ~100 hours, no keypad in use): 0.7 and 40 ms read 66 digits out of
# speech and noise; 0.8 and 60 ms read 1 (a "#" that looks pressed). Test tones through the phone codec: 20 of 20.
MIN_SHARE = 0.8        # the two tones carry at least this share of the frame's energy (speech spreads it widely)
MIN_FRAMES = 3         # 60 ms of the same key before a digit is read (a key press lasts 70 ms or more)

_basis: dict[int, np.ndarray] = {}


def _waves(n: int) -> np.ndarray:
    """cos and sin rows at the eight frequencies for a frame of n samples."""
    if n not in _basis:
        t = np.arange(n) / RATE
        f = np.array(ROWS + COLS, dtype=np.float64)[:, None]
        _basis[n] = np.vstack([np.cos(2 * np.pi * f * t), np.sin(2 * np.pi * f * t)])
    return _basis[n]


def key_in(pcm: bytes) -> str:
    """The key whose tone fills this frame of 16 kHz 16-bit audio, or ""."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    n = len(x)
    if n < 160:
        return ""
    energy = float(np.dot(x, x))
    if energy < MIN_LEVEL * MIN_LEVEL * n:
        return ""
    proj = _waves(n) @ x
    power = (proj[:8] ** 2 + proj[8:] ** 2) * 2 / n   # each tone's energy in the frame
    r, c = int(np.argmax(power[:4])), int(np.argmax(power[4:]))
    row, col = power[r], power[4 + c]
    if row + col < MIN_SHARE * energy:
        return ""
    if min(row, col) < 0.15 * max(row, col):   # one tone far louder than the other: a whistle, not a key
        return ""
    return KEYS[r][c]


class KeyListener:
    """Follows the tones frame by frame: is_tone says a frame is a key tone, not a voice; feed returns a digit once per
    press, after MIN_FRAMES of it."""

    def __init__(self) -> None:
        self.current = ""
        self.frames = 0
        self.reported = False
        self.is_tone = False

    def feed(self, pcm: bytes) -> str:
        key = key_in(pcm)
        self.is_tone = bool(key)
        if key != self.current:
            self.current, self.frames, self.reported = key, 0, False
        if not key:
            return ""
        self.frames += 1
        if self.frames >= MIN_FRAMES and not self.reported:
            self.reported = True
            return key
        return ""
