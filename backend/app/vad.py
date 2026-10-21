"""Speech detector for phone audio (Silero VAD, runs locally on the CPU, free).

Loudness alone cannot tell a customer from a noise: a soft voice on speakerphone is quieter than
a door slam. Silero is a small neural network that recognises the pattern of a human voice, so a
quiet "haan ji" scores high and a loud knock scores low. One check takes well under a millisecond.

If onnxruntime or the model file is missing, available() is False and callers fall back to loudness.
"""
import logging
from pathlib import Path

log = logging.getLogger("vad")

MODEL_PATH = Path(__file__).parent / "models" / "silero_vad.onnx"
WINDOW = 512    # samples per check at 16 kHz (32 ms); the model accepts exactly this
CONTEXT = 64    # tail of the previous window, which the model expects in front of each new one

_session = None
_failed = False


def _load():
    global _session, _failed
    if _session is None and not _failed:
        try:
            import numpy  # noqa: F401
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            _session = ort.InferenceSession(str(MODEL_PATH), sess_options=opts, providers=["CPUExecutionProvider"])
            log.info("Silero speech detector loaded")
        except Exception as exc:  # noqa: BLE001
            _failed = True
            log.warning("Silero speech detector unavailable (%s); using loudness only", exc)
    return _session


def available() -> bool:
    return _load() is not None


class SpeechDetector:
    """One per call. feed() takes 16 kHz 16-bit PCM of any length and returns the latest
    probability (0 to 1) that a person is speaking."""

    def __init__(self) -> None:
        import numpy as np
        self.np = np
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, CONTEXT), dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)
        self.sr = np.array(16000, dtype=np.int64)
        self.prob = 0.0

    def feed(self, pcm: bytes) -> float:
        np = self.np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self.pending = np.concatenate([self.pending, samples])
        while len(self.pending) >= WINDOW:
            window, self.pending = self.pending[:WINDOW].reshape(1, -1), self.pending[WINDOW:]
            out, self.state = _session.run(None, {"input": np.concatenate([self.context, window], axis=1),
                                                  "state": self.state, "sr": self.sr})
            self.context = window[:, -CONTEXT:]
            self.prob = float(out[0][0])
        return self.prob
