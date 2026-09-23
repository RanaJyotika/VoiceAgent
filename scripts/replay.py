"""Replay a recorded call through the phone-line speech detector, with no API calls.

    .venv\\Scripts\\python.exe scripts\\replay.py data\\recordings\\call_53.wav
    .venv\\Scripts\\python.exe scripts\\replay.py data\\recordings\\call_53.wav --long   (as after "address batayein?")

For every burst of sound in the recording it prints how much of it the detector took as speech,
then the turns it would have sent to Gemini. "captured" close to "present" is the goal; many short
turns where one long answer was spoken is the failure mode of call 53.
"""
import argparse
import audioop
import sys
import types
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import plivo_bridge, vad  # noqa: E402
from app.live_bridge import ACTIVITY_END, ACTIVITY_START, Nudge  # noqa: E402

FRAME = 320  # samples, 20 ms at 16 kHz


def bursts(levels: list[int]) -> list[tuple[int, int]]:
    """Stretches where the line carries sound above the room: (first frame, last frame)."""
    floor = sorted(levels)[len(levels) // 5]
    thr = max(150, floor * 3)
    out, start, last = [], None, None
    for i, lv in enumerate(levels):
        if lv >= thr:
            if start is None:
                start = i
            last = i
        elif start is not None and i - last > 30:      # a gap over 0.6 s ends the burst
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    return [(a, b) for a, b in out if b - a >= 10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--long", action="store_true", help="use the long-answer pause, as after an address question")
    ap.add_argument("--end-silence", type=float, help="override the pause (seconds) that closes a turn")
    ap.add_argument("--no-bg-gate", action="store_true", help="switch off the background-voice gate, to measure its effect")
    ap.add_argument("--bg", type=float, nargs=2, metavar=("RATIO", "PROB"), help="background gate thresholds to try, e.g. 0.25 0.6")
    args = ap.parse_args()
    if not vad.available():
        sys.exit("Silero model not available")

    w = wave.open(args.wav)
    pcm = w.readframes(w.getnframes())
    n = len(pcm) // (FRAME * 2)
    levels = [audioop.rms(pcm[i * FRAME * 2:(i + 1) * FRAME * 2], 2) for i in range(n)]

    clock = [1000.0]
    plivo_bridge.time = types.SimpleNamespace(monotonic=lambda: clock[0])
    pc = plivo_bridge.PendingCall({"id": 0}, "Aoede")
    pc.greeted = True
    pc.token = "replay"
    if args.end_silence:
        pc.end_silence = args.end_silence
    elif args.long:
        pc.end_silence = plivo_bridge.PHONE_LONG_ANSWER_MS / 1000

    if args.no_bg_gate:
        pc.BACKGROUND_GATE = False
    if args.bg:
        pc.BG_RATIO, pc.BG_PROB = args.bg

    # The agent's side, as on a real call: its audio arrives about 1.5 s after the customer's
    # turn ends (clearing "reply pending") and plays for about 3 s.
    turns, cur, sent_frames = [], None, set()
    reply_at = None
    for i in range(n):
        clock[0] += 0.02
        if reply_at is not None and clock[0] >= reply_at:
            pc.reply_pending = False
            pc.play_until = clock[0] + 3.0
            reply_at = None
        for item in pc.listen(pcm[i * FRAME * 2:(i + 1) * FRAME * 2]):
            if item is ACTIVITY_START:
                cur = {"start": i, "voiced": 0}
            elif item is ACTIVITY_END:
                cur["end"] = i
                turns.append(cur)
                cur = None
                reply_at = clock[0] + 1.5
            elif isinstance(item, Nudge):
                turns.append({"nudge": str(item)[:60], "at": i})
            elif cur is not None:
                sent_frames.add(i)
                if pc.levels and pc.last_loud:
                    cur["voiced"] += 1

    present = bursts(levels)
    print(f"{n * 0.02:.0f}s of audio; gated line: {'yes' if pc.gated_line else 'no'}; "
          f"pause that closes a turn: {pc.end_silence:.1f}s (+0.6 on a gated line)\n")
    print("Bursts of sound present -> frames the detector sent to Gemini:")
    total_present = total_sent = 0
    for a, b in present:
        got = sum(1 for i in range(a, b + 1) if i in sent_frames)
        total_present += b - a + 1
        total_sent += got
        mark = "" if got >= 0.8 * (b - a + 1) else "   <-- mostly lost"
        print(f"  {a * 0.02:6.1f}s  {(b - a + 1) * 0.02:4.1f}s present, {got * 0.02:4.1f}s sent{mark}")
    print(f"\ncaptured {total_sent * 0.02:.1f}s of {total_present * 0.02:.1f}s present "
          f"({100 * total_sent / max(1, total_present):.0f}%)\n")
    print(f"Turns sent to Gemini: {sum(1 for t in turns if 'start' in t)}")
    for t in turns:
        if "nudge" in t:
            print(f"  {t['at'] * 0.02:6.1f}s  nudge: {t['nudge']}")
        else:
            print(f"  {t['start'] * 0.02:6.1f}s -> {t['end'] * 0.02:6.1f}s  ({(t['end'] - t['start']) * 0.02:.1f}s)")


if __name__ == "__main__":
    main()
