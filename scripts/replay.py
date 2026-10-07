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
from app import plivo_bridge, vad, voiceprint  # noqa: E402
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
    ap.add_argument("--end-silence", type=float, help="override the pause (seconds) that closes a sentence")
    ap.add_argument("--short-end", type=float, help="override the pause (seconds) that closes a short answer")
    ap.add_argument("--no-bg-gate", action="store_true", help="switch off the background-voice gate, to measure its effect")
    ap.add_argument("--bg", type=float, nargs=2, metavar=("RATIO", "PROB"), help="background gate thresholds to try, e.g. 0.25 0.6")
    ap.add_argument("--no-caller-gate", action="store_true", help="switch off the caller-level gate (CALLER_GATE)")
    ap.add_argument("--gate", type=float, nargs=2, metavar=("LOW", "HIGH"), help="caller gate shares to try, e.g. 0.15 0.40")
    ap.add_argument("--reply-delay", type=float, default=3.0, help="seconds from a turn's end to the agent's first audio")
    ap.add_argument("--summary", action="store_true", help="print one line instead of the full report")
    args = ap.parse_args()
    if not vad.available():
        sys.exit("Silero model not available")

    w = wave.open(args.wav)
    pcm = w.readframes(w.getnframes())
    n = len(pcm) // (FRAME * 2)
    levels = [audioop.rms(pcm[i * FRAME * 2:(i + 1) * FRAME * 2], 2) for i in range(n)]

    # The agent's audio as the echo canceller lined it up on the call (recorded since 6 Oct 2026), if any.
    agent_path = Path(args.wav).with_name(Path(args.wav).stem + "_agent.wav")
    agent = wave.open(str(agent_path)).readframes(n * FRAME) if agent_path.exists() else b""

    voiceprint.available(wait=True)   # the speaker model loads in the background; a replay waits for it

    clock = [1000.0]
    plivo_bridge.time = types.SimpleNamespace(monotonic=lambda: clock[0])
    pc = plivo_bridge.PendingCall({"id": 0}, "Aoede")
    pc.greeted = True
    pc.token = "replay"
    if args.end_silence:
        plivo_bridge.PHONE_END_SILENCE_MS = int(args.end_silence * 1000)
    if args.short_end:
        plivo_bridge.PHONE_SHORT_END_MS = int(args.short_end * 1000)
    pc.long_question = args.long

    if args.no_bg_gate:
        pc.BACKGROUND_GATE = False
    if args.bg:
        pc.BG_RATIO, pc.BG_PROB = args.bg
    if args.no_caller_gate:
        pc.caller_gate = False
    if args.gate:
        plivo_bridge.CALLER_GATE_LOW, plivo_bridge.CALLER_GATE_HIGH = args.gate

    # The agent's side, as on a real call: its audio arrives args.reply_delay after the customer's
    # turn ends (clearing "reply pending") and plays for about 3 s. A turn that starts before then
    # throws that reply away, as Gemini does.
    turns, cur, sent_frames = [], None, set()
    reply_at, killed = None, 0
    for i in range(n):
        clock[0] += 0.02
        if reply_at is not None and clock[0] >= reply_at:
            pc.reply_pending = False
            pc.lost_replies = 0
            pc.play_until = clock[0] + 3.0
            reply_at = None
        if agent and pc.cleaner is not None:
            pc.cleaner.played(agent[i * FRAME * 2:(i + 1) * FRAME * 2], clock[0] - 0.02)
        for item in pc.listen(pcm[i * FRAME * 2:(i + 1) * FRAME * 2]):
            if item is ACTIVITY_START:
                cur = {"start": i, "voiced": 0}
                if reply_at is not None:
                    killed += 1
                    reply_at = None
            elif item is ACTIVITY_END:
                cur["end"] = i
                cur["pause"] = clock[0] - pc.last_voiced   # quiet waited before closing the turn
                turns.append(cur)
                cur = None
                reply_at = clock[0] + args.reply_delay
            elif isinstance(item, Nudge):
                turns.append({"nudge": str(item)[:60], "at": i})
            elif cur is not None:
                sent_frames.add(i)
                if pc.levels and pc.last_loud:
                    cur["voiced"] += 1

    present = bursts(levels)
    if args.summary:
        got = sum(1 for a, b in present for i in range(a, b + 1) if i in sent_frames)
        tot = sum(b - a + 1 for a, b in present)
        pauses = [t["pause"] for t in turns if "pause" in t]
        print(f"{Path(args.wav).stem:10s} caller level {pc.caller_level:6.0f}  turns {sum(1 for t in turns if 'start' in t):3d}  "
              f"replies thrown away {killed:2d}  background ignored {pc.stats.get('background', 0):3d}  "
              f"sound captured {100 * got / max(1, tot):3.0f}%  "
              f"avg close {1000 * sum(pauses) / max(1, len(pauses)):4.0f} ms")
        return
    print(f"caller level: {pc.caller_level:.0f}; replies thrown away: {killed}; "
          f"background voices ignored: {pc.stats.get('background', 0)}")
    print(f"{n * 0.02:.0f}s of audio; gated line: {'yes' if pc.gated_line else 'no'}; "
          f"pause that closes a turn: short answer {plivo_bridge.PHONE_SHORT_END_MS} ms, sentence "
          f"{plivo_bridge.PHONE_END_SILENCE_MS} ms, long answer {plivo_bridge.PHONE_LONG_ANSWER_MS} ms"
          f"{' (in use)' if pc.long_question else ''}\n")
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
