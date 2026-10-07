"""Noise suppression and echo cancellation for the customer's side of a phone call.

Google's WebRTC Audio Processing Module (the one in Chrome and Meet), through LiveKit's Python
wrapper (`livekit.rtc.AudioProcessingModule`). Chosen over RNNoise and DeepFilterNet on 6 Oct 2026:
it works at our 16 kHz with no resampling, costs about 44 us per 10 ms frame, and one module does
both jobs (owner's decision).

Echo cancellation needs the agent's audio as the phone plays it (the "reverse stream"). The phone
bridge reports each piece of agent audio with the moment it starts playing (`played`), and every
10 ms of customer audio is processed together with the 10 ms of agent audio playing at that moment,
or silence. What is left between the two is the network's round trip, which the canceller finds
itself.

Anything that goes wrong here must never cost a call: on an error the customer's audio passes
through untouched and the cleaner switches itself off.
"""
import logging

log = logging.getLogger("plivo")

FRAME = 320                  # bytes in 10 ms of 16 kHz 16-bit mono: the only size the module takes
SILENCE = b"\x00" * FRAME
BYTES_PER_SECOND = 32000

try:
    from livekit import rtc
except Exception as exc:  # noqa: BLE001
    rtc = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def available() -> bool:
    return rtc is not None


class LineCleaner:
    """One per call. capture() cleans a frame of the customer's audio; played() and clear_played()
    keep the agent's audio that the echo canceller compares it with."""

    def __init__(self, noise: bool, echo: bool) -> None:
        self.echo = echo
        self.apm = rtc.AudioProcessingModule(noise_suppression=noise, echo_cancellation=echo, high_pass_filter=True)
        self._render = bytearray()   # agent audio still to play, in order
        self._render_at = 0.0        # when its first byte plays
        self.last_render = b""       # the agent audio processed with the last captured frame (for the recording)
        self.failed = False

    def played(self, chunk: bytes, start: float) -> None:
        """Agent audio that starts playing on the phone at `start` (time.monotonic())."""
        if not self.echo or self.failed or not chunk:
            return
        if not self._render:
            self._render_at = start
        else:
            gap = start - (self._render_at + len(self._render) / BYTES_PER_SECOND)
            if gap > 0.002:   # a pause between two replies: the phone plays silence there
                self._render += b"\x00" * (int(gap * BYTES_PER_SECOND) & ~1)
        self._render += chunk

    def clear_played(self) -> None:
        """The agent's audio was cut (a barge-in): what was queued will never play."""
        self._render.clear()

    def _render_at_time(self, t: float) -> bytes:
        """The 10 ms of agent audio playing at moment t, or silence."""
        if not self._render or self._render_at > t + 0.005:
            return SILENCE
        behind = int((t - self._render_at) * BYTES_PER_SECOND) & ~1
        if behind >= FRAME:   # this side fell behind (a busy moment): skip what has already played
            del self._render[:behind]
            self._render_at += behind / BYTES_PER_SECOND
        out = bytes(self._render[:FRAME])
        del self._render[:FRAME]
        self._render_at += FRAME / BYTES_PER_SECOND
        return out.ljust(FRAME, b"\x00")

    def capture(self, pcm: bytes, now: float) -> bytes:
        """One frame of the customer's audio (any whole number of 10 ms pieces, normally 20 ms) that
        arrived at `now`. Returns it cleaned, or unchanged if the module has failed."""
        if self.failed or len(pcm) % FRAME:
            return pcm
        n = len(pcm) // FRAME
        out, render = bytearray(), bytearray()
        try:
            for i in range(n):
                if self.echo:
                    r = self._render_at_time(now - (n - i) / 100)
                    render += r
                    self.apm.process_reverse_stream(rtc.AudioFrame(r, 16000, 1, FRAME // 2))
                frame = rtc.AudioFrame(pcm[i * FRAME:(i + 1) * FRAME], 16000, 1, FRAME // 2)
                self.apm.process_stream(frame)
                out += frame.data.cast("B")
        except Exception as exc:  # noqa: BLE001
            self.failed = True
            log.warning("audio clean-up failed (%s); the customer's audio goes through untouched from now on", exc)
            return pcm
        self.last_render = bytes(render)
        return bytes(out)


def make_cleaner(mode: str, noise: bool, echo: bool) -> LineCleaner | None:
    """A cleaner for one call, or None when clean-up is off or the module cannot be loaded."""
    if mode == "off" or not (noise or echo):
        return None
    if rtc is None:
        log.warning("audio clean-up is on but livekit is not installed (%s); running without it", _IMPORT_ERROR)
        return None
    try:
        return LineCleaner(noise=noise, echo=echo)
    except Exception as exc:  # noqa: BLE001
        log.warning("audio clean-up could not start (%s); running without it", exc)
        return None
