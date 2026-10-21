"""Voice fingerprint of the caller: is this voice the customer, or someone else near the phone?

The loudness gate (plivo_bridge.caller_band) cannot tell two equally loud voices apart, and throws away a
customer who moves away from the phone. A speaker model can: NeMo TitaNet-small (trained partly on telephone
speech), run on ONNX through sherpa-onnx, about 11 ms per fingerprint.

Chosen on 6 Oct 2026 from five models on our own recordings: same person across calls against 13 other
callers. With a profile from the caller's first clean utterances (owner's plan), a 0.6 s clip of another
person scored under 0.10 in 39% of cases and the caller's own clips never did; under 0.15 caught 61% and
wrongly flagged 1 of 46 caller clips. So it only overrules the loudness gate when clearly sure.

Enrollment (owner's plan, 6 Oct 2026): candidate fingerprints from clean utterances of the caller; ones that
disagree with the rest are dropped; the rest are averaged, weighted by length, into the profile. Later long,
confident matches update it a little, and the first profile stays as an anchor so it cannot drift away.
"""
import logging
import threading
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger("plivo")

MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_PATH = Path(__file__).parent / "models" / MODEL_NAME
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_NAME}"

BYTES_PER_SECOND = 32000       # 16 kHz 16-bit mono
CHECK_SECONDS = 0.6            # the least voice a check is made on (tested at 0.6 s)
ENROLL_CLIP_SECONDS = 0.8      # the least voice in one utterance for it to join the profile
ENROLL_SECONDS = 3.0           # voice the profile needs in total...
ENROLL_CLIPS = 2               # ...from at least this many utterances
AGREE = 0.25                   # an enrollment clip whose average similarity to the others is below this is dropped
UPDATE_SECONDS = 1.5           # a later utterance this long that clearly matches nudges the profile
UPDATE_WEIGHT = 0.15
SHORT_SECONDS = 1.0            # under this much voice a fingerprint is shakier...
SHORT_MARGIN = 0.04            # ...so "clearly someone else" needs this much lower a score (replayed call 104: two
                               # 0.7 s turns at 0.09 and 0.12 that may well have been the caller's "ok")
MAX_SECONDS = 4.0              # fingerprints use at most the last 4 s of an utterance

_extractor = None
_lock = threading.Lock()
_loading = False
_thread: threading.Thread | None = None


def _load() -> None:
    global _extractor, _loading
    try:
        if not MODEL_PATH.exists():
            log.info("voice fingerprint: downloading the speaker model (%s, about 40 MB)", MODEL_NAME)
            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = MODEL_PATH.with_suffix(".part")
            urllib.request.urlretrieve(MODEL_URL, tmp)
            tmp.replace(MODEL_PATH)
        import sherpa_onnx
        ext = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(MODEL_PATH), num_threads=1, provider="cpu"))
        with _lock:
            _extractor = ext
        log.info("voice fingerprint: speaker model loaded (%s)", MODEL_NAME)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice fingerprint: could not load the speaker model (%s); running without it", exc)
    finally:
        _loading = False


def available(wait: bool = False) -> bool:
    """True once the model is loaded. The first call starts loading it in the background (and downloads it
    if missing), so a call placed meanwhile simply runs without fingerprints."""
    global _loading, _thread
    if _extractor is not None:
        return True
    with _lock:
        if not _loading and _extractor is None:
            _loading = True
            _thread = threading.Thread(target=_load, name="voiceprint-load", daemon=True)
            _thread.start()
    if wait and _thread is not None:
        _thread.join()
    return _extractor is not None


def fingerprint(pcm: bytes) -> np.ndarray | None:
    """The voice fingerprint of 16 kHz 16-bit mono speech, unit length, or None."""
    if _extractor is None or len(pcm) < int(CHECK_SECONDS * BYTES_PER_SECOND):
        return None
    pcm = pcm[-int(MAX_SECONDS * BYTES_PER_SECOND):]
    try:
        st = _extractor.create_stream()
        st.accept_waveform(sample_rate=16000, waveform=np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0)
        st.input_finished()
        e = np.asarray(_extractor.compute(st), dtype=np.float32)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice fingerprint failed: %s", exc)
        return None
    n = float(np.linalg.norm(e))
    return e / n if n else None


