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
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from .config import BUSINESS_NAME, PRONUNCIATION, STORE_NAME
from .prompts import (GOODBYE, REPLY_WRONG, TAKING_AN_ADDRESS, amount_line, compose, delivery_line, forms, hindi_number,
                      missing_block, persona)

log = logging.getLogger("call_types")

# Set by us from the phone line, never by the agent: nobody answered, or the call ended before
# the agent recorded anything (customer hung up, line dropped).
LINE_OUTCOMES = ("no_answer", "busy", "dropped")
# Every call type offers these; they are about the line and the person, not the purpose.
COMMON_OUTCOMES = ("call_later", "wrong_number", "do_not_call", "unclear")

COMMON_DETAILS = {
    "callback_time": "When to call back if the customer asked for a later call.",
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
    tidy = lambda p: p.title() if p.isupper() else p   # noqa: E731  ("KHORDA" -> "Khorda")
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


def paid_online(contact: dict | None) -> bool:
    """The order's data says it is paid (prepaid / online); an empty or cash-on-delivery payment is not."""
    pay = re.sub(r"[\s_-]+", " ", str((contact or {}).get("payment_mode") or "")).strip().lower()
    return bool(pay) and not ("cod" in pay or "cash" in pay or "delivery" in pay)


def prepaid_coupon_for(contact: dict | None, facts: dict | None = None) -> dict | None:
    """The coupon this call may offer on WhatsApp: an order not paid yet (cash on delivery, or no payment in the data:
    call 509, the customer is asked), a campaign that shares one, and a customer who does not have this code already.
    facts: the campaign's (looked up when not given). Never raises."""
    try:
        if not contact or paid_online(contact):
            return None
        if facts is None:
            from . import db   # here: db is not needed by the rest of this module
            facts = ((db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None) or {}).get("facts") or {}
        offer = whatsapp_offer(facts)
        if not offer:
            return None
        from . import messaging   # here: messaging reads this module
        return None if messaging.already_sent(contact.get("phone") or "", offer["code"]) else offer
    except Exception:  # noqa: BLE001
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
    """Their answer was cash on delivery (or unsure): the offer, once, to win them over. Their yes to it is followed by
    PREPAID_SEND_QUESTION."""
    return f"ji. hamare yahan abhi prepaid orders par offer chal raha hai. {_gain(offer)}. aap prepaid karna chahenge?"


def prepaid_online_line(offer: dict) -> str:
    """Their answer was online payment: the coupon goes with it."""
    return f"ji, {_gain(offer)}. {PREPAID_SEND_QUESTION}"


def prepaid_yes_line(voice: str = "") -> str:
    # Owner, 22 Oct 2026: a yes moves the order to online payment (shopify.mark_prepaid) and the code goes on WhatsApp.
    return f"ji theek hai, main coupon aapke WhatsApp par bhej {forms(voice)['rahi']} hoon. dhanyavaad ji."


PREPAID_NO_LINE = "koi baat nahi ji, aapka order confirm ho gaya hai. dhanyavaad ji, aapka din shubh ho."


def _whatsapp_coupon_block(offer: dict | None, contact: dict | None = None, voice: str = "") -> str:
    if not offer:
        return ""
    cod = bool(str((contact or {}).get("payment_mode") or "").strip())
    paid = ("they say it is already paid: MISMATCH (payment), no offer." if cod else
            f'they say it is already paid: "theek hai ji, aapka order confirm ho gaya hai. dhanyavaad ji, aapka din shubh ho." '
            'mark_outcome "confirmed" with notes "customer says already paid online", end_call. No offer.')
    return f"""PAYMENT AND WHATSAPP COUPON (owner, 21 Oct 2026; the payment is asked here and nowhere else)
After their yes to the address, with nothing different, in place of the usual close:
a. Ask exactly these words, nothing added: "{PREPAID_PAYMENT_QUESTION}" Then wait. If {paid}
b. They choose online: "{prepaid_online_line(offer)}" Then d.
   They want cash on delivery, or are unsure: try once to win them over, exactly: "{prepaid_offer_line(offer)}"
   Then wait for their answer.
c. A yes to prepaid, or they ask what the code is: "{PREPAID_SEND_QUESTION}" Never read the code out.
   A question about the offer: its answer in one short sentence from OFFERS, then "{PREPAID_SEND_QUESTION}"
   A no, or anything else: "{PREPAID_NO_LINE}" mark_outcome "confirmed", end_call.
d. A yes to sending it ("haan", "bhej do", "theek hai"): "{prepaid_yes_line(voice)}" mark_outcome "confirmed", end_call. The payment change and the code on WhatsApp happen by themselves; promise nothing else.
   A no: "{PREPAID_NO_LINE}" mark_outcome "confirmed", end_call.
Ask about the payment once and make the offer once: after a no, never ask again or push. Never after a mismatch, a cancellation or any other result."""


def _order_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    rahi = forms(voice)["rahi"]
    payment = (contact.get("payment_mode") or "").upper()
    payment_line = "cash on delivery" if payment == "COD" else "already paid online" if payment else "unknown"
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
        # Call 282: "teeno mila ke 1300, ek ko nahi?" about a combo of three bracelets was taken as a dispute of the amount.
        '- A question about what the order holds or what the amount covers ("teeno milenge?", "combo mein kya hai?", "ek ka hai ya sab ka?"): '
        'not a mismatch. Answer from PRODUCT INFO in one sentence (a combo or set is one order, all of it for the amount above), then your question again.',
        '- They want to cancel: CANCELLATION.',
        # Calls 93, 96 and 98 handled "I did not order" three different ways.
        f'- They did not place this order: never offer to cancel. Ask once "kya yeh <name> ji ka number hai?" Not theirs: wrong number. '
        'Theirs: "theek hai ji, maine yeh note kar liya hai. dhanyavaad ji, aapka din shubh ho", '
        'mark_outcome "not_ordered", end_call.',
        f'- "{BUSINESS_NAME} kaun? Maine to {STORE_NAME} se order kiya": "ji, {STORE_NAME} {BUSINESS_NAME} ka hi online store hai." Then your question again. '
        # Call 119: "kisi aur site se kiya tha" was closed as not ordered at once.
        f'"Maine to kisi aur site se kiya tha": the same, then "kya aapne <product> order kiya tha?"; only a no to that is "did not place this order".',
        # Call 120 called this off topic and hung up.
        f'- "Aur kya-kya products hain?": not off topic. "ji, saare products {STORE_NAME} par dekh sakte hain." Then your question again.',
    ]
    replies_last = ['- A bare "nahi" to "kya aapka order confirm hai?": "achha ji, kya galat hai isme?"']

    # Every order ends at step 3 then 4, whatever happened on the way (a mismatch included): on call
    # 102 the agent never came back to the missing address after the customer disputed the amount.
    flow = f"""THE USUAL ORDER
1. Greet, then wait: "namaste <name> ji, main {agent_name}, {STORE_NAME} se bol {rahi} hoon. bas aapke order ka ek chhota sa confirmation call hai, ek minute baat ho sakti hai?"
2. After their yes, the order in one sentence, always ending in "kya aapka order confirm hai?": "ji, aapne {STORE_NAME} se <product>, <quantity> piece, <amount> rupaye ka order kiya tha, <payment>. kya aapka order confirm hai?"
3. Address, every time (call 122 skipped it). On record: ask it as its own question, "ji, aur delivery <address> mein karni hai?", and get a yes. A yes in any words ("haan ji", "sahi hai", "yahi hai", "isi address par") is a yes: go on. An answer you could not make out, or a mixed one ("na haan ji"): only "ji, ek baar delivery address confirm kar dijiye." Never ask for the whole address unless it is MISSING or they say it is wrong (call 515 asked for it after "haan ji, isi address par delivery karni hai"). MISSING, or they say it is wrong: "ji, delivery address ek baar poora bata dijiye" (TAKING AN ADDRESS). This comes after a mismatch too; skip it only after a cancellation.
4. Close in one turn, straight after their yes to the address (nothing more to ask), then mark_outcome, then end_call:
   - all correct (owner, 19 Oct 2026), exactly this and nothing after it: "{ORDER_CONFIRMED_CLOSE}" (with "delivery <date> tak aa jayegi." before "dhanyavaad ji" only if Expected delivery gives a date)
   - after a mismatch: "theek hai ji, maine yeh badlaav note kar liya hai. dhanyavaad ji, aapka din shubh ho."
Ask for a yes at most twice in the call: once to the order (step 2), once to the address (step 3, or the read-back). Never again (owner, call 253).
Every other call ends with "dhanyavaad ji, aapka din shubh ho"; never with "namaste". Never end_call while a question of yours is unanswered."""

    # Call 102: "cancel kyun karna hai?" twice, by rote. Owner: handle it intelligently.
    cancellation = f"""CANCELLATION (they clearly want to cancel)
Be a helpful person, not a form: never push, never argue, never ask the same thing twice.
a. If they have not said why, ask once, naturally: "koi baat nahi ji. koi dikkat hai kya?" If they already said why, skip this.
b. If it is something that can be looked into (price, payment, delivery time, wrong item or quantity): "achha ji, maine yeh note kar liya hai. toh kya aap order rakhna chahenge?" A yes to keeping it now: put their issue in mismatch or notes and carry on with step 3, then the mismatch close. "Baad mein", "phir kar denge" is not a yes: c.
c. Otherwise, or if they still want to cancel: "theek hai ji, maine aapki cancellation request note kar li hai. dhanyavaad ji, aapka din shubh ho." mark_outcome "cancelled" with cancel_reason in their words ("not given" if they would not say), then end_call.
A "nahi" to cancelling, a worry, or a request (COD, a discount) is not a cancellation: deal with it and carry on. After a cancellation, never read the order again or ask for the address. Paid online and asking about the money: "ji, maine yeh note kar liya hai", no dates."""

    # Call 102: "quantity sahi hai, bas price 900 hai aur prepaid" needed one reply, not one turn per difference.
    mismatch = f"""MISMATCH (they say the product, quantity, amount or payment is different)
You cannot change the order on the call; you only note it, so it is checked before dispatch.
a. Say back, in one sentence, only what they said is different, in their words, and check: "achha, toh aapke hisaab se <what they said>, sahi?" Never add anything they did not say (call 282: "payment ho chuki hai", which nobody said). If you did not catch their version, ask for it; never suggest a number or a product yourself.
b. On their yes: "theek hai ji, maine yeh note kar liya hai." Then step 3 if the address is MISSING, then the mismatch close.
Never argue about who is right, never agree to change anything, never quote a new price or a refund. In mark_outcome: outcome "unclear", mismatch = each difference as "customer says ...; record says ...", new_product if they named another product."""

    rules = ['- You cannot cancel, refund or change anything yourself: you note it, "maine note kar liya hai", never "kar diya".']
    coupon = _whatsapp_coupon_block(prepaid_coupon_for(contact, facts or {}), contact, voice)
    if coupon:
        # The payment is asked about after the address (PAYMENT AND WHATSAPP COUPON), so step 2 leaves it out.
        flow = flow.replace("<amount> rupaye ka order kiya tha, <payment>. kya aapka order confirm hai?",
                            "<amount> rupaye ka order kiya tha. kya aapka order confirm hai?\" Never mention or ask about the "
                            "payment here (call 509 asked \"payment ho chuki hai?\"): it is asked after the address.")
        flow = re.sub(r"(?m)^   - all correct \(owner.*$", "   - all correct: PAYMENT AND WHATSAPP COUPON (below), in place of the usual close", flow)
        flow = flow.replace("Close in one turn, straight after their yes to the address (nothing more to ask), then mark_outcome, then end_call:",
                            "Straight after their yes to the address:")
        flow = flow.replace("Ask for a yes at most twice in the call:",
                            "Ask for a yes at most twice in the call (the payment question and the coupon offer aside):")

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="ORDER DETAILS",
                   details_noun="order details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[TAKING_AN_ADDRESS, *([coupon] if coupon else []), cancellation, mismatch], rules=rules,
                   not_mine='not the person named, or not their number',
                   topic="aapke order")


