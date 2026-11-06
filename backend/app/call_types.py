"""Call types: what a call is for, and everything that follows from it.

A call type supplies its own script parts (the shared layer in prompts.py goes around them), the
results the agent may record, which of those results need a clear yes from the customer, the extra
details it writes down, and whether it takes addresses. The contact row names its call type in
the `campaign` column; a row without one is an order confirmation, as every row was before.

Only order confirmation exists so far. Booking confirmation and campaign invites come next, and
then types written from a brief on the dashboard; they all fill the same fields.
"""
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from .config import BUSINESS_NAME, PRONUNCIATION, STORE_NAME
from .prompts import (
    GOODBYE,
    REPLY_WRONG,
    TAKING_AN_ADDRESS,
    amount_line,
    compose,
    delivery_line,
    forms,
    hindi_number,
    missing_block,
    persona,
)

log = logging.getLogger("call_types")

# Set by us from the phone line, never by the agent: nobody answered, or the call ended before
# the agent recorded anything (customer hung up, line dropped).
LINE_OUTCOMES = ("no_answer", "busy", "dropped")
# Every call type offers these; they are about the line and the person, not the purpose.
COMMON_OUTCOMES = ("complaint", "call_later", "wrong_number", "do_not_call", "unclear")

COMMON_DETAILS = {
    "callback_time": "When to call back if the customer asked for a later call.",
    # Owner, 24 Oct 2026: what a complaint is about, for the team's review.
    "complaint_type": "For outcome complaint only: refund, fraud, late, payment, legal or other.",
    "notes": "Anything else the team should know, in one or two sentences.",
}


@dataclass(frozen=True)
class Column:
    """One column of the contact sheet for a call type."""
    key: str                  # where it is stored: a contacts column, or a key in contacts.extra
    label: str                # the header in the template and on the dashboard
    required: bool = False
    example: str = ""


@dataclass(frozen=True)
class Fact:
    """One thing the person setting up a campaign tells the agent (same for every contact)."""
    key: str
    label: str
    required: bool = False
    long: bool = False        # a text area rather than one line
    date: bool = False
    hint: str = ""


@dataclass(frozen=True)
class CallType:
    key: str                              # stored in contacts.campaign
    name: str                             # shown on the dashboard
    kind: str                             # "service" or "promotional" (promotional calls have stricter rules)
    outcomes: tuple[str, ...]             # results the agent may record, in the order it sees them
    positive: tuple[str, ...]             # results refused without a clear yes from the customer
    details: dict[str, str]               # extra mark_outcome fields -> what goes in them
    build_prompt: Callable[[dict, str, dict], str]   # (contact, voice, campaign facts) -> system prompt
    takes_address: bool = False           # offers record_address and its checks
    # Offers the campaign's WhatsApp coupon (coupons.offer_for): at the payment step and once to a customer who says no.
    offers_coupon: bool = False
    confirm_question: str = "sab sahi hai? confirm kar doon?"   # asked again when a yes was not heard
    # The call's main question, as a pattern over the agent's words ("kya aapka order confirm hai?"). A clear
    # yes to it is kept when the call ends before mark_outcome (calls 88 and 92 confirmed, then were lost).
    main_question: str = ""
    # Results refused once if the agent asked nothing after its opening (calls 133, 134 closed on the first reply).
    probe_first: tuple[str, ...] = ()
    summary_keys: tuple[str, ...] = field(default=("notes",))   # detail shown as the call's one-line result
    description: str = ""                 # one line on the dashboard: what this call does
    columns: tuple[Column, ...] = ()      # the contact sheet for this type
    facts: tuple[Fact, ...] = ()          # asked once per campaign
    # The exact first line for the ChatGPT agent, (contact, voice) -> text; "" or None: it makes its own from step 1.
    greeting: Callable[[dict, str], str] | None = None

    def public(self) -> dict:
        """What the dashboard needs to know about this type."""
        return {"key": self.key, "name": self.name, "kind": self.kind, "description": self.description,
                "outcomes": list(self.outcomes), "positive": list(self.positive),
                "details": list(self.details), "summary_keys": list(self.summary_keys),
                "columns": [c.__dict__ for c in self.columns], "facts": [f.__dict__ for f in self.facts]}


def extra_of(contact: dict) -> dict:
    """The sheet's own columns beyond the fixed ones, as stored in contacts.extra."""
    raw = contact.get("extra")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _other_details(contact: dict, used: set[str]) -> str:
    """Sheet columns the call type does not read itself, one line each, so nothing uploaded is lost."""
    lines = [f"- {v.get('label') or k}: {v.get('value')}" for k, v in extra_of(contact).items()
             if k not in used and isinstance(v, dict) and str(v.get("value") or "").strip()]
    return "\n".join(lines)


def _xv(contact: dict, key: str) -> str:
    """One value from contacts.extra."""
    v = extra_of(contact).get(key)
    return str(v.get("value") if isinstance(v, dict) else v or "").strip()


_STATES = {"andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa", "gujarat", "haryana",
           "himachal pradesh", "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
           "mizoram", "nagaland", "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura",
           "uttar pradesh", "uttarakhand", "west bengal", "delhi", "jammu and kashmir", "jammu & kashmir", "ladakh",
           "chandigarh", "puducherry", "andaman and nicobar islands", "dadra and nagar haveli and daman and diu", "lakshadweep"}


def address_to_say(address: str) -> str:
    """The part of an address on record to check it by: the city, with the area before it when that is a short name
    ("Nayapalli, Khorda"). Call 282 read out "Plot - fg/131 I.D.market.nayapalli .bbsr" three times. Store addresses
    are "line 1, line 2, CITY, State, PIN"."""
    parts = [p.strip(" .") for p in str(address or "").split(",") if p.strip(" .")]
    while parts and (re.fullmatch(r"[\d\s-]+", parts[-1]) or parts[-1].lower() in _STATES):
        parts.pop()   # the PIN and the state
    if not parts:
        return ""
    def tidy(p: str) -> str:
        return p.title() if p.isupper() else p   # "KHORDA" -> "Khorda"

    city = tidy(parts[-1])
    area = parts[-2] if len(parts) >= 2 else ""
    if area and (len(area.split()) > 3 or re.search(r"\d", area) or area.lower() == parts[-1].lower()):
        area = ""
    return ", ".join(x for x in (tidy(area), city) if x)


_COMBO = re.compile(r"combo|\bset\b|\bpack\b|\bkit\b|bundle|\bduo\b|\btrio\b", re.IGNORECASE)


def _quantity_line(contact: dict) -> str:
    """The quantity, and for a combo or set how to say it. Call 282: "Aries Power Combo, 1 piece, 1319 rupaye" was heard
    as one bracelet for 1319, and the customer asked whether all three came for that (owner, 19 Oct 2026)."""
    qty = str(contact.get("quantity") or "1").strip() or "1"
    if not _COMBO.search(str(contact.get("product") or "")):
        return qty
    return (f'{qty} (a combo, one set of several items: say "{"ek" if qty == "1" else qty} combo", never "piece", and in a few '
            'words what it holds from PRODUCT INFO, e.g. "ek combo, jismein teen bracelet hain")')


def _address_line(contact: dict) -> str:
    address = str(contact.get("address") or "").strip()
    if not address:
        return "MISSING"
    short = address_to_say(address)
    return f'{address} (to check it, say only "{short}")' if short and short != address else address


# --- order confirmation ------------------------------------------------------------------------

# Owner, 19 Oct 2026: once the order and the address are both confirmed, this and nothing more, then the call is cut.
ORDER_CONFIRMED_CLOSE = "ji, aapka order confirm ho gaya hai. dhanyavaad ji."

# Keypad options (owner, 2 Nov 2026, the owner's script): on a campaign that has them switched on (Sarvam phone calls
# only) the call opens with them, said by the code (keypad_greeting); live_bridge acts on the keys.
# Order calls: 1 confirms and closes (the address is asked only when MISSING); 2 asks again (1 cancels, 2 goes back).
# Cart calls: 1 asks cash on delivery (1) or prepaid (2), then the link (and coupon) goes on WhatsApp; 2 closes.
# Both: 3 hands the call to the agent for their question, 0 repeats; three misses (0, a wrong key, no key) end the call.
# Owner, 3 Nov 2026 (call 38825): a spoken "haan" / "nahi" (and COD / prepaid) counts as the key (live_bridge
# keypad_choice), each line is said once, a repeat is the question alone (keypad_short), and every close hangs up.
# Numbers in words, so the voice says "ek", not "one".
# Campaign invites (owner, 3 Nov 2026: awareness and product campaigns too): the campaign's own opening, then 1 for yes
# (interested: the campaign's WhatsApp message goes), 2 for no, 3 for a question, 0 to hear the question again.
KEYPAD_KEY = "keypad"
KEYPAD_TYPES = ("order_confirmation", "abandoned_cart", "campaign_invite")
# The menu a call type's opening gives (KEYPAD_HEARD).
KEYPAD_MAIN_MENU = {"order_confirmation": "order", "abandoned_cart": "cart", "campaign_invite": "invite"}
# Owner, 3 Nov 2026: "not clear", "looking AI generated": each option its own short sentence, "दबाइए" as a person says it,
# a comma where a person takes a breath (Bulbul pauses there), and the customer told they may simply say it.
KEYPAD_ORDER_KEYS = ("इसे कन्फर्म करने के लिए, एक दबाइए। कैंसिल करने के लिए, दो दबाइए। कोई सवाल हो, तो तीन दबाइए। "
                     "और दोबारा सुनने के लिए, ज़ीरो दबाइए।")
KEYPAD_CART_KEYS = ("ऑर्डर करना हो, तो एक दबाइए। नहीं चाहिए, तो दो दबाइए। कोई सवाल हो, तो तीन दबाइए। "
                    "और दोबारा सुनने के लिए, ज़ीरो दबाइए।")
KEYPAD_INVITE_KEYS = ("हाँ के लिए, एक दबाइए। नहीं चाहिए, तो दो दबाइए। कोई सवाल हो, तो तीन दबाइए। "
                      "और दोबारा सुनने के लिए, ज़ीरो दबाइए।")
KEYPAD_SAY_IT = "आप चाहें तो, बोलकर भी बता सकते हैं।"
KEYPAD_ORDER_CONFIRMED = "धन्यवाद जी। आपका ऑर्डर कन्फर्म हो गया है।"
# Never cancelled on the first key: this asks again. Said after "जी, " (also when said again). "पक्का कैंसिल" rather
# than the script's "कन्फर्म करने के लिए एक": "confirm" here was the opposite of confirming the order.
KEYPAD_CANCEL_ASK = "आप अपना ऑर्डर कैंसिल करना चाहते हैं? पक्का कैंसिल करना हो, तो एक दबाइए। ऑर्डर रखना हो, तो दो दबाइए।"
KEYPAD_CANCEL_CLOSE = "theek hai ji, maine aapki cancellation request note kar li hai. dhanyavaad ji, aapka din shubh ho."
KEYPAD_ORDER_QUESTION = "जी बताइए, ऑर्डर के बारे में क्या जानना चाहेंगे?"
# Said once, after "बहुत अच्छा जी। " (and the offer, keypad_payment_ask); a repeat is KEYPAD_PAYMENT_KEYS alone.
# Owner, 3 Nov 2026: prepaid is 1 and cash on delivery 2 (prepaid first, after its discount).
KEYPAD_CART_PAYMENT_ASK = ("आप प्रीपेड लेना चाहेंगे, या कैश ऑन डिलीवरी? प्रीपेड के लिए, एक दबाइए। "
                           "कैश ऑन डिलीवरी के लिए, दो दबाइए।")
