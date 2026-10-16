"""Product catalogue: what the agent may say about a product, read from the Vedic Vaibhav MongoDB.

It reaches the agent in two ways:
- At the start of a call, the products in the customer's order or cart (matched by name) go into the
  prompt as PRODUCT INFO, so a question about them is answered with no wait at all.
- During a call, a question about any other product makes the agent call product_info; the search
  runs here and the closest few products go back to it.

The whole catalogue is held in memory and read again every CATALOG_REFRESH_MINUTES, so a lookup during
a call never waits on the database, and a database that is down only means a slightly older copy.
Nothing here writes to MongoDB; give it a read-only user.

The documents are Shopify products as the main site keeps them. Each field is looked for at the top
level and in rawShopifyData (Shopify's own shape), as lists or as GraphQL edges, because the exact
layout of the main site's product model is not fixed here.
"""
import asyncio
import html
import logging
import re

from fastapi import APIRouter

from .config import (CATALOG_REFRESH_MINUTES, MONGODB_COLLECTIONS, MONGODB_DB, MONGODB_ORDERS, MONGODB_PRODUCTS,
                     MONGODB_SHOP, MONGODB_URI, PRONUNCIATION)
from .db import now_iso
from .prompts import forms

log = logging.getLogger("catalog")
router = APIRouter()

STATE: dict = {"products": [], "by_title": {}, "by_id": {}, "by_sku": {}, "loaded_at": "", "source": "", "error": ""}
# Product metafields that are for the team or for Google, not for a customer: who listed it (built_by),
# shopping-feed and SEO fields, review counters. Any key containing one of these is left out.
INTERNAL_FIELDS = ("built_by", "internal", "supplier", "cost", "vendor_code", "google", "review", "rating", "seo",
                   "title_tag", "description_tag", "package", "sku", "barcode", "hsn", "weight", "age_group", "condition",
                   "mpn", "custom_label")
MAX_IN_PROMPT = 3          # products of one order or cart put into the prompt
ABOUT_CHARS = 350          # of a product's description, in the prompt and in a tool answer


def enabled() -> bool:
    return bool(MONGODB_URI)


# --- reading a product document ---------------------------------------------------------------------

