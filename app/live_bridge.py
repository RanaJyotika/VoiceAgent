"""Bridge between an audio source (browser now, Plivo later) and a Gemini Live session.

The source pushes 16 kHz 16-bit PCM chunks in; we push 24 kHz PCM chunks out, plus
JSON events (transcripts, interruptions, outcome, end). Both directions run as
independent tasks so neither blocks the other.
"""
import asyncio
import audioop
import contextlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import types

from . import catalog, coupons, db, messaging, openai_bridge, sarvam_bridge
from .db import IST
from .config import (AFFECTIVE_DIALOG, AGENT_LABELS, BUSINESS_NAME, STORE_NAME, GEMINI_LIVE_MODEL, GEMINI_PRICE_PER_M, GEMINI_THINKING_BUDGET,
                     GEMINI_VERTEX, GEMINI_VOICE, GOOGLE_API_KEY, OUTPUT_SAMPLE_RATE, RING_SECONDS,
                     OPENAI_FALLBACK, OPENAI_PRICE_PER_M,
                     OPENAI_REALTIME_MODEL, PROVIDER_COOLDOWN_SECONDS, REPLY_WATCHDOG_SECONDS, SARVAM_PRICE_PER_M,
                     TRANSCRIPT_LANGUAGES, gemini_voice)
from .call_types import (CART_NO_CLOSE, DEFAULT as DEFAULT_CALL_TYPE, ORDER_CONFIRMED_CLOSE, CallType, call_coupon,
                         call_type_for, coupon_offer_line, coupon_yes_line)
from .prompts import forms
from .tools import tools_for

log = logging.getLogger("bridge")

# Calls that are ringing or in progress right now, for the dashboard's live panel.
ACTIVE: dict[int, dict] = {}

# Markers an audio source can put on the incoming queue when it does its own speech detection
# (manual_vad): "the customer started talking" / "the customer stopped talking".
# With affective dialog on, the model's private mood notes sometimes leak into the written
# transcript: "emotion_user\ncalmness<ctrl95>emotion_model\ncalmness<ctrl95>". They are not spoken.
_MOOD_NOTE = re.compile(r"emotion_(?:user|model)\s*[a-zA-Z_]*\s*(?:<ctrl\d+>)?")
# ...and the bare label on its own, glued to the first word ("excitementhello", "calmnessji").
# Only at the very start of a turn: these are English words the agent could legitimately say
# in the middle of a Hinglish sentence.
_MOODS = (r"calmness|excitement|confusion|surprise|sadness|anger|joy|happiness|amusement|neutral|"
          r"disappointment|concern|empathy|sympathy|curiosity|frustration|relief|enthusiasm|warmth|"
          r"seriousness|embarrassment|gratitude|hesitation|determination|affection|admiration|"
          r"boredom|fear|disgust|pride|hope|annoyance|anticipation|nervousness")
_MOOD_PREFIX = re.compile(r"^\s*(?:" + _MOODS + r")(?:\s*\((?:positive|negative|neutral)\))?\s*(?:<ctrl\d+>)?",
                          re.IGNORECASE)


def clean_agent_text(text: str) -> str:
    out = re.sub(r"<ctrl\d+>", "", _MOOD_NOTE.sub("", text)).strip()
    return _MOOD_PREFIX.sub("", out).strip()


_NOTHING = re.compile(r"^[\s.,!?…\-–—'\"()\[\]]*$")
# Letters that can carry a customer's words on these calls: Latin, digits, Devanagari and the other
# Indian scripts, Arabic (Urdu). Anything else on a Hindi call is the transcriber guessing at noise:
# call 53 transcribed a fragment as Thai.
_OUR_LETTERS = re.compile(r"[A-Za-z0-9ऀ-෿؀-ۿ٠-٩०-९]")
_ANY_LETTER = re.compile(r"[^\W\d_]")
# Word edges that also work for Indian scripts: \b fails there, because vowel signs such as the
# "ं" in "ਹਾਂ" do not count as word characters, so "ਹਾਂ ਜੀ ਸਹੀ ਹੈ" matched nothing (call 71).
_L, _R = r"(?<![\wऀ-෿])", r"(?![\wऀ-෿])"
_YES = re.compile(_L + r"(haan|han|ha|haa|hanji|ji|jee|sahi|sahee|theek|thik|ok|okay|yes|yeah|confirm|bilkul|kar do|kar dijiye|"
                  r"kardo|karo|done|right|correct|हाँ|हां|हा|जी|हाँजी|हांजी|सही|ठीक|बिल्कुल|कर दो|कर दीजिए|"
                  r"जीजी|हन्जी|हंजी|हंदी|हनजी|कंफर्म|कन्फर्म|कनफर्म|"   # "haan ji" as the transcriber wrote it on calls 167-168, 218
                  r"ਹਾਂ|ਹਾਂਜੀ|ਜੀ|ਸਹੀ|ਠੀਕ|ਬਿਲਕੁਲ|"   # Punjabi script (call 71)
                  r"ہاں|ہاںجی|جی|صحیح|ٹھیک|بالکل|کر دو|"   # Urdu script: Gemini writes Hindi in it (calls 134, 137)
                  # The other languages a customer may ask for (owner, 19 Oct 2026): yes, okay, correct.
                  r"sure|avunu|అవును|సరే|సరి|ఓకే|aamaa|aama|ஆமா|ஆமாம்|சரி|haudu|ಹೌದು|ಸರಿ|ಓಕೆ|athe|അതെ|ശരി|"
                  r"হ্যাঁ|হাঁ|ঠিক আছে|হ্যা|હા|બરાબર|ହଁ|ଠିକ)" + _R, re.IGNORECASE)
_NO = re.compile(_L + r"(nahi|nahin|nai|nhi|no|not|galat|wrong|cancel|mat|नहीं|नही|नई|नाई|गलत|मत|कैंसल|कैंसिल|कैन्सल|केंसल|"   # नाई: "nahi" on call 182
                 r"ਨਹੀਂ|ਨਹੀ|ਗਲਤ|ਕੈਂਸਲ|نہیں|نہی|غلط|کینسل|مت|"
                 r"ledu|vaddu|లేదు|వద్దు|కాదు|illai|vendaam|இல்லை|இல்ல|வேண்டாம்|ಇಲ್ಲ|ಬೇಡ|ഇല്ല|വേണ്ട|না|নাই|"
                 r"नाही|नको|ના|નથી|ନା|ନାହିଁ)" + _R, re.IGNORECASE)


# Recorded messages of the phone network or the customer's phone, never the customer (owner, 19 Oct 2026). Call 294:
# "The person you are speaking with has put your call on hold... Yes, put your call on hold" played for minutes, the
# agent answered each one ("ji, main line par hi hoon") and the call was saved as confirmed on that "Yes". Call 302:
# "Please stay on the line" then "If you would like to leave an additional message, please reply after the tone".
_ON_HOLD = re.compile(r"(put|placed|kept)\s+(your|the)\s+call\s+on\s+hold|stay\s+on\s+the\s+line|"
                      r"(call|कॉल|कौल|काल)\s*(को)?\s*(hold|होल्ड)\s*(पर|par)\s*(रखा|rakha)|"
                      r"(लाइन|line)\s*(पर|par)\s*(बने|bane)\s*(रहें|rahen|rahein)|ਹੋਲਡ|"
                      r"आप\s+जिस\s+व्यक्ति\s+से\s+बात", re.IGNORECASE)
_RECORDED = re.compile(r"leave\s+(a|an|your)?\s*(additional\s+)?message|after\s+the\s+(tone|beep)|record\s+your\s+message|"
                       r"voice\s*mail|mailbox|not\s+available\s+to\s+take|is\s+not\s+reachable|switched\s+off|"
                       r"number\s+you\s+have\s+(dialled|dialed|called)|subscriber\s+you\s+have|"
                       r"please\s+try\s+(again\s+)?later|busy\s+on\s+another\s+call|using\s+a\s+(call\s+)?screening|"
                       r"संदेश\s+(छोड़|रिकॉर्ड)|बीप\s+(के|की)|(स्विच|switch)\s*(ऑफ|off)|पहुंच\s+से\s+बाहर|पहुँच\s+से\s+बाहर|"
                       r"(डायल|dial)\s+किया\s+(गया|है)|नंबर\s+(अभी\s+)?(उपलब्ध|व्यस्त)\s+(नहीं|है)|"
                       r"थोड़ी\s+देर\s+(बाद|में)\s+(प्रयास|कोशिश)|दूसरी\s+(कॉल|call)\s+पर\s+व्यस्त", re.IGNORECASE)


# A customer asking for another language (owner, 19 Oct 2026). Call 306: "speak in English", and the agent said it
# would, then went on in Hindi. Call 268: "Telugu bhasha mein bataiye" five times, answered as off topic. Sarvam's
# codes; the agent's goodbye in each (said by our code when it hangs up), and the words a turn ends its call on.
LANGUAGES = {
    "en-IN": ("English", r"english|इंग्लिश|अंग्रेज़ी|अंग्रेजी|angrezi|angreji", "Thank you, have a nice day.", r"nice day|good day"),
    "hi-IN": ("Hindi", r"hindi|हिंदी|हिन्दी", "dhanyavaad ji, aapka din shubh ho.", r"shubh ho|शुभ हो"),
    "te-IN": ("Telugu", r"telugu|telgu|तेलुगु|तेलुगू|तेलगू|తెలుగు", "ధన్యవాదాలు అండి, మీ రోజు శుభంగా ఉండాలి.", r"శుభంగా ఉండాలి"),
    "ta-IN": ("Tamil", r"tamil(?!\s*nadu)|तमिल(?!\s*नाडु)|தமிழ்", "நன்றி, உங்கள் நாள் இனிய நாளாக அமையட்டும்.", r"அமையட்டும்"),
    "kn-IN": ("Kannada", r"kannada|कन्नड़|कन्नड|ಕನ್ನಡ", "ಧನ್ಯವಾದಗಳು, ನಿಮ್ಮ ದಿನ ಶುಭವಾಗಿರಲಿ.", r"ಶುಭವಾಗಿರಲಿ"),
    "ml-IN": ("Malayalam", r"malayalam|मलयालम|മലയാളം", "നന്ദി, നിങ്ങളുടെ ദിവസം ശുഭമായിരിക്കട്ടെ.", r"ശുഭമായിരിക്കട്ടെ"),
    "bn-IN": ("Bengali", r"bengali|bangla|बंगाली|बांग्ला|বাংলা", "ধন্যবাদ, আপনার দিনটি শুভ হোক।", r"শুভ হোক"),
    "mr-IN": ("Marathi", r"marathi|मराठी", "धन्यवाद, तुमचा दिवस शुभ जावो.", r"शुभ जावो"),
    "gu-IN": ("Gujarati", r"gujarati|गुजराती|ગુજરાતી", "આભાર, તમારો દિવસ શુભ રહે.", r"શુભ રહે"),
    "pa-IN": ("Punjabi", r"punjabi|पंजाबी|ਪੰਜਾਬੀ", "ਧੰਨਵਾਦ ਜੀ, ਤੁਹਾਡਾ ਦਿਨ ਸ਼ੁਭ ਹੋਵੇ।", r"ਸ਼ੁਭ ਹੋਵੇ"),
    "od-IN": ("Odia", r"odia|oriya|उड़िया|ओड़िया|ଓଡ଼ିଆ", "ଧନ୍ୟବାଦ, ଆପଣଙ୍କ ଦିନ ଶୁଭ ହେଉ।", r"ଶୁଭ ହେଉ"),
}
_LANG_NAME = {code: re.compile(pat, re.IGNORECASE) for code, (_, pat, _, _) in LANGUAGES.items()}
# Words of asking for it: "<language> mein baat karo", "speak in <language>", "<language> bhasha".
_LANG_ASK = re.compile(r"mein|में|me\b|\bin\b|bolo|boliye|bol\b|baat|बोल|बात|talk|speak|language|bhasha|भाषा|samajh|समझ|"
                       r"aata|आता|aati|आती|please|plz|करो|kijiye|कीजिए|chahiye|चाहिए", re.IGNORECASE)


def asked_language(text: str) -> str:
    """The Sarvam code of a language the customer asks for ("speak in English", "Telugu bhasha mein", or the name
    alone, "तेलुगु तेलुगु"), else ""."""
    text = (text or "").strip()
    for code, name in _LANG_NAME.items():
        m = name.search(text)
        # "English nahi aati" / "Telugu not" says the opposite: that language is not wanted.
        if not m or re.match(r"[\s,]*(\S+\s+)?(nahi|nahin|nhi|नहीं|नही|not|no)(?![\wऀ-෿])", text[m.end():], re.IGNORECASE):
            continue
        if _LANG_ASK.search(text) or len(text.split()) <= 3:
            return code
    return ""


def language_note(code: str) -> str:
    """Instructions that come with every reply once the customer asked for a language other than Hindi."""
    if not code or code == "hi-IN":
        return ""
    lang = LANGUAGES[code][0]
    script = "English letters" if code == "en-IN" else f"{lang} script"
    return (f"\n\nLANGUAGE NOW (the customer asked for it; this overrides LANGUAGE and VOICE AND ACCENT above): speak only "
            f"{lang}, natural and polite, every reply written in {script}. The fixed lines of this prompt are in Hindi: say "
            f"the same in {lang}, just as short. Prices and numbers in {lang} words; product names, order, payment, COD and "
            f"delivery may stay in English. Your first reply in {lang} says once that you will speak {lang} now, then "
            f"carries on with the call. Close with \"{LANGUAGES[code][2]}\". If they ask for Hindi again, speak Hindi.")


def machine_message(text: str) -> str:
    """ "hold" or "recorded" when this customer turn is the network's or phone's recorded voice, else ""."""
    if _ON_HOLD.search(text or ""):
        return "hold"
    return "recorded" if _RECORDED.search(text or "") else ""


def heard_nothing(customer_text: str) -> bool:
    """A customer turn happened but its transcript carries no words: '.', '<noise>', a dash, or
    a script that cannot be this customer's language. A recorded network message counts as no words too."""
    t = customer_text.strip()
    if not t:
        return False
    if machine_message(t):
        return True
    bare = t.replace("<noise>", "").replace("[noise]", "")
    if _NOTHING.match(bare):
        return True
    return _ANY_LETTER.search(bare) is not None and _OUR_LETTERS.search(bare) is None


def only_noise(customer_text: str) -> bool:
    """Stricter than heard_nothing, for holding a reply back: the transcript is only '<noise>' or punctuation.
    A transcript in a foreign script still means the customer spoke, and Gemini answers the audio, not the
    transcript: on call 121 "haan ji bolo" came back in Thai, and the right reply was held back."""
    t = customer_text.strip()
    return bool(t) and _NOTHING.match(t.replace("<noise>", "").replace("[noise]", "")) is not None


def said_yes(customer_text: str) -> bool:
    """A clear agreement with no refusal in the same breath."""
    t = customer_text.strip()
    return bool(t) and not heard_nothing(t) and _YES.search(t) is not None and _NO.search(t) is None


# Digits a customer may say as words. Used to check that a number the model wants to record was
# actually spoken: on call 54 it called record_address with "Sector 45, Gurgaon, 122003" and later
# "641002" when the customer had only said "house number 302".
_DIGIT_WORDS = {
    "zero": "0", "shunya": "0", "शून्य": "0", "ek": "1", "एक": "1", "do": "2", "दो": "2", "teen": "3", "tin": "3", "तीन": "3",
    "char": "4", "chaar": "4", "चार": "4", "paanch": "5", "panch": "5", "पांच": "5", "पाँच": "5", "chhe": "6", "che": "6",
    "chah": "6", "chhah": "6", "छह": "6", "छे": "6", "छः": "6", "saat": "7", "sat": "7", "सात": "7", "aath": "8", "ath": "8",
    "आठ": "8", "nau": "9", "नौ": "9", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
    # English digits written in Devanagari: call 71's "हाउस नंबर थ्री जीरो 35" was heard as only "35",
    # so house number 3035 was refused twice and the customer was asked for it three times.
    "ज़ीरो": "0", "जीरो": "0", "ज़ेरो": "0", "वन": "1", "टू": "2", "थ्री": "3", "थ्रि": "3", "फोर": "4", "फ़ोर": "4",
    "फाइव": "5", "फ़ाइव": "5", "सिक्स": "6", "सेवन": "7", "सेवेन": "7", "एट": "8", "एइट": "8", "नाइन": "9",
    # Punjabi (Gurmukhi): the transcriber writes some Chandigarh callers in it (call 71).
    "ਸਿਫ਼ਰ": "0", "ਸਿਫਰ": "0", "ਜ਼ੀਰੋ": "0", "ਜੀਰੋ": "0", "ਇੱਕ": "1", "ਇਕ": "1", "ਦੋ": "2", "ਤਿੰਨ": "3", "ਚਾਰ": "4",
    "ਪੰਜ": "5", "ਛੇ": "6", "ਸੱਤ": "7", "ਅੱਠ": "8", "ਨੌਂ": "9", "ਨੌ": "9",
}
# A run of digits, or a word: anything between spaces and punctuation (Devanagari vowel signs are not
# "word characters" to the regex engine, so \w+ would cut "पांच" into pieces).
_DIGIT_RUN = re.compile(r"\d+|[^\s\d.,;:!?()\[\]/\-]+")


def spoken_digits(text: str) -> str:
    """Every digit in the text, in order: numerals in any script plus digit words ("ek saat chhe")."""
    out = []
    for tok in _DIGIT_RUN.findall(text or ""):
        if tok[0].isdigit():
            out.append("".join(str(unicodedata.digit(ch)) for ch in tok))
        else:
            out.append(_DIGIT_WORDS.get(tok.lower(), ""))
    return "".join(out)


def spoken_digits_spaced(text: str) -> str:
    """Like spoken_digits, but each number stays separate: "Tower 10, 502, 5th floor" -> "10 502 5"."""
    out = []
    for tok in _DIGIT_RUN.findall(text or ""):
        if tok[0].isdigit():
            out.append("".join(str(unicodedata.digit(ch)) for ch in tok))
        elif tok.lower() in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok.lower()])
    return " ".join(out)