KEYPAD_PAYMENT_KEYS = f"जी, प्रीपेड के लिए, एक दबाइए। कैश ऑन डिलीवरी के लिए, दो दबाइए। {KEYPAD_SAY_IT}"
KEYPAD_CART_NO = "कोई बात नहीं जी। आपका समय देने के लिए धन्यवाद।"
KEYPAD_CART_QUESTION = "जी बिल्कुल, बताइए आपको क्या जानना है?"
# After the agent has answered a key-3 question, the code asks the call's question again (the agent asks nothing).
KEYPAD_FOLLOW_UP = {"cart": "तो क्या आप इसे ऑर्डर करना चाहेंगे? ऑर्डर करना हो, तो एक दबाइए। नहीं चाहिए, तो दो दबाइए।",
                    "order": "तो क्या आपका ऑर्डर कन्फर्म कर दूँ? कन्फर्म करना हो, तो एक दबाइए। कैंसिल करना हो, तो दो दबाइए।"}
KEYPAD_WRONG_KEY = "माफ़ कीजिए, यह विकल्प नहीं है।"
KEYPAD_GIVE_UP = "कोई बात नहीं जी। धन्यवाद, आपका दिन शुभ हो।"
# What each key stands for in each menu, as the transcript (and the agent's history) shows it. "confirm" counts as the
# customer's yes to the order (live_bridge.said_yes): only the order's 1 says it.
KEYPAD_HEARD = {
    "order": {"1": "[keypad 1: confirm]", "2": "[keypad 2: cancel? (asked once more)]", "3": "[keypad 3: question]",
              "0": "[keypad 0: repeat]"},
    "cart": {"1": "[keypad 1: wants to order]", "2": "[keypad 2: not interested]", "3": "[keypad 3: question]",
             "0": "[keypad 0: repeat]"},
    "invite": {"1": "[keypad 1: interested]", "2": "[keypad 2: not interested]", "3": "[keypad 3: question]",
               "0": "[keypad 0: repeat]"},
    "cancel": {"1": "[keypad 1: cancel it]", "2": "[keypad 2: keep the order]", "0": "[keypad 0: repeat]"},
    "payment": {"1": "[keypad 1: prepaid]", "2": "[keypad 2: cash on delivery]", "0": "[keypad 0: repeat]"},
    "address": {"1": "[keypad 1: address is right]", "2": "[keypad 2: change the address]", "0": "[keypad 0: repeat]"},
}
# Owner, 3 Nov 2026: a cart customer who chose cash on delivery is booked on the call (draft_orders), not sent the link:
# the address on record is checked (KEYPAD_HEARD["address"]), or taken on the call when there is none or it is wrong.
KEYPAD_ADDRESS_ASK = "जी, अपना पूरा डिलीवरी पता बता दीजिए।"
KEYPAD_NEW_ADDRESS_ASK = "जी, नया पूरा डिलीवरी पता बता दीजिए।"
# Each menu's question by voice, for the agent when no key comes.
KEYPAD_VOICE_ASK = {"order": "kya aapka order confirm hai?", "cart": "aap yeh order karna chahenge?",
                    "cancel": "kya aap order cancel karna chahte hain?", "payment": "aap prepaid chahenge ya cash on delivery?",
                    "address": "kya order isi pate par bhej dein?", "invite": "kya aap isme interested hain?"}


def _facts_of(contact: dict, facts: dict | None = None) -> dict:
    """The contact's campaign facts (looked up when not given; {} on any trouble)."""
    if facts is not None:
        return facts
    try:
        from . import db  # here: db is not needed by the rest of this module
        return ((db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None) or {}).get("facts") or {}
    except Exception:
        log.exception("campaign lookup failed for contact %s", contact.get("id"))
        return {}


def _invite_question(facts: dict) -> tuple[str, str]:
    """A campaign invite's offer as the keypad opening says it, and its question alone (said again on 0, after a
    question): the campaign's own opening word for word, else its title."""
    opening = str(facts.get("opening") or "").strip()
    title = str(facts.get("title") or "").strip()
    default_ask = "क्या आप इसमें interested हैं?"
    if opening:
        if opening.endswith("?"):
            return opening, re.split(r"(?<=[.!।])\s+", opening)[-1]
        return f"{opening} {default_ask}", default_ask
    return (f"हमारे यहाँ {title} है। {default_ask}" if title else default_ask), default_ask


def keypad_invite_yes(contact: dict, voice: str, facts: dict | None = None) -> str:
    """Their yes to a campaign invite: the campaign's WhatsApp message goes after the call when it has one (the owner's
    line, as live_bridge.invite_link_close); else how to book."""
    facts = _facts_of(contact, facts)
    if campaign_message(facts):
        return f"ji, main aapko WhatsApp par link bhej {forms(voice)['rahi']} hoon, aap wahan se order kar sakte hain. dhanyavaad ji."
    how = str(facts.get("how_to_book") or "").strip() or f"{BUSINESS_NAME} ki website ya app"
    return f"बहुत अच्छा जी। आप {how} पर book कर सकते हैं। धन्यवाद जी।"


def keypad_follow_up(contact: dict, call_type_key: str, facts: dict | None = None) -> str:
    """After the agent has answered a key-3 question: the call's question again, with its keys."""
    if call_type_key == "campaign_invite":
        _, ask = _invite_question(_facts_of(contact, facts))
        return f"तो, {ask} हाँ के लिए, एक दबाइए। नहीं चाहिए, तो दो दबाइए।"
    return KEYPAD_FOLLOW_UP["cart" if call_type_key == "abandoned_cart" else "order"]


def keypad_address_check(contact: dict) -> str:
    """The address on record, to check before the order is booked: said short (area and city), as order calls say it."""
    address = str(contact.get("address") or "").strip()
    said = address_to_say(address) or address
    return (f"आपका ऑर्डर {said} वाले पते पर भेजा जाएगा। यही पता सही है, तो एक दबाइए। पता बदलना हो, तो दो दबाइए। "
            "आप चाहें तो, बोलकर भी बता सकते हैं।")


def _rupees_said(amount) -> str:
    """"799" / "₹1,499.00" -> "सात सौ निन्यानवे रुपये"; "" when there is no amount."""
    m = re.search(r"\d[\d,]*(\.\d+)?", str(amount or ""))
    if not m:
        return ""
    try:
        return f"{hindi_number(round(float(m.group(0).replace(',', ''))))} रुपये"
    except ValueError:
        return ""


def keypad_booked(contact: dict) -> str:
    """The close once the address is confirmed: the order is booked (a draft for the team, draft_orders), the call ends."""
    amount = _rupees_said(contact.get("amount"))
    return f"आपका {amount + ' का ' if amount else ''}ऑर्डर कैश ऑन डिलीवरी पर बुक हो गया है। धन्यवाद जी।"


def _product_said(contact: dict) -> str:
    """The product as the greetings say it, in Devanagari where PRONUNCIATION knows the word ("" when none)."""
    product = spoken_product(str(contact.get("product") or ""))
    for word, said in PRONUNCIATION.items():   # "Rudraksha" -> "रुद्राक्ष", as the agent's and the store's names
        product = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", said, product, flags=re.IGNORECASE)
    return product


def keypad_body(contact: dict, call_type_key: str, facts: dict | None = None) -> str:
    """The opening after its hello: the order, cart or campaign, and the options. Said again on 0 (order calls)."""
    if call_type_key == "campaign_invite":
        offer, _ = _invite_question(_facts_of(contact, facts))
        return f"{offer} {KEYPAD_INVITE_KEYS}"
    product = _product_said(contact)
    if call_type_key == "abandoned_cart":
        what = f"आपने हमारी वेबसाइट पर {product} कार्ट में रखा था।" if product else "आपने हमारी वेबसाइट पर कुछ कार्ट में रखा था।"
        return f"{what} क्या आप इसे ऑर्डर करना चाहेंगे? {KEYPAD_CART_KEYS}"
    what = f"आपने {product} का ऑर्डर किया है।" if product else "आपने हमारी वेबसाइट पर ऑर्डर किया है।"
    return f"{what} {KEYPAD_ORDER_KEYS}"


def keypad_greeting(contact: dict, voice: str, facts: dict | None = None) -> str:
    """The call's first line when the options are on (owner's script, 2 Nov 2026). The hello is the same on every call,
    so it is replayed from a saved clip (sarvam_bridge._Clips)."""
    name, _ = persona(voice)
    speaking = "बोल रही हूँ" if forms(voice)["rahi"] == "rahi" else "बोल रहा हूँ"
    hello = f"नमस्ते जी, मैं {PRONUNCIATION.get(name, name)}, {PRONUNCIATION.get(BUSINESS_NAME, BUSINESS_NAME)} से {speaking}।"
    return f"{hello} {keypad_body(contact, call_type_for(contact).key, facts)}"


def keypad_repeat(call_type_key: str, voice: str) -> str:
    """Before the order again, on 0 (owner's words). Cart calls repeat only the question (keypad_short)."""
    female = forms(voice)["rahi"] == "rahi"
    if call_type_key == "abandoned_cart":
        return f"जी, दोबारा बता {'देती' if female else 'देता'} हूँ।"
    return f"जी, मैं दोबारा बोल {'रही' if female else 'रहा'} हूँ।"


def keypad_short(contact: dict, call_type_key: str, facts: dict | None = None) -> str:
    """The opening's question and keys alone (owner, 3 Nov 2026): said again on 0 (cart and invite calls), a wrong key
    or no key."""
    if call_type_key == "campaign_invite":
        _, ask = _invite_question(_facts_of(contact, facts))
        return f"जी, {ask} हाँ के लिए, एक दबाइए। नहीं चाहिए, तो दो दबाइए। {KEYPAD_SAY_IT}"
    product = _product_said(contact)
    if call_type_key == "abandoned_cart":
        return (f"जी, क्या आप {product + ' ' if product else ''}ऑर्डर करना चाहेंगे? "
                f"ऑर्डर करना हो, तो एक दबाइए। नहीं चाहिए, तो दो दबाइए। {KEYPAD_SAY_IT}")
    return (f"जी, क्या {product + ' का ' if product else 'आपका '}ऑर्डर कन्फर्म कर दूँ? "
            f"कन्फर्म करना हो, तो एक दबाइए। कैंसिल करना हो, तो दो दबाइए। {KEYPAD_SAY_IT}")


def keypad_payment_ask(coupon: dict | None) -> str:
    """After their yes to the cart (owner, 3 Nov 2026): the campaign's prepaid discount, when it has one, then the question."""
    off = f"प्रीपेड पर {_spoken_off(coupon)} की छूट है। " if coupon and coupon.get("max_discount") else ""
    return f"बहुत अच्छा जी। {off}{KEYPAD_CART_PAYMENT_ASK}"


def keypad_cart_close(voice: str, cod: bool, coupon: bool) -> str:
    """The cart call's last line once they chose how to pay (owner, 3 Nov 2026); the call hangs up after it."""
    rahi = "रही" if forms(voice)["rahi"] == "rahi" else "रहा"
    if cod:
        return f"ठीक है जी, ऑर्डर का link WhatsApp पर भेज {rahi} हूँ, वहाँ COD चुन लीजिएगा। धन्यवाद जी।"
    return f"ठीक है जी, link {'और coupon ' if coupon else ''}WhatsApp पर भेज {rahi} हूँ। धन्यवाद जी।"


def keypad_rule(call_type_key: str) -> str:
    """The prompt's one rule about keys (only when the options are on)."""
    if call_type_key == "campaign_invite":
        after = ('After "[keypad 3: question]", answer it from CAMPAIGN DETAILS in one or two short sentences and ask '
                 'nothing: the system asks the next question.')
    elif call_type_key == "abandoned_cart":
        after = ('After "[keypad 3: question]", answer it in one or two short sentences and ask nothing: the system asks '
                 f'the next question. An unclear answer to the system\'s "{KEYPAD_CART_PAYMENT_ASK.split("? ")[0]}?" '
                 'picks the close in ORDERING; make no offer.')
    else:
        after = ('After "[keypad 3: question]", answer it in one or two short sentences and ask nothing: the system asks '
                 'the next question. A spoken yes to the system\'s cancel check is CANCELLATION b; a no keeps the order: '
                 'ask "kya aapka order confirm hai?"')
    return ('- "[keypad ...]" lines are the customer pressing phone keys; the lines after them are the system\'s, said '
            f'for you. Never mention phone keys or pressing them yourself. {after}')