def _nodes(value) -> list:
    """A list, Shopify GraphQL edges ({"edges": [{"node": ...}]}) or nodes ({"nodes": [...]}), as a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("edges"), list):
            return [e.get("node", e) if isinstance(e, dict) else e for e in value["edges"]]
        if isinstance(value.get("nodes"), list):
            return value["nodes"]
    return []


def _text(html_text) -> str:
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", ". ", str(html_text or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s*\.\s*(\.\s*)+", ". ", text)
    return re.sub(r"\s+", " ", text).strip(" .")


def _money(value) -> str:
    if isinstance(value, dict):
        value = value.get("amount")
    text = str(value if value is not None else "").strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    return text


def _short(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("। "))
    return (cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "…").strip()


def parse(doc: dict) -> dict | None:
    """One product document as the few things the agent needs; None for a deleted or nameless one. Drafts and
    unlisted products are kept (an order may be for one) but marked, and a search never offers them."""
    if not isinstance(doc, dict) or doc.get("isDeleted") or doc.get("deletedAt"):
        return None
    raw = doc.get("rawShopifyData") if isinstance(doc.get("rawShopifyData"), dict) else {}

    def get(*keys):
        for k in keys:
            for src in (doc, raw):
                v = src.get(k)
                if v not in (None, "", [], {}):
                    return v
        return None

    title = str(get("title") or "").strip()
    status = str(get("status") or "").upper()
    if not title:
        return None
    gid = str(get("adminGraphqlApiId", "admin_graphql_api_id", "shopifyProductId") or "")
    if not gid.startswith("gid://shopify/Product/"):
        gid = next((str(x) for x in (raw.get("id"), doc.get("id")) if str(x or "").startswith("gid://shopify/Product/")), "")
    variants, skus = [], []
    for v in _nodes(doc.get("variants")) or _nodes(raw.get("variants")):
        if not isinstance(v, dict):
            continue
        if str(v.get("sku") or "").strip():
            skus.append(str(v["sku"]).strip().lower())
        available = v.get("availableForSale", v.get("available"))
        if available is None:
            qty = v.get("inventoryQuantity", v.get("inventory_quantity"))
            available = None if qty is None else qty > 0
        variants.append({"title": str(v.get("title") or "").strip(), "price": _money(v.get("price")),
                         "compare_at": _money(v.get("compareAtPrice") or v.get("compare_at_price")), "available": available})
    if not variants:
        low = (get("priceRangeV2", "priceRange") or {}).get("minVariantPrice")
        if low:
            variants.append({"title": "", "price": _money(low), "compare_at": "", "available": None})
    tags = get("tags") or []
    tags = [t.strip() for t in (tags.split(",") if isinstance(tags, str) else tags) if isinstance(t, str) and t.strip()]
    details = []
    for m in _nodes(get("metafields")):
        key = str(m.get("key") or "") if isinstance(m, dict) else ""
        if not key or any(x in key.lower() for x in INTERNAL_FIELDS) or not isinstance(m.get("value"), (str, int, float, bool)):
            continue
        value = {"true": "yes", "false": "no"}.get(str(m["value"]).strip().lower(), str(m["value"]).strip())
        line = f"{key.replace('_', ' ')}: {value}"
        if value and len(value) <= 200 and not value.startswith(("{", "[", "gid://")) and line not in details:
            details.append(line)
    about = str(get("description") or "").strip() or _text(get("descriptionHtml", "bodyHtml", "body_html"))
    about = re.sub(r"^(what this is|introduction)\s*[.:]\s*", "", about, flags=re.I)   # the store's section headings
    product = {
        "title": title, "gid": gid, "handle": str(get("handle") or ""),
        "type": str(get("productType", "product_type") or "").strip(), "vendor": str(get("vendor") or "").strip(),
        "tags": tags, "about": re.sub(r"\s+", " ", about), "variants": variants, "details": details[:4],
        "collections": [], "skus": skus, "active": not status or status == "ACTIVE",
        "id": gid.rsplit("/", 1)[-1] if gid else "",
        # The product photo, for the Store orders page (never read to a customer).
        "image": str((get("featuredImage", "image") or {}).get("url") or (get("featuredImage", "image") or {}).get("src") or "")
        if isinstance(get("featuredImage", "image"), dict) else "",
    }
    product["title_words"] = set(words(title))
    return product


def _index(products: list[dict]) -> None:
    for p in products:
        p["all_words"] = p["title_words"] | set(words(" ".join([p["type"], p["handle"].replace("-", " "),
                                                                 *p["tags"], *p["collections"]])))
    STATE["products"] = products
    ranked = sorted(products, key=lambda p: p["active"])          # active last, so they win below
    STATE["by_title"] = {_key(p["title"]): p for p in ranked}
    STATE["by_id"] = {p["id"]: p for p in products if p["id"]}
    STATE["by_sku"] = {sku: p for p in ranked for sku in p["skus"]}


async def _load() -> tuple[list[dict], str]:
    from pymongo import AsyncMongoClient   # here: the server starts without it when no catalogue is set
    client = AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000, appname="voice-agent")
    try:
        db = client[MONGODB_DB]
        names = await db.list_collection_names()
        query = {"shop": MONGODB_SHOP} if MONGODB_SHOP else {}
        coll = MONGODB_PRODUCTS or next((n for n in sorted(names) if "product" in n.lower() and "collection" not in n.lower()), "")
        found: dict[str, dict] = {}
        if coll:
            async for doc in db[coll].find(query):
                p = parse(doc)
                if p:
                    found[p["gid"] or _key(p["title"])] = p
        source = coll
        if MONGODB_COLLECTIONS in names:
            # Shopify collections ("Rudraksha", "Bracelets"): their titles help a search; and if there is no
            # product collection of its own, the products listed inside them are the catalogue.
            async for doc in db[MONGODB_COLLECTIONS].find({**query, "isDeleted": {"$ne": True}}):
                raw = doc.get("rawShopifyData") if isinstance(doc.get("rawShopifyData"), dict) else {}
                ctitle = str(doc.get("title") or raw.get("title") or "").strip()
                for item in _nodes(doc.get("products")) + _nodes(raw.get("products")):
                    p = parse(item) if isinstance(item, dict) else None
                    key = (p["gid"] or _key(p["title"])) if p else ""
                    if p and key not in found and not coll:
                        found[key] = p
                    if key in found and ctitle and ctitle.lower() not in ("home page", "all", "all products"):
                        found[key]["collections"].append(ctitle)
            if not coll:
                source = f"{MONGODB_COLLECTIONS} (products listed inside collections)"
        return list(found.values()), source or "no product collection found"
    finally:
        await client.close()


async def refresh() -> dict:
    if not enabled():
        return status()
    try:
        products, source = await _load()
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("catalogue not read: %s (keeping %s products from before)", STATE["error"], len(STATE["products"]))
        return status()
    STATE["source"] = source
    if not products:
        STATE["error"] = f"no products found in {MONGODB_DB} ({source})"
        log.warning("catalogue: %s", STATE["error"])
        return status()
    _index(products)
    STATE["loaded_at"], STATE["error"] = now_iso(), ""
    log.info("catalogue: %s products from %s.%s", len(products), MONGODB_DB, source)
    return status()


async def worker() -> None:
    if not enabled():
        return
    while True:
        await refresh()
        await asyncio.sleep(max(1.0, CATALOG_REFRESH_MINUTES) * 60)


# --- matching what a customer says to a product ------------------------------------------------------

# Saaras writes Hindi in Devanagari, the catalogue is in English letters; and "paanch mukhi" is "5 Mukhi".
_NUMBERS = {w: str(n) for n, ws in enumerate([
    (), ("ek", "एक"), ("do", "दो"), ("teen", "तीन"), ("char", "chaar", "चार"), ("panch", "paanch", "पांच", "पाँच"),
    ("chhe", "chhah", "che", "छह", "छः", "छे"), ("saat", "सात"), ("aath", "आठ"), ("nau", "नौ"), ("das", "दस"),
    ("gyarah", "gyara", "ग्यारह"), ("barah", "bara", "बारह"), ("terah", "तेरह"), ("chaudah", "चौदह"),
]) for w in ws}
_HINDI = {**{hi: en.lower() for en, hi in PRONUNCIATION.items()}, **{
    "मुखी": "mukhi", "माला": "mala", "ब्रेसलेट": "bracelet", "कड़ा": "kada", "रत्न": "gemstone", "स्टोन": "stone",
    "पेंडेंट": "pendant", "लॉकेट": "locket", "अंगूठी": "ring", "रिंग": "ring", "चांदी": "silver", "सोना": "gold",
    "गौरी": "gauri", "शंकर": "shankar", "गणेश": "ganesh", "कछुआ": "tortoise", "पिरामिड": "pyramid", "सिक्का": "coin",
}}
# The same name spelt several ways, in the titles and in what Saaras writes.
_SPELLINGS = {"shree": "shri", "sri": "shri", "shre": "shri", "ganesha": "ganesh", "laxmi": "lakshmi", "laxmiji": "lakshmi",
              "rudraksh": "rudraksha", "rudrakhs": "rudraksha", "rudraksh": "rudraksha", "panchmukhi": "5 mukhi",
              "ekmukhi": "1 mukhi", "navgrah": "navgraha", "kuber": "kubera", "kaala": "kala", "pendent": "pendant"}
_STOP = {"ji", "जी", "hai", "hain", "kya", "ka", "ki", "ke", "ko", "aap", "aapke", "aapka", "paas", "wala", "wali", "the", "and", "of",
         "for", "with", "a", "an", "mein", "me", "se", "chahiye", "kitne", "kitna", "price", "rate", "batao", "bataiye",
         "क्या", "है", "हैं", "का", "की", "के", "में", "से", "वाला", "वाली", "आपके", "पास", "कितने", "कितना"}


def words(text: str) -> list[str]:
    out = []
    for raw_word in re.split(r"[\s,/|()\-:+&]+", str(text or "").lower()):
        w = raw_word.strip(".!?'\"")
        if not w:
            continue
        w = _NUMBERS.get(w) or _HINDI.get(w) or w
        for part in w.split():
            part = _SPELLINGS.get(part, part)
            # bracelets / bracelet, malas / mala: the same on both sides, so a plain cut is enough.
            if len(part) > 4 and part.endswith("s") and not part.endswith("ss") and part.isascii():
                part = part[:-1]
            for piece in part.split():
                if piece not in _STOP:
                    out.append(piece)
    return out


def _key(title: str) -> str:
    """A title as one key: case, spaces and punctuation ignored. The store lists some products twice that differ only
    so ("Golden Glow" after "|" and after "||"), and those are one product to a customer."""
    return " ".join(re.sub(r"[^\w]+", " ", str(title or "").lower()).split())


def search(query: str, limit: int = 3) -> list[dict]:
    q = list(dict.fromkeys(words(query)))
    if not q:
        return []
    scored = []
    for p in STATE["products"]:
        if not p["active"]:
            continue    # a draft or unlisted product is never offered
        if p["variants"] and all(re.fullmatch(r"0*(\.0*)?", v["price"] or "0") for v in p["variants"]):
            continue    # a free-gift listing: never offered as a product, never quoted at Rs 0
        in_title = sum(1 for w in q if w in p["title_words"] or (len(w) >= 4 and any(t.startswith(w) for t in p["title_words"])))
        anywhere = sum(1 for w in q if w in p["all_words"])
        # Most of what they said must be in the product: "gomti chakra" is not the "Seven Chakra Diya".
        if max(in_title, anywhere) < 0.6 * len(q):
            continue
        score = (2 * in_title + anywhere) / (3 * len(q))
        in_stock = any(v["available"] is not False for v in p["variants"]) if p["variants"] else True
        scored.append((score, in_stock, -len(p["title_words"]), p))
    # The same product is often listed twice (an old and a new listing): the one in stock first.
    scored.sort(key=lambda s: s[:3], reverse=True)
    best = scored[0][0] if scored else 0
    # Keep the close ones only: "5 mukhi rudraksha" should not bring every rudraksha along.
    out, seen = [], set()
    for score, _, _, p in scored:
        if score >= max(0.34, best - 0.2) and _key(p["title"]) not in seen:
            seen.add(_key(p["title"]))
            out.append(p)
    return out[:limit]


def product_ids(contact: dict) -> list[str]:
    """A contact's Shopify product ids (numbers or gid://shopify/Product/... in the sheet), as numbers."""
    return [i.rsplit("/", 1)[-1] for i in re.split(r"[,\s]+", str(contact.get("product_ids") or "")) if i.strip()]