_PLACEHOLDER = re.compile(r"^(unknown|not (given|provided|known|available)|n/?a|none|nil|null|-+|\?+)$", re.IGNORECASE)
_PART_WORDS = re.compile(r"house|हाउस|flat|फ्लैट|makaan|मकान|area|एरिया|sector|सेक्टर|society|सोसाइटी|colony|कॉलोनी|street|"
                         r"स्ट्रीट|gali|गली|city|सिटी|shehar|शहर|pin|पिन|landmark|लैंडमार्क", re.IGNORECASE)


def asks_for_address_part(agent_text: str) -> str:
    """The short question the agent has just asked for an address part, if its current turn ends in
    one ("aur area ya society?", "PIN code?"), else "". A long read-back or recap question ("...PIN
    1 4 0 6 0 3, sahi hai?", "sab sahi hai?") is not an ask, so it returns ""."""
    text = clean_agent_text(agent_text)
    if not text.rstrip().endswith("?"):
        return ""
    sentences = [x.strip() for x in re.split(r"(?<=[?।.!])\s+", text) if x.strip()]
    last = sentences[-1] if sentences else ""
    if last.endswith("?") and len(last.split()) <= 7 and _PART_WORDS.search(last) and not re.search(r"\d", last):
        return last
    return ""


# Questions whose answer is long (an address, a number, "what did you order") get a longer pause
# before the turn is closed; a yes/no gets a short one. Decided from the agent's own words.
_LONG_ANSWER = re.compile(r"address|एड्रेस|पता|house|हाउस|flat|फ्लैट|makaan|मकान|area|एरिया|sector|सेक्टर|society|सोसाइटी|"
                          r"colony|कॉलोनी|city|सिटी|शहर|pin|पिन|landmark|number|नंबर|kya order|क्या ऑर्डर|kaun ?sa|कौन ?सा|"
                          r"kitne|कितने|kab |कब |time|टाइम|naam|नाम|bata|बता", re.IGNORECASE)


def expects_long_answer(agent_text: str) -> bool:
    return _LONG_ANSWER.search(clean_agent_text(agent_text)) is not None


# A customer turn whose transcript carries no words ("<noise>") is not an answer. On call 72 the
# customer made a short "hmm" after the question; Gemini heard no words but answered it anyway ("koi
# baat nahi ji, koi khaas wajah?"), and the note sent after that reply then made the agent say "sorry
# ji, aapki awaaz nahi aayi" straight after it, to someone who had not spoken. So the reply to such a
# turn is held back, never played (run_call), and the agent speaks up only when it keeps happening.
# What it is told, by how many such turns in a row. The first gets no note here: the customer may go
# on to answer, and if they wait instead, the phone line asks them to repeat (PHONE_UNCLEAR_SECONDS).
INAUDIBLE_NOTES = {
    2: "[Note from the phone line: again a sound from the customer's side with no words in it; you still have no answer "
       "to your question. Say 'sorry ji, awaaz saaf nahi aa rahi' and ask that question again, in fewer words.]",
    3: "[Note from the phone line: three sounds in a row from the customer's side carried no words. Ask them once, warmly, "
       "to hold the phone a little closer and speak slowly ('ji, phone thoda paas rakh ke dheere se boliye'), then ask the "
       "same question again. Do not move on.]",
    4: "[Note from the phone line: four replies in a row could not be heard. Do not ask this question again. If it was "
       "about the address or another detail: say 'koi baat nahi ji, maine note kar liya hai', put that in mark_outcome notes, and "
       "carry on with the next step. If it was the final confirmation or the opening: say the line is bad and you will "
       "call back a little later ('awaaz nahi aa rahi, hum thodi der mein dobara call karte hain'), then call end_call "
       "without mark_outcome.]",
}
# Goes with the customer's next words (or the next note from the line) after a reply was held back,
# so the agent does not take their answer as a reply to something they never heard.
UNHEARD_NOTE = ("[Note from the phone line: your last reply was NOT played to the customer. It answered a sound that was "
                "not the customer talking to you (a breath, a 'hmm', background noise, or another person near the phone). Treat that reply as never said: anything it asked, you "
                "have NOT asked. What the customer says now answers the last thing they heard from you, which was: "
                "\"{question}\". Reply to their words as an answer to that.]")

# Did the agent ask why? Only turns the customer heard count (a held-back reply is not in the transcript).
_ASKED_WHY = re.compile(r"wajah|kyun|kyon|kyu\b|reason|kaaran|dikkat|वजह|क्यों|क्यूँ|क्यू|कारण|दिक्कत", re.IGNORECASE)
# Goes with the customer's answer to the main question while the reason has not been asked yet. Call 145:
# a "nai" (written "ใน") to "kya aap yeh order karna chahenge?" went straight to the goodbye, and the
# result check comes only after the goodbye has been spoken.
ASK_WHY_NOTE = ("[Note from the phone line: if this answer is a no, do not say goodbye yet. First ask once: 'koi baat "
                "nahi ji. agar bura na maanein toh, koi khaas wajah?' and wait for their answer.]")
# A "reason" made only of these words is a no said again, not a reason: on call 77 the agent recorded
# not_interested_reason "not interested" without having asked.
_NO_WORDS = {"nahi", "nahin", "nhi", "no", "not", "interested", "interest", "hoon", "hu", "hai", "hain", "main", "mai",
             "mujhe", "me", "i", "am", "ji", "chahiye", "given", "none", "at", "all", "in", "this", "it", "the", "customer",
             "is", "says", "said", "नहीं", "नही", "इंटरेस्टेड", "इंटरेस्ट", "हूं", "हूँ", "है", "हैं", "मैं", "मुझे", "चाहिए", "जी"}


def bare_no(reason: str) -> bool:
    """True when the recorded reason says nothing beyond "no" ("not interested", "nahi chahiye", "")."""
    # Split on spaces and punctuation, not on \w: Devanagari vowel signs are not "word characters" to the
    # regex engine, so \w would cut "नहीं" into pieces.
    words = re.findall(r"[^\s.,;:!?()\[\]/\-\"'|]+", (reason or "").lower())
    return not any(w not in _NO_WORDS for w in words)


def probe_answer(turns: list[dict[str, str]]) -> str:
    """What the customer said after the agent last asked why ("koi dikkat aayi thi kya?"), as one line.
    "" when it was never asked or not answered yet. Their answer to a coupon offer after it is not part of it."""
    asked_at = next((i for i in range(len(turns) - 1, -1, -1) if turns[i]["role"] == "agent"
                     and _ASKED_WHY.search(turns[i]["text"])), None)
    if asked_at is None:
        return ""
    said = []
    for t in turns[asked_at + 1:]:
        if t["role"] == "agent" and offers_coupon(t["text"]):
            break
        if t["role"] == "customer" and not heard_nothing(t["text"]):
            said.append(t["text"].strip())
    return " ".join(said).strip()


def last_question(transcript: list[dict[str, str]]) -> str:
    """The end of the last thing the customer heard the agent say: the question they are answering."""
    said = next((t["text"] for t in reversed(transcript) if t["role"] == "agent"), "")
    sentences = [x.strip() for x in re.split(r"(?<=[?।.!])\s+", said) if x.strip()]
    return sentences[-1] if sentences else said


# Results that throw an order or a booking away. Refused unless the customer said a no somewhere.
NEEDS_A_NO = ("cancelled", "not_ordered")

# A cancellation needs the customer asking for one. Any "nahi" was enough before, and on call 120
# "pichhla order abhi tak nahi aaya" let the agent cancel an order the customer only complained about.
_CANCEL = re.compile(_L + r"(cancel\w*|कैंसल|कैंसिल|कैन्सल|केंसल|केंचल|कैंचल|केंसिल|कैन्सिल|केन्सल|केन्सिल|ਕੈਂਸਲ|کینسل|nahi chahiye|nahin chahiye|nhi chahiye|नहीं चाहिए|"
                     r"नही चाहिए|ਨਹੀਂ ਚਾਹੀਦਾ|نہیں چاہیے|mat bhejo|mat bhejna|मत भेजो|मत भेजना|nahi rakhna|नहीं रखना|"
                     r"wapas le lo|वापस ले लो)" + _R, re.IGNORECASE)
_KEEP_QUESTION = re.compile(r"rakhna|रखना|cancel|कैंसल|कैंसिल", re.IGNORECASE)
# "Order rakhna chahenge?" after a cancel request, and answers that put it off ("baad mein kar denge") or keep it now.
_KEEP_IT = re.compile(r"rakhna|रखना", re.IGNORECASE)
_LATER = re.compile(r"baad\s*(mein|me|men)|बाद\s*(में|मे)|later|phir\s*kabhi|फिर\s*कभी|abhi\s*nahi|अभी\s*नहीं|agli\s*baar|"
                    r"अगली\s*बार|next\s*time|soch\s*ke|सोच\s*(के|कर)", re.IGNORECASE)
_KEEPS = re.compile(r"bhej\s*do|भेज\s*दो|भेज\s*दीजिए|rakh\s*lo|रख\s*लो|rakhna\s*hai|रखना\s*है|confirm\s*kar|कन्फर्म\s*कर|"
                    r"कंफर्म\s*कर|अभी\s*भेज", re.IGNORECASE)


def asked_to_cancel(turns: list[dict[str, str]]) -> bool:
    """The customer asked to cancel, or opened their answer to "order rakhna chahenge?" with a no."""
    agent = ""
    for t in turns:
        if t["role"] == "agent":
            agent = t["text"]
        elif _CANCEL.search(t["text"]):
            return True
        elif _KEEP_QUESTION.search(agent):
            head = " ".join(t["text"].split()[:3])
            if _NO.search(head) or "نہیں" in head:
                return True
    return False


# "Not ordered" drops the order too, so it needs the customer saying they did not order it. Any "nahi"
# was enough before: on call 121 "nahi, mere ko to laga Vedic Vaibhav ki site" closed a real order.
_NAHI = r"(nahi|nahin|nhi|नहीं|नही|ਨਹੀਂ|نہیں)"
_ORDERED = r"(kiya|किया|ਕੀਤਾ|کیا|mangaya|मंगाया|mangwaya|मंगवाया|manga|मंगा)"
_NOT_ORDERED = re.compile(_ORDERED + r"\s*(hi|ही|ਹੀ)?\s*" + _NAHI + "|" + _NAHI + r"\s*" + _ORDERED +
                          r"|did ?n[o']?t order|not ordered|never ordered", re.IGNORECASE)
_NUMBER_CHECK = re.compile(r"(number|नंबर|ਨੰਬਰ)\s*(hai|है|ਹੈ)", re.IGNORECASE)   # "kya yeh <name> ji ka number hai?"
# The agent's own questions, sentence by sentence: did it ask about the address, did it say a mismatch back?
_ADDRESS_WORDS = re.compile(r"address|एड्रेस|पता|delivery|डिलीवरी", re.IGNORECASE)
# The agent taking an abandoned cart's order: the payment question or the close that sends it to the team.
_TAKING_ORDER = re.compile(r"payment|पेमेंट|cash on delivery|कैश ऑन|team ko bhej|टीम को भेज", re.IGNORECASE)
# A refusal without a "nahi" in it ("rehne do", "chhodo").
_REFUSAL = re.compile(r"rehne do|rahne do|chhod|chod do|रहने दो|छोड़|छोड", re.IGNORECASE)
# The customer asking for a call later (busy now), as opposed to buying later ("baad mein kharidungi", call 182).
_CALL_BACK = re.compile(r"call|phone|फोन|फ़ोन|कॉल|busy|बिज़ी|बिजी|व्यस्त|baat kar|बात कर|meeting|मीटिंग|drive|ड्राइव",
                        re.IGNORECASE)
# Someone else answered and says the person named is not there (call 193: "jyotika ji toh bahar gayi hai"). Third-person
# forms only, so a customer's own "main busy hoon" is not taken as this.
_NOT_AVAILABLE = re.compile(r"bahar\s+(gayi|gaye|gaya|gai|hai|hain)|बाहर\s+(गई|गयी|गए|गया|है|हैं)|"
                            r"ghar\s+(par|pe|mein)\s+nahi|घर\s+(पर|पे|में)\s+नहीं|"
                            r"(woh|wo|vo|unka|unki|unse|unko)\s+(\S+\s+){0,5}(nahi hai|nahi hain|nahi ho|busy|vyast)|"
                            r"(वो|वह|उनसे|उनका|उनकी|उनको)\s+(\S+\s+){0,5}(नहीं है|नहीं हैं|नहीं हो|बिज़ी|बिजी|व्यस्त)|"
                            r"available\s+nahi|अवेलेबल\s+नहीं|अवेलेबर\s+नहीं|"
                            r"unse baat nahi|उनसे बात नहीं|baat nahi ho (sakti|payegi|rahi)|बात नहीं हो (सकती|पाएगी|रही)|"
                            r"baad mein (call|phone) (karna|kariye|karo|karein)|बाद में (कॉल|फोन|फ़ोन) (करना|करिए|करो|करें|कीजिए)",
                            re.IGNORECASE)
AWAY_LINE = "theek hai ji, thodi der baad connect karte hain. dhanyavaad ji."   # owner, 8 Oct 2026 (call 193)
# A no given as a reason (call 143): "maine kisi aur site se mangwaya tha", "aapka price bahut zyada".
# The transcriber writes "site" as "साइड".
_DECLINE_REASON = re.compile(r"(kisi aur|kahin aur|kahi aur|dusri|doosri|dusre|doosre|another|other|किसी और|कहीं और|कही और|"
                             r"दूसरी|दूसरे)\s+(site|side|website|jagah|dukaan|shop|app|साइट|साइड|वेबसाइट|जगह|दुकान|शॉप|ऐप)|"
                             r"bahar se|बाहर से|mehenga|mehnga|mahanga|costly|expensive|महंगा|महँगा|मेहंगा|"
                             r"(price|daam|dam|rate|keemat|kimat|प्राइस|दाम|रेट|कीमत|क़ीमत)\s+(\S+\s+){0,2}"
                             r"(zyada|jyada|jada|high|ज्यादा|ज़्यादा|जादा|हाई)", re.IGNORECASE)
# The abandoned cart's check that the customer already ordered: "kya aapne yahi <items> order kar liya hai?"
_ALREADY_CHECK = re.compile(r"(order|ऑर्डर)\s*(kar|कर)\s*(liya|li|diya|di|लिया|ली|दिया|दी)", re.IGNORECASE)
# The customer saying in their own words that they already ordered it (call 217: "मैंने पहले कर लिया", "Vedic shop से
# मंगवाया था"). Owner, 12 Oct 2026: that is enough; thank them and close, never check again.
# Past tense only: "haan confirm kar dijiye" or "mangwana hai" is a yes to ordering, never "already ordered".
_END = r"(?![\wऀ-ॿ])"   # a word's end; Python's \b breaks inside Hindi words at a vowel sign ("दी|जिए")
_SAID_ORDERED = re.compile(r"pehle|pahle|पहले|already|ऑलरेडी|"
                           rf"(kar|कर)\s*(liya|li|diya|di|लिया|ली|दिया|दी){_END}|"
                           rf"mangwa(ya|yi|i|\s*liya|\s*li){_END}|मंगवा(या|ई|\s*लिया|\s*ली){_END}|मँगवा(या|ई|\s*लिया){_END}|"
                           rf"manga\s*(liya|li){_END}|मंगा\s*(लिया|ली){_END}|(le|ले)\s*(liya|li|लिया|ली){_END}|"
                           r"(confirm|कन्फर्म|कंफर्म)\s*(ho|हो)\s*(gaya|gayi|chuka|chuki|गया|गई|चुका|चुकी)|"
                           rf"(order|ऑर्डर)\s*(kiya|ki|किया|की){_END}", re.IGNORECASE)
# The opening's "kya meri baat <name> ji se ho rahi hai?" (owner, 6 Oct 2026): not a question about the order.
_IDENTITY = re.compile(r"baat\s+\S+.{0,20}(ho rahi|ho rahi hai)|बात\s+\S+.{0,20}हो रही", re.IGNORECASE)
_SAY_BACK = re.compile(r"hisaab|हिसाब|mutabik|मुताबिक|anusaar|अनुसार|matlab|मतलब|toh aap|तो आप", re.IGNORECASE)


def agent_questions(turns: list[dict[str, str]]) -> list[str]:
    """Every question sentence the agent said on the call. "?", "।" and "!" end a sentence even with no space after
    (Sarvam joins its sentences: "...है।जी"), a full stop only before a space: call 282's address "Plot - fg/131
    I.D.market.nayapalli .bbsr" was cut at each dot, the address question lost its "delivery", and the code made the
    agent ask it a fourth time."""
    out = []
    for t in turns:
        if t["role"] == "agent":
            out += [s for s in re.split(r"(?<=[?।!])\s*|(?<=\.)\s+", t["text"]) if s.strip().endswith("?")]
    return out


# Complaint words: the customer's own lines with these go into the notes, so the team sees them
# even when the agent wrote nothing (call 120: fake products, an order that never came, no notes).
_COMPLAINT = re.compile(r"nakli|नकली|ਨਕਲੀ|نقلی|fraud|फ्रॉड|फ्राड|फ्रोड|fake|फेक|फ़ेक|फैक|scam|स्कैम|ghatiya|घटिया|"
                        r"duplicate|डुप्लीकेट|dhokha|धोखा|kharab|खराब|"
                        r"ਖਰਾਬ|nahi aaya|नहीं आया|नही आया|ਨਹੀਂ ਆਇਆ|refund|रिफंड", re.IGNORECASE)

# A problem with the site, the price, delivery or payment, named in answer to "koi dikkat aayi thi kya?". The transcriber
# writes "site" as "साइड".
_PROBLEM = re.compile(r"site|side|website|साइट|साइड|वेबसाइट|price|daam|keemat|kimat|कीमत|क़ीमत|दाम|प्राइस|mehenga|mehnga|"
                      r"महंगा|महँगा|delivery|डिलीवरी|payment|पेमेंट|quality|क्वालिटी", re.IGNORECASE)

