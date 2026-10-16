"""System prompts per campaign. Built from a contact row so the agent knows the order.

Kept short on purpose: every rule is said once, in one place. A longer prompt with the same rule in
three sections confused the agent more than it guided it (owner's feedback, 30 Sep 2026). The hard
guarantees (address digits, a clear yes, a real no) are checked in code, in live_bridge.handle_tool.
"""
import re
from datetime import date, datetime

from .config import AGENT_NAME, AGENT_NAME_MALE, BUSINESS_NAME, PRONUNCIATION, ROOT, VOICES

COMPANY_FILE = ROOT / "company.txt"

LANGUAGE_RULES = {
    "hindi": "Speak in natural, polite, everyday Hindi, the way a friendly shop assistant talks. Not formal or Sanskritised Hindi. Common English words like order, delivery, address, confirm are fine.",
    "hinglish": "Speak in natural Hinglish, the way people in Indian cities talk: Hindi sentences with common English words mixed in.",
    "english": "Speak in clear, friendly Indian English.",
}


def persona(voice: str) -> tuple[str, str]:
    """Return (agent_name, grammar_rule) matching the voice's gender. The rule gives verb endings, not
    whole phrases: on call 104 the ChatGPT agent took the example "bol raha hoon" as a line to say and
    opened nearly every turn with "Arjun bol raha hoon"."""
    name, own, other = ((AGENT_NAME_MALE, "a man", "masculine") if VOICES.get(voice, "female") == "male"
                        else (AGENT_NAME, "a woman", "feminine"))
    endings = "-raha, -gaya, -unga, -sakta, -wala" if own == "a man" else "-rahi, -gayi, -ungi, -sakti, -wali"
    # Call 138: the customer was "aap le sakti hain" in one turn and "aap chahte hain" in the next. A name does not
    # tell a customer's gender, so they always get the respectful plural, which fits a man or a woman.
    return name, (f"You are {own}: when you speak about yourself, use {other} verb endings ({endings}), whatever "
                  "the customer's gender. The customer always gets the respectful plural, man or woman: \"aap kar sakte "
                  "hain\", \"aap chahte hain\", never \"sakti hain\" or \"sakta hai\". Say your name only in the greeting, never again.")


STYLE = """HOW TO SOUND
A warm, relaxed telecaller from a small Indian shop, on a real phone call.
- Speak only the words the customer should hear. Never say a stage direction or a tone label ("warmly", "[cheerful]", "प्रोत्साहन भरा"): everything you write is spoken aloud.
- One or two short sentences per turn, ending in one question or the goodbye. Only the turn that says what the call is about may be longer.
- Say numbers the way people say them ("pandrah sau rupaye"). Order IDs only if asked.
- The customer's full name once, in the greeting; after that "ji". Vary your words; do not open every reply with the same filler.
- If interrupted, stop and answer what they said."""


# Added only for the OpenAI agent. Gemini hears the Roman-letter Hinglish below as Hindi; OpenAI
# reads it as generic Indian English and drifts into a South Indian accent and English phrasing
# ("quick confirmation act", "esko", "batadungi"), so it is told plainly which Hindi to speak.
# Calls 150-151 (6 Oct 2026): told to "think" in Devanagari, it still wrote every reply in English letters and
# kept the accent, so it is now told to write its replies in Devanagari (its words are what it voices).
def openai_accent(voice: str) -> str:
    """OPENAI_ACCENT plus the agent's own gender: on call 138 the ChatGPT agent, a woman's voice named Gauri,
    said "thoda sa time leta hoon, phir main aage batata hoon". Owner's wording, 6 Oct 2026."""
    female = VOICES.get(voice, "female") != "male"
    pairs = [("kar rahi hoon", "kar raha hoon"), ("bata dungi", "bata dunga"), ("check karti hoon", "check karta hoon"),
             ("dekh leti hoon", "dekh leta hoon"), ("samajh gayi", "samajh gaya"),
             ("confirm kar deti hoon", "confirm kar deta hoon"), ("samajh sakti hoon", "samajh sakta hoon"),
             ("poochne wali hoon", "poochne wala hoon")]   # call 168: "पूछने वाला हूँ" in a woman's voice
    if not female:
        pairs = [(b, a) for a, b in pairs]
    own, forms_ = ("FEMALE", "feminine") if female else ("MALE", "masculine")
    examples = "\n".join(f'  "main {say}", never "main {never}"' for say, never in pairs)
    return OPENAI_ACCENT + f"""
LANGUAGE AND AGENT GENDER (strict)
- You are a {own} Hindi/Hinglish-speaking calling agent, and your voice is {own.lower()}. Whenever you refer to
  yourself, in every turn, use {forms_} forms, even if the customer is of the other gender:
{examples}
  These show the verb endings only; they are not lines to say.
- The customer: never guess their gender from their voice or name. Use the respectful plural, which fits
  anyone: "aap kar sakte hain", "aap chahte hain".
"""