def keypad_on(contact: dict | None, facts: dict | None = None) -> bool:
    """The contact's campaign has the keypad options switched on. facts: the campaign's (looked up when not given)."""
    try:
        if not contact or call_type_for(contact).key not in KEYPAD_TYPES:
            return False
        if facts is None:
            from . import db  # here: db is not needed by the rest of this module
            facts = ((db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None) or {}).get("facts") or {}
        return str(facts.get(KEYPAD_KEY) or "").strip().lower() in ("on", "yes", "true", "1")
    except Exception:
        log.exception("keypad setting lookup failed for contact %s; no keypad on this call", (contact or {}).get("id"))
        return False


def offers_of(facts: dict) -> list[dict]:
    """The campaign's offers: a JSON list in facts["offers"], each {title, code, max_discount, valid_until, details}."""
    try:
        raw = json.loads(facts.get("offers") or "[]")
    except (TypeError, ValueError):
        return []
    keys = ("title", "code", "max_discount", "valid_until", "details")
    out = [{k: str(o.get(k) or "").strip() for k in keys} for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
    return [o for o in out if o["title"] or o["details"]]


def _offers_block(facts: dict) -> str:
    """Every offer still valid, for the agent; "" when there is none."""
    lines = []
    for o in offers_of(facts):
        until = _parse_date(o["valid_until"]) if o["valid_until"] else None
        if until and until < date.today():
            continue
        terms = [f"code {o['code']}" if o["code"] else "", f"at most {o['max_discount']} off" if o["max_discount"] else "",
                 f"valid until {until.strftime('%d %B')}" if until else ""]
        head = o["title"] or "Offer"
        lines.append(f"- {head}" + (f": {o['details'].rstrip('.')}" if o["details"] else "") + "".join(f"; {x}" for x in terms if x))
    if not lines:
        return ""
    return ("\nOFFERS (the only offers, codes or discounts you may mention)\n" + "\n".join(lines) +
            "\n- Mention one only when it fits, once: they ask about price, discount or delivery charges, or are unsure "
            "about a cash order. Never as pressure, never anything not written here.\n")


# --- the prepaid coupon on WhatsApp (owner, 21 Oct 2026) -----------------------------------------------
# Facts & offers has "Share a coupon on WhatsApp": facts["whatsapp_coupon"] holds the code of one of the campaign's offers.
# On a cash-on-delivery order, once it is confirmed, the agent offers that coupon for paying online instead; on a yes
# (checked in code, live_bridge) the code goes to the customer on WhatsApp (messaging.queue_prepaid_coupon).

def whatsapp_offer(facts: dict | None) -> dict | None:
    """The offer the campaign shares on WhatsApp, while it is still valid; None when the switch is off."""
    code = str((facts or {}).get("whatsapp_coupon") or "").strip().upper()
    if not code:
        return None
    for o in offers_of(facts or {}):
        if o["code"].upper() == code:
            until = _parse_date(o["valid_until"]) if o["valid_until"] else None
            return None if until and until < date.today() else {**o, "code": code}
    return None


# --- the campaign's own WhatsApp message (owner, 27 Oct 2026) ---------------------------------------
# A campaign invite (e.g. a Vedic Vaibhav puja) can send its own approved Pinnacle template after each call:
# facts["whatsapp_message"] is a JSON object filled in on the dashboard: {on, template, language, line1, line2, line3, link,
# image, button}. The three lines fill the template's {{1}}, {{2}}, {{3}}; button: the template's link button is
# https://<site>/{{1}}, and gets the link's part after the site (messaging.queue_campaign_message).
MESSAGE_KEYS = ("template", "language", "line1", "line2", "line3", "link", "image")


def campaign_message(facts: dict | None) -> dict | None:
    """The campaign's WhatsApp message while it is switched on; None when off."""
    try:
        raw = json.loads((facts or {}).get("whatsapp_message") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("on"):
        return None
    return {**{k: str(raw.get(k) or "").strip() for k in MESSAGE_KEYS}, "button": raw.get("button", True) is not False}


def message_missing(facts: dict | None) -> list[str]:
    """What the switched-on message still lacks: a template takes exactly its three values, never an empty one."""
    msg = campaign_message(facts)
    if not msg:
        return []
    # Owner, 28 Oct 2026: always the store's one template (messaging.campaign_values), so no template name is asked for.
    need = [("line1", "WhatsApp line 1"), ("line2", "WhatsApp line 2"), ("line3", "WhatsApp line 3"), ("link", "WhatsApp link")]
    return [label for key, label in need if not msg[key]]


def message_offer(facts: dict | None) -> dict:
    """The offer whose code and discount fill {code} and {discount} in the message: the first still valid one with a
    code ({} when there is none)."""
    for o in offers_of(facts or {}):
        until = _parse_date(o["valid_until"]) if o["valid_until"] else None
        if o["code"] and not (until and until < date.today()):
            return {**o, "code": o["code"].upper()}
    return {}


def paid_online(contact: dict | None) -> bool:
    """The order's data says it is paid (prepaid / online); an empty or cash-on-delivery payment is not."""
    pay = re.sub(r"[\s_-]+", " ", str((contact or {}).get("payment_mode") or "")).strip().lower()
    return bool(pay) and not ("cod" in pay or "cash" in pay or "delivery" in pay)


def prepaid_coupon_for(contact: dict | None, facts: dict | None = None) -> dict | None:
    """The coupon this call may offer on WhatsApp: an order not paid yet (cash on delivery, or no payment in the data:
    call 509, the customer is asked) and a campaign that shares one. facts: the campaign's (looked up when not given). Never raises."""
    try:
        if not contact or paid_online(contact):
            return None
        if facts is None:
            from . import db  # here: db is not needed by the rest of this module
            facts = ((db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None) or {}).get("facts") or {}
        # Owner, 27 Oct 2026: offered on every call, even to a customer who has the code already.
        return whatsapp_offer(facts)
    except Exception:
        log.exception("WhatsApp coupon lookup failed for contact %s; no offer on this call", (contact or {}).get("id"))
        return None


def _spoken_off(offer: dict) -> str:
    """The offer's discount as the agent says it: "10%" -> "दस परसेंट", "₹100" -> "एक सौ रुपये", other words as written."""
    text = " ".join(str(offer.get("max_discount") or "").split())
    m = re.fullmatch(r"(?:rs\.?|inr|₹)?\s*(\d{1,7})\s*(%|percent|परसेंट|rs\.?|rupees?|रुपये|/-|₹)?(?:\s*off)?", text, re.IGNORECASE)
    if m:
        unit = (m.group(2) or "").lower()
        words = hindi_number(int(m.group(1)))
        # A bare number is a percentage up to 90 ("10"), else rupees ("200").
        rupees = bool(re.match(r"(rs|inr|₹)", text, re.IGNORECASE)) or (unit not in ("%", "percent", "परसेंट")
                                                                         and (unit or int(m.group(1)) > 90))
        return f"{words} रुपये" if rupees else f"{words} परसेंट"
    return text or str(offer.get("title") or "").strip()


# Owner, 21 Oct 2026 (call 507): ask how they will pay; a cash-on-delivery answer gets the prepaid offer, made once to win
# them over and never pressed again. Owner, 22 Oct 2026 (call 514): fewer words in every line of it.
PREPAID_PAYMENT_QUESTION = "theek hai ji. aur payment mode cash on delivery rakhna hai ya prepaid?"
PREPAID_SEND_QUESTION = "main coupon code aapke WhatsApp par bhej doon?"


def _gain(offer: dict) -> str:
    """"coupon code lagane par दस परसेंट off mil jayega", or the offer's own words when it has no discount figure."""
    off = _spoken_off(offer)
    return f"coupon code lagane par {off} off mil jayega" if offer.get("max_discount") else f"coupon code par yeh offer milega: {off}"


def prepaid_offer_line(offer: dict) -> str:
    """The offer, once, after their yes to the address. Owner, 24 Oct 2026: every order these calls confirm is cash on
    delivery, so there is no payment question first; owner's wording. live_bridge.offers_coupon knows it by "off"."""
    off = _spoken_off(offer)
    gain = f"{off} off hai" if offer.get("max_discount") else f"yeh offer hai: {off}"
    return f"prepaid orders par {gain}. aap prepaid karna chahenge?"


def prepaid_online_line(offer: dict) -> str:
    """Their answer was online payment: the coupon goes with it."""
    return f"ji, {_gain(offer)}. {PREPAID_SEND_QUESTION}"


def prepaid_yes_line(voice: str = "") -> str:
    """The close when they chose prepaid. Owner, 22 Oct 2026: a yes moves the order to online payment (shopify.mark_prepaid)
    and the code goes on WhatsApp. 24 Oct: the close also says the order is confirmed (the offer now comes before the
    address, so this is the call's last line)."""
    return f"ji, aapka order confirm ho gaya hai. coupon aapke WhatsApp par bhej {forms(voice)['rahi']} hoon. dhanyavaad ji."


# The close when they keep cash on delivery (owner's words, 24 Oct 2026).
PREPAID_NO_LINE = "theek hai ji, aapka order COD ke saath confirm ho gaya hai. dhanyavaad ji."
# Their answer to the offer, then the address question in the same turn.
PREPAID_YES_ACK = "ji, coupon WhatsApp par aa jayega."
PREPAID_NO_ACK = "theek hai ji."


def _whatsapp_coupon_block(offer: dict | None, contact: dict | None = None, voice: str = "") -> str:
    if not offer:
        return ""
    cod = bool(str((contact or {}).get("payment_mode") or "").strip())
    paid = ("they say it is already paid: MISMATCH (payment), no offer." if cod else
            'they say it is already paid: "theek hai ji, aapka order confirm ho gaya hai. dhanyavaad ji, aapka din shubh ho." '
            'mark_outcome "confirmed" with notes "customer says already paid online", end_call. No offer.')
    return f"""PAYMENT AND WHATSAPP COUPON (owner, 24 Oct 2026: the order is cash on delivery; the offer comes right after their yes to the order, before the address)
a. Straight after their yes to the order (step 2), with nothing different, say exactly these words, nothing added: "{prepaid_offer_line(offer)}" Then wait. If {paid}
b. Their answer, then the address question (step 3) in the same turn:
   - Yes ("haan", "kar denge", "offer chahiye", "bhej do", or they ask what the code is): "{PREPAID_YES_ACK}" Never read the code out.
   - No, or they keep cash on delivery: "{PREPAID_NO_ACK}"
   - A question about the offer: its answer in one short sentence from OFFERS, then "aap prepaid karna chahenge?"
c. After their yes to the address, close with the line for their choice, word for word, then mark_outcome "confirmed", end_call:
   - chose prepaid: "{prepaid_yes_line(voice)}"
   - cash on delivery: "{PREPAID_NO_LINE}"
The payment change and the code on WhatsApp happen by themselves; promise nothing else. Make the offer once and never push. Keeping cash on delivery is not a mismatch: the order is already cash on delivery. If they change their mind later, their last word on it decides. No offer after a mismatch or a cancellation."""


def _order_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    rahi = forms(voice)["rahi"]
    # As the record gives it, said as is (owner, 24 Oct 2026). "Cash on Delivery", "cod", "COD" are all cash on delivery
    # (only "COD" was, so "Cash on delivery" read as paid); none on record: the payment is not said and not asked.
    payment = str(contact.get("payment_mode") or "").strip()
    payment_line = ("prepaid" if paid_online(contact) else "cash on delivery") if payment else \
        "not on record (never say or ask it)"
    answers = str((facts or {}).get("answers") or "").strip()

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian company selling devotional products, to confirm "
             f"an order the customer placed on {STORE_NAME}, {BUSINESS_NAME}'s online store. Warm and short: about a minute.")

    details = f"""ORDER DETAILS
- Order ID: {contact.get('order_id') or 'not available'}
- Customer name: {contact.get('customer_name') or 'MISSING (greet with "hello ji")'}
- Product: {contact.get('product') or 'MISSING'}
- Quantity: {_quantity_line(contact)}
- Amount: {amount_line(contact, 'MISSING')}
- Payment: {payment_line}
- Delivery address: {_address_line(contact)}
- Expected delivery: {delivery_line(contact.get('delivery_date'))}
- Notes: {contact.get('notes') or 'none'}
- Answers to common questions: {answers or 'none'}
{missing_block(contact)}{_offers_block(facts or {})}"""

    replies_first = [
        '- Yes ("haan", "ji", "sahi hai"): next step.',
        '- Something in the order is different (product, quantity, amount, payment): MISMATCH. A wrong address is not a mismatch: take it again at step 3.',
        '- They ask for another product instead of this one: PRODUCT CHANGE.',
        # Call 282: "teeno mila ke 1300, ek ko nahi?" about a combo of three bracelets was taken as a dispute of the amount.
        ('- A question about what the order holds or what the amount covers ("teeno milenge?", "combo mein kya hai?", "ek ka hai ya sab ka?"): '
         'not a mismatch. Answer from PRODUCT INFO in one sentence (a combo or set is one order, all of it for the amount above), then your question again.'),
        '- They want to cancel: CANCELLATION.',
        # Calls 93, 96 and 98 handled "I did not order" three different ways.
        ('- They did not place this order: never offer to cancel. Ask once "kya yeh <name> ji ka number hai?" Not theirs: wrong number. '
         'Theirs: "theek hai ji, maine yeh note kar liya hai. dhanyavaad ji, aapka din shubh ho", '
         'mark_outcome "not_ordered", end_call.'),
        (f'- "{BUSINESS_NAME} kaun? Maine to {STORE_NAME} se order kiya": "ji, {STORE_NAME} {BUSINESS_NAME} ka hi online store hai." Then your question again. '
         # Call 119: "kisi aur site se kiya tha" was closed as not ordered at once.
         '"Maine to kisi aur site se kiya tha": the same, then "kya aapne <product> order kiya tha?"; only a no to that is "did not place this order".'),
        # Call 120 called this off topic and hung up.
        f'- "Aur kya-kya products hain?": not off topic. "ji, saare products {STORE_NAME} par dekh sakte hain." Then your question again.',
    ]
    replies_last = ['- A bare "nahi" to "kya aapka order confirm hai?": "achha ji, kya galat hai isme?"']

    # Every order ends at step 3 then 4, whatever happened on the way (a mismatch included): on call
    # 102 the agent never came back to the missing address after the customer disputed the amount.
    flow = f"""THE USUAL ORDER
1. Greet, then wait: "namaste <name> ji, main {STORE_NAME} se baat kar {rahi} hoon. aapka order confirm karna tha. ek minute baat ho sakti hai?"
2. After their yes, the order in one sentence, always ending in "kya aapka order confirm hai?": "aapne <product>, <quantity> <amount> rupaye ka, <payment> mein order kiya tha. kya aapka order confirm hai?" (say the payment as Payment gives it; when it is not on record, leave it out and never ask about it)
3. Address, every time (call 122 skipped it). On record: ask it as its own question, "ji, aur delivery <address> mein karni hai?", and get a yes. A yes in any words ("haan ji", "sahi hai", "yahi hai", "isi address par") is a yes: go on. An answer you could not make out, or a mixed one ("na haan ji"): only "ji, ek baar delivery address confirm kar dijiye." Never ask for the whole address unless it is MISSING or they say it is wrong (call 515 asked for it after "haan ji, isi address par delivery karni hai"). MISSING, or they say it is wrong: "ji, delivery address ek baar poora bata dijiye" (TAKING AN ADDRESS). This comes after a mismatch too; skip it only after a cancellation.
4. Close in one turn, straight after their yes to the address (nothing more to ask), then mark_outcome, then end_call:
   - all correct (owner, 19 Oct 2026), exactly this and nothing after it: "{ORDER_CONFIRMED_CLOSE}" (with "delivery <date> tak aa jayegi." before "dhanyavaad ji" only if Expected delivery gives a date)
   - after a mismatch: "theek hai ji, maine yeh badlaav note kar liya hai. dhanyavaad ji, aapka din shubh ho."
Ask for a yes at most twice in the call: once to the order (step 2), once to the address (step 3, or the read-back). Never again (owner, call 253).
Every other call ends with "dhanyavaad ji, aapka din shubh ho"; never with "namaste". Never end_call while a question of yours is unanswered."""

    # Call 102: "cancel kyun karna hai?" twice, by rote. Owner: handle it intelligently.
    cancellation = """CANCELLATION (they clearly want to cancel)
Be a helpful person, not a form: never push, never argue, never ask the same thing twice.
a. If they have not said why, ask once, naturally: "koi baat nahi ji. koi dikkat hai kya?" If they already said why, skip this.
b. Their reason, whatever it is (owner, 3 Nov 2026: never ask whether they want to keep the order): "theek hai ji, maine aapki cancellation request note kar li hai. dhanyavaad ji, aapka din shubh ho." mark_outcome "cancelled" with cancel_reason in their words ("not given" if they would not say), then end_call.
A "nahi" to cancelling, a worry, or a request (COD, a discount) is not a cancellation: deal with it and carry on. After a cancellation, never read the order again or ask for the address. Paid online and asking about the money: "ji, maine yeh note kar liya hai", no dates."""

    # Call 102: "quantity sahi hai, bas price 900 hai aur prepaid" needed one reply, not one turn per difference.
    mismatch = """MISMATCH (they say the product, quantity, amount or payment is different)
You cannot change the order on the call; you only note it, so it is checked before dispatch.
a. Say back, in one sentence, only what they said is different, in their words, and check: "achha, toh aapke hisaab se <what they said>, sahi?" Never add anything they did not say (call 282: "payment ho chuki hai", which nobody said). If you did not catch their version, ask for it; never suggest a number or a product yourself.
b. On their yes: "theek hai ji, maine yeh note kar liya hai." Then step 3 if the address is MISSING, then the mismatch close.
Never argue about who is right, never agree to change anything, never quote a new price or a refund. In mark_outcome: outcome "unclear", mismatch = each difference as "customer says ...; record says ...", new_product if they named another product."""

    # Owner, 2 Nov 2026: a customer who wants another product is told it is replaced; the order is only tagged for the
    # team (shopify.AI_TAGS), nothing in it is changed.
    product_change = """PRODUCT CHANGE (they ask for a different product instead of this one; never suggest one yourself)
a. Check once, with its name from product_info: "achha, aapko <product> chahiye, sahi?" Never quote its price or a delivery date.
b. On their yes (step 3 first if the address is MISSING): "theek hai ji, aapka product <product> se replace ho gaya hai. dhanyavaad ji, aapka din shubh ho." mark_outcome "changed" with new_product = the product (and quantity) they asked for, end_call."""

    rules = [('- You cannot cancel, refund or change anything yourself: you note it, "maine note kar liya hai", never "kar '
              'diya" (PRODUCT CHANGE\'s line aside).')]
    if keypad_on(contact, facts or {}):
        rules.append(keypad_rule("order_confirmation"))
        # The opening is the code's (keypad_greeting): it names the product and gives the keys, so step 1 is said.
        step1 = (f'1. The greeting has been said for you: "{keypad_greeting(contact, voice)}" A spoken yes to it is a yes '
                 "to step 2's question: go on as after that yes.")
        flow = re.sub(r"(?m)^1\. Greet, then wait: .*$", lambda _: step1, flow)
    coupon = _whatsapp_coupon_block(prepaid_coupon_for(contact, facts or {}), contact, voice)
    if coupon:
        # Owner, 24 Oct 2026: the order is cash on delivery, so the prepaid offer comes straight after their yes to the
        # order, before the address; the close then says what they chose (PAYMENT AND WHATSAPP COUPON).
        flow = flow.replace('kya aapka order confirm hai?" (say the payment as Payment gives it; when it is not on record, leave it out and never ask about it)',
                            'kya aapka order confirm hai?" (say the payment as Payment gives it; when it is not on record, leave it out and never ask about it) After their yes: the '
                            "prepaid offer (PAYMENT AND WHATSAPP COUPON), then step 3.")
        flow = re.sub(r"(?m)^   - all correct \(owner.*$", "   - all correct: the close PAYMENT AND WHATSAPP COUPON gives for "
                      "their choice (prepaid or cash on delivery), word for word", flow)
        flow = flow.replace("Ask for a yes at most twice in the call:",
                            "Ask for a yes at most twice in the call (the prepaid offer aside):")

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="ORDER DETAILS",
                   details_noun="order details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[TAKING_AN_ADDRESS, *([coupon] if coupon else []), cancellation, mismatch, product_change], rules=rules,
                   not_mine='not the person named, or not their number',
                   topic="aapke order")