# Call 195 (ChatGPT): its answer to "koi dikkat aayi thi kya?" got a 13 s and then a 16 s speech, the order question
# twice more and a certificate that is in no record. Sent as this one reply's own instructions, not as a message:
# ChatGPT spoke a quoted note out loud whatever the customer said (calls 155, 157).
CART_CLOSE_HINT = ("\n\nTHIS REPLY ONLY: the customer is answering what stopped them. That is their second no. At most two "
                   "short sentences, ending in 'dhanyavaad ji, aapka din shubh ho.': a doubt or complaint gets the complaint "
                   "reply from LISTEN FIRST, a question gets a one-sentence answer from CART DETAILS, anything else gets only "
                   "'theek hai ji, dhanyavaad. aapka din shubh ho.' Never ask about the order again, never explain the store, "
                   "say nothing that is not in CART DETAILS. Speak first, then call mark_outcome 'not_interested' and "
                   "end_call. Only if they now clearly want it: step 3. If you could not make out their words: 'sorry ji, "
                   "samajh nahi aaya, ek baar phir bata dijiye'.")


# The coupon offer (owner, 19 Oct 2026): "ji, abhi hum online payment par coupon lagane se ... ki chhoot de rahe hain.
# agar aap lena chahein, toh kya main woh coupon aapko bhej doon?": a turn that names the coupon and asks whether to
# send it. The agents write "coupon" in English letters or in Devanagari.
_COUPON = re.compile(r"coupon|cupon|kupon|कूपन|कुपन|कूपॉन|कूपोन|कोपन|కూపన్|கூப்பன்|ಕೂಪನ್|കൂപ്പൺ|কুপন|કૂપન|ਕੂਪਨ|କୁପନ",
                     re.IGNORECASE)
_SEND_IT = re.compile(r"bhej|भेज|share|शेयर|send|పంప|அனுப்ப|ಕಳುಹಿಸ|ಕಳಿಸ|അയ|পাঠা|पाठव|મોકલ|ਭੇਜ|ପଠା", re.IGNORECASE)
# A yes that only says "send it", which the yes words do not hold ("bhej do"). "Mat bhejo" has a no in it.
_SEND_YES = re.compile(_L + r"(bhej do|bhej dijiye|bhej dijie|bhej de|bhejiye|bhejo|send kar do|send karo|share kar do|"
                       r"भेज दो|भेज दीजिए|भेज दीजिये|भेज दे|भेजिए|भेजिये|भेजो|सेंड कर दो|सेंड करो|शेयर कर दो)" + _R,
                       re.IGNORECASE)
_DONT_SEND = re.compile(r"mat bhej|मत भेज|rehne do|rahne do|रहने दो", re.IGNORECASE)


def offers_coupon(text: str) -> bool:
    """This agent turn makes the coupon offer: it names the coupon and asks whether to send it."""
    return bool(_COUPON.search(text)) and any(_SEND_IT.search(q) for q in agent_questions([{"role": "agent", "text": text}]))


def coupon_offered(turns: list[dict[str, str]]) -> bool:
    return any(t["role"] == "agent" and offers_coupon(t["text"]) for t in turns)


def yes_to_coupon(turns: list[dict[str, str]]) -> str:
    """The customer's clear yes to the last coupon offer, unless a later turn of theirs takes it back; "" otherwise.
    A bare "ji?" is "pardon?", not a yes."""
    for i in range(len(turns) - 1, -1, -1):
        if turns[i]["role"] == "agent" and offers_coupon(turns[i]["text"]):
            later = [t["text"] for t in turns[i + 1:] if t["role"] == "customer" and not heard_nothing(t["text"])]
            if not later or any(_DONT_SEND.search(x) or _starts_with_no(x) for x in later[1:]):
                return ""
            answer = later[0]
            sends = _SEND_YES.search(answer) is not None and _NO.search(answer) is None
            return answer if sends or (said_yes(answer) and "?" not in answer) else ""
    return ""


def _starts_with_no(text: str) -> bool:
    words = re.findall(r"[^\s.,;:!?।|\"'()\-]+", text.lower())
    return bool(words) and words[0] in _OPENING_NO


def _coupon_offer_hint(coupon: dict) -> str:
    return ("\n\nTHIS REPLY ONLY: the customer is answering what stopped them. Do not say goodbye yet. A doubt or complaint: "
            "the complaint reply from LISTEN FIRST, then 'dhanyavaad ji, aapka din shubh ho.', and no offer. Anything else: "
            "at most one short sentence for what they said (a question: its answer from CART DETAILS), then THE OFFER, "
            f"exactly: '{coupon_offer_line(coupon)}' Then stop and wait for their answer. Never ask about the order "
            "again. If you could not make out their words: 'sorry ji, samajh nahi aaya, ek baar phir bata dijiye'.")


def _coupon_answer_hint(voice: str) -> str:
    return ("\n\nTHIS REPLY ONLY: the customer is answering your coupon offer; never make the offer again. A yes: say "
            f"exactly '{coupon_yes_line(voice)}' Then call mark_outcome 'wants_coupon' and end_call. A question about it: "
            "its answer in one short sentence from CART DETAILS, then 'toh kya main woh coupon aapko bhej doon?' Anything "
            f"else: say exactly '{CART_NO_CLOSE}' Then call mark_outcome 'not_interested' and end_call. If you could not "
            "make out their words: 'sorry ji, samajh nahi aaya, ek baar phir bata dijiye'.")


def closing_hint(state: "CallState") -> str:
    """Extra instructions for the reply to the customer's answer to the cart's why-question (ChatGPT; Sarvam too when
    the call has a coupon to offer), and to their answer to the coupon offer."""
    if state.call_type.key != "abandoned_cart" or state.outcome:
        return ""
    last = next((t["text"] for t in reversed(state.transcript) if t["role"] == "agent"), "")
    asked_why = any(_ASKED_WHY.search(q) for q in agent_questions([{"role": "agent", "text": last}]))
    if state.coupon and state.provider in ("openai", "sarvam"):
        if offers_coupon(last):
            return _coupon_answer_hint((ACTIVE.get(state.call_id) or {}).get("voice") or "")
        if asked_why and not coupon_offered(state.transcript):
            return _coupon_offer_hint(state.coupon)
    return CART_CLOSE_HINT if state.provider == "openai" and asked_why else ""


# Thanks in the other languages a customer may ask for (owner, 19 Oct 2026), and their closing lines (LANGUAGES).
_THANKS_ANY = r"dhanyavaad|dhanyawad|dhanyavad|धन्यवाद|thank you|thanks|ధన్యవాదాలు|ధన్యవాదం|நன்றி|ಧನ್ಯವಾದ\S*|നന്ദി|ধন্যবাদ|ਧੰਨਵਾਦ|આભાર|ଧନ୍ୟବାଦ"
_GOODBYE = re.compile(_THANKS_ANY + r"|shubh ho|शुभ हो", re.IGNORECASE)
# The closing line as the last words of a turn ("... dhanyavaad ji, aapka din shubh ho."): the call is over.
_FINAL_GOODBYE = re.compile("(" + "|".join(end for _, _, _, end in LANGUAGES.values()) + r")[\s.!।]*$", re.IGNORECASE)
# "... dhanyavaad ji." as the last words: the close of the wrong-number and person-away lines. Counts as the end of
# the call only once a result is recorded (see turn_ends_call), so a passing thanks mid-call does not hang up.
_FINAL_THANKS = re.compile("(" + _THANKS_ANY + r")\s*(ji|जी|అండి)?[\s.!।]*$", re.IGNORECASE)


# The confirmed order's close (owner, 19 Oct 2026): "ji, aapka order confirm ho gaya hai. dhanyavaad ji." ends the call
# even before the result is written, so the line is cut right after it (the result is then asked for, as after "shubh ho").
_ORDER_CONFIRMED_END = re.compile(r"(confirm|कंफर्म|कन्फर्म|कनफर्म)\s*(ho|हो)\s*(gaya|gayi|गया|गई|गयी).{0,60}"
                                  r"(dhanyavaad|dhanyawad|dhanyavad|धन्यवाद)\s*(ji|जी)?[\s.!।]*$", re.IGNORECASE)


def turn_ends_call(text: str, state: "CallState") -> bool:
    """The agent's turn ended in its goodbye: "shubh ho", the confirmed order's close, or "dhanyavaad ji" with the
    result already recorded."""
    return (_FINAL_GOODBYE.search(text) is not None or _ORDER_CONFIRMED_END.search(text) is not None
            or (bool(state.outcome) and _FINAL_THANKS.search(text) is not None))
# Call 135: the agent said the closing line but never called end_call; 3 s later the silence check had it ask
# "hello ji, meri awaaz aa rahi hai?" and say the goodbye again. Owner: after the closing line, hang up.
GOODBYE_HANGUP_SECONDS = 0.0   # owner, 7 Oct 2026: cut the call the moment the goodbye has played
# Call 176: a question and the goodbye in one turn ("koi dikkat aayi? ... aapka din shubh ho"). Owner: hear the answer,
# note it, say goodbye, then hang up; if they say nothing, hang up this long after the goodbye finishes.
ASK_BYE_WAIT_SECONDS = 5.0
# On hold (owner, 19 Oct 2026, call 294): the agent waits in silence, and hangs up after this long if nobody comes back;
# the order is rung again later like a busy line.
HOLD_HANGUP_SECONDS = 30.0


def said_goodbye(transcript: list[dict[str, str]]) -> bool:
    """The last thing the customer heard from the agent was its goodbye."""
    said = next((t["text"] for t in reversed(transcript) if t["role"] == "agent"), "")
    return _GOODBYE.search(said) is not None


_ONLY_HELLO = re.compile(r"^[\s.,!?।]*((hello|helo|hallo|hailo|हेलो|हैलो|हलो|हल्लो|ji|जी)[\s.,!?।]*)*$", re.IGNORECASE)


def never_got_through(transcript: list[dict[str, str]]) -> bool:
    """The customer said nothing but "hello" all call: they cannot hear us, so a goodbye is pointless (owner, 13 Oct 2026)."""
    return all(_ONLY_HELLO.match(t["text"]) for t in transcript if t["role"] == "customer")


def closing_line(state: "CallState", outcome: str | None) -> str:
    """The goodbye the agent is told to say when it recorded a result without saying one (calls 178, 183)."""
    voice = (ACTIVE.get(state.call_id) or {}).get("voice") or ""
    f = forms(voice)   # an unknown voice gets the feminine forms
    if state.language != "hi-IN":
        # The customer asked for another language (owner, 19 Oct 2026): the goodbye in it.
        if state.language == "en-IN" and outcome == "confirmed" and state.call_type.key == "order_confirmation":
            return "Your order is confirmed. Thank you."
        if state.language == "en-IN" and outcome == "wants_coupon":
            return "Okay, I will send you the coupon code and the product details by message. Thank you, have a nice day."
        return LANGUAGES[state.language][2]
    if outcome == "wants_coupon":
        return coupon_yes_line(voice)   # owner, 19 Oct 2026
    if outcome == "confirmed" and state.call_type.key == "order_confirmation":
        return ORDER_CONFIRMED_CLOSE   # owner, 19 Oct 2026: it had said only "dhanyavaad ji, aapka din shubh ho"
    if outcome == "not_interested" and _COMPLAINT.search(probe_answer(state.transcript)):
        # Call 191: "aapki site fake product bhejti hai" to "koi dikkat aayi thi?" got only "theek hai ji, dhanyavaad".
        # The complaint reply (owner's words, 1 Oct 2026), then the goodbye.
        return (f"ji, aapki chinta samajh {f['sakti']} hoon. hamari poori koshish rehti hai ki original quality ka product hi "
                f"pahunche. aapki yeh baat maine note kar li hai. dhanyavaad ji, aapka din shubh ho.")
    if outcome == "not_interested" and state.call_type.key == "abandoned_cart":
        return CART_NO_CLOSE   # owner, 7 Oct 2026 (call 182)
    return (f"achha ji, samajh {f['gayi']}. " if outcome == "not_interested" else "") + "dhanyavaad ji, aapka din shubh ho."


def yes_to_main_question(transcript: list[dict[str, str]], pattern: str) -> str:
    """The customer's clear yes to the call's main question ("kya aapka order confirm hai?"), if the
    last time it was asked they said yes and nothing later took it back. "" otherwise."""
    if not pattern:
        return ""
    asked = re.compile(pattern, re.IGNORECASE)
    for i in range(len(transcript) - 1, -1, -1):
        t = transcript[i]
        if t["role"] == "agent" and asked.search(t["text"]):
            later = [x["text"] for x in transcript[i + 1:] if x["role"] == "customer" and not heard_nothing(x["text"])]
            if later and said_yes(later[0]) and not any(_NO.search(x) for x in later[1:]):
                return later[0]
            return ""
    return ""


def ever_yes_to_main_question(transcript: list[dict[str, str]], pattern: str) -> str:
    """The customer's clear yes to the main question at any point in the call, even if it was asked again later."""
    if not pattern:
        return ""
    asked = re.compile(pattern, re.IGNORECASE)
    for i, t in enumerate(transcript):
        if t["role"] == "agent" and asked.search(t["text"]):
            answer = next((x["text"] for x in transcript[i + 1:] if x["role"] == "customer" and not heard_nothing(x["text"])), "")
            if said_yes(answer) and "?" not in answer:   # call 168: "...क्या नाम बताया जी?" is a question, not a yes
                return answer
    return ""


_OPENING_NO = {"nahi", "nahin", "nai", "nhi", "no", "nope", "na", "नहीं", "नही", "नई", "ना", "ਨਹੀਂ", "ਨਹੀ", "نہیں",
               "ledu", "vaddu", "లేదు", "వద్దు", "illai", "இல்லை", "வேண்டாம்", "ಇಲ್ಲ", "ಬೇಡ", "ഇല്ല", "വേണ്ട", "না",
               "नाही", "नको", "ના", "ନା"}


def no_to_main_question(transcript: list[dict[str, str]], pattern: str) -> str:
    """The customer's clear no to the call's main question, if they gave one and never said yes after it.
    Call 139: "kya aap wahi order karna chahenge?" "नहीं", then they hung up, and the call was saved as
    dropped and queued to be rung again."""
    if not pattern:
        return ""
    asked = re.compile(pattern, re.IGNORECASE)
    no, at = "", -1
    for i, t in enumerate(transcript):
        if t["role"] == "agent" and asked.search(t["text"]):
            j = next((k for k in range(i + 1, len(transcript)) if transcript[k]["role"] == "customer"
                      and not heard_nothing(transcript[k]["text"])), None)
            # The answer must open with the no: "पर वो बोल ही नहीं रही" (call 90) has a नहीं in it but refuses nothing.
            words = re.findall(r"[^\s.,;:!?।|\"'()\-]+", transcript[j]["text"].lower()) if j is not None else []
            if words and words[0] in _OPENING_NO and not said_yes(transcript[j]["text"]):
                no, at = transcript[j]["text"], j
    if no and not any(t["role"] == "customer" and said_yes(t["text"]) for t in transcript[at + 1:]):
        return no
    return ""


# Strong Hindi and English abuse, as the transcriber writes it. Only tags the call for the team: the
# agent hears the audio itself and follows the prompt, and a misheard word (call 98's "चूपिया") slips past.
_ABUSE = re.compile(_L + r"(madarchod|maderchod|madarchood|behenchod|behnchod|bhenchod|benchod|chutiya|chutiye|chutia|"
                    r"chootiya|bhosdi\w*|bhosadi\w*|gandu|haramzad\w*|harami|randi|kamina|kamine|fuck\w*|bastard|"
                    r"मादरचोद|बहनचोद|बहनचो|भेनचोद|बेनचोद|चूतिया|चुतिया|चूतिये|चुतिये|भोसड़ी\S*|भोसडी\S*|भोसड़ीके|"
                    r"गांडू|गांडु|हरामी|हरामज़ादा|हरामजादा|रंडी|कमीना|कमीने)" + _R, re.IGNORECASE)


def was_abusive(transcript: list[dict[str, str]]) -> bool:
    return any(t["role"] == "customer" and _ABUSE.search(t["text"]) for t in transcript)


MAX_RECONNECTS = 3
_refused_models: set[str] = set()   # models that closed a session with "1007 invalid argument"

_hints_ok = True # set to False once a model refuses the transcript language hint

ACTIVITY_START = object()
ACTIVITY_END = object()
# Sent just before ACTIVITY_END when the voice fingerprint says the turn was someone else near the phone, not the
# customer (plivo_bridge, VOICEPRINT=on): the reply to it is held back like the reply to a sound with no words.
NOT_CALLER = object()
# From the phone line: "hello ji, meri awaaz aa rahi hai?" asked twice and still silence. Owner, 13 Oct 2026 (call
# 228): they cannot hear us, so hang up with no goodbye.
SILENT_HANGUP = object()


class Nudge(str):
    """A note from the audio source for the agent's ears only, e.g. "the line has been noisy for
    8 seconds". Goes to Gemini as a text turn; never spoken and never in the transcript."""


GOODBYE_NOTE = Nudge("[Note from the system: you have said goodbye and the customer heard it. Say nothing more. "
                     "Call mark_outcome with the result of this call now, then end_call.]")


def is_billing_error(exc: BaseException) -> bool:
    """Out of credits, billing off, quota gone: retrying cannot help, so fail fast and say why."""
    text = str(exc).lower()
    return any(w in text for w in ("credits", "billing", "quota", "exhausted", "payment"))


def make_client() -> genai.Client:
    if GEMINI_VERTEX:
        return genai.Client(vertexai=True, api_key=GOOGLE_API_KEY)
    return genai.Client(api_key=GOOGLE_API_KEY)


