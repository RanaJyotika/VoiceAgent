"""Environment and settings. Loaded once at import."""
import os
import time
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
# The company's online store, where customers place orders. They know the order by this name, so
# "Vedic Vaibhav se" alone left them asking who was calling (call 97: "order to Vedic Shop se kiya").
STORE_NAME = os.getenv("STORE_NAME", "Vedic Shop")
# Gemini's tone matching. It leaks the model's private mood label into the turn, and on calls 61
# and 62 the label was spoken aloud: "excitement" before the greeting, "calmness" before "ji",
# "surprise (negative)" before "achha". A flatter read is better than that. AFFECTIVE_DIALOG=true
# switches it back on.
AFFECTIVE_DIALOG = os.getenv("AFFECTIVE_DIALOG", "false").lower() == "true"
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
# How long the phone is allowed to ring before Plivo gives up. This is also how long the dashboard
# can sit on "Ringing" when nobody picks up: on 21-23 Sep the answer came back 31 s after dialling
# with this at 30. A rejected call is reported by the carrier when it chooses (26 s on call 51),
# and nothing here can make that quicker.
RING_SECONDS = int(os.getenv("RING_SECONDS", "25"))
# Most calls the campaign may run at the same time. Each call is its own Plivo call, Gemini
# session and ~0.7 Mbps each way through the tunnel; Gemini and Plivo also cap concurrent
# calls per account, and the campaign pauses itself when calls start failing.
MAX_CONCURRENT_CALLS = max(1, int(os.getenv("MAX_CONCURRENT_CALLS", "10")))


# --- Shopify (app/shopify.py). Everything stays idle until these are filled in. ---
# The store's own address, e.g. vedicshop.myshopify.com.
SHOPIFY_STORE = (os.getenv("SHOPIFY_STORE") or os.getenv("SHOPIFY_STORE_DOMAIN") or "").strip().removeprefix("https://").rstrip("/")
# A Dev Dashboard app installed on the store: the server trades these for a 24-hour token by itself,
# and the client secret also signs every webhook. (An older admin-made app gives a fixed token instead.)
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
SHOPIFY_ADMIN_TOKEN = (os.getenv("SHOPIFY_ADMIN_TOKEN") or os.getenv("SHOPIFY_ADMIN_API_TOKEN") or "").strip()
# Webhooks made by hand in Shopify admin (Settings > Notifications > Webhooks) are signed with the
# key shown on that page, not the app's secret.
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "").strip() or SHOPIFY_CLIENT_SECRET
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-10")

# --- Product catalogue (app/catalog.py): the Vedic Vaibhav MongoDB, read only. Empty URI = no product
# answers beyond what the call's own row says. Use a read-only database user.
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "VV-Ecom").strip()
# Where the products are: a collection of product documents, or "" to find one whose name has
# "product" in it; failing that, the products listed inside MONGODB_COLLECTIONS documents.
MONGODB_PRODUCTS = os.getenv("MONGODB_PRODUCTS", "shopifyproducts").strip()
MONGODB_COLLECTIONS = os.getenv("MONGODB_COLLECTIONS", "shopifycollections").strip()
# Only this store's products, when the database holds more than one shop ("" = all).
MONGODB_SHOP = os.getenv("MONGODB_SHOP", "").strip()
CATALOG_REFRESH_MINUTES = float(os.getenv("CATALOG_REFRESH_MINUTES", "15"))
# The main site's copies of Shopify orders, looked up by order number when a call starts (live status,
# and the products of a sheet row that has only an order number). The first one holding the order wins.
MONGODB_ORDERS = [c.strip() for c in os.getenv("MONGODB_ORDERS", "shopifyOrders,shopifyorders").split(",") if c.strip()]
# Every finished call is copied here (app/mongo_sync.py). The collection must already exist: that is
# how a URI for the wrong cluster is caught before anything is written there. "" = no copying.
MONGODB_CALLS_DB = os.getenv("MONGODB_CALLS_DB", "ai-calling").strip()
MONGODB_CALLS_COLLECTION = os.getenv("MONGODB_CALLS_COLLECTION", "calling").strip()