ORDER_CONFIRMATION = CallType(
    key="order_confirmation",
    name="Order confirmation",
    kind="service",
    outcomes=("confirmed", "changed", "cancelled", "not_ordered", "complaint", "call_later", "wrong_number", "do_not_call", "unclear"),
    positive=("confirmed", "changed"),
    details={
        "mismatch": "What the customer claims versus what our record shows, raised for the team to verify. Example: 'customer says already paid online by UPI; record shows COD'.",
        "new_product": "Product and quantity the customer asked for instead (PRODUCT CHANGE), or says they ordered if different from our record (MISMATCH). Noted for the team; nothing is changed.",
        "new_address": "New delivery address the customer confirmed after you read it back, as they gave it (a missing PIN is fine).",
        "new_delivery_date": "New delivery date if the customer changed it.",
        "cancel_reason": "Reason given if cancelled.",
        **COMMON_DETAILS,
    },
    build_prompt=_order_prompt,
    takes_address=True,
    confirm_question="kya aapka order confirm hai?",
    main_question=r"(confirm|कंफर्म|कन्फर्म)\s*(hai|है|karna|करना)",
    summary_keys=("notes", "cancel_reason", "callback_time", "mismatch", "new_address"),
    description="Confirm an order placed on the website or app: product, amount, payment and address.",
    columns=(
        # Owner, 21 Oct 2026: a sheet needs name, phone, product id, product name, price and payment; the rest is optional.
        Column("customer_name", "Customer name", True, "Ramesh Kumar"),
        Column("phone", "Phone", True, "PUT 10 DIGIT NUMBER"),
        Column("product_ids", "Product ID", True, "8123456789012"),
        Column("product", "Product name", True, "Rudraksha Mala 108 beads"),
        Column("amount", "Price", True, "1499"),
        Column("payment_mode", "Payment method (COD / Prepaid)", True, "COD"),
        Column("order_id", "Order ID", False, "VV-1001"),
        Column("quantity", "Quantity", False, "1"),
        Column("address", "Address", False, "12 MG Road, Indore, MP 452001"),
        Column("delivery_date", "Delivery date", False, "30 September 2026"),
        Column("notes", "Notes", False, ""),
    ),
    facts=(
        # Without these the agent invented them (call 99: "certificate bhi saath aayega").
        Fact("answers", "Business facts (optional)", long=True,
             hint="Sabhi products original hain. Rudraksha ke saath lab certificate aata hai. Delivery ke baad 7 din mein return."),
        # Owner, 21 Oct 2026: any number of offers the agent may mention (_offers_block), kept as a JSON list of
        # {title, code, max_discount, valid_until, details}; the new dashboard edits it as a list. None: it offers nothing.
        Fact("offers", "Offers (optional)", long=True, hint='[{"title": "Free delivery on prepaid", "details": "..."}]'),
        # Owner, 21 Oct 2026: the code of the offer above that a cash-on-delivery customer may get on WhatsApp for paying
        # online (whatsapp_offer); empty = switched off.
        Fact("whatsapp_coupon", "Share a coupon on WhatsApp (optional)", hint="The code of one of the offers above"),
        # Owner, 2 Nov 2026: "on" = the call opens with press 1 / 2 / 3 / 0 (keypad_greeting); empty = off.
        Fact(KEYPAD_KEY, "Keypad options 1/2/0/3 (optional)", hint="on"),
    ),
)