def turn_usage(um: Any) -> dict[str, int]:
    """Token counts of one model turn, split by modality, from a usage_metadata message."""
    out = {"prompt_tokens": um.prompt_token_count or 0, "response_tokens": um.response_token_count or 0,
           "text_in": 0, "audio_in": 0, "text_out": 0, "audio_out": 0}
    for details, kind in ((um.prompt_tokens_details, "in"), (um.response_tokens_details, "out")):
        for d in details or []:
            modality = str(getattr(d, "modality", "") or "").upper()
            key = ("audio" if "AUDIO" in modality else "text") + "_" + kind
            out[key] += d.token_count or 0
    # Repeated prompt text that the provider billed at its cheap cached rate (OpenAI only;
    # Gemini Live has no such discount, which is why its text input dominates a call's cost).
    out["cached_text_in"] = int(getattr(um, "cached_text_in", 0) or 0)
    out["cached_audio_in"] = int(getattr(um, "cached_audio_in", 0) or 0)
    # Sarvam bills its speech services by the second of the customer's audio and the character of the agent's.
    for key in ("stt_seconds", "tts_chars"):
        if getattr(um, key, 0):
            out[key] = getattr(um, key)
    return out


def usage_cost_usd(usage: dict[str, Any], prices: dict[str, float] | None = None) -> float:
    return sum(usage.get(k, 0) * price / 1_000_000 for k, price in (prices or GEMINI_PRICE_PER_M).items())


# Providers that just failed for a reason retrying cannot fix, and the moment they may be tried
# again. Shared by every call on this server run.
_provider_down: dict[str, float] = {}


def gemini_is_down() -> bool:
    return time.monotonic() < _provider_down.get("gemini", 0.0)


def can_fall_back(model: str, exc: BaseException, exhausted: bool = False) -> bool:
    """Should this call leave Gemini for OpenAI? Only when Gemini cannot take the call at all:
    out of credit, key refused, or (exhausted) it has already failed to reconnect the allowed
    number of times. A single internal error is a blip and reconnecting to Gemini fixes it; on
    call 79 the same "1011 internal error" came back on every reconnect and the call was lost."""
    if not OPENAI_FALLBACK or openai_bridge.is_openai_model(model) or sarvam_bridge.is_sarvam_model(model):
        return False
    return exhausted or is_billing_error(exc) or "api key" in str(exc).lower() or "permission" in str(exc).lower()


SendAudio = Callable[[bytes], Awaitable[None]]
SendEvent = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class CallState:
    call_id: int
    call_type: CallType = DEFAULT_CALL_TYPE   # what the call is for: its results, checks and tools
    started: float = field(default_factory=time.monotonic)
    transcript: list[dict[str, str]] = field(default_factory=list)
    outcome: str | None = None
    outcome_details: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    _turn_usage: dict[str, int] | None = None   # latest usage report for the turn in progress
    ended: bool = False
    speaking: bool = False
    idle_since: float | None = None     # when the agent last finished a turn
    loud_since: float | None = None     # first clear customer sound after that
    nudges: int = 0
    address_refusals: int = 0           # record_address sent back because the digits did not match
    address: dict[str, str] | None = None   # what record_address accepted
    inaudible_streak: int = 0           # customer replies in a row that carried no words
    inaudible_total: int = 0
    held: bool | None = None            # the reply in progress answers a turn with no words and is not played (None: not decided yet)
    unheard: bool = False               # a reply was held back and the agent has not been told yet
    not_caller: bool = False            # the voice fingerprint says this turn was someone else, not the customer
    answering: str = "note"             # what the next reply answers: the customer's "speech" or a "note" (phone only)
    noise_told: bool = False            # the phone line was told this turn carried no words
    confirm_refusals: int = 0           # mark_outcome "confirmed" sent back for want of a clear yes
    denial_refusals: int = 0            # mark_outcome "cancelled" / "not_ordered" sent back for want of a no
    reason_refused: bool = False        # mark_outcome "not_interested" sent back once because why was never asked
    probe_refused: bool = False         # a call type's probe_first result sent back once: nothing asked after the opening
    help_refused: bool = False          # an abandoned cart's needs_help sent back once: the customer had said yes
    later_refused: bool = False         # call_later sent back once: the customer never asked for a call back
    last_tool_at: float = 0.0           # when the agent last called a tool (a reply is on its way after it)
    away_refused: bool = False          # not_interested sent back once: the person named is not on the line
    goodbye_refusals: int = 0           # end_call sent back because the goodbye was never said
    record_pending: bool = False        # a mark_outcome was sent back and nothing has been recorded since
    end_refusals: int = 0               # end_call refused while record_pending (at most twice a call)
    loose_address_refusals: int = 0     # mark_outcome sent back for a new_address that record_address never took
    question_refusals: int = 0          # end_call sent back because it came with an unanswered question
    goodbye_at: float | None = None     # the closing line was heard but the agent did not hang up (call 135)
    ask_bye_at: float | None = None     # a question and the goodbye in one turn: waiting for the answer (call 176)
    address_yes_refusals: int = 0       # a positive result sent back: the address question got no clear yes (call 135)
    order_refusals: int = 0             # "already_ordered" sent back: no clear yes to the check, or the order was being taken
    address_refusals: int = 0           # mark_outcome sent back because the address on record was never asked about
    say_back_refusals: int = 0          # mark_outcome sent back because a mismatch was never said back
    coupon: dict | None = None          # the coupon this cart call may offer once (coupons.py), or None
    coupon_refused: bool = False        # "not_interested" sent back once: the coupon offer was never made
    coupon_yes_refusals: int = 0        # "wants_coupon" sent back: no clear yes to the offer
    offer_wait_refused: bool = False    # a result sent back once: it came in the same breath as the coupon offer
    later_cancel_refused: bool = False  # "confirmed" sent back once: asked to cancel, then "baad mein" to keeping it
    hold_since: float | None = None     # the network's "your call is on hold" message is playing (machine_message)
    language: str = "hi-IN"             # the language the customer asked to be spoken to in (LANGUAGES)
    reconnects: int = 0                 # reconnects to the current provider (reset when the call changes provider)
    drops: int = 0                      # times the line dropped and the call carried on (any provider)
    error: str | None = None
    _in_buf: str = ""
    _out_buf: str = ""
    speech_until: float = 0.0                       # when the audio already sent finishes playing
    provider: str = "gemini"                        # which voice agent answered this call
    model: str = ""                                 # ...and its model, as it is now (it can switch mid-call)
    prices: dict[str, float] = field(default_factory=lambda: GEMINI_PRICE_PER_M)

    def add_input_text(self, text: str) -> None:
        self._in_buf += text

    def add_output_text(self, text: str) -> None:
        self._out_buf += text

    def note_usage(self, um: Any) -> None:
        # Gemini reports a turn's usage as a running total, so keep the latest and add it up
        # once the turn is over (flush_turn). Each turn's prompt count is the whole conversation
        # so far, and each turn is billed, which is why long calls cost far more than short ones.
        self._turn_usage = turn_usage(um)

    def close_usage_turn(self) -> None:
        if not self._turn_usage:
            return
        total = self.usage or {"turns": 0}
        total["turns"] = total.get("turns", 0) + 1
        for k, v in self._turn_usage.items():
            total[k] = total.get(k, 0) + v
        total["total_tokens"] = total["prompt_tokens"] + total["response_tokens"]
        total["cost_usd"] = round(usage_cost_usd(total, self.prices), 5)
        self.usage = total
        self._turn_usage = None

    def recent_customer_turns(self, n: int = 2) -> list[str]:
        """The last n things the customer said, latest last (the unfinished turn included)."""
        said = [t["text"] for t in self.transcript if t["role"] == "customer"]
        if self._in_buf.strip():
            said.append(self._in_buf.strip())
        return said[-n:]

    def flush_turn(self) -> None:
        self.close_usage_turn()
        if self._in_buf.strip():
            self.transcript.append({"role": "customer", "text": self._in_buf.strip()})
        if clean_agent_text(self._out_buf):
            self.transcript.append({"role": "agent", "text": clean_agent_text(self._out_buf)})
        self._in_buf = ""
        self._out_buf = ""


def address_text(a: dict[str, str]) -> str:
    """An address record_address accepted, as one line for the team."""
    return ", ".join(x for x in (a["house"] and f"house number {a['house']}", a["area"], a.get("landmark", ""),
                                 a["city"] if a["city"] != a["area"] else "",
                                 f"PIN {a['pin']}" if a["pin"] else "PIN not given") if x)


def read_back_confirmed(state: "CallState", parts: dict[str, str]) -> bool:
    """The agent's last turn read this address back as a question and the customer's latest words are a yes."""
    last_agent = next((t["text"] for t in reversed(state.transcript) if t["role"] == "agent"), "")
    said = state.recent_customer_turns(1)
    if not last_agent.rstrip().endswith("?") or not said or not said_yes(said[-1]):
        return False
    house = spoken_digits(parts["house"])
    return bool((house and house in spoken_digits(last_agent))
                or any(p and p.lower() in last_agent.lower() for p in (parts["area"], parts["city"])))


def build_config(system_prompt: str, voice: str = GEMINI_VOICE,
                 sensitive_listening: bool = False, fast: bool = True,
                 manual_vad: bool = False, language_hints: bool = False,
                 call_type: CallType = DEFAULT_CALL_TYPE) -> types.LiveConnectConfig:
    extra: dict[str, Any] = {}
    heard = types.AudioTranscriptionConfig(language_codes=TRANSCRIPT_LANGUAGES) if language_hints \
        else types.AudioTranscriptionConfig()
    if manual_vad:
        # The audio source marks speech itself with ACTIVITY_START / ACTIVITY_END.
        detection = types.AutomaticActivityDetection(disabled=True)
    else:
        detection = types.AutomaticActivityDetection(
            disabled=False,
            # LOW = a sound must be clearly speech before it counts as the customer talking.
            # HIGH made line noise interrupt the agent mid-sentence on real phone calls.
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            prefix_padding_ms=200,
            silence_duration_ms=500 if sensitive_listening else 600,
        )
    if AFFECTIVE_DIALOG:
        # Lets the model match the caller's tone and emotion instead of a flat read.
        extra["enable_affective_dialog"] = True
    if fast and GEMINI_THINKING_BUDGET >= 0:
        # The model otherwise "thinks" for about 3 seconds before every reply. On a phone call
        # that silence is worse than a slightly less considered sentence.
        extra["thinking_config"] = types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET)
    return types.LiveConnectConfig(
        **extra,
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=gemini_voice(voice))
            )
        ),
        tools=tools_for(call_type),
        input_audio_transcription=heard,
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=detection),
    )