# --- Other systems adding contacts (POST /api/intake/{campaign_id}), e.g. the Vedic Vaibhav main
# database. Sent in the X-Api-Key header; empty = the intake is closed.
INTAKE_API_KEY = os.getenv("INTAKE_API_KEY", "").strip()


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
# WebRTC noise suppression and echo cancellation on the customer's audio (app/line_audio.py), 6 Oct 2026.
# "detector": our own listening (speech detector, barge-in, caller gate) hears the cleaned audio and the AI
#   hears the original, since a speech model trained on noisy audio can understand cleaned audio worse.
# "all": the AI hears the cleaned audio too. "off": no clean-up.
AUDIO_CLEANUP = os.getenv("AUDIO_CLEANUP", "detector").lower()
NOISE_SUPPRESSION = os.getenv("NOISE_SUPPRESSION", "true").lower() != "false"
ECHO_CANCEL = os.getenv("ECHO_CANCEL", "true").lower() != "false"
# The caller's voice fingerprint (app/voiceprint.py), owner 6 Oct 2026: is this voice the customer or someone else
# near the phone? "log": only writes what it would decide; "on": decides; "off". Tested on our recordings with a
# 0.6 s clip: under 0.12 is clearly someone else (about half of other voices, none of the caller's own clips);
# 0.42 and over is clearly the caller.
VOICEPRINT = os.getenv("VOICEPRINT", "log").lower()
VOICEPRINT_OTHER = float(os.getenv("VOICEPRINT_OTHER", "0.12"))
VOICEPRINT_CALLER = float(os.getenv("VOICEPRINT_CALLER", "0.42"))
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
# 1 Oct 2026: down to 650 ms on test, the gap before every reply still felt too long. Now only for
# sentences: a short answer has its own, shorter pause (PHONE_SHORT_END_MS). 800 ms was tried for
# sentences; back to the owner's 650 ms for the final test (1 Oct 2026).
# 5 Oct 2026, owner's test values (safe end of the ranges asked for), replayed over 73 recordings against the
# old ones: pause before a reply 492 -> 345 ms, replies thrown away 128 -> 126, customer sound reaching the agent
# 56.5% -> 51.8%. The shorter pauses alone split more sentences (156 replies thrown away); the higher
# thresholds below (barge-in 180, preparing 500, after a lost reply 350) cancel that out.
# 6 Oct 2026, owner's test values (sentence 150, short 120 under 1000 ms, long 500, preparing 700, after a lost
# reply 400), replayed over 75 recordings with a 2.8 s reply: pause before a reply 345 -> 146 ms, but replies
# thrown away by split sentences 119 -> 162, customer sound reaching the agent 52.3% -> 43.8%. 300/150 gave
# 206 ms, 136 thrown away, 47.1%.
# Back to 450 on 6 Oct 2026 (owner): at 150 ms calls 155-158 sent the agent 0.0-0.4 s scraps of the customer's
# sentences ("site par | nahi, main cancel...") and the agents answered half sentences.
PHONE_END_SILENCE_MS = int(os.getenv("PHONE_END_SILENCE_MS", "450"))   # 150 from earlier on 6 Oct (see below)
# A turn with less voice than this is a short answer ("haan", "nahi", "hmm", "ji bolo"): nothing more is
# coming, so it is closed after PHONE_SHORT_END_MS instead of waiting as for a sentence. Owner, 1 Oct 2026.
# Replayed over 58 recordings: one 650 ms pause for everything closed turns after 660 ms on average
# with 65 replies thrown away by split sentences; 350 ms short / 800 ms sentence: 557 ms and 64.
PHONE_SHORT_TURN_MS = int(os.getenv("PHONE_SHORT_TURN_MS", "1000"))   # was 800 (6 Oct 2026)
PHONE_SHORT_END_MS = int(os.getenv("PHONE_SHORT_END_MS", "250"))   # 120 earlier on 6 Oct, back to 250 with the above
# ...and after a question whose answer is long (an address, a number, "what did you order"): the
# customer thinks between parts, and call 53 showed a phone that cuts its own mic between bursts.
# Was 1800 ms; 900 ms on test from 1 Oct 2026.
PHONE_LONG_ANSWER_MS = int(os.getenv("PHONE_LONG_ANSWER_MS", "500"))   # was 700 (6 Oct 2026), 900 before 5 Oct
# ...but only for this long into the answer; after it the usual pause closes the turn. On call 101 the
# customer answered "address batayein?" by repeating "900 ka order tha" with 1 s gaps, the long pause
# never came, and the agent said nothing for 15 s. Real addresses ran 10 to 15 s (calls 55, 57, 71),
# so 10 s keeps an address whole in all but the slowest cases.
PHONE_LONG_TURN_SECONDS = float(os.getenv("PHONE_LONG_TURN_SECONDS", "10"))
# Agent audio to the phone: how far ahead of real time it is fed, and the size of each piece.
# A smaller lead and smaller pieces make the reply start and a barge-in cut sooner; too small and
# a slow tunnel leaves gaps in the agent's voice. Were 1200 ms and 200 ms until 1 Oct 2026.
PHONE_SEND_LEAD_MS = int(os.getenv("PHONE_SEND_LEAD_MS", "600"))
PHONE_OUT_CHUNK_MS = int(os.getenv("PHONE_OUT_CHUNK_MS", "40"))   # was 80 (6 Oct 2026)
# How much voice it takes to count as the customer talking (plivo_bridge.listen):
# - over the agent. Was 400 ms: on 30 Sep 2026 coughs and bumps of 0.0 to 0.2 s wiped whole
#   replies (call 105). 160 ms on test from 1 Oct 2026, for quicker interruptions.
# - while the agent prepares its reply, once a reply has already been thrown away that way. Was
#   900 ms: on call 90 each "hello?" threw away the reply to the one before. 260 ms was tried on
#   1 Oct 2026 and on call 123 faint background voices threw away 12 replies in a row: back to 900.
#   Owner set 250 ms again for the final test (1 Oct 2026); the caller gate now filters faint voices.
PHONE_BARGEIN_MS = int(os.getenv("PHONE_BARGEIN_MS", "180"))   # was 160 (5 Oct 2026 test values)
# - while the agent prepares its reply (before any reply was lost). Was fixed in code at 400 ms.
PHONE_PREPARING_MS = int(os.getenv("PHONE_PREPARING_MS", "700"))   # was 500 (6 Oct 2026)
PHONE_AFTER_LOST_REPLY_MS = int(os.getenv("PHONE_AFTER_LOST_REPLY_MS", "400"))   # was 350 (6 Oct 2026), 250 before 5 Oct
# The caller's own level, locked from their first clear speech, decides whether a new voice is them or
# someone in the room (call 123: the caller at 44-190% of it, the voices that killed replies at 4-15%).
# Under LOW: background, ignored. LOW to HIGH: the caller only if it lasts 400 ms (900 ms after a lost
# reply). Over HIGH: the caller. Tested on 54 recordings with scripts/replay.py before use.
CALLER_GATE = os.getenv("CALLER_GATE", "true").lower() != "false"
CALLER_GATE_LOW = float(os.getenv("CALLER_GATE_LOW", "0.15"))
CALLER_GATE_HIGH = float(os.getenv("CALLER_GATE_HIGH", "0.40"))
# How long a LOW-to-HIGH voice must last to count as the caller. Replayed over 58 recordings (1 Oct 2026):
# no gate 71 replies thrown away, 400 ms 64, 600 ms 57; speech sent to the agent 58% -> 57%.
CALLER_GATE_MID_MS = int(os.getenv("CALLER_GATE_MID_MS", "600"))
# If the customer has clearly spoken and the agent still has not replied after this many
# seconds, we tell Gemini "the customer paused" so it answers instead of waiting forever.
REPLY_WATCHDOG_SECONDS = float(os.getenv("REPLY_WATCHDOG_SECONDS", "4"))   # was 5 (6 Oct 2026), 6 before 5 Oct
# Manual mode safety nets, both counted from the moment the agent finishes a question:
# - the line has carried loud sound for this long but nothing was recognised as speech (a hot,
#   noisy speakerphone line: call 45 lost 25 to 33 s this way, three times) -> tell the agent the
#   voice is unclear so it repeats the question, and listen more leniently for the rest of the call;
# - the line has been quiet for this long -> tell the agent to ask "hello ji, awaaz aa rahi hai?".
# 8 s until 8 Oct 2026; owner: when the audio is not clear, ask to repeat at once.
PHONE_STUCK_SECONDS = float(os.getenv("PHONE_STUCK_SECONDS", "4"))
# Was 12 s; on call 118 the customer waited 15 s for a reply that never came. 3 s: owner, 1 Oct 2026.
# 1.5 s: owner, 8 Oct 2026 (call 194): if the customer says nothing, ask "meri awaaz aa rahi hai?" within 1-2 s.
# Counted from when the agent's voice has finished playing on the phone (plus 0.3 s for the line's tail).
PHONE_SILENT_SECONDS = float(os.getenv("PHONE_SILENT_SECONDS", "1.5"))
# ...and after that check has gone unanswered, this long again before the goodbye (12 s until 8 Oct 2026).
PHONE_SILENT_GOODBYE_SECONDS = float(os.getenv("PHONE_SILENT_GOODBYE_SECONDS", "4"))
# The customer finished and no sound of the agent's reply has come after this long: the reply is stuck. Call 194:
# the reply to "haan ji bolo" never came, the silence checks waited 8 s for it plus 3 s, and the customer heard 13 s
# of nothing. 95% of 180 logged replies started within 4.6 s. After this the agent is told to reply now.
PHONE_REPLY_LATE_SECONDS = float(os.getenv("PHONE_REPLY_LATE_SECONDS", "5"))
# The customer said something Gemini could not make out ("<noise>"); the reply to it is held back.
# If they then stay quiet this long, they are waiting for us: ask them to repeat. Call 118: a quiet
# "nahi" was heard as noise and the agent said nothing until the 12 s silence nudge.
PHONE_UNCLEAR_SECONDS = float(os.getenv("PHONE_UNCLEAR_SECONDS", "1.2"))
# A loud sound after the agent's question that was not a voice (a distorted line, a muffled answer),
# then quiet for this long: ask them to repeat instead of waiting for the 8 s stuck check. Owner, 1 Oct 2026.
PHONE_NOISE_ASK_SECONDS = float(os.getenv("PHONE_NOISE_ASK_SECONDS", "1.2"))
# Save each phone call's audio (the customer's side) as data/recordings/call_<id>.wav, so a bad
# call can be listened to instead of guessed at from the log. Free; about 2 MB per minute.
PHONE_RECORD = os.getenv("PHONE_RECORD", "true").lower() != "false"
RECORDINGS_DIR = DATA_DIR / "recordings"

