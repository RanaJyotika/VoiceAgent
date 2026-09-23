"""Environment and settings. Loaded once at import."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GOOGLE_API_KEY = os.getenv("GOOGLE_GEMINI_LIVE_API") or os.getenv("GOOGLE_API_KEY") or ""
# The key talks to the Gemini API (generativelanguage.googleapis.com). Set GEMINI_VERTEX=true to
# send the same key to Vertex AI "express mode" instead (billed to the Google Cloud project; model
# names differ there, so set GEMINI_LIVE_MODEL as well).
GEMINI_VERTEX = os.getenv("GEMINI_VERTEX", "").lower() == "true"
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
# Most calls the campaign may run at the same time. Each call is its own Plivo call, Gemini
# session and ~0.7 Mbps each way through the tunnel; Gemini and Plivo also cap concurrent
# calls per account, and the campaign pauses itself when calls start failing.
MAX_CONCURRENT_CALLS = max(1, int(os.getenv("MAX_CONCURRENT_CALLS", "10")))


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
# Was 600 ms. On call 45 a customer said two sentences 0.8 s apart; the first went to Gemini as a
# finished turn, the second interrupted the reply being made, and the answer came 5.6 s later.
# A 900 ms pause keeps such sentences in one turn at the cost of 0.3 s on every reply.
PHONE_END_SILENCE_MS = int(os.getenv("PHONE_END_SILENCE_MS", "900"))
# ...and after a question whose answer is long (an address, a number, "what did you order"): the
# customer thinks between parts, and call 53 showed a phone that cuts its own mic between bursts.
PHONE_LONG_ANSWER_MS = int(os.getenv("PHONE_LONG_ANSWER_MS", "1800"))
# If the customer has clearly spoken and the agent still has not replied after this many
# seconds, we tell Gemini "the customer paused" so it answers instead of waiting forever.
REPLY_WATCHDOG_SECONDS = float(os.getenv("REPLY_WATCHDOG_SECONDS", "6"))
# Manual mode safety nets, both counted from the moment the agent finishes a question:
# - the line has carried loud sound for this long but nothing was recognised as speech (a hot,
#   noisy speakerphone line: call 45 lost 25 to 33 s this way, three times) -> tell the agent the
#   voice is unclear so it repeats the question, and listen more leniently for the rest of the call;
# - the line has been quiet for this long -> tell the agent to ask "hello ji, awaaz aa rahi hai?".
PHONE_STUCK_SECONDS = float(os.getenv("PHONE_STUCK_SECONDS", "8"))
PHONE_SILENT_SECONDS = float(os.getenv("PHONE_SILENT_SECONDS", "12"))
# Save each phone call's audio (the customer's side) as data/recordings/call_<id>.wav, so a bad
# call can be listened to instead of guessed at from the log. Free; about 2 MB per minute.
PHONE_RECORD = os.getenv("PHONE_RECORD", "true").lower() != "false"
RECORDINGS_DIR = DATA_DIR / "recordings"

# --- Filler sounds ---
# About 3 s pass between the customer finishing and Gemini's first audio. A short "hmm" / "achha"
# in the agent's own voice, played the moment the customer's turn closes, covers that wait.
# Clips are made once per voice with Gemini text-to-speech (same voice names as the Live model)
# and cached in data/fillers/<voice>/. FILLER_GENERATE=false forbids that paid call (a few paise
# per voice); a voice-neutral keyboard tap is used when no clips exist.
FILLERS = os.getenv("FILLERS", "true").lower() != "false"
FILLER_GENERATE = os.getenv("FILLER_GENERATE", "true").lower() != "false"
GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")

# --- Cost ---
# Gemini Live bills every model turn for the whole conversation so far (prompt tokens) plus that
# turn's reply, per modality. Prices are USD per one million tokens for the 2.5 Flash native audio
# model; update here if Google changes them. USD_INR is the exchange rate used on the dashboard.
GEMINI_PRICE_PER_M = {
    "text_in": float(os.getenv("GEMINI_PRICE_TEXT_IN", "0.50")),
    "audio_in": float(os.getenv("GEMINI_PRICE_AUDIO_IN", "3.00")),
    "text_out": float(os.getenv("GEMINI_PRICE_TEXT_OUT", "2.00")),
    "audio_out": float(os.getenv("GEMINI_PRICE_AUDIO_OUT", "12.00")),
}
USD_INR = float(os.getenv("USD_INR", "90"))

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
