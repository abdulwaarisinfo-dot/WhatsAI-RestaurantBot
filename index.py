"""
WhatsApp AI Restaurant Bot — FastAPI Backend (Production v13.0)
===============================================================
v13.0 fixes & enhancements over v12.0:

  ✅ FIX G: "✅ Order Now" button now correctly resumes the pending product
             stored in session["last_shown_product"]. No more "I'm not sure"
             fallback when user taps Order Now after seeing item sizes.

  ✅ FIX H: Smart keyword matching — ALL dish names, categories, and aliases
             are auto-indexed from PRODUCTS_DATA at startup so the bot
             recognises any product mention without hardcoded lists.

  ✅ FIX I: Human-friendly NLP — the bot now handles:
             • Casual phrasing: "give me a pizza", "I'll have biryani",
               "bhai ek burger dena", "kuch meetha chahiye"
             • Affirmative continuations: "yes", "sure", "haan", "theek hai"
               correctly resume the active product flow instead of going to
               confirm-order.
             • Clarification questions: "what sizes do you have?",
               "tell me about your biryani", "kaisa biryani hai?"
             • Polite chit-chat: greetings mid-order, "thank you", etc.

  ✅ FIX J: Multi-order in one message fully robust:
             "1 half biryani aur 2 xl pizza aur 1 small burger"
             all parsed and queued correctly even with Urdu conjunctions.

  ✅ FIX K: "Order Now" / "Order Again" button stores + retrieves the last
             confirmed order items for instant reorder.

  ✅ KEEP: All v12.0 fixes 100% preserved (A–F).
"""

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from dotenv import load_dotenv
from langdetect import detect, DetectorFactory
import certifi, os, re, logging, random, httpx, asyncio, json, time
from typing import List, Dict, Any, Optional
from datetime import datetime
from collections import defaultdict
from difflib import SequenceMatcher

# ============================================================
# INITIAL SETUP
# ============================================================

load_dotenv()
DetectorFactory.seed = 0
logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RestaurantBot.v13")

BOT_DATA: Dict[str, Any] = {}
PRODUCTS_DATA: List[Dict[str, Any]] = []

# FIX H: Auto-built product keyword index { keyword_lower: product_dict }
PRODUCT_KEYWORD_INDEX: Dict[str, Dict] = {}

# In-memory session store
USER_SESSIONS: Dict[str, Dict[str, Any]] = {}

# In-memory rate limiter
_rate_store: Dict[str, list] = defaultdict(list)
RATE_LIMIT_PER_MINUTE = 15


def _is_rate_limited(user_id: str) -> bool:
    now = time.time()
    timestamps = [t for t in _rate_store[user_id] if now - t < 60]
    _rate_store[user_id] = timestamps
    if len(timestamps) >= RATE_LIMIT_PER_MINUTE:
        return True
    _rate_store[user_id].append(now)
    return False


app = FastAPI(
    title="WhatsApp AI Restaurant Bot v13.0",
    version="13.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

MONGO_URI         = os.getenv("MONGO_URI", "")
WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN", "my_verify_token")
SECRET_PASSWORD   = os.getenv("SECRET_PASSWORD", "admin")
CRM_USERNAME      = os.getenv("USER_NAME", "admin")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

WHATSAPP_API_URL = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_ID}/messages"

# ============================================================
# DATABASE CONNECTION
# ============================================================

try:
    client = MongoClient(
        MONGO_URI,
        tls=True,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=10000,
    )
    db            = client["restaurant"]
    products_col  = db["products"]
    meta_col      = db["bot_metadata"]
    analytics_col = db["analytics"]
    orders_col    = db["orders"]
    carts_col     = db["carts"]
    sessions_col  = db["sessions"]

    client.admin.command("ping")
    logger.info("✅ MongoDB connected successfully")

except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    products_col = meta_col = analytics_col = orders_col = carts_col = sessions_col = None

# ============================================================
# UTILITY HELPERS
# ============================================================

def _str_id(doc: dict) -> dict:
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


def _parse_json_field(value, fallback=None):
    if fallback is None:
        fallback = []
    if not value:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _normalize_size(raw: str) -> str:
    s = raw.lower().strip()
    s = re.sub(r'half\s*kg?', '0.5kg', s)
    s = re.sub(r'quarter\s*kg?', '0.25kg', s)
    s = re.sub(r'(\d+\.?\d*)\s*gram[s]?', lambda m: f"{float(m.group(1))/1000}kg", s)
    s = re.sub(r'(\d+\.?\d*)\s*g\b',      lambda m: f"{float(m.group(1))/1000}kg", s)
    s = re.sub(r'(\d+\.?\d*)\s*kg',       lambda m: f"{float(m.group(1))}kg",      s)
    s = re.sub(r'(\d+\.?\d*)\s*ml',       lambda m: f"{int(float(m.group(1)))}ml", s)
    s = re.sub(r'(\d+\.?\d*)\s*l\b',      lambda m: f"{float(m.group(1))}l",       s)

    plate_map = {
        'half plate':    'Half Plate',
        'half plat':     'Half Plate',
        'halfplate':     'Half Plate',
        'full plate':    'Full Plate',
        'full plat':     'Full Plate',
        'fullplate':     'Full Plate',
        'family pack':   'Family Pack',
        'familypack':    'Family Pack',
        'family':        'Family Pack',
        'quarter plate': 'Quarter Plate',
    }
    for k, v in plate_map.items():
        if k in s:
            return v

    size_map = {
        'small':   'Small',
        'medium':  'Medium',
        'large':   'Large',
        'regular': 'Regular',
        'xl':      'XL',
        'xxl':     'XXL',
        'full':    '1kg',
        'half':    '0.5kg',
    }
    for k, v in size_map.items():
        if re.search(r'\b' + re.escape(k) + r'\b', s):
            return v

    return s.strip()


def _match_variant(variants: List[Dict], size_hint: str) -> Optional[Dict]:
    if not variants or not size_hint:
        return None
    normalized = _normalize_size(size_hint.strip()).lower()

    for v in variants:
        if v.get("size", "").lower().strip() == normalized:
            return v

    for v in variants:
        vs = v.get("size", "").lower().strip()
        if normalized in vs or vs in normalized:
            return v

    norm_words = set(re.findall(r'\w+', normalized))
    best_score, best_v = 0, None
    for v in variants:
        vs_words = set(re.findall(r'\w+', v.get("size", "").lower()))
        score    = len(norm_words & vs_words)
        if score > best_score:
            best_score = score
            best_v     = v
    if best_score > 0:
        return best_v

    return None


def _recalc_cart(cart_items: List[Dict]) -> float:
    return sum(
        (item.get("base_price", 0) + item.get("extras_price", 0)) * item.get("quantity", 1)
        for item in cart_items
    )


def _build_cart_summary(
    items: List[Dict],
    total: float,
    lang: str = "en",
    delivery_charge: float = 0.0,
    show_delivery: bool = False,
) -> str:
    headers = {
        "en": "🛒 *Your Cart:*\n",
        "ur": "🛒 *آپ کی ٹوکری:*\n",
        "de": "🛒 *Ihr Warenkorb:*\n",
    }
    lines = [headers.get(lang, headers["en"])]
    for item in items:
        name   = item.get("title", "Item").strip().title()
        size   = item.get("size", "").strip()
        qty    = item.get("quantity", 1)
        price  = (item.get("base_price", 0) + item.get("extras_price", 0)) * qty
        extras = ", ".join(e.strip().title() for e in item.get("extras", []))
        spice  = item.get("spice", "").strip().title()
        line   = f"• *{name}*"
        if size:    line += f" ({size})"
        if qty > 1: line += f" ×{qty}"
        line += f" — PKR {int(price)}"
        if extras:  line += f"\n  ➕ Extras: {extras}"
        if spice:   line += f"\n  🌶️ Spice: {spice}"
        lines.append(line)

    totals = {
        "en": f"\n💰 *Subtotal: PKR {int(total)}*",
        "ur": f"\n💰 *سب ٹوٹل: PKR {int(total)}*",
        "de": f"\n💰 *Zwischensumme: PKR {int(total)}*",
    }
    lines.append(totals.get(lang, totals["en"]))

    if show_delivery:
        if delivery_charge > 0:
            dc_line = {
                "en": f"🚚 *Delivery: PKR {int(delivery_charge)}*",
                "ur": f"🚚 *ڈلیوری: PKR {int(delivery_charge)}*",
                "de": f"🚚 *Lieferung: PKR {int(delivery_charge)}*",
            }
        else:
            dc_line = {
                "en": "🚚 *Delivery: FREE* 🎉",
                "ur": "🚚 *ڈلیوری: مفت* 🎉",
                "de": "🚚 *Lieferung: KOSTENLOS* 🎉",
            }
        lines.append(dc_line.get(lang, dc_line["en"]))
        grand = total + delivery_charge
        gt_line = {
            "en": f"💳 *Grand Total: PKR {int(grand)}*",
            "ur": f"💳 *کل رقم: PKR {int(grand)}*",
            "de": f"💳 *Gesamtbetrag: PKR {int(grand)}*",
        }
        lines.append(gt_line.get(lang, gt_line["en"]))

    return "\n".join(lines)


def _build_full_price_menu(products: List[Dict], category_emoji: str = "🍽️", title: str = "Menu & Prices") -> str:
    lines = [f"{category_emoji} *{title}*\n"]
    for product in products:
        lines.append(f"• *{product.get('title', 'Item').strip().title()}*")
        variants = product.get("variants", [])
        if variants:
            for v in variants:
                lines.append(f"  ‣ {v.get('size', 'N/A')} — PKR {v.get('price', '?')}")
        else:
            price = product.get("price", "N/A")
            lines.append(f"  ‣ PKR {price}")
        lines.append("")
    return "\n".join(lines).strip()


def _fuzzy_match_extra(query_word: str, extra_name: str, threshold: float = 0.75) -> bool:
    q = query_word.lower().strip()
    e = extra_name.lower().strip()
    if q in e or e in q:
        return True
    ratio = SequenceMatcher(None, q, e).ratio()
    return ratio >= threshold


def _extract_extras_from_text(text: str, extras_options: List[Dict]) -> List[str]:
    q      = text.lower()
    chosen = []
    for e in extras_options:
        name = e["name"].strip()
        if name.lower() in q:
            chosen.append(name.strip().title())
            continue
        extra_words = name.lower().split()
        query_words = re.findall(r'\w+', q)
        matched_words = 0
        for ew in extra_words:
            for qw in query_words:
                if _fuzzy_match_extra(qw, ew):
                    matched_words += 1
                    break
        if matched_words == len(extra_words):
            chosen.append(name.strip().title())
    return chosen


_ADDRESS_KEYWORDS = re.compile(
    r'\b(street|st|road|rd|avenue|ave|lane|block|sector|phase|house|flat|floor|'
    r'building|near|opposite|plot|no\.?|number|h\.?no|area|town|city|karachi|'
    r'lahore|islamabad|gulshan|clifton|defence|defense|dha|gulberg|johar|nazimabad|'
    r'گلی|سڑک|گھر|مکان|بلاک|فیز|شہر|پتہ)\b',
    re.IGNORECASE,
)

_SHORT_WORDS = {
    "yes", "no", "ok", "okay", "sure", "fine", "yep", "yeah",
    "haan", "nahi", "done", "same", "correct", "right",
}


def _is_valid_address(text: str) -> bool:
    stripped = text.strip()
    if stripped.lower() in _SHORT_WORDS:
        return False
    if len(stripped) < 8:
        return False
    if _ADDRESS_KEYWORDS.search(stripped):
        return True
    if len(stripped) >= 12 and (any(c.isdigit() for c in stripped) or ',' in stripped):
        return True
    return len(stripped) >= 18

# ============================================================
# DATA LOADER + FIX H: Auto product keyword index
# ============================================================

def _build_product_keyword_index():
    """
    Auto-index every product by all meaningful words in its title, category,
    description, and any aliases. Called after PRODUCTS_DATA is loaded.
    """
    global PRODUCT_KEYWORD_INDEX
    PRODUCT_KEYWORD_INDEX = {}

    for product in PRODUCTS_DATA:
        title    = product.get("title", "")
        category = product.get("category", "")
        desc     = product.get("description", "")

        # Full title as a key
        PRODUCT_KEYWORD_INDEX[title.lower().strip()] = product

        # Each word of title (>2 chars)
        for word in re.findall(r'\w+', title.lower()):
            if len(word) > 2:
                PRODUCT_KEYWORD_INDEX.setdefault(word, product)

        # Category
        if category:
            PRODUCT_KEYWORD_INDEX.setdefault(category.lower().strip(), product)

        # Common aliases for each category
        aliases = {
            "burger":  ["burger", "brgr", "برگر", "zinger", "cheeseburger"],
            "pizza":   ["pizza", "پیزا", "پیزہ", "margherita", "pepperoni"],
            "biryani": ["biryani", "بریانی", "baryani", "rice dish", "dum biryani"],
            "drinks":  ["drink", "مشروب", "cola", "juice", "coke", "pepsi", "lassi",
                        "cold drink", "soda", "7up", "sprite", "fanta"],
            "dessert": ["dessert", "مٹھائی", "sweet", "cake", "kheer", "meetha",
                        "halwa", "gulab jamun"],
            "karahi":  ["karahi", "کڑاہی", "karai", "karhai"],
            "rice":    ["rice", "چاول", "pulao", "fried rice"],
            "rolls":   ["roll", "رول", "shawarma", "wrap", "paratha roll"],
        }
        cat_lower = category.lower()
        for cat, kws in aliases.items():
            if cat == cat_lower or cat in desc.lower():
                for kw in kws:
                    PRODUCT_KEYWORD_INDEX.setdefault(kw, product)

    logger.info(f"Product keyword index built: {len(PRODUCT_KEYWORD_INDEX)} keys")


def load_data_realtime():
    global PRODUCTS_DATA, BOT_DATA
    if products_col is None or meta_col is None:
        return
    try:
        PRODUCTS_DATA = [_str_id(p) for p in products_col.find({})]
        _build_product_keyword_index()

        merged: Dict[str, Any] = {}
        for doc in meta_col.find({}):
            _str_id(doc)
            merged.update({k: v for k, v in doc.items() if k != "_id"})

        BOT_DATA = {
            "supported_languages":      ["en", "ur", "de"],
            "initial_message":          {
                "en": "Welcome! 🍽️ How can I help you today?",
                "ur": "خوش آمدید! 🍽️ آج میں آپ کی کیا مدد کر سکتا ہوں؟",
                "de": "Willkommen! 🍽️ Wie kann ich Ihnen heute helfen?",
            },
            "discount_message":         {},
            "faq":                      {},
            "smart_suggestions":        {},
            "delivery_time":            "35-45 mins",
            "delivery_time_exceptions": {},
            "delivery_charges": {
                "flat_charge":   0,
                "free_above":    0,
                "per_area":      {},
                "free_keywords": [],
            },
        }
        BOT_DATA.update(merged)

        config_doc = meta_col.find_one({"type": "config"})
        if config_doc:
            _str_id(config_doc)
            priority_keys = [
                "faq", "smart_suggestions", "initial_message",
                "discount_message", "supported_languages",
                "delivery_time", "delivery_time_exceptions",
                "delivery_charges",
            ]
            for k in priority_keys:
                if k in config_doc:
                    BOT_DATA[k] = config_doc[k]

        dc = BOT_DATA.get("delivery_charges", {})
        if not isinstance(dc, dict):
            dc = {}
        BOT_DATA["delivery_charges"] = {
            "flat_charge":   float(dc.get("flat_charge", 0) or 0),
            "free_above":    float(dc.get("free_above", 0) or 0),
            "per_area":      dc.get("per_area", {}) if isinstance(dc.get("per_area"), dict) else {},
            "free_keywords": dc.get("free_keywords", []) if isinstance(dc.get("free_keywords"), list) else [],
        }

        logger.info(
            f"Data synced | Products: {len(PRODUCTS_DATA)} | "
            f"FAQ keys: {list(BOT_DATA.get('faq', {}).keys())} | "
            f"Delivery time: {BOT_DATA['delivery_time']} | "
            f"Delivery charges: {BOT_DATA['delivery_charges']}"
        )
    except Exception as e:
        logger.error(f"Data load error: {e}")