# --- booking confirmation -----------------------------------------------------------------------

def _parse_date(raw: str) -> date | None:
    for text, fmt in ((raw, "%d %B %Y"), (raw, "%d %b %Y"), (raw, "%d-%m-%Y"), (raw, "%d/%m/%Y"), (raw[:10], "%Y-%m-%d")):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _date_line(raw: str) -> str:
    """A puja, seva or campaign date as the agent should treat it; a date that has passed is flagged."""
    raw = (raw or "").strip()
    if not raw:
        return "not on record (do not name a date; if asked: 'ji, maine aapka sawaal note kar liya hai')"
    day = _parse_date(raw)
    if day and day < date.today():
        return f"{raw} (THIS DATE HAS ALREADY PASSED; do not say it, and put it in notes)"
    return raw


def _spoken_date(raw: str) -> str:
    """Like _date_line, but told to be said as a person says it on the phone: "3 October", no year
    (call 72 read out "3 October 2026")."""
    day = _parse_date((raw or "").strip())
    if not day or day < date.today():
        return _date_line(raw)
    return f'{day.day} {day:%B} {day.year} (say it as "{day.day} {day:%B}", never the year)'


def _block(title: str, body: str) -> str:
    """An optional block inside the details: nothing at all when there is no body."""
    return f"{title}\n{body}\n" if body else ""


def _booking_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    f = forms(voice)
    sakti = f["sakti"]
    payment = (contact.get("payment_mode") or "").upper()
    payment_line = "not paid yet (pay later)" if payment == "COD" else "already paid online" if payment else "unknown"
    other = _block("Other details from the sheet:", _other_details(contact, {"temple", "puja_date", "devotee_names"}))
    missing = "" if contact.get("product") else _block(
        "\nMISSING DETAILS (ask the customer; never guess)",
        '- WHAT WAS BOOKED: name none. Ask "aapne kaunsi puja ya seva book ki thi ji?", say back their words, get a yes; put it in notes.')

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian online store for devotional services, to confirm "
             "a puja, chadhava or seva booking the customer made on our website or app. Short, warm, efficient: about a "
             "minute, never rushing them.")

    details = f"""BOOKING DETAILS
- Booking ID: {contact.get('order_id') or 'not available'}
- Customer name: {contact.get('customer_name') or 'MISSING (greet with "hello ji")'}
- Booked: {contact.get('product') or 'MISSING'}
- Temple: {_xv(contact, 'temple') or 'not on record'}
- Date: {_date_line(_xv(contact, 'puja_date'))}
- Devotee names (sankalp): {_xv(contact, 'devotee_names') or 'not on record'}
- Amount: {amount_line(contact, 'not on record')}
- Payment: {payment_line}
- Notes: {contact.get('notes') or 'none'}
{other}{missing}"""

    replies_first = [
        '- Clear yes ("haan", "ji", "sahi hai", "theek hai"): next step.',
        '- Cancel ("cancel kar do", "nahi karwani", "booking nahi chahiye"): see CANCELLATION.',
        '- They disagree with the record (what was booked, temple, date, names, amount, payment): see MISMATCH.',
        REPLY_WRONG,
    ]
    replies_last = ['- A bare "nahi": "achha ji, kya galat hai isme?"']

    flow = f"""THE USUAL ORDER (three short turns on a clean booking; add nothing)
1. Greet, say it is only a quick booking confirmation call, ask for a minute, then STOP and wait: "namaste <name> ji, main {agent_name}, {BUSINESS_NAME} se. bas aapki booking ka ek chhota sa confirmation call hai, ek minute baat ho sakti hai?" Nothing else in this turn.
2. After their yes, say what THEY booked in one flowing sentence: what, at which temple, for which date, and the amount, then ask. For example: "ji, aapne <temple> mein <date> ke liye <what was booked> book kiya tha, <amount> rupaye. kya aapki booking confirm hai?" Leave out any part that is not on record. Say the devotee names only if they ask. Never announce what you are about to do: no "recap", no "summary". Go on only after a clear yes.
3. Close in ONE turn: "ji, aapki booking confirm ho gayi. dhanyavaad ji, aapka din shubh ho." Then mark_outcome "confirmed", then end_call. After a mismatch or a cancellation, close as those sections say instead.
{GOODBYE}
Never end_call with a question unanswered."""

    cancellation = f"""CANCELLATION (they want to cancel, at any point in the call)
Stop confirming the moment they say it. Warm, never pushy, never arguing. At most four short turns:
  a. Only if it was unclear, check once: "achha ji, aap booking cancel karna chahte hain?" If it was clear, skip this.
  b. Ask the reason once, gently: "koi baat nahi ji, bas bata dijiye, cancel kyun karna hai?"
  c. Only if the reason is the date, the price or a mistake in the booking, offer ONE time: "ji samajh {sakti} hoon. maine yeh baat note kar li hai; kya aap booking rakhna chahenge?" For any other reason skip this and accept.
  d. Accept: "theek hai ji, maine aapki cancellation request note kar li hai. dhanyavaad ji, aapka din shubh ho." Then mark_outcome "cancelled" with cancel_reason in their words, then end_call.
If at c they decide to keep the booking, go back to confirming it.
Once they want to cancel: never ask "confirm kar doon?" and never read the booking back again. If it was paid and they ask about the money: "ji, maine yeh note kar liya hai"; never a date, a method or a number of days."""

    mismatch = """MISMATCH (they say what was booked, the temple, the date, the names, the amount or the payment is different)
BOOKING DETAILS are the only truth; you cannot change them on the call, only note the issue so it is checked. Warm, on their side, never arguing. One short turn each:
  a. Say what the record shows, once, only the part they dispute: "ji, mere record mein <that part> hai."
  b. Get their version in their words and say it back: "achha, aapne <their words> book kiya tha, sahi?" Never offer a name, a date or a number yourself.
  c. On their yes: "theek hai ji, maine yeh note kar liya hai. baaki sab sahi hai?" Ask "baaki sab sahi hai?" at most twice in a call.
  d. Close: "theek hai ji, maine sab note kar liya hai. dhanyavaad ji, aapka din shubh ho." Do not say "booking confirm ho gayi" after a mismatch.
Never agree to change anything; never invent an offer, discount, date or policy.
In mark_outcome: mismatch = their claim vs the record, one line per difference, outcome "unclear" (or "cancelled" if they cancelled)."""

    rules = [('- You cannot cancel, refund, reschedule or edit anything yourself: "maine note kar liya hai", never "kar diya". '
              'Unknowns: "maine aapka sawaal note kar liya hai"; put it in notes.'),
             '- Never promise what a puja will bring: no "manokamna poori hogi", no "guaranteed", no health or money claims.']

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="BOOKING DETAILS",
                   details_noun="booking details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[cancellation, mismatch], rules=rules, not_mine='"I did not book anything"',
                   topic="aapki booking")


BOOKING_CONFIRMATION = CallType(
    key="booking_confirmation",
    name="Booking confirmation",
    kind="service",
    outcomes=("confirmed", "cancelled", "complaint", "call_later", "wrong_number", "do_not_call", "unclear"),
    positive=("confirmed",),
    details={
        "mismatch": "What the customer says versus what the booking record shows, raised for the team to verify.",
        "cancel_reason": "Reason given if cancelled.",
        **COMMON_DETAILS,
    },
    build_prompt=_booking_prompt,
    confirm_question="booking confirm kar doon?",
    main_question=r"(confirm|कंफर्म|कन्फर्म)\s*(hai|है|karna|करना)",
    summary_keys=("notes", "cancel_reason", "callback_time", "mismatch"),
    description="Confirm a puja, chadhava or seva booked on the website or app: yes, cancel, or something is wrong.",
    columns=(
        Column("customer_name", "Customer name", True, "Sunita Sharma"),
        Column("phone", "Phone", True, "PUT 10 DIGIT NUMBER"),
        Column("order_id", "Booking ID", False, "BK-2001"),
        Column("product", "Puja / seva", True, "Navratri chadhava"),
        Column("temple", "Temple", False, "Kamakhya Mandir, Guwahati"),
        Column("puja_date", "Puja date", False, "3 October 2026"),
        Column("devotee_names", "Devotee names", False, "Sunita Sharma, Rakesh Sharma"),
        Column("amount", "Amount", False, "551"),
        Column("payment_mode", "Payment (COD / Prepaid)", False, "Prepaid"),
        Column("notes", "Notes", False, ""),
    ),
)


# --- campaign invite ------------------------------------------------------------------------------