OPENAI_ACCENT = """
VOICE AND ACCENT (most important of all)
- Speak natural conversational Hindi/Hinglish the way people in North India (Delhi / western Uttar Pradesh)
  speak on the phone, with a neutral North-Indian Hindi pronunciation. Keep Hindi words natural and clear.
- NEVER a South Indian, Tamil, Telugu, Malayalam or Kannada accent or turn of phrase, and never an American or
  British accent. Do not drift into any other regional accent, and do not imitate the customer's accent:
  keep the same speaking style for the whole call.
- WRITE every reply in Devanagari, as a Hindi speaker types it, keeping only the English words below in English
  letters: "जी, आपका order confirm है?", "धन्यवाद जी, आपका दिन शुभ हो।" Never write a reply in English letters.
- Always write and say the names as "वैदिक वैभव" and "वैदिक शॉप", slowly and clearly, the Hindi way, every word.
- Write every price in Hindi words, exactly as the details give it ("दो सौ पचास रुपये"), never in digits.
- The Hindi lines in this prompt are in English letters only for convenience. Say the same words, written in
  Devanagari: "theek hai" is "ठीक है", "bata dijiye" is "बता दीजिए", "isko" is "इसको", "bata dungi" is "बता दूँगी".
- Your sentences are Hindi sentences. Use English only for common everyday words Hindi speakers say in English
  anyway (order, payment, address, product, delivery, confirm, cancel, PIN code). Never build a sentence in
  English, and never invent English phrases like "quick confirmation", "official store experience" or "clarity".
- Copy the words of the example lines in this prompt closely; they are the tone and the words to use.

TALK LIKE A PERSON, NOT A SYSTEM
- One or two short sentences per turn, then stop and let them speak. Never a paragraph.
- One question per turn. If they asked you something, answer every question they asked first, one short sentence
  each, then ask your question.
- Never narrate what you are doing or about to do: no "ek pal", "thoda time leta hoon", "main ye sambhal leti
  hoon", "main isko aage dekhti hoon", "ab main recap bol rahi hoon". Call your tools silently and say only the
  next thing a telecaller would say.
- Never say where a fact comes from ("cart mein likha hai", "details mein hai", "mere paas likha hai"): just say the fact.
- Speak to the customer as "aap", with "aap keh rahe hain" / "aapne order kiya tha"; never "tum" or "aap bol rahi ho".
- If you did not understand them, say so simply ("sorry ji, samajh nahi aaya, ek baar phir bata dijiye")
  instead of guessing what they meant.
"""

# Hindi number words 0-99: irregular, so they are listed. Call 159: ChatGPT said "Rs 250" wrong in Hindi twice,
# so the prompt now gives the exact words to say.
_HI = ("शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस "
       "तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस "
       "उनतालीस चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन "
       "तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर "
       "सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी इक्यासी बयासी तिरासी "
       "चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे "
       "अट्ठानवे निन्यानवे").split()