# --- Filler sounds ---
# A short "hmm" / "achha" / "ji" in the agent's own voice when its reply is slow (app/fillers.py). Off since
# call 72 (25 Sep 2026: after every reply, and after sounds that were not answers); back on 6 Oct 2026 (owner)
# with rules: only when the reply has not started FILLER_DELAY_MS after a real answer of at least
# FILLER_MIN_VOICE_MS of voice, at most every other reply, at most FILLER_MAX_PER_CALL per call (0: no limit).
# The clips are made once with scripts/make_fillers.py; nothing is generated during calls.
FILLERS = os.getenv("FILLERS", "true").lower() != "false"
# When, by how long the gap really is (owner, 6 Oct 2026, after call 159: at a fixed 0.7 s the "hmm" landed just
# before ChatGPT's reply). Each call learns how long its agent takes to answer. A slow agent (FILLER_SLOW_MS or
# more, Gemini's ~3.5 s) gets the filler FILLER_DELAY_MS into the wait; a fast one only when a reply runs
# FILLER_LATE_MS past that call's usual time.
FILLER_DELAY_MS = int(os.getenv("FILLER_DELAY_MS", "1200"))
FILLER_SLOW_MS = int(os.getenv("FILLER_SLOW_MS", "2500"))
FILLER_LATE_MS = int(os.getenv("FILLER_LATE_MS", "800"))
FILLER_MIN_VOICE_MS = int(os.getenv("FILLER_MIN_VOICE_MS", "400"))
FILLER_MAX_PER_CALL = int(os.getenv("FILLER_MAX_PER_CALL", "0"))
# Which clips may be played (names in app/fillers.TEXTS). Only "hmm" by default: it carries no accent, and
# words like "achha" came out with a southern sound in the ChatGPT voices (owner, 6 Oct 2026).
FILLER_WORDS = [w.strip() for w in os.getenv("FILLER_WORDS", "hmm").split(",") if w.strip()]
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