def find_for_contact(contact: dict) -> list[dict]:
    """The catalogue products behind a contact: by Shopify product id when it has them (a Shopify order or cart),
    else by the names in its "product" (a sheet's column)."""
    if not STATE["products"]:
        return []
    found: list[dict] = []
    for i in product_ids(contact):
        p = STATE["by_id"].get(i)
        if p and p not in found:
            found.append(p)
    if found:
        return found[:MAX_IN_PROMPT]
    for item in re.split(r",\s*", str(contact.get("product") or "")):
        item = re.sub(r"\s+x\d+$", "", item.strip())
        if not item:
            continue
        # The item's name, then the name without a trailing "(variant)", which may itself hold brackets:
        # "Kirtimukh ... Golden Glow (12 cm (height) × 11 cm (width))".
        names = [item] + [item[:m.start()] for m in re.finditer(r"\s\(", item)]
        p = next((STATE["by_title"][_key(n)] for n in names if _key(n) in STATE["by_title"]), None)
        if not p:
            hits = search(item, 1)
            p = hits[0] if hits and len(set(words(item)) & hits[0]["title_words"]) >= max(1, len(set(words(item))) * 0.6) else None
        if p and p not in found:
            found.append(p)
    return found[:MAX_IN_PROMPT]


def facts(p: dict, own: bool = False) -> str:
    """One product as the agent reads it: name, price or options, stock, a short description.

    own: a product of the call's own order or cart. Its price is the order's amount (the catalogue's
    differs by shipping or COD charges: Rs 899 against an order of Rs 919), and a stock count is no
    news to give a customer confirming an order, so neither is shown; only what the product is."""
    lines = [f"- {p['title']}"]
    priced = [v for v in p["variants"] if v["price"]] if not own else []
    prices = {v["price"] for v in priced}
    if len(prices) == 1:
        v = priced[0]
        mrp = f" (MRP Rs {v['compare_at']})" if v["compare_at"] and v["compare_at"] != v["price"] else ""
        lines.append(f"  Price: Rs {v['price']}{mrp}")
    elif priced:
        opts = [f"{v['title'] or 'standard'} Rs {v['price']}" + (" (out of stock)" if v["available"] is False else "")
                for v in priced[:6]]
        lines.append("  Options: " + "; ".join(opts))
    if not own and p["variants"] and all(v["available"] is False for v in p["variants"]):
        lines.append("  Stock: out of stock right now")
    if p["type"]:
        lines.append(f"  Type: {p['type']}")
    if p["about"]:
        lines.append(f"  About: {_short(p['about'], ABOUT_CHARS)}")
    if p["details"]:
        lines.append("  Details: " + "; ".join(p["details"]))
    return "\n".join(lines)