def get_delivery_time(category: str = "") -> str:
    def _unwrap(val, fallback: str = "35-45 mins") -> str:
        if val is None:
            return fallback
        if isinstance(val, str):
            return val.strip() or fallback
        if isinstance(val, dict):
            if category and category.lower() in {k.lower() for k in val}:
                for k, v in val.items():
                    if k.lower() == category.lower():
                        return _unwrap(v, fallback)
            if "default" in val:
                return _unwrap(val["default"], fallback)
            if val:
                return _unwrap(next(iter(val.values())), fallback)
        return fallback

    exceptions = BOT_DATA.get("delivery_time_exceptions", {})
    default_raw = BOT_DATA.get("delivery_time", "35-45 mins")
    default     = _unwrap(default_raw, "35-45 mins")

    if category:
        for k, v in exceptions.items():
            if k.lower() == category.lower():
                return _unwrap(v, default)

    return default

# ============================================================
# DELIVERY CHARGES ENGINE
# ============================================================

def calculate_delivery_charge(order_total: float, address: str = "") -> float:
    dc = BOT_DATA.get("delivery_charges", {})
    flat_charge   = float(dc.get("flat_charge", 0) or 0)
    free_above    = float(dc.get("free_above", 0) or 0)
    per_area      = dc.get("per_area", {}) if isinstance(dc.get("per_area"), dict) else {}
    free_keywords = dc.get("free_keywords", []) if isinstance(dc.get("free_keywords"), list) else []

    addr_lower = address.lower()
    for kw in free_keywords:
        if str(kw).lower().strip() in addr_lower:
            return 0.0
    if free_above > 0 and order_total >= free_above:
        return 0.0
    for area_key, area_charge in per_area.items():
        if str(area_key).lower().strip() in addr_lower:
            return float(area_charge)
    return flat_charge


def _delivery_charge_info_text(charge: float, lang: str) -> str:
    if charge <= 0:
        return {"en": "🚚 Free delivery!", "ur": "🚚 مفت ڈلیوری!", "de": "🚚 Kostenlose Lieferung!"}.get(lang, "🚚 Free delivery!")
    return {
        "en": f"🚚 Delivery charge: PKR {int(charge)}",
        "ur": f"🚚 ڈلیوری چارج: PKR {int(charge)}",
        "de": f"🚚 Liefergebühr: PKR {int(charge)}",
    }.get(lang, f"🚚 Delivery: PKR {int(charge)}")


def init_analytics():
    if analytics_col is not None and analytics_col.count_documents({"type": "analytics"}) == 0:
        analytics_col.insert_one({
            "type": "analytics",
            "total_searches": 0,
            "total_orders": 0,
            "total_clicks": 0,
            "total_cart_additions": 0,
            "most_questions": {},
            "product_search": {},
            "product_clicks": {},
            "size_preference": {},
            "spice_preference": {},
            "extras_preference": {},
            "supported_languages": {},
        })


def _track(inc_dict: Dict):
    if analytics_col is not None:
        analytics_col.update_one({"type": "analytics"}, {"$inc": inc_dict})

# ============================================================
# LANGUAGE DETECTION
# ============================================================

_BUTTON_TEXTS = {
    "view menu 📋", "place order 🛒", "contact us 📞",
    "✅ confirm order", "➕ add more", "🗑️ clear cart",
    "✅ order now", "📋 view menu", "✅ order now",
    "order again 🔄", "view menu", "place order", "contact us",
    "confirm order", "add more", "clear cart", "order now",
}


def detect_language(text: str, session_lang: str = "en") -> str:
    if not text or not text.strip():
        return session_lang
    stripped = text.strip()
    if any("\u0600" <= c <= "\u06FF" for c in stripped):
        return "ur"
    if len(stripped) <= 25 and stripped.lower() in _BUTTON_TEXTS:
        return session_lang
    if len(stripped) <= 10:
        return session_lang
    try:
        lang = detect(stripped)
        if lang.startswith("ur"): return "ur"
        if lang.startswith("de"): return "de"
        if lang not in ("en", "ur", "de"):
            return session_lang
        return "en"
    except Exception:
        return session_lang

# ============================================================
# KEYWORD DATABASES
# ============================================================

CATEGORY_KEYWORDS = {
    "burger":  ["burger", "برگر", "brgr", "cheeseburger", "double burger", "zinger"],
    "pizza":   ["pizza", "پیزا", "پیزہ", "margherita", "pepperoni", "pizza slice", "tikka pizza"],
    "biryani": ["biryani", "بریانی", "dum biryani", "chicken biryani", "beef biryani", "baryani"],
    "drinks":  ["drink", "مشروب", "juice", "cola", "water", "سافٹ ڈرنک", "lassi",
                "coke", "pepsi", "7up", "sprite", "fanta", "soda", "cold drink"],
    "dessert": ["dessert", "مٹھائی", "cake", "kheer", "halwa", "gulab jamun", "brownie",
                "meetha", "sweet", "mithai"],
    "karahi":  ["karahi", "کڑاہی", "chicken karahi", "beef karahi", "mutton karahi", "karhai"],
    "rice":    ["rice", "چاول", "pulao", "plov", "fried rice"],
    "rolls":   ["roll", "رول", "shawarma", "wrap", "paratha roll"],
}

# FIX I: Expanded human-friendly intent keywords
INTENT_KEYWORDS = {
    "discount":  ["discount", "sale", "deal", "offer", "cheap", "سستا", "رعایت", "rabatt",
                  "special offer", "promo", "coupon"],
    "order":     ["order", "آرڈر", "buy", "place order", "i want", "مجھے چاہیے", "bestellen",
                  "chahiye", "dena", "lena", "add", "mujhe", "give me", "i'll have",
                  "i'd like", "can i get", "get me", "send me", "bhai dena", "yaar dena",
                  "ek dena", "do dena", "lao", "manga", "mangwao", "order karo",
                  "order now", "order again"],
    "menu":      ["menu", "مینو", "menü", "what do you have", "show menu", "list", "items",
                  "all items", "show all", "kya hai", "kya milta", "aapke paas kya",
                  "what's available", "what do you serve", "show items"],
    "price":     ["price", "قیمت", "preis", "cost", "how much", "kitna", "rate",
                  "all prices", "all flavours", "all flavors", "price list", "rates",
                  "kitne ka", "kitni", "kya rate", "daam", "qeemat"],
    "greeting":  ["hi", "hello", "hey", "assalam", "السلام", "hallo", "guten tag", "سلام",
                  "start", "begin", "aoa", "aslam", "good morning", "good evening",
                  "good afternoon", "salam", "as salam", "walaikum"],
    "address":   ["address", "پتہ", "adresse", "location", "deliver to", "my address"],
    "status":    ["status", "where", "order status", "track", "delivered", "pending",
                  "where is my order", "track order", "mera order", "kahan hai order"],
    "cancel":    ["cancel", "منسوخ", "stornieren", "nahi chahiye", "remove order",
                  "delete order", "hatao", "band karo", "order cancel", "cancel order",
                  "delete", "remove", "clear order", "order delete", "order hatao",
                  "order band", "mujhe nahi chahiye", "order mat karo"],
    "cart":      ["cart", "basket", "my order", "show cart", "view cart", "what did i order",
                  "my cart", "mera cart", "meri basket"],
    "confirm":   ["confirm", "yes", "okay", "ok", "haan", "ہاں", "proceed", "place", "done",
                  "confirm order", "place order", "theek hai", "bilkul", "zaroor",
                  "sure", "absolutely", "go ahead", "finalize"],
    "clear":     ["clear cart", "empty cart", "start over", "restart", "reset cart"],
    "new_order": [
        "new order", "naya order", "start new", "nayi order", "fresh order",
        "order again", "reorder", "new aaorder", "new ordar", "new aorder",
        "order new", "nai order", "dobara order", "phir order", "again order",
    ],
    "show_total": [
        "tell me total", "show total", "my total", "mera total", "total kitna",
        "kitna total", "total kya", "abhi total", "total bta", "price total",
        "how much total", "total price", "total amount",
    ],
    "delivery_charge": [
        "delivery charge", "delivery fee", "delivery cost", "delivery kitna",
        "delivery charges", "kitna delivery", "free delivery", "delivery free",
        "ڈلیوری چارج", "ڈلیوری فیس", "liefergebühr",
    ],
    # FIX I: inquiry intent — user wants info, not to order
    "inquiry": [
        "tell me about", "what is", "describe", "kya hai", "kaisa hai",
        "batao", "bataiye", "details", "more info", "information about",
        "what sizes", "what flavors", "what options", "kaunse size",
        "kya varieties", "available sizes",
    ],
    # FIX I: thanks / chit-chat
    "thanks": [
        "thank", "thanks", "thankyou", "thank you", "shukriya", "شکریہ",
        "jazakallah", "jazak allah", "great", "awesome", "perfect", "excellent",
        "wonderful", "brilliant", "amazing",
    ],
}

# ============================================================
# SESSION MANAGEMENT
# ============================================================

def _default_session() -> Dict[str, Any]:
    return {
        "lang":                "en",
        "shown":               [],
        "step":                0,
        "pending_order":       {},
        "cart":                [],
        "missing_info_queue":  [],
        "multi_size_queue":    [],
        "product_queue":       [],
        "preferred_size":      None,
        "preferred_spice":     None,
        "frequent_items":      [],
        "last_address":        None,
        "order_count":         0,
        # FIX G: store last shown product for "Order Now" button
        "last_shown_product":  None,
        # FIX K: store last confirmed order items for reorder
        "last_order_items":    [],
    }


def get_user_session(user_id: str) -> Dict:
    if user_id not in USER_SESSIONS:
        USER_SESSIONS[user_id] = _default_session()
    session = USER_SESSIONS[user_id]
    for key, val in _default_session().items():
        if key not in session:
            session[key] = val
    return session


def reset_cart_only(session: Dict):
    session["step"]               = 0
    session["cart"]               = []
    session["pending_order"]      = {}
    session["missing_info_queue"] = []
    session["multi_size_queue"]   = []
    session["product_queue"]      = []


def reset_for_new_order(session: Dict):
    session["step"]               = 0
    session["cart"]               = []
    session["pending_order"]      = {}
    session["missing_info_queue"] = []
    session["multi_size_queue"]   = []
    session["product_queue"]      = []


def update_preferences(user_id: str, size: str = None, spice: str = None, product_title: str = None):
    session = get_user_session(user_id)
    if size:
        session["preferred_size"] = size
    if spice:
        session["preferred_spice"] = spice
    if product_title:
        freq = session.get("frequent_items", [])
        freq.append(product_title)
        session["frequent_items"] = freq[-10:]

# ============================================================
# PRODUCT HELPERS
# ============================================================

QUANTITY_WORDS = {
    "ek": 1, "one": 1, "aik": 1, "ik": 1,
    "do": 2, "two": 2, "dou": 2,
    "teen": 3, "three": 3, "tin": 3,
    "char": 4, "four": 4,
    "panch": 5, "five": 5,
    "chay": 6, "six": 6,
    "1st": 1, "2nd": 1, "3rd": 1, "4th": 1, "5th": 1,
    "first": 1, "second": 1, "third": 1, "fourth": 1,
}

SIZE_HINTS = [
    "family pack", "half plate", "full plate", "quarter plate",
    "half plat", "full plat",
    "0.5kg", "1.5kg", "2kg", "1kg", "0.25kg", "half kg", "1 kg", "2 kg",
    "500ml", "1.5l", "1.5L", "1l",
    "small", "medium", "large", "regular", "xl", "xxl",
    "half", "full",
]

# FIX I: Noise prefixes for cleaning order text
_ORDER_NOISE_PREFIXES = re.compile(
    r'^(i\s+want\s+to\s+order|i\s+want|want\s+to\s+order|please\s+give\s+me|'
    r'please|kindly|mujhe\s+chahiye|mujhe|chahiye|dena|lena|please\s+give|'
    r'give\s+me|add|can\s+i\s+get|get\s+me|send\s+me|i\'ll\s+have|'
    r'i\s+would\s+like|i\'d\s+like|bhai\s+dena|yaar\s+dena|bhai|yaar|'
    r'kuch|ek|do|teen|mujhe\s+ek|mujhe\s+do|lao|la\s+do|mangwao)\s+',
    re.IGNORECASE,
)


def _extract_quantity(token: str) -> int:
    t = token.strip().lower()
    t_clean = re.sub(r'(st|nd|rd|th)$', '', t)
    if t_clean.isdigit():
        val = int(t_clean)
        if re.search(r'(st|nd|rd|th)$', t):
            return 1
        return val
    return QUANTITY_WORDS.get(t, QUANTITY_WORDS.get(t_clean, 1))


def _find_product_by_query(query: str) -> Optional[Dict]:
    """
    FIX H: Check auto-built keyword index first (O(1) for exact matches),
    then fall back to scoring for partial/fuzzy matches.
    """
    q = query.lower().strip()

    # Strip noise prefixes
    q_clean = _ORDER_NOISE_PREFIXES.sub("", q).strip()

    # 1. Exact index lookup
    if q_clean in PRODUCT_KEYWORD_INDEX:
        return PRODUCT_KEYWORD_INDEX[q_clean]
    if q in PRODUCT_KEYWORD_INDEX:
        return PRODUCT_KEYWORD_INDEX[q]

    # 2. Check each word in query against index
    words = re.findall(r'\w+', q_clean)
    for word in sorted(words, key=len, reverse=True):
        if len(word) > 2 and word in PRODUCT_KEYWORD_INDEX:
            return PRODUCT_KEYWORD_INDEX[word]

    # 3. Scoring fallback
    best_score, best_product = 0, None
    for product in PRODUCTS_DATA:
        title    = product.get("title", "").lower()
        category = product.get("category", "").lower()
        score    = 0

        if q_clean in title or title in q_clean:
            score += 10
        q_words = set(re.findall(r"\w+", q_clean))
        t_words = set(re.findall(r"\w+", title))
        score  += len(q_words & t_words) * 3

        for cat, kws in CATEGORY_KEYWORDS.items():
            if cat == category and any(kw in q_clean for kw in kws):
                score += 5

        for word in t_words:
            if word in q_clean and len(word) > 3:
                score += 2

        if score > best_score:
            best_score   = score
            best_product = product

    return best_product if best_score > 0 else None


def _products_by_category(category_key: str) -> List[Dict]:
    return [p for p in PRODUCTS_DATA if p.get("category", "").lower() == category_key.lower()]


def parse_price_range(query: str) -> Dict[str, float]:
    q = query.lower().replace("rs", "").replace("pkr", "").replace("$", "").replace("€", "")
    result: Dict[str, float] = {}
    under = re.search(r"(under|below|less than|کم|unter)\s*(\d+)", q)
    over  = re.search(r"(over|above|greater than|زیادہ|über)\s*(\d+)", q)
    if under:
        try: result["max"] = float(under.group(2))
        except: pass
    if over:
        try: result["min"] = float(over.group(2))
        except: pass
    return result