# --- OpenAI Realtime, the fallback voice agent (app/openai_bridge.py) ---
# Used when Gemini cannot take the call at all: credits gone, key refused, repeated failure to
# connect. Those are the cases a reconnect cannot fix (22 Sep 2026: calls 46, 47 and 48 all died
# on "prepayment credits are depleted"). A transient 1011 still just reconnects to Gemini.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# gpt-realtime-2.1-mini costs about the same per call as Gemini 2.5 Flash native audio once its
# cached-prompt discount is counted; the full gpt-realtime-2.1 is roughly four times dearer.
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
# Writes the customer's words. Everything that guards the call reads this text (the spoken-digit
# check on an address, the clear-yes check, the inaudible-reply counter), so a change here
# changes how those behave.
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
# ISO code for that transcriber. Left to detect for itself on call 60 it turned a Hindi address
# into English nonsense ("You can call house number when you don't think that's a good"), and the
# agent then invented an address to fill the gap. Set it to "" to let it detect again.
OPENAI_TRANSCRIBE_LANGUAGE = os.getenv("OPENAI_TRANSCRIBE_LANGUAGE", "hi").strip()
OPENAI_FALLBACK = os.getenv("OPENAI_FALLBACK", "true").lower() != "false" and bool(OPENAI_API_KEY)