# --- when a call starts: the order and its products, fresh from the database ---------------------------

LOOKUP_SECONDS = 3.0       # the phone is already being dialled; never hold a call up longer than this
_CLIENT: dict = {}


def _client():
    """One connection for the lookups at call start (opening one costs more than the lookup itself)."""
    from pymongo import AsyncMongoClient
    loop = asyncio.get_running_loop()
    if _CLIENT.get("loop") is not loop:
        _CLIENT.update(loop=loop, client=AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=2500, appname="voice-agent"))
    return _CLIENT["client"]


def _digits(text) -> str:
    return re.sub(r"\D", "", str(text or ""))


def _when(value) -> str:
    return value.strftime("%d %b %Y") if hasattr(value, "strftime") else ""


def _status_word(text) -> str:
    return str(text or "").replace("_", " ").strip().lower()


def order_status(o: dict) -> str:
    """What the agent may say about where an order is: only what the record says, nothing promised."""
    lines = [f"- Placed: {_when(o.get('shopifyCreatedAt') or o.get('createdAt')) or 'on record'}"]
    if o.get("cancelledAt"):
        reason = _status_word(o.get("cancelReason"))
        lines.append(f"- Cancelled on {_when(o['cancelledAt'])}" + (f" ({reason})" if reason else ""))
    elif o.get("deliveredAt") or _status_word(o.get("deliveryStatus")) == "delivered":
        lines.append("- Delivered" + (f" on {_when(o['deliveredAt'])}" if o.get("deliveredAt") else ""))
    elif _status_word(o.get("fulfillmentStatus")) in ("fulfilled", "partially fulfilled") or o.get("trackingNumber"):
        courier = str(o.get("courier") or "").strip()
        lines.append("- Shipped" + (f" with {courier}" if courier else "")
                     + (f", tracking number {o['trackingNumber']}" if o.get("trackingNumber") else ""))
        if _status_word(o.get("deliveryStatus")):
            lines.append(f"- Courier status: {_status_word(o['deliveryStatus'])}")
    else:
        lines.append("- Not shipped yet")
    return "\n".join(lines)