def _invite_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    facts = facts or {}

    def fx(key: str) -> str:
        return str(facts.get(key) or "").strip()

    title = fx("title") or "our campaign"
    # How the agent names it when turning an off-topic question away: "is chadhava", or plain "isi".
    kind = next((w for w in ("chadhava", "puja", "seva") if w in title.lower()), "")
    topic = f"is {kind}" if kind else "isi"
    how = fx("how_to_book") or f"{BUSINESS_NAME} ki website ya app"
    other = _block("Other details from the sheet:", _other_details(contact, set()))

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian online store for devotional products and "
             "services, to tell a past customer about one campaign and ask whether they are interested. You inform, ask "
             "once and respect the answer: never a sales push. Short: the whole offer is in your first turn.")

    details = f"""CAMPAIGN DETAILS (the only facts you may give about the campaign)
- Campaign: {title}
- What it is: {fx('about') or 'MISSING (say only the campaign name)'}
- Temple or place: {fx('place') or 'not given (do not name one)'}
- Date: {_spoken_date(fx('start_date'))}
- Price: {fx('price') or 'not given (if asked: "website ya app par price dikh jayega")'}
- How to book: {how}
- How it works, for questions and misunderstandings: {fx('more_info') or 'none'}

CUSTOMER DETAILS
- Customer name: {contact.get('customer_name') or 'MISSING (greet with "hello ji")'}
- Notes: {contact.get('notes') or 'none'}
{other}""" + _offers_block(facts)

    replies_first = [
        '- Interested ("haan", "achha hai", "batao", "book karna hai"): see INTERESTED.',
        '- Not interested ("nahi chahiye", "interest nahi hai", "abhi nahi"): see NOT INTERESTED.',
        '- Already booked ("maine book kar liya hai"): "bahut achha ji, dhanyavaad", goodbye, mark_outcome "already_booked", end_call.',
        ('- They want help to book, or a payment link: "theek hai ji, maine yeh note kar liya hai." Goodbye, '
         'mark_outcome "needs_help" with their request in notes, end_call. Never promise a link, a WhatsApp or a time.'),
        REPLY_WRONG,
    ]
    replies_last = ['- A bare "nahi" to "kya aap isme interested hain?" means not interested: see NOT INTERESTED. Anywhere else: "achha ji, kya?"']

    # Owner, 28 Oct 2026 (call 32118): the campaign's own opening, word for word, when written ("usi puja mein abhimantrit
    # hue the... kya aap ye yantra kharidne mein interested hain?"); the agent's fifteen-word summary otherwise.
    if opening := fx("opening"):
        ask = re.split(r"(?<=[.!।])\s+", opening)[-1] if opening.endswith("?") else "kya aap isme interested hain?"
        first = (f'1. Your first turn is the whole offer, word for word, then STOP and wait: "namaste <name> ji, main {agent_name}, '
                 f'{BUSINESS_NAME} se. {opening}" Never shorten it, add to it, or ask whether they have a minute.')
    else:
        ask = "kya aap isme interested hain?"
        first = f"""1. Your first turn is the whole offer: greet, say who you are, give the campaign in one short sentence and ask, then STOP and wait: "namaste <name> ji, main {agent_name}, {BUSINESS_NAME} se. <the campaign>. kya aap isme interested hain?" Never ask whether they have a minute or how they are. The campaign sentence is about fifteen words, in your own words, from CAMPAIGN DETAILS: what it is, where, the date, and the price if one is given. For example: "<date> ko <place> mein <campaign> hai, <price> rupaye se." Never read "What it is" out and never list the details."""
    keypad = keypad_on(contact, facts)
    if keypad:
        # Owner, 3 Nov 2026: the opening is the code's (keypad_greeting), with the phone-key options.
        _, ask = _invite_question(facts)
        first = (f'1. The greeting has been said for you: "{keypad_greeting(contact, voice, facts)}" A spoken answer to it '
                 f'is their answer to "{ask}".')

    flow = f"""THE USUAL ORDER (short turns; add nothing)
{first}
2. Their answer decides the rest: INTERESTED, NOT INTERESTED, already booked, or a question (QUESTIONS). If they did not catch it or ask who is calling, say it again, shorter, and ask again: "{ask}"
{GOODBYE}
Never end_call with a question unanswered."""

    if campaign_message(facts):
        # Owner's wording, 28 Oct 2026: the link goes on WhatsApp after the call (live_bridge.invite_link_close).
        interested = f"""INTERESTED
Say only this: "ji, main aapko WhatsApp par link bhej {forms(voice)["rahi"]} hoon, aap wahan se order kar sakte hain. dhanyavaad ji." Then mark_outcome "interested", end_call."""
    else:
        interested = f"""INTERESTED
One short turn: "bahut achha ji. aap {how} par book kar sakte hain", adding "<date> se pehle" only if a date is given. Then goodbye, mark_outcome "interested", end_call. You cannot book for them, take payment or send anything yourself."""

    # Calls 73 and 74: "mere paas time nahi hai" was accepted and the call closed, though the chadhava
    # is done for them and needs only a name and gotra. A reason born of not knowing that is cleared up once.
    not_interested = f"""NOT INTERESTED
Never push and never argue. First ask the reason, ONCE and gently: "koi khaas wajah ji?" A bare no ("nahi", "interested nahi hoon", "nahi chahiye") is not a reason: always ask. Skip the question only when their no already carried a reason ("nahi, paise nahi hain"). Then:
- A reason that comes from not knowing how it works, when CAMPAIGN DETAILS answer it (no time that day, cannot come to the temple, lives far away, how will they know it was done): clear it up ONCE, in one or two short sentences from CAMPAIGN DETAILS, then ask once more: "toh kya aap karwana chahenge?" For example, to "time nahi hai" when the details say the pandit does it in their name: "ji, aapko us din kuch karna nahi hai. pandit ji aapke naam aur gotra se puja karenge. toh kya aap karwana chahenge?" A yes: INTERESTED.
- Any other reason (does not want it, money, does not believe in it, already booked elsewhere), or a no after you cleared it up: accept it: "achha ji, samajh {forms(voice)["gayi"]}. dhanyavaad ji, aapka din shubh ho." mark_outcome "not_interested" with not_interested_reason in their words ("not given" if they would not say), then end_call.
Clear up at most one misunderstanding in a call. No offer, no discount, no pressure."""

    # Owner, 28 Oct 2026 (Baglamukhi yantra test call): "iska benefit kya hoga?" got "main kuch nahi bata sakti".
    questions = f"""QUESTIONS
Answer only from CAMPAIGN DETAILS, in one short sentence, then ask again "{ask}" A benefit asked about ("fayda kya hoga?"): one benefit written in CAMPAIGN DETAILS, as belief: "maana jaata hai ki ...". If the answer is not there: "iski poori jaankari website ya app par mil jayegi", and put the question in the question field of mark_outcome. Never guess a price, date, place, benefit or offer."""

    sections = [interested, not_interested, questions, """CAMPAIGN RULES (these always hold)
- Never promise what a puja, chadhava or product will bring: no "manokamna poori hogi", no "guaranteed", no health, money or family claims. Only a benefit written in CAMPAIGN DETAILS, said as belief ("maana jaata hai ki ...").
- No pressure and no false urgency: no "sirf aaj", "last chance", "jaldi kijiye".
- No discount, offer, gift or free item unless it is written in CAMPAIGN DETAILS or OFFERS.
- If the date has already passed, do not invite them to it: apologise, say goodbye, mark_outcome "unclear" with notes "campaign date passed"."""]
    if fx("never_say"):
        sections.append("NEVER SAY (set by the person who created this campaign)\n" + fx("never_say"))

    rules = [('- You cannot book, take payment, send links or change anything yourself: "maine note kar liya hai" is the most you say. '
              'Unknowns go in the question field.')]
    if campaign_message(facts):
        # Owner, 27 Oct 2026: the campaign's WhatsApp message (with its link) goes after the call, by code.
        # Owner's wording, 28 Oct 2026 (the code's close says the same: live_bridge.invite_link_close).
        rules = [('- You cannot book, take payment or change anything yourself. A WhatsApp message with the link goes to them '
                  f'after this call: when they are interested, or ask how to book or for the link, say "ji, main aapko WhatsApp '
                  f'par link bhej {forms(voice)["rahi"]} hoon, aap wahan se order kar sakte hain". Unknowns go in the question field.')]
    if keypad:
        rules.append(keypad_rule("campaign_invite"))

    return compose(contact=contact, voice=voice, intro=intro, details=details,
                   details_title="CAMPAIGN DETAILS or CUSTOMER DETAILS", details_noun="campaign and customer details",
                   replies_first=replies_first, replies_last=replies_last, flow=flow, sections=sections, rules=rules,
                   not_mine='"main aapka customer nahi hoon"', topic=topic)


CAMPAIGN_INVITE = CallType(
    key="campaign_invite",
    name="Campaign invite",
    kind="promotional",
    outcomes=("interested", "not_interested", "already_booked", "needs_help", "complaint", "call_later", "wrong_number",
              "do_not_call", "unclear"),
    positive=("interested",),
    details={
        "not_interested_reason": "Why they are not interested, in their words; 'not given' if they would not say.",
        "question": "A question they asked that the campaign details could not answer.",
        **COMMON_DETAILS,
    },
    build_prompt=_invite_prompt,
    confirm_question="kya aap isme interested hain?",
    # Owner, 28 Oct 2026: a campaign's own opening may ask "kya aap ye yantra mangwana chahenge?".
    main_question=r"(interested|इंटरेस्टेड)\s*(hain|hai|हैं|है)|\bchahenge\b|चाहेंगे",
    summary_keys=("not_interested_reason", "question", "notes", "callback_time"),
    description="Tell past customers about a campaign, ask if they are interested, and note the reason if not.",
    columns=(
        Column("customer_name", "Customer name", True, "Ramesh Kumar"),
        Column("phone", "Phone", True, "PUT 10 DIGIT NUMBER"),
        Column("notes", "Notes", False, "Bought a Rudraksha mala in 2025"),
    ),
    facts=(
        Fact("title", "Campaign name, as the agent will say it", True, hint="Navratri Kamakhya Chadhava"),
        Fact("about", "What it is, in one or two lines", True, long=True,
             hint="Navratri par Kamakhya Mandir mein aapke naam se chadhava; prasad ghar par aayega."),
        # Owner, 28 Oct 2026: said word for word after "namaste <name> ji, main <agent>, <business> se." (_invite_prompt).
        Fact("opening", "What the agent says after its hello, word for word; end with \"... interested hain?\" or \"... chahenge?\" (optional)",
             long=True, hint="Jis puja mein aapka sankalp liya gaya tha, usi puja mein Maa Baglamukhi Yantra aur Vyapar Vridhi "
                             "Yantra abhimantrit hue the. Kya aap ye yantra kharidne mein interested hain?"),
        Fact("place", "Temple or place", hint="Kamakhya Mandir, Guwahati"),
        Fact("price", "Price", hint="551 rupaye"),
        # Stored as start_date since the first campaign, but it is the day the agent names and after which it
        # stops inviting. Labelled "Campaign start date", it was read as the day calling starts (25 Sep, for
        # a 3 Oct puja), which would have ended every call from the next day on as "date passed".
        # Optional (owner, 27 Oct 2026): with none, the agent names no date (_date_line).
        Fact("start_date", "Date of the puja or event (optional)", date=True),
        Fact("how_to_book", "How to book", True, hint="Vedic Vaibhav ki website ya app"),
        Fact("more_info", "How it works, and answers to likely questions (optional)", long=True,
             hint="Aapko aana nahi hai: pandit ji aapke naam aur gotra se puja karenge. Puja ka video WhatsApp par aayega. "
                  "Prasad 10 din mein ghar pahunchega."),
        Fact("never_say", "Never say (optional)", long=True, hint="Discount ki baat mat karna."),
        # Owner, 27 Oct 2026: coupons and discounts the agent may mention (_offers_block), as for the other campaigns.
        Fact("offers", "Offers (optional)", long=True, hint='[{"title": "10% off", "code": "PUJA10", "details": "..."}]'),
        Fact("whatsapp_message", "WhatsApp message after the call (optional)", long=True),
        # Owner, 3 Nov 2026: "on" = the call opens with press 1 / 2 / 3 / 0 (keypad_greeting); empty = off.
        Fact(KEYPAD_KEY, "Keypad options 1/2/0/3 (optional)", hint="on"),
    ),
)


# --- abandoned cart -------------------------------------------------------------------------------

# Owner, 22 Oct 2026: the cart call in the owner's words, short. Find out what stopped them; if they want it, the cart's
# link (and the campaign's WhatsApp coupon, when it has one) goes to them on WhatsApp (messaging.queue_cart_coupon). No
# order, address or payment is taken on the call.

def call_coupon(contact: dict) -> dict | None:
    """The coupon this contact's call may offer: the one attached when the call was placed (coupons.attach), else
    looked up now. None on any trouble: a call never waits on, or fails for, a coupon."""
    if "_coupon" in contact:
        return contact["_coupon"]
    try:
        # Here, not at the top: coupons reads the call types.
        from .coupons import offer_for
        return offer_for(contact)
    except Exception:
        log.exception("coupon lookup failed for contact %s; no offer on this call", contact.get("id"))
        return None


# Owner, 23 Oct 2026 (test calls): a bare "nahi" to the greeting's "koi issue tha?" means only "no problem", not a no to the
# product; the agent hung up on it. This is asked instead (live_bridge asks it too, through probe_first, if the agent
# still closes on that first "nahi").
# Owner, 24 Oct 2026: this wording.
CART_WANT_QUESTION = "aap yeh order karna chahenge?"
# "Bas dekh raha tha": the link, for later, if they want it. "link WhatsApp" is the call's main question (main_question).
CART_LATER_QUESTION = "ji, baad ke liye link bhej doon?"   # owner's wording, 24 Oct 2026
CART_SEND_COUPON = "coupon WhatsApp par bhej doon?"
# Owner, 23 Oct 2026: a no gets this and nothing more (wording: owner, 23 Oct, fewer "ji").
CART_NOT_NOW = "koi baat nahi, dhanyavaad ji."

# Every line below that sends something says "WhatsApp ... bhej": live_bridge.promised_on_whatsapp queues the message by
# it. The link opens their cart, where COD and prepaid are both offered, so it is never called a "payment link".


def cart_help_line(voice: str) -> str:
    """Owner, 24 Oct 2026: there was an issue; ask what it was, in these words."""
    return "kya issue aa raha tha?"


def cart_link_line(voice: str, sent: dict | None = None) -> str:
    """After helping: the link, if they want it. sent: the message is on their WhatsApp already, so ask if they want it."""
    return f"ji. {CART_WANT_QUESTION}" if sent else "ji. agar aap order complete karna chahein, toh link WhatsApp par bhej doon?"