async def run_call(
    *,
    contact: dict | None,
    system_prompt: str,
    channel: str,
    incoming_audio: "asyncio.Queue[bytes | None]",
    send_audio: SendAudio,
    send_event: SendEvent,
    model: str = GEMINI_LIVE_MODEL,
    voice: str = GEMINI_VOICE,
    sensitive_listening: bool = False,
    manual_vad: bool = False,
    start_gate: "asyncio.Event | None" = None,
    meta: dict | None = None,
) -> CallState:
    """Run one full conversation. Returns when the agent ends the call or the source closes.

    start_gate: for phone calls. We connect to Gemini while the phone is still ringing and
    only start the greeting when the gate opens (the customer picked up), so the greeting
    is not delayed by connection setup. Waiting connected costs nothing.
    """
    if model in _refused_models:
        log.warning("%s refused an earlier session on this run; using %s for this call instead", model, GEMINI_LIVE_MODEL)
        model = GEMINI_LIVE_MODEL
    if gemini_is_down() and not openai_bridge.is_openai_model(model) and not sarvam_bridge.is_sarvam_model(model):
        left = _provider_down["gemini"] - time.monotonic()
        log.warning("Gemini failed recently; this call goes straight to %s (retrying Gemini in %.0f min)",
                    OPENAI_REALTIME_MODEL, left / 60)
        model = OPENAI_REALTIME_MODEL
    call_id = db.start_call(contact["id"] if contact else None, channel)
    state = CallState(call_id=call_id, call_type=call_type_for(contact), model=model)
    if contact and state.call_type.offers_coupon:
        state.coupon = call_coupon(contact)   # the one the prompt was built with (attached when the call was placed)
    if openai_bridge.is_openai_model(model):
        state.provider, state.prices = "openai", OPENAI_PRICE_PER_M
    elif sarvam_bridge.is_sarvam_model(model):
        state.provider, state.prices = "sarvam", SARVAM_PRICE_PER_M
    if meta is not None:
        meta["provider"] = state.provider   # the phone bridge handles barge-in differently for OpenAI and Sarvam
    ACTIVE[call_id] = {"state": state, "contact": contact or {}, "channel": channel, "gate": start_gate,
                       "meta": meta or {}, "dialed_at": time.monotonic(), "voice": voice}
    client = make_client()
    fast_modes = [True, False] if GEMINI_THINKING_BUDGET >= 0 else [False]
    await send_event({"type": "status", "text": f"connecting to {model}", "call_id": call_id})

    OPENING = "[The customer has answered the call. Say your first turn now (THE USUAL ORDER, step 1), then wait for their reply.]"

    def may_hold_back() -> bool:
        """Can the reply being prepared be held back if the customer's turn carried no words? Only on the
        phone with Gemini: its transcript of the customer always arrived before its reply (111 of 111 turns
        on calls 35 to 72). OpenAI writes the customer's words after its reply has begun. Sarvam replies to the
        transcript, so it always comes first."""
        return manual_vad and state.provider in ("gemini", "sarvam") and state.answering == "speech"

    def reply_begins() -> None:
        """The model's first output of a new turn (audio, text or a tool call): decide, once, whether
        the customer hears it."""
        if state.held is not None:
            return
        said = state._in_buf.strip()
        if manual_vad and state.answering == "speech" and state.not_caller:
            # Someone else near the phone: their words get no answer, and the customer, who said nothing, is told nothing.
            state.held, state.unheard = True, True
            log.info("call %s: that turn was another voice, not the customer (%r); holding back the reply to it",
                     call_id, said[:40])
            return
        if may_hold_back() and said and machine_message(said):
            # The network's recorded voice (hold, voicemail): nothing to answer. Sarvam never wrote a reply to it.
            state.held, state.unheard = True, state.provider != "sarvam"
            log.info("call %s: that turn was a recorded message (%r); holding back the reply to it", call_id, said[:60])
            return
        state.held = may_hold_back() and bool(said) and only_noise(said)
        if not state.held:
            if said and may_hold_back():
                state.inaudible_streak = 0
            state.unheard = False   # the customer hears this reply, so an earlier held-back one no longer matters
            return
        state.inaudible_streak += 1
        state.inaudible_total += 1
        state.unheard = True
        log.info("call %s: the customer's turn carried no words (%r, %s in a row); holding back the reply to it",
                 call_id, said[:40], state.inaudible_streak)
        note = INAUDIBLE_NOTES.get(min(state.inaudible_streak, max(INAUDIBLE_NOTES)))
        if note:
            incoming_audio.put_nowait(Nudge(note))   # also cuts the held-back reply short

    async def hang_up_after_goodbye() -> None:
        """The closing line was heard and no end_call came with it: end the call ourselves, a moment
        after the goodbye has finished playing, whether or not the agent records a result in between."""
        await asyncio.sleep(max(0.0, state.speech_until - time.monotonic()) + GOODBYE_HANGUP_SECONDS)
        if not state.ended:
            log.info("call %s: no end_call after the goodbye; hanging up", call_id)
            state.ended = True
            incoming_audio.put_nowait(None)   # stops the session; the phone line hangs up after it

    async def hang_up_if_no_answer() -> None:
        """A question and the goodbye came in one turn: give the customer a moment to answer it, else hang up."""
        mark = state.ask_bye_at
        await asyncio.sleep(max(0.0, state.speech_until - time.monotonic()) + ASK_BYE_WAIT_SECONDS)
        if not state.ended and state.ask_bye_at is not None and state.ask_bye_at == mark:
            log.info("call %s: no answer to the question asked with the goodbye; hanging up", call_id)
            state.goodbye_at = time.monotonic()   # a finished call, not a dropped one
            state.ended = True
            incoming_audio.put_nowait(None)

    def end_unreached(outcome: str, note: str) -> None:
        """Nobody to talk to on the line (a recorded message, a hold nobody came back from): hang up, ring again later."""
        if state.ended:
            return
        if not state.outcome:
            state.outcome, state.outcome_details = outcome, {"outcome": outcome, "notes": note}
        log.info("call %s: %s; hanging up", call_id, note)
        state.ended = True
        incoming_audio.put_nowait(None)   # stops the session; the phone line hangs up after it

    async def hang_up_if_still_on_hold(mark: float) -> None:
        await asyncio.sleep(HOLD_HANGUP_SECONDS)
        if state.hold_since == mark:
            end_unreached("busy", f"customer put the call on hold and did not come back within {HOLD_HANGUP_SECONDS:.0f} s; "
                                  "to be called again")

    def customer_words(text: str) -> None:
        """The customer's transcript so far this turn: a recorded message on the line, the customer back from hold, or
        the customer asking for another language."""
        if (code := asked_language(text)) and code != state.language and not machine_message(text):
            log.info("call %s: the customer asked for %s; the agent speaks it from now on", call_id, LANGUAGES[code][0])
            state.language = code
        kind = machine_message(text)
        if kind == "recorded":
            end_unreached("no_answer", f"a recorded message answered ({text.strip()[:80]!r}): voicemail, call screening or "
                                       "the network; to be called again")
        elif kind == "hold":
            if state.hold_since is None:
                state.hold_since = time.monotonic()
                log.info("call %s: the call is on hold; waiting quietly, hanging up after %.0f s", call_id, HOLD_HANGUP_SECONDS)
                asyncio.create_task(hang_up_if_still_on_hold(state.hold_since))
        elif state.hold_since is not None and not heard_nothing(text):
            log.info("call %s: the customer is back from hold", call_id)
            state.hold_since = None

    def drop_held_reply() -> None:
        """A held-back reply is over: keep it out of the transcript (nobody heard it), but in the log."""
        text = clean_agent_text(state._out_buf)
        if text:
            log.info("call %s: not played (it answered a sound that was not the customer): %s", call_id, text[:200])
        state._out_buf = ""

    async def session_once(prompt: str, opening: str, first: bool) -> None:
        state.held, state.unheard, state.answering, state.noise_told = None, False, "note", False
        async with contextlib.AsyncExitStack() as stack:
            global _hints_ok
            session = None
            # Optional settings a model may refuse. Try the full set first, then drop them one by one.
            attempts = [(fast, hints) for fast in fast_modes
                        for hints in ([True, False] if TRANSCRIPT_LANGUAGES and _hints_ok else [False])]
            if openai_bridge.is_openai_model(model):
                # OpenAI Realtime wearing the same session interface (app/openai_bridge.py), so
                # everything below this line is shared with Gemini.
                session = await stack.enter_async_context(openai_bridge.connect(
                    model=model, prompt=prompt, voice=voice, label=f"call {call_id}", manual_vad=manual_vad,
                    tools=tools_for(state.call_type),
                    greeting=state.call_type.greeting(contact or {}, voice) if state.call_type.greeting else ""))
                attempts = []
            elif sarvam_bridge.is_sarvam_model(model):
                # Sarvam's three services wearing the same session interface (app/sarvam_bridge.py).
                session = await stack.enter_async_context(sarvam_bridge.connect(
                    prompt=prompt, voice=voice, label=f"call {call_id}", manual_vad=manual_vad,
                    tools=tools_for(state.call_type),
                    greeting=state.call_type.greeting(contact or {}, voice) if state.call_type.greeting else ""))
                attempts = []
            for n, (fast, hints) in enumerate(attempts):
                try:
                    session = await stack.enter_async_context(client.aio.live.connect(
                        model=model, config=build_config(prompt, voice, sensitive_listening, fast, manual_vad, hints, state.call_type)))
                    log.info("call %s: connected to %s via %s (fast replies %s, transcript language hint %s)",
                             call_id, model, "Vertex AI" if GEMINI_VERTEX else "Gemini API",
                             "on" if fast else "off", "on" if hints else "off")
                    break
                except Exception as exc:  # noqa: BLE001
                    if n == len(attempts) - 1 or is_billing_error(exc):
                        raise
                    if hints:
                        _hints_ok = False   # do not waste a connection attempt on every later call
                        log.warning("call %s: transcript language hint was refused (%s); retrying without it", call_id, exc)
                    else:
                        log.warning("call %s: fast mode was refused (%s); retrying with model defaults", call_id, exc)
            await send_event({"type": "status", "text": "connected"})

            if start_gate is not None and first:
                await start_gate.wait()
                state.started = time.monotonic()   # measure the call from pick-up, not from dialing

            # Ask the agent to open the call. Phone calls start with the agent speaking.
            # A completed turn makes the agent greet at once. (A realtime text message would
            # wait for the microphone to go quiet first, which never happens in a noisy room.)
            if model.startswith("gemini-2.") or openai_bridge.is_openai_model(model) or sarvam_bridge.is_sarvam_model(model):
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=opening)]),
                    turn_complete=True,
                )
            else:
                # Newer live models close the session with "1007 invalid argument" on a
                # client_content turn. They take text on the realtime channel instead; with our own
                # speech detection the text has to sit inside a start/end pair to count as a turn.
                if manual_vad:
                    await session.send_realtime_input(activity_start=types.ActivityStart())
                await session.send_realtime_input(text=opening)
                if manual_vad:
                    await session.send_realtime_input(activity_end=types.ActivityEnd())
            log.info("call %s: opening sent, voice=%s model=%s", call_id, voice, model)

            async def pump_in() -> None:
                in_activity = False   # manual_vad: between ACTIVITY_START and ACTIVITY_END
                while True:
                    chunk = await incoming_audio.get()
                    if chunk is None or state.ended:
                        return
                    if chunk is SILENT_HANGUP:
                        if in_activity:
                            continue   # they started talking after all
                        if not state.outcome and state.hold_since is not None:
                            state.outcome = "busy"   # on hold, not lost: rung again later
                            state.outcome_details = {"outcome": "busy", "notes": "customer put the call on hold; to be called again"}
                        elif not state.outcome:
                            state.outcome = "unclear"
                            state.outcome_details = {"outcome": "unclear", "notes": "no response on the line"}
                        log.info("call %s: still silent after asking twice; hanging up with no goodbye", call_id)
                        state.ended = True
                        return
                    if chunk is ACTIVITY_START:
                        in_activity = True
                        state.answering, state.noise_told, state.not_caller = "speech", False, False
                        state.ask_bye_at = None   # they are answering the question asked with the goodbye
                        log.info("call %s: customer started talking at %.1fs", call_id, time.monotonic() - state.started)
                        await session.send_realtime_input(activity_start=types.ActivityStart())
                        if state.unheard:
                            # Their words follow a reply they never heard; say so inside the same turn.
                            state.unheard = False
                            await session.send_realtime_input(text=UNHEARD_NOTE.format(question=last_question(state.transcript)))
                        # Gemini only: ChatGPT took it as a line to say, whatever the customer had said (calls 155, 157).
                        if (state.provider == "gemini" and "not_interested" in state.call_type.outcomes
                                and state.call_type.main_question
                                and re.search(state.call_type.main_question, last_question(state.transcript), re.IGNORECASE)
                                and not any(t["role"] == "agent" and _ASKED_WHY.search(t["text"]) for t in state.transcript)):
                            log.info("call %s: reason not asked yet; reminding the agent to ask it before any goodbye", call_id)
                            await session.send_realtime_input(text=ASK_WHY_NOTE)
                        continue
                    if chunk is NOT_CALLER:
                        state.not_caller = True
                        continue
                    if chunk is ACTIVITY_END:
                        if in_activity:   # a marker left over from before a reconnect has no start
                            in_activity = False
                            log.info("call %s: customer stopped talking at %.1fs", call_id, time.monotonic() - state.started)
                            hint = closing_hint(state)
                            if state.provider == "openai":   # Sarvam adds the language note itself (sarvam_bridge)
                                hint += language_note(state.language)
                            if hint:
                                log.info("call %s: this reply gets its own instructions: %s", call_id, hint.strip()[:90])
                                session.next_hint = hint
                            await session.send_realtime_input(activity_end=types.ActivityEnd())
                            if meta is not None:   # for the phone line's per-turn timing line
                                meta.update(started=state.started, turn_sent=time.monotonic(), first_event=None)
                        continue
                    if isinstance(chunk, Nudge):
                        # A note from the phone line ("noisy for 8 s, nothing understood"). With
                        # our own speech detection a text turn has to sit inside start/end marks.
                        if state.goodbye_at is not None and chunk is not GOODBYE_NOTE:
                            continue   # the call is over: no "hello ji, meri awaaz aa rahi hai?" after the goodbye
                        if state.hold_since is not None:
                            continue   # on hold: no "awaaz aa rahi hai?" into the hold music (call 302)
                        if in_activity:
                            continue   # the customer is mid-sentence; that audio matters more
                        if state.ask_bye_at is not None:
                            continue   # goodbye already said: wait quietly for the answer, or hang up
                        if chunk is not GOODBYE_NOTE and state.speaking and time.monotonic() < state.speech_until + 10:
                            # Call 170: OpenAI paused mid-reply, the line looked quiet for 3 s, and the nudge (sent as
                            # "customer started talking") cancelled the rest of the agent's sentence.
                            continue
                        note = str(chunk)
                        if "heard nothing from you for" in note and time.monotonic() - state.last_tool_at < 4:
                            # Call 195: the reply was late because of a tool round trip, not stuck; the note collided with it.
                            log.info("call %s: late-reply note skipped; a tool call is being answered", call_id)
                            continue
                        if "meri awaaz aa rahi hai" in note and not state.record_pending:
                            heard = next((t["text"] for t in reversed(state.transcript) if t["role"] == "agent"), "")
                            if state.outcome:
                                # Call 191: the result was recorded, the goodbye still to come, and the 3 s silence check
                                # had the agent ask "hello ji, meri awaaz aa rahi hai?" instead of saying it.
                                note = ("[Note from the system: the result is recorded and the customer is waiting for you. "
                                        "Do not ask whether they can hear you. Say only this, nothing before or after it: "
                                        f"'{closing_line(state, state.outcome)}' Then end_call.]")
                            elif heard and "?" not in heard and _GOODBYE.search(heard) is None:
                                # Their silence answers a turn that asked them nothing: there is nothing to say "hello" about.
                                note = ("[Note from the phone line: nothing has been heard from the customer for a few seconds, "
                                        "and your last turn asked them nothing, so they are waiting for you. Do not ask whether "
                                        "they can hear you. Ask your question now, in one short sentence; if the call is "
                                        "finished, say the goodbye instead.]")
                        if state.unheard:
                            state.unheard = False
                            note = UNHEARD_NOTE.format(question=last_question(state.transcript)) + " " + note
                        state.answering = "note"
                        log.info("call %s: nudging the agent: %s", call_id, note)
                        if manual_vad:
                            await session.send_realtime_input(activity_start=types.ActivityStart())
                        await session.send_realtime_input(text=note)
                        if manual_vad:
                            await session.send_realtime_input(activity_end=types.ActivityEnd())
                        continue
                    if manual_vad and isinstance(chunk, bytes):
                        if in_activity:
                            await session.send_realtime_input(
                                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
                        continue
                    if isinstance(chunk, str):
                        # Typed customer reply from the test page (no microphone needed).
                        state.transcript.append({"role": "customer", "text": chunk})
                        customer_words(chunk)
                        if state.ended:
                            return
                        # Text goes on the realtime channel; mixing client_content turns with a
                        # live audio stream makes native-audio models reject the session.
                        await session.send_realtime_input(audio_stream_end=True)
                        await session.send_realtime_input(text=chunk)
                        log.info("call %s: typed reply sent", call_id)
                        continue
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
                    # Reply watchdog. Gemini answers only after it hears the customer pause. On a
                    # noisy line that pause may never be detected, and the agent sits silent while
                    # the customer keeps repeating themselves. If the customer has clearly spoken
                    # and nothing has come back for a few seconds, mark a pause ourselves.
                    if state.idle_since is not None and not state.speaking:
                        now = time.monotonic()
                        if state.loud_since is None:
                            if audioop.rms(chunk, 2) > 700:
                                state.loud_since = now
                        elif now - state.loud_since > REPLY_WATCHDOG_SECONDS:
                            state.nudges += 1
                            state.loud_since = None
                            log.info("call %s: customer spoke but no reply for %.0fs; marking a pause (nudge %s)",
                                     call_id, REPLY_WATCHDOG_SECONDS, state.nudges)
                            await session.send_realtime_input(audio_stream_end=True)

            async def pump_out() -> None:
                # session.receive() yields ONE model turn and then stops,
                # so keep calling it until the call is over.
                while not state.ended:
                    async for msg in session.receive():
                        if state.ended:
                            return
                        if meta is not None and meta.get("turn_sent") and not meta.get("first_event"):
                            meta["first_event"] = time.monotonic()
                        if msg.usage_metadata is not None:
                            state.note_usage(msg.usage_metadata)
                        sc = msg.server_content
                        if sc is not None:
                            if sc.interrupted:
                                state.speaking = False
                                state.idle_since = time.monotonic()
                                state.loud_since = None
                                state.close_usage_turn()   # a cut-off turn is still billed
                                if state.held:
                                    drop_held_reply()      # none of it was played, so there is nothing to cut
                                else:
                                    state.speech_until = 0.0   # that audio is dropped, not played
                                    log.info("call %s: customer interrupted", call_id)
                                    await send_event({"type": "interrupted"})
                            if sc.input_transcription is not None and sc.input_transcription.text:
                                if not state._in_buf:
                                    log.info("call %s: customer speech detected", call_id)
                                state.add_input_text(sc.input_transcription.text)
                                customer_words(state._in_buf)
                                await send_event({"type": "transcript", "role": "customer",
                                                  "text": sc.input_transcription.text, "partial": True})
                                if may_hold_back() and not state.noise_told and only_noise(state._in_buf):
                                    # No words in it: the reply to it will be held back, so the phone line
                                    # can stop waiting for one.
                                    state.noise_told = True
                                    await send_event({"type": "heard_nothing", "first": state.inaudible_streak == 0})
                            if sc.output_transcription is not None and sc.output_transcription.text:
                                reply_begins()
                                state.add_output_text(sc.output_transcription.text)
                                if not state.held and state.goodbye_at is None:
                                    await send_event({"type": "transcript", "role": "agent",
                                                      "text": sc.output_transcription.text, "partial": True})
                            if sc.model_turn is not None:
                                for part in sc.model_turn.parts or []:
                                    if part.inline_data is not None and part.inline_data.data:
                                        reply_begins()
                                        if state.held or state.goodbye_at is not None:
                                            continue   # not played: unheard noise, or after the goodbye
                                        if not state.speaking:
                                            state.speaking = True
                                            state.idle_since = None
                                            state.loud_since = None
                                            log.info("call %s: agent started speaking at %.1fs", call_id, time.monotonic() - state.started)
                                        # How long this audio takes to speak. OpenAI hands over a
                                        # whole sentence in a fraction of the time it takes to say
                                        # it, so "the model has stopped" is not "the customer has
                                        # heard it" and the goodbye was being cut off.
                                        state.speech_until = max(state.speech_until, time.monotonic()) + \
                                            len(part.inline_data.data) / (OUTPUT_SAMPLE_RATE * 2)
                                        await send_audio(part.inline_data.data)
                            if sc.turn_complete:
                                state.speaking = False
                                state.idle_since = time.monotonic()
                                state.loud_since = None
                                if state.held:
                                    drop_held_reply()
                                    log.info("call %s: held-back reply finished; still waiting for the customer's answer", call_id)
                                else:
                                    log.info("call %s: agent turn complete, now waiting for the customer", call_id)
                                    if state.goodbye_at is not None:
                                        text = clean_agent_text(state._out_buf)
                                        if text:
                                            log.info("call %s: not played (after the goodbye): %s", call_id, text[:200])
                                        state._out_buf = ""
                                    elif (not state.ended and not state.record_pending   # a sent-back result: let it fix that
                                          and turn_ends_call(clean_agent_text(state._out_buf), state)
                                          # Call 176: "koi dikkat aayi? ... dhanyavaad ji, aapka din shubh ho" in one turn;
                                          # the customer's answer to that question must still be heard.
                                          and "?" not in clean_agent_text(state._out_buf)):
                                        state.goodbye_at = time.monotonic()
                                        log.info("call %s: goodbye said without end_call; %shanging up %.0fs after it finishes "
                                                 "at the latest", call_id, "" if state.outcome else "asking for the result, ",
                                                 GOODBYE_HANGUP_SECONDS)
                                        if not state.outcome:   # call 138: recorded already, then asked and recorded again
                                            incoming_audio.put_nowait(GOODBYE_NOTE)
                                        asyncio.create_task(hang_up_after_goodbye())
                                    elif (not state.ended and _FINAL_GOODBYE.search(clean_agent_text(state._out_buf))
                                          and "?" in clean_agent_text(state._out_buf)):
                                        state.ask_bye_at = time.monotonic()
                                        log.info("call %s: a question and the goodbye in one turn; waiting %.0fs for the answer",
                                                 call_id, ASK_BYE_WAIT_SECONDS)
                                        asyncio.create_task(hang_up_if_no_answer())
                                state.held, state.noise_told = None, False
                                # The pause that closes the customer's next turn follows the last question
                                # they actually heard (a held-back reply leaves nothing of its own).
                                long_answer = expects_long_answer(state._out_buf or last_question(state.transcript))
                                state.flush_turn()
                                await send_event({"type": "turn_complete", "long_answer": long_answer})
                        if msg.tool_call is not None:
                            reply_begins()
                            state.last_tool_at = time.monotonic()
                            responses = []
                            for fc in msg.tool_call.function_calls or []:
                                result = await handle_tool(fc.name, dict(fc.args or {}), state, send_event)
                                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
                            await session.send_tool_response(function_responses=responses)
                            if state.ended:
                                # Wait for the goodbye to actually finish in the customer's ear,
                                # not merely for the model to stop producing it. Capped so a
                                # runaway reply cannot hold the line open.
                                # Owner, 7 Oct 2026: cut the call the moment the goodbye has played (no extra margin;
                                # the phone side waits for its own playback to finish).
                                left = min(20.0, max(0.0, state.speech_until - time.monotonic()))
                                log.info("call %s: waiting %.1fs for the goodbye to finish", call_id, left)
                                await asyncio.sleep(left)
                                return

            in_task = asyncio.create_task(pump_in())
            out_task = asyncio.create_task(pump_out())
            done, pending = await asyncio.wait({in_task, out_task}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc

    def drain_stale_audio() -> bool:
        """Drop audio that queued up while we were reconnecting. Returns False if the line has closed."""
        alive = True
        while not incoming_audio.empty():
            if incoming_audio.get_nowait() is None:
                alive = False
        if not alive:
            incoming_audio.put_nowait(None)
        return alive

    try:
        prompt, opening = system_prompt, OPENING
        while True:
            try:
                await session_once(prompt, opening, state.drops == 0)
                break
            except Exception as exc:  # noqa: BLE001
                # The connection dropped (network blip, Gemini "1011 internal error"). The customer
                # is still on the line, so reconnect and carry on instead of going silent and
                # hanging up. Gemini has failed twice in one call, so allow a few tries.
                alive = drain_stale_audio()
                saying = clean_agent_text(state._out_buf)   # the reply that died with the line, for the log
                exhausted = state.reconnects >= MAX_RECONNECTS
                switching = alive and not state.ended and can_fall_back(model, exc, exhausted)
                if switching:
                    # Gemini cannot take this call. Rather than hang up on a customer who is still
                    # holding the phone, carry on with the other provider.
                    if not exhausted:
                        # Out of credit or key refused: every later call would fail the same way.
                        # Repeated internal errors are about this call; the next one may be fine.
                        _provider_down["gemini"] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS
                    log.warning("call %s: Gemini cannot take this call (%s%s) while the agent was saying %r; switching to %s",
                                call_id, exc, f"; {state.reconnects} reconnects failed" if exhausted else "", saying,
                                OPENAI_REALTIME_MODEL)
                    model = state.model = OPENAI_REALTIME_MODEL
                    state.provider, state.prices = "openai", OPENAI_PRICE_PER_M
                    state.reconnects = 0   # the new provider gets its own tries
                    if meta is not None:
                        meta["provider"] = "openai"   # the phone line cuts barge-ins itself for OpenAI
                elif state.ended or exhausted or is_billing_error(exc) or not alive:
                    raise
                else:
                    state.reconnects += 1
                    log.warning("call %s: connection to %s dropped (%s: %s) while the agent was saying %r; reconnecting (%s of %s)",
                                call_id, state.provider, type(exc).__name__, exc, saying, state.reconnects, MAX_RECONNECTS)
                    if ("1007" in str(exc) and model != GEMINI_LIVE_MODEL and not openai_bridge.is_openai_model(model)
                            and not sarvam_bridge.is_sarvam_model(model)):
                        # "Invalid argument" is not a blip: this model refuses how we talk to it, and
                        # would refuse again. Carry the call on the default model instead of dropping it.
                        log.warning("call %s: %s refused the session; switching to %s", call_id, model, GEMINI_LIVE_MODEL)
                        _refused_models.add(model)   # later calls skip it at once instead of greeting 3 s late
                        model = state.model = GEMINI_LIVE_MODEL
                state.drops += 1
                state.speaking, state.speech_until = False, 0.0   # that reply died with the line
                if state.held:
                    drop_held_reply()
                state.flush_turn()
                if not state.transcript:
                    continue   # nothing was said yet: greet normally, there is no break to apologise for
                so_far = "\n".join(f"{'You' if t['role'] == 'agent' else 'Customer'}: {t['text']}" for t in state.transcript) or "(nothing yet)"
                finished = "If the conversation above was already finished, just say a short goodbye, then call mark_outcome and end_call."
                if state.drops == 1:
                    prompt = (system_prompt + "\n\nTHE LINE DROPPED FOR A MOMENT AND IS BACK. Conversation so far:\n" + so_far +
                              "\nDo not greet or introduce yourself again. Say one short apology for the break "
                              "(for example: 'sorry ji, awaaz cut gayi thi') and continue from where you stopped. " + finished)
                    opening = "[The line is back. Apologise briefly for the break and continue.]"
                else:
                    # Call 79: four drops in a row, and the customer heard "sorry ji, awaaz cut gayi thi" four times.
                    prompt = (system_prompt + "\n\nTHE LINE DROPPED AGAIN AND IS BACK. Conversation so far:\n" + so_far +
                              "\nDo not greet or introduce yourself again, and do NOT apologise again: the customer has already "
                              "heard 'sorry ji, awaaz cut gayi thi'. Carry on from where you stopped, in one short sentence. " + finished)
                    opening = "[The line is back again. Do not apologise; carry on from where you stopped.]"
    except Exception as exc:  # noqa: BLE001
        if is_billing_error(exc):
            # The agent that refused, not always Gemini: on 19 Oct 2026 Sarvam's credits ran out mid-campaign.
            name = {"sarvam": "Sarvam", "openai": "ChatGPT"}.get(state.provider, "Gemini")
            log.error("call %s failed: %s refused for billing reasons: %s", call_id, name, exc)
            state.error = f"{name} billing: {exc}"
        else:
            log.exception("call %s failed", call_id)
            state.error = f"{type(exc).__name__}: {exc}"
        await send_event({"type": "error", "text": state.error})
    finally:
        ACTIVE.pop(call_id, None)
        if state.held:
            drop_held_reply()
        state.flush_turn()
        duration = time.monotonic() - state.started
        log.info("call %s ended: %.0fs, outcome=%s, agent=%s (%s), watchdog nudges=%s, reconnects=%s",
                 call_id, duration, state.outcome, state.provider, model, state.nudges, state.reconnects)
        if state.usage:
            # Kept with the call so the two agents can be compared later from the database
            # alone, without digging through the log.
            state.usage["provider"], state.usage["model"] = state.provider, model
            u = state.usage
            log.info("call %s usage: %s turns, in: %s audio + %s text tokens, out: %s audio + %s text tokens, about $%.4f",
                     call_id, u.get("turns"), u.get("audio_in"), u.get("text_in"), u.get("audio_out"), u.get("text_out"),
                     u.get("cost_usd", 0))
        cause = str((meta or {}).get("hangup_cause") or "")
        result = ""
        if start_gate is not None and not start_gate.is_set():
            duration = 0.0
            if cause == "NO_CALLBACK" and not state.error:
                state.error = "Plivo never called back: the tunnel is probably down or PUBLIC_BASE_URL is stale"
            if state.error:
                result = f"not called: {state.error}"     # our failure; the customer was never rung
            else:
                state.outcome, result = classify_unanswered(cause)
        elif not state.outcome and (yes := yes_to_main_question(state.transcript, state.call_type.main_question)):
            # The customer said yes to the call's main question and the call ended before the result was
            # written (calls 88 and 92: confirmed orders saved as "dropped" and queued to be rung again).
            has_address = bool(state.address or (contact or {}).get("address"))
            if state.call_type.takes_address and not has_address:
                state.outcome = "unclear"
                notes = (f"customer said yes ({yes!r}) but the call ended before the delivery address was taken; "
                         "team to get the address")
            else:
                state.outcome = state.call_type.positive[0]
                notes = f"customer said yes ({yes!r}); the call ended before the goodbye"
            details = {**(state.outcome_details or {}), "outcome": state.outcome, "notes": notes}
            if state.address:
                details["new_address"] = address_text(state.address)
            state.outcome_details = details
            log.info("call %s: no result recorded, but the customer said yes to the main question; saved as %s",
                     call_id, state.outcome)
        elif not state.outcome and state.coupon and (yes := yes_to_coupon(state.transcript)):
            # A clear yes to the coupon offer, then the line went before the result was written: they still get it.
            state.outcome = "wants_coupon"
            state.outcome_details = {**(state.outcome_details or {}), "outcome": "wants_coupon",
                                     "coupon_code": state.coupon["code"],
                                     "notes": f"customer said yes to the coupon ({yes!r}); the call ended before the goodbye"}
            if (why := probe_answer(state.transcript)[:200]) and not bare_no(why):
                state.outcome_details["not_interested_reason"] = why
            log.info("call %s: no result recorded, but the customer said yes to the coupon; saved as wants_coupon", call_id)
        elif not state.outcome and (no := no_to_main_question(state.transcript, state.call_type.main_question)):
            # A clear no, then the line went: a finished call, not a dropped one to ring again (call 139).
            refused = "not_interested" in state.call_type.outcomes
            # Call 447 (owner, 19 Oct 2026): "nahi, mujhe woh nahi chahiye", then they hung up. A clear ask to cancel an
            # order is a cancellation, not "unclear".
            cancels = not refused and "cancelled" in state.call_type.outcomes and asked_to_cancel(state.transcript)
            state.outcome = "not_interested" if refused else "cancelled" if cancels else "unclear"
            result = f"customer said no ({no!r}) to the main question, then the call ended"
            complaints = [t["text"][:120] for t in state.transcript if t["role"] == "customer" and _COMPLAINT.search(t["text"])]
            if complaints:
                result += " (customer said: " + " | ".join(complaints[-3:]) + ")"
            state.outcome_details = {**(state.outcome_details or {}), "outcome": state.outcome, "notes": result,
                                     **({"not_interested_reason": "not given (call ended)"} if refused else {}),
                                     **({"cancel_reason": probe_answer(state.transcript) or "not given (call ended)"}
                                        if cancels else {})}
            log.info("call %s: no result recorded, but the customer said no to the main question; saved as %s",
                     call_id, state.outcome)
        elif not state.outcome and state.goodbye_at is not None:
            # We hung up after the goodbye and the agent never recorded a result: not a dropped call to
            # ring again, a finished one a person should check.
            state.outcome = "unclear"
            result = "the agent said goodbye without recording a result; team to check the call"
            state.outcome_details = {**(state.outcome_details or {}), "outcome": "unclear", "notes": result}
        elif not state.outcome:
            # Someone answered but the call ended with nothing recorded: the customer hung up or the
            # line dropped -> call back later. If the agent itself failed mid-way, a person looks first.
            state.outcome = "unclear" if state.error else "dropped"
            notes = (state.outcome_details or {}).get("notes") or (
                f"call failed mid-way: {state.error}" if state.error else "call ended without a result (customer hung up or line dropped)")
            state.outcome_details = {**(state.outcome_details or {}), "notes": notes}
            result = notes
        notes = str((state.outcome_details or {}).get("notes") or "")
        if state.outcome_details is not None and was_abusive(state.transcript) and "abusive" not in notes:
            state.outcome_details["notes"] = notes = f"{notes} (customer used abusive language)".strip()
        if state.outcome_details is not None and state.language != "hi-IN":
            # So the team knows to call this customer in their language (owner, 19 Oct 2026).
            state.outcome_details["notes"] = f"{notes} (customer asked to talk in {LANGUAGES[state.language][0]})".strip()
        if not result:
            result = (state.outcome or "").replace("_", " ")
            for key in state.call_type.summary_keys:
                if (state.outcome_details or {}).get(key):
                    result += f": {state.outcome_details[key]}"
                    break
        if state.outcome == "wants_coupon" and contact:
            # Owner, 19 Oct 2026: the coupon code and the product go to them by message (messaging.py), to the number
            # this call rang. Never stops the call being saved.
            try:
                code = str((state.outcome_details or {}).get("coupon_code") or "")
                coupon = state.coupon or (coupons.get_by_code(code) if code else None)
                message_id = messaging.queue_coupon(contact=contact, coupon=coupon, call_id=call_id,
                                                    to=str((meta or {}).get("to") or ""), channel=channel) if coupon else None
                if message_id:
                    state.outcome_details = {**(state.outcome_details or {}), "message_id": message_id}
                else:
                    log.warning("call %s: the customer wants the coupon, but no message was queued", call_id)
            except Exception:  # noqa: BLE001
                log.exception("call %s: could not queue the coupon message", call_id)
        db.finish_call(
            call_id,
            outcome=state.outcome,
            outcome_details=state.outcome_details,
            transcript=state.transcript,
            usage=state.usage,
            error=state.error,
            duration_sec=round(duration, 1),
            hangup_cause=cause or None,
            result=result[:200],
        )
        if contact and state.outcome in db.RETRY_STATUSES:
            # Into the call-back queue with a time. The campaign may overwrite it with its own
            # retry gap; a single call from the dashboard keeps this default.
            when = None
            if state.outcome == "call_later":
                when = parse_callback_time((state.outcome_details or {}).get("callback_time") or "")
            db.set_next_try(contact["id"], when or datetime.now(timezone.utc) + timedelta(minutes=DEFAULT_RETRY_MINUTES))
        await send_event({"type": "ended", "call_id": call_id, "outcome": state.outcome,
                          "duration_sec": round(duration, 1)})
    return state


DEFAULT_RETRY_MINUTES = 30


def parse_callback_time(text: str, now: datetime | None = None) -> datetime | None:
    """A time from what the customer said ("kal subah", "shaam ko 6 baje", "after 2 hours",
    "tomorrow evening"). None when nothing usable is in it."""
    t = (text or "").lower().strip()
    if not t:
        return None
    now = now or datetime.now(IST)
    now = now.astimezone(IST)
    day = now.date()
    hour: int | None = None
    if re.search(r"\bparso\b|day after", t):
        day += timedelta(days=2)
    elif re.search(r"\bkal\b|\bkl\b|tomorrow|अगले दिन|कल", t):
        day += timedelta(days=1)
    m = re.search(r"(\d+)\s*(?:ghante|ghanta|hour|hrs?|घंटे|घंटा)", t)
    if m and re.search(r"baad|after|later|में|mein|me\b", t):
        return now + timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*(?:minute|min|मिनट)", t)
    if m and re.search(r"baad|after|later|में|mein|me\b", t):
        return now + timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(?:baje|बजे|o'?clock|am|pm|a\.m|p\.m)?", t)
    if m and (re.search(r"baje|बजे|o'?clock|am|pm|a\.m|p\.m|:\d{2}", t) or re.search(r"\b" + m.group(1) + r"\b", t)):
        h = int(m.group(1))
        if h <= 24 and (re.search(r"baje|बजे|o'?clock|am|pm|a\.m|p\.m|:\d{2}", t)):
            if re.search(r"pm|p\.m|shaam|शाम|evening|raat|रात|night|dopahar|दोपहर|afternoon", t) and h < 12:
                h += 12
            hour = h % 24
    if hour is None:
        if re.search(r"subah|सुबह|morning", t):
            hour = 10
        elif re.search(r"dopahar|दोपहर|afternoon|lunch", t):
            hour = 13
        elif re.search(r"shaam|शाम|evening", t):
            hour = 18
        elif re.search(r"raat|रात|night", t):
            hour = 20
    if hour is None and day == now.date():
        return None
    if hour is None:
        hour = 11
    when = datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)
    if when <= now:
        when += timedelta(days=1)
    return when


def classify_unanswered(cause: str) -> tuple[str, str]:
    """Outcome and a one-line note for a call nobody picked up, from Plivo's hangup cause."""
    c = cause.upper()
    if c in ("USER_BUSY", "CALL_REJECTED"):
        return "busy", "busy" if c == "USER_BUSY" else "call rejected"
    if c in ("UNALLOCATED_NUMBER", "INVALID_NUMBER_FORMAT", "NO_ROUTE_DESTINATION", "NUMBER_CHANGED"):
        return "wrong_number", "number does not exist"
    if c in ("SUBSCRIBER_ABSENT", "NORMAL_UNSPECIFIED", "NORMAL_TEMPORARY_FAILURE"):
        return "no_answer", "switched off or not reachable"
    return "no_answer", "no answer"


async def handle_tool(name: str, args: dict[str, Any], state: CallState, send_event: SendEvent) -> dict[str, Any]:
    log.info("call %s: tool %s %s", state.call_id, name, json.dumps(args, ensure_ascii=False))
    if name == "product_info":
        # A lookup records nothing and ends nothing, so it is answered even with a reply to a noise.
        contact = (ACTIVE.get(state.call_id) or {}).get("contact") or {}
        answer = catalog.tool_answer(str(args.get("query") or ""), order_amount=str(contact.get("amount") or ""))
        log.info("call %s: product_info %r -> %s", state.call_id, args.get("query"),
                 [p.splitlines()[0] for p in answer.get("products", [])] or "nothing found")
        return answer
    after_goodbye = name in ("mark_outcome", "end_call") and said_goodbye(state.transcript)
    if state.held and after_goodbye:
        # Call 88: the customer confirmed and heard the goodbye; the result came a moment later, with a
        # reply to a stray noise, and was refused, so a confirmed order was saved as "dropped". The
        # decision was made on words the customer heard, so it stands.
        log.info("call %s: %s taken although it came with a reply to a sound with no words; the goodbye was already heard",
                 state.call_id, name)
    elif state.held and name in ("mark_outcome", "end_call", "record_address"):
        # Call 72 recorded "not interested" and hung up on a "hmm": nothing was answered, nothing is recorded.
        log.info("call %s: %s refused; it came with a reply to a sound that carried no words", state.call_id, name)
        return {"status": "not done", "reason": "The customer has not answered: the last sound on the line carried no words, "
                "and this reply of yours is not being played to them. Record nothing and do not end the call; they have "
                "still to answer your question."}
    if name == "record_address":
        # The address must come from the customer, not be assembled by the model out of fragments.
        recent = state.recent_customer_turns(4)
        heard = [t for t in recent if not heard_nothing(t)]
        if not heard or sum(len(t.split()) for t in heard) < 3:
            log.info("call %s: record_address refused; nothing intelligible from the customer in the last turns", state.call_id)
            return {"status": "not recorded", "reason": "You have not heard an address from the customer: their last replies were "
                    "inaudible. Do not read any address back. Ask them to hold the phone closer and say the address again, "
                    "slowly, starting with the house number. If it still cannot be heard, say 'koi baat nahi ji, maine "
                    "note kar liya hai' and carry on."}
        asked = asks_for_address_part(state._out_buf)
        if asked:
            # The model asked for a part and, in the same breath, filled it in itself (call 54).
            log.info("call %s: record_address refused; the agent asked %r in this same turn and has no answer yet", state.call_id, asked)
            return {"status": "not recorded", "reason": "You just asked the customer a question in this same turn and have not heard "
                    "their answer. Do not read any address back and do not fill in the answer yourself. Stop talking and wait "
                    "for their reply; call record_address only after they have said every part."}
        # Owner, 16 Oct 2026 (call 253): what the customer gave is enough. A PIN they do not remember, or a missing
        # area, is asked once in the prompt and never forced here; "PIN code to batana padega" after their yes was wrong.
        # "Unknown" as the city (call 58) is a gap the model papered over, not something the customer said: left blank.
        parts = {k: str(args.get(k) or "").strip() for k in ("house", "area", "city", "pin", "landmark")}
        parts = {k: "" if _PLACEHOLDER.match(v) else v for k, v in parts.items()}
        pin = spoken_digits(parts["pin"])
        parts["pin"] = pin if len(pin) == 6 else ""
        if not any(parts[k] for k in ("house", "area", "city")):
            return {"status": "not recorded", "reason": "No part of an address was given. Ask once for the address; if they "
                    "cannot give it, say 'koi baat nahi ji, maine note kar liya hai' and close."}
        # The numbers must have come out of the customer's mouth on this call, not out of the model.
        # Only a transcript we actually have can say that. OpenAI writes the customer's words down
        # in a separate pass that often lands after the reply, so an empty result here means "not
        # transcribed yet", not "never said" (call 60 refused a correct PIN three times on that).
        said = spoken_digits(" ".join(state.recent_customer_turns(10)))
        wrong = []
        if said:
            if parts["pin"] and parts["pin"] not in said:
                wrong.append(f"PIN code {parts['pin']}")
            # House numbers of two digits or more; a lone digit is a floor or a wing, not a number
            # the customer had to say ("Tower No 10, 502, 5th Floor" on call 58).
            wrong += [f"house number {n}" for n in
                      (n for n in re.findall(r"\d+", spoken_digits_spaced(parts["house"])) if len(n) >= 2)
                      if n not in said]
        if wrong:
            state.address_refusals += 1
            log.info("call %s: record_address refused (%s of 3); %s never said (customer digits heard: %r)",
                     state.call_id, state.address_refusals, " and ".join(wrong), said)
            if state.address_refusals >= 3:
                # Three refusals in a row is a loop the customer is sitting through. Stop asking.
                return {"status": "not recorded", "reason": "The address still does not match what the customer said, and they have "
                        "been asked enough times. Do not ask again and do not read any address back. Say 'koi baat nahi ji, maine "
                        "note kar liya hai', put that in mark_outcome notes, and carry on to the close."}
            return {"status": "not recorded", "reason": f"The customer has NOT said {' and '.join(wrong)} on this call. The digits they "
                    f"were actually heard to say, in order, are '{said}'. Never fill a number in yourself. Call record_address again "
                    "with only what they said, leaving that number out."}
        if not said:
            log.info("call %s: record_address taken unchecked; nothing of the customer's has been transcribed yet", state.call_id)
        state.address = parts
        state.address_refusals = 0   # a good one clears the slate; a later correction gets its own tries
        log.info("call %s: address recorded %s", state.call_id, json.dumps(parts, ensure_ascii=False))
        if read_back_confirmed(state, parts):
            # Call 253: the agent read it back, got "ji haan ji", and only then wrote it down. A second read-back is a
            # second confirmation the customer has to sit through.
            log.info("call %s: the customer already said yes to this address read back", state.call_id)
            return {"status": "recorded", "next": "The customer already said yes to this address. Do not read it back or ask "
                    "anything more about it. Close now, in one turn."}
        spoken = ", ".join(x for x in (parts["house"] and f"house number {parts['house']}", parts["area"],
                                       parts["city"] if parts["city"] != parts["area"] else "",
                                       parts["pin"] and f"PIN {' '.join(parts['pin'])}") if x)
        return {"status": "recorded", "next": f"Read it back ONCE: 'toh address hai {spoken}, sahi hai na?' Nothing about a "
                "missing part. On their yes, close at once. If they correct a part, call record_address again with it."}
    if name == "mark_outcome":
        outcome = args.get("outcome", "unclear")
        if state.address:
            # The address the checks accepted, not the model's own wording of it: on call 88 the free text
            # carried a town and a sector the customer never said.
            args["new_address"] = address_text(state.address)
        elif state.call_type.takes_address and str(args.get("new_address") or "").strip():
            # Call 88 skipped record_address and put an invented address straight in here, where the
            # spoken-digit check never ran.
            state.loose_address_refusals += 1
            if state.loose_address_refusals <= 2:
                state.record_pending = True
                log.info("call %s: mark_outcome refused; new_address %r never went through record_address",
                         state.call_id, args.get("new_address"))
                return {"status": "not recorded", "reason": "An address can only be written down through record_address, "
                        "which checks it against what the customer actually said. Do not end the call. Call record_address "
                        "with only the parts the customer said; never ask for a missing part, and never add a sector, city "
                        "or PIN yourself. Then read it back, get a yes, and call "
                        "mark_outcome again without new_address."}
            loose = str(args.pop("new_address"))
            args["notes"] = f"{args.get('notes') or ''} (address heard on the call but not checked, team to verify: {loose})".strip()
        # Owner, 16 Oct 2026: no second round for a missing PIN code; the address is taken as the customer gave it.
        # Call 160: "haan ji, kar do confirm" three replies back, then a garbled answer to the address question; only
        # the last two replies were looked at, the order was refused after the goodbye and the agent asked again.
        if (outcome in state.call_type.positive and not any(said_yes(t) for t in state.recent_customer_turns(2))
                and not yes_to_main_question(state.transcript, state.call_type.main_question)):
            # No clear yes on record: the model is confirming on silence or noise (call 53 did exactly this).
            state.confirm_refusals += 1
            goodbye_said = _GOODBYE.search(clean_agent_text(state._out_buf)) is not None
            if goodbye_said:
                log.info("call %s: mark_outcome %s taken as unclear; no yes was heard, but the goodbye was already said",
                         state.call_id, outcome)
            # Call 168: four re-asks (two here, two on the address) for a customer who had said "haan ji" each time
            # through a garbled transcript. Owner: finish the call quickly. One re-ask per call across both checks.
            if state.confirm_refusals + state.address_yes_refusals <= 1 and not goodbye_said:
                state.record_pending = True
                log.info("call %s: mark_outcome %s refused; no clear yes from the customer (last: %r)",
                         state.call_id, outcome, state.recent_customer_turns(1))
                return {"status": "not recorded", "reason": "The customer has NOT clearly said yes: their last replies were inaudible "
                        f"or not an agreement. Do not confirm. Ask it simply, in a few words, '{state.call_type.confirm_question}', with no lead-in such as 'let me ask clearly', and "
                        "wait for a clear haan. If you still cannot hear them, say 'ji, awaaz saaf nahi aa rahi. dhanyavaad ji, aapka din shubh ho', call "
                        "mark_outcome with outcome 'unclear' and notes 'could not hear the customer's confirmation', and end_call."}
            if ever_yes_to_main_question(state.transcript, state.call_type.main_question):
                # They said yes to the main question earlier; only the last replies were unclear. Keep it, for the team to check.
                args = {**args, "verify": "yes not clearly heard at the end of the call",
                        "notes": ((args.get("notes") or "") + " (final yes not clearly heard)").strip()}
            else:
                outcome = "unclear"
                args = {**args, "outcome": outcome, "notes": ((args.get("notes") or "") + " (confirmation was not clearly heard)").strip()}
        turns = state.transcript + ([{"role": "customer", "text": state._in_buf.strip()}] if state._in_buf.strip() else [])
        away = next((t["text"] for t in turns if t["role"] == "customer" and _NOT_AVAILABLE.search(t["text"])), "")
        if (outcome in ("not_interested", "unclear", "needs_help") and away
                and not ever_yes_to_main_question(turns, state.call_type.main_question)):
            # Call 193: someone else answered ("jyotika ji toh bahar gayi hai"); the agent went on about the order and saved
            # not_interested with a reason nobody said. Owner: say the line and call back later.
            if not state.away_refused and _GOODBYE.search(clean_agent_text(state._out_buf)) is None:
                state.away_refused = state.record_pending = True
                log.info("call %s: mark_outcome %s refused; the person named is not on the line (%r)", state.call_id, outcome, away[:80])
                return {"status": "not recorded", "reason": f"The person named is not on the line ('{away[:80]}'), so this is not "
                        f"a no. Ask nothing more. Say only this, nothing before or after it: '{AWAY_LINE}' Then call mark_outcome "
                        "with outcome 'call_later' and callback_time 'thodi der baad', then end_call."}
            outcome = "call_later"
            args = {"outcome": outcome, "callback_time": str(args.get("callback_time") or "").strip() or "thodi der baad",
                    "notes": ((args.get("notes") or "") + f" (the person named was not available: {away[:80]})").strip()}
        if outcome in ("confirmed", "changed") and "cancelled" in state.call_type.outcomes and asked_to_cancel(turns):
            # Call 403 (owner, 19 Oct 2026): "cancel kar dijiye", then "haan, baad mein kar denge phir" to "order rakhna
            # chahengi?" was saved as confirmed. Later is not now: that answer keeps the cancellation.
            at = next((i for i in range(len(turns) - 1, -1, -1) if turns[i]["role"] == "agent"
                       and _KEEP_IT.search(turns[i]["text"])), None)
            after = [] if at is None else [t["text"] for t in turns[at + 1:] if t["role"] == "customer" and not heard_nothing(t["text"])]
            if after and _LATER.search(after[0]) and not any(_KEEPS.search(x) for x in after):
                goodbye_said = _GOODBYE.search(clean_agent_text(state._out_buf)) is not None or said_goodbye(state.transcript)
                if not state.later_cancel_refused and not goodbye_said:
                    state.later_cancel_refused = state.record_pending = True
                    log.info("call %s: mark_outcome %s refused; asked to cancel, then 'later' (%r)", state.call_id, outcome, after[0])
                    return {"status": "not recorded", "reason": f"They asked to cancel, and to keeping it they said '{after[0]}': "
                            "later is not now, so this is a cancellation. Say only: 'theek hai ji, maine aapki cancellation "
                            "request note kar li hai. dhanyavaad ji, aapka din shubh ho.' Then mark_outcome 'cancelled' with "
                            "cancel_reason in their words, then end_call."}
                log.info("call %s: %s taken as cancelled; asked to cancel, then 'later' (%r)", state.call_id, outcome, after[0])
                outcome = "cancelled"
                # Their reason: what they said between "koi dikkat hai?" and "order rakhna chahenge?", then the "later".
                why_at = next((i for i in range(at - 1, -1, -1) if turns[i]["role"] == "agent" and _ASKED_WHY.search(turns[i]["text"])), None)
                why = " ".join(t["text"] for t in turns[(why_at if why_at is not None else at) + 1:at]
                               if t["role"] == "customer" and not heard_nothing(t["text"]))
                args = {**args, "outcome": outcome, "cancel_reason": f"{why} ({after[0]})".strip() if why else after[0],
                        "notes": ((args.get("notes") or "") + f" (recorded as {args.get('outcome')}, but the customer asked to "
                                                              f"cancel and then said '{after[0][:60]}'; order not to be sent)").strip()}
        if (state.coupon and outcome in ("wants_coupon", "not_interested") and not state.offer_wait_refused
                and offers_coupon(clean_agent_text(state._out_buf))):
            # The offer and the result in one breath: the customer has not answered the offer yet.
            state.offer_wait_refused = state.record_pending = True
            log.info("call %s: mark_outcome %s refused; it came with the coupon offer, still unanswered", state.call_id, outcome)
            return {"status": "not recorded", "reason": "You have just made the coupon offer and the customer has not "
                    "answered it yet. Do not say it again and do not end the call: wait for their answer, then call "
                    "mark_outcome."}
        if outcome == "wants_coupon":
            # Owner, 19 Oct 2026: the coupon message goes only to a customer who said yes to the offer itself.
            offered, yes = coupon_offered(state.transcript), yes_to_coupon(turns)
            if state.coupon and offered and yes:
                args = {**args, "coupon_code": state.coupon["code"]}
                if (why := probe_answer(turns)[:200]) and not bare_no(why):
                    args["not_interested_reason"] = why   # what stopped them, in their words
            else:
                state.coupon_yes_refusals += 1
                goodbye_said = _GOODBYE.search(clean_agent_text(state._out_buf)) is not None
                if state.coupon and state.coupon_yes_refusals <= 1 and not goodbye_said:
                    state.record_pending = True
                    log.info("call %s: mark_outcome wants_coupon refused; offered: %s, answer: %r",
                             state.call_id, offered, state.recent_customer_turns(1))
                    reason = ("The customer has not clearly said yes to the coupon. A no is 'not_interested'. If you could "
                              "not make out their answer, ask once, in a few words: 'ji, toh kya main woh coupon aapko bhej "
                              "doon?' and go by it." if offered else
                              "You have not made the coupon offer, so the customer has not said yes to it. Make it now, "
                              f"exactly: '{coupon_offer_line(state.coupon)}' and wait for their answer.")
                    return {"status": "not recorded", "reason": f"{reason} Then call mark_outcome again."}
                log.info("call %s: mark_outcome wants_coupon taken as unclear; no clear yes to a coupon offer", state.call_id)
                outcome = "unclear"
                args = {**args, "outcome": outcome, "notes": ((args.get("notes") or "") +
                                                              " (recorded as wanting the coupon, but no clear yes to it was "
                                                              "heard; no message sent)").strip()}
        if outcome == "cancelled":
            said_no = asked_to_cancel(turns)
        elif outcome == "not_ordered":
            said_no = (any(t["role"] == "customer" and _NOT_ORDERED.search(t["text"]) for t in turns)
                       and any(_NUMBER_CHECK.search(q) for q in agent_questions(turns)))
        elif outcome == "not_interested":
            # Call 140: a 0.3 s sound (written "นี่") was taken as a no, and two "ek baar repeat kar dijiye" never got
            # the offer again: the call closed as not interested with no no in it.
            said_no = any(t["role"] == "customer" and not heard_nothing(t["text"])
                          and (_NO.search(t["text"]) or _REFUSAL.search(t["text"]) or _DECLINE_REASON.search(t["text"])
                               or _CANCEL.search(t["text"]))   # call 153: "main cancel karna chahti hoon" to a cart
                          for t in turns)
            # Call 196: the no came through as Thai letters, then "aapki site se" (fake products) answered "koi dikkat
            # aayi thi?". A problem named in answer to the why-question is their no.
            reason_given = probe_answer(turns)
            said_no = said_no or bool(reason_given and (_COMPLAINT.search(reason_given) or _PROBLEM.search(reason_given)))
        else:
            said_no = True
        if not said_no and outcome == "not_ordered" and state.denial_refusals < 2:
            state.denial_refusals += 1
            state.record_pending = True
            log.info("call %s: mark_outcome not_ordered refused; no clear 'maine order nahi kiya' or no number check (last: %r)",
                     state.call_id, state.recent_customer_turns(1))
            return {"status": "not recorded", "reason": "The customer has NOT clearly said they did not place this order "
                    "('maine order nahi kiya'), or you have not asked 'kya yeh <name> ji ka number hai?' yet. A 'nahi' to "
                    "something else is not that: they may have ordered on the website under another name. Do not record "
                    "'not_ordered'. Ask once, plainly: 'kya aapne <product> order kiya tha?' If they did not, ask "
                    "'kya yeh <name> ji ka number hai?' and go by their answer. If you still cannot tell, say 'theek hai ji, "
                    "maine note kar liya hai. dhanyavaad ji, aapka din shubh ho', call mark_outcome with outcome 'unclear', and end_call."}
        if not said_no:
            # Throwing an order away needs the customer's own "nahi" or "cancel" somewhere in the call. On
            # call 100 the transcript was gibberish and the agent recorded "not ordered" all the same.
            state.denial_refusals += 1
            # Call 143: the goodbye was already spoken, so the refusal had the agent say "dhanyavaad ji, aapka din
            # shubh ho. Maaf kijiye, main sun nahi paayi..." Once the goodbye is out, write it down as unclear.
            goodbye_said = _GOODBYE.search(clean_agent_text(state._out_buf)) is not None
            if goodbye_said:
                log.info("call %s: mark_outcome %s taken as unclear; no no was heard, but the goodbye was already said",
                         state.call_id, outcome)
            if state.denial_refusals <= 2 and not goodbye_said:
                state.record_pending = True
                log.info("call %s: mark_outcome %s refused; the customer never said no or cancel (last: %r)",
                         state.call_id, outcome, state.recent_customer_turns(1))
                heard = ("The customer has NOT asked to cancel: a worry, a complaint or a question is not a cancellation."
                         if outcome == "cancelled" else
                         "The customer has NOT said no: a sound, a question or 'repeat kar dijiye' is not a no. If they asked "
                         "you to repeat, say the offer again in one short sentence." if outcome == "not_interested" else
                         "The customer has NOT said 'nahi', 'cancel' or that they did not order: nothing they said on this call was a no.")
                ask = (f"'{state.call_type.confirm_question}'" if outcome == "not_interested"
                       else "'ji, aap order rakhna chahte hain ya cancel karna chahte hain?'")
                return {"status": "not recorded", "reason": f"{heard} Do not record '{outcome}'. You may have misheard them. "
                        f"Ask them plainly, in a few words, what they want ({ask}) and go by their answer. If you still "
                        "cannot make it out, say 'ji, awaaz saaf nahi aa rahi. dhanyavaad ji, aapka din shubh ho', call mark_outcome with "
                        "outcome 'unclear', and end_call."}
            outcome = "unclear"
            args = {**args, "outcome": outcome,
                    "notes": ((args.get("notes") or "") + " (the agent took it as a no, but no no was heard)").strip()}
        if outcome == "not_interested" and not state.reason_refused and bare_no(str(args.get("not_interested_reason") or "")):
            # The script asks the reason once. On call 77 the agent skipped it and closed on a bare "no",
            # because a question it had asked in a held-back (unplayed) reply was still in its own memory.
            heard = [t["text"] for t in state.transcript if t["role"] == "agent"] + [clean_agent_text(state._out_buf)]
            asked_at = next((i for i in range(len(state.transcript) - 1, -1, -1) if state.transcript[i]["role"] == "agent"
                             and _ASKED_WHY.search(state.transcript[i]["text"])), None)
            answered = (asked_at is not None and not _ASKED_WHY.search(clean_agent_text(state._out_buf)) and any(
                t["role"] == "customer" and not heard_nothing(t["text"]) for t in state.transcript[asked_at + 1:]))
            if any(_ASKED_WHY.search(t) for t in heard) and not answered and not state._in_buf.strip():
                # Call 176: the reason was asked in the same turn as the goodbye, and the call closed before the answer.
                state.reason_refused = state.record_pending = True
                log.info("call %s: mark_outcome not_interested refused; the reason was asked but not yet answered", state.call_id)
                return {"status": "not recorded", "reason": "You asked why, but the customer has not answered yet. Do not "
                        "end the call and do not say goodbye. Wait for their answer, then call mark_outcome with their reason "
                        "in their words."}
            if not any(_ASKED_WHY.search(t) for t in heard):
                state.reason_refused = state.record_pending = True
                log.info("call %s: mark_outcome not_interested refused; the reason was never asked (recorded: %r)",
                         state.call_id, args.get("not_interested_reason"))
                return {"status": "not recorded", "reason": "You have not asked why, and 'not interested' is a no, not a reason. "
                        "Do not end the call. Ask once, gently: 'koi baat nahi ji. agar bura na maanein toh, koi khaas wajah?' and wait. "
                        "If their reason is a misunderstanding that CAMPAIGN DETAILS answer, clear it up once as NOT INTERESTED says. "
                        "Then call mark_outcome again with their reason in their words, or 'not given' if they would not say."}
        # Owner, 12 Oct 2026 (call 217): a customer who says in their own words that they already ordered it is thanked
        # and the call closed; no probing first.
        said_ordered = outcome == "already_ordered" and any(
            _SAID_ORDERED.search(t["text"]) for t in state.transcript[-6:] if t["role"] == "customer")
        if outcome in state.call_type.probe_first and not state.probe_refused and not said_ordered:
            # Calls 133 and 134 (abandoned cart): "Vedic Vaibhav se order kiya tha" and "kisi aur site se kiya tha"
            # were closed on the spot. Owner: handle the customer until a clear yes or no.
            agent = [t["text"] for t in state.transcript if t["role"] == "agent"] + [clean_agent_text(state._out_buf)]
            main = re.compile(state.call_type.main_question, re.IGNORECASE) if state.call_type.main_question else None
            if not [q for q in agent_questions([{"role": "agent", "text": t} for t in agent[1:]])
                    if not (main and main.search(q)) and not _IDENTITY.search(q)]:
                state.probe_refused = state.record_pending = True
                log.info("call %s: mark_outcome %s refused; nothing was asked after the opening", state.call_id, outcome)
                check = ("Check it was this cart (ALREADY ORDERED): 'achha ji, kya aapne yahi <items> order kar liya hai?' "
                         f"If they named {BUSINESS_NAME}, first: 'ji, {STORE_NAME} {BUSINESS_NAME} ka hi part hai.'"
                         if outcome == "already_ordered" else
                         "Ask once what stopped them (NOT INTERESTED): 'achha ji. koi dikkat aayi thi kya, hamari site par ya price mein?'")
                return {"status": "not recorded", "reason": "Do not end the call yet: you have asked nothing since the opening. "
                        f"{check} Wait for their answer, then go by it and call mark_outcome again."}
        if outcome == "not_interested" and state.coupon and not state.coupon_refused:
            # Owner, 19 Oct 2026: a customer who says no hears the coupon offer once before the goodbye. Not after a
            # complaint or abuse, and not once the goodbye is out (the customer has heard the call end).
            heard = turns + [{"role": "agent", "text": clean_agent_text(state._out_buf)}]
            their_reason = probe_answer(turns) or " ".join(t["text"] for t in turns if t["role"] == "customer")
            if (not coupon_offered(heard) and not _COMPLAINT.search(their_reason) and not was_abusive(turns)
                    and _GOODBYE.search(clean_agent_text(state._out_buf)) is None and not said_goodbye(state.transcript)):
                state.coupon_refused = state.record_pending = True
                log.info("call %s: mark_outcome not_interested refused; the coupon offer was never made", state.call_id)
                offer = coupon_offer_line(state.coupon)
                voice = (ACTIVE.get(state.call_id) or {}).get("voice") or ""
                # "say": Sarvam speaks it as given; the others are told to. In another language the agent says it in that.
                return {"status": "not recorded", **({"say": offer} if state.language == "hi-IN" else {}),
                        "reason": "Not yet: before the goodbye, make THE OFFER once "
                        f"(NOT INTERESTED). Say only this, nothing before or after it: '{offer}' Then wait for their answer. "
                        f"A yes: '{coupon_yes_line(voice)}', mark_outcome 'wants_coupon', end_call. Anything else: "
                        f"'{CART_NO_CLOSE}', mark_outcome 'not_interested' with their reason, end_call."}
        if outcome == "needs_help" and state.call_type.key == "abandoned_cart" and not state.help_refused:
            # Call 167: "haan ji, main yeh order lena chahti hoon", then a freebie question; the agent asked again,
            # heard "nahi haan ji", and closed as needs_help with no order taken.
            yes = ever_yes_to_main_question(state.transcript, state.call_type.main_question)
            if yes and _GOODBYE.search(clean_agent_text(state._out_buf)) is None:
                state.help_refused = state.record_pending = True
                log.info("call %s: mark_outcome needs_help refused; the customer already said yes (%r)", state.call_id, yes)
                return {"status": "not recorded", "reason": f"The customer already said yes to the order ('{yes}'). Do not end "
                        "the call and do not ask whether they want it again. Say 'ji, maine yeh note kar liya hai' about their "
                        "question and carry on with the order: payment (step 3), address (step 4), then the close (step 5) "
                        "with mark_outcome 'wants_to_order' and their question in question."}
        if outcome == "call_later":
            # Call 182: "nahi, main baad mein kharidungi, abhi nahi" was recorded as call_later, with the agent's own
            # "aap kis samay wapas call karwana chahenge?" as the time. Buying later is a no, not a call back.
            when = str(args.get("callback_time") or "").strip()
            asked = any(t["role"] == "customer" and (_CALL_BACK.search(t["text"]) or _NOT_AVAILABLE.search(t["text"]))
                        for t in turns)
            if away and not when:
                args = {**args, "callback_time": "thodi der baad"}
                when = "thodi der baad"
            if ("?" in when or not asked) and not state.later_refused:
                state.later_refused = True
                if _GOODBYE.search(clean_agent_text(state._out_buf)) is None:
                    state.record_pending = True
                    log.info("call %s: mark_outcome call_later refused; the customer never asked for a call back (time: %r)",
                             state.call_id, when)
                    return {"status": "not recorded", "reason": "The customer has not asked to be called back. 'call_later' "
                            "is only for a customer who is busy now and asks for a call later, with the time THEY gave. "
                            "'Baad mein le lungi', 'abhi nahi' about the order is a no: record 'not_interested' with their "
                            "reason. If they did ask for a call back, ask 'ji, kis samay call karein?', wait, and record "
                            "call_later with their answer."}
                # The goodbye is already out: write down what they said, not a call back.
                said_no = any(t["role"] == "customer" and _NO.search(t["text"]) for t in turns)
                outcome = "not_interested" if said_no else "unclear"
                args = {**args, "outcome": outcome, "callback_time": "",
                        "notes": ((args.get("notes") or "") + " (recorded as call back, but the customer never asked for one)").strip()}
            elif "?" in when:
                args = {**args, "callback_time": "", "notes": ((args.get("notes") or "") + " (no call-back time given)").strip()}
        if state.inaudible_total >= 2:
            args = {**args, "notes": ((args.get("notes") or "") + f" (customer was inaudible {state.inaudible_total} times on this call)").strip()}
        asked = agent_questions(turns)
        if args.get("mismatch") and not any(_SAY_BACK.search(q) for q in asked) and state.say_back_refusals < 2:
            # Call 122: "maine to prepaid kiya tha" went straight to the close, never said back or agreed.
            state.say_back_refusals += 1
            state.record_pending = True
            log.info("call %s: mark_outcome refused; the mismatch was never said back", state.call_id)
            return {"status": "not recorded", "reason": "You have not said the difference back to the customer. Do not "
                    "close yet. Say it back in one sentence and check (MISMATCH a: 'achha, toh aapke hisaab se ..., sahi?'), "
                    "wait for their yes, then carry on with step 3 and call mark_outcome again."}
        contact = (ACTIVE.get(state.call_id) or {}).get("contact") or {}
        # Owner, 12 Oct 2026: the address is confirmed on every order, paid online too (7 Oct had skipped it for online carts).
        if (state.call_type.takes_address and str(contact.get("address") or "").strip() and not state.address
                and (outcome in state.call_type.positive or args.get("mismatch"))
                and not any(_ADDRESS_WORDS.search(q) for q in asked)):
            # Call 122: the address on record was only mentioned in passing (and dropped on a repeat), never asked about.
            state.address_refusals += 1
            if state.address_refusals <= 2:
                state.record_pending = True
                log.info("call %s: mark_outcome refused; the address on record was never asked about", state.call_id)
                return {"status": "not recorded", "reason": "You have not checked the delivery address. Do not close yet. "
                        "Ask it as its own question (step 3): 'aur delivery <area>, <city> mein hi karni hai na?' and wait. "
                        "If it is wrong, take it again (TAKING AN ADDRESS). Then call mark_outcome again."}
            args = {**args, "notes": ((args.get("notes") or "") + " (address on record was not checked on the call)").strip()}
        if state.call_type.takes_address and outcome in state.call_type.positive and not state.address:
            # Call 135: "aur delivery Mohali mein hi karni hai na?" got only noise back and the order was closed on it.
            asked_at = next((i for i in range(len(turns) - 1, -1, -1) if turns[i]["role"] == "agent"
                             and any(_ADDRESS_WORDS.search(q) for q in agent_questions([turns[i]]))), None)
            answer = "" if asked_at is None else next(
                (t["text"] for t in turns[asked_at + 1:] if t["role"] == "customer" and not heard_nothing(t["text"])), "")
            if asked_at is not None and not said_yes(answer):
                state.address_yes_refusals += 1
                # Owner, 12 Oct 2026 (call 218): the address is always confirmed before the goodbye. It had shared one
                # re-ask with the clear-yes check (call 168), so a re-ask there left the address unchecked.
                if state.address_yes_refusals <= 2:
                    state.record_pending = True
                    log.info("call %s: mark_outcome %s refused; no clear yes to the address question (answer: %r)",
                             state.call_id, outcome, answer)
                    return {"status": "not recorded", "reason": "The customer has not clearly said yes to the delivery address: "
                            "their answer was noise or not a yes. Do not close. Ask again, in a few words and with no lead-in "
                            "such as 'ek baar phir check karti hoon': 'ji, delivery <area>, <city> mein hi karni hai na?' and wait. If it is wrong, take it again (TAKING AN ADDRESS). "
                            "Then call mark_outcome again."}
                args = {**args, "verify": "address not confirmed on the call",
                        "notes": ((args.get("notes") or "") + " (customer's yes to the address was not heard)").strip()}
        if outcome == "already_ordered":
            # Call 135: the agent took the order (payment, address), then recorded already_ordered. Call 137: the
            # customer answered "kya aapne yahi keychain order kar liya hai?" with a price question, the agent moved
            # on to the address, and took the "haan ji" to THAT as "already ordered". Only a yes to the check counts.
            took_order = (any(_TAKING_ORDER.search(t["text"]) for t in turns   # the coupon offer names "online payment"
                              if t["role"] == "agent" and not offers_coupon(t["text"]))
                          or any(_ADDRESS_WORDS.search(q) for q in agent_questions(turns)))
            checked_at = next((i for i in range(len(turns) - 1, -1, -1) if turns[i]["role"] == "agent"
                               and any(_ALREADY_CHECK.search(q) for q in agent_questions([turns[i]]))), None)
            answer = "" if checked_at is None else next(
                (t["text"] for t in turns[checked_at + 1:] if t["role"] == "customer" and not heard_nothing(t["text"])), "")
            # The customer's own words count as much as a yes to the check (call 217 asked four times).
            own_words = any(_SAID_ORDERED.search(t["text"]) for t in turns[-6:] if t["role"] == "customer")
            if took_order or not (said_yes(answer) or own_words):
                state.order_refusals += 1
                if state.order_refusals <= 2:
                    state.record_pending = True
                    log.info("call %s: mark_outcome already_ordered refused; took the order: %s, answer to the check: %r",
                             state.call_id, took_order, answer)
                    return {"status": "not recorded", "reason": "Not recorded: the customer has not said they already ordered "
                            "it, and it is never right once you have asked about the address or payment. If they agreed to "
                            "order, the result is 'wants_to_order'. If you are not sure, ask once: 'kya main yeh order confirm "
                            "kar doon?' and go by their answer. Then call mark_outcome again."}
                outcome = "unclear"
                args = {**args, "outcome": outcome, "notes": ((args.get("notes") or "") +
                                                              " (recorded 'already ordered' without a clear yes to it; check)").strip()}
        if outcome == "not_interested":
            # Call 191: "aapki site fake product bhejti hai" was written down as "aapne kisi aur site se product le liya
            # hai". The reason is the customer's own words; the agent's reading of them goes to the notes.
            said = probe_answer(turns)[:200]
            reason = str(args.get("not_interested_reason") or "").strip()
            if said and not bare_no(said) and said != reason:
                reading = f" (agent's reading: {reason})" if reason and not bare_no(reason) else ""
                log.info("call %s: not_interested_reason set to the customer's words %r (agent wrote %r)", state.call_id, said, reason)
                args = {**args, "not_interested_reason": said, "notes": ((args.get("notes") or "") + reading).strip()}
        reason = str(args.get("not_interested_reason") or "")
        complaints = [t["text"][:120] for t in turns if t["role"] == "customer" and _COMPLAINT.search(t["text"])
                      and t["text"].strip() not in reason]
        if complaints:
            args = {**args, "notes": ((args.get("notes") or "") + " (customer said: " + " | ".join(complaints[-3:]) + ")").strip()}
        state.outcome = outcome
        state.outcome_details = args
        state.record_pending = False
        await send_event({"type": "outcome", "outcome": state.outcome, "details": args})
        spoken = clean_agent_text(state._out_buf).strip()
        if not said_goodbye(state.transcript) and _GOODBYE.search(spoken) is None and "?" not in spoken:
            # Call 178: the reason was written down with nothing said, and the reply asked for after it was
            # "theek hai, main aapki baat samajh rahi hoon. dekhti hoon, phir aage badhte hain." Owner: say the goodbye.
            # Call 191: a line about what it was doing, then the result, and the goodbye never came: same answer.
            # (A question in this turn is left alone: the customer's answer to it is still to come, call 176.)
            return {"status": "recorded", "next": f"Say only this, nothing before or after it: '{closing_line(state, outcome)}' "
                    "Then end_call."}
        return {"status": "recorded"}
    if name == "end_call":
        # A call that the agent ends within seconds of pick-up, before the customer has said
        # anything we could write down, is a mistake (it once hung up on a customer's "hello").
        heard = any(t["role"] == "customer" for t in state.transcript) or state._in_buf.strip()
        if time.monotonic() - state.started < 12 and not heard:
            log.warning("call %s: agent tried to hang up %.1fs after pick-up; refused", state.call_id, time.monotonic() - state.started)
            state.outcome = state.outcome_details = None
            return {"status": "refused", "reason": "The customer has only just picked up and is on the line. "
                    "Do not hang up. Greet them and run the call."}
        if state.record_pending and not state.outcome and state.end_refusals < 2:
            # The model asks for mark_outcome and end_call in one breath (call 77: 0.5 s apart). When the
            # first was sent back, the second must not end the call with nothing recorded.
            state.end_refusals += 1
            log.info("call %s: end_call refused; the result was sent back and nothing is recorded yet", state.call_id)
            return {"status": "refused", "reason": "Nothing has been recorded: your mark_outcome was sent back. Do not hang up. "
                    "Do what its reply asked, then call mark_outcome again, and only then end_call."}
        said = clean_agent_text(state._out_buf).rstrip()
        if (state.outcome and _GOODBYE.search(said) is None and not said_goodbye(state.transcript)
                and not (state.outcome == "unclear" and never_got_through(state.transcript))
                and state.goodbye_refusals < 2):
            # Call 183: mark_outcome and end_call 0.6 s apart with nothing spoken; the line went dead with no goodbye.
            state.goodbye_refusals += 1
            log.info("call %s: end_call refused; the goodbye was never said", state.call_id)
            # "say": an agent that can be handed a line speaks it itself (Sarvam: call 217 hung up twice with nothing
            # said); the others are told to say it.
            line = closing_line(state, state.outcome)
            return {"status": "refused", "say": line, "reason": "You have not said goodbye. Do not hang up yet. Say only "
                    f"this, nothing before or after it: '{line}' Then end_call."}
        # Also a question with the goodbye straight after it in the same turn (call 120: "kya aap cancellation
        # rakhna chahti hain? theek hai ji, maine cancellation request raise kar di hai... dhanyavaad").
        asking = said.endswith("?") or ("?" in said and _GOODBYE.search(said.rsplit("?", 1)[1]) is not None)
        if asking and not state.held and state.goodbye_at is None and state.question_refusals < 2:
            # Call 98: "achha ji, kya poochhna chahte hain?" and the line went dead in the same breath.
            state.question_refusals += 1
            log.info("call %s: end_call refused; it came with a question the customer has not answered: %r",
                     state.call_id, clean_agent_text(state._out_buf)[-80:])
            return {"status": "refused", "reason": "You have just asked the customer a question and they have not answered. "
                    "Do not hang up. Wait for their answer and reply to it; if anything changes, call mark_outcome again. "
                    "End the call only after your goodbye."}
        state.ended = True
        return {"status": "ending"}
    return {"error": f"unknown tool {name}"}


def live_snapshot() -> list[dict]:
    """What the dashboard shows for calls that are ringing or in progress."""
    now = time.monotonic()
    out = []
    for call_id, a in list(ACTIVE.items()):
        state: CallState = a["state"]
        gate = a["gate"]
        ring_left = None
        if gate is not None and not gate.is_set():
            status, seconds = "ringing", now - a["dialed_at"]
            # So the dashboard can say how much longer this can go on, instead of a timer that
            # counts up with no end in sight.
            ring_left = max(0, round(RING_SECONDS - seconds))
        elif state.ended:
            status, seconds = "ending", now - state.started
        elif state.speaking:
            status, seconds = "agent_speaking", now - state.started
        else:
            status, seconds = "listening", now - state.started
        lines = list(state.transcript[-3:])
        if state._in_buf.strip():
            lines.append({"role": "customer", "text": state._in_buf.strip()})
        if clean_agent_text(state._out_buf) and not state.held:   # a held-back reply is never heard
            lines.append({"role": "agent", "text": clean_agent_text(state._out_buf)})
        c = a["contact"]
        out.append({
            "call_id": call_id, "channel": a["channel"], "status": status, "seconds": round(seconds),
            "ring_left": ring_left,
            "customer_name": c.get("customer_name", ""), "order_id": c.get("order_id", ""),
            "product": c.get("product", ""), "to": a["meta"].get("to") or c.get("phone", ""),
            "voice": a["voice"], "outcome": state.outcome, "lines": lines[-3:],
            "agent": AGENT_LABELS.get(state.model, state.model),
        })
    return out