def _fill_from_order(contact: dict, o: dict) -> dict:
    """A sheet row with only an order number gets the order's own details; whatever the row says is kept."""
    items = [li for li in o.get("lineItems") or [] if isinstance(li, dict) and li.get("title")]
    ship = o.get("shippingAddress") or {}
    gateway = str(o.get("paymentGatewayName") or "").lower()
    found = {
        "product": ", ".join(str(li["title"]).strip() + (f" x{li['quantity']}" if int(li.get("quantity") or 1) > 1 else "")
                             for li in items),
        "quantity": str(sum(int(li.get("quantity") or 1) for li in items)) if items else "",
        "amount": re.sub(r"\.0+$", "", str(o.get("totalPrice") or o.get("currentTotalPrice") or "")),
        "payment_mode": ("COD" if o["isCOD"] else "Prepaid") if isinstance(o.get("isCOD"), bool)
        else ("COD" if "cod" in gateway or "cash" in gateway else ""),
        "address": ", ".join(str(ship[k]).strip() for k in ("address1", "address2", "city", "province", "zip")
                             if str(ship.get(k) or "").strip()),
    }
    return {**contact, **{k: v for k, v in found.items() if v and not str(contact.get(k) or "").strip()}}


async def _find_order(db, contact: dict) -> dict | None:
    """The main site's copy of this contact's order, by its number; not used if it carries another phone number."""
    number = _digits(contact.get("order_id"))
    if not number:
        return None
    for name in MONGODB_ORDERS:
        o = await db[name].find_one({"$or": [{"name": f"#{number}"}, {"orderNumber": number}, {"orderNumber": int(number)}]},
                                    sort=[("_id", -1)])
        if not o:
            continue
        theirs = _digits(o.get("phone") or (o.get("shippingAddress") or {}).get("phone"))[-10:]
        ours = _digits(contact.get("phone"))[-10:]
        if theirs and ours and theirs != ours:
            log.warning("order %s in %s has another phone number than contact %s; not used", number, name, contact.get("id"))
            return None
        return o
    return None