def score_product(query: str, product: Dict, price_range: Dict) -> float:
    q    = query.lower()
    text = " ".join(str(product.get(f, "")).lower() for f in ["title", "description", "category"])
    s    = len(set(re.findall(r"\w+", q)).intersection(set(re.findall(r"\w+", text)))) * 0.8
    s   += float(product.get("trending_score", 0)) * 1.5
    s   += float(product.get("rating", 0)) * 1.0
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(kw in q for kw in kws) and cat in text:
            s += 3.0
    try:
        variants = product.get("variants", [])
        pp = variants[0]["price"] if variants else float(str(product.get("price", "0")).replace(",", "").strip() or 0)
        if "max" in price_range and pp <= price_range["max"]: s += 2.0
        if "min" in price_range and pp >= price_range["min"]: s += 2.0
    except: pass
    return s


def filter_products(query: str) -> List[Dict]:
    price_range = parse_price_range(query)
    scored = [{"p": p, "s": score_product(query, p, price_range)} for p in PRODUCTS_DATA]
    return [x["p"] for x in sorted(scored, key=lambda x: x["s"], reverse=True) if x["s"] > 0.0][:8]

# ============================================================
# MULTI-ITEM PARSERS
# ============================================================

def _parse_multi_size_from_text(text: str, product: Dict) -> List[Dict]:
    variants     = product.get("variants", [])
    spice_levels = product.get("spice_levels", [])
    q            = text.lower()

    parts = re.split(
        r'\b(?:and|aur|or|also|\+|,|اور|پھر|;)\b',
        q, flags=re.IGNORECASE
    )

    results = []
    for part in parts:
        part = part.strip()
        if not part:
            continue

        qty = 1
        qty_match = re.match(
            r'^(\d+(?:st|nd|rd|th)?|' + '|'.join(re.escape(k) for k in QUANTITY_WORDS.keys()) + r')\s+',
            part, re.IGNORECASE
        )
        if qty_match:
            raw_qty = qty_match.group(1).lower()
            qty     = _extract_quantity(raw_qty)
            part    = part[qty_match.end():].strip()

        part = re.sub(r'^one\s+is\s+', '', part).strip()
        part = re.sub(r'^is\s+', '', part).strip()

        matched_variant    = None
        matched_size_label = ""
        for sh in sorted(SIZE_HINTS, key=len, reverse=True):
            if sh.lower() in part:
                mv = _match_variant(variants, sh)
                if mv:
                    matched_variant    = mv
                    matched_size_label = sh
                    break

        if not matched_variant:
            for v in variants:
                if v.get("size", "").lower() in part:
                    matched_variant    = v
                    matched_size_label = v["size"]
                    break

        if not matched_variant:
            continue

        found_spice = ""
        if spice_levels:
            for s in sorted(spice_levels, key=len, reverse=True):
                if s.lower() in part:
                    found_spice = s.strip().title()
                    break

        results.append({
            "qty":             qty,
            "size_hint":       matched_size_label,
            "matched_variant": matched_variant,
            "spice":           found_spice,
        })

    return results


# FIX J: Expanded separators to handle Urdu + mixed messages
_MULTI_ITEM_SEPARATORS = re.compile(
    r'\b(?:and|aur|or|also|پھر|اور|saath|ke\s+saath|plus)\b|[,;+\.\n]',
    re.IGNORECASE
)


def parse_multi_item_order(text: str) -> List[Dict]:
    parts   = _MULTI_ITEM_SEPARATORS.split(text)
    results = []

    for part in parts:
        part = part.strip()
        if not part or len(part) < 3:
            continue

        part = _ORDER_NOISE_PREFIXES.sub("", part).strip()
        if not part or len(part) < 2:
            continue

        qty = 1
        qty_match = re.match(
            r'^(\d+(?:st|nd|rd|th)?|' + '|'.join(re.escape(k) for k in QUANTITY_WORDS.keys()) + r')\s+',
            part, re.IGNORECASE
        )
        if qty_match:
            qty         = _extract_quantity(qty_match.group(1))
            part_no_qty = part[qty_match.end():].strip()
        else:
            part_no_qty = part

        size_hint   = ""
        part_clean  = part_no_qty
        for sh in sorted(SIZE_HINTS, key=len, reverse=True):
            if sh.lower() in part_clean.lower():
                size_hint  = sh
                part_clean = re.sub(re.escape(sh), "", part_clean, flags=re.IGNORECASE).strip()
                break

        size_match = re.match(
            r'^(\d+\.?\d*\s*kg|\d+\.?\d*\s*g\b|\d+\s*ml|\d+\.?\d*\s*l\b)',
            part_clean, re.IGNORECASE
        )
        if size_match and not size_hint:
            size_hint  = size_match.group(1).strip()
            part_clean = part_clean[size_match.end():].strip()

        product = _find_product_by_query(part_clean) or _find_product_by_query(part_no_qty) or _find_product_by_query(part)
        if product:
            results.append({
                "raw":       part,
                "qty":       qty,
                "size_hint": size_hint,
                "product":   product,
            })

    return results


def _group_parsed_by_product(parsed_items: List[Dict]) -> List[Dict]:
    groups: Dict[str, Dict] = {}
    order:  List[str]       = []

    for parsed in parsed_items:
        pid = str(parsed["product"].get("_id", ""))
        if pid not in groups:
            groups[pid] = {"product": parsed["product"], "items": []}
            order.append(pid)
        groups[pid]["items"].append({
            "qty":       parsed["qty"],
            "size_hint": parsed["size_hint"],
        })

    return [groups[pid] for pid in order]

# ============================================================
# CART & ORDER BUILDING
# ============================================================

def build_cart_item(product: Dict, size: str, spice: str, extras: List[str], quantity: int) -> Dict:
    size            = size.strip() if size else ""
    spice           = spice.strip().title() if spice else ""
    variants        = product.get("variants", [])
    matched_variant = _match_variant(variants, size) if size else (variants[0] if variants else None)

    if matched_variant:
        base_price = matched_variant["price"]
        final_size = matched_variant["size"]
    else:
        base_price = variants[0]["price"] if variants else float(str(product.get("price", 0)).replace(",", "") or 0)
        final_size = variants[0]["size"] if variants else size

    extras_options = product.get("extras", [])
    extras_clean   = [e.strip().title() for e in extras]
    extras_price   = sum(
        e["price"] for e in extras_options
        if e["name"].strip().title() in extras_clean or e["name"] in extras
    )

    return {
        "product_id":       str(product.get("_id", "")),
        "title":            product.get("title", "Item").strip().title(),
        "category":         product.get("category", "").strip().lower(),
        "size":             final_size,
        "quantity":         quantity,
        "spice":            spice,
        "extras":           extras_clean,
        "base_price":       base_price,
        "extras_price":     extras_price,
        "total_item_price": (base_price + extras_price) * quantity,
    }


def create_order_from_cart(
    user_id: str,
    cart_items: List[Dict],
    address: str,
    delivery_charge: float = 0.0,
) -> str:
    if orders_col is None:
        return "db_error"

    subtotal = sum(item["total_item_price"] for item in cart_items)
    total    = subtotal + delivery_charge

    order = {
        "user_id":         user_id,
        "items":           cart_items,
        "dish":            cart_items[0]["title"] if cart_items else "Order",
        "quantity":        sum(i["quantity"] for i in cart_items),
        "subtotal":        subtotal,
        "delivery_charge": delivery_charge,
        "total_price":     total,
        "address":         address.strip(),
        "status":          "Pending",
        "timestamp":       datetime.utcnow().isoformat(),
        "customization": {
            "size":   cart_items[0].get("size", "") if cart_items else "",
            "spice":  cart_items[0].get("spice", "") if cart_items else "",
            "extras": ", ".join(cart_items[0].get("extras", [])) if cart_items else "",
        },
    }
    result = orders_col.insert_one(order)

    inc = {"total_orders": 1}
    for item in cart_items:
        if item.get("size"):  inc[f"size_preference.{item['size']}"]   = inc.get(f"size_preference.{item['size']}", 0) + 1
        if item.get("spice"): inc[f"spice_preference.{item['spice']}"] = inc.get(f"spice_preference.{item['spice']}", 0) + 1
        for extra in item.get("extras", []):
            inc[f"extras_preference.{extra}"] = inc.get(f"extras_preference.{extra}", 0) + 1
    _track(inc)

    session = get_user_session(user_id)
    session["order_count"]    = session.get("order_count", 0) + 1
    session["last_order_items"] = cart_items  # FIX K: store for reorder

    return str(result.inserted_id)


def create_order_from_session(user_id: str, session: Dict, address: str, delivery_charge: float = 0.0) -> str:
    po    = session.get("pending_order", {})
    items = po.get("items", [])
    if not items:
        items = [{
            "product_id":       po.get("product_id", ""),
            "title":            po.get("dish", "Item").strip().title(),
            "size":             po.get("size", "").strip(),
            "spice":            po.get("spice", "").strip().title(),
            "extras":           [e.strip().title() for e in po.get("extras", [])],
            "quantity":         po.get("qty", 1),
            "base_price":       po.get("price", 0),
            "extras_price":     0,
            "total_item_price": po.get("price", 0),
        }]
    return create_order_from_cart(user_id, items, address, delivery_charge)

# ============================================================
# FAQ ENGINE
# ============================================================

def get_faq_response(query: str, lang: str) -> Optional[str]:
    faq = BOT_DATA.get("faq", {})
    q   = query.lower().strip()
    mapping = {
        "delivery": ["deliver", "ship", "ارسال", "versand", "kab ayega", "delivery time",
                     "kitne time", "kab milega"],
        "return":   ["return", "refund", "واپسی", "rückgabe", "exchange", "cancel",
                     "wapas", "change"],
        "track":    ["track", "order status", "ٹریک", "verfolgen", "kahan hai"],
        "quality":  ["quality", "fresh", "معیار", "qualität", "ingredients", "halal"],
        "hours":    ["open", "close", "hours", "timing", "اوقات", "öffnungszeiten",
                     "band", "khula", "kab khulta"],
        "payment":  ["pay", "payment", "cash", "card", "ادائیگی", "zahlung",
                     "online pay", "easypaisa", "jazzcash"],
    }
    for key, keywords in mapping.items():
        if any(kw in q for kw in keywords):
            entry = faq.get(key, {})
            if isinstance(entry, dict):
                return entry.get(lang, entry.get("en"))
            return entry or None
    return None

# ============================================================
# SMART SUGGESTIONS
# ============================================================

def get_suggestions(user_id: str, lang: str) -> List[str]:
    session = get_user_session(user_id)
    sugs    = BOT_DATA.get("smart_suggestions", {}).get("greeting", {}).get(lang, [])
    shown   = session.get("shown", [])
    avail   = [s for s in sugs if s not in shown] or sugs
    sel     = random.sample(avail, min(4, len(avail)))
    session["shown"] = list(set(shown + sel))
    return sel

# ============================================================
# ADDRESS HELPERS
# ============================================================

ADDRESS_PATTERN = re.compile(
    r"(\d+.{3,}(?:street|st|road|rd|avenue|ave|lane|block|sector|phase|house|flat|floor|"
    r"building|near|opposite|گلی|سڑک|گھر|مکان|بلاک|فیز).{5,})",
    re.IGNORECASE,
)


def extract_address(text: str) -> Optional[str]:
    m = ADDRESS_PATTERN.search(text)
    if m:
        return m.group(0).strip()
    text = text.strip()
    if len(text) >= 15:
        return text
    return None

# ============================================================
# WHATSAPP API HELPERS
# ============================================================

async def send_whatsapp_text(to: str, body: str):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logger.warning("WhatsApp credentials not configured — skipping send.")
        return
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(WHATSAPP_API_URL, json=payload, headers=headers)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"WhatsApp send failed: {e}")


async def send_whatsapp_list(to: str, header: str, items: List[Dict[str, Any]], lang: str = "en"):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        return
    rows = []
    for i, item in enumerate(items[:10]):
        variants  = item.get("variants", [])
        price_str = f"PKR {variants[0]['price']}" if variants else f"PKR {item.get('price', '')}"
        rows.append({
            "id":          f"item_{i}",
            "title":       item.get("title", "Item").strip().title()[:24],
            "description": f"{item.get('description', '').strip()[:50]} — {price_str}",
        })

    body_text   = {"en": "Tap an item to order or ask me anything! 🍽️",
                   "ur": "کوئی آئٹم چنیں یا کچھ بھی پوچھیں! 🍽️",
                   "de": "Tippen Sie auf ein Element oder fragen Sie mich! 🍽️"}
    footer_text = {"en": "Powered by AI Restaurant Bot v13",
                   "ur": "AI ریسٹورنٹ بوٹ v13",
                   "de": "Betrieben von AI Restaurant Bot v13"}
    button_text = {"en": "View Menu", "ur": "مینو دیکھیں", "de": "Menü anzeigen"}
    section_title = {"en": "Our Menu", "ur": "ہمارا مینو", "de": "Unsere Speisekarte"}

    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "interactive",
        "interactive": {
            "type":   "list",
            "header": {"type": "text", "text": header[:60]},
            "body":   {"text": body_text.get(lang, body_text["en"])},
            "footer": {"text": footer_text.get(lang, footer_text["en"])},
            "action": {
                "button":   button_text.get(lang, button_text["en"]),
                "sections": [{"title": section_title.get(lang, section_title["en"]), "rows": rows}],
            },
        },
    }
    headers_h = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(WHATSAPP_API_URL, json=payload, headers=headers_h)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"WhatsApp list send failed: {e}")


async def send_whatsapp_buttons(to: str, body: str, buttons: List[str]):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        return
    btn_list = [
        {"type": "reply", "reply": {"id": f"btn_{i}", "title": b[:20]}}
        for i, b in enumerate(buttons[:3])
    ]
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "interactive",
        "interactive": {
            "type":   "button",
            "body":   {"text": body[:1024]},
            "action": {"buttons": btn_list},
        },
    }
    headers_h = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(WHATSAPP_API_URL, json=payload, headers=headers_h)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"WhatsApp buttons send failed: {e}")

# ============================================================
# INTENT DETECTION HELPERS
# ============================================================

def _detect_price_menu_intent(q: str) -> bool:
    price_phrases = [
        "all prices", "all flavours", "all flavors", "all pizza", "all burger",
        "all karahi", "price list", "show all", "menu prices", "full menu",
        "all items price", "all rates", "complete menu",
    ]
    return any(ph in q for ph in price_phrases) or (
        any(kw in q for kw in INTENT_KEYWORDS["price"]) and
        any(kw in q for kw in INTENT_KEYWORDS["menu"])
    )


def _detect_category_from_query(q: str) -> Optional[str]:
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return cat
    return None


def _is_affirmative(q: str) -> bool:
    """FIX I: Detect affirmative responses that mean 'yes, proceed'."""
    affirmatives = {
        "yes", "sure", "ok", "okay", "haan", "theek hai", "bilkul",
        "zaroor", "absolutely", "go ahead", "proceed", "yep", "yeah",
        "finalize", "done", "correct", "right", "ji", "ji haan",
    }
    return q.strip().lower() in affirmatives


def _is_order_now_button(q: str) -> bool:
    """FIX G: Detect the Order Now / Order Again button taps."""
    cleaned = re.sub(r'[✅📋🛒🔄]', '', q).strip().lower()
    return cleaned in {"order now", "order again", "reorder"}