def cart_sent_close(sent: dict | None, cod: bool = False) -> str:
    """Owner, 26 Oct 2026: from the second call on, the customer is reminded of the message they have in these words.
    "WhatsApp par bhej" keeps live_bridge's promised_on_whatsapp seeing the WhatsApp line. (27 Oct 2026: it also goes
    again after every call.)"""
    what = "link aur coupon" if (sent or {}).get("code") else "link"
    tail = "wahan se order kar lijiye, COD choose kar lijiyega" if cod else "wahan se order kar lijiye"
    return f"ji, maine {what} aapke WhatsApp par bhej diya hai, {tail}. dhanyavaad ji."


def cart_link_close(voice: str, sent: dict | None = None) -> str:
    """Their yes to the link: it goes on WhatsApp and the call ends. Owner's wording, 23 Oct 2026 ("send": live_bridge's
    _WA_PROMISE takes it as well as "bhej")."""
    if sent:
        return cart_sent_close(sent)
    return f"theek hai, main link aapke WhatsApp par send kar {forms(voice)['rahi']} hoon. dhanyavaad ji."


def cart_offer_line(coupon: dict, cod: bool = False) -> str:
    """They want to order (cod: and asked for COD): the campaign's prepaid offer, once, with the dashboard's own
    discount ("10%" -> "दस परसेंट")."""
    off = _spoken_off(coupon)
    # Owner's wording, 23 Oct 2026.
    gain = (f"coupon code use karne se {off} off mil raha hai" if coupon.get("max_discount")
            else f"coupon code se yeh offer mil raha hai: {off}")
    return ("ji, COD ho sakta hai. " if cod else "ji, ") + f"prepaid order par {gain}. aap prepaid order karna chahenge?"


def cart_coupon_close(voice: str, sent: dict | None = None) -> str:
    """Their yes to getting the coupon: it goes in the same WhatsApp message as the link."""
    if sent:
        return cart_sent_close(sent)
    return f"ji, coupon aur link WhatsApp par bhej {forms(voice)['rahi']} hoon. dhanyavaad ji."


def cart_cod_close(voice: str, sent: dict | None = None) -> str:
    """They keep cash on delivery: no second pitch, the link goes on WhatsApp and the call ends."""
    if sent:
        return cart_sent_close(sent, cod=True)
    return f"theek hai ji, link WhatsApp par bhej {forms(voice)['rahi']} hoon. COD choose kar lijiyega. dhanyavaad ji."


def cart_price_close(voice: str, sent: dict | None = None) -> str:
    """"Price zyada hai": the offer (the campaign's coupon, when it has one) and the link go on WhatsApp; the call ends."""
    if sent:
        return cart_sent_close(sent)
    return (f"ji, abhi agar koi applicable offer hua toh main WhatsApp par share kar {forms(voice)['deti']} hoon. "
            "aap check kar lijiyega. dhanyavaad ji.")


def cart_sent(contact: dict | None) -> dict | None:
    """The cart message this customer already has ({"id", "code"}), as attached when the call was placed
    (coupons.attach), else looked up now. None on any trouble: the call then speaks as on a first call."""
    contact = contact or {}
    if "_cart_sent" in contact:
        return contact["_cart_sent"]
    try:
        from .messaging import sent_cart_message   # here, not at the top: messaging reads the call types
        return sent_cart_message(contact)
    except Exception:
        log.exception("cart message lookup failed for contact %s", contact.get("id"))
        return None


# Words that only sell, at the start of a catalogue title, and number words said better as digits ("7 Mukhi").
_SELLING_WORDS = re.compile(r"^((natural|certified|lab[\s-]*certified|original|genuine|authentic|premium|energi[sz]ed|"
                            r"abhimantrit|100\s*%)\s+)+", re.IGNORECASE)
_TITLE_TAIL = re.compile(r"\s+with\s+.*$|\s+for\s+(men|women|man|woman|boys|girls|kids|unisex|him|her|all)\b.*$|"
                         r"\s*[(\[|].*$|\s+[-–]\s+.*$", re.IGNORECASE)
