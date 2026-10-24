"""Transcripts as the team reads them: one script for the agent's lines, Punjabi letters as Hindi ones.

Owner, 24 Oct 2026: about 1 agent line in 10 was in English letters (the lines our code makes the agent say, such as
"dhanyavaad ji, aapka din shubh ho.") among replies written in Devanagari, and Saaras wrote some customers' Hindi in
Punjabi letters ("ਹਾਂਜੀ"). Only what is SHOWN changes (the call panel, the downloaded sheet): the saved transcript, the
voice and every check on the call keep the original words. Free: a word list and a letter-for-letter map, no service.
"""

import re

# The Hindi words of the lines our code says (call_types, prompts, live_bridge), in English letters -> Devanagari.
# Everyday English words (order, coupon, WhatsApp, link, COD, payment...) are not here: they stay in English letters,
# as the agent writes them in its own replies.
_HINDI = {
    "aa": "आ", "aap": "आप", "aapka": "आपका", "aapke": "आपके", "aapki": "आपकी", "aapko": "आपको", "aayi": "आई", "abhi": "अभी",
    "achha": "अच्छा", "agar": "अगर", "apne": "अपने", "aur": "और", "awaaz": "आवाज़", "baad": "बाद", "baar": "बार", "baat": "बात",
    "bas": "बस", "bata": "बता", "bataiye": "बताइए", "batayenge": "बताएंगे", "bhej": "भेज", "bhi": "भी", "bilkul": "बिल्कुल",
    "boliye": "बोलिए", "chahein": "चाहें", "chahenge": "चाहेंगे", "chahiye": "चाहिए", "dekh": "देख", "dene": "देने",
    "denge": "देंगे", "der": "देर", "deta": "देता", "deti": "देती", "dhanyavaad": "धन्यवाद", "dhanyawaad": "धन्यवाद",
    "dheere": "धीरे", "di": "दी", "dijiye": "दीजिए", "dikkat": "दिक्कत", "din": "दिन", "dobara": "दोबारा", "doon": "दूँ",
    "ek": "एक", "galat": "गलत", "galti": "गलती", "gaya": "गया", "gaye": "गए", "gayi": "गई", "haan": "हाँ", "hai": "है",
    "hain": "हैं", "ho": "हो", "hoon": "हूँ", "hua": "हुआ", "hui": "हुई", "hum": "हम", "humein": "हमें", "isme": "इसमें",
    "ismein": "इसमें", "jaye": "जाए", "jayega": "जाएगा", "ji": "जी", "kabhi": "कभी", "kar": "कर", "karke": "करके",
    "karna": "करना", "karne": "करने", "karte": "करते", "kaunsa": "कौनसा", "ke": "के", "khaas": "ख़ास", "ki": "की",
    "kijiye": "कीजिए", "ko": "को", "koi": "कोई", "koshish": "कोशिश", "kuch": "कुछ", "kya": "क्या", "lag": "लग", "lekar": "लेकर",
    "li": "ली", "lijiyega": "लीजिएगा", "liya": "लिया", "liye": "लिए", "maaf": "माफ़", "main": "मैं", "maine": "मैंने",
    "mauka": "मौका", "mein": "में", "meri": "मेरी", "mil": "मिल", "na": "ना", "nahi": "नहीं", "paas": "पास", "paise": "पैसे",
    "par": "पर", "phir": "फिर", "poori": "पूरी", "raha": "रहा", "rahe": "रहे", "rahi": "रही", "rakh": "रख", "rakhna": "रखना",
    "saaf": "साफ़", "saath": "साथ", "sahi": "सही", "sakta": "सकता", "sakte": "सकते", "sakti": "सकती", "samajh": "समझ",
    "se": "से", "shubh": "शुभ", "tha": "था", "theek": "ठीक", "thodi": "थोड़ी", "toh": "तो", "umeed": "उम्मीद", "wahan": "वहाँ",
    "wahin": "वहीं", "wajah": "वजह", "wapas": "वापस", "ya": "या", "yeh": "यह", "zarurat": "ज़रूरत",
}
_NAMES = [(re.compile(r"\bVedic\s+Shop\b", re.IGNORECASE), "वैदिक शॉप"),
          (re.compile(r"\bVedic\s+Vaibhav\b", re.IGNORECASE), "वैदिक वैभव")]
_WORD = re.compile(r"[A-Za-z']+")
# The agent's own misspellings of words it says on every call (call 5345: "धन्वंद जी").
_MISSPELT = [(re.compile("धन्वंद|धन्यवद"), "धन्यवाद")]


def _agent(text: str) -> str:
    for pattern, right in _MISSPELT:
        text = pattern.sub(right, text)
    words = _WORD.findall(text)
    hindi = [w for w in words if w.lower() in _HINDI]
    # Only a line written mostly in Hindi words: "Thank you, have a nice day." and "Order here" stay as they are.
    if not hindi or len(hindi) * 2 < len(words):
        return text
    for pattern, name in _NAMES:
        text = pattern.sub(name, text)
    text = _WORD.sub(lambda m: _HINDI.get(m.group(0).lower(), m.group(0)), text)
    return re.sub(r"\.(\s|$)", r"।\1", text)   # a sentence ends as the agent's own Hindi lines end


def _gurmukhi(text: str) -> str:
    """Punjabi letters as the same Hindi ones ("ਹਾਂਜੀ" -> "हांजी"): the two scripts sit 0x100 apart, letter for letter;
    the few Punjabi-only signs are mapped by hand."""
    special = {"ੰ": "ं", "ੱ": "", "ੲ": "इ", "ੳ": "उ", "ੴ": "ॐ"}
    return "".join(special.get(ch, chr(ord(ch) - 0x100)) if "਀" <= ch <= "੿" else ch for ch in text)


def line(role: str, text: str) -> str:
    """One transcript line as shown to the team."""
    text = str(text or "")
    return _agent(text) if role == "agent" else _gurmukhi(text)


def lines(transcript: list) -> list:
    """A whole transcript as shown to the team; the list given is not changed."""
    return [{**t, "text": line(t.get("role", ""), t.get("text", ""))} if isinstance(t, dict) else t
            for t in (transcript or [])]