async def enrich(contact: dict) -> dict:
    """Called as a call is placed: the contact with its products and (for an order) the order's live status
    attached for the prompt. Prices and stock come fresh from the database by product id; a sheet row with
    only an order number gets its products through the order (by SKU, then by name). If anything fails or
    takes too long, the call goes ahead with the catalogue copy in memory."""
    if not enabled():
        return contact
    try:
        return await asyncio.wait_for(_enrich(contact), LOOKUP_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("lookup for contact %s skipped: %s", contact.get("id"), f"{type(exc).__name__}: {exc}"[:200])
        return contact


async def _enrich(contact: dict) -> dict:
    db = _client()[MONGODB_DB]
    c = dict(contact)
    order = None
    if str(c.get("campaign") or "order_confirmation") == "order_confirmation" and c.get("order_id"):
        order = await _find_order(db, c)
        if order:
            c = _fill_from_order(c, order)
            c["_order_status"] = order_status(order)
    found: list[dict] = []
    ids = product_ids(c)
    if ids:
        gids = [f"gid://shopify/Product/{i}" for i in ids]
        async for doc in db[MONGODB_PRODUCTS].find({"shopifyProductId": {"$in": gids}}):
            p = parse(doc)
            if p:
                found.append(p)
    if not found and order:
        for li in order.get("lineItems") or []:
            sku = str((li or {}).get("sku") or "").strip().lower()
            p = STATE["by_sku"].get(sku) if sku else None
            if p and p not in found:
                found.append(p)
    found = found or find_for_contact(c)
    if found:
        c["_product_facts"] = [facts(p, own=True) for p in found[:MAX_IN_PROMPT]]
    log.info("call start lookup for contact %s: order %s, products %s", c.get("id"),
             "found" if order else "none", [p["title"] for p in found[:MAX_IN_PROMPT]] or "none")
    return c


# --- what the agent gets ------------------------------------------------------------------------------

def product_block(contact: dict, voice: str) -> str:
    """Added to a call's prompt: the order's live status, the call's own products, and how to look up any other."""
    parts = []
    if contact.get("_order_status"):
        parts.append('ORDER STATUS (from our system as this call started; "kab aayega?", "kahan hai?" are answered only '
                     "from this, never with a date that is not here)\n" + contact["_order_status"])
    product_facts = contact.get("_product_facts") or [facts(p, own=True) for p in find_for_contact(contact)]
    if product_facts:
        parts.append("PRODUCT INFO (what the customer's products are, from our catalogue; a question about them is answered "
                     "from here in one short sentence. Their price is the amount above; stock and anything not here: the team)\n"
                     + "\n".join(product_facts))
    if not STATE["products"]:
        return "\n\n" + "\n\n".join(parts) if parts else ""
    parts.append(f'ANOTHER PRODUCT: a question about a product that is not in PRODUCT INFO ("5 mukhi rudraksha hai?", '
                 f'"bracelet kitne ka hai?"): say "ek second ji, dekh {forms(voice)["rahi"]} hoon" and call product_info with '
                 'the product in English letters. Answer from what it returns in one short sentence, then your question again.')
    return "\n\n" + "\n\n".join(parts)


NOTED_LINE = '"ji, aapka yeh sawaal maine note kar liya hai"'


def tool_answer(query: str, order_amount: str = "") -> dict:
    """product_info's reply to the agent. order_amount: the call's own order or cart, so the new total is worked out
    here and never by the model (owner, 12 Oct 2026: "Kirtimukh ke saath yeh bhi add kar diya, ab total itna hai")."""
    if not STATE["products"]:
        return {"found": False, "say": f"The catalogue is not available right now. Say {NOTED_LINE} and put the question in question."}
    hits = search(query, 3)
    if not hits:
        return {"found": False, "say": f"No product by that name in our catalogue. Ask once which product they mean; if it is "
                f"still not found, say {NOTED_LINE} and put it in question. Never say we do not sell it."}
    answer = {"found": True, "products": [facts(p) for p in hits],
              "say": "Answer only from these, in one short sentence: what they asked (price, size, what it is). If none of these "
                     f"is what they asked for, say {NOTED_LINE}. Never name a price or product that is not here."}
    try:
        base, price = float(re.sub(r"[^\d.]", "", order_amount) or 0), float(_price(hits[0]).split()[0] or 0)
    except ValueError:
        base = price = 0
    if base and price:
        answer["total_if_added"] = f"{base + price:g} rupaye (the order {base:g} + {hits[0]['title']} {price:g})"
    others = related(hits[0], exclude=hits)
    if others:
        answer["related"] = [f"{p['title']}, Rs {_price(p)}" for p in others]
        answer["say"] += (" Name one of related only if they ask what else there is, or what they asked for is out of "
                          "stock; never push it.")
    return answer


def _price(p: dict) -> str:
    prices = sorted({v["price"] for v in p["variants"] if v["price"]}, key=lambda x: float(x or 0))
    return prices[0] if len(prices) == 1 else f"{prices[0]} se" if prices else ""


def _in_stock(p: dict) -> bool:
    return not p["variants"] or any(v["available"] is not False for v in p["variants"])


def related(p: dict, exclude: list[dict] = (), n: int = 2) -> list[dict]:
    """Products like this one, for "aur kya hai?" or when it is out of stock: the same type or collection,
    sharing the most tags, active, in stock and priced; never another listing of the same product."""
    taken = {_key(x["title"]) for x in [p, *exclude]}
    tags = {t.lower() for t in p["tags"]}
    scored = []
    for q in STATE["products"]:
        if not q["active"] or not _in_stock(q) or not _price(q) or _key(q["title"]) in taken:
            continue
        same_type = bool(p["type"]) and q["type"] == p["type"]
        same_collection = bool(set(p["collections"]) & set(q["collections"]))
        shared = len(tags & {t.lower() for t in q["tags"]})
        if not (same_type or same_collection or shared >= 2):
            continue
        scored.append((2 * same_type + 2 * same_collection + shared, q))
    scored.sort(key=lambda s: s[0], reverse=True)
    out, seen = [], set()
    for _, q in scored:
        if _key(q["title"]) not in seen:
            seen.add(_key(q["title"]))
            out.append(q)
        if len(out) == n:
            break
    return out


def status() -> dict:
    return {"enabled": enabled(), "products": len(STATE["products"]), "loaded_at": STATE["loaded_at"],
            "source": STATE["source"], "error": STATE["error"], "database": MONGODB_DB if enabled() else ""}


@router.get("/api/catalog")
async def catalog_status(q: str = "") -> dict:
    """The catalogue's state, and with ?q=... what product_info would answer (free: no AI involved)."""
    out = status()
    if q:
        out["answer"] = tool_answer(q)
    return out


@router.post("/api/catalog/refresh")
async def catalog_refresh() -> dict:
    return await refresh()