def hindi_number(n: int) -> str:
    """A whole number as people say a price in Hindi: 250 "दो सौ पचास", 1499 "चौदह सौ निन्यानवे",
    1050 "एक हज़ार पचास", 12500 "बारह हज़ार पाँच सौ"."""
    if n < 100:
        return _HI[n]
    if n < 1000 or (n < 10000 and (n // 100) % 10):
        head, rest = f"{_HI[n // 100]} सौ", n % 100     # 1499: "चौदह सौ", as prices are said
    elif n < 100000:
        head, rest = f"{_HI[n // 1000]} हज़ार", n % 1000
    elif n < 10000000:
        head, rest = f"{hindi_number(n // 100000)} लाख", n % 100000
    else:
        return str(n)
    return f"{head} {hindi_number(rest)}" if rest else head


def amount_line(contact: dict, missing: str) -> str:
    """'Rs 250 (say it as "दो सौ पचास रुपये")', or `missing` when there is no amount."""
    raw = str(contact.get("amount") or "").strip()
    if not raw:
        return missing
    digits = raw.replace(",", "").replace("Rs", "").replace("₹", "").strip()
    if digits.isdigit():
        return f'Rs {raw} (say it exactly as "{hindi_number(int(digits))} रुपये")'
    return f"Rs {raw}"


def pronunciation_block(contact: dict, agent_name: str = "") -> str:
    """Tell the model how to say the names that appear in this call."""
    text = " ".join(str(contact.get(k) or "") for k in ("product", "notes")) + f" {BUSINESS_NAME} {agent_name}"
    lines = [f'  - "{k}" is said as "{v}"' for k, v in PRONUNCIATION.items() if k.lower() in text.lower()]
    return "\n".join(lines) if lines else "  - (no special words in this call)"


def delivery_line(raw) -> str:
    """The delivery date as the agent should treat it. A date that has passed is never read out."""
    raw = str(raw or "").strip()
    fallback = "say no date. If asked: 'exact date abhi confirm nahi hai, jaldi dispatch ho jayega'."
    if not raw:
        return "not available: " + fallback
    parsed = None
    for text, fmt in ((raw, "%d %B %Y"), (raw, "%d %b %Y"), (raw, "%d-%m-%Y"), (raw, "%d/%m/%Y"), (raw[:10], "%Y-%m-%d")):
        try:
            parsed = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    if parsed and parsed < date.today():
        return f"{raw} HAS ALREADY PASSED: " + fallback
    return f"{raw} (say it once, in the close: 'delivery {raw} tak aa jayegi')"


LANGUAGES = list(LANGUAGE_RULES)   # what the dashboard offers


def with_language(contact: dict, language: str) -> dict:
    """The language chosen on the dashboard, unless the sheet row names one itself."""
    language = (language or "").strip().lower()
    if language in LANGUAGE_RULES and not (contact.get("language") or "").strip():
        return {**contact, "language": language}
    return contact


def missing_block(contact: dict) -> str:
    """Instructions for whatever the sheet left blank: ask the customer for it and write it down.
    Without this the model fills the gap itself (it once announced a city that was on no record)."""
    asks = []
    if not (contact.get("product") or "").strip():
        asks.append('Product: ask "aapne kya order kiya tha ji?", say back their words, get a yes; mark_outcome new_product.')
    if not (contact.get("amount") or "").strip():
        asks.append('Amount: ask "order kitne ka tha ji?"; put the answer in notes.')
    if not (contact.get("payment_mode") or "").strip():
        asks.append('Payment: ask "payment cash on delivery hai ya ho chuki hai?"; put the answer in notes.')
    if not (contact.get("address") or "").strip():
        asks.append("Address: none on record, so never say one; take it at step 3.")
    if not asks:
        return ""
    return "\nMISSING (ask; never guess)\n" + "\n".join(f"- {a}" for a in asks) + "\n"


def forms(voice: str) -> dict[str, str]:
    """The agent's own verb endings in its gender, for lines like "bhej {deti} hoon"."""
    female = VOICES.get(voice, "female") != "male"
    return dict(zip(("sakti", "deti", "rahi", "gayi"),
                    ("sakti", "deti", "rahi", "gayi") if female else ("sakta", "deta", "raha", "gaya")))


# --- what the agent knows about the company ----------------------------------------------------
# Owner, 8 Oct 2026: the agent could not say what Vedic Vaibhav, Vedic Shop or the other sites are.
# The facts live in company.txt, read at every call, so the owner can change them without a restart.

def about_lines() -> list[str]:
    """The "- Name: what to say" lines of company.txt; none if the file is missing."""
    try:
        text = COMPANY_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip().startswith("- ")]


# What a customer's words point at, and the company.txt lines (by name) that answer it. Saaras writes
# in Devanagari and English letters, and mishears the name ("Vedic भाव", "वेदिक वाइबाव"), so both
# scripts and loose spellings. A wrong match costs little: the hint says to use the lines only if they answer.
_VEDIC = r"(vedic|वैदिक|वेदिक|vaibhav|वैभव|वाइबाव)"
_ASKS = r"(kya|क्या|kaun|कौन|kahan|कहाँ|कहां)"
_NUMBER = r"(number|नंबर|नम्बर)"
_FROM = r"(kahan|कहाँ|कहां|kaise|कैसे|mila|मिला)"
# (what the words point at, the company.txt names that answer it, used only when nothing above matched).
# Call 210 (9 Oct 2026): "वैदिक वैभव क्या है" got the address and the website, not what Vedic Vaibhav does;
# "fake लगती है" got the worry line with no fact to stand on; Saaras heard "site fake" as "side shake".
# Call 212: "आपके पास मेरा नंबर कहाँ से आया?" had no answer at all.
ABOUT_TOPICS = [
    (re.compile(rf"{_NUMBER}.*{_FROM}|{_FROM}.*{_NUMBER}", re.I), ("Number",), False),
    (re.compile(r"astro|एस्ट्रो|ज्योतिष|jyotish", re.I), ("Astro Vaibhav",), False),
    (re.compile(r"pandit|पंडित", re.I), ("Pandit Ji at Request",), False),
    (re.compile(r"shop|शॉप", re.I), ("Vedic Shop",), False),
    (re.compile(r"nakli|नकली|fake|फेक|फ़ेक|fraud|फ्रॉड|फ़्रॉड|froud|side\s*shake|साइड\s*शेक|original|ओरिजिनल|ओरिजनल|"
                r"asli|असली|duplicate|डुप्लीकेट|lab|लैब|certificate|सर्टिफिकेट|bharosa|भरोसा|bharosemand|bharose", re.I),
     ("Company", "Products"), False),
    (re.compile(r"return|रिटर्न|refund|रिफंड|wapas|वापस|replace|रिप्लेस", re.I), ("Return",), False),
    (re.compile(r"company|कंपनी|कम्पनी", re.I), ("Company", "Vedic Vaibhav"), True),
    (re.compile(rf"{_VEDIC}.*{_ASKS}|{_ASKS}.*{_VEDIC}", re.I), ("Vedic Vaibhav", "Vedic Shop"), True),
]


def about_hint(words: str) -> str:
    """For one reply: the company.txt lines that answer what the customer just said, or "".
    Sarvam only, where the words are known before the reply is written."""
    if not words:
        return ""
    lines = about_lines()
    names: list[str] = []
    for pattern, ns, only_alone in ABOUT_TOPICS:
        # A named site or a worry comes first: "Astro Vaibhav kya hai?" wants that one line, not the company's.
        if pattern.search(words) and not (only_alone and names):
            names += ns
    picked = [line for n in dict.fromkeys(names) for line in lines if line[2:].lower().startswith(n.lower() + ":")]
    if not picked:
        return ""
    return ("\n\nFOR THIS REPLY ONLY: the customer may be asking about the company. If so, answer with the first line "
            "below that fits, in one short sentence of your own, then your question again. A worry (\"fake\", \"fraud\", "
            "\"nakli\") still gets the worry line, plus one of these facts. If they asked something else, ignore this.\n"
            + "\n".join(picked))


# --- the shared layer: every call type gets these, word for word ------------------------------
# A call type (app/call_types.py) supplies only what is particular to its purpose; persona,
# language, style, pronunciation, the facts rule, the common replies, taking an address and the
# common rules come from here, so what was tuned on real calls carries over to every new type.

REPLY_WRONG = '- "Galat hai" about something you said: ask what is right, say it back, get a yes. Do not move on while it is wrong.'


def _common_replies(f: dict[str, str], details_title: str, topic: str) -> list[str]:
    """What every call type does with questions, line trouble, noise, worries, abuse and off-topic talk.
    Each came from a real call: 90 (the customer could not hear the agent), 97 (a worry taken as a
    cancel), 93 and 99 (promises), 98 (abuse), 72 (agreeing to a noise)."""
    return [
        '- A question in reply to yours ("kaunsa order?", "kitne ka hai?") is not a yes or a no: answer it in one sentence, then ask your question again.',
        f'- A product or policy question ("original hai?", "certificate milega?", "return hoga?"): answer only from {details_title}. '
        f'If it is not there: "ji, aapka yeh sawaal maine note kar liya hai", note it. Never make up a certificate, guarantee, policy, discount or date.',
        # Call 120: one canned line three times, then a cancel nobody asked for. Wording: owner, 1 Oct 2026.
        f'- A worry or complaint about {BUSINESS_NAME} or the product: never argue, never defend, never offer to cancel; a worry is not a cancellation. '
        f'A doubt ("nakli bhejte ho", "site fraud hai"): "ji bilkul, aapki chinta samajh {f["sakti"]} hoon. hamari poori koshish rehti hai ki aapne '
        'jo order kiya hai, wahi achhi quality mein safely aap tak pahunche. aap nishchint rahiye." '
        f'A bad experience ("nakli bheja tha", "kharab product aaya tha"): "maaf kijiye ji, aapko aisa experience hua. hamari poori koshish rehti hai '
        'ki original quality ka product hi pahunche. aapki yeh baat maine note kar li hai." '
        f'Add a fact from {details_title} only if it answers the worry (cash on delivery: "aur payment aap delivery par hi karenge"). '
        'Never the same words twice in a call. Note it, then ask your question again; if nothing is left to ask (they said no '
        'and you have asked why), the goodbye instead, in the same turn.',
        f'- Line trouble ("hello?", "awaaz aa rahi hai?", "aapki awaaz nahi aa rahi"): "ji, main sun {f["rahi"]} hoon" and repeat your last point in a few words.',
        '- Nothing you could make out (noise, half a word, "<noise>"): "sorry ji, awaaz saaf nahi aayi" and ask again, shorter. Never agree to what you did not hear.',
        '- Busy: ask when to call back (if vague: "aaj shaam ya kal subah?"), goodbye, mark_outcome "call_later" with callback_time, end_call.',
        f'- Off topic (weather, news, jokes, personal questions): "maaf kijiye ji, main is call par sirf {topic} ke baare mein baat kar '
        f'{f["sakti"]} hoon", then your question again. A second time: goodbye, mark_outcome ("unclear" if unanswered, notes "kept going off topic"), end_call.',
        f'- About {BUSINESS_NAME} but not this call (an old order, a refund): "theek hai ji, maine yeh baat note kar li hai", note it, then your question again.',
        '- Rude or abusive words: stay polite; never repeat them or answer back. The first time, carry on with your question. Again, or a threat: '
        '"dhanyavaad ji, aapka din shubh ho", mark_outcome with notes "customer used abusive language", end_call.',
        '- "[Note from the phone line: ...]" comes from the system, not the customer: do what it says, never read it out.',
    ]


GOODBYE = 'End every call with "dhanyavaad ji, aapka din shubh ho"; never with "namaste".'

# Owner, 16 Oct 2026 (call 253): take what they give, never force a part, one read-back, then close.
TAKING_AN_ADDRESS = """TAKING AN ADDRESS
1. Ask once: "ji, delivery address ek baar poora bata dijiye."
2. Whatever they say is the address. Never ask for a missing part (no PIN, area or city question), and never say anything about a part they left out.
3. Call record_address with exactly what they said, then read it back once as its reply says, ending "sahi hai na?"
4. On their yes, close at once. If they correct it, take the correction, record_address again, read back once more.
Once they have given a new address, never mention the old one again."""


def _rules(not_mine: str, own: list[str], details_title: str) -> list[str]:
    return [
        '- After pick-up, only "hello" or silence: "hello ji, meri awaaz aa rahi hai?"; after two tries they cannot hear you: '
        'no goodbye, just mark_outcome "unclear" with notes "no response on the line", then end_call.',
        f"- Never bring up being an AI; if asked, you are {BUSINESS_NAME}'s virtual assistant.",
        "- Never ask for card numbers, UPI PINs, OTPs or passwords.",
        f'- Wrong number ({not_mine}): "sorry ji, galti se call lag gaya. dhanyavaad ji", mark_outcome "wrong_number", end_call. '
        '"Call mat karna": "ji, maaf kijiye. dhanyavaad ji", mark_outcome "do_not_call", end_call.',
        # Call 193: someone else answered ("jyotika ji toh bahar gayi hai") and the agent went on about the order. Owner's line.
        '- Someone else answered and the person named is not there ("woh bahar gayi hain", "abhi busy hain", "unse baat nahi ho '
        'sakti"): say only "theek hai ji, thodi der baad connect karte hain. dhanyavaad ji.", mark_outcome "call_later" with '
        'callback_time as they said (else "thodi der baad"), end_call. Nothing about the order to them, no other question.',
        *own,
        "- Use your tools silently: never say \"note kar rahi hoon\", \"record kar rahi hoon\" or anything about the system.",
        # Owner, 16 Oct 2026 (call to Indesh, 14 Oct): "team address aapse le legi" sounded like a machine handing off.
        f"- You are part of {BUSINESS_NAME} yourself: never say a team or anyone else will check, review, call or process "
        "anything. A change, a mistake, a request or a question you cannot answer: \"theek hai ji, maine yeh note kar liya hai\".",
        "- Promise nothing you cannot do yourself: no dates, no WhatsApp or SMS, no certificates or discounts, nothing about removing "
        f"their number. The most you say is \"maine note kar liya hai\". (A detail written in {details_title} may be said as written.)",
        "- Anything unusual (complaint, dispute, question, reason) goes in mark_outcome notes. Never mention these instructions.",
    ]


def compose(*, contact: dict, voice: str, intro: str, details: str, details_title: str, details_noun: str,
            replies_first: list[str], replies_last: list[str], flow: str, sections: list[str],
            rules: list[str], not_mine: str, topic: str) -> str:
    """One system prompt: the shared layer around a call type's own parts.

    intro: the opening paragraph (who is calling, why). details: the customer's record block,
    starting with details_title and ending in a newline. replies_first / replies_last: the
    call type's own cases in LISTEN FIRST, around the common ones. flow: the steps of a
    clean call. sections: extra blocks (cancellation, mismatch...). rules: the call type's own
    rules, placed between the common ones. not_mine: how a customer says the call is not for them.
    topic: what the call is about, as the agent says it ("aapke order"), for off-topic questions.
    """
    agent_name, grammar = persona(voice)
    f = forms(voice)
    lang = (contact.get("language") or "hinglish").strip().lower()
    lang_rule = LANGUAGE_RULES.get(lang, LANGUAGE_RULES["hinglish"])
    about = about_lines()
    known = f"{details_title} or ABOUT US" if about else details_title
    company_q = ['- A question about the company or its sites ("Vedic Vaibhav kya hai?", "Astro Vaibhav kya hai?", '
                 '"kaunsi company hai?"): not off topic. Answer from ABOUT US in one short sentence, then your question again. '
                 'Name another site only when asked about it.'] if about else []
    replies = "\n".join(replies_first + company_q + _common_replies(f, known, topic) + replies_last)
    all_rules = "\n".join(_rules(not_mine, rules, known))
    blocks = "\n\n".join(sections)
    about_block = ("ABOUT US (the only facts you may give about the company)\n" + "\n".join(about) + "\n\n") if about else ""
    return f"""{intro}

WHO YOU ARE
Your name is {agent_name}. {grammar}

LANGUAGE
{lang_rule} If the customer switches language, switch with them.

{STYLE}

PRONUNCIATION
Hindi words in the {details_noun} are written in English letters; say them as a native Hindi speaker would, never letter by letter.
{pronunciation_block(contact, agent_name)}

{details}
{about_block}FACTS
Say only what is in {known} or what this customer said on this call. Never guess, and never add anything from your own knowledge (a city, a PIN, a policy). Never say your record has something it does not. If you did not catch it, ask.

LISTEN FIRST
Take in everything they said and answer all of it in one turn: if they say several things at once, handle them together.
{replies}

{flow}

{blocks}

RULES
{all_rules}
"""
