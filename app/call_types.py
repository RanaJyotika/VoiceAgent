"""Call types: what a call is for, and everything that follows from it.

A call type supplies its own script parts (the shared layer in prompts.py goes around them), the
results the agent may record, which of those results need a clear yes from the customer, the extra
details it writes down, and whether it takes addresses. The contact row names its call type in
the `campaign` column; a row without one is an order confirmation, as every row was before.

Only order confirmation exists so far. Booking confirmation and campaign invites come next, and
then types written from a brief on the dashboard; they all fill the same fields.
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from .config import BUSINESS_NAME, PRONUNCIATION, STORE_NAME
from .prompts import (GOODBYE, REPLY_WRONG, TAKING_AN_ADDRESS, amount_line, compose, delivery_line, forms, missing_block,
                      persona)

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


# --- order confirmation ------------------------------------------------------------------------

def _order_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    deti = forms(voice)["deti"]
    payment = (contact.get("payment_mode") or "").upper()
    payment_line = "cash on delivery" if payment == "COD" else "already paid online" if payment else "unknown"
    answers = str((facts or {}).get("answers") or "").strip()

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian company selling devotional products, to confirm "
             f"an order the customer placed on {STORE_NAME}, {BUSINESS_NAME}'s online store. Warm and short: about a minute.")

    details = f"""ORDER DETAILS