# ============================================================
# SMART FALLBACK (Claude AI)
# ============================================================

async def _smart_fallback(from_number: str, user_message: str, lang: str) -> str:
    if not ANTHROPIC_API_KEY:
        return _static_fallback(lang)

    product_names = [p.get("title", "") for p in PRODUCTS_DATA[:20]]
    product_list  = ", ".join(product_names) if product_names else "various dishes"

    system_prompt = (
        f"You are a friendly, warm WhatsApp restaurant assistant. "
        f"The restaurant serves: {product_list}. "
        f"Respond in {'Urdu' if lang == 'ur' else 'German' if lang == 'de' else 'English'}, "
        f"keeping replies under 3 sentences. "
        f"Be conversational, warm, and helpful like a real human restaurant staff. "
        f"If asked about a dish, describe it enthusiastically and suggest ordering. "
        f"If the question is completely unrelated to food or restaurants, "
        f"politely say you can only help with restaurant-related queries."
    )

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version":  "2023-06-01",
                    "content-type":       "application/json",
                },
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "system":     system_prompt,
                    "messages":   [{"role": "user", "content": user_message}],
                },
            )
            data    = resp.json()
            ai_text = data.get("content", [{}])[0].get("text", "").strip()
            if ai_text:
                return ai_text
    except Exception as e:
        logger.warning(f"AI fallback failed: {e}")

    return _static_fallback(lang)


def _static_fallback(lang: str) -> str:
    fallback = {
        "en": (
            "I didn't quite get that 🤔 Here's what I can help with:\n\n"
            "• *Show menu* — see all our items\n"
            "• *Order [dish name]* — place an order\n"
            "• *All prices* — full price list\n"
            "• *Order status* — track your order\n\n"
            "Just type the dish name to start ordering! 😊"
        ),
        "ur": (
            "مجھے سمجھ نہیں آیا 🤔 میں ان چیزوں میں مدد کر سکتا ہوں:\n\n"
            "• *مینو دکھائیں* — سب آئٹم دیکھیں\n"
            "• *[ڈش کا نام] آرڈر* — آرڈر دیں\n"
            "• *تمام قیمتیں* — قیمت کی فہرست\n"
            "• *آرڈر اسٹیٹس* — ٹریکنگ\n\n"
            "ڈش کا نام لکھیں آرڈر شروع کریں! 😊"
        ),
        "de": (
            "Das habe ich nicht verstanden 🤔 Ich helfe gerne mit:\n\n"
            "• *Menü anzeigen* — alle Artikel\n"
            "• *[Gericht] bestellen* — Bestellung aufgeben\n"
            "• *Alle Preise* — komplette Preisliste\n"
            "• *Bestellstatus* — verfolgen\n\n"
            "Einfach den Gerichtnamen eingeben! 😊"
        ),
    }
    return fallback.get(lang, fallback["en"])

# ============================================================
# BOT FLOW HELPERS
# ============================================================

async def _ask_size(to: str, product: Dict, lang: str):
    variants  = product.get("variants", [])
    size_list = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants)
    name      = product.get("title", "Item").strip().title()
    msgs = {
        "en": f"📏 Which size would you like for *{name}*?\n\n{size_list}",
        "ur": f"📏 *{name}* کا سائز بتائیں:\n\n{size_list}",
        "de": f"📏 Welche Größe für *{name}*?\n\n{size_list}",
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))


async def _ask_spice(to: str, product: Dict, lang: str) -> bool:
    spice_levels = product.get("spice_levels", [])
    if not spice_levels:
        return False
    options = " / ".join(s.strip().title() for s in spice_levels)
    name    = product.get("title", "Item").strip().title()
    msgs = {
        "en": f"🌶️ How spicy for *{name}*?\n  {options}",
        "ur": f"🌶️ *{name}* کے لیے مسالے کی سطح:\n  {options}",
        "de": f"🌶️ Schärfegrad für *{name}*:\n  {options}",
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _ask_extras(to: str, product: Dict, lang: str) -> bool:
    extras = product.get("extras", [])
    if not extras:
        return False
    extras_list = "\n".join(f"  • {e['name'].strip().title()} +PKR {e['price']}" for e in extras)
    name        = product.get("title", "Item").strip().title()
    msgs = {
        "en": f"➕ Want any extras with *{name}*?\n{extras_list}\n\n(Type names or 'no')",
        "ur": f"➕ *{name}* کے ساتھ کچھ اضافی؟\n{extras_list}\n\n(نام لکھیں یا 'no')",
        "de": f"➕ Extras für *{name}*?\n{extras_list}\n\n(Namen oder 'nein')",
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _ask_multi_spice(to: str, items_needing_spice: List[Dict], product: Dict, lang: str):
    spice_levels = product.get("spice_levels", [])
    options      = " / ".join(s.strip().title() for s in spice_levels)
    name         = product.get("title", "Item").strip().title()

    lines = []
    for item in items_needing_spice:
        mv   = item["matched_variant"]
        size = mv.get("size", "")
        qty  = item.get("qty", 1)
        qty_label = f" ×{qty}" if qty > 1 else ""
        lines.append(f"  • *{size}*{qty_label} — {options}")

    body = "\n".join(lines)
    msgs = {
        "en": f"🌶️ Spice level for *{name}*:\n{body}\n\n(e.g. 'Half Plate Spicy and Family Pack Extra Spicy')",
        "ur": f"🌶️ *{name}* کے لیے مسالے کی سطح:\n{body}\n\n(جیسے: 'Half Plate Spicy and Family Pack Extra Spicy')",
        "de": f"🌶️ Schärfegrad für *{name}*:\n{body}\n\n(z.B. 'Half Plate Spicy and Family Pack Extra Spicy')",
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))


async def _advance_product_queue(from_num: str, session: Dict, lang: str):
    pq = session.get("product_queue", [])
    if not pq:
        session["step"] = 5
        cart    = session.get("cart", [])
        total   = _recalc_cart(cart)
        summary = _build_cart_summary(cart, total, lang)
        confirm_msgs = {
            "en": f"{summary}\n\n👉 Ready to confirm or want to add more?",
            "ur": f"{summary}\n\n👉 آرڈر تصدیق کریں یا مزید شامل کریں؟",
            "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
        }
        await send_whatsapp_buttons(
            from_num,
            confirm_msgs.get(lang, confirm_msgs["en"]),
            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
        )
        return

    next_group   = pq[0]
    product      = next_group["product"]
    items        = next_group["items"]
    variants     = product.get("variants", [])
    spice_levels = product.get("spice_levels", [])

    cart_items_ready    = list(session.get("cart", []))
    items_needing_spice = []
    items_need_size     = []

    for it in items:
        qty       = it["qty"]
        size_hint = it["size_hint"]
        mv        = _match_variant(variants, size_hint) if size_hint else (variants[0] if variants else None)

        if variants and not mv:
            items_need_size.append(it)
            continue

        if spice_levels:
            items_needing_spice.append({
                "qty":             qty,
                "size_hint":       size_hint,
                "matched_variant": mv,
                "spice":           "",
            })
        else:
            size = mv["size"] if mv else size_hint
            ci   = build_cart_item(product, size, "", [], qty)
            cart_items_ready.append(ci)

    if items_need_size:
        session["cart"]               = cart_items_ready
        session["missing_info_queue"] = [{"type": "size", "product": product, "qty": it["qty"]} for it in items_need_size]
        session["step"] = 10
        await _ask_size(from_num, product, lang)
        return

    if items_needing_spice:
        session["cart"]             = cart_items_ready
        session["multi_size_queue"] = items_needing_spice
        session["pending_order"]["product_ref"] = product
        session["step"] = 20
        await _ask_multi_spice(from_num, items_needing_spice, product, lang)
        return

    session["cart"] = cart_items_ready
    extras_options  = product.get("extras", [])
    if extras_options:
        session["pending_order"]["product_ref"] = product
        session["step"] = 30
        pq.pop(0)
        session["product_queue"] = pq
        await _ask_extras(from_num, product, lang)
        return

    pq.pop(0)
    session["product_queue"] = pq
    await _advance_product_queue(from_num, session, lang)


async def _finalise_single_item(
    from_num: str,
    session: Dict,
    cart_item: Dict,
    lang: str,
):
    session["cart"].append(cart_item)
    pq = session.get("product_queue", [])

    if pq:
        await _advance_product_queue(from_num, session, lang)
        return

    if len(session["cart"]) > 1:
        session["step"] = 5
        total   = _recalc_cart(session["cart"])
        summary = _build_cart_summary(session["cart"], total, lang)
        confirm_msgs = {
            "en": f"{summary}\n\n👉 Ready to confirm or want to add more?",
            "ur": f"{summary}\n\n👉 آرڈر تصدیق کریں یا مزید شامل کریں؟",
            "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
        }
        await send_whatsapp_buttons(
            from_num,
            confirm_msgs.get(lang, confirm_msgs["en"]),
            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
        )
    else:
        session["step"] = 4
        ask_addr = {
            "en": "📍 Perfect! Now share your full delivery address (house no., street, area, city):",
            "ur": "📍 بہترین! اب اپنا مکمل پتہ دیں (مکان نمبر، گلی، علاقہ، شہر):",
            "de": "📍 Super! Bitte vollständige Lieferadresse angeben:",
        }
        await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))