# --- Sarvam, a third voice agent (app/sarvam_bridge.py) ---
# Not one voice model but three services in a row: Saaras writes the customer's words down, the
# Sarvam-105B chat model writes the reply, Bulbul speaks it.
SARVAM_API_KEY = os.getenv("SARVAM_API") or os.getenv("SARVAM_API_KEY") or ""
SARVAM_MODEL = os.getenv("SARVAM_MODEL", "sarvam-105b-conversations")
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3-realtime")
# "codemix" writes Hindi words in Devanagari and English words in English letters, as Hinglish is spoken.
SARVAM_STT_MODE = os.getenv("SARVAM_STT_MODE", "codemix")
SARVAM_LANGUAGE = os.getenv("SARVAM_LANGUAGE", "hi-IN")
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
# Bulbul v3 speakers for Gauri (female) and Arjun (male). Owner picked Priya and Varun (13 Oct 2026).
SARVAM_VOICE_FEMALE = os.getenv("SARVAM_VOICE_FEMALE", "priya").strip().lower()
SARVAM_VOICE_MALE = os.getenv("SARVAM_VOICE_MALE", "varun").strip().lower()
# Every Bulbul v3 speaker (docs.sarvam.ai, 13 Oct 2026), offered on the dashboard when the agent is Sarvam;
# the two above first. They join VOICES, so a call, campaign or auto-dialer can carry one; Gemini and ChatGPT
# then speak in their own voice of the same gender (gemini_voice below). Sarvam's docs call varun deep and
# dramatic, not a neutral default.
_BULBUL_FEMALE = ["priya", "ishita", "ritu", "neha", "pooja", "simran", "kavya", "shreya", "roopa", "tanya",
                  "shruti", "suhani", "kavitha", "rupali"]
