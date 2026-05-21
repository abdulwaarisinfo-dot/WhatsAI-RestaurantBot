"""
WhatsApp AI Restaurant Bot — FastAPI Backend (Production v14.5)
===============================================================
v14.5 improvements over v14.4:

  ✅ ALL v14.4 logic 100% preserved — only targeted improvements applied.

  ✅ NEW 1: Example menu shown FULLY — all items with sizes/prices displayed
             in a rich, human-readable format whenever menu is requested.
             No truncation, no "check back soon" on example/demo products.

  ✅ NEW 2: WhatsApp card-style product display — when showing cart or a
             product, buttons now include "➕ Add More" alongside
             "✅ Confirm Order" and "🗑️ Clear Cart" for seamless flow.

  ✅ NEW 3: Human-friendly bot personality — warm, conversational,
             emotionally intelligent responses throughout all steps.
             Bot speaks like a real, caring restaurant staff member.
             Handles confusion, hesitation, and off-topic messages gracefully.

  ✅ NEW 4: Professional & reliable — improved error messaging, address
             validation feedback, and fallback handling with personality.

  ✅ NEW 5: Rich greeting with full menu teaser — on first contact the bot
             introduces itself warmly and shows a compact menu preview
             to spark interest immediately.

  ✅ FIX 1  (v14.4): Multi-size same-product orders correctly enter order flow.
  ✅ FIX 2  (v14.4): Spice resolution no longer misidentifies size words.
  ✅ FIX 3  (v14.4): Shared spice for multi-size orders works correctly.
  ✅ FIX 4  (v14.4): Cart summary shows accurate spice/extras per line-item.
  ✅ FIX 5  (v14.4): Price-menu intent guard for multi-size order strings.
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
logger = logging.getLogger("RestaurantBot.v14.5")

BOT_DATA: Dict[str, Any] = {}
PRODUCTS_DATA: List[Dict[str, Any]] = []
PRODUCT_KEYWORD_INDEX: Dict[str, List[Dict]] = {}
USER_SESSIONS: Dict[str, Dict[str, Any]] = {}
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
    title="WhatsApp AI Restaurant Bot v14.5",
    version="14.5",
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
        "en": "🛒 *Your Order So Far:*\n",
        "ur": "🛒 *آپ کا آرڈر:*\n",
        "de": "🛒 *Ihr Warenkorb:*\n",
    }
    lines = [headers.get(lang, headers["en"])]
    for idx, item in enumerate(items, 1):
        name   = item.get("title", "Item").strip().title()
        size   = item.get("size", "").strip()
        qty    = item.get("quantity", 1)
        price  = (item.get("base_price", 0) + item.get("extras_price", 0)) * qty
        extras = ", ".join(e.strip().title() for e in item.get("extras", []))
        spice  = item.get("spice", "").strip().title()
        line   = f"{idx}. *{name}*"
        if size:    line += f" ({size})"
        if qty > 1: line += f" ×{qty}"
        line += f" — PKR {int(price)}"
        if extras:  line += f"\n   ➕ Extras: {extras}"
        if spice:   line += f"\n   🌶️ Spice: {spice}"
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


# ============================================================
# CATEGORY / MENU HELPERS
# ============================================================

_CATEGORY_EMOJI_MAP = {
    "pizza":    "🍕",
    "burger":   "🍔",
    "biryani":  "🍛",
    "karahi":   "🥘",
    "drinks":   "🥤",
    "dessert":  "🍰",
    "rice":     "🍚",
    "rolls":    "🌯",
    "chicken":  "🍗",
    "beef":     "🥩",
    "mutton":   "🍖",
    "fish":     "🐟",
    "soup":     "🍲",
    "salad":    "🥗",
    "bread":    "🫓",
    "pasta":    "🍝",
    "steak":    "🥩",
    "shawarma": "🌯",
    "sandwich": "🥪",
}


# ── v14.5 NEW 1: Full example menu — shown completely, never truncated ──

EXAMPLE_MENU: Dict[str, List[Dict]] = {
    "pizza": [
        {"title": "Margherita Pizza",    "variants": [{"size": "Small", "price": 490}, {"size": "Medium", "price": 790}, {"size": "Large", "price": 1090}]},
        {"title": "BBQ Chicken Pizza",   "variants": [{"size": "Small", "price": 590}, {"size": "Medium", "price": 950}, {"size": "Large", "price": 1290}]},
        {"title": "Tikka Pizza",         "variants": [{"size": "Small", "price": 550}, {"size": "Medium", "price": 890}, {"size": "Large", "price": 1190}]},
        {"title": "Pepperoni Pizza",     "variants": [{"size": "Small", "price": 620}, {"size": "Medium", "price": 990}, {"size": "Large", "price": 1390}]},
    ],
    "burger": [
        {"title": "Zinger Burger",       "variants": [{"size": "Regular", "price": 320}, {"size": "Large", "price": 490}]},
        {"title": "Smash Burger",        "variants": [{"size": "Single", "price": 390}, {"size": "Double", "price": 590}]},
        {"title": "Cheese Burger",       "variants": [{"size": "Regular", "price": 290}, {"size": "Large", "price": 440}]},
        {"title": "Crispy Chicken Burger","variants":[{"size": "Regular", "price": 350}, {"size": "Large", "price": 520}]},
    ],
    "biryani": [
        {"title": "Chicken Biryani",     "variants": [{"size": "Half Plate", "price": 320}, {"size": "Full Plate", "price": 590}, {"size": "Family Pack", "price": 1190}]},
        {"title": "Beef Biryani",        "variants": [{"size": "Half Plate", "price": 370}, {"size": "Full Plate", "price": 650}, {"size": "Family Pack", "price": 1290}]},
        {"title": "Sindhi Biryani",      "variants": [{"size": "Half Plate", "price": 340}, {"size": "Full Plate", "price": 620}, {"size": "Family Pack", "price": 1250}]},
    ],
    "karahi": [
        {"title": "Chicken Karahi",      "variants": [{"size": "0.5kg", "price": 590}, {"size": "1kg", "price": 1090}]},
        {"title": "Beef Karahi",         "variants": [{"size": "0.5kg", "price": 690}, {"size": "1kg", "price": 1290}]},
        {"title": "Mutton Karahi",       "variants": [{"size": "0.5kg", "price": 890}, {"size": "1kg", "price": 1690}]},
    ],
    "drinks": [
        {"title": "Soft Drink",          "variants": [{"size": "Regular", "price": 80}, {"size": "Large", "price": 120}]},
        {"title": "Fresh Juice",         "variants": [{"size": "Small", "price": 150}, {"size": "Large", "price": 250}]},
        {"title": "Lassi",               "variants": [{"size": "Regular", "price": 120}, {"size": "Large", "price": 200}]},
        {"title": "Cold Coffee",         "variants": [{"size": "Regular", "price": 180}, {"size": "Large", "price": 280}]},
    ],
    "dessert": [
        {"title": "Gulab Jamun",         "variants": [{"size": "6 Pieces", "price": 180}, {"size": "12 Pieces", "price": 340}]},
        {"title": "Kheer",               "variants": [{"size": "Small", "price": 120}, {"size": "Large", "price": 220}]},
        {"title": "Ice Cream",           "variants": [{"size": "Single Scoop", "price": 90}, {"size": "Double Scoop", "price": 160}]},
    ],
}


def _build_full_example_menu(lang: str = "en") -> str:
    """
    v14.5 NEW 1: Build a complete, human-readable example menu.
    Shows ALL categories and ALL items with full price breakdown.
    """
    header_map = {
        "en": (
            "🍽️ *Our Full Menu*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "_Fresh ingredients • Made with love_\n"
        ),
        "ur": (
            "🍽️ *ہمارا مکمل مینو*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "_تازہ اجزاء • محبت سے بنا ہوا_\n"
        ),
        "de": (
            "🍽️ *Unsere vollständige Speisekarte*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "_Frische Zutaten • Mit Liebe gemacht_\n"
        ),
    }
    lines = [header_map.get(lang, header_map["en"])]

    for cat, items in EXAMPLE_MENU.items():
        emoji    = _CATEGORY_EMOJI_MAP.get(cat, "🍽️")
        cat_name = cat.replace("_", " ").title()
        lines.append(f"\n{emoji} *{cat_name}*")
        lines.append("─" * 24)
        for item in items:
            name     = item["title"]
            variants = item.get("variants", [])
            if len(variants) == 1:
                lines.append(f"  • *{name}*  —  PKR {variants[0]['price']}")
            elif variants:
                lines.append(f"  • *{name}*")
                for v in variants:
                    lines.append(f"      ▸ {v['size']}  —  PKR {v['price']}")
            else:
                lines.append(f"  • *{name}*")

    footer_map = {
        "en": (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👆 *How to order:* Just type the dish name!\n"
            "Example: _'1kg Chicken Karahi'_ or _'Large BBQ Pizza'_\n\n"
            "💬 Questions? Just ask — I'm here! 😊"
        ),
        "ur": (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👆 *آرڈر کیسے دیں:* ڈش کا نام لکھیں!\n"
            "مثال: _'1kg چکن کڑاہی'_ یا _'Large BBQ پیزا'_\n\n"
            "💬 کوئی سوال؟ بس پوچھیں! 😊"
        ),
        "de": (
            "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👆 *Bestellen:* Einfach den Gerichtnamen eingeben!\n"
            "Beispiel: _'1kg Chicken Karahi'_ oder _'Large BBQ Pizza'_\n\n"
            "💬 Fragen? Einfach fragen! 😊"
        ),
    }
    lines.append(footer_map.get(lang, footer_map["en"]))
    return "\n".join(lines)


def _build_text_menu(products: List[Dict], lang: str = "en", title: str = "") -> str:
    """Build a beautifully formatted text menu grouped by category."""
    if not products:
        # v14.5: instead of "nothing available", show example menu
        return _build_full_example_menu(lang)

    grouped: Dict[str, List[Dict]] = {}
    for p in products:
        cat = p.get("category", "other").strip().lower() or "other"
        grouped.setdefault(cat, []).append(p)

    header_map = {
        "en": title or "🍽️ *Our Menu*\n━━━━━━━━━━━━━━━━━━━━━━━━",
        "ur": title or "🍽️ *ہمارا مینو*\n━━━━━━━━━━━━━━━━━━━━━━━━",
        "de": title or "🍽️ *Unsere Speisekarte*\n━━━━━━━━━━━━━━━━━━━━━━━━",
    }
    lines = [header_map.get(lang, header_map["en"]), ""]

    for cat, items in grouped.items():
        emoji      = _CATEGORY_EMOJI_MAP.get(cat, "🍽️")
        cat_display = cat.replace("_", " ").title()
        lines.append(f"{emoji} *{cat_display}*")
        lines.append("─" * 24)
        for item in items:
            name     = item.get("title", "").strip().title()
            variants = item.get("variants", [])
            if variants:
                if len(variants) == 1:
                    lines.append(f"  • *{name}*  —  PKR {int(variants[0]['price'])}")
                else:
                    lines.append(f"  • *{name}*")
                    for v in variants:
                        lines.append(f"      ▸ {v['size']}  —  PKR {int(v['price'])}")
            else:
                price = item.get("price", "—")
                lines.append(f"  • *{name}*  —  PKR {price}")
        lines.append("")

    footer_map = {
        "en": (
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👆 *How to order:* Just type the dish name!\n"
            "Example: _'1kg Karahi'_ or _'Large Zinger Burger'_\n"
            "💬 Need help choosing? Just ask! 😊"
        ),
        "ur": (
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👆 ڈش کا نام لکھ کر آرڈر دیں!\n"
            "مثال: _'1kg کڑاہی'_ یا _'زنگر برگر'_\n"
            "💬 مدد چاہیے؟ پوچھیں! 😊"
        ),
        "de": (
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👆 Einfach den Gerichtnamen eingeben!\n"
            "z.B. _'1kg Karahi'_ oder _'Zinger Burger'_\n"
            "💬 Brauchen Sie Hilfe? Fragen Sie einfach! 😊"
        ),
    }
    lines.append(footer_map.get(lang, footer_map["en"]))
    return "\n".join(lines)


def _build_full_price_menu(
    products: List[Dict],
    category_emoji: str = "🍽️",
    title: str = "Menu & Prices",
) -> str:
    lines = [f"{category_emoji} *{title}*\n━━━━━━━━━━━━━━━━━━━━━━━━\n"]
    for product in products:
        name = product.get("title", "Item").strip().title()
        lines.append(f"• *{name}*")
        variants = product.get("variants", [])
        if variants:
            for v in variants:
                lines.append(f"   ▸ {v.get('size', 'N/A')}  —  PKR {v.get('price', '?')}")
        else:
            price = product.get("price", "N/A")
            lines.append(f"   ▸ PKR {price}")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines).strip()


def _fuzzy_match_extra(query_word: str, extra_name: str, threshold: float = 0.75) -> bool:
    q = query_word.lower().strip()
    e = extra_name.lower().strip()
    if q in e or e in q:
        return True
    ratio = SequenceMatcher(None, q, e).ratio()
    return ratio >= threshold


def _extract_extras_from_text(text: str, extras_options: List[Dict]) -> List[str]:
    """Token-level matching so 'naan' matches 'Naan X2'."""
    q      = text.lower()
    chosen = []
    for e in extras_options:
        name       = e["name"].strip()
        name_lower = name.lower()

        if name_lower in q:
            chosen.append(name.strip().title())
            continue

        extra_tokens  = [t for t in re.findall(r'\w+', name_lower) if len(t) > 2]
        query_words   = re.findall(r'\w+', q)
        token_matched = False
        for et in extra_tokens:
            for qw in query_words:
                if _fuzzy_match_extra(qw, et):
                    token_matched = True
                    break
            if token_matched:
                break
        if token_matched:
            chosen.append(name.strip().title())
            continue

        extra_words   = name_lower.split()
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
# DATA LOADER
# ============================================================

_UNIVERSAL_CATEGORY_ALIASES: Dict[str, List[str]] = {
    "burger":  [
        "burger", "brgr", "برگر", "zinger", "cheeseburger", "double burger",
        "chicken burger", "beef burger", "crispy burger", "smash burger",
        "whopper", "mcburger", "spicy burger", "cheese burger",
    ],
    "pizza":   [
        "pizza", "پیزا", "پیزہ", "margherita", "pepperoni", "pizza slice",
        "tikka pizza", "bbq pizza", "pizza pie", "thin crust", "thick crust",
        "cheese pizza", "chicken pizza", "veggie pizza", "piza",
    ],
    "biryani": [
        "biryani", "بریانی", "baryani", "dum biryani", "chicken biryani",
        "beef biryani", "mutton biryani", "hyderabadi biryani", "sindhi biryani",
        "biryani rice", "biriyani", "biryaani", "briyani", "biriyan",
    ],
    "drinks":  [
        "drink", "مشروب", "cola", "juice", "coke", "pepsi", "lassi",
        "cold drink", "soda", "7up", "sprite", "fanta", "water", "سافٹ ڈرنک",
        "doodh", "milk", "chai", "tea", "coffee", "shake", "milkshake",
        "smoothie", "lemonade", "nimbu pani", "mango juice", "orange juice",
        "fresh juice", "cold coffee", "iced tea", "rooh afza",
    ],
    "dessert": [
        "dessert", "مٹھائی", "sweet", "cake", "kheer", "meetha",
        "halwa", "gulab jamun", "brownie", "mithai", "ice cream", "icecream",
        "kulfi", "rabri", "sewaiyan", "falooda", "pudding", "pastry",
        "muffin", "donut", "doughnut", "waffle", "gajar halwa",
    ],
    "karahi":  [
        "karahi", "کڑاہی", "karai", "karhai", "chicken karahi",
        "beef karahi", "mutton karahi", "dum karahi", "karahi gosht",
        "peshwari karahi", "balti", "kadai", "kadhai", "karai gosht",
    ],
    "rice":    [
        "rice", "چاول", "pulao", "fried rice", "plov", "chawal",
        "plain rice", "steamed rice", "zeera rice", "matar pulao",
        "kabuli pulao", "yakhni pulao",
    ],
    "rolls":   [
        "roll", "رول", "shawarma", "wrap", "paratha roll",
        "chicken roll", "beef roll", "seekh roll", "tikka roll",
        "spring roll", "egg roll",
    ],
    "chicken": [
        "chicken", "چکن", "murgh", "murg", "chkn",
        "grilled chicken", "fried chicken", "chicken pieces",
        "chicken tikka", "chicken boti", "chicken malai",
    ],
    "beef":    [
        "beef", "گوشت", "gosht", "gai ka gosht",
        "beef boti", "beef tikka", "beef seekh", "beaf",
    ],
    "mutton":  [
        "mutton", "lamb", "دنبہ", "bhed", "bakra",
        "mutton karahi", "mutton biryani", "mutton chops",
    ],
    "soup":    ["soup", "شوربہ", "shorba", "yakhni", "lentil soup", "dal soup"],
    "salad":   ["salad", "سلاد", "raita", "slaw"],
    "bread":   [
        "bread", "naan", "roti", "paratha", "روٹی", "نان", "پراٹھا",
        "chapati", "tandoori roti", "plain naan",
        "butter naan", "garlic naan", "puri", "bhatura",
    ],
    "shawarma": [
        "shawarma", "شوارمہ", "shwarma", "shaverma", "chicken shawarma",
        "beef shawarma",
    ],
    "sandwich": [
        "sandwich", "سینڈوچ", "sub", "club sandwich",
        "chicken sandwich", "grilled sandwich",
    ],
    "pasta":   [
        "pasta", "پاستا", "spaghetti", "macaroni", "penne",
        "fettuccine", "alfredo", "bolognese", "arabiata",
    ],
    "steak":   [
        "steak", "اسٹیک", "grilled", "bbq steak", "fillet",
        "beef steak", "chicken steak",
    ],
    "fish":    [
        "fish", "مچھلی", "seafood", "prawn", "shrimp",
        "fried fish", "grilled fish", "machli",
    ],
}


def _build_product_keyword_index():
    global PRODUCT_KEYWORD_INDEX
    PRODUCT_KEYWORD_INDEX = {}

    def _add(key: str, product: Dict):
        key = key.lower().strip()
        if not key or len(key) < 2:
            return
        if key not in PRODUCT_KEYWORD_INDEX:
            PRODUCT_KEYWORD_INDEX[key] = []
        pid = str(product.get("_id", id(product)))
        if not any(str(p.get("_id", id(p))) == pid for p in PRODUCT_KEYWORD_INDEX[key]):
            PRODUCT_KEYWORD_INDEX[key].append(product)

    for product in PRODUCTS_DATA:
        title    = product.get("title", "").strip()
        category = product.get("category", "").strip().lower()
        desc     = product.get("description", "").strip()

        _add(title, product)
        _add(title.lower(), product)
        for word in re.findall(r'\w+', title.lower()):
            if len(word) > 2:
                _add(word, product)
        if category:
            _add(category, product)
        for cat_key, aliases in _UNIVERSAL_CATEGORY_ALIASES.items():
            if cat_key == category or cat_key in title.lower() or cat_key in desc.lower():
                for alias in aliases:
                    _add(alias, product)
                    for tok in re.findall(r'\w+', alias.lower()):
                        if len(tok) > 2:
                            _add(tok, product)
        for word in re.findall(r'\w+', desc.lower()):
            if len(word) > 3:
                _add(word, product)

    logger.info(f"Product keyword index built: {len(PRODUCT_KEYWORD_INDEX)} keys "
                f"across {len(PRODUCTS_DATA)} products")


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
                "en": (
                    "Hey there! 👋 So happy you're here!\n\n"
                    "I'm your personal food assistant — think of me as your foodie friend "
                    "who knows every dish on our menu inside out. 🍽️\n\n"
                    "Whether you're craving something spicy, cheesy, or comforting — "
                    "I've got you covered. Just tell me what you're in the mood for!"
                ),
                "ur": (
                    "خوش آمدید! 👋 بہت خوشی ہوئی آپ کو دیکھ کر!\n\n"
                    "میں آپ کا ذاتی فوڈ اسسٹنٹ ہوں — آج کیا کھانا ہے؟\n"
                    "بس بتائیں، میں سب سنبھال لوں گا! 🍽️"
                ),
                "de": (
                    "Hallo! 👋 Schön, Sie zu sehen!\n\n"
                    "Ich bin Ihr persönlicher Essensassistent — "
                    "sagen Sie mir einfach, worauf Sie Appetit haben! 🍽️"
                ),
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

    exceptions  = BOT_DATA.get("delivery_time_exceptions", {})
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
    dc            = BOT_DATA.get("delivery_charges", {})
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
        return {
            "en": "🚚 Delivery: FREE 🎉",
            "ur": "🚚 ڈلیوری: مفت 🎉",
            "de": "🚚 Lieferung: KOSTENLOS 🎉",
        }.get(lang, "🚚 Delivery: FREE 🎉")
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
    "burger":   [
        "burger", "برگر", "brgr", "cheeseburger", "double burger", "zinger",
        "chicken burger", "beef burger", "smash burger", "crispy burger",
    ],
    "pizza":    [
        "pizza", "پیزا", "پیزہ", "margherita", "pepperoni", "pizza slice",
        "tikka pizza", "bbq pizza", "piza",
    ],
    "biryani":  [
        "biryani", "بریانی", "dum biryani", "chicken biryani", "beef biryani",
        "baryani", "biriyani", "briyani", "biryaani",
    ],
    "drinks":   [
        "drink", "مشروب", "juice", "cola", "water", "سافٹ ڈرنک", "lassi",
        "coke", "pepsi", "7up", "sprite", "fanta", "soda", "cold drink",
        "doodh", "milk", "chai", "tea", "coffee", "shake", "milkshake",
        "smoothie", "lemonade", "nimbu pani", "fresh juice", "cold coffee",
    ],
    "dessert":  [
        "dessert", "مٹھائی", "cake", "kheer", "halwa", "gulab jamun",
        "brownie", "meetha", "sweet", "mithai", "ice cream", "kulfi",
        "falooda", "rabri", "gajar halwa",
    ],
    "karahi":   [
        "karahi", "کڑاہی", "chicken karahi", "beef karahi", "mutton karahi",
        "karhai", "dum karahi", "karai", "kadai", "karahi gosht",
    ],
    "rice":     ["rice", "چاول", "pulao", "plov", "fried rice", "chawal", "zeera rice"],
    "rolls":    ["roll", "رول", "shawarma", "wrap", "paratha roll", "spring roll"],
    "chicken":  ["chicken", "چکن", "murgh", "murg", "grilled chicken", "fried chicken"],
    "beef":     ["beef", "گوشت", "gosht", "beef boti", "beef tikka"],
    "mutton":   ["mutton", "lamb", "دنبہ", "bhed"],
    "fish":     ["fish", "مچھلی", "seafood", "prawn", "machli"],
    "bread":    [
        "naan", "roti", "paratha", "bread", "روٹی", "نان", "chapati",
        "tandoori roti", "butter naan", "garlic naan",
    ],
    "shawarma": ["shawarma", "شوارمہ", "shwarma", "shaverma"],
    "pasta":    ["pasta", "پاستا", "spaghetti", "macaroni", "penne"],
    "sandwich": ["sandwich", "سینڈوچ", "sub", "club sandwich"],
    "soup":     ["soup", "شوربہ", "shorba", "yakhni"],
    "salad":    ["salad", "سلاد", "raita"],
    "steak":    ["steak", "اسٹیک", "grilled", "bbq steak"],
}

INTENT_KEYWORDS = {
    "discount":  ["discount", "sale", "deal", "offer", "cheap", "سستا", "رعایت", "rabatt",
                  "special offer", "promo", "coupon"],
    "order":     ["order", "آرڈر", "buy", "place order", "i want", "مجھے چاہیے", "bestellen",
                  "chahiye", "dena", "lena", "add", "mujhe", "give me", "i'll have",
                  "i'd like", "can i get", "get me", "send me", "bhai dena", "yaar dena",
                  "ek dena", "do dena", "lao", "manga", "mangwao", "order karo",
                  "order now", "order again", "want to order", "want"],
    "menu":      ["menu", "مینو", "menü", "what do you have", "show menu", "list", "items",
                  "all items", "show all", "kya hai", "kya milta", "aapke paas kya",
                  "what's available", "what do you serve", "show items", "menu dikhao",
                  "mujhe menu", "kya kya hai", "dekhna chahta"],
    "price":     ["price", "قیمت", "preis", "cost", "how much", "kitna", "rate",
                  "all prices", "all flavours", "all flavors", "price list", "rates",
                  "kitne ka", "kitni", "kya rate", "daam", "qeemat"],
    "greeting":  ["hi", "hello", "hey", "assalam", "السلام", "hallo", "guten tag", "سلام",
                  "start", "begin", "aoa", "aslam", "good morning", "good evening",
                  "good afternoon", "salam", "as salam", "walaikum"],
    "address":   ["address", "پتہ", "adresse", "location", "deliver to", "my address"],
    "status":    ["status", "where", "order status", "track", "delivered", "pending",
                  "where is my order", "track order", "mera order", "kahan hai order"],
    "cancel":    [
        "cancel", "منسوخ", "stornieren", "nahi chahiye", "remove order",
        "delete order", "hatao", "band karo", "order cancel", "cancel order",
        "delete", "remove", "clear order", "order delete", "order hatao",
        "order band", "mujhe nahi chahiye", "order mat karo",
        "order nahi", "order wapas", "order bhool jao",
    ],
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
    "inquiry": [
        "tell me about", "what is", "describe", "kya hai", "kaisa hai",
        "batao", "bataiye", "details", "more info", "information about",
        "what sizes", "what flavors", "what options", "kaunse size",
        "kya varieties", "available sizes",
    ],
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
        "last_shown_product":  None,
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

_ORDER_NOISE_PREFIXES = re.compile(
    r'^(i\s+want\s+to\s+order|i\s+want\s+to|i\s+want|want\s+to\s+order|'
    r'please\s+give\s+me|please|kindly|mujhe\s+chahiye|mujhe|chahiye|'
    r'dena|lena|please\s+give|give\s+me|add\s+(?=\d)|add\s+(?=[a-zA-Z])|'
    r'can\s+i\s+get|get\s+me|send\s+me|i\'ll\s+have|i\s+would\s+like|i\'d\s+like|'
    r'bhai\s+dena|yaar\s+dena|bhai|yaar|lao|la\s+do|mangwao|order\s+karo|'
    r'mujhe\s+ek|mujhe\s+do|ek|do|teen)\s+',
    re.IGNORECASE,
)


def _extract_quantity(token: str) -> int:
    t       = token.strip().lower()
    t_clean = re.sub(r'(st|nd|rd|th)$', '', t)
    if t_clean.isdigit():
        val = int(t_clean)
        if re.search(r'(st|nd|rd|th)$', t):
            return 1
        return val
    return QUANTITY_WORDS.get(t, QUANTITY_WORDS.get(t_clean, 1))


def _find_product_by_query(query: str) -> Optional[Dict]:
    if not query:
        return None

    q       = query.lower().strip()
    q_clean = _ORDER_NOISE_PREFIXES.sub("", q).strip()

    candidates: Dict[str, Dict] = {}

    def _add_candidate(p: Dict):
        pid = str(p.get("_id", id(p)))
        candidates[pid] = p

    for lookup in [q_clean, q]:
        if lookup in PRODUCT_KEYWORD_INDEX:
            for p in PRODUCT_KEYWORD_INDEX[lookup]:
                _add_candidate(p)

    words = re.findall(r'\w+', q_clean)
    for word in words:
        if len(word) > 2 and word in PRODUCT_KEYWORD_INDEX:
            for p in PRODUCT_KEYWORD_INDEX[word]:
                _add_candidate(p)

    if not candidates:
        for p in PRODUCTS_DATA:
            candidates[str(p.get("_id", id(p)))] = p

    if not candidates:
        return None

    def _score(product: Dict) -> float:
        title    = product.get("title", "").lower()
        category = product.get("category", "").lower()
        desc     = product.get("description", "").lower()
        score    = 0.0

        if q_clean == title or q == title:
            score += 20
        if q_clean in title or title in q_clean:
            score += 10

        q_words = set(re.findall(r"\w+", q_clean))
        t_words = set(re.findall(r"\w+", title))
        overlap = q_words & t_words
        score  += len(overlap) * 4

        for cat, kws in CATEGORY_KEYWORDS.items():
            if cat == category and any(kw in q_clean for kw in kws):
                score += 6

        if category and category in q_clean:
            score += 8

        d_words = set(re.findall(r"\w+", desc))
        score  += len(q_words & d_words) * 1

        score += float(product.get("trending_score", 0)) * 0.5
        score += float(product.get("rating", 0)) * 0.3

        return score

    best_product = max(candidates.values(), key=_score)
    best_score   = _score(best_product)

    return best_product if best_score > 0 else None


def _is_product_query(q: str) -> bool:
    q_clean = _ORDER_NOISE_PREFIXES.sub("", q.lower().strip()).strip()
    words   = re.findall(r'\w+', q_clean)

    for word in words:
        if len(word) > 2 and word in PRODUCT_KEYWORD_INDEX:
            return True

    if q_clean in PRODUCT_KEYWORD_INDEX:
        return True

    for kws in CATEGORY_KEYWORDS.values():
        if any(kw in q_clean for kw in kws):
            return True

    return False


def _products_by_category(category_key: str) -> List[Dict]:
    return [p for p in PRODUCTS_DATA if p.get("category", "").lower() == category_key.lower()]


def parse_price_range(query: str) -> Dict[str, float]:
    q      = query.lower().replace("rs", "").replace("pkr", "").replace("$", "").replace("€", "")
    result: Dict[str, float] = {}
    under  = re.search(r"(under|below|less than|کم|unter)\s*(\d+)", q)
    over   = re.search(r"(over|above|greater than|زیادہ|über)\s*(\d+)", q)
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
        pp       = variants[0]["price"] if variants else float(str(product.get("price", "0")).replace(",", "").strip() or 0)
        if "max" in price_range and pp <= price_range["max"]: s += 2.0
        if "min" in price_range and pp >= price_range["min"]: s += 2.0
    except: pass
    return s


def filter_products(query: str) -> List[Dict]:
    price_range = parse_price_range(query)
    scored      = [{"p": p, "s": score_product(query, p, price_range)} for p in PRODUCTS_DATA]
    return [x["p"] for x in sorted(scored, key=lambda x: x["s"], reverse=True) if x["s"] > 0.0][:8]

# ============================================================
# MULTI-ITEM PARSERS
# ============================================================

def _parse_multi_size_from_text(text: str, product: Dict) -> List[Dict]:
    """
    v14.4 FIX 1: Quantity tokens at the start of each part are stripped
    before size-hint matching, so "5 Small Pizza and 5 Medium Pizza"
    correctly produces two sized entries.
    """
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

        # strip leading quantity token first (v14.4 FIX 1)
        qty         = 1
        qty_pattern = re.compile(
            r'^(\d+(?:st|nd|rd|th)?|' +
            '|'.join(re.escape(k) for k in sorted(QUANTITY_WORDS.keys(), key=len, reverse=True)) +
            r')\s+',
            re.IGNORECASE
        )
        qty_match = qty_pattern.match(part)
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

        qty       = 1
        qty_match = re.match(
            r'^(\d+(?:st|nd|rd|th)?|' + '|'.join(re.escape(k) for k in QUANTITY_WORDS.keys()) + r')\s+',
            part, re.IGNORECASE
        )
        if qty_match:
            qty         = _extract_quantity(qty_match.group(1))
            part_no_qty = part[qty_match.end():].strip()
        else:
            part_no_qty = part

        size_hint  = ""
        part_clean = part_no_qty
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

        product = (
            _find_product_by_query(part_clean) or
            _find_product_by_query(part_no_qty) or
            _find_product_by_query(part)
        )
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
    session["order_count"]      = session.get("order_count", 0) + 1
    session["last_order_items"] = cart_items

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
    """
    Sends a well-formatted TEXT menu (not WhatsApp interactive list).
    Interactive lists were causing rendering issues on many devices.
    """
    menu_text = _build_text_menu(items, lang)
    await send_whatsapp_text(to, menu_text)


async def send_whatsapp_buttons(to: str, body: str, buttons: List[str]):
    """
    v14.5 NEW 2: Sends WhatsApp interactive button message.
    Used for cart display, product cards, and confirmation flows.
    Max 3 buttons (WhatsApp API limit).
    """
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
    headers_h = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type":  "application/json",
    }
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
    """
    v14.4 FIX 5: Guard against firing on pure multi-size order strings like
    '5 small pizza and 5 medium pizza'.
    """
    multi_size_order_pattern = re.compile(
        r'\d+\s+(?:small|medium|large|regular|xl|xxl|half|full|'
        r'half\s*plate|full\s*plate|family\s*pack|\d+\.?\d*\s*kg)',
        re.IGNORECASE,
    )
    if multi_size_order_pattern.search(q) and _is_product_query(q):
        return False

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
    affirmatives = {
        "yes", "sure", "ok", "okay", "haan", "theek hai", "bilkul",
        "zaroor", "absolutely", "go ahead", "proceed", "yep", "yeah",
        "finalize", "done", "correct", "right", "ji", "ji haan",
    }
    return q.strip().lower() in affirmatives


def _is_pure_greeting(q: str) -> bool:
    greeting_only = {
        "hi", "hello", "hey", "salam", "assalam", "aoa", "aslam",
        "hallo", "guten tag", "good morning", "good evening", "good afternoon",
        "as salam", "walaikum", "start", "begin",
    }
    q_stripped = q.strip().lower()
    if q_stripped in greeting_only:
        return True
    if any(kw in q_stripped for kw in greeting_only) and not _is_product_query(q_stripped):
        return True
    return False


def _is_order_now_button(q: str) -> bool:
    cleaned = re.sub(r'[✅📋🛒🔄]', '', q).strip().lower()
    return cleaned in {"order now", "order again", "reorder"}


def _is_post_order_small_talk(q: str, session: Dict) -> bool:
    """Returns True when the user sends a short acknowledgement / thanks
    after their order has been placed and no active cart exists."""
    if session.get("step", 0) != 0:
        return False
    if session.get("cart") or session.get("pending_order"):
        return False
    small_talk_words = {
        "ok", "okay", "k", "kk", "alright", "got it", "noted",
        "cool", "great", "nice", "done", "fine", "perfect",
        "👍", "👌", "😊", "🙏",
        "thanks", "thank you", "thankyou", "shukriya",
        "jazakallah", "jazak allah", "شکریہ",
        "theek hai", "theek", "acha", "accha", "achha",
    }
    q_stripped = q.strip().lower()
    if q_stripped in small_talk_words:
        return True
    if len(q_stripped) <= 15 and not _is_product_query(q_stripped):
        for word in small_talk_words:
            if word in q_stripped:
                return True
    return False

# ============================================================
# SMART FALLBACK (Claude AI)
# ============================================================

async def _smart_fallback(from_number: str, user_message: str, lang: str) -> str:
    if not ANTHROPIC_API_KEY:
        return _static_fallback(lang)

    product_names = [p.get("title", "") for p in PRODUCTS_DATA[:20]]
    product_list  = ", ".join(product_names) if product_names else "various delicious dishes"

    system_prompt = (
        f"You are Zara, a warm, friendly, and professional WhatsApp restaurant assistant. "
        f"You speak like a real human restaurant staff member — conversational, caring, and enthusiastic about food. "
        f"The restaurant serves: {product_list}. "
        f"Respond in {'Urdu' if lang == 'ur' else 'German' if lang == 'de' else 'English'}, "
        f"keeping replies under 3 sentences. "
        f"Use relevant food emojis naturally. "
        f"If someone asks about a dish, describe it with genuine enthusiasm and gently guide them to order. "
        f"If confused, apologise warmly and redirect to food ordering. "
        f"Never sound robotic or use formal language. "
        f"If completely unrelated to food/restaurant, say warmly that you specialise in food only."
    )

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":          ANTHROPIC_API_KEY,
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
    """v14.5: Warm, human-friendly static fallback messages."""
    fallback = {
        "en": (
            "Hmm, I'm not quite sure I caught that — sorry about that! 😅\n\n"
            "Here's how I can help you:\n\n"
            "🍽️ *Show menu* — browse everything we've got\n"
            "📦 *Order [dish]* — e.g. _'Zinger Burger'_ or _'1kg Karahi'_\n"
            "💰 *All prices* — see our full price list\n"
            "📍 *Order status* — track your latest order\n\n"
            "Just tell me what you're craving and I'll sort it out! 😊"
        ),
        "ur": (
            "معذرت، سمجھ نہیں آیا 😅 میں ان چیزوں میں مدد کر سکتا ہوں:\n\n"
            "🍽️ *مینو دکھائیں* — سب آئٹم دیکھیں\n"
            "📦 *آرڈر [ڈش]* — جیسے _'بریانی'_ یا _'1kg کڑاہی'_\n"
            "💰 *تمام قیمتیں* — قیمت کی فہرست\n"
            "📍 *آرڈر اسٹیٹس* — ٹریکنگ\n\n"
            "بس بتائیں، میں مدد کروں گا! 😊"
        ),
        "de": (
            "Das habe ich leider nicht verstanden — entschuldigung! 😅\n\n"
            "So kann ich helfen:\n\n"
            "🍽️ *Menü anzeigen* — alle Gerichte\n"
            "📦 *[Gericht] bestellen* — z.B. _'Zinger Burger'_\n"
            "💰 *Alle Preise* — komplette Preisliste\n"
            "📍 *Bestellstatus* — verfolgen\n\n"
            "Einfach eingeben, was Sie möchten! 😊"
        ),
    }
    return fallback.get(lang, fallback["en"])

# ============================================================
# BOT FLOW HELPERS
# ============================================================

async def _ask_size(to: str, product: Dict, lang: str):
    variants  = product.get("variants", [])
    size_list = "\n".join(f"  ▸ *{v['size']}*  —  PKR {v['price']}" for v in variants)
    name      = product.get("title", "Item").strip().title()
    msgs = {
        "en": (
            f"📏 Great choice! *{name}* comes in these sizes:\n\n"
            f"{size_list}\n\n"
            f"Which size works for you? Just type it — e.g. _'Large'_ or _'1kg'_ 😊"
        ),
        "ur": (
            f"📏 بہترین انتخاب! *{name}* کے سائز:\n\n"
            f"{size_list}\n\n"
            f"کون سا سائز چاہیے؟ لکھیں!"
        ),
        "de": (
            f"📏 Tolle Wahl! *{name}* gibt es in diesen Größen:\n\n"
            f"{size_list}\n\n"
            f"Welche Größe darf es sein?"
        ),
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))


async def _ask_spice(to: str, product: Dict, lang: str) -> bool:
    spice_levels = product.get("spice_levels", [])
    if not spice_levels:
        return False
    options = "  •  ".join(s.strip().title() for s in spice_levels)
    name    = product.get("title", "Item").strip().title()
    msgs = {
        "en": (
            f"🌶️ Love it! Now, how spicy would you like your *{name}*?\n\n"
            f"  {options}\n\n"
            f"Pick your heat level! 🔥"
        ),
        "ur": (
            f"🌶️ *{name}* کے لیے مسالے کی سطح بتائیں:\n"
            f"  {options}"
        ),
        "de": (
            f"🌶️ Schärfegrad für *{name}*:\n"
            f"  {options}"
        ),
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _ask_extras(to: str, product: Dict, lang: str) -> bool:
    extras = product.get("extras", [])
    if not extras:
        return False
    extras_list = "\n".join(f"  ▸ *{e['name'].strip().title()}*  +PKR {e['price']}" for e in extras)
    name        = product.get("title", "Item").strip().title()
    msgs = {
        "en": (
            f"➕ Almost done! Would you like to add anything to your *{name}*?\n\n"
            f"{extras_list}\n\n"
            f"_(Type the name(s) to add, or just say *no* to skip)_"
        ),
        "ur": (
            f"➕ *{name}* کے ساتھ کچھ اضافی چاہیے؟\n"
            f"{extras_list}\n\n"
            f"(نام لکھیں یا *no* لکھیں)"
        ),
        "de": (
            f"➕ Extras für *{name}*?\n"
            f"{extras_list}\n\n"
            f"(Namen oder *nein* eingeben)"
        ),
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _ask_multi_spice(to: str, items_needing_spice: List[Dict], product: Dict, lang: str):
    spice_levels = product.get("spice_levels", [])
    options      = "  •  ".join(s.strip().title() for s in spice_levels)
    name         = product.get("title", "Item").strip().title()

    lines = []
    for item in items_needing_spice:
        mv        = item["matched_variant"]
        size      = mv.get("size", "")
        qty       = item.get("qty", 1)
        qty_label = f" ×{qty}" if qty > 1 else ""
        lines.append(f"  ▸ *{size}*{qty_label}")

    body = "\n".join(lines)
    msgs = {
        "en": (
            f"🌶️ Almost ready! What spice level for your *{name}*?\n\n"
            f"{body}\n\n"
            f"Options: {options}\n\n"
            f"_(e.g. 'Small Spicy, Medium Mild' — or just 'Spicy' to apply to all)_"
        ),
        "ur": (
            f"🌶️ *{name}* کے لیے مسالے کی سطح:\n"
            f"{body}\n\n"
            f"آپشن: {options}\n"
            f"(جیسے: 'Small Spicy, Medium Mild' یا سب کے لیے 'Spicy')"
        ),
        "de": (
            f"🌶️ Schärfegrad für *{name}*:\n"
            f"{body}\n\n"
            f"Optionen: {options}\n"
            f"(z.B. 'Small Spicy, Medium Mild' oder 'Spicy' für alle)"
        ),
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
            "en": (
                f"{summary}\n\n"
                f"✨ Looking good! Ready to place this order, or want to add something else?"
            ),
            "ur": f"{summary}\n\n✨ آرڈر تصدیق کریں یا مزید شامل کریں؟",
            "de": f"{summary}\n\n✨ Sieht gut aus! Bestätigen oder mehr hinzufügen?",
        }
        # v14.5 NEW 2: Always include "Add More" in cart confirmation buttons
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
            "en": (
                f"{summary}\n\n"
                f"✨ All looking great! Want to confirm this order, or add something more? 😊"
            ),
            "ur": f"{summary}\n\n✨ آرڈر تصدیق کریں یا مزید شامل کریں؟",
            "de": f"{summary}\n\n✨ Fertig! Bestätigen oder mehr hinzufügen?",
        }
        # v14.5 NEW 2: "Add More" button always present
        await send_whatsapp_buttons(
            from_num,
            confirm_msgs.get(lang, confirm_msgs["en"]),
            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
        )
    else:
        session["step"] = 4
        ask_addr = {
            "en": (
                "📍 Perfect! Just one last thing — where should we deliver this?\n\n"
                "_(Please share your full address: house no., street, area, city)_"
            ),
            "ur": "📍 بہترین! اب اپنا مکمل پتہ دیں (مکان نمبر، گلی، علاقہ، شہر):",
            "de": "📍 Fast geschafft! Bitte vollständige Lieferadresse angeben:",
        }
        await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))


async def _handle_single_item_order(from_number: str, text: str, lang: str) -> bool:
    session = get_user_session(from_number)

    p = _find_product_by_query(text)

    if not p:
        products = filter_products(text)
        p        = products[0] if products else None

    if not p:
        return False

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
                    "en": f"{summary}\n\n✨ Ready to place the order? 😊",
                    "ur": f"{summary}\n\n✨ آرڈر تصدیق کریں یا مزید شامل کریں؟",
                    "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
                }
                await send_whatsapp_buttons(
                    from_number,
                    confirm_msgs.get(lang, confirm_msgs["en"]),
                    ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                )
                session["step"] = 5
            return True

        session["step"] = 1
        await _ask_size(from_number, p, lang)
    else:
        name_msg = {
            "en": f"🎉 Nice choice! *{dish_name}* — PKR {int(base_price)} added to your order.",
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
        emoji    = _CATEGORY_EMOJI_MAP.get(category, "🍽️")
        title_map = {
            "en": f"{emoji} {cat_name} Menu & Prices",
            "ur": f"{emoji} {cat_name} مینو اور قیمتیں",
            "de": f"{emoji} {cat_name} Menü & Preise",
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
        # v14.5: Show example menu when no products in DB
        example_menu_text = _build_full_example_menu(lang)
        await send_whatsapp_text(from_number, example_menu_text)
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
                        "en": f"{summary}\n\n✨ Ready to confirm? 😊",
                        "ur": f"{summary}\n\n✨ آرڈر تصدیق کریں یا مزید شامل کریں؟",
                        "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
                    }
                    await send_whatsapp_buttons(
                        from_number,
                        confirm_msgs.get(lang, confirm_msgs["en"]),
                        ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                    )
                    session["step"] = 5
                    return True

    cart_items_direct = list(session.get("cart", []))
    pending_queue     = []

    for group in groups:
        product      = group["product"]
        items        = group["items"]
        variants     = product.get("variants", [])
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
            "en": f"{summary}\n\n✨ All set! Want to confirm? 😊",
            "ur": f"{summary}\n\n✨ آرڈر تصدیق کریں یا مزید شامل کریں؟",
            "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
        }
        await send_whatsapp_buttons(
            from_number,
            confirm_msgs.get(lang, confirm_msgs["en"]),
            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
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

        q       = msg_text.lower().strip()
        q_clean = re.sub(r'[^\w\s\u0600-\u06FF]', '', q).strip()
        step    = session.get("step", 0)

        _track({"total_searches": 1, f"supported_languages.{lang}": 1})

        # ═══════════════════════════════════════════════════════
        # PRIORITY 0 — "new order" → reset cart immediately
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["new_order"]):
            reset_for_new_order(session)
            last_addr = session.get("last_address")
            addr_hint_map = {
                "en": f"\n\n📍 Last delivery address: _{last_addr}_\n(Type *same* to reuse it)" if last_addr else "",
                "ur": f"\n\n📍 پرانا پتہ: _{last_addr}_\n(*same* لکھیں دوبارہ استعمال کے لیے)" if last_addr else "",
                "de": f"\n\n📍 Letzte Adresse: _{last_addr}_\n(*same* zum Wiederverwenden)" if last_addr else "",
            }
            addr_hint = addr_hint_map.get(lang, addr_hint_map["en"])
            new_order_msg = {
                "en": (
                    f"🆕 Fresh start — let's get you something delicious! 🍽️\n"
                    f"What are you craving today?{addr_hint}"
                ),
                "ur": f"🆕 نیا آرڈر! کیا آرڈر کرنا ہے؟ 🍽️{addr_hint}",
                "de": f"🆕 Neue Bestellung! Was möchten Sie heute? 🍽️{addr_hint}",
            }
            await send_whatsapp_buttons(
                from_num,
                new_order_msg.get(lang, new_order_msg["en"]),
                ["View Menu 📋", "Order Again 🔄", "Contact Us 📞"],
            )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # "Order Now" / "Order Again" button handler
        # ═══════════════════════════════════════════════════════
        if _is_order_now_button(q):
            last_product = session.get("last_shown_product")
            last_items   = session.get("last_order_items", [])

            if "again" in q or "reorder" in q:
                if last_items:
                    session["cart"] = list(last_items)
                    session["step"] = 5
                    total   = _recalc_cart(last_items)
                    summary = _build_cart_summary(last_items, total, lang)
                    reorder_msg = {
                        "en": (
                            f"🔄 Here's your last order:\n\n"
                            f"{summary}\n\n"
                            f"Want to go ahead with this, or make any changes? 😊"
                        ),
                        "ur": f"🔄 آپ کا پرانا آرڈر:\n\n{summary}\n\n👉 تصدیق کریں یا تبدیل کریں؟",
                        "de": f"🔄 Ihre letzte Bestellung:\n\n{summary}\n\n👉 Bestätigen oder ändern?",
                    }
                    await send_whatsapp_buttons(
                        from_num,
                        reorder_msg.get(lang, reorder_msg["en"]),
                        ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                    )
                    return JSONResponse({"status": "ok"})

            if last_product:
                reset_for_new_order(session)
                handled = await _handle_single_item_order(from_num, last_product.get("title", ""), lang)
                if handled:
                    return JSONResponse({"status": "ok"})

            ask_what = {
                "en": "Sure! What would you like to order today? 🍽️\n_(Type any dish name or 'show menu')_",
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
                    "en": (
                        "🗑️ Done! I've cancelled your order — no problem at all.\n\n"
                        "Whenever you're ready to order again, just let me know! 😊"
                    ),
                    "ur": "🗑️ آرڈر منسوخ کر دیا! جب چاہیں دوبارہ آرڈر دے سکتے ہیں 🍽️",
                    "de": "🗑️ Erledigt — Bestellung storniert! Einfach wieder melden, wenn Sie bestellen möchten.",
                }
                await send_whatsapp_buttons(
                    from_num,
                    cancel_msg.get(lang, cancel_msg["en"]),
                    ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"],
                )
            else:
                no_order_msg = {
                    "en": (
                        "Looks like there's nothing active to cancel right now 😊\n\n"
                        "Whenever you're ready to order, just tell me what you'd like!"
                    ),
                    "ur": "ابھی کوئی فعال آرڈر نہیں ہے 😊 جب آرڈر کرنا ہو بتائیں!",
                    "de": "Aktuell gibt es nichts zu stornieren 😊 Sagen Sie mir einfach, wenn Sie bestellen möchten!",
                }
                await send_whatsapp_buttons(
                    from_num,
                    no_order_msg.get(lang, no_order_msg["en"]),
                    ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"],
                )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # Post-order small talk guard
        # ═══════════════════════════════════════════════════════
        if _is_post_order_small_talk(q, session):
            order_count = session.get("order_count", 0)
            if order_count > 0:
                follow_up = {
                    "en": (
                        "😊 Wonderful! Your order is on its way — "
                        "our team is working on it right now!\n\n"
                        "Can I help you with anything else?"
                    ),
                    "ur": "😊 بہت خوب! آپ کا آرڈر راستے میں ہے۔\n\nاور کچھ چاہیے؟",
                    "de": "😊 Freut mich! Ihre Bestellung ist unterwegs.\n\nKann ich noch helfen?",
                }
            else:
                follow_up = {
                    "en": "😊 Of course! Just let me know whenever you're ready to order. 🍽️",
                    "ur": "😊 جی ضرور! جب آرڈر کرنا ہو بتائیں۔",
                    "de": "😊 Natürlich! Sagen Sie mir, wenn Sie bestellen möchten.",
                }
            await send_whatsapp_buttons(
                from_num,
                follow_up.get(lang, follow_up["en"]),
                ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"],
            )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # Thanks mid-flow (step 0 only)
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["thanks"]) and step == 0 and not _is_product_query(q):
            thanks_msg = {
                "en": (
                    "You're so welcome — it's truly my pleasure! 😊\n\n"
                    "• *Show menu* — browse all our dishes\n"
                    "• *Place order* — order your favourite food\n\n"
                    "Anything else I can do for you? 🍽️"
                ),
                "ur": "خوشی ہوئی! 😊 اور کچھ چاہیے؟\n\n• *مینو دکھائیں*\n• *آرڈر دیں*",
                "de": "Gern geschehen! 😊 Kann ich noch helfen?\n\n• *Menü anzeigen*\n• *Bestellen*",
            }
            await send_whatsapp_text(from_num, thanks_msg.get(lang, thanks_msg["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 1 — SIZE
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
                                "en": f"{summary}\n\n✨ Ready to confirm? 😊",
                                "ur": f"{summary}\n\n✨ تصدیق کریں یا مزید شامل کریں؟",
                                "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
                            }
                            await send_whatsapp_buttons(
                                from_num,
                                confirm_msgs.get(lang, confirm_msgs["en"]),
                                ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                            )
                    return JSONResponse({"status": "ok"})

            matched = _match_variant(variants, msg_text)
            if not matched and variants:
                sizes_str = " / ".join(v["size"] for v in variants)
                size_err = {
                    "en": (
                        f"Hmm, I didn't quite catch that size 🤔\n\n"
                        f"Please choose from:\n*{sizes_str}*\n\n"
                        f"Just type the size — e.g. _'Large'_ or _'1kg'_"
                    ),
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
                            "en": (
                                "📍 Almost there! Just need your delivery address.\n\n"
                                "_(House no., street, area, city — the more detail the better!)_"
                            ),
                            "ur": "📍 بہترین! اپنا مکمل پتہ دیں:",
                            "de": "📍 Fast geschafft! Lieferadresse angeben:",
                        }
                        await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 2 — SPICE
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
                        "en": (
                            "📍 Great choice! Now — where are we delivering to?\n\n"
                            "_(House no., street, area, city)_"
                        ),
                        "ur": "📍 اپنا مکمل پتہ دیں:",
                        "de": "📍 Lieferadresse angeben:",
                    }
                    await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 3 — EXTRAS
        # ═══════════════════════════════════════════════════════
        if step == 3:
            po             = session.get("pending_order", {})
            extras_options = po.get("extras_options", [])

            if any(kw in q for kw in INTENT_KEYWORDS["show_total"]):
                current_total = po.get("price", 0)
                total_msg = {
                    "en": (
                        f"💰 Your current total: *PKR {int(current_total)}*\n\n"
                        f"Would you like to add any extras to your *{po.get('dish','')}*?\n"
                        f"_(Type name(s) or say 'no')_"
                    ),
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
                        "en": (
                            "⚠️ I don't have a previous address saved for you yet.\n\n"
                            "Could you type your full delivery address?"
                        ),
                        "ur": "⚠️ پرانا پتہ نہیں ملا۔ اپنا مکمل پتہ لکھیں۔",
                        "de": "⚠️ Keine frühere Adresse. Bitte vollständige Adresse eingeben.",
                    }
                    await send_whatsapp_text(from_num, no_addr.get(lang, no_addr["en"]))
                    return JSONResponse({"status": "ok"})
            else:
                address_candidate = extract_address(msg_text) or msg_text.strip()
                if not _is_valid_address(address_candidate):
                    retry_addr = {
                        "en": (
                            "📍 I need a bit more detail for the address — want to make sure we find you! 😊\n\n"
                            "Please share: *House no., Street, Area, City*\n"
                            "Example: _House 12, Block B, Gulshan, Karachi_"
                        ),
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

            cart_items_snap = list(session["cart"])
            reset_cart_only(session)

            if order_id == "db_error":
                db_err = {
                    "en": (
                        "⚠️ Oh no — something went wrong on our end placing your order.\n\n"
                        "Please try again in a moment, or contact us directly!"
                    ),
                    "ur": "⚠️ معذرت، آرڈر دینے میں مسئلہ ہوا۔ دوبارہ کوشش کریں۔",
                    "de": "⚠️ Entschuldigung, Fehler bei der Bestellung. Bitte erneut versuchen.",
                }
                await send_whatsapp_text(from_num, db_err.get(lang, db_err["en"]))
                return JSONResponse({"status": "ok"})

            first_item  = cart_items_snap[0] if cart_items_snap else {}
            dish_name   = first_item.get("title", po.get("dish", "Item")).strip().title()
            size_disp   = first_item.get("size", po.get("size", "N/A"))
            spice_disp  = first_item.get("spice", po.get("spice", "")) or "Default"
            extras_disp = ", ".join(first_item.get("extras", po.get("extras", []))) or {
                "en": "None", "ur": "کچھ نہیں", "de": "Keine"
            }.get(lang, "None")

            item_category = first_item.get("category", po.get("category", ""))
            delivery_time = get_delivery_time(item_category)
            dc_line       = _delivery_charge_info_text(delivery_charge, lang)

            conf = {
                "en": (
                    f"✅ *Order Confirmed!* 🎉\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🍽️ *{dish_name}*\n"
                    f"📏 Size: {size_disp}\n"
                    f"🌶️ Spice: {spice_disp}\n"
                    f"➕ Extras: {extras_disp}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Subtotal: PKR {int(subtotal)}\n"
                    f"{dc_line}\n"
                    f"💳 *Grand Total: PKR {int(grand_total)}*\n\n"
                    f"📍 Delivering to:\n_{address}_\n\n"
                    f"🔖 Order ID: *#{order_id[-6:]}*\n"
                    f"⏱️ Estimated delivery: *{delivery_time}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"We're on it! Our team is preparing your order right now. 🙌\n"
                    f"Type *new order* anytime to order again 😊"
                ),
                "ur": (
                    f"✅ *آرڈر تصدیق ہوگیا!* 🎉\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🍽️ *{dish_name}*\n"
                    f"📏 سائز: {size_disp}\n"
                    f"🌶️ مسالہ: {spice_disp}\n"
                    f"➕ اضافی: {extras_disp}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 سب ٹوٹل: PKR {int(subtotal)}\n"
                    f"{dc_line}\n"
                    f"💳 *کل رقم: PKR {int(grand_total)}*\n\n"
                    f"📍 پتہ: _{address}_\n"
                    f"🔖 آرڈر نمبر: *#{order_id[-6:]}*\n"
                    f"⏱️ تخمینی ڈلیوری: *{delivery_time}*\n\n"
                    f"جلد پہنچائیں گے! نیا آرڈر دینے کے لیے *new order* لکھیں۔"
                ),
                "de": (
                    f"✅ *Bestellung bestätigt!* 🎉\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🍽️ *{dish_name}*\n"
                    f"📏 Größe: {size_disp}\n"
                    f"🌶️ Schärfe: {spice_disp}\n"
                    f"➕ Extras: {extras_disp}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Zwischensumme: PKR {int(subtotal)}\n"
                    f"{dc_line}\n"
                    f"💳 *Gesamtbetrag: PKR {int(grand_total)}*\n\n"
                    f"📍 Lieferadresse:\n_{address}_\n\n"
                    f"🔖 Bestellnr: *#{order_id[-6:]}*\n"
                    f"⏱️ Voraussichtliche Lieferung: *{delivery_time}*\n\n"
                    f"Wir sind dabei! Tippen Sie *new order* für eine neue Bestellung."
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
                        "en": (
                            f"📍 Should we deliver to your previous address?\n\n"
                            f"_{last_addr}_\n\n"
                            f"Type *same* to confirm, or enter a new address:"
                        ),
                        "ur": f"📍 پرانے پتے پر ڈلیوری؟\n_{last_addr}_\n\n*same* لکھیں یا نیا پتہ دیں:",
                        "de": f"📍 An letzte Adresse?\n_{last_addr}_\n\n*same* oder neue Adresse:",
                    }
                else:
                    addr_prompt = {
                        "en": (
                            "📍 Perfect! What's your delivery address?\n\n"
                            "_(House no., street, area, city)_"
                        ),
                        "ur": "📍 اپنا مکمل پتہ دیں (مکان نمبر، گلی، علاقہ، شہر):",
                        "de": "📍 Bitte vollständige Lieferadresse angeben:",
                    }
                await send_whatsapp_text(from_num, addr_prompt.get(lang, addr_prompt["en"]))

            elif any(kw in q for kw in ["add more", "more", "aur", "add", "➕", "aur kuch"]):
                session["step"]          = 0
                session["pending_order"] = {}
                add_more = {
                    "en": "Of course! What else would you like to add to your order? 🍽️",
                    "ur": "بالکل! اور کیا شامل کرنا ہے؟ 🍽️",
                    "de": "Natürlich! Was möchten Sie noch hinzufügen? 🍽️",
                }
                await send_whatsapp_text(from_num, add_more.get(lang, add_more["en"]))

            elif any(kw in q for kw in ["clear", "reset", "cancel", "empty", "🗑️"]):
                session["cart"] = []
                session["step"] = 0
                cleared = {
                    "en": "🗑️ No problem — cart cleared! What would you like to order instead? 😊",
                    "ur": "🗑️ ٹوکری صاف! کیا آرڈر کرنا ہے؟",
                    "de": "🗑️ Warenkorb geleert! Was möchten Sie bestellen?",
                }
                await send_whatsapp_text(from_num, cleared.get(lang, cleared["en"]))

            else:
                cart    = session.get("cart", [])
                total   = _recalc_cart(cart)
                summary = _build_cart_summary(cart, total, lang)
                recap_prompt = {
                    "en": f"{summary}\n\n👉 Ready to confirm, or want to add something more?",
                    "ur": f"{summary}\n\n👉 تصدیق کریں یا مزید شامل کریں؟",
                    "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
                }
                await send_whatsapp_buttons(
                    from_num,
                    recap_prompt.get(lang, recap_prompt["en"]),
                    ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
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
                    "en": "🛒 Hmm, your cart seems to be empty. What would you like to order? 😊",
                    "ur": "🛒 ٹوکری خالی ہے۔ کیا آرڈر کرنا ہے؟",
                    "de": "🛒 Warenkorb ist leer. Was möchten Sie bestellen?",
                }
                await send_whatsapp_text(from_num, cart_empty.get(lang, cart_empty["en"]))
                return JSONResponse({"status": "ok"})

            if q.strip() in ["same", "same address", "same adress", "same add"]:
                address = session.get("last_address")
                if not address:
                    no_addr = {
                        "en": (
                            "⚠️ I don't have a previous address saved yet.\n\n"
                            "Could you type your full delivery address?"
                        ),
                        "ur": "⚠️ پرانا پتہ نہیں ملا۔ اپنا مکمل پتہ لکھیں۔",
                        "de": "⚠️ Keine frühere Adresse. Bitte vollständige Adresse eingeben.",
                    }
                    await send_whatsapp_text(from_num, no_addr.get(lang, no_addr["en"]))
                    return JSONResponse({"status": "ok"})
            else:
                address_candidate = extract_address(msg_text) or msg_text.strip()
                if not _is_valid_address(address_candidate):
                    retry_addr = {
                        "en": (
                            "📍 I need a little more detail to find you — no worries! 😊\n\n"
                            "Please share: *House no., Street, Area, City*\n"
                            "Example: _House 12, Block B, Gulshan, Karachi_"
                        ),
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
                    "en": "⚠️ Something went wrong — please try again! We're sorry for the trouble.",
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
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{summary}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📍 Delivering to:\n_{address}_\n\n"
                    f"🔖 Order ID: *#{order_id[-6:]}*\n"
                    f"⏱️ Estimated delivery: *{delivery_time}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Our team is on it! 🙌 We'll get this to you fresh and fast.\n"
                    f"Type *new order* anytime to order again 😊"
                ),
                "ur": (
                    f"✅ *آرڈر تصدیق ہوگیا!* 🎉\n\n"
                    f"{summary}\n\n"
                    f"📍 پتہ: _{address}_\n"
                    f"🔖 نمبر: *#{order_id[-6:]}*\n"
                    f"⏱️ تخمینی ڈلیوری: *{delivery_time}*\n\n"
                    f"جلد پہنچائیں گے! نیا آرڈر دینے کے لیے *new order* لکھیں۔"
                ),
                "de": (
                    f"✅ *Bestellung bestätigt!* 🎉\n\n"
                    f"{summary}\n\n"
                    f"📍 Adresse: _{address}_\n"
                    f"🔖 Nr: *#{order_id[-6:]}*\n"
                    f"⏱️ Voraussichtliche Lieferung: *{delivery_time}*\n\n"
                    f"Wir liefern so schnell wie möglich! Tippen Sie *new order* für eine neue Bestellung."
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
                        "en": (
                            f"Hmm, didn't catch that — could you please choose from these sizes?\n\n"
                            f"*{sizes_str}*"
                        ),
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
                            "en": f"{summary}\n\n✨ Ready to confirm? 😊",
                            "ur": f"{summary}\n\n✨ تصدیق کریں یا مزید شامل کریں؟",
                            "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
                        }
                        await send_whatsapp_buttons(
                            from_num,
                            confirm_msgs.get(lang, confirm_msgs["en"]),
                            ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                        )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 20 — Multi-size SPICE RESOLUTION
        # v14.4 FIX 2 & 3: size-word collision guard + shared spice
        # ═══════════════════════════════════════════════════════
        if step == 20:
            multi_queue  = session.get("multi_size_queue", [])
            product      = session.get("pending_order", {}).get("product_ref", {})
            spice_levels = product.get("spice_levels", []) if product else []
            cart_items   = list(session.get("cart", []))

            # Build set of known size labels to avoid collision with spice matching
            known_size_labels: set = set()
            if product:
                for v in product.get("variants", []):
                    for tok in re.findall(r'\w+', v.get("size", "").lower()):
                        known_size_labels.add(tok)

            per_item_parsed = _parse_multi_size_from_text(msg_text, product) if product else []
            size_spice_map: Dict[str, str] = {}
            for pi in per_item_parsed:
                if pi.get("spice") and pi.get("matched_variant"):
                    size_key = pi["matched_variant"].get("size", "").lower()
                    size_spice_map[size_key] = pi["spice"]

            # v14.4 FIX 2: only accept shared spice if it doesn't collide with size labels
            shared_spice = ""
            for sl in sorted(spice_levels, key=len, reverse=True):
                sl_tokens = set(re.findall(r'\w+', sl.lower()))
                if sl_tokens and sl_tokens.issubset(known_size_labels):
                    continue
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
                        "en": f"{summary}\n\n✨ Ready to confirm? 😊",
                        "ur": f"{summary}\n\n✨ تصدیق کریں یا مزید شامل کریں؟",
                        "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
                    }
                    await send_whatsapp_buttons(
                        from_num,
                        confirm_msgs.get(lang, confirm_msgs["en"]),
                        ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                    )
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
                    "en": f"{summary}\n\n✨ Looking great! Ready to confirm? 😊",
                    "ur": f"{summary}\n\n✨ تصدیق کریں یا مزید شامل کریں؟",
                    "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
                }
                await send_whatsapp_buttons(
                    from_num,
                    confirm_msgs.get(lang, confirm_msgs["en"]),
                    ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 0 — Normal intent routing
        # ═══════════════════════════════════════════════════════

        # ── Delivery charge inquiry ────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["delivery_charge"]):
            dc         = BOT_DATA.get("delivery_charges", {})
            flat       = float(dc.get("flat_charge", 0) or 0)
            free_above = float(dc.get("free_above", 0) or 0)
            per_area   = dc.get("per_area", {})

            lines = []
            if free_above > 0:
                lines.append({
                    "en": f"🎉 Great news — orders above *PKR {int(free_above)}* get *free delivery*!",
                    "ur": f"🎉 PKR {int(free_above)} سے زیادہ آرڈر پر مفت ڈلیوری!",
                    "de": f"🎉 Kostenlose Lieferung bei Bestellungen über PKR {int(free_above)}!",
                }.get(lang, f"✅ Free delivery above PKR {int(free_above)}!"))
            if flat > 0 and not lines:
                lines.append({
                    "en": f"🚚 Standard delivery charge: *PKR {int(flat)}*",
                    "ur": f"🚚 معیاری ڈلیوری چارج: PKR {int(flat)}",
                    "de": f"🚚 Standard-Liefergebühr: PKR {int(flat)}",
                }.get(lang, f"🚚 Delivery charge: PKR {int(flat)}"))
            elif flat == 0 and not lines:
                lines.append({
                    "en": "🎉 Amazing — we offer *FREE delivery* on all orders!",
                    "ur": "🎉 خوشخبری — ہم مفت ڈلیوری کرتے ہیں!",
                    "de": "🎉 Wir liefern KOSTENLOS!",
                }.get(lang, "🎉 FREE delivery!"))
            if per_area:
                area_lines = "\n".join(f"  ▸ {k.title()}: PKR {int(v)}" for k, v in per_area.items())
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
                # v14.5 NEW 2: Always show "Add More" in cart view
                await send_whatsapp_buttons(
                    from_num,
                    summary,
                    ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
                )
                session["step"] = 5
            else:
                cart_empty = {
                    "en": "🛒 Your cart is empty right now — let's fix that! 🍽️\n\nWhat are you in the mood for?",
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
                "en": "🗑️ Done! Cart cleared — fresh start! What would you like to order? 😊",
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
                    "en": (
                        f"📍 Shall we deliver to your previous address?\n\n"
                        f"_{last_addr}_\n\n"
                        f"Type *same* to confirm, or enter a new address:"
                    ),
                    "ur": f"📍 پرانے پتے پر ڈلیوری؟\n_{last_addr}_\n\n*same* لکھیں یا نیا پتہ دیں:",
                    "de": f"📍 An letzte Adresse?\n_{last_addr}_\n\n*same* oder neue Adresse:",
                }
            else:
                addr_prompt = {
                    "en": "📍 Wonderful! What's your delivery address?\n_(House no., street, area, city)_",
                    "ur": "📍 اپنا مکمل پتہ دیں:",
                    "de": "📍 Lieferadresse angeben:",
                }
            await send_whatsapp_text(from_num, addr_prompt.get(lang, addr_prompt["en"]))
            return JSONResponse({"status": "ok"})

        # ── MIXED INTENT detection ─────────────────────────────
        order_intent   = any(kw in q for kw in INTENT_KEYWORDS["order"])
        price_intent   = _detect_price_menu_intent(q)   # v14.4 FIX 5
        menu_intent    = any(kw in q for kw in INTENT_KEYWORDS["menu"])
        inquiry_intent = any(kw in q for kw in INTENT_KEYWORDS["inquiry"])
        multi_signals  = ["and", "aur", "+", "also", "ke saath", "اور", "saath", "plus"]
        is_multi       = any(s in q for s in multi_signals)
        product_query  = _is_product_query(q)

        if price_intent:
            _track({"total_searches": 1})
            await _handle_full_price_display(from_num, q, lang)
            if not order_intent and not product_query:
                return JSONResponse({"status": "ok"})

        # ── Inquiry about a dish (not ordering) ───────────────
        if inquiry_intent and not order_intent:
            product = _find_product_by_query(msg_text)
            if product:
                name        = product.get("title", "Item").strip().title()
                desc        = product.get("description", "")
                variants    = product.get("variants", [])
                spice_lvls  = product.get("spice_levels", [])
                size_list   = "\n".join(f"  ▸ *{v['size']}*  —  PKR {int(v['price'])}" for v in variants) if variants else ""
                spice_str   = "  •  ".join(s.strip().title() for s in spice_lvls) if spice_lvls else ""

                reply_parts = [f"🍽️ *{name}*"]
                if desc:
                    reply_parts.append(f"\n_{desc.strip()}_")
                if size_list:
                    sizes_label = {"en": "📏 Available sizes:", "ur": "📏 سائز:", "de": "📏 Größen:"}.get(lang, "Sizes:")
                    reply_parts.append(f"\n{sizes_label}\n{size_list}")
                if spice_str:
                    spice_label = {"en": "🌶️ Spice options:", "ur": "🌶️ مسالے:", "de": "🌶️ Schärfe:"}.get(lang, "Spice levels:")
                    reply_parts.append(f"\n{spice_label} {spice_str}")

                order_q = {
                    "en": "\n\nWant to go ahead and order? Just tap below! 😊",
                    "ur": "\n\nکیا آرڈر کرنا ہے؟ 😊",
                    "de": "\n\nMöchten Sie bestellen? 😊",
                }.get(lang, "\nWant to order? 😊")
                reply_parts.append(order_q)

                session["last_shown_product"] = product
                await send_whatsapp_buttons(
                    from_num,
                    "\n".join(reply_parts),
                    ["✅ Order Now", "📋 View Menu"],
                )
                return JSONResponse({"status": "ok"})

        # ── Order intent OR direct product name ────────────────
        if order_intent or product_query or is_multi or re.search(r'\d+\s*(?:kg|ml|l\b|g\b)', q):
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
            category = _detect_category_from_query(q)
            if category:
                products = _products_by_category(category) or filter_products(q)
            else:
                products = PRODUCTS_DATA or []

            if products:
                header_map = {
                    "en": "Here's everything we've got for you today! 🍽️",
                    "ur": "آج کا مینو آپ کے لیے! 🍽️",
                    "de": "Das haben wir heute für Sie! 🍽️",
                }
                header = header_map.get(lang, header_map["en"])
                await send_whatsapp_text(from_num, header)
                menu_text = _build_text_menu(products, lang)
                await send_whatsapp_text(from_num, menu_text)
            else:
                # v14.5 NEW 1: Show full example menu when DB is empty
                example_msg = {
                    "en": "Here's a look at our full menu! 🍽️",
                    "ur": "ہمارا مکمل مینو! 🍽️",
                    "de": "Unsere vollständige Speisekarte! 🍽️",
                }
                await send_whatsapp_text(from_num, example_msg.get(lang, example_msg["en"]))
                await send_whatsapp_text(from_num, _build_full_example_menu(lang))
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
                status_emoji = {
                    "Pending":    "⏳",
                    "Accepted":   "✅",
                    "Processing": "👨‍🍳",
                    "Delivered":  "🚗",
                    "Rejected":   "❌",
                }.get(status, "📦")
                status_desc  = {
                    "Pending":    {"en": "We've received your order and will confirm it shortly!", "ur": "آرڈر موصول ہوگیا، جلد تصدیق ہوگی!", "de": "Bestellung erhalten, wird bald bestätigt!"},
                    "Accepted":   {"en": "Your order has been accepted and we're on it! 🙌",        "ur": "آرڈر قبول ہوگیا!", "de": "Bestellung angenommen!"},
                    "Processing": {"en": "Our chefs are cooking your food right now! 👨‍🍳",             "ur": "شیف آپ کا کھانا بنا رہے ہیں!",   "de": "Unser Koch bereitet Ihr Essen zu!"},
                    "Delivered":  {"en": "Your order has been delivered — enjoy! 😊",               "ur": "آرڈر پہنچا دیا گیا!",               "de": "Ihre Bestellung wurde geliefert!"},
                    "Rejected":   {"en": "Sorry, we couldn't fulfil this order. Please contact us.", "ur": "معذرت، آرڈر پورا نہیں ہوسکا۔",    "de": "Leider konnten wir die Bestellung nicht erfüllen."},
                }
                desc = status_desc.get(status, {}).get(lang, status)
                st   = {
                    "en": f"{status_emoji} *Order Status — {dish_name}*\n\nStatus: *{status}*\n{desc}\n\n🔖 Order ID: *#{str(latest.get('_id', ''))[-6:]}*",
                    "ur": f"{status_emoji} *آرڈر کی حالت — {dish_name}*\n\nحالت: *{status}*\n{desc}\n\n🔖 نمبر: *#{str(latest.get('_id', ''))[-6:]}*",
                    "de": f"{status_emoji} *Bestellstatus — {dish_name}*\n\nStatus: *{status}*\n{desc}\n\n🔖 Nr: *#{str(latest.get('_id', ''))[-6:]}*",
                }
                await send_whatsapp_text(from_num, st.get(lang, st["en"]))
            else:
                no_order = {
                    "en": (
                        "Hmm, looks like you haven't placed an order with us yet! 😊\n\n"
                        "Ready to place your first one? Check out our menu!"
                    ),
                    "ur": "ابھی تک کوئی آرڈر نہیں۔ پہلا آرڈر دیں! 🍽️",
                    "de": "Noch keine Bestellungen. Geben Sie Ihre erste auf! 🍽️",
                }
                await send_whatsapp_text(from_num, no_order.get(lang, no_order["en"]))
            return JSONResponse({"status": "ok"})

        # ── Greeting — checked AFTER product-name check ────────
        if _is_pure_greeting(q):
            greeting = BOT_DATA.get("initial_message", {}).get(lang, "Hey! 👋 Welcome. What would you like today? 🍽️")
            sugs     = get_suggestions(from_num, lang)

            # v14.5 NEW 5: Show a menu teaser with greeting
            top_items = PRODUCTS_DATA[:3] if PRODUCTS_DATA else []
            teaser    = ""
            if top_items:
                teaser_lines = [
                    {"en": "\n\n🔥 *Today's Favourites:*", "ur": "\n\n🔥 *آج کے مقبول آئٹم:*", "de": "\n\n🔥 *Heutige Favoriten:*"}.get(lang, "\n\n🔥 *Today's Picks:*")
                ]
                for item in top_items:
                    variants = item.get("variants", [])
                    name     = item.get("title", "").strip().title()
                    if variants:
                        price = f"from PKR {int(variants[0]['price'])}"
                    else:
                        price = f"PKR {item.get('price', '?')}"
                    teaser_lines.append(f"  • *{name}* — {price}")
                teaser = "\n".join(teaser_lines)

            reply = greeting + teaser
            if sugs:
                reply += {
                    "en": "\n\n💡 *Popular right now:*\n• ",
                    "ur": "\n\n💡 *مقبول آئٹم:*\n• ",
                    "de": "\n\n💡 *Gerade beliebt:*\n• ",
                }.get(lang, "\n\n💡 Popular:\n• ") + "\n• ".join(sugs)

            await send_whatsapp_buttons(
                from_num,
                reply,
                ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"],
            )
            return JSONResponse({"status": "ok"})

        # ── Product name search (index-powered) ────────────────
        matched_product = _find_product_by_query(msg_text)
        if matched_product:
            product_name = matched_product.get("title", "Item").strip().title()
            variants     = matched_product.get("variants", [])
            desc         = matched_product.get("description", "").strip()

            session["last_shown_product"] = matched_product

            if variants:
                size_list = "\n".join(f"  ▸ *{v['size']}*  —  PKR {int(v['price'])}" for v in variants)
                reply_parts = [f"🍽️ *{product_name}*"]
                if desc:
                    reply_parts.append(f"\n_{desc}_")
                reply_parts.append(f"\n\n📏 *Available sizes:*\n{size_list}")
                reply_parts.append({
                    "en": "\n\nWant to order? Just tap below! 😊",
                    "ur": "\n\nآرڈر کرنا ہے؟",
                    "de": "\n\nMöchten Sie bestellen?",
                }.get(lang, "\nOrder now?"))

                await send_whatsapp_buttons(
                    from_num,
                    "\n".join(reply_parts),
                    ["✅ Order Now", "📋 View Menu"],
                )
            else:
                price_str         = f"PKR {matched_product.get('price', 'N/A')}"
                no_variant_reply  = [f"🍽️ *{product_name}*  —  {price_str}"]
                if desc:
                    no_variant_reply.append(f"\n_{desc}_")
                no_variant_reply.append({
                    "en": "\n\nWant to go ahead and order this? 😊",
                    "ur": "\n\nکیا آرڈر کرنا ہے؟",
                    "de": "\n\nMöchten Sie bestellen?",
                }.get(lang, "\nOrder?"))

                await send_whatsapp_buttons(
                    from_num,
                    "\n".join(no_variant_reply),
                    ["✅ Order Now", "📋 View Menu"],
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
        existing["quantity"]         += quantity
        existing["total_item_price"]  = (existing["base_price"] + existing["extras_price"]) * existing["quantity"]
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
                "Pending":    f"⏳ Hey! Your order for *{dish_name}* is pending confirmation — we'll be with you in just a moment!",
                "Accepted":   f"✅ Great news! Your *{dish_name}* order has been accepted — our team is on it! 🙌",
                "Processing": f"👨‍🍳 Your *{dish_name}* is being freshly prepared right now — almost ready!",
                "Delivered":  f"🚗 Your *{dish_name}* has been delivered! We hope you enjoy every bite 😊",
                "Rejected":   f"❌ We're so sorry — we couldn't fulfil your *{dish_name}* order this time. Please contact us and we'll make it right!",
            }
            msg = status_msg.get(new_status, f"📦 Order *{dish_name}* status updated to: *{new_status}*")
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

    existing   = BOT_DATA.get("delivery_charges", {})
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
    logger.info("🚀 Restaurant Bot v14.5 started!")
    logger.info(f"   Products loaded    : {len(PRODUCTS_DATA)}")
    logger.info(f"   Keyword index size : {len(PRODUCT_KEYWORD_INDEX)}")
    logger.info(f"   FAQ keys           : {list(BOT_DATA.get('faq', {}).keys())}")
    logger.info(f"   Delivery time      : {get_delivery_time()}")
    logger.info(f"   Delivery charges   : {BOT_DATA.get('delivery_charges', {})}")
    logger.info(f"   WhatsApp connected : {'✅' if WHATSAPP_TOKEN else '❌'}")
    logger.info(f"   MongoDB connected  : {'✅' if products_col is not None else '❌'}")
    logger.info(f"   AI fallback        : {'✅' if ANTHROPIC_API_KEY else '⚠️  Static fallback active'}")