class CallerVoice:
    """One per call: builds the caller's profile and says how alike a new voice is."""

    def __init__(self, other: float, caller: float) -> None:
        self.other, self.caller = other, caller   # below `other`: clearly someone else; above `caller`: clearly them
        self.candidates: list[tuple[np.ndarray, float]] = []   # (fingerprint, seconds) while enrolling
        self.profile: np.ndarray | None = None
        self.anchor: np.ndarray | None = None
        self.other_turns = 0          # long "someone else" turns in a row (two means the phone was handed over)
        self.stats = {"checked": 0, "other": 0, "rescued": 0, "handovers": 0}

    @property
    def ready(self) -> bool:
        return self.profile is not None

    def similarity(self, pcm: bytes) -> float | None:
        """How alike this voice is to the caller's (-1..1), or None if not ready or too little voice."""
        return self._compare(pcm)[0]

    def _compare(self, pcm: bytes) -> tuple[float | None, np.ndarray | None]:
        if self.profile is None:
            return None, None
        e = fingerprint(pcm)
        if e is None:
            return None, None
        self.stats["checked"] += 1
        return max(float(self.profile @ e), float(self.anchor @ e)), e

    def verdict(self, sim: float | None, secs: float = 0.0) -> str:
        if sim is None:
            return "unknown"
        other = self.other - (SHORT_MARGIN if secs < SHORT_SECONDS else 0.0)
        return "other" if sim < other else "caller" if sim >= self.caller else "unsure"

    def turn(self, pcm: bytes, clean: bool) -> tuple[float | None, str]:
        """A finished turn's voice. `clean` = fit to build the profile from (not over the agent, at the
        caller's loudness). Returns (similarity, what it means for this turn)."""
        secs = len(pcm) / BYTES_PER_SECOND
        if self.profile is None:
            if clean and secs >= ENROLL_CLIP_SECONDS:
                return None, self._enroll(pcm, secs)
            return None, "too short to learn the caller's voice from" if clean else "not used to learn the caller's voice"
        sim, e = self._compare(pcm)
        v = self.verdict(sim, secs)
        if v == "other" and secs >= UPDATE_SECONDS:
            self.other_turns += 1
            if self.other_turns >= 2:
                # Two long turns in a row from another voice: most likely the phone was handed over
                # ("mere husband se baat kar lijiye"). Learn the new voice instead of fighting it.
                self.stats["handovers"] += 1
                self.profile = self.anchor = None
                self.candidates, self.other_turns = [], 0
                self._enroll(pcm, secs)
                return sim, "handover"
        elif v != "other":
            self.other_turns = 0
        if v == "caller" and clean and secs >= UPDATE_SECONDS and e is not None:
            p = (1 - UPDATE_WEIGHT) * self.profile + UPDATE_WEIGHT * e
            self.profile = p / np.linalg.norm(p)
        if v == "other":
            self.stats["other"] += 1
        return sim, v

    def _enroll(self, pcm: bytes, secs: float) -> str:
        e = fingerprint(pcm)
        if e is None:
            return "enrolling: no fingerprint"
        self.candidates.append((e, min(secs, MAX_SECONDS)))
        keep = self.candidates
        if len(keep) >= 3:
            # Drop any utterance that disagrees with the rest (the first "haan ji" may have been someone else).
            keep = [c for i, c in enumerate(keep)
                    if np.mean([float(c[0] @ o[0]) for j, o in enumerate(keep) if j != i]) >= AGREE]
        elif len(keep) == 2 and float(keep[0][0] @ keep[1][0]) < AGREE:
            return f"enrolling: 2 utterances that disagree ({float(keep[0][0] @ keep[1][0]):.2f}); waiting for a third"
        total = sum(s for _, s in keep)
        if len(keep) < ENROLL_CLIPS or total < ENROLL_SECONDS:
            return f"enrolling: {len(keep)} utterance(s), {total:.1f} s of voice"
        p = sum(f * s for f, s in keep)
        self.profile = self.anchor = p / np.linalg.norm(p)
        dropped = len(self.candidates) - len(keep)
        self.candidates = []
        return f"profile ready from {len(keep)} utterances ({total:.1f} s){f', {dropped} dropped as unlike the rest' if dropped else ''}"
