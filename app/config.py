"""Environment and settings. Loaded once at import."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GOOGLE_API_KEY = os.getenv("GOOGLE_GEMINI_LIVE_API") or os.getenv("GOOGLE_API_KEY") or ""
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "")

GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Aoede")
AGENT_NAME = os.getenv("AGENT_NAME", "Gauri")            # used with female voices
AGENT_NAME_MALE = os.getenv("AGENT_NAME_MALE", "Arjun")  # used with male voices
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Vedic Vaibhav")
AFFECTIVE_DIALOG = os.getenv("AFFECTIVE_DIALOG", "true").lower() == "true"
# 0 = the model answers immediately (fast turns). -1 = leave the model default (slower, ~3 s pauses).
GEMINI_THINKING_BUDGET = int(os.getenv("GEMINI_THINKING_BUDGET", "0"))

# Voices verified on this key. Gender decides the agent's name and Hindi verb forms.
VOICES = {
    "Aoede": "female", "Kore": "female", "Leda": "female", "Zephyr": "female",
    "Callirrhoe": "female", "Autonoe": "female", "Despina": "female", "Sulafat": "female",
    "Achernar": "female", "Vindemiatrix": "female",
    "Puck": "male", "Charon": "male", "Fenrir": "male", "Orus": "male",
    "Enceladus": "male", "Iapetus": "male",
}

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "voice_agent.db"
UPLOAD_DIR = DATA_DIR / "uploads"

# Audio formats used by the Gemini Live API
INPUT_SAMPLE_RATE = 16000   # what we send to Gemini (16-bit PCM mono)
OUTPUT_SAMPLE_RATE = 24000  # what Gemini sends back (16-bit PCM mono)

if not GOOGLE_API_KEY:
    raise RuntimeError("GOOGLE_GEMINI_LIVE_API is not set in .env")

# --- Plivo (phone line). All optional until you are ready for real calls. ---
PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID", "")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN", "")
PLIVO_FROM_NUMBER = os.getenv("PLIVO_FROM_NUMBER", "")      # your Plivo number, e.g. +9180xxxxxxx
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")  # https://xxxx.trycloudflare.com
# Optional safety switch: when true, every phone call goes to PHONE_NUMBER from .env instead of
# the number on screen. Off by default: the number comes from the Excel row or what you type.
DIAL_ONLY_MY_NUMBER = os.getenv("DIAL_ONLY_MY_NUMBER", "false").lower() == "true" and bool(PHONE_NUMBER)
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "240"))


def plivo_missing() -> list[str]:
    need = {"PLIVO_AUTH_ID": PLIVO_AUTH_ID, "PLIVO_AUTH_TOKEN": PLIVO_AUTH_TOKEN,
            "PLIVO_FROM_NUMBER": PLIVO_FROM_NUMBER, "PUBLIC_BASE_URL": PUBLIC_BASE_URL}
    if DIAL_ONLY_MY_NUMBER:
        need["PHONE_NUMBER"] = PHONE_NUMBER
    return [k for k, v in need.items() if not v]


# --- Phone audio clean-up ---
# Phone lines are never silent (room noise, echo of the agent's own voice). Gemini decides the
# customer has finished speaking only when it hears a pause, so we turn low-level sound into
# true silence before sending it on. Set NOISE_GATE=false to switch this off.
NOISE_GATE = os.getenv("NOISE_GATE", "true").lower() != "false"
# Languages the customer is expected to speak, as a hint for the written transcript only
# (e.g. "hi-IN,en-IN"). Off by default: on a real call it changed nothing (Hindi was still written
# in Urdu and Gurmukhi script), and that call was the first to hit Gemini "1011 internal error".
TRANSCRIPT_LANGUAGES = [c.strip() for c in os.getenv("TRANSCRIPT_LANGUAGES", "").split(",") if c.strip()]

# Who decides when the customer starts and stops speaking on a phone call.
# "manual": we do, from the loudness of the line, and tell Gemini explicitly. Gemini's own
# detector kept missing short phone replies ("haan ji"), so the customer had to repeat themselves.
# "auto": leave it to Gemini, as the browser test does.
PHONE_VAD = os.getenv("PHONE_VAD", "manual").lower()
# Manual mode: this much quiet after speech means the customer has finished their sentence.
PHONE_END_SILENCE_MS = int(os.getenv("PHONE_END_SILENCE_MS", "600"))
# If the customer has clearly spoken and the agent still has not replied after this many
# seconds, we tell Gemini "the customer paused" so it answers instead of waiting forever.
REPLY_WATCHDOG_SECONDS = float(os.getenv("REPLY_WATCHDOG_SECONDS", "6"))

# How to pronounce names. Key = how it appears in your Excel sheet, value = Hindi spelling.
# Add your own products here; matching ignores upper/lower case.
PRONUNCIATION = {
    "Vedic Vaibhav": "वैदिक वैभव",
    "Gauri": "गौरी",
    "Shri Yantra": "श्री यंत्र",
    "Sri Yantra": "श्री यंत्र",
    "Rudraksha": "रुद्राक्ष",
    "Mala": "माला",
    "Yantra": "यंत्र",
    "Kavach": "कवच",
    "Puja": "पूजा",
    "Prasad": "प्रसाद",
    "Chadhava": "चढ़ावा",
    "Gangajal": "गंगाजल",
    "Tulsi": "तुलसी",
    "Sphatik": "स्फटिक",
    "Hanuman": "हनुमान",
    "Ganesh": "गणेश",
    "Lakshmi": "लक्ष्मी",
    "Shiv": "शिव",
}