async def _handle_single_item_order(from_number: str, text: str, lang: str) -> bool:
    session  = get_user_session(from_number)
    products = filter_products(text)
    if not products:
        return False

    p              = products[0]
    variants       = p.get("variants", [])
    spice_levels   = p.get("spice_levels", [])
    extras_options = p.get("extras", [])
    base_price     = variants[0]["price"] if variants else float(str(p.get("price", 0)).replace(",", "") or 0)
    dish_name      = p.get("title", "Item").strip().title()

    session["pending_order"] = {
        "product_id":     p.get("_id", ""),
        "dish":           dish_name,
        "price":          base_price,
        "qty":            1,
        "variants":       variants,
        "spice_levels":   spice_levels,
        "extras_options": extras_options,
        "size":           "",
        "spice":          "",
        "extras":         [],
        "product_ref":    p,
    }

    if variants:
        pre_parsed = _parse_multi_size_from_text(text, p)
        if len(pre_parsed) >= 2:
            cart_items          = list(session.get("cart", []))
            items_needing_spice = []
            for parsed in pre_parsed:
                mv    = parsed["matched_variant"]
                spice = parsed["spice"]
                if p.get("spice_levels") and not spice:
                    items_needing_spice.append(parsed)
                else:
                    ci = build_cart_item(p, mv["size"], spice, [], parsed["qty"])
                    cart_items.append(ci)

            if items_needing_spice:
                session["cart"]             = cart_items
                session["multi_size_queue"] = items_needing_spice
                session["pending_order"]["product_ref"] = p
                session["step"] = 20
                await _ask_multi_spice(from_number, items_needing_spice, p, lang)
            else:
                session["cart"] = cart_items
                total   = _recalc_cart(session["cart"])
                summary = _build_cart_summary(session["cart"], total, lang)
                confirm_msgs = {
                    "en": f"{summary}\n\n👉 Ready to confirm or want to add more?",
                    "ur": f"{summary}\n\n👉 آرڈر تصدیق کریں یا مزید شامل کریں؟",
                    "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                }
                await send_whatsapp_buttons(from_number, confirm_msgs.get(lang, confirm_msgs["en"]),
                                            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
                session["step"] = 5
            return True

        session["step"] = 1
        await _ask_size(from_number, p, lang)
    else:
        name_msg = {
            "en": f"🎉 *{dish_name}* — PKR {int(base_price)} added to your order!",
            "ur": f"🎉 *{dish_name}* — PKR {int(base_price)} آپ کے آرڈر میں شامل!",
            "de": f"🎉 *{dish_name}* — PKR {int(base_price)} hinzugefügt!",
        }
        await send_whatsapp_text(from_number, name_msg.get(lang, name_msg["en"]))
        cart_item = build_cart_item(p, "", "", [], 1)
        await _finalise_single_item(from_number, session, cart_item, lang)
    return True


async def _handle_full_price_display(from_number: str, q: str, lang: str):
    category = _detect_category_from_query(q)
    if category:
        products = _products_by_category(category) or filter_products(q)
        cat_name = category.capitalize()
        emoji_map = {
            "pizza": "🍕", "burger": "🍔", "biryani": "🍛",
            "drinks": "🥤", "karahi": "🥘", "dessert": "🍰",
            "rice": "🍚", "rolls": "🌯",
        }
        emoji = emoji_map.get(category, "🍽️")
        title_map = {
            "en": f"{cat_name} Menu & Prices",
            "ur": f"{cat_name} مینو اور قیمتیں",
            "de": f"{cat_name} Menü & Preise",
        }
        title = title_map.get(lang, title_map["en"])
    else:
        products = PRODUCTS_DATA[:15]
        emoji    = "🍽️"
        title_map = {
            "en": "Full Menu & Prices",
            "ur": "مکمل مینو اور قیمتیں",
            "de": "Vollständiges Menü & Preise",
        }
        title = title_map.get(lang, title_map["en"])

    if not products:
        no_prod = {
            "en": "No products found at the moment. Please try again! 🙏",
            "ur": "ابھی کوئی آئٹم نہیں ملا۔ دوبارہ کوشش کریں! 🙏",
            "de": "Keine Produkte gefunden. Bitte erneut versuchen! 🙏",
        }
        await send_whatsapp_text(from_number, no_prod.get(lang, no_prod["en"]))
        return

    menu_text = _build_full_price_menu(products, emoji, title)
    await send_whatsapp_text(from_number, menu_text)


async def handle_multi_item_order(from_number: str, text: str, lang: str) -> bool:
    session      = get_user_session(from_number)
    parsed_items = parse_multi_item_order(text)

    if not parsed_items:
        return False

    groups = _group_parsed_by_product(parsed_items)

    if not groups:
        return False

    if len(groups) == 1:
        group   = groups[0]
        product = group["product"]
        items   = group["items"]

        if len(items) >= 2:
            multi_sizes = _parse_multi_size_from_text(text, product)
            if len(multi_sizes) >= 2:
                cart_items          = list(session.get("cart", []))
                items_needing_spice = []
                spice_levels        = product.get("spice_levels", [])

                for parsed in multi_sizes:
                    mv    = parsed["matched_variant"]
                    spice = parsed["spice"]
                    if spice_levels and not spice:
                        items_needing_spice.append(parsed)
                    else:
                        ci = build_cart_item(product, mv["size"], spice, [], parsed["qty"])
                        cart_items.append(ci)

                if items_needing_spice:
                    session["cart"]             = cart_items
                    session["multi_size_queue"] = items_needing_spice
                    session["pending_order"]["product_ref"] = product
                    session["step"] = 20
                    await _ask_multi_spice(from_number, items_needing_spice, product, lang)
                    return True

                if cart_items:
                    session["cart"] = cart_items
                    total   = _recalc_cart(cart_items)
                    summary = _build_cart_summary(cart_items, total, lang)
                    confirm_msgs = {
                        "en": f"{summary}\n\n👉 Ready to confirm or want to add more?",
                        "ur": f"{summary}\n\n👉 آرڈر تصدیق کریں یا مزید شامل کریں؟",
                        "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                    }
                    await send_whatsapp_buttons(from_number, confirm_msgs.get(lang, confirm_msgs["en"]),
                                                ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
                    session["step"] = 5
                    return True

    cart_items_direct = list(session.get("cart", []))
    pending_queue     = []

    for group in groups:
        product  = group["product"]
        items    = group["items"]
        variants = product.get("variants", [])
        spice_levels = product.get("spice_levels", [])

        items_needing_spice = []
        items_need_size     = []

        for it in items:
            qty       = it["qty"]
            size_hint = it["size_hint"]
            mv        = _match_variant(variants, size_hint) if size_hint else (variants[0] if variants else None)

            if variants and not mv:
                items_need_size.append(it)
                continue

            if spice_levels:
                items_needing_spice.append({
                    "qty":             qty,
                    "size_hint":       size_hint,
                    "matched_variant": mv,
                    "spice":           "",
                })
            else:
                size = mv["size"] if mv else size_hint
                ci   = build_cart_item(product, size, "", [], qty)
                cart_items_direct.append(ci)

        if items_need_size or items_needing_spice:
            pending_queue.append(group)

    session["cart"]          = cart_items_direct
    session["product_queue"] = pending_queue

    if pending_queue:
        session["pending_order"] = {}
        await _advance_product_queue(from_number, session, lang)
        return True

    if cart_items_direct:
        session["step"] = 5
        total   = _recalc_cart(cart_items_direct)
        summary = _build_cart_summary(cart_items_direct, total, lang)
        confirm_msgs = {
            "en": f"{summary}\n\n👉 Ready to confirm or want to add more?",
            "ur": f"{summary}\n\n👉 آرڈر تصدیق کریں یا مزید شامل کریں؟",
            "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
        }
        await send_whatsapp_buttons(
            from_number,
            confirm_msgs.get(lang, confirm_msgs["en"]),
            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"]
        )
        return True

    return False

# ============================================================
# MAIN WEBHOOK
# ============================================================

@app.get("/webhook")
async def verify_webhook(
    hub_mode: str          = None,
    hub_verify_token: str  = None,
    hub_challenge: str     = None,
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    if hub_mode is None:
        return PlainTextResponse("Webhook active", status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_message(request: Request):
    try:
        data     = await request.json()
        entry    = data.get("entry", [{}])[0]
        changes  = entry.get("changes", [{}])[0]
        value    = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return JSONResponse({"status": "ok"})

        msg      = messages[0]
        from_num = msg.get("from", "")
        msg_type = msg.get("type", "text")

        if _is_rate_limited(from_num):
            logger.warning(f"Rate limited: {from_num}")
            return JSONResponse({"status": "rate_limited"})

        session      = get_user_session(from_num)
        session_lang = session.get("lang", "en")
        is_button    = False

        if msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                msg_text  = interactive["button_reply"].get("title", "").strip()
                is_button = True
            elif interactive.get("type") == "list_reply":
                msg_text  = interactive["list_reply"].get("title", "").strip()
                is_button = True
            else:
                msg_text = ""
        else:
            msg_text = msg.get("text", {}).get("body", "").strip()

        if not msg_text:
            return JSONResponse({"status": "ok"})

        if is_button:
            lang = session_lang
        else:
            lang = detect_language(msg_text, session_lang)

        if not is_button:
            session["lang"] = lang

        q    = msg_text.lower().strip()
        # Remove emoji/special chars for cleaner matching
        q_clean = re.sub(r'[^\w\s\u0600-\u06FF]', '', q).strip()
        step = session.get("step", 0)

        _track({"total_searches": 1, f"supported_languages.{lang}": 1})

        # ═══════════════════════════════════════════════════════
        # PRIORITY 0 — "new order" → reset cart immediately
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["new_order"]):
            reset_for_new_order(session)
            last_addr = session.get("last_address")
            addr_hint_map = {
                "en": f"\n📍 Last address: _{last_addr}_\n(type 'same' to reuse)" if last_addr else "",
                "ur": f"\n📍 پرانا پتہ: _{last_addr}_\n('same' لکھیں دوبارہ استعمال کے لیے)" if last_addr else "",
                "de": f"\n📍 Letzte Adresse: _{last_addr}_\n('same' eingeben zum Wiederverwenden)" if last_addr else "",
            }
            addr_hint = addr_hint_map.get(lang, addr_hint_map["en"])
            new_order_msg = {
                "en": f"🆕 Starting fresh! What would you like today? 🍽️{addr_hint}",
                "ur": f"🆕 نیا آرڈر شروع! کیا آرڈر کرنا ہے؟ 🍽️{addr_hint}",
                "de": f"🆕 Neue Bestellung! Was möchten Sie heute? 🍽️{addr_hint}",
            }
            await send_whatsapp_buttons(
                from_num,
                new_order_msg.get(lang, new_order_msg["en"]),
                ["View Menu 📋", "Order Again 🔄", "Contact Us 📞"]
            )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # FIX G: "Order Now" / "Order Again" button handler
        # ═══════════════════════════════════════════════════════
        if _is_order_now_button(q):
            last_product = session.get("last_shown_product")
            last_items   = session.get("last_order_items", [])

            # "Order Again" — reorder previous items
            if "again" in q or "reorder" in q:
                if last_items:
                    session["cart"] = list(last_items)
                    session["step"] = 5
                    total   = _recalc_cart(last_items)
                    summary = _build_cart_summary(last_items, total, lang)
                    reorder_msg = {
                        "en": f"🔄 Here's your previous order:\n\n{summary}\n\n👉 Confirm or modify?",
                        "ur": f"🔄 آپ کا پرانا آرڈر:\n\n{summary}\n\n👉 تصدیق کریں یا تبدیل کریں؟",
                        "de": f"🔄 Ihre letzte Bestellung:\n\n{summary}\n\n👉 Bestätigen oder ändern?",
                    }
                    await send_whatsapp_buttons(from_num, reorder_msg.get(lang, reorder_msg["en"]),
                                                ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
                    return JSONResponse({"status": "ok"})

            # "Order Now" — resume the last shown product
            if last_product:
                reset_for_new_order(session)
                handled = await _handle_single_item_order(from_num, last_product.get("title", ""), lang)
                if handled:
                    return JSONResponse({"status": "ok"})

            # Fallback — ask what they want
            ask_what = {
                "en": "Sure! What would you like to order? 🍽️\n(Type any dish name or 'show menu')",
                "ur": "ضرور! کیا آرڈر کرنا ہے؟ 🍽️\n(ڈش کا نام لکھیں یا 'مینو دکھائیں')",
                "de": "Natürlich! Was möchten Sie bestellen? 🍽️\n(Gerichtnamen eingeben oder 'Menü anzeigen')",
            }
            await send_whatsapp_text(from_num, ask_what.get(lang, ask_what["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # PRIORITY 1 — Cancel / Delete order
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["cancel"]):
            cart = session.get("cart", [])
            po   = session.get("pending_order", {})
            if cart or po:
                reset_cart_only(session)
                cancel_msg = {
                    "en": "🗑️ Order cancelled and cart cleared. What else can I get you? 🍽️",
                    "ur": "🗑️ آرڈر منسوخ! ٹوکری صاف ہوگئی۔ کیا آرڈر کرنا ہے؟ 🍽️",
                    "de": "🗑️ Bestellung storniert! Warenkorb geleert. Was möchten Sie bestellen?",
                }
                await send_whatsapp_buttons(
                    from_num,
                    cancel_msg.get(lang, cancel_msg["en"]),
                    ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"]
                )
            else:
                no_order_msg = {
                    "en": "ℹ️ No active order to cancel. Ready to start a new one? 🍽️",
                    "ur": "ℹ️ کوئی فعال آرڈر نہیں۔ نیا آرڈر دیں؟ 🍽️",
                    "de": "ℹ️ Keine aktive Bestellung. Neue aufgeben? 🍽️",
                }
                await send_whatsapp_buttons(
                    from_num,
                    no_order_msg.get(lang, no_order_msg["en"]),
                    ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"]
                )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # FIX I: Thanks / chit-chat mid-flow
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["thanks"]) and step == 0:
            thanks_msg = {
                "en": "You're welcome! 😊 Anything else I can help with?\n\n• *Show menu* — view all items\n• *Place order* — order food",
                "ur": "خوشی ہوئی! 😊 اور کچھ چاہیے؟\n\n• *مینو دکھائیں* — سب آئٹم\n• *آرڈر دیں* — کھانا آرڈر کریں",
                "de": "Gern geschehen! 😊 Kann ich noch helfen?\n\n• *Menü anzeigen* — alle Artikel\n• *Bestellen* — Essen bestellen",
            }
            await send_whatsapp_text(from_num, thanks_msg.get(lang, thanks_msg["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 1 — User picks SIZE (single item flow)
        # ═══════════════════════════════════════════════════════
        if step == 1:
            po       = session.get("pending_order", {})
            variants = po.get("variants", [])
            product  = po.get("product_ref", {})

            if product:
                multi_sizes = _parse_multi_size_from_text(msg_text, product)
                if len(multi_sizes) >= 2:
                    cart_items          = list(session.get("cart", []))
                    items_needing_spice = []
                    spice_levels        = product.get("spice_levels", [])

                    for parsed in multi_sizes:
                        mv    = parsed["matched_variant"]
                        spice = parsed["spice"]
                        if spice_levels and not spice:
                            items_needing_spice.append(parsed)
                        else:
                            ci = build_cart_item(product, mv["size"], spice, [], parsed["qty"])
                            cart_items.append(ci)

                    if items_needing_spice:
                        session["cart"]             = cart_items
                        session["multi_size_queue"] = items_needing_spice
                        session["step"] = 20
                        await _ask_multi_spice(from_num, items_needing_spice, product, lang)
                    else:
                        extras_options = product.get("extras", [])
                        session["cart"] = cart_items
                        if extras_options:
                            session["pending_order"]["product_ref"] = product
                            session["step"] = 30
                            await _ask_extras(from_num, product, lang)
                        else:
                            session["step"] = 5
                            total   = _recalc_cart(cart_items)
                            summary = _build_cart_summary(cart_items, total, lang)
                            confirm_msgs = {
                                "en": f"{summary}\n\n👉 Confirm or add more?",
                                "ur": f"{summary}\n\n👉 تصدیق کریں یا مزید شامل کریں؟",
                                "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                            }
                            await send_whatsapp_buttons(from_num, confirm_msgs.get(lang, confirm_msgs["en"]),
                                                        ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
                    return JSONResponse({"status": "ok"})

            matched = _match_variant(variants, msg_text)
            if not matched and variants:
                sizes_str = " / ".join(v["size"] for v in variants)
                size_err = {
                    "en": f"⚠️ Please choose one of these sizes: *{sizes_str}*",
                    "ur": f"⚠️ براہ کرم یہ سائز چنیں: *{sizes_str}*",
                    "de": f"⚠️ Bitte eine dieser Größen wählen: *{sizes_str}*",
                }
                await send_whatsapp_text(from_num, size_err.get(lang, size_err["en"]))
                return JSONResponse({"status": "ok"})

            po["size"]  = matched["size"]
            po["price"] = matched["price"]
            update_preferences(from_num, size=matched["size"])
            _track({f"size_preference.{matched['size']}": 1})

            spice_levels = po.get("spice_levels", [])
            if spice_levels:
                session["step"] = 2
                product_ref     = po.get("product_ref", {"title": po.get("dish", ""), "spice_levels": spice_levels})
                await _ask_spice(from_num, product_ref, lang)
            else:
                po["spice"]     = ""
                product_ref     = po.get("product_ref", {"title": po.get("dish", ""), "extras": po.get("extras_options", [])})
                has_extras      = await _ask_extras(from_num, product_ref, lang)
                session["step"] = 3 if has_extras else 4
                if not has_extras:
                    if session.get("cart"):
                        cart_item = build_cart_item(
                            po.get("product_ref", {}),
                            po.get("size", ""), po.get("spice", ""),
                            po.get("extras", []), po.get("qty", 1)
                        )
                        await _finalise_single_item(from_num, session, cart_item, lang)
                    else:
                        ask_addr = {
                            "en": "📍 Perfect! Share your full delivery address:",
                            "ur": "📍 بہترین! اپنا مکمل پتہ دیں:",
                            "de": "📍 Super! Lieferadresse angeben:",
                        }
                        await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 2 — User picks SPICE
        # ═══════════════════════════════════════════════════════
        if step == 2:
            po            = session.get("pending_order", {})
            spice_levels  = po.get("spice_levels", [])
            matched_spice = next(
                (s for s in sorted(spice_levels, key=len, reverse=True)
                 if s.lower().strip() in q),
                spice_levels[0] if spice_levels else ""
            )
            po["spice"] = matched_spice.strip().title()
            update_preferences(from_num, spice=po["spice"])
            _track({f"spice_preference.{po['spice']}": 1})

            product_ref    = po.get("product_ref", {"title": po.get("dish", ""), "extras": po.get("extras_options", [])})
            has_extras     = await _ask_extras(from_num, product_ref, lang)
            session["step"] = 3 if has_extras else 4
            if not has_extras:
                if session.get("cart"):
                    cart_item = build_cart_item(
                        po.get("product_ref", {}),
                        po.get("size", ""), po.get("spice", ""),
                        po.get("extras", []), po.get("qty", 1)
                    )
                    await _finalise_single_item(from_num, session, cart_item, lang)
                else:
                    ask_addr = {
                        "en": "📍 Share your full delivery address:",
                        "ur": "📍 اپنا مکمل پتہ دیں:",
                        "de": "📍 Lieferadresse angeben:",
                    }
                    await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 3 — User picks EXTRAS
        # ═══════════════════════════════════════════════════════
        if step == 3:
            po             = session.get("pending_order", {})
            extras_options = po.get("extras_options", [])

            if any(kw in q for kw in INTENT_KEYWORDS["show_total"]):
                current_total = po.get("price", 0)
                total_msg = {
                    "en": f"💰 Current total: *PKR {int(current_total)}*\n\nAdd extras for *{po.get('dish','')}*? (type names or 'no')",
                    "ur": f"💰 ابھی تک کل: *PKR {int(current_total)}*\n\n*{po.get('dish','')}* کے ساتھ اضافی؟",
                    "de": f"💰 Bisheriger Betrag: *PKR {int(current_total)}*\n\nExtras für *{po.get('dish','')}*?",
                }
                await send_whatsapp_text(from_num, total_msg.get(lang, total_msg["en"]))
                return JSONResponse({"status": "ok"})

            chosen = []
            if not any(skip in q for skip in ["no", "skip", "nothing", "nahi", "nope", "nein", "nahin"]):
                chosen = _extract_extras_from_text(msg_text, extras_options)
                for e_name in chosen:
                    _track({f"extras_preference.{e_name}": 1})

            extras_price    = sum(e["price"] for e in extras_options if e["name"].strip().title() in chosen)
            po["extras"]    = chosen
            po["price"]     = po.get("price", 0) + extras_price

            cart_item = build_cart_item(
                po.get("product_ref", {}),
                po.get("size", ""), po.get("spice", ""),
                chosen, po.get("qty", 1)
            )

            await _finalise_single_item(from_num, session, cart_item, lang)
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 4 — ADDRESS (single item)
        # ═══════════════════════════════════════════════════════
        if step == 4:
            po = session.get("pending_order", {})

            if q.strip() in ["same", "same address", "same adress", "same add"]:
                address = session.get("last_address")
                if not address:
                    no_addr = {
                        "en": "⚠️ No previous address found. Please type your full address.",
                        "ur": "⚠️ پرانا پتہ نہیں ملا۔ اپنا مکمل پتہ لکھیں۔",
                        "de": "⚠️ Keine frühere Adresse. Bitte vollständige Adresse eingeben.",
                    }
                    await send_whatsapp_text(from_num, no_addr.get(lang, no_addr["en"]))
                    return JSONResponse({"status": "ok"})
            else:
                address_candidate = extract_address(msg_text) or msg_text.strip()
                if not _is_valid_address(address_candidate):
                    retry_addr = {
                        "en": "📍 Please share your *full* delivery address.\nExample: *House 12, Block B, Gulshan, Karachi*",
                        "ur": "📍 اپنا *مکمل* پتہ لکھیں۔\nمثال: *مکان 12، بلاک بی، گلشن، کراچی*",
                        "de": "📍 Bitte *vollständige* Lieferadresse eingeben.\nBeispiel: *Haus 12, Block B, Gulshan, Karachi*",
                    }
                    await send_whatsapp_text(from_num, retry_addr.get(lang, retry_addr["en"]))
                    return JSONResponse({"status": "ok"})
                address = address_candidate

            if not session.get("cart"):
                cart_item = build_cart_item(
                    po.get("product_ref", {}),
                    po.get("size", ""), po.get("spice", ""),
                    po.get("extras", []), po.get("qty", 1)
                )
                session["cart"] = [cart_item]

            subtotal        = _recalc_cart(session["cart"])
            delivery_charge = calculate_delivery_charge(subtotal, address)
            grand_total     = subtotal + delivery_charge
            order_id        = create_order_from_cart(from_num, session["cart"], address, delivery_charge)

            session["last_address"] = address
            update_preferences(from_num, product_title=po.get("dish", ""))
            reset_cart_only(session)

            if order_id == "db_error":
                db_err = {
                    "en": "⚠️ Sorry, there was an issue placing your order. Please try again.",
                    "ur": "⚠️ معذرت، آرڈر دینے میں مسئلہ ہوا۔ دوبارہ کوشش کریں۔",
                    "de": "⚠️ Entschuldigung, Fehler bei der Bestellung. Bitte erneut versuchen.",
                }
                await send_whatsapp_text(from_num, db_err.get(lang, db_err["en"]))
                return JSONResponse({"status": "ok"})

            extras_text = ", ".join(po.get("extras", [])) or {
                "en": "None", "ur": "کچھ نہیں", "de": "Keine"
            }.get(lang, "None")

            item_category = po.get("category", "")
            delivery_time = get_delivery_time(item_category)
            dc_line       = _delivery_charge_info_text(delivery_charge, lang)

            conf = {
                "en": (
                    f"✅ *Order Confirmed!* 🎉\n\n"
                    f"🍽️ *{po.get('dish', 'Item')}*\n"
                    f"📏 Size: {po.get('size', 'N/A')}\n"
                    f"🌶️ Spice: {po.get('spice', '') or 'Default'}\n"
                    f"➕ Extras: {extras_text}\n"
                    f"💰 Subtotal: PKR {int(subtotal)}\n"
                    f"{dc_line}\n"
                    f"💳 Grand Total: PKR {int(grand_total)}\n"
                    f"📍 Address: {address}\n"
                    f"🔖 Order ID: #{order_id[-6:]}\n\n"
                    f"⏱️ Estimated delivery: {delivery_time}\n"
                    f"📲 Type *new order* anytime to order again!"
                ),
                "ur": (
                    f"✅ *آرڈر تصدیق ہوگیا!* 🎉\n\n"
                    f"🍽️ *{po.get('dish', 'Item')}*\n"
                    f"📏 سائز: {po.get('size', '') or 'N/A'}\n"
                    f"🌶️ مسالہ: {po.get('spice', '') or 'ڈیفالٹ'}\n"
                    f"➕ اضافی: {extras_text}\n"
                    f"💰 سب ٹوٹل: PKR {int(subtotal)}\n"
                    f"{dc_line}\n"
                    f"💳 کل رقم: PKR {int(grand_total)}\n"
                    f"📍 پتہ: {address}\n"
                    f"🔖 آرڈر نمبر: #{order_id[-6:]}\n\n"
                    f"⏱️ تخمینی ڈلیوری: {delivery_time}\n"
                    f"📲 نیا آرڈر دینے کے لیے *new order* لکھیں۔"
                ),
                "de": (
                    f"✅ *Bestellung bestätigt!* 🎉\n\n"
                    f"🍽️ *{po.get('dish', 'Item')}*\n"
                    f"📏 Größe: {po.get('size', '') or 'N/A'}\n"
                    f"🌶️ Schärfe: {po.get('spice', '') or 'Standard'}\n"
                    f"➕ Extras: {extras_text}\n"
                    f"💰 Zwischensumme: PKR {int(subtotal)}\n"
                    f"{dc_line}\n"
                    f"💳 Gesamtbetrag: PKR {int(grand_total)}\n"
                    f"📍 Adresse: {address}\n"
                    f"🔖 Bestellnr: #{order_id[-6:]}\n\n"
                    f"⏱️ Voraussichtliche Lieferung: {delivery_time}\n"
                    f"📲 Tippen Sie *new order* für eine neue Bestellung."
                ),
            }
            await send_whatsapp_text(from_num, conf.get(lang, conf["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 5 — Cart CONFIRMATION
        # ═══════════════════════════════════════════════════════
        if step == 5:
            if any(kw in q for kw in ["confirm", "yes", "okay", "ok", "haan", "proceed", "place",
                                       "done", "✅", "theek", "bilkul", "zaroor", "sure"]):
                session["step"] = 6
                last_addr = session.get("last_address")
                if last_addr:
                    addr_prompt = {
                        "en": f"📍 Deliver to your last address?\n_{last_addr}_\n\nType *same* to confirm or enter a new address:",
                        "ur": f"📍 پرانے پتے پر ڈلیوری؟\n_{last_addr}_\n\n*same* لکھیں یا نیا پتہ دیں:",
                        "de": f"📍 An letzte Adresse?\n_{last_addr}_\n\n*same* oder neue Adresse:",
                    }
                else:
                    addr_prompt = {
                        "en": "📍 Please share your full delivery address (house no., street, area, city):",
                        "ur": "📍 اپنا مکمل پتہ دیں (مکان نمبر، گلی، علاقہ، شہر):",
                        "de": "📍 Bitte vollständige Lieferadresse angeben:",
                    }
                await send_whatsapp_text(from_num, addr_prompt.get(lang, addr_prompt["en"]))

            elif any(kw in q for kw in ["add more", "more", "aur", "add", "➕", "aur kuch"]):
                session["step"] = 0
                add_more = {
                    "en": "Sure! What else would you like to add? 🍽️",
                    "ur": "بالکل! اور کیا شامل کرنا ہے؟ 🍽️",
                    "de": "Natürlich! Was möchten Sie noch hinzufügen? 🍽️",
                }
                await send_whatsapp_text(from_num, add_more.get(lang, add_more["en"]))

            elif any(kw in q for kw in ["clear", "reset", "cancel", "empty", "🗑️"]):
                session["cart"] = []
                session["step"] = 0
                cleared = {
                    "en": "🗑️ Cart cleared! What would you like to order?",
                    "ur": "🗑️ ٹوکری صاف! کیا آرڈر کرنا ہے؟",
                    "de": "🗑️ Warenkorb geleert! Was möchten Sie bestellen?",
                }
                await send_whatsapp_text(from_num, cleared.get(lang, cleared["en"]))

            else:
                cart    = session.get("cart", [])
                total   = _recalc_cart(cart)
                summary = _build_cart_summary(cart, total, lang)
                recap_prompt = {
                    "en": f"{summary}\n\n👉 Confirm order or add more?",
                    "ur": f"{summary}\n\n👉 تصدیق کریں یا مزید شامل کریں؟",
                    "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                }
                await send_whatsapp_buttons(
                    from_num,
                    recap_prompt.get(lang, recap_prompt["en"]),
                    ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"]
                )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 6 — ADDRESS for cart order
        # ═══════════════════════════════════════════════════════
        if step == 6:
            cart_items = session.get("cart", [])
            if not cart_items:
                session["step"] = 0
                cart_empty = {
                    "en": "🛒 Your cart is empty. What would you like to order?",
                    "ur": "🛒 ٹوکری خالی ہے۔ کیا آرڈر کرنا ہے؟",
                    "de": "🛒 Warenkorb ist leer. Was möchten Sie bestellen?",
                }
                await send_whatsapp_text(from_num, cart_empty.get(lang, cart_empty["en"]))
                return JSONResponse({"status": "ok"})

            if q.strip() in ["same", "same address", "same adress", "same add"]:
                address = session.get("last_address")
                if not address:
                    no_addr = {
                        "en": "⚠️ No previous address found. Please type your full address.",
                        "ur": "⚠️ پرانا پتہ نہیں ملا۔ اپنا مکمل پتہ لکھیں۔",
                        "de": "⚠️ Keine frühere Adresse. Bitte vollständige Adresse eingeben.",
                    }
                    await send_whatsapp_text(from_num, no_addr.get(lang, no_addr["en"]))
                    return JSONResponse({"status": "ok"})
            else:
                address_candidate = extract_address(msg_text) or msg_text.strip()
                if not _is_valid_address(address_candidate):
                    retry_addr = {
                        "en": "📍 Please share your *full* delivery address.\nExample: *House 12, Block B, Gulshan, Karachi*",
                        "ur": "📍 اپنا *مکمل* پتہ لکھیں۔\nمثال: *مکان 12، بلاک بی، گلشن، کراچی*",
                        "de": "📍 Bitte *vollständige* Lieferadresse eingeben.\nBeispiel: *Haus 12, Block B, Gulshan, Karachi*",
                    }
                    await send_whatsapp_text(from_num, retry_addr.get(lang, retry_addr["en"]))
                    return JSONResponse({"status": "ok"})
                address = address_candidate

            subtotal        = _recalc_cart(cart_items)
            delivery_charge = calculate_delivery_charge(subtotal, address)
            grand_total     = subtotal + delivery_charge
            summary         = _build_cart_summary(cart_items, subtotal, lang, delivery_charge, show_delivery=True)
            order_id        = create_order_from_cart(from_num, cart_items, address, delivery_charge)

            session["last_address"] = address
            reset_cart_only(session)

            if order_id == "db_error":
                db_err = {
                    "en": "⚠️ Sorry, issue placing your order. Please try again.",
                    "ur": "⚠️ معذرت، مسئلہ ہوا۔ دوبارہ کوشش کریں۔",
                    "de": "⚠️ Fehler bei der Bestellung. Bitte erneut versuchen.",
                }
                await send_whatsapp_text(from_num, db_err.get(lang, db_err["en"]))
                return JSONResponse({"status": "ok"})

            cart_cats     = [i.get("category", "") for i in cart_items]
            dominant_cat  = max(set(cart_cats), key=cart_cats.count) if cart_cats else ""
            delivery_time = get_delivery_time(dominant_cat)

            conf = {
                "en": (
                    f"✅ *Order Confirmed!* 🎉\n\n"
                    f"{summary}\n\n"
                    f"📍 Address: {address}\n"
                    f"🔖 Order ID: #{order_id[-6:]}\n"
                    f"⏱️ Estimated delivery: {delivery_time}\n\n"
                    f"📲 Type *new order* anytime to order again!"
                ),
                "ur": (
                    f"✅ *آرڈر تصدیق ہوگیا!* 🎉\n\n"
                    f"{summary}\n\n"
                    f"📍 پتہ: {address}\n"
                    f"🔖 نمبر: #{order_id[-6:]}\n"
                    f"⏱️ تخمینی ڈلیوری: {delivery_time}\n\n"
                    f"📲 نیا آرڈر دینے کے لیے *new order* لکھیں۔"
                ),
                "de": (
                    f"✅ *Bestellung bestätigt!* 🎉\n\n"
                    f"{summary}\n\n"
                    f"📍 Adresse: {address}\n"
                    f"🔖 Nr: #{order_id[-6:]}\n"
                    f"⏱️ Voraussichtliche Lieferung: {delivery_time}\n\n"
                    f"📲 Tippen Sie *new order* für eine neue Bestellung."
                ),
            }
            await send_whatsapp_text(from_num, conf.get(lang, conf["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 10 — Multi-item MISSING SIZE QUEUE
        # ═══════════════════════════════════════════════════════
        if step == 10:
            missing_queue = session.get("missing_info_queue", [])
            if missing_queue:
                first    = missing_queue[0]
                product  = first["product"]
                qty      = first["qty"]
                variants = product.get("variants", [])
                matched  = _match_variant(variants, msg_text)

                if not matched and variants:
                    sizes_str = " / ".join(v["size"] for v in variants)
                    size_err = {
                        "en": f"⚠️ Please choose one of: *{sizes_str}*",
                        "ur": f"⚠️ یہ سائز چنیں: *{sizes_str}*",
                        "de": f"⚠️ Bitte wählen: *{sizes_str}*",
                    }
                    await send_whatsapp_text(from_num, size_err.get(lang, size_err["en"]))
                    return JSONResponse({"status": "ok"})

                if matched:
                    cart_item = build_cart_item(product, matched["size"], "", [], qty)
                    session["cart"].append(cart_item)
                    missing_queue.pop(0)

                if missing_queue:
                    session["missing_info_queue"] = missing_queue
                    await _ask_size(from_num, missing_queue[0]["product"], lang)
                else:
                    session["missing_info_queue"] = []
                    pq = session.get("product_queue", [])
                    if pq:
                        pq.pop(0)
                        session["product_queue"] = pq
                        await _advance_product_queue(from_num, session, lang)
                    else:
                        session["step"] = 5
                        cart    = session.get("cart", [])
                        total   = _recalc_cart(cart)
                        summary = _build_cart_summary(cart, total, lang)
                        confirm_msgs = {
                            "en": f"{summary}\n\n👉 Confirm order or add more?",
                            "ur": f"{summary}\n\n👉 تصدیق کریں یا مزید شامل کریں؟",
                            "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                        }
                        await send_whatsapp_buttons(
                            from_num,
                            confirm_msgs.get(lang, confirm_msgs["en"]),
                            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"]
                        )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 20 — Multi-size SPICE RESOLUTION
        # ═══════════════════════════════════════════════════════
        if step == 20:
            multi_queue  = session.get("multi_size_queue", [])
            product      = session.get("pending_order", {}).get("product_ref", {})
            spice_levels = product.get("spice_levels", []) if product else []
            cart_items   = list(session.get("cart", []))

            per_item_parsed = _parse_multi_size_from_text(msg_text, product) if product else []
            size_spice_map: Dict[str, str] = {}
            for pi in per_item_parsed:
                if pi.get("spice") and pi.get("matched_variant"):
                    size_key = pi["matched_variant"].get("size", "").lower()
                    size_spice_map[size_key] = pi["spice"]

            shared_spice = ""
            for sl in sorted(spice_levels, key=len, reverse=True):
                if sl.lower() in q:
                    shared_spice = sl.strip().title()
                    break

            for queued_item in multi_queue:
                mv         = queued_item["matched_variant"]
                size_label = mv.get("size", "").lower()
                qty        = queued_item.get("qty", 1)

                found_spice = (
                    size_spice_map.get(size_label)
                    or shared_spice
                    or (spice_levels[0].strip().title() if spice_levels else "")
                )

                ci = build_cart_item(product, mv["size"], found_spice, [], qty)
                cart_items.append(ci)

            session["cart"]             = cart_items
            session["multi_size_queue"] = []

            extras_options = product.get("extras", []) if product else []
            if extras_options:
                session["pending_order"]["product_ref"] = product
                session["step"] = 30
                pq = session.get("product_queue", [])
                if pq:
                    pq.pop(0)
                    session["product_queue"] = pq
                await _ask_extras(from_num, product, lang)
            else:
                pq = session.get("product_queue", [])
                if pq:
                    pq.pop(0)
                    session["product_queue"] = pq
                    await _advance_product_queue(from_num, session, lang)
                else:
                    session["step"] = 5
                    total   = _recalc_cart(cart_items)
                    summary = _build_cart_summary(cart_items, total, lang)
                    confirm_msgs = {
                        "en": f"{summary}\n\n👉 Confirm order or add more?",
                        "ur": f"{summary}\n\n👉 تصدیق کریں یا مزید شامل کریں؟",
                        "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                    }
                    await send_whatsapp_buttons(from_num, confirm_msgs.get(lang, confirm_msgs["en"]),
                                                ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 30 — Multi-size EXTRAS
        # ═══════════════════════════════════════════════════════
        if step == 30:
            product        = session.get("pending_order", {}).get("product_ref", {})
            extras_options = product.get("extras", []) if product else []
            cart_items     = session.get("cart", [])

            chosen = []
            if not any(skip in q for skip in ["no", "skip", "nothing", "nahi", "nope", "nein", "nahin"]):
                chosen = _extract_extras_from_text(msg_text, extras_options)
                for e_name in chosen:
                    _track({f"extras_preference.{e_name}": 1})

            extras_price = sum(e["price"] for e in extras_options if e["name"].strip().title() in chosen)

            current_product_id = str(product.get("_id", "")) if product else ""
            targeted_size = ""
            if product:
                for v in product.get("variants", []):
                    if v.get("size", "").lower() in q:
                        targeted_size = v["size"].lower()
                        break

            for item in cart_items:
                is_same_product = (item.get("product_id") == current_product_id)
                if not is_same_product:
                    continue
                if targeted_size and item.get("size", "").lower() != targeted_size:
                    continue
                if chosen:
                    item["extras"]           = chosen
                    item["extras_price"]     = extras_price
                    item["total_item_price"] = (item["base_price"] + extras_price) * item["quantity"]

            session["cart"] = cart_items

            pq = session.get("product_queue", [])
            if pq:
                await _advance_product_queue(from_num, session, lang)
            else:
                session["step"] = 5
                total   = _recalc_cart(cart_items)
                summary = _build_cart_summary(cart_items, total, lang)
                confirm_msgs = {
                    "en": f"{summary}\n\n👉 Confirm order or add more?",
                    "ur": f"{summary}\n\n👉 تصدیق کریں یا مزید شامل کریں؟",
                    "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                }
                await send_whatsapp_buttons(from_num, confirm_msgs.get(lang, confirm_msgs["en"]),
                                            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 0 — Normal intent routing
        # ═══════════════════════════════════════════════════════

        # ── Greeting ──────────────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["greeting"]):
            greeting = BOT_DATA.get("initial_message", {}).get(lang, "Welcome! 🍽️ How can I help you?")
            sugs     = get_suggestions(from_num, lang)
            reply    = greeting
            if sugs:
                reply += "\n\n💡 " + "\n• ".join(sugs)
            await send_whatsapp_buttons(from_num, reply, ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"])
            return JSONResponse({"status": "ok"})

        # ── Delivery charge inquiry ────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["delivery_charge"]):
            dc         = BOT_DATA.get("delivery_charges", {})
            flat       = float(dc.get("flat_charge", 0) or 0)
            free_above = float(dc.get("free_above", 0) or 0)
            per_area   = dc.get("per_area", {})

            lines = []
            if free_above > 0:
                lines.append({
                    "en": f"✅ Free delivery on orders above PKR {int(free_above)}!",
                    "ur": f"✅ PKR {int(free_above)} سے زیادہ آرڈر پر مفت ڈلیوری!",
                    "de": f"✅ Kostenlose Lieferung bei Bestellungen über PKR {int(free_above)}!",
                }.get(lang, f"✅ Free delivery above PKR {int(free_above)}!"))
            if flat > 0 and not lines:
                lines.append({
                    "en": f"🚚 Standard delivery charge: PKR {int(flat)}",
                    "ur": f"🚚 معیاری ڈلیوری چارج: PKR {int(flat)}",
                    "de": f"🚚 Standard-Liefergebühr: PKR {int(flat)}",
                }.get(lang, f"🚚 Delivery charge: PKR {int(flat)}"))
            elif flat == 0 and not lines:
                lines.append({
                    "en": "🎉 Great news — we offer FREE delivery!",
                    "ur": "🎉 خوشخبری — ہم مفت ڈلیوری کرتے ہیں!",
                    "de": "🎉 Wir liefern KOSTENLOS!",
                }.get(lang, "🎉 FREE delivery!"))
            if per_area:
                area_lines = "\n".join(f"  • {k.title()}: PKR {int(v)}" for k, v in per_area.items())
                lines.append({
                    "en": f"📍 Area-specific charges:\n{area_lines}",
                    "ur": f"📍 علاقہ مخصوص چارجز:\n{area_lines}",
                    "de": f"📍 Bereichsspezifische Gebühren:\n{area_lines}",
                }.get(lang, f"📍 Area charges:\n{area_lines}"))

            reply = "\n\n".join(lines)
            await send_whatsapp_text(from_num, reply)
            return JSONResponse({"status": "ok"})

        # ── Show cart ──────────────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["cart"]):
            cart = session.get("cart", [])
            if cart:
                total   = _recalc_cart(cart)
                summary = _build_cart_summary(cart, total, lang)
                await send_whatsapp_buttons(from_num, summary, ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
                session["step"] = 5
            else:
                cart_empty = {
                    "en": "🛒 Your cart is empty. What would you like to order? 🍽️",
                    "ur": "🛒 ٹوکری خالی ہے۔ کیا آرڈر کرنا ہے؟ 🍽️",
                    "de": "🛒 Warenkorb ist leer. Was möchten Sie bestellen? 🍽️",
                }
                await send_whatsapp_text(from_num, cart_empty.get(lang, cart_empty["en"]))
            return JSONResponse({"status": "ok"})

        # ── Clear cart ─────────────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["clear"]):
            session["cart"] = []
            session["step"] = 0
            cleared = {
                "en": "🗑️ Cart cleared! What would you like to order?",
                "ur": "🗑️ ٹوکری صاف! کیا آرڈر کرنا ہے؟",
                "de": "🗑️ Warenkorb geleert! Was möchten Sie bestellen?",
            }
            await send_whatsapp_text(from_num, cleared.get(lang, cleared["en"]))
            return JSONResponse({"status": "ok"})

        # ── Confirm order ──────────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["confirm"]) and session.get("cart"):
            session["step"] = 6
            last_addr = session.get("last_address")
            if last_addr:
                addr_prompt = {
                    "en": f"📍 Deliver to your last address?\n_{last_addr}_\n\nType *same* or enter a new address:",
                    "ur": f"📍 پرانے پتے پر ڈلیوری؟\n_{last_addr}_\n\n*same* لکھیں یا نیا پتہ دیں:",
                    "de": f"📍 An letzte Adresse?\n_{last_addr}_\n\n*same* oder neue Adresse:",
                }
            else:
                addr_prompt = {
                    "en": "📍 Please share your full delivery address:",
                    "ur": "📍 اپنا مکمل پتہ دیں:",
                    "de": "📍 Lieferadresse angeben:",
                }
            await send_whatsapp_text(from_num, addr_prompt.get(lang, addr_prompt["en"]))
            return JSONResponse({"status": "ok"})

        # ── MIXED INTENT: order + price display ────────────────
        order_intent  = any(kw in q for kw in INTENT_KEYWORDS["order"])
        price_intent  = _detect_price_menu_intent(q)
        menu_intent   = any(kw in q for kw in INTENT_KEYWORDS["menu"])
        inquiry_intent = any(kw in q for kw in INTENT_KEYWORDS["inquiry"])
        multi_signals = ["and", "aur", "+", "also", "ke saath", "اور", "saath", "plus"]
        is_multi      = any(s in q for s in multi_signals)

        if price_intent:
            _track({"total_searches": 1})
            await _handle_full_price_display(from_num, q, lang)
            if not order_intent:
                return JSONResponse({"status": "ok"})

        # ── FIX I: Inquiry about a dish (not an order) ─────────
        if inquiry_intent and not order_intent:
            product = _find_product_by_query(msg_text)
            if product:
                name        = product.get("title", "Item").strip().title()
                desc        = product.get("description", "")
                variants    = product.get("variants", [])
                spice_lvls  = product.get("spice_levels", [])
                size_list   = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants) if variants else ""
                spice_str   = " / ".join(s.strip().title() for s in spice_lvls) if spice_lvls else ""

                reply_parts = [f"🍽️ *{name}*"]
                if desc:
                    reply_parts.append(desc.strip())
                if size_list:
                    sizes_label = {"en": "Sizes:", "ur": "سائز:", "de": "Größen:"}.get(lang, "Sizes:")
                    reply_parts.append(f"\n{sizes_label}\n{size_list}")
                if spice_str:
                    spice_label = {"en": "Spice levels:", "ur": "مسالے کی سطح:", "de": "Schärfegrade:"}.get(lang, "Spice levels:")
                    reply_parts.append(f"{spice_label} {spice_str}")

                order_q = {"en": "\nWould you like to order? 😊", "ur": "\nکیا آرڈر کرنا ہے؟ 😊", "de": "\nMöchten Sie bestellen? 😊"}.get(lang, "\nWould you like to order? 😊")
                reply_parts.append(order_q)

                # FIX G: store as last_shown_product
                session["last_shown_product"] = product
                await send_whatsapp_buttons(from_num, "\n".join(reply_parts),
                                            ["✅ Order Now", "📋 View Menu"])
                return JSONResponse({"status": "ok"})

        # ── Order intent ───────────────────────────────────────
        if order_intent or is_multi or re.search(r'\d+\s*(?:kg|ml|l\b|g\b)', q):
            _track({"total_cart_additions": 1})
            if is_multi or re.search(r'\d+\s*(?:kg|ml|l\b|g\b)', q):
                handled = await handle_multi_item_order(from_num, msg_text, lang)
                if handled:
                    return JSONResponse({"status": "ok"})
            handled = await _handle_single_item_order(from_num, msg_text, lang)
            if handled:
                return JSONResponse({"status": "ok"})

        # ── Menu display ───────────────────────────────────────
        if menu_intent:
            _track({"total_searches": 1})
            products = filter_products(msg_text) or PRODUCTS_DATA[:8]
            if products:
                header = {
                    "en": "🍽️ Our Menu",
                    "ur": "🍽️ ہمارا مینو",
                    "de": "🍽️ Unsere Speisekarte",
                }.get(lang, "🍽️ Our Menu")
                await send_whatsapp_list(from_num, header, products, lang)
            else:
                no_menu = {
                    "en": "Menu not available right now. Try again soon! 🙏",
                    "ur": "مینو دستیاب نہیں۔ تھوڑی دیر بعد کوشش کریں! 🙏",
                    "de": "Menü gerade nicht verfügbar. Bitte später versuchen! 🙏",
                }
                await send_whatsapp_text(from_num, no_menu.get(lang, no_menu["en"]))
            return JSONResponse({"status": "ok"})

        # ── FAQ ────────────────────────────────────────────────
        faq_resp = get_faq_response(msg_text, lang)
        if faq_resp:
            await send_whatsapp_text(from_num, faq_resp)
            return JSONResponse({"status": "ok"})

        # ── Discount ───────────────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["discount"]):
            disc = BOT_DATA.get("discount_message", {}).get(lang, BOT_DATA.get("discount_message", {}).get("en"))
            if disc:
                await send_whatsapp_text(from_num, disc)
                return JSONResponse({"status": "ok"})

        # ── Order status ───────────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["status"]) and orders_col:
            latest = orders_col.find_one({"user_id": from_num}, sort=[("timestamp", DESCENDING)])
            if latest:
                dish_name    = latest.get("dish") or (latest.get("items", [{}])[0].get("title", "Order"))
                dish_name    = dish_name.strip().title()
                status       = latest.get("status", "Pending")
                status_emoji = {"Pending": "⏳", "Accepted": "✅", "Processing": "👨‍🍳", "Delivered": "🚗", "Rejected": "❌"}.get(status, "📦")
                st = {
                    "en": f"{status_emoji} Your latest order (*{dish_name}*): *{status}*\n🔖 ID: #{str(latest.get('_id', ''))[-6:]}",
                    "ur": f"{status_emoji} آپ کے آخری آرڈر کی حالت (*{dish_name}*): *{status}*\n🔖 نمبر: #{str(latest.get('_id', ''))[-6:]}",
                    "de": f"{status_emoji} Letzter Auftragsstatus (*{dish_name}*): *{status}*\n🔖 Nr: #{str(latest.get('_id', ''))[-6:]}",
                }
                await send_whatsapp_text(from_num, st.get(lang, st["en"]))
            else:
                no_order = {
                    "en": "No orders found yet. Place your first order! 🍽️",
                    "ur": "ابھی تک کوئی آرڈر نہیں۔ پہلا آرڈر دیں! 🍽️",
                    "de": "Noch keine Bestellungen. Geben Sie Ihre erste auf! 🍽️",
                }
                await send_whatsapp_text(from_num, no_order.get(lang, no_order["en"]))
            return JSONResponse({"status": "ok"})

        # ── Product name search (FIX H: index-powered) ─────────
        matched_product = _find_product_by_query(msg_text)
        if matched_product:
            product_name = matched_product.get("title", "Item").strip().title()
            variants     = matched_product.get("variants", [])

            # FIX G: Always store last shown product
            session["last_shown_product"] = matched_product

            if variants:
                size_list = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants)
                reply = {
                    "en": f"🍽️ *{product_name}*\n\nAvailable sizes:\n{size_list}\n\nWould you like to order?",
                    "ur": f"🍽️ *{product_name}*\n\nدستیاب سائز:\n{size_list}\n\nکیا آرڈر کرنا ہے؟",
                    "de": f"🍽️ *{product_name}*\n\nVerfügbare Größen:\n{size_list}\n\nMöchten Sie bestellen?",
                }
                await send_whatsapp_buttons(from_num, reply.get(lang, reply["en"]), ["✅ Order Now", "📋 View Menu"])
            else:
                price_str = f"PKR {matched_product.get('price', 'N/A')}"
                no_variant_reply = {
                    "en": f"🍽️ *{product_name}* — {price_str}\n\nWould you like to order?",
                    "ur": f"🍽️ *{product_name}* — {price_str}\n\nکیا آرڈر کرنا ہے؟",
                    "de": f"🍽️ *{product_name}* — {price_str}\n\nMöchten Sie bestellen?",
                }
                await send_whatsapp_buttons(
                    from_num,
                    no_variant_reply.get(lang, no_variant_reply["en"]),
                    ["✅ Order Now", "📋 View Menu"]
                )
            return JSONResponse({"status": "ok"})

        # ── Smart AI-powered fallback ───────────────────────────
        smart_reply = await _smart_fallback(from_num, msg_text, lang)
        await send_whatsapp_text(from_num, smart_reply)
        return JSONResponse({"status": "ok"})

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=200)

# ============================================================
# CRM / ADMIN PANEL
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def serve_crm(request: Request):
    return templates.TemplateResponse("crm.html", {"request": request})


@app.post("/password")
async def check_password(username: str = Form(...), password: str = Form(...)):
    if username == CRM_USERNAME and password == SECRET_PASSWORD:
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "message": "Invalid credentials"}, status_code=401)

# ============================================================
# PRODUCTS API
# ============================================================

@app.get("/api/products")
async def get_products():
    load_data_realtime()
    return {"products": PRODUCTS_DATA}


@app.post("/add_product")
async def add_product(request: Request):
    if products_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    for field in ["variants", "extras", "spice_levels"]:
        if isinstance(body.get(field), str):
            body[field] = _parse_json_field(body[field], [])

    for field in ["rating", "trending_score"]:
        try:
            body[field] = float(body.get(field, 0) or 0)
        except (ValueError, TypeError):
            body[field] = 0.0

    body.setdefault("availability", "available")
    body["created_at"] = datetime.utcnow().isoformat()
    result = products_col.insert_one(body)
    load_data_realtime()
    return JSONResponse({"message": "Product added!", "id": str(result.inserted_id)})


@app.post("/update_product")
async def update_product(request: Request):
    if products_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    product_id = body.pop("id", None)
    if not product_id:
        return JSONResponse({"message": "Missing product id"}, status_code=400)

    for field in ["variants", "extras", "spice_levels"]:
        if isinstance(body.get(field), str):
            body[field] = _parse_json_field(body[field], [])

    for field in ["rating", "trending_score"]:
        try:
            body[field] = float(body.get(field, 0) or 0)
        except (ValueError, TypeError):
            body[field] = 0.0

    body["updated_at"] = datetime.utcnow().isoformat()
    products_col.update_one({"_id": ObjectId(product_id)}, {"$set": body})
    load_data_realtime()
    return JSONResponse({"message": "Product updated!", "status": "success"})


@app.post("/delete_product")
async def delete_product(id: str = Form(...)):
    if products_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    products_col.delete_one({"_id": ObjectId(id)})
    load_data_realtime()
    return JSONResponse({"message": "Product deleted!", "status": "success"})

# ============================================================
# CART API
# ============================================================

@app.post("/api/cart/add")
async def cart_add(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"message": "Invalid JSON body"}, status_code=400)

    user_id    = body.get("user_id")
    product_id = body.get("product_id")
    size       = body.get("size", "").strip()
    spice      = body.get("spice", "").strip()
    extras     = body.get("extras", [])
    quantity   = int(body.get("quantity", 1))

    if not user_id or not product_id:
        return JSONResponse({"message": "user_id and product_id required"}, status_code=400)
    if products_col is None or carts_col is None:
        return JSONResponse({"message": "Database not connected"}, status_code=500)

    try:
        product = products_col.find_one({"_id": ObjectId(product_id)})
    except Exception:
        product = products_col.find_one({"title": product_id})

    if not product:
        return JSONResponse({"message": "Product not found"}, status_code=404)

    cart_item = build_cart_item(product, size, spice, extras, quantity)
    cart      = carts_col.find_one({"user_id": user_id})
    if not cart:
        cart = {"user_id": user_id, "items": [], "total_price": 0, "created_at": datetime.utcnow().isoformat()}

    items    = cart.get("items", [])
    existing = next((i for i in items if i["product_id"] == str(product["_id"]) and i.get("size") == size), None)
    if existing:
        existing["quantity"]        += quantity
        existing["total_item_price"] = (existing["base_price"] + existing["extras_price"]) * existing["quantity"]
    else:
        items.append(cart_item)

    total = _recalc_cart(items)
    carts_col.update_one(
        {"user_id": user_id},
        {"$set": {"items": items, "total_price": total, "updated_at": datetime.utcnow().isoformat()}},
        upsert=True,
    )
    _track({"total_cart_additions": 1})
    updated = carts_col.find_one({"user_id": user_id})
    return JSONResponse({"message": "Added to cart", "cart": _str_id(updated)})


@app.post("/api/cart/remove")
async def cart_remove(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"message": "Invalid JSON body"}, status_code=400)

    user_id    = body.get("user_id")
    product_id = body.get("product_id")
    size       = body.get("size", "").strip()

    if carts_col is None:
        return JSONResponse({"message": "Database not connected"}, status_code=500)

    cart = carts_col.find_one({"user_id": user_id})
    if not cart:
        return JSONResponse({"message": "Cart not found"}, status_code=404)

    items = [i for i in cart.get("items", []) if not (i["product_id"] == product_id and i.get("size") == size)]
    total = _recalc_cart(items)
    carts_col.update_one({"user_id": user_id}, {"$set": {"items": items, "total_price": total}})
    updated = carts_col.find_one({"user_id": user_id})
    return JSONResponse({"message": "Item removed", "cart": _str_id(updated)})


@app.post("/api/cart/update")
async def cart_update(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"message": "Invalid JSON body"}, status_code=400)

    user_id    = body.get("user_id")
    product_id = body.get("product_id")
    size       = body.get("size", "").strip()
    quantity   = int(body.get("quantity", 1))

    if carts_col is None:
        return JSONResponse({"message": "Database not connected"}, status_code=500)

    cart = carts_col.find_one({"user_id": user_id})
    if not cart:
        return JSONResponse({"message": "Cart not found"}, status_code=404)

    items = cart.get("items", [])
    for it in items:
        if it["product_id"] == product_id and it.get("size") == size:
            if quantity <= 0:
                items.remove(it)
            else:
                it["quantity"]         = quantity
                it["total_item_price"] = (it["base_price"] + it["extras_price"]) * quantity
            break

    total = _recalc_cart(items)
    carts_col.update_one({"user_id": user_id}, {"$set": {"items": items, "total_price": total}})
    updated = carts_col.find_one({"user_id": user_id})
    return JSONResponse({"message": "Cart updated", "cart": _str_id(updated)})


@app.get("/api/cart/{user_id}")
async def cart_get(user_id: str):
    if carts_col is None:
        return JSONResponse({"message": "Database not connected"}, status_code=500)
    cart = carts_col.find_one({"user_id": user_id})
    if not cart:
        return JSONResponse({"user_id": user_id, "items": [], "total_price": 0})
    return JSONResponse(_str_id(cart))

# ============================================================
# ORDERS API
# ============================================================

@app.get("/api/orders")
async def get_orders(status: Optional[str] = None):
    if orders_col is None:
        return {"orders": []}
    query  = {"status": status} if status else {}
    orders = [_str_id(o) for o in orders_col.find(query).sort("timestamp", DESCENDING).limit(100)]
    return {"orders": orders}


@app.post("/api/orders/{order_id}/status")
async def update_order_status(order_id: str, request: Request):
    if orders_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"message": "Invalid JSON body"}, status_code=400)

    new_status = body.get("status")
    valid      = ["Pending", "Accepted", "Rejected", "Processing", "Delivered"]
    if new_status not in valid:
        return JSONResponse({"message": f"Invalid status. Use: {valid}"}, status_code=400)

    result = orders_col.update_one({"_id": ObjectId(order_id)}, {"$set": {"status": new_status}})
    if result.matched_count:
        order = orders_col.find_one({"_id": ObjectId(order_id)})
        if order:
            dish_name  = order.get("dish") or (order.get("items", [{}])[0].get("title", "Order"))
            dish_name  = dish_name.strip().title()
            status_msg = {
                "Pending":    f"⏳ Your order *{dish_name}* is pending.",
                "Accepted":   f"✅ Great news! Your *{dish_name}* order has been accepted. Preparing now!",
                "Processing": f"👨‍🍳 Your *{dish_name}* is being prepared!",
                "Delivered":  f"🚗 Your *{dish_name}* is on its way!",
                "Rejected":   f"❌ Sorry, your *{dish_name}* order was rejected. Please contact support.",
            }
            msg = status_msg.get(new_status, f"📦 Order *{dish_name}* status: *{new_status}*")
            asyncio.create_task(send_whatsapp_text(order["user_id"], msg))
        return JSONResponse({"message": f"Status updated to {new_status}", "status": "success"})
    return JSONResponse({"message": "Order not found."}, status_code=404)

# ============================================================
# FAQ API
# ============================================================

@app.get("/api/faqs")
async def get_faqs():
    load_data_realtime()
    return {"faqs": BOT_DATA.get("faq", {})}


@app.post("/api/faqs")
async def update_faqs(request: Request):
    if meta_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    body = await request.json()
    meta_col.update_one({"type": "config"}, {"$set": {"faq": body}}, upsert=True)
    load_data_realtime()
    return JSONResponse({"message": "FAQs updated!", "status": "success"})

# ============================================================
# SUGGESTIONS API
# ============================================================

@app.get("/api/suggestions")
async def get_suggestions_api():
    load_data_realtime()
    return {"smart_suggestions": BOT_DATA.get("smart_suggestions", {})}


@app.post("/api/suggestions")
async def update_suggestions(request: Request):
    if meta_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    body = await request.json()
    meta_col.update_one({"type": "config"}, {"$set": {"smart_suggestions": body}}, upsert=True)
    load_data_realtime()
    return JSONResponse({"message": "Suggestions updated!", "status": "success"})

# ============================================================
# DELIVERY TIME API
# ============================================================

@app.get("/api/delivery-time")
async def get_delivery_time_api():
    load_data_realtime()
    return {
        "delivery_time":            BOT_DATA.get("delivery_time", "35-45 mins"),
        "delivery_time_exceptions": BOT_DATA.get("delivery_time_exceptions", {}),
    }


@app.post("/api/delivery-time")
async def update_delivery_time(request: Request):
    if meta_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"message": "Invalid JSON body"}, status_code=400)

    update_fields: Dict[str, Any] = {}
    if "delivery_time" in body:
        update_fields["delivery_time"] = str(body["delivery_time"]).strip()
    if "delivery_time_exceptions" in body:
        exceptions = body["delivery_time_exceptions"]
        if not isinstance(exceptions, dict):
            return JSONResponse({"message": "delivery_time_exceptions must be an object"}, status_code=400)
        update_fields["delivery_time_exceptions"] = {
            k.lower().strip(): str(v).strip() for k, v in exceptions.items()
        }
    if not update_fields:
        return JSONResponse({"message": "No valid fields provided."}, status_code=400)

    meta_col.update_one({"type": "config"}, {"$set": update_fields}, upsert=True)
    load_data_realtime()
    return JSONResponse({
        "message":                  "Delivery time updated!",
        "status":                   "success",
        "delivery_time":            BOT_DATA.get("delivery_time", "35-45 mins"),
        "delivery_time_exceptions": BOT_DATA.get("delivery_time_exceptions", {}),
    })

# ============================================================
# DELIVERY CHARGES API
# ============================================================

@app.get("/api/delivery-charges")
async def get_delivery_charges():
    load_data_realtime()
    return {"delivery_charges": BOT_DATA.get("delivery_charges", {})}


@app.post("/api/delivery-charges")
async def update_delivery_charges(request: Request):
    if meta_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"message": "Invalid JSON body"}, status_code=400)

    allowed_keys = {"flat_charge", "free_above", "per_area", "free_keywords"}
    if not any(k in body for k in allowed_keys):
        return JSONResponse({"message": f"Provide at least one of: {allowed_keys}"}, status_code=400)

    existing = BOT_DATA.get("delivery_charges", {})
    updated_dc = {
        "flat_charge":   float(existing.get("flat_charge", 0) or 0),
        "free_above":    float(existing.get("free_above", 0) or 0),
        "per_area":      existing.get("per_area", {}),
        "free_keywords": existing.get("free_keywords", []),
    }

    if "flat_charge" in body:
        try: updated_dc["flat_charge"] = float(body["flat_charge"])
        except (TypeError, ValueError):
            return JSONResponse({"message": "flat_charge must be a number"}, status_code=400)

    if "free_above" in body:
        try: updated_dc["free_above"] = float(body["free_above"])
        except (TypeError, ValueError):
            return JSONResponse({"message": "free_above must be a number"}, status_code=400)

    if "per_area" in body:
        if not isinstance(body["per_area"], dict):
            return JSONResponse({"message": "per_area must be an object"}, status_code=400)
        updated_dc["per_area"] = {k.lower().strip(): float(v) for k, v in body["per_area"].items()}

    if "free_keywords" in body:
        if not isinstance(body["free_keywords"], list):
            return JSONResponse({"message": "free_keywords must be a list"}, status_code=400)
        updated_dc["free_keywords"] = [str(kw).lower().strip() for kw in body["free_keywords"]]

    meta_col.update_one({"type": "config"}, {"$set": {"delivery_charges": updated_dc}}, upsert=True)
    load_data_realtime()
    return JSONResponse({
        "message":          "Delivery charges updated!",
        "status":           "success",
        "delivery_charges": BOT_DATA.get("delivery_charges", {}),
    })

# ============================================================
# ANALYTICS API
# ============================================================

@app.get("/api/analytics")
async def get_analytics():
    if analytics_col is None:
        return {}
    data = analytics_col.find_one({"type": "analytics"}) or {}
    return _str_id(data)


@app.post("/track_click")
async def track_click(product_id: str = Form(...)):
    _track({"total_clicks": 1, f"product_clicks.{product_id}": 1})
    return {"status": "tracked"}


@app.post("/track_language")
async def track_language(language: str = Form(...)):
    _track({f"supported_languages.{language.lower().strip()}": 1})
    return {"status": "tracked", "language": language}


@app.post("/track_size")
async def track_size(size: str = Form(...)):
    _track({f"size_preference.{size.strip()}": 1})
    return {"status": "tracked"}


@app.post("/track_spice")
async def track_spice(spice: str = Form(...)):
    _track({f"spice_preference.{spice.strip().title()}": 1})
    return {"status": "tracked"}

# ============================================================
# FULL DATA API (Dashboard bootstrap)
# ============================================================

@app.get("/api/data")
async def get_api_data():
    load_data_realtime()
    try:
        orders    = []
        analytics = {}
        if orders_col is not None:
            orders = [_str_id(o) for o in orders_col.find({}).sort("timestamp", DESCENDING).limit(50)]
        if analytics_col is not None:
            analytics = _str_id(analytics_col.find_one({"type": "analytics"}) or {})
        return {
            "products":  PRODUCTS_DATA,
            "orders":    orders,
            "analytics": analytics,
            "config": {
                "faq":                 BOT_DATA.get("faq", {}),
                "initial_message":     BOT_DATA.get("initial_message", {}),
                "discount_message":    BOT_DATA.get("discount_message", {}),
                "supported_languages": BOT_DATA.get("supported_languages", ["en", "ur", "de"]),
                "smart_suggestions":   BOT_DATA.get("smart_suggestions", {}),
                "delivery_charges":    BOT_DATA.get("delivery_charges", {}),
            },
        }
    except Exception as e:
        logger.error(f"API data error: {e}")
        return {"products": [], "orders": [], "analytics": {}, "config": {}}

# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():
    load_data_realtime()
    init_analytics()
    logger.info("🚀 Restaurant Bot v13.0 started!")
    logger.info(f"   Products loaded    : {len(PRODUCTS_DATA)}")
    logger.info(f"   Keyword index size : {len(PRODUCT_KEYWORD_INDEX)}")
    logger.info(f"   FAQ keys           : {list(BOT_DATA.get('faq', {}).keys())}")
    logger.info(f"   Delivery time      : {get_delivery_time()}")
    logger.info(f"   Delivery charges   : {BOT_DATA.get('delivery_charges', {})}")
    logger.info(f"   WhatsApp connected : {'✅' if WHATSAPP_TOKEN else '❌'}")
    logger.info(f"   MongoDB connected  : {'✅' if products_col is not None else '❌'}")
    logger.info(f"   AI fallback        : {'✅' if ANTHROPIC_API_KEY else '⚠️ Static fallback active'}")