ORDER_CONFIRMATION = CallType(
    key="order_confirmation",
    name="Order confirmation",
    kind="service",
    outcomes=("confirmed", "changed", "cancelled", "not_ordered", "call_later", "wrong_number", "do_not_call", "unclear"),
    positive=("confirmed", "changed"),
    details={
        "mismatch": "What the customer claims versus what our record shows, raised for the team to verify. Example: 'customer says already paid online by UPI; record shows COD'.",
        "new_product": "Product and quantity the customer says they ordered, if different from our record. A claim to verify, not a change.",
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

    rules = ['- You cannot cancel, refund, reschedule or edit anything yourself: "maine note kar liya hai", never "kar diya". '
             'Unknowns: "maine aapka sawaal note kar liya hai"; put it in notes.',
             '- Never promise what a puja will bring: no "manokamna poori hogi", no "guaranteed", no health or money claims.']

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="BOOKING DETAILS",
                   details_noun="booking details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[cancellation, mismatch], rules=rules, not_mine='"I did not book anything"',
                   topic="aapki booking")


BOOKING_CONFIRMATION = CallType(
    key="booking_confirmation",
    name="Booking confirmation",
    kind="service",
    outcomes=("confirmed", "cancelled", "call_later", "wrong_number", "do_not_call", "unclear"),
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
{other}"""

    replies_first = [
        '- Interested ("haan", "achha hai", "batao", "book karna hai"): see INTERESTED.',
        '- Not interested ("nahi chahiye", "interest nahi hai", "abhi nahi"): see NOT INTERESTED.',
        '- Already booked ("maine book kar liya hai"): "bahut achha ji, dhanyavaad", goodbye, mark_outcome "already_booked", end_call.',
        '- They want help to book, or a payment link: "theek hai ji, maine yeh note kar liya hai." Goodbye, '
        'mark_outcome "needs_help" with their request in notes, end_call. Never promise a link, a WhatsApp or a time.',
        REPLY_WRONG,
    ]
    replies_last = ['- A bare "nahi" to "kya aap isme interested hain?" means not interested: see NOT INTERESTED. Anywhere else: "achha ji, kya?"']

    flow = f"""THE USUAL ORDER (short turns; add nothing)
1. Your first turn is the whole offer: greet, say who you are, give the campaign in one short sentence and ask, then STOP and wait: "namaste <name> ji, main {agent_name}, {BUSINESS_NAME} se. <the campaign>. kya aap isme interested hain?" Never ask whether they have a minute or how they are. The campaign sentence is about fifteen words, in your own words, from CAMPAIGN DETAILS: what it is, where, the date, and the price if one is given. For example: "<date> ko <place> mein <campaign> hai, <price> rupaye se." Never read "What it is" out and never list the details.
2. Their answer decides the rest: INTERESTED, NOT INTERESTED, already booked, or a question (QUESTIONS). If they did not catch it or ask who is calling, say it again, shorter, and ask again.
{GOODBYE}
Never end_call with a question unanswered."""

    interested = f"""INTERESTED
One short turn: "bahut achha ji. aap {how} par book kar sakte hain", adding "<date> se pehle" only if a date is given. Then goodbye, mark_outcome "interested", end_call. You cannot book for them, take payment or send anything yourself."""

    # Calls 73 and 74: "mere paas time nahi hai" was accepted and the call closed, though the chadhava
    # is done for them and needs only a name and gotra. A reason born of not knowing that is cleared up once.
    not_interested = f"""NOT INTERESTED
Never push and never argue. First ask the reason, ONCE and gently: "koi baat nahi ji. agar bura na maanein toh, koi khaas wajah?" A bare no ("nahi", "interested nahi hoon", "nahi chahiye") is not a reason: always ask. Skip the question only when their no already carried a reason ("nahi, paise nahi hain"). Then:
- A reason that comes from not knowing how it works, when CAMPAIGN DETAILS answer it (no time that day, cannot come to the temple, lives far away, how will they know it was done): clear it up ONCE, in one or two short sentences from CAMPAIGN DETAILS, then ask once more: "toh kya aap karwana chahenge?" For example, to "time nahi hai" when the details say the pandit does it in their name: "ji, aapko us din kuch karna nahi hai. pandit ji aapke naam aur gotra se puja karenge. toh kya aap karwana chahenge?" A yes: INTERESTED.
- Any other reason (does not want it, money, does not believe in it, already booked elsewhere), or a no after you cleared it up: accept it: "achha ji, samajh {forms(voice)["gayi"]}. dhanyavaad ji, aapka din shubh ho." mark_outcome "not_interested" with not_interested_reason in their words ("not given" if they would not say), then end_call.
Clear up at most one misunderstanding in a call. No offer, no discount, no pressure."""

    questions = """QUESTIONS
Answer only from CAMPAIGN DETAILS, in one short sentence, then ask again "kya aap isme interested hain?" If the answer is not there: "iski poori jaankari website ya app par mil jayegi", and put the question in the question field of mark_outcome. Never guess a price, date, place, benefit or offer."""

    sections = [interested, not_interested, questions, """CAMPAIGN RULES (these always hold)
- Never promise what a puja, chadhava or product will bring: no "manokamna poori hogi", no "guaranteed", no health, money or family claims.
- No pressure and no false urgency: no "sirf aaj", "last chance", "jaldi kijiye".
- No discount, offer, gift or free item unless it is written in CAMPAIGN DETAILS.
- If the date has already passed, do not invite them to it: apologise, say goodbye, mark_outcome "unclear" with notes "campaign date passed"."""]
    if fx("never_say"):
        sections.append("NEVER SAY (set by the person who created this campaign)\n" + fx("never_say"))

    rules = ['- You cannot book, take payment, send links or change anything yourself: "maine note kar liya hai" is the most you say. '
             'Unknowns go in the question field.']

    return compose(contact=contact, voice=voice, intro=intro, details=details,
                   details_title="CAMPAIGN DETAILS or CUSTOMER DETAILS", details_noun="campaign and customer details",
                   replies_first=replies_first, replies_last=replies_last, flow=flow, sections=sections, rules=rules,
                   not_mine='"main aapka customer nahi hoon"', topic=topic)


CAMPAIGN_INVITE = CallType(
    key="campaign_invite",
    name="Campaign invite",
    kind="promotional",
    outcomes=("interested", "not_interested", "already_booked", "needs_help", "call_later", "wrong_number",
              "do_not_call", "unclear"),
    positive=("interested",),
    details={
        "not_interested_reason": "Why they are not interested, in their words; 'not given' if they would not say.",
        "question": "A question they asked that the campaign details could not answer.",
        **COMMON_DETAILS,
    },
    build_prompt=_invite_prompt,
    confirm_question="kya aap isme interested hain?",
    main_question=r"(interested|इंटरेस्टेड)\s*(hain|hai|हैं|है)",
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
        Fact("place", "Temple or place", hint="Kamakhya Mandir, Guwahati"),
        Fact("price", "Price", hint="551 rupaye"),
        # Stored as start_date since the first campaign, but it is the day the agent names and after which it
        # stops inviting. Labelled "Campaign start date", it was read as the day calling starts (25 Sep, for
        # a 3 Oct puja), which would have ended every call from the next day on as "date passed".
        Fact("start_date", "Date of the puja or event", True, date=True),
        Fact("how_to_book", "How to book", True, hint="Vedic Vaibhav ki website ya app"),
        Fact("more_info", "How it works, and answers to likely questions (optional)", long=True,
             hint="Aapko aana nahi hai: pandit ji aapke naam aur gotra se puja karenge. Puja ka video WhatsApp par aayega. "
                  "Prasad 10 din mein ghar pahunchega."),
        Fact("never_say", "Never say (optional)", long=True, hint="Discount ki baat mat karna."),
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
        from .coupons import offer_for   # here, not at the top: coupons reads the call types
        return offer_for(contact)
    except Exception:  # noqa: BLE001
        log.exception("coupon lookup failed for contact %s; no offer on this call", contact.get("id"))
        return None


def cart_help_line(voice: str) -> str:
    f = forms(voice)
    return f"ji, samajh {f['gayi']}. product ya payment ko lekar koi doubt hai toh main help kar {f['sakti']} hoon."


def cart_link_line(voice: str) -> str:
    return f"ji. agar aap order complete karna chahein, main payment link WhatsApp par share kar {forms(voice)['deti']} hoon."


def cart_link_close(voice: str) -> str:
    """Their yes to the link: it goes on WhatsApp and the call ends."""
    return (f"ji theek hai, main link WhatsApp par bhej {forms(voice)['rahi']} hoon. aap wahin se order complete kar sakte "
            "hain. dhanyavaad ji.")


def cart_price_close(voice: str) -> str:
    """"Price zyada hai": the offer (the campaign's coupon, when it has one) and the link go on WhatsApp; the call ends."""
    return (f"ji, abhi agar koi applicable offer hua toh main WhatsApp par share kar {forms(voice)['deti']} hoon. "
            "aap check kar lijiyega. dhanyavaad ji.")


CART_NOT_NOW = f"ji koi baat nahi. future mein zarurat ho toh {STORE_NAME} par kabhi bhi order kar sakte hain. dhanyavaad ji."


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
    coupon = call_coupon(contact)

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian company selling devotional products. The customer "
             f"looked at a product on {STORE_NAME} but did not finish the order. Find out what stopped them, help, and if they "
             "want it, the payment link goes to them on WhatsApp. Very short turns, never a sales push.")

    details = f"""CART DETAILS
- Customer name: {contact.get('customer_name') or 'MISSING'}
- In the cart: {contact.get('product') or 'not on record'}
- Cart value: {amount_line(contact, 'not on record (never name a price)')}
- Notes: {contact.get('notes') or 'none'}
- Answers to common questions: {answers or 'none'}
{_offer_detail(coupon) if coupon else ""}{other}{_offers_block(facts or {})}"""

    replies_first = [
        f'- "Nahi chahiye", "abhi nahi", "baad mein", bought it elsewhere, or any no: "{CART_NOT_NOW}" mark_outcome '
        '"not_interested" with not_interested_reason in their words ("not given" if none), end_call. No question, no push.',
        '- "Price zyada hai", "mehenga hai", or they ask for a discount: PRICE.',
        '- Already ordered ("order kar diya", "le liya"): "achha ji, bahut badhiya. dhanyavaad ji." mark_outcome '
        '"already_ordered", end_call.',
        '- A question about the product (price, size, what it is, delivery): answer it in one short sentence from CART '
        f'DETAILS or product_info, then "{cart_link_line(voice)}"',
        f'- "{BUSINESS_NAME} se dekha tha" or "{STORE_NAME} kya hai?": "ji, {STORE_NAME} {BUSINESS_NAME} ka hi online store '
        'hai." Then go on.',
        REPLY_WRONG,
    ]
    replies_last = ['- A sound you could not make out, or "repeat kar dijiye": say your last line again, shorter. Never take '
                    'it as a no.']

    # Owner, 22 Oct 2026: these lines, word for word, and nothing more.
    flow = f"""THE USUAL ORDER
1. The greeting has been said for you ("...aapne hamari website par ek product dekha tha, lekin order complete nahi hua. bas check karna tha, koi issue aa raha tha kya?"). Wait for their answer.
2. Their answer is a yes, "payment issue tha", "bas dekh raha tha", or anything that is not a no or PRICE: "{cart_help_line(voice)}" Then wait.
3. What they say next, a payment problem, a doubt, or that they want it: "{cart_link_line(voice)}" Then wait. (A no or PRICE: as said above.)
4. Their yes to the link ("haan", "bhej do", "theek hai"): "{cart_link_close(voice)}" mark_outcome "wants_to_order", end_call. A no: "{CART_NOT_NOW}" mark_outcome "not_interested", end_call.
Never take an order, an address or a payment on the call, and never read a link or a code out: they go on WhatsApp by themselves.
End the call only with one of the closing lines above, word for word; never with "namaste". Never end_call while a question of yours is unanswered."""

    price = f"""PRICE
"Price zyada hai", "mehenga hai", or a discount asked for, at any point: "{cart_price_close(voice)}" mark_outcome "wants_coupon" with not_interested_reason in their words, end_call. Never name a discount or a code yourself."""

    rules = ['- You cannot place the order or take payment yourself: the link (and any offer) goes on WhatsApp by itself '
             'after the lines above. Promise nothing else: "maine note kar liya hai" is the most you say about anything else.']

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="CART DETAILS",
                   details_noun="cart details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[price], rules=rules,
                   not_mine='not the person named, or "maine kuch cart mein nahi dala"', topic="aapke order")


def _cart_greeting(contact: dict, voice: str) -> str:
    """The owner's first line, word for word, in Devanagari (owner, 22 Oct 2026): who calls, why, and the question."""
    name = str(contact.get("customer_name") or "").strip()
    agent_name, _ = persona(voice)
    rahi = "रही" if forms(voice)["rahi"] == "rahi" else "रहा"
    return (f"नमस्ते {name + ' ' if name else ''}जी, मैं {PRONUNCIATION.get(agent_name, agent_name)}, "
            f"{PRONUNCIATION.get(STORE_NAME, STORE_NAME)} से बोल {rahi} हूँ। आपने हमारी website पर एक product देखा था, "
            "लेकिन order complete नहीं हुआ। बस check करना था, कोई issue आ रहा था क्या?")


ABANDONED_CART = CallType(
    key="abandoned_cart",
    name="Abandoned cart",
    kind="promotional",
    outcomes=("wants_to_order", "wants_coupon", "not_interested", "already_ordered", "needs_help", "call_later",
              "wrong_number", "do_not_call", "unclear"),
    positive=("wants_to_order",),   # a yes to the WhatsApp link
    details={
        "not_interested_reason": "Why they do not want it, or what stopped them, in their words; 'not given' if they would not say.",
        "question": "A problem or question only the team can handle (payment failed, website trouble, something you could not answer).",
        **COMMON_DETAILS,
    },
    build_prompt=_cart_prompt,
    greeting=_cart_greeting,
    offers_coupon=True,
    confirm_question="kya main payment link WhatsApp par share kar doon?",
    # The link line (owner, 22 Oct 2026): "...main payment link WhatsApp par share kar deti hoon."
    main_question=r"payment\s*link|पेमेंट\s*लिंक|link\s+whats\s*app|लिंक",
    summary_keys=("not_interested_reason", "question", "callback_time", "notes"),
    description="Call customers who left items in their cart: find out what stopped them, help, and send the payment link "
                "(and the campaign's WhatsApp coupon) on WhatsApp to those who want it.",
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
        '- They refuse or are not interested: accept it at once, never push; ask the reason once only if the reason for '
        'the call asks you to. Close with mark_outcome "not_interested".',
        '- They want something you cannot do (a refund, a link, a person to call them): "theek hai ji, maine yeh note kar liya hai." '
        'Close with mark_outcome "needs_help" and what they want in notes. Never promise a time.',
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
    outcomes=("done", "not_interested", "needs_help", "call_later", "wrong_number", "do_not_call", "unclear"),
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
    return [f.label for f in ct.facts if f.required and not str(facts.get(f.key) or "").strip()] if ct else []


def call_type_for(contact: dict | None) -> CallType:
    """The call type a contact row names; order confirmation when it names none or an unknown one."""
    key = str((contact or {}).get("campaign") or "").strip()
    return ALL_TYPES.get(key, DEFAULT)


def get_prompt(contact: dict, voice: str) -> str:
    """The system prompt for one call: the contact's call type, with its campaign's facts."""
    from . import db   # here, not at the top: db is not needed to define the types
    from .catalog import product_block
    campaign = db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None
    return call_type_for(contact).build_prompt(contact, voice, (campaign or {}).get("facts") or {}) + product_block(contact, voice)
