"""Make the filler clips ("hmm", "achha", "ji", "ek second") once, for the voices in use.

PAID: Gemini text-to-speech for Gemini voices and OpenAI text-to-speech for ChatGPT voices, a few paise per
voice. Only clips that do not exist yet are made, so running it again costs nothing. Nothing makes clips
during a call (owner, 6 Oct 2026: "only once").

Usage: .venv\\Scripts\\python.exe scripts\\make_fillers.py [voice ...]
       (default: Aoede Orus for Gemini and marin cedar coral for ChatGPT)
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import fillers  # noqa: E402
from app.config import OPENAI_VOICES, VOICES  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
voices = sys.argv[1:] or ["Aoede", "Orus", "marin", "cedar", "coral"]
for v in voices:
    try:
        if v in OPENAI_VOICES:
            made = fillers.make_openai(v, female=OPENAI_VOICES[v] == "female")
            folder = fillers.openai_key(v)
        elif v in VOICES:
            made = fillers.make_gemini(v, female=VOICES[v] != "male")
            folder = v
        else:
            print(f"{v}: not a known Gemini or OpenAI voice, skipped")
            continue
    except Exception as exc:  # noqa: BLE001
        print(f"{v}: failed ({exc})")
        continue
    have = sorted(fillers.load(folder))
    print(f"{v}: made {made or 'nothing new'}; clips now: {have}")