_BULBUL_MALE = ["varun", "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "manan", "sumit", "kabir",
                "aayan", "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "gokul", "vijay", "mohit", "rehan", "soham"]
SARVAM_VOICES = {
    **{n.capitalize(): "female" for n in dict.fromkeys([SARVAM_VOICE_FEMALE, *_BULBUL_FEMALE])},
    **{n.capitalize(): "male" for n in dict.fromkeys([SARVAM_VOICE_MALE, *_BULBUL_MALE])},
}
VOICES.update(SARVAM_VOICES)


def gemini_voice(voice: str) -> str:
    """The Gemini voice for a dashboard voice: a Sarvam one becomes Gemini's voice of the same gender."""
    gender = SARVAM_VOICES.get(voice)
    if gender is None:
        return voice
    if VOICES.get(GEMINI_VOICE) == gender:
        return GEMINI_VOICE
    return next(n for n, g in VOICES.items() if g == gender and n not in SARVAM_VOICES)


# 0.5 to 2.0. At 1.0 Priya and Varun sounded slow, as if reading a script (owner, 13 Oct 2026).
SARVAM_PACE = float(os.getenv("SARVAM_PACE", "1.15"))
# Rupees, from Sarvam's price page (8 Oct 2026): chat per million tokens, Saaras Rs 30 an hour of
# audio, Bulbul Rs 30 per 10,000 characters. Held as USD per million units, like the other agents.
SARVAM_PRICE_PER_M = {k: v / USD_INR for k, v in {
    "text_in": float(os.getenv("SARVAM_RS_TEXT_IN", "29.28")),
    "cached_text_in": float(os.getenv("SARVAM_RS_CACHED_TEXT_IN", "10.98")),
    "text_out": float(os.getenv("SARVAM_RS_TEXT_OUT", "73.20")),
    "stt_seconds": float(os.getenv("SARVAM_RS_STT_HOUR", "30")) / 3600 * 1_000_000,
    "tts_chars": float(os.getenv("SARVAM_RS_TTS_10K_CHARS", "30")) * 100,
}.items()}

# The agents the owner picks between on the dashboard, for a single call or a whole campaign; the
# server takes no other (a browser tab opened before a restart once sent a model no longer offered).
# gemini-3.8-live has refused every session on this key so far ("1007 invalid argument" as the
# greeting is sent: calls 38, 40, 56); the call then carries on with GEMINI_LIVE_MODEL, and the
# dashboard shows the agent that actually answered.
# Sarvam first (the default for a single call): the owner found it far better than the others on 9 Oct 2026.
AGENT_LABELS = {
    **({SARVAM_MODEL: "Sarvam"} if SARVAM_API_KEY else {}),
    **({OPENAI_REALTIME_MODEL: "ChatGPT"} if OPENAI_API_KEY else {}),
    GEMINI_LIVE_MODEL: "Gemini 2.5 Flash",
    "gemini-3.8-live": "Gemini 3.8 Live",
}
OFFERED_MODELS = list(AGENT_LABELS)
# Changes on every server start. The page sends it with every call it starts, and a page loaded before
# the restart is refused with "reload the page": on 30 Sep 2026 a tab left open across restarts kept
# starting calls on ChatGPT (calls 100 to 105) while the new code would have picked Gemini.
PAGE_BUILD = str(int(time.time()))
STALE_PAGE = "This page is older than the server (it was restarted). Reload the page (F5) and try again."
# After Gemini fails for a reason a retry cannot fix, later calls go straight to OpenAI for this
# long instead of spending three seconds each rediscovering it.
PROVIDER_COOLDOWN_SECONDS = float(os.getenv("PROVIDER_COOLDOWN_SECONDS", "900"))
# OpenAI voices, by the gender of the agent name they have to carry (Gauri / Arjun).
OPENAI_VOICES = {
    "marin": "female", "coral": "female", "shimmer": "female", "sage": "female", "alloy": "female",
    "cedar": "male", "ash": "male", "echo": "male", "verse": "male", "ballad": "male",
}
# Which OpenAI voice speaks for Gauri (female) and Arjun (male); empty = the first of that gender above.
# Calls 138, 150 and 153 on "marin" kept a South Indian sound, so another can be tried here without a code change.
OPENAI_VOICE_FEMALE = os.getenv("OPENAI_VOICE_FEMALE", "").strip()
OPENAI_VOICE_MALE = os.getenv("OPENAI_VOICE_MALE", "").strip()
# USD per million tokens for OPENAI_REALTIME_MODEL. Cached input is billed at a tenth, which is
# why it is counted separately: on these calls the repeated prompt is most of the bill.
OPENAI_PRICE_PER_M = {
    "text_in": float(os.getenv("OPENAI_PRICE_TEXT_IN", "0.60")),
    "audio_in": float(os.getenv("OPENAI_PRICE_AUDIO_IN", "10.00")),
    "text_out": float(os.getenv("OPENAI_PRICE_TEXT_OUT", "2.40")),
    "audio_out": float(os.getenv("OPENAI_PRICE_AUDIO_OUT", "20.00")),
    "cached_text_in": float(os.getenv("OPENAI_PRICE_CACHED_TEXT_IN", "0.06")),
    "cached_audio_in": float(os.getenv("OPENAI_PRICE_CACHED_AUDIO_IN", "0.30")),
}

# How to pronounce names. Key = how it appears in your Excel sheet, value = Hindi spelling.
# Add your own products here; matching ignores upper/lower case.
PRONUNCIATION = {
    "Vedic Vaibhav": "वैदिक वैभव",
    "Vedic Shop": "वैदिक शॉप",
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


# --- Coupons, and the messages that carry them (app/coupons.py, app/messaging.py) ---
# Owner, 19 Oct 2026: a customer who says no on an abandoned-cart call is offered the dashboard's call coupon once,
# and on their yes the code and their product go to them in a message. Nothing is sent while MESSAGE_PROVIDER is
# empty: the messages wait on the Coupons page, where a person can send them by hand.
# The store's own address, for the link in the message when the cart has no link of its own.
STORE_URL = (os.getenv("STORE_URL") or "https://vedicshop.store").strip().rstrip("/")
if not STORE_URL.startswith(("http://", "https://")):
    STORE_URL = "https://" + STORE_URL
# Who sends them: "whatsapp" (Meta's WhatsApp Cloud API), "plivo_sms", "webhook" (another system sends them), or ""
# (nobody yet).
MESSAGE_PROVIDER = os.getenv("MESSAGE_PROVIDER", "").strip().lower()
# The SMS sender (a number, or the sender id registered on DLT); empty = PLIVO_FROM_NUMBER.
MESSAGE_FROM_NUMBER = os.getenv("MESSAGE_FROM_NUMBER", "").strip()
# A message still waiting when the service is ready goes out on its own only while it is younger than this; an
# older one is held for a person to decide (the customer agreed to it on a call long before).
MESSAGE_MAX_AGE_HOURS = float(os.getenv("MESSAGE_MAX_AGE_HOURS", "24"))
# Tries for one message while the service is down, busy or slow to answer; then it is marked failed.
MESSAGE_RETRIES = max(1, int(os.getenv("MESSAGE_RETRIES", "3")))
# The SMS text, word for word as registered on DLT, with {name}, {product}, {offer}, {code}, {link}, {store}...;
# empty = messaging.DEFAULT_SMS_TEMPLATE.
MESSAGE_SMS_TEMPLATE = os.getenv("MESSAGE_SMS_TEMPLATE", "").strip()
# WhatsApp Cloud API: a permanent token and the sending number's id (Meta app > WhatsApp > API setup).
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
# A template approved by Meta. Without one, only a customer who wrote to us in the last 24 hours can get the message.
WHATSAPP_TEMPLATE = os.getenv("WHATSAPP_TEMPLATE", "").strip()
WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en").strip()
# What fills the template's {{1}}, {{2}}..., in that order (names from messaging.PLACEHOLDERS).
WHATSAPP_TEMPLATE_PARAMS = [p.strip().lower() for p in os.getenv("WHATSAPP_TEMPLATE_PARAMS", "name,product,offer,code,link").split(",")
                            if p.strip()]
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v23.0").strip()
# Plivo SMS to Indian numbers: the DLT registration the text was approved under.
PLIVO_DLT_ENTITY_ID = os.getenv("PLIVO_DLT_ENTITY_ID", "").strip()
PLIVO_DLT_TEMPLATE_ID = os.getenv("PLIVO_DLT_TEMPLATE_ID", "").strip()
PLIVO_DLT_TEMPLATE_CATEGORY = os.getenv("PLIVO_DLT_TEMPLATE_CATEGORY", "").strip()
# Another system sends the messages: each one is POSTed here as JSON, with "Authorization: Bearer <token>" when set.
MESSAGE_WEBHOOK_URL = os.getenv("MESSAGE_WEBHOOK_URL", "").strip()
MESSAGE_WEBHOOK_TOKEN = os.getenv("MESSAGE_WEBHOOK_TOKEN", "").strip()