- Order ID: {contact.get('order_id') or 'not available'}
- Customer name: {contact.get('customer_name') or 'MISSING (greet with "hello ji")'}
- Product: {contact.get('product') or 'MISSING'}
- Quantity: {contact.get('quantity') or '1'}
- Amount: {amount_line(contact, 'MISSING')}
- Payment: {payment_line}
- Delivery address: {contact.get('address') or 'MISSING'}
- Expected delivery: {delivery_line(contact.get('delivery_date'))}
- Notes: {contact.get('notes') or 'none'}
- Answers to common questions: {answers or 'none'}
{missing_block(contact)}"""

    replies_first = [
        '- Yes ("haan", "ji", "sahi hai"): next step.',
        '- Something in the order is different (product, quantity, amount, payment): MISMATCH. A wrong address is not a mismatch: take it again at step 3.',
        '- They want to cancel: CANCELLATION.',
        # Calls 93, 96 and 98 handled "I did not order" three different ways.
        f'- They did not place this order: never offer to cancel. Ask once "kya yeh <name> ji ka number hai?" Not theirs: wrong number. '
        f'Theirs: "koi baat nahi ji, main team ko bata {deti} hoon, team check kar legi. dhanyavaad ji, aapka din shubh ho", '
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
1. Greet, then wait: "namaste <name> ji, main {agent_name}, {BUSINESS_NAME} se. bas aapke order ka ek chhota sa confirmation call hai, ek minute baat ho sakti hai?"
2. After their yes, the order in one sentence, always ending in "kya aapka order confirm hai?": "ji, aapne {STORE_NAME} se <product>, <quantity> piece, <amount> rupaye ka order kiya tha, <payment>. kya aapka order confirm hai?"
3. Address, every time (call 122 skipped it). On record: ask it as its own question, "aur delivery <area>, <city> mein hi karni hai na?", and get a yes. MISSING, or they say it is wrong: "ji, delivery address ek baar poora bata dijiye" (TAKING AN ADDRESS). This comes after a mismatch too; skip it only after a cancellation.
4. Close in one turn, then mark_outcome, then end_call:
   - all correct: "ji, order confirm ho gaya" (+ "delivery <date> tak aa jayegi" if Expected delivery gives a date) + "dhanyavaad ji, aapka din shubh ho."
   - after a mismatch: "ji, team verify karke order aage badhayegi. dhanyavaad ji, aapka din shubh ho."
{GOODBYE} Never end_call while a question of yours is unanswered."""

    # Call 102: "cancel kyun karna hai?" twice, by rote. Owner: handle it intelligently.
    cancellation = f"""CANCELLATION (they clearly want to cancel)
Be a helpful person, not a form: never push, never argue, never ask the same thing twice.
a. If they have not said why, ask once, naturally: "koi baat nahi ji. koi dikkat hai kya?" If they already said why, skip this.
b. If it is something the team can look into (price, payment, delivery time, wrong item or quantity): "achha, main yeh team ko bata {deti} hoon, woh check kar lenge. toh kya aap order rakhna chahenge?" A yes: put their issue in mismatch or notes and carry on with step 3, then the mismatch close.
c. Otherwise, or if they still want to cancel: "theek hai ji, maine cancellation request raise kar di hai, team process kar degi. dhanyavaad ji, aapka din shubh ho." mark_outcome "cancelled" with cancel_reason in their words ("not given" if they would not say), then end_call.
A "nahi" to cancelling, a worry, or a request (COD, a discount) is not a cancellation: deal with it and carry on. After a cancellation, never read the order again or ask for the address. Paid online and asking about the money: "refund team process kar degi", no dates."""

    # Call 102: "quantity sahi hai, bas price 900 hai aur prepaid" needed one reply, not one turn per difference.
    mismatch = f"""MISMATCH (they say the product, quantity, amount or payment is different)
You cannot change the order on the call; you raise it so the team checks it before dispatch.
a. Say back, in one sentence, only what they said is different, and check: "achha, toh aapke hisaab se amount nau sau rupaye hai aur payment ho chuki hai, sahi?" Leave out what they said is correct. If you did not catch their version, ask for it; never suggest a number or a product yourself.
b. On their yes: "theek hai ji, main yeh team ko verify ke liye bhej {deti} hoon." Then step 3 if the address is MISSING, then the mismatch close.
Never argue about who is right, never agree to change anything, never quote a new price or a refund. In mark_outcome: outcome "unclear", mismatch = each difference as "customer says ...; record says ...", new_product if they named another product."""

    rules = ['- You cannot cancel, refund or change anything yourself: you raise it, "team process kar degi".']

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="ORDER DETAILS",
                   details_noun="order details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[TAKING_AN_ADDRESS, cancellation, mismatch], rules=rules,
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
        "new_address": "New delivery address the customer confirmed after you read it back, with house number and 6-digit PIN code.",
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
        Column("customer_name", "Customer name", True, "Ramesh Kumar"),
        Column("phone", "Phone", True, "PUT 10 DIGIT NUMBER"),
        Column("order_id", "Order ID", False, "VV-1001"),
        Column("product", "Product", False, "Rudraksha Mala 108 beads"),
        Column("quantity", "Quantity", False, "1"),
        Column("amount", "Amount", False, "1499"),
        Column("payment_mode", "Payment (COD / Prepaid)", False, "COD"),
        Column("address", "Address", False, "12 MG Road, Indore, MP 452001"),
        Column("delivery_date", "Delivery date", False, "30 September 2026"),
        Column("notes", "Notes", False, ""),
    ),
    facts=(
        # Without these the agent invented them (call 99: "certificate bhi saath aayega").
        Fact("answers", "Answers to common customer questions (optional)", long=True,
             hint="Sabhi products original hain. Rudraksha ke saath lab certificate aata hai. Delivery ke baad 7 din mein return."),
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
        return "not on record (do not name a date; if asked: 'team confirm kar degi')"
    day = _parse_date(raw)
    if day and day < date.today():
        return f"{raw} (THIS DATE HAS ALREADY PASSED; do not say it, and note it for the team)"
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
    sakti, deti = f["sakti"], f["deti"]
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
  c. Only if the reason is the date, the price or a mistake in the booking, offer ONE time: "ji samajh {sakti} hoon. main yeh baat team tak pahuncha {deti} hoon; kya aap booking rakhna chahenge?" For any other reason skip this and accept.
  d. Accept: "ji, maine aapki cancellation request raise kar di hai, team process kar degi. dhanyavaad ji, aapka din shubh ho." Then mark_outcome "cancelled" with cancel_reason in their words, then end_call.
If at c they decide to keep the booking, go back to confirming it.
Once they want to cancel: never ask "confirm kar doon?" and never read the booking back again. If it was paid and they ask about the money: "refund team process kar degi"; never a date, a method or a number of days."""

    mismatch = """MISMATCH (they say what was booked, the temple, the date, the names, the amount or the payment is different)
BOOKING DETAILS are the only truth; you cannot change them on the call, only raise the issue so the team verifies it. Warm, on their side, never arguing. One short turn each:
  a. Say what the record shows, once, only the part they dispute: "ji, mere record mein <that part> hai."
  b. Get their version in their words and say it back: "achha, aapne <their words> book kiya tha, sahi?" Never offer a name, a date or a number yourself.
  c. On their yes: "ji, maine yeh issue raise kar diya hai, team verify kar legi. baaki sab sahi hai?" Ask "baaki sab sahi hai?" at most twice in a call.
  d. Close: "ji, team verify karke booking aage badhayegi. dhanyavaad ji, aapka din shubh ho." Do not say "booking confirm ho gayi" after a mismatch.
Never agree to change anything; never invent an offer, discount, date or policy.
In mark_outcome: mismatch = their claim vs the record, one line per difference, outcome "unclear" so the team reviews (or "cancelled" if they cancelled)."""

    rules = ['- You cannot cancel, refund, reschedule or edit anything yourself: "note kar liya, team process kar degi", never "kar diya". '
             'Unknowns: "team check kar legi"; put it in notes.',
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
    deti = forms(voice)["deti"]
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
        f'- They want help to book, or a payment link: "ji, main team ko bata {deti} hoon, team aapki madad kar degi." Goodbye, '
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

    rules = [f'- You cannot book, take payment, send links or change anything yourself: "main team ko bata {deti} hoon, team madad kar degi". '
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

def _cart_prompt(contact: dict, voice: str, facts: dict | None = None) -> str:
    agent_name, _ = persona(voice)
    f = forms(voice)
    deti = f["deti"]
    answers = str((facts or {}).get("answers") or "").strip()
    other = _block("Other details from the sheet:", _other_details(contact, set()))
    missing = "" if (contact.get("address") or "").strip() else \
        "\nMISSING (ask; never guess)\n- Address: none on record, so never say one; take it at step 4.\n"

    intro = (f"You are {agent_name}, calling for {BUSINESS_NAME}, an Indian company selling devotional products. The customer "
             f"put items in their cart on {STORE_NAME} but did not finish the order. You remind them, help if something stopped "
             "them, and if they want it, take the order for the team to place. Friendly and short, never a sales push.")

    details = f"""CART DETAILS
- Customer name: {contact.get('customer_name') or 'MISSING (greet with "hello ji")'}
- In the cart: {contact.get('product') or 'MISSING (ask "aap kya order karna chahte the ji?")'}
- Quantity: {contact.get('quantity') or '1'}
- Cart value: {amount_line(contact, 'not on record (never name a price)')}
- Address on record: {contact.get('address') or 'MISSING'}
- Notes: {contact.get('notes') or 'none'}
- Answers to common questions: {answers or 'none'}
{other}{missing}"""

    # Calls 133 and 134 closed on the customer's first sentence ("Vedic Vaibhav se order kiya tha", "kisi aur site se
    # kiya tha"). Owner, 5 Oct 2026: stay with the customer and their questions until there is a clear yes or no.
    replies_first = [
        '- Wants it ("haan", "order karna hai", "bhej do"), or asks how to pay: step 3.',
        # Call 140: two "repeat kar dijiye" / "kya order tha?" got the item name and a goodbye.
        '- "Repeat kar dijiye", "kya order tha?", or a sound you could not make out: say the item and price again in one '
        'short sentence, then "kya yeh order aap rakhna chahte hain?" Never take it as a no.',
        f'- "{BUSINESS_NAME} se order kiya tha" or "{STORE_NAME} kya hai?": "ji, {STORE_NAME} {BUSINESS_NAME} ka hi part hai, '
        'unka online store." Then ALREADY ORDERED.',
        '- Already ordered ("maine order kar diya"): ALREADY ORDERED.',
        # Call 182: "baad mein kharidungi, abhi nahi" was recorded as call_later.
        '- No, not now, "baad mein le lunga", bought it elsewhere, or any excuse: NOT INTERESTED, never call_later.',
        '- Something stopped them (payment failed, website problem, delivery charge, a question): answer from CART DETAILS if you can, '
        'put the problem in question, then "toh kya yeh order aap rakhna chahte hain?" A yes: step 3. If only the team can solve it: '
        f'"main team ko bata {deti} hoon, team aapki madad kar degi", goodbye, mark_outcome "needs_help", end_call. '
        # Call 167: a freebie question after a clear yes ended the call as needs_help, with no order taken.
        'After their yes, never needs_help: "yeh team confirm kar degi", put it in question, and carry on with the order.',
        REPLY_WRONG,
    ]
    replies_last = ['- A bare "nahi" to "kya yeh order aap rakhna chahte hain?": NOT INTERESTED.',
                    '- Until you have a clear yes or a clear no to "kya yeh order aap rakhna chahte hain?", never say goodbye: answer '
                    'what they said, then ask it again. After a clear yes, never ask it again (call 167 asked it seven times).']

    # Owner, 6 Oct 2026: the opening checks it is the right person first. Calls 155-158 gave the whole offer in one
    # breath and three of four times the customer asked "aap kahan se bol rahe hain?".
    # Owner, 7 Oct 2026 (call 173): steps 2-5 in the owner's words; online payment is paid on the site, no address.
    # Owner, 7 Oct 2026 (call 167): say where you call from in the greeting, and the name only once, in the question.
    flow = f"""THE USUAL ORDER
1. Greet, say where you call from and check you have the right person, nothing else, then wait: "namaste ji, main {BUSINESS_NAME} se baat kar {f["rahi"]} hoon. kya meri baat <name> ji se ho rahi hai?" The name only there, not after "namaste". (Name MISSING: start at step 2 with "namaste ji, main {BUSINESS_NAME} se baat kar {f["rahi"]} hoon.")
2. After "haan" / "ji boliye" / "bolo": say exactly this, with nothing before it, then wait: "ji, aapne cart mein ek <items> add kiya tha, <quantity> quantity mein, <amount> rupaye mein. kya yeh order aap rakhna chahte hain?" Leave out the amount if it is not on record. If they ask who is calling: "ji, main {agent_name}, {BUSINESS_NAME} se", then this turn.
3. On a yes: "theek hai ji. payment cash on delivery karenge ya online?" A change they ask for (another item, a different quantity) goes in order_changes; say it back and get a yes.
   - Online: "theek hai ji, aap site par jaakar hi online pay kar dijiye. dhanyavaad ji, aapka din shubh ho." Then mark_outcome "wants_to_order" with payment_preference "online", then end_call.
   - Cash on delivery: step 4.
4. Address. On record: "theek hai ji. delivery <area>, <city> mein hi karni hai na?" and get a yes. MISSING, or they say it changed: "theek hai ji. delivery address ek baar poora bata dijiye" (TAKING AN ADDRESS).
5. Close in one turn, with exactly these words and nothing added: "dhanyavaad ji, aapka din shubh ho." Then mark_outcome "wants_to_order" with payment_preference "cash on delivery", then end_call.
{GOODBYE} Never end_call while a question of yours is unanswered."""

    already_ordered = f"""ALREADY ORDERED
Check it was this cart: "achha ji, kya aapne yahi <items> order kar liya hai?" (never "order confirm kar liya?": it sounds like "shall I confirm it?"). A clear yes: "bahut achha ji. dhanyavaad ji, aapka din shubh ho", mark_outcome "already_ordered", end_call. They ordered something else, or earlier: "ji, toh kya aap <items> bhi order karna chahenge?" and go by their answer. Anything else (a question about price or charges, a "nahi"): answer it, then "toh kya yeh order aap rakhna chahte hain?" Never go to step 3 or 4 before a yes to that."""

    not_interested = f"""NOT INTERESTED
Never push and never argue, but always ask once what stopped them, even when they gave a reason. Say exactly this, nothing before or after it: "achha ji. koi dikkat aayi thi kya, hamari site par ya price mein?" ("kisi aur site se le liya": "achha ji, wahan se kyun liya, koi khaas wajah?") Then stop and wait for their answer: no goodbye in the same turn.
Whatever they answer is their second no (owner, call 183): handle it in ONE turn that ends with the goodbye. No new question of any kind, never "kya yeh order aap rakhna chahte hain?" again, no lower price or change, and no line about what you are doing ("main aapke jawab ko samajh rahi hoon"). First the one sentence their answer calls for, then "dhanyavaad ji, aapka din shubh ho":
- A doubt or complaint ("fake product bhejte ho", "nakli hai", "site fraud hai", a bad earlier order): the complaint reply from LISTEN FIRST, in those words, then the goodbye. The complaint is their reason.
- A question (price, delivery, charges, quality): the answer in one sentence from CART DETAILS, or "iski jaankari team aapko de degi", then the goodbye.
- A reason or an excuse (price, bought elsewhere, no need, later, nothing to add): "theek hai ji, dhanyavaad. aapka din shubh ho."
- Only if they say on their own that they want it after all: step 3.
mark_outcome "not_interested" with not_interested_reason in exactly their words, never your own reading of them ("not given" if they would not say), then end_call."""

    rules = ['- You cannot place the order, take payment, send a link or give a discount yourself: "team place kar degi" is the most you say.']

    return compose(contact=contact, voice=voice, intro=intro, details=details, details_title="CART DETAILS",
                   details_noun="cart details", replies_first=replies_first, replies_last=replies_last, flow=flow,
                   sections=[TAKING_AN_ADDRESS, already_ordered, not_interested], rules=rules,
                   not_mine='not the person named, or "maine kuch cart mein nahi dala"', topic="aapke order")


def _cart_greeting(contact: dict, voice: str) -> str:
    """Step 1 word for word, in Devanagari. Call 177: left to itself, ChatGPT opened with "ठीक है, मैं सबसे पहले
    अभिवादन के साथ शुरू करता हूँ..." before the greeting."""
    name = str(contact.get("customer_name") or "").strip()
    if not name:
        return ""   # step 1 then runs straight into step 2: left to the agent
    rahi = "रही" if forms(voice)["rahi"] == "rahi" else "रहा"
    return (f"नमस्ते जी, मैं {PRONUNCIATION.get(BUSINESS_NAME, BUSINESS_NAME)} से बात कर {rahi} हूँ। "
            f"क्या मेरी बात {name} जी से हो रही है?")


ABANDONED_CART = CallType(
    key="abandoned_cart",
    name="Abandoned cart",
    kind="promotional",
    outcomes=("wants_to_order", "not_interested", "already_ordered", "needs_help", "call_later", "wrong_number",
              "do_not_call", "unclear"),
    positive=("wants_to_order",),
    details={
        "payment_preference": "Cash on delivery or online, as the customer said, when they want to order.",
        "order_changes": "Changes to the cart the customer asked for (an item added or removed, another quantity), for the team.",
        "new_address": "Delivery address the customer gave or corrected on this call, after you read it back and they said yes.",
        "not_interested_reason": "Why they do not want it, in their words; 'not given' if they would not say.",
        "question": "A problem or question only the team can handle (payment failed, website trouble, something you could not answer).",
        **COMMON_DETAILS,
    },
    build_prompt=_cart_prompt,
    greeting=_cart_greeting,
    takes_address=True,
    confirm_question="kya yeh order aap rakhna chahte hain?",
    # Also "chahengi" to a woman (Gemini, call 160) and "karwana / kara chahenge" (ChatGPT, call 159).
    # And "karna chahte hain" (ChatGPT, call 168).
    main_question=r"(order|ऑर्डर)\s+(\S+\s+){0,3}(chahenge|chahengi|chahte|चाहेंगे|चाहेंगी|चाहते)",
    probe_first=("not_interested", "already_ordered"),
    summary_keys=("not_interested_reason", "question", "order_changes", "callback_time", "notes"),
    description="Call customers who left items in their cart: remind them, help, and take the order and address for the team.",
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
    ),
)


CALL_TYPES: dict[str, CallType] = {t.key: t for t in (ORDER_CONFIRMATION, BOOKING_CONFIRMATION, CAMPAIGN_INVITE, ABANDONED_CART)}
DEFAULT = ORDER_CONFIRMATION


def call_type_for(contact: dict | None) -> CallType:
    """The call type a contact row names; order confirmation when it names none or an unknown one."""
    key = str((contact or {}).get("campaign") or "").strip()
    return CALL_TYPES.get(key, DEFAULT)


def get_prompt(contact: dict, voice: str) -> str:
    """The system prompt for one call: the contact's call type, with its campaign's facts."""
    from . import db   # here, not at the top: db is not needed to define the types
    campaign = db.get_campaign(contact["campaign_id"]) if contact.get("campaign_id") else None
    return call_type_for(contact).build_prompt(contact, voice, (campaign or {}).get("facts") or {})