_NUMBER_WORDS = {w: str(i) for i, w in enumerate((
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen",
    "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "twenty-one"))}


def spoken_product(product: str) -> str:
    """The cart's product as a person names it on the phone (owner, 23 Oct 2026): the first item's title without its
    variant, selling words and audience ("Natural Certified Seven Mukhi Rudraksha Pendant with Premium Adjustable
    Thread for Men & Women" -> "7 Mukhi Rudraksha Pendant"), plus how many more items. "" when there is none."""
    items = [i.strip() for i in str(product or "").split(", ") if i.strip()]
    if not items:
        return ""
    title = re.sub(r"\s+x\d+$", "", items[0])
    short = _SELLING_WORDS.sub("", _TITLE_TAIL.sub("", title)).strip(" ,;:") or title
    short = " ".join(_NUMBER_WORDS.get(w.lower(), w) for w in short.split())
    more = len(items) - 1
    return short + ("" if not more else " aur ek aur product" if more == 1 else " aur kuch aur products")


def _offer_detail(coupon: dict) -> str:
    """The Offer line of CART DETAILS: what goes on WhatsApp with the link, never said as a promise of a discount."""
    until = _parse_date(str(coupon.get("valid_until") or ""))
    valid = f", till {until.day} {until:%B}" if until else ""
    gives = ": ".join(x for x in (coupon.get("title"), coupon.get("details")) if x) or coupon.get("code")
    return f"- Offer (goes on WhatsApp with the link; never read the code out): {gives}{valid}\n"


def _cart_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    answers = str((facts or {}).get("answers") or "").strip()
    # The cart's own link goes into the WhatsApp message only; never read out.
    other = _block("Other details from the sheet:", _other_details(contact, {"cart_url"}))
    # Owner, 26 Oct 2026: from the second call on, the cart message (sent after the first call, picked up or not) is on
    # their WhatsApp already: the lines remind them of it, and the offer is the coupon that message carried.
    sent = cart_sent(contact)
    if sent:
        from .coupons import offer_by_code   # here, not at the top: coupons reads this module
        coupon = offer_by_code(contact, sent["code"]) if sent["code"] else None
    else:
        coupon = call_coupon(contact)
    product = str(contact.get("product") or "").strip()
    short = spoken_product(product)

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian company selling devotional products. The customer "
             f"looked at a product on {STORE_NAME} but did not finish the order. Find out what stopped them, help, and if they "
             "want it, the link goes to them on WhatsApp. Never a sales push.")

    details = f"""CART DETAILS
- Customer name: {contact.get('customer_name') or 'MISSING'}
- In the cart: {(product + (f' (say it as "{short}")' if short != product else '')) if product else 'not on record'}
- Cart value: {amount_line(contact, 'not on record (never name a price)')}
- Notes: {contact.get('notes') or 'none'}
- Answers to common questions: {answers or 'none'}
{_offer_detail(coupon) if coupon else ""}{_sent_detail(sent) if sent else ""}{other}{_offers_block(facts or {})}"""

    replies_first = [
        (f'- "Nahi chahiye", "abhi nahi", "baad mein", bought it elsewhere: "{CART_NOT_NOW}" mark_outcome "not_interested" '
         'with not_interested_reason in their words ("not given" if none), end_call. Ask nothing else. A bare "nahi" to '
         '"koi issue tha?" is not this: it means no problem (step 2).'),
        '- They ask for COD or cash at any point ("COD hoga?", "COD chahiye"): ORDERING.',
        '- "Price zyada hai", "mehenga hai", or they ask for a discount: PRICE.',
        ('- Already ordered ("order kar diya", "le liya"): "achha ji, dhanyavaad ji, aapka din shubh ho." mark_outcome '
         '"already_ordered", end_call.'),
        (f'- "Kaunsa product?": "{short} ji." The greeting named it: never name it again otherwise.' if short else
         '- "Kaunsa product?": none is on record; never name one. Ask what they were looking at.'),
        ('- A question about the product (price, size, what it is, delivery): answer it in one short sentence from CART '
         f'DETAILS or product_info, then "{cart_link_line(voice, sent)}"'),
        *([(f'- The WhatsApp message did not come ("nahi aaya", "nahi mila"): "ji, main abhi dobara bhej '
            f'{forms(voice)["deti"]} hoon." Then carry on.')] if sent else []),
        (f'- "{BUSINESS_NAME} se dekha tha" or "{STORE_NAME} kya hai?": "ji, {STORE_NAME} {BUSINESS_NAME} ka hi online store '
         'hai." Then go on.'),
        REPLY_WRONG,
    ]
    replies_last = [('- A sound you could not make out, or "repeat kar dijiye": say your last line again, shorter. Never take '
                     'it as a no.')]

    # Owner, 22 Oct 2026: the cart call in the owner's words. 23 Oct: the product in the greeting; a bare "nahi" is "no
    # problem" (asked what they meant); just looking gets the link for later; wanting it gets the prepaid offer once.
    # 26 Oct: once the link is on their WhatsApp (a later call), just looking is asked whether they want it instead.
    just_looking = (f'"{CART_WANT_QUESTION}" A yes: ORDERING. A no: "{CART_NOT_NOW}" mark_outcome "not_interested", end_call.'
                    if sent else
                    f'"{CART_LATER_QUESTION}" A yes: "{cart_link_close(voice)}" mark_outcome "wants_to_order", end_call. '
                    f'A no: "{CART_NOT_NOW}" mark_outcome "not_interested", end_call.')
    flow = f"""THE USUAL ORDER
1. The greeting has been said for you: "{_cart_greeting(contact, voice)}" Wait for their answer.
2. Their answer, or their answer to "{CART_WANT_QUESTION}":
   - A bare "nahi" / "koi issue nahi" to the greeting (no problem; not a no to the product): "{CART_WANT_QUESTION}" Then go by their answer, below. A "nahi" to that question is a no: "{CART_NOT_NOW}" mark_outcome "not_interested", end_call.
   - Just looking ("bas dekh raha tha", "aise hi dekha tha"): {just_looking}
   - They want it ("haan, lena tha", "le lenge", "order karna hai"): ORDERING. "Nahi, order karna hai" is the same: whatever follows a "nahi" decides.
   - "Haan" (there was an issue), a vague problem, or a doubt: "{cart_help_line(voice)}" Then answer what they say in one or two short sentences, and "{cart_link_line(voice, sent)}" A yes: {"ORDERING" if sent else f'"{cart_link_close(voice)}" mark_outcome "wants_to_order", end_call'}. A no: "{CART_NOT_NOW}" mark_outcome "not_interested", end_call.
Never take an order, an address or a payment on the call, never read a link or a code out, and never call it a "payment link".
End the call only with one of the closing lines in this prompt, word for word; never with "namaste". Never end_call while a question of yours is unanswered."""

    if coupon:
        ordering = f"""ORDERING (they want it; the prepaid offer is made once in a call)
a. "{cart_offer_line(coupon)}" If they asked for COD ("lena hai, but COD chahiye"): "{cart_offer_line(coupon, cod=True)}" If the offer was already made: c.
b. A yes to prepaid ("haan", "prepaid kar lenge"), or they ask for the code: {f'"{cart_coupon_close(voice, sent)}"' if sent else f'"{CART_SEND_COUPON}" A yes: "{cart_coupon_close(voice)}"'} mark_outcome "wants_to_order" (notes "chose prepaid" only if they said prepaid in words; asking for the code is not that), end_call.
c. Still COD, or a no to prepaid or to the coupon: "{cart_cod_close(voice, sent)}" mark_outcome "wants_to_order" with notes "wants COD", end_call. Never repeat the offer."""
    else:
        ordering = f"""ORDERING (they want it; there is no offer: never name a discount or a code)
"{cart_link_close(voice, sent)}" mark_outcome "wants_to_order", end_call. They asked for COD: "ji, COD ho sakta hai." then "{cart_cod_close(voice, sent)}" mark_outcome "wants_to_order" with notes "wants COD", end_call."""

    price = f"""PRICE
"Price zyada hai", "mehenga hai", or a discount asked for, at any point: "{cart_price_close(voice, sent)}" mark_outcome "wants_coupon" with not_interested_reason in their words, end_call. Never name a discount or a code yourself."""

    rules = [('- You cannot place the order or take payment yourself: the link (and any offer) goes on WhatsApp by itself '
              'after the lines above. Promise nothing else: "maine note kar liya hai" is the most you say about anything else.')
             if not sent else
             ('- You cannot place the order or take payment yourself: the link is on their WhatsApp already. Never say it '
              'goes now. Promise nothing else: "maine note kar liya hai" is the most you say about anything else.')]
    if keypad_on(contact, facts or {}):
        rules.append(keypad_rule("abandoned_cart"))
        # The opening is the code's (keypad_greeting): it asks "क्या आप इसे ऑर्डर करना चाहेंगे?" and gives the keys.
        step1 = (f'1. The greeting has been said for you: "{keypad_greeting(contact, voice)}" A spoken answer to it is their '
                 f'answer to "{CART_WANT_QUESTION}": a "nahi" is a no.')
        flow = re.sub(r"(?m)^1\. The greeting has been said for you: .*$", lambda _: step1, flow)
        # Owner, 3 Nov 2026: a cash-on-delivery customer is booked on the call (draft_orders); the address is taken here
        # when the system asks for it.
        flow = flow.replace("Never take an order, an address or a payment on the call,",
                            "Never take a payment on the call; an order and an address only as BOOKING says;")
        extra = [TAKING_AN_ADDRESS, f"""BOOKING (the customer chose cash on delivery; the system books the order)
When the system says it needs the delivery address: TAKING AN ADDRESS. After their yes to the read-back, say exactly "{keypad_booked(contact)}" then mark_outcome "wants_to_order" with notes "COD order booked, address taken on the call", end_call. Never say it is booked before that yes."""]
    else:
        extra = []

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="CART DETAILS",
                   details_noun="cart details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[ordering, price, *extra], rules=rules,
                   not_mine='not the person named, or "maine kuch cart mein nahi dala"', topic="aapke order")


def _sent_detail(sent: dict) -> str:
    """The CART DETAILS line of a later call: the message they already have."""
    what = "the cart link and the coupon" if sent.get("code") else "the cart link"
    return (f"- WhatsApp: {what} went to them on WhatsApp after an earlier call. Remind them with the lines here; never "
            "offer to send it.\n")


def _cart_greeting(contact: dict, voice: str) -> str:
    """The first line, in Devanagari: name, who calls, the product, why, one question (owner, 23 Oct 2026: the product is
    named in the first turn). Without a product on record it names none."""
    # "Rajeshsah ." as the list had it: no stray dot or comma before "जी" (owner, 24 Oct 2026).
    name = re.sub(r"[\s.,;:_\-]+$", "", str(contact.get("customer_name") or "")).strip()
    product = _product_said(contact)
    what = f"आपने {product} देखा था, order complete नहीं हुआ था।" if product else "आपका order complete नहीं हुआ था।"
    # "जी।", not "जी,": the name is a sentence of its own, so the rest of the greeting is the same on every call and is
    # replayed from a saved clip instead of paid for again (owner, 24 Oct 2026: the greeting was 69% of the voice cost).
    # Owner's wording, 24 Oct 2026: "मैं वैदिक शॉप से बोल रही हूँ।", no agent name (as in the order call's greeting).
    speaking = "बोल रही हूँ" if forms(voice)["rahi"] == "rahi" else "बोल रहा हूँ"
    return (f"नमस्ते {name + ' ' if name else ''}जी। मैं {PRONUNCIATION.get(STORE_NAME, STORE_NAME)} से {speaking}। "
            f"{what} कोई issue था?")


ABANDONED_CART = CallType(
    key="abandoned_cart",
    name="Abandoned cart",
    kind="promotional",
    outcomes=("wants_to_order", "wants_coupon", "not_interested", "already_ordered", "needs_help", "complaint",
              "call_later", "wrong_number", "do_not_call", "unclear"),
    positive=("wants_to_order",),   # a yes to the WhatsApp link
    details={
        "not_interested_reason": "Why they do not want it, or what stopped them, in their words; 'not given' if they would not say.",
        "question": "A problem or question only the team can handle (payment failed, website trouble, something you could not answer).",
        **COMMON_DETAILS,
    },
    build_prompt=_cart_prompt,
    probe_first=("not_interested",),   # a "nahi" to the greeting's question is not a no to the product (CART_WANT_QUESTION)
    greeting=_cart_greeting,
    offers_coupon=True,
    confirm_question="link WhatsApp par bhej doon?",
    # The link questions ("...link WhatsApp par bhej doon?"); "payment link" still matched for calls made before 23 Oct 2026.
    main_question=r"payment\s*link|पेमेंट\s*लिंक|link\s+whats\s*app|link\s+(bhej|भेज)|लिंक",
    summary_keys=("not_interested_reason", "question", "callback_time", "notes"),
    description="Call customers who left items in their cart: find out what stopped them, help, and send the cart link "
                "(and the campaign's WhatsApp coupon) on WhatsApp to those who want it. COD or prepaid is their choice.",
    columns=(
        Column("customer_name", "Customer name", True, "Ramesh Kumar"),
        Column("phone", "Phone", True, "PUT 10 DIGIT NUMBER"),
        Column("product", "Items in cart", True, "Rudraksha Mala 108 beads"),
        Column("quantity", "Quantity", False, "1"),
        Column("amount", "Cart value", False, "1499"),
        Column("address", "Address", False, "12 MG Road, Indore, MP 452001"),
        Column("notes", "Notes", False, ""),
    ),
    facts=(
        Fact("answers", "Answers to common customer questions (optional)", long=True,
             hint="Sabhi products original hain. Cash on delivery available hai. Delivery 5-7 din mein."),
        # Owner, 22 Oct 2026: the coupon as for order confirmations, set on the campaign; it goes in the WhatsApp message.
        Fact("offers", "Offers (optional)", long=True, hint='[{"title": "Free delivery on prepaid", "details": "..."}]'),
        Fact("whatsapp_coupon", "Share a coupon on WhatsApp (optional)", hint="The code of one of the offers above"),
        # Owner, 2 Nov 2026: "on" = the call opens with press 1 / 2 / 3 / 0 (keypad_greeting); empty = off.
        Fact(KEYPAD_KEY, "Keypad options 1/2/0/3 (optional)", hint="on"),
    ),
)


# --- a single call for a reason typed on the dashboard ----------------------------------------------
# Owner, 21 Oct 2026: the Make a call dialog takes a reason, and the agent calls about that.

def call_reason(contact: dict) -> str:
    """The reason typed for this call: set for the call (with_reason), or kept on a contact made for it."""
    return str(contact.get("call_reason") or _xv(contact, "call_reason")).strip()


def with_reason(contact: dict, reason: str) -> dict:
    """This call is about `reason`, whatever the contact's own call type: only for this call, the row keeps its type."""
    reason = str(reason or "").strip()[:1000]
    return {**contact, "campaign": CUSTOM_CALL.key, "call_reason": reason} if reason else contact


def _custom_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    rahi = forms(voice)["rahi"]
    order = ", ".join(x for x in (str(contact.get("order_id") or ""), str(contact.get("product") or "")) if x)
    other = _block("Other details from the sheet:", _other_details(contact, {"call_reason"}))

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian company selling devotional products, for one "
             "reason its team wrote below. Say it, answer what you can, note the customer's reply. Warm and short.")

    details = f"""REASON FOR THIS CALL (written by the team; the only facts you may give about it)
{call_reason(contact) or 'MISSING: say who you are, ask how you can help, and note what they say.'}

CUSTOMER DETAILS
- Customer name: {contact.get('customer_name') or 'MISSING (greet with "hello ji")'}
- Order: {order or 'none'}
{other}"""

    replies_first = [
        '- They agree, accept or confirm: close (step 3).',
        ('- They refuse or are not interested: accept it at once, never push; ask the reason once only if the reason for '
         'the call asks you to. Close with mark_outcome "not_interested".'),
        ('- They want something you cannot do (a refund, a link, a person to call them): "theek hai ji, maine yeh note kar liya hai." '
         'Close with mark_outcome "needs_help" and what they want in notes. Never promise a time.'),
        REPLY_WRONG,
    ]

    flow = f"""THE USUAL ORDER (short turns)
1. Greet, say who you are and give the reason in one or two short sentences of your own, then STOP and wait: "namaste <name> ji, main {agent_name}, {STORE_NAME} se bol {rahi} hoon. <the reason>." If the reason asks the customer something, end with that question. Never read the reason out word for word; never ask whether they have a minute.
2. Answer their questions from REASON FOR THIS CALL in one short sentence each. Not there: "maine aapka sawaal note kar liya hai", and put it in question.
3. Once they have answered: "theek hai ji, maine yeh note kar liya hai. dhanyavaad ji, aapka din shubh ho." Then mark_outcome ("done" unless a case above says otherwise) with their answer in reply, then end_call.
{GOODBYE}"""

    rules = ['- Say only what REASON FOR THIS CALL says: never invent a price, date, offer, refund or promise.',
             '- You cannot change orders, take payment or send anything yourself: "maine note kar liya hai" is the most you say.']

    return compose(contact=contact, voice=voice, intro=intro, details=details,
                   details_title="REASON FOR THIS CALL or CUSTOMER DETAILS", details_noun="reason and customer details",
                   replies_first=replies_first, replies_last=[], flow=flow, sections=[], rules=rules,
                   not_mine='"main aapka customer nahi hoon"', topic="isi baat")


CUSTOM_CALL = CallType(
    key="custom",
    name="Custom reason",
    kind="service",
    outcomes=("done", "not_interested", "needs_help", "complaint", "call_later", "wrong_number", "do_not_call", "unclear"),
    positive=(),
    details={
        "reply": "What the customer said to the reason for the call, in their words.",
        "question": "A question they asked that the reason for the call did not answer.",
        **COMMON_DETAILS,
    },
    build_prompt=_custom_prompt,
    summary_keys=("reply", "question", "notes", "callback_time"),
    description="One call about a reason typed in the Make a call dialog.",
)


# The call types a campaign can be made of. CUSTOM_CALL is not one: it exists only for single calls.
CALL_TYPES: dict[str, CallType] = {t.key: t for t in (ORDER_CONFIRMATION, BOOKING_CONFIRMATION, CAMPAIGN_INVITE, ABANDONED_CART)}
ALL_TYPES: dict[str, CallType] = {**CALL_TYPES, CUSTOM_CALL.key: CUSTOM_CALL}
DEFAULT = ORDER_CONFIRMATION


def missing_facts(camp: dict | None) -> list[str]:
    """The campaign details its call type needs that are still empty. A draft may lack them (the new dashboard's
    wizard asks for them after the campaign exists), but it is never called until they are filled in."""
    ct = CALL_TYPES.get(str((camp or {}).get("call_type") or ""))
    facts = (camp or {}).get("facts") or {}
    if not ct:
        return []
    return [f.label for f in ct.facts if f.required and not str(facts.get(f.key) or "").strip()] + message_missing(facts)


def call_type_for(contact: dict | None) -> CallType:
    """The call type a contact row names; order confirmation when it names none or an unknown one."""
    key = str((contact or {}).get("campaign") or "").strip()
    return ALL_TYPES.get(key, DEFAULT)


# Owner, 28 Oct 2026: the campaign's goal, written in the new dashboard's Details step (any call type). Empty: the
# type's own goal alone.
GOAL_KEY = "goal"


def goal_block(facts: dict) -> str:
    goal = str(facts.get(GOAL_KEY) or "").strip()
    return f"\n\nCAMPAIGN GOAL (the owner's words for this campaign; steer the call toward it, within the rules above)\n{goal}" if goal else ""


def get_prompt(contact: dict, voice: str) -> str:
    """The system prompt for one call: the contact's call type, with its campaign's facts."""
    from . import db  # here, not at the top: db is not needed to define the types
    from .catalog import product_block
    campaign = db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None
    facts = (campaign or {}).get("facts") or {}
    return call_type_for(contact).build_prompt(contact, voice, facts) + goal_block(facts) + product_block(contact, voice)
