"""
WhatsApp AI Restaurant Bot — FastAPI Backend (v15.0)
=====================================================
v15.0 — Complete rewrite of order flow logic:

  ✅ FIX 1: Smart one-shot order parsing — "Small Pizza Spicy extra cheese"
             is parsed ONCE and ALL info (size + spice + extras) extracted
             immediately. No unnecessary follow-up questions.

  ✅ FIX 2: Product detection fixed — best-match scoring now correctly
             picks the right product. Pizza query → Pizza, not Karahi.

  ✅ FIX 3: Minimal questions — bot ONLY asks what it genuinely can't infer.
             If user gives size+spice+extras in one message, bot goes straight
             to address. Human-friendly, calm, no repetition.

  ✅ FIX 4: Unlimited items in one message handled cleanly:
             "2 small pizza spicy, 1 kg karahi mild, large coke" → all parsed,
             missing info resolved per-product in one pass.

  ✅ FIX 5: Clean, concise responses. No over-formatted walls of text.

  ✅ FIX 6: Step flow simplified to 4 states:
             0 = idle, 1 = waiting address, 2 = waiting cart confirm,
             10 = waiting size for specific product,
             20 = waiting spice for specific product,
             30 = waiting extras for specific product

  ✅ KEEP: All v14.0 keyword index, delivery charges, FAQ, analytics, CRM.
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
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from collections import defaultdict
from difflib import SequenceMatcher

# ============================================================
# INITIAL SETUP
# ============================================================

load_dotenv()
DetectorFactory.seed = 0
logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RestaurantBot.v15")

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


app = FastAPI(title="WhatsApp AI Restaurant Bot v15.0", version="15.0",
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="templates")

# ============================================================
# ENVIRONMENT
# ============================================================

MONGO_URI         = os.getenv("MONGO_URI", "")
WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN", "my_verify_token")
SECRET_PASSWORD   = os.getenv("SECRET_PASSWORD", "admin")
CRM_USERNAME      = os.getenv("USER_NAME", "admin")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
WHATSAPP_API_URL  = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_ID}/messages"

# ============================================================
# DATABASE
# ============================================================

try:
    client        = MongoClient(MONGO_URI, tls=True, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=10000)
    db            = client["restaurant"]
    products_col  = db["products"]
    meta_col      = db["bot_metadata"]
    analytics_col = db["analytics"]
    orders_col    = db["orders"]
    carts_col     = db["carts"]
    sessions_col  = db["sessions"]
    client.admin.command("ping")
    logger.info("✅ MongoDB connected")
except Exception as e:
    logger.error(f"❌ MongoDB failed: {e}")
    products_col = meta_col = analytics_col = orders_col = carts_col = sessions_col = None

# ============================================================
# UTILITIES
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
        'half plate': 'Half Plate', 'half plat': 'Half Plate', 'halfplate': 'Half Plate',
        'full plate': 'Full Plate', 'full plat': 'Full Plate', 'fullplate': 'Full Plate',
        'family pack': 'Family Pack', 'familypack': 'Family Pack', 'family': 'Family Pack',
        'quarter plate': 'Quarter Plate',
    }
    for k, v in plate_map.items():
        if k in s:
            return v
    size_map = {
        'small': 'Small', 'medium': 'Medium', 'large': 'Large',
        'regular': 'Regular', 'xl': 'XL', 'xxl': 'XXL',
        'full': '1kg', 'half': '0.5kg',
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
    return best_v if best_score > 0 else None


def _recalc_cart(cart_items: List[Dict]) -> float:
    return sum(
        (item.get("base_price", 0) + item.get("extras_price", 0)) * item.get("quantity", 1)
        for item in cart_items
    )


def _build_cart_summary(items, total, lang="en", delivery_charge=0.0, show_delivery=False) -> str:
    h = {"en": "🛒 *Your Order:*\n", "ur": "🛒 *آپ کا آرڈر:*\n", "de": "🛒 *Ihre Bestellung:*\n"}
    lines = [h.get(lang, h["en"])]
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
        if extras: line += f"\n  ➕ {extras}"
        if spice:  line += f"\n  🌶️ {spice}"
        lines.append(line)
    t = {"en": f"\n💰 *Subtotal: PKR {int(total)}*", "ur": f"\n💰 *سب ٹوٹل: PKR {int(total)}*", "de": f"\n💰 *Zwischensumme: PKR {int(total)}*"}
    lines.append(t.get(lang, t["en"]))
    if show_delivery:
        if delivery_charge > 0:
            dc = {"en": f"🚚 *Delivery: PKR {int(delivery_charge)}*", "ur": f"🚚 *ڈلیوری: PKR {int(delivery_charge)}*", "de": f"🚚 *Lieferung: PKR {int(delivery_charge)}*"}
        else:
            dc = {"en": "🚚 *Delivery: FREE* 🎉", "ur": "🚚 *ڈلیوری: مفت* 🎉", "de": "🚚 *Lieferung: KOSTENLOS* 🎉"}
        lines.append(dc.get(lang, dc["en"]))
        grand = total + delivery_charge
        gt = {"en": f"💳 *Grand Total: PKR {int(grand)}*", "ur": f"💳 *کل رقم: PKR {int(grand)}*", "de": f"💳 *Gesamtbetrag: PKR {int(grand)}*"}
        lines.append(gt.get(lang, gt["en"]))
    return "\n".join(lines)


def _build_full_price_menu(products, category_emoji="🍽️", title="Menu & Prices") -> str:
    lines = [f"{category_emoji} *{title}*\n"]
    for product in products:
        lines.append(f"• *{product.get('title', 'Item').strip().title()}*")
        variants = product.get("variants", [])
        if variants:
            for v in variants:
                lines.append(f"  ‣ {v.get('size', 'N/A')} — PKR {v.get('price', '?')}")
        else:
            lines.append(f"  ‣ PKR {product.get('price', 'N/A')}")
        lines.append("")
    return "\n".join(lines).strip()


_ADDRESS_KEYWORDS = re.compile(
    r'\b(street|st|road|rd|avenue|ave|lane|block|sector|phase|house|flat|floor|'
    r'building|near|opposite|plot|no\.?|number|h\.?no|area|town|city|karachi|'
    r'lahore|islamabad|gulshan|clifton|defence|defense|dha|gulberg|johar|nazimabad|'
    r'گلی|سڑک|گھر|مکان|بلاک|فیز|شہر|پتہ)\b', re.IGNORECASE)

_SHORT_WORDS = {"yes","no","ok","okay","sure","fine","yep","yeah","haan","nahi","done","same","correct","right"}

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
# PRODUCT KEYWORD INDEX
# ============================================================

_UNIVERSAL_CATEGORY_ALIASES: Dict[str, List[str]] = {
    "burger":   ["burger","brgr","برگر","zinger","cheeseburger","double burger"],
    "pizza":    ["pizza","پیزا","پیزہ","margherita","pepperoni","tikka pizza"],
    "biryani":  ["biryani","بریانی","baryani","dum biryani","chicken biryani","beef biryani","mutton biryani"],
    "drinks":   ["drink","مشروب","cola","juice","coke","pepsi","lassi","cold drink","soda","7up","sprite","fanta","water","سافٹ ڈرنک"],
    "dessert":  ["dessert","مٹھائی","sweet","cake","kheer","meetha","halwa","gulab jamun","brownie","mithai"],
    "karahi":   ["karahi","کڑاہی","karai","karhai","chicken karahi","beef karahi","mutton karahi","dum karahi"],
    "rice":     ["rice","چاول","pulao","fried rice","plov"],
    "rolls":    ["roll","رول","shawarma","wrap","paratha roll"],
    "chicken":  ["chicken","چکن","murgh"],
    "beef":     ["beef","گوشت","gosht"],
    "mutton":   ["mutton","lamb","دنبہ"],
    "soup":     ["soup","شوربہ","shorba"],
    "salad":    ["salad","سلاد"],
    "bread":    ["bread","naan","roti","paratha","روٹی","نان","پراٹھا"],
    "shawarma": ["shawarma","شوارمہ"],
    "sandwich": ["sandwich","سینڈوچ","sub"],
    "pasta":    ["pasta","پاستا","spaghetti","macaroni"],
    "steak":    ["steak","اسٹیک","grilled"],
    "fish":     ["fish","مچھلی","seafood","prawn","shrimp"],
}

CATEGORY_KEYWORDS = {
    "burger":   ["burger","برگر","brgr","cheeseburger","double burger","zinger"],
    "pizza":    ["pizza","پیزا","پیزہ","margherita","pepperoni","tikka pizza"],
    "biryani":  ["biryani","بریانی","dum biryani","chicken biryani","baryani"],
    "drinks":   ["drink","مشروب","juice","cola","water","سافٹ ڈرنک","lassi","coke","pepsi","7up","sprite","fanta","soda","cold drink"],
    "dessert":  ["dessert","مٹھائی","cake","kheer","halwa","gulab jamun","brownie","meetha","sweet","mithai"],
    "karahi":   ["karahi","کڑاہی","chicken karahi","beef karahi","mutton karahi","karhai","dum karahi"],
    "rice":     ["rice","چاول","pulao","plov","fried rice"],
    "rolls":    ["roll","رول","shawarma","wrap","paratha roll"],
    "chicken":  ["chicken","چکن","murgh"],
    "beef":     ["beef","گوشت"],
    "mutton":   ["mutton","lamb"],
    "fish":     ["fish","مچھلی","seafood","prawn"],
    "bread":    ["naan","roti","paratha","bread","روٹی","نان"],
    "shawarma": ["shawarma","شوارمہ"],
    "pasta":    ["pasta","پاستا","spaghetti"],
    "sandwich": ["sandwich","سینڈوچ","sub"],
    "soup":     ["soup","شوربہ","shorba"],
    "salad":    ["salad","سلاد"],
    "steak":    ["steak","اسٹیک","grilled"],
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
        for word in re.findall(r'\w+', title.lower()):
            if len(word) > 2:
                _add(word, product)
        if category:
            _add(category, product)
        for cat_key, aliases in _UNIVERSAL_CATEGORY_ALIASES.items():
            if cat_key == category or cat_key in title.lower() or cat_key in desc.lower():
                for alias in aliases:
                    _add(alias, product)
        for word in re.findall(r'\w+', desc.lower()):
            if len(word) > 3:
                _add(word, product)

    logger.info(f"Keyword index: {len(PRODUCT_KEYWORD_INDEX)} keys, {len(PRODUCTS_DATA)} products")


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
            "supported_languages": ["en", "ur", "de"],
            "initial_message":     {"en": "Welcome! 🍽️ How can I help you today?", "ur": "خوش آمدید! 🍽️ آج میں آپ کی کیا مدد کر سکتا ہوں؟", "de": "Willkommen! 🍽️ Wie kann ich Ihnen helfen?"},
            "discount_message":    {}, "faq": {}, "smart_suggestions": {},
            "delivery_time": "35-45 mins", "delivery_time_exceptions": {},
            "delivery_charges": {"flat_charge": 0, "free_above": 0, "per_area": {}, "free_keywords": []},
        }
        BOT_DATA.update(merged)

        config_doc = meta_col.find_one({"type": "config"})
        if config_doc:
            _str_id(config_doc)
            for k in ["faq","smart_suggestions","initial_message","discount_message",
                      "supported_languages","delivery_time","delivery_time_exceptions","delivery_charges"]:
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
        logger.info(f"Data synced | Products: {len(PRODUCTS_DATA)}")
    except Exception as e:
        logger.error(f"Data load error: {e}")


def get_delivery_time(category: str = "") -> str:
    def _unwrap(val, fallback="35-45 mins"):
        if val is None: return fallback
        if isinstance(val, str): return val.strip() or fallback
        if isinstance(val, dict):
            if category and category.lower() in {k.lower() for k in val}:
                for k, v in val.items():
                    if k.lower() == category.lower():
                        return _unwrap(v, fallback)
            if "default" in val: return _unwrap(val["default"], fallback)
            if val: return _unwrap(next(iter(val.values())), fallback)
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
# DELIVERY CHARGES
# ============================================================

def calculate_delivery_charge(order_total: float, address: str = "") -> float:
    dc            = BOT_DATA.get("delivery_charges", {})
    flat_charge   = float(dc.get("flat_charge", 0) or 0)
    free_above    = float(dc.get("free_above", 0) or 0)
    per_area      = dc.get("per_area", {})
    free_keywords = dc.get("free_keywords", [])
    addr_lower    = address.lower()
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
    return {"en": f"🚚 Delivery: PKR {int(charge)}", "ur": f"🚚 ڈلیوری: PKR {int(charge)}", "de": f"🚚 Liefergebühr: PKR {int(charge)}"}.get(lang, f"🚚 Delivery: PKR {int(charge)}")


def init_analytics():
    if analytics_col is not None and analytics_col.count_documents({"type": "analytics"}) == 0:
        analytics_col.insert_one({
            "type": "analytics", "total_searches": 0, "total_orders": 0,
            "total_clicks": 0, "total_cart_additions": 0,
            "most_questions": {}, "product_search": {}, "product_clicks": {},
            "size_preference": {}, "spice_preference": {}, "extras_preference": {},
            "supported_languages": {},
        })


def _track(inc_dict: Dict):
    if analytics_col is not None:
        analytics_col.update_one({"type": "analytics"}, {"$inc": inc_dict})

# ============================================================
# LANGUAGE DETECTION
# ============================================================

_BUTTON_TEXTS = {
    "view menu 📋","place order 🛒","contact us 📞","✅ confirm order",
    "➕ add more","🗑️ clear cart","✅ order now","📋 view menu",
    "order again 🔄","view menu","place order","contact us",
    "confirm order","add more","clear cart","order now",
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
        if lang not in ("en", "ur", "de"): return session_lang
        return "en"
    except Exception:
        return session_lang

# ============================================================
# INTENT KEYWORDS
# ============================================================

INTENT_KEYWORDS = {
    "discount":  ["discount","sale","deal","offer","cheap","سستا","رعایت","rabatt","special offer","promo","coupon"],
    "order":     ["order","آرڈر","buy","place order","i want","مجھے چاہیے","bestellen","chahiye","dena","lena",
                  "add","mujhe","give me","i'll have","i'd like","can i get","get me","send me","bhai dena",
                  "yaar dena","ek dena","do dena","lao","manga","mangwao","order karo","order now","want to order","want"],
    "menu":      ["menu","مینو","menü","what do you have","show menu","list","items","all items","show all",
                  "kya hai","kya milta","what's available","what do you serve","show items"],
    "price":     ["price","قیمت","preis","cost","how much","kitna","rate","all prices","all flavours",
                  "all flavors","price list","rates","kitne ka","kitni","kya rate","daam","qeemat"],
    "greeting":  ["hi","hello","hey","assalam","السلام","hallo","guten tag","سلام","start","begin","aoa",
                  "aslam","good morning","good evening","good afternoon","salam","as salam","walaikum"],
    "status":    ["status","where","order status","track","delivered","pending","track order","mera order","kahan hai"],
    "cancel":    ["cancel","منسوخ","stornieren","nahi chahiye","remove order","delete order","hatao",
                  "band karo","order cancel","cancel order","delete","remove","clear order","mujhe nahi chahiye"],
    "cart":      ["cart","basket","my order","show cart","view cart","what did i order","my cart","mera cart"],
    "confirm":   ["confirm","yes","okay","ok","haan","ہاں","proceed","place","done","confirm order",
                  "place order","theek hai","bilkul","zaroor","sure","absolutely","go ahead","finalize"],
    "clear":     ["clear cart","empty cart","start over","restart","reset cart"],
    "new_order": ["new order","naya order","start new","nayi order","fresh order","order again","reorder",
                  "new aaorder","new ordar","nai order","dobara order","phir order","again order"],
    "show_total":["tell me total","show total","my total","mera total","total kitna","kitna total",
                  "total kya","total bta","price total","how much total","total price","total amount"],
    "delivery_charge": ["delivery charge","delivery fee","delivery cost","delivery kitna","delivery charges",
                        "kitna delivery","free delivery","delivery free","ڈلیوری چارج","ڈلیوری فیس","liefergebühr"],
    "inquiry":   ["tell me about","what is","describe","kya hai","kaisa hai","batao","bataiye","details",
                  "more info","information about","what sizes","what flavors","what options","kaunse size",
                  "kya varieties","available sizes"],
    "thanks":    ["thank","thanks","thankyou","thank you","shukriya","شکریہ","jazakallah","great","awesome",
                  "perfect","excellent","wonderful","brilliant","amazing"],
}

SIZE_HINTS = [
    "family pack","half plate","full plate","quarter plate",
    "0.5kg","1.5kg","2kg","1kg","0.25kg","half kg","1 kg","2 kg",
    "500ml","1.5l","1.5L","1l","small","medium","large","regular","xl","xxl","half","full",
]

QUANTITY_WORDS = {
    "ek":1,"one":1,"aik":1,"ik":1,"do":2,"two":2,"dou":2,
    "teen":3,"three":3,"tin":3,"char":4,"four":4,"panch":5,"five":5,
    "chay":6,"six":6,"first":1,"second":1,"third":1,"fourth":1,
}

_ORDER_NOISE_PREFIXES = re.compile(
    r'^(i\s+want\s+to\s+order|i\s+want\s+to|i\s+want|want\s+to\s+order|'
    r'please\s+give\s+me|please|kindly|mujhe\s+chahiye|mujhe|chahiye|'
    r'dena|lena|please\s+give|give\s+me|add|can\s+i\s+get|get\s+me|'
    r'send\s+me|i\'ll\s+have|i\s+would\s+like|i\'d\s+like|bhai\s+dena|'
    r'yaar\s+dena|bhai|yaar|lao|la\s+do|mangwao|order\s+karo|'
    r'mujhe\s+ek|mujhe\s+do|ek|do|teen)\s+',
    re.IGNORECASE,
)

# ============================================================
# SESSION
# ============================================================

def _default_session() -> Dict[str, Any]:
    return {
        "lang": "en", "shown": [], "step": 0,
        "pending_order": {}, "cart": [],
        "pending_resolution": [],   # list of {product, qty, missing: ["size","spice","extras"]}
        "preferred_size": None, "preferred_spice": None,
        "frequent_items": [], "last_address": None,
        "order_count": 0, "last_shown_product": None, "last_order_items": [],
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
    session.update({"step": 0, "cart": [], "pending_order": {}, "pending_resolution": []})


def reset_for_new_order(session: Dict):
    session.update({"step": 0, "cart": [], "pending_order": {}, "pending_resolution": []})


def update_preferences(user_id: str, size=None, spice=None, product_title=None):
    session = get_user_session(user_id)
    if size:  session["preferred_size"]  = size
    if spice: session["preferred_spice"] = spice
    if product_title:
        freq = session.get("frequent_items", [])
        freq.append(product_title)
        session["frequent_items"] = freq[-10:]

# ============================================================
# ★ CORE: SMART ONE-SHOT ORDER PARSER
# ============================================================
# This is the key innovation in v15:
# Parse a single message and extract product + size + spice + extras + qty
# ALL AT ONCE before asking any questions.

def _extract_quantity_from_text(text: str) -> Tuple[int, str]:
    """Returns (quantity, remaining_text)"""
    t = text.strip()
    qty_match = re.match(
        r'^(\d+(?:st|nd|rd|th)?|' + '|'.join(re.escape(k) for k in QUANTITY_WORDS.keys()) + r')\s+',
        t, re.IGNORECASE
    )
    if qty_match:
        raw = qty_match.group(1).lower()
        raw_clean = re.sub(r'(st|nd|rd|th)$', '', raw)
        if raw_clean.isdigit():
            qty = int(raw_clean)
        else:
            qty = QUANTITY_WORDS.get(raw, QUANTITY_WORDS.get(raw_clean, 1))
        return qty, t[qty_match.end():].strip()
    return 1, t


def _extract_size_from_text(text: str, variants: List[Dict]) -> Tuple[str, str]:
    """
    Returns (matched_size_label, remaining_text_with_size_removed).
    Tries SIZE_HINTS first (longest match), then variant names directly.
    """
    t = text.lower()
    # Try SIZE_HINTS longest-first
    for sh in sorted(SIZE_HINTS, key=len, reverse=True):
        if sh.lower() in t:
            mv = _match_variant(variants, sh)
            if mv:
                cleaned = re.sub(re.escape(sh.lower()), '', t, flags=re.IGNORECASE).strip()
                return sh, cleaned

    # Try direct variant name match
    for v in sorted(variants, key=lambda x: len(x.get("size", "")), reverse=True):
        vsize = v.get("size", "").lower()
        if vsize and vsize in t:
            cleaned = t.replace(vsize, "").strip()
            return v["size"], cleaned

    # Numeric size patterns
    m = re.search(r'(\d+\.?\d*\s*(?:kg|g\b|ml|l\b))', t, re.IGNORECASE)
    if m:
        sh   = m.group(1).strip()
        mv   = _match_variant(variants, sh)
        if mv:
            cleaned = t[:m.start()].strip() + " " + t[m.end():].strip()
            return sh, cleaned.strip()

    return "", text


def _extract_spice_from_text(text: str, spice_levels: List[str]) -> Tuple[str, str]:
    """Returns (matched_spice, remaining_text)"""
    if not spice_levels:
        return "", text
    t = text.lower()
    for sl in sorted(spice_levels, key=len, reverse=True):
        if sl.lower() in t:
            cleaned = re.sub(re.escape(sl.lower()), '', t, flags=re.IGNORECASE).strip()
            return sl.strip().title(), cleaned
    return "", text


def _extract_extras_from_text(text: str, extras_options: List[Dict]) -> Tuple[List[str], str]:
    """Returns (chosen_extras_list, remaining_text)"""
    if not extras_options:
        return [], text
    q      = text.lower()
    chosen = []
    remaining = q
    for e in extras_options:
        name = e["name"].strip()
        nl   = name.lower()
        if nl in q:
            chosen.append(name.strip().title())
            remaining = remaining.replace(nl, "").strip()
            continue
        # Word-level fuzzy match
        extra_words = nl.split()
        query_words = re.findall(r'\w+', q)
        matched = sum(
            1 for ew in extra_words
            if any(SequenceMatcher(None, qw, ew).ratio() >= 0.75 or qw in ew or ew in qw
                   for qw in query_words)
        )
        if matched == len(extra_words):
            chosen.append(name.strip().title())
    return chosen, remaining


def smart_parse_single_item(raw_text: str, product: Dict) -> Dict:
    """
    v15 CORE: Given a raw message and a matched product, extract
    size + spice + extras + qty in one shot.
    Returns dict with keys: size, spice, extras, qty, missing (list of what's still needed)
    """
    variants       = product.get("variants", [])
    spice_levels   = product.get("spice_levels", [])
    extras_options = product.get("extras", [])

    # Remove order noise prefixes
    text = _ORDER_NOISE_PREFIXES.sub("", raw_text.lower().strip()).strip()

    # Extract quantity
    qty, text = _extract_quantity_from_text(text)

    # Extract size
    size_hint, text = _extract_size_from_text(text, variants)
    matched_variant = _match_variant(variants, size_hint) if size_hint else None

    # Extract spice
    spice, text = _extract_spice_from_text(text, spice_levels)

    # Extract extras
    extras, text = _extract_extras_from_text(text, extras_options)

    # Determine what's still missing
    missing = []
    if variants and not matched_variant:
        missing.append("size")
    if spice_levels and not spice:
        missing.append("spice")
    # Extras: only ask if product has extras AND user didn't mention any and didn't say "no extras"
    no_extras_words = ["no extra", "no extras", "without", "plain", "simple", "nothing", "nahi", "no"]
    user_declined_extras = any(w in raw_text.lower() for w in no_extras_words)
    if extras_options and not extras and not user_declined_extras:
        missing.append("extras")

    return {
        "qty":             qty,
        "size":            matched_variant["size"] if matched_variant else size_hint,
        "matched_variant": matched_variant,
        "spice":           spice,
        "extras":          extras,
        "missing":         missing,
    }


# ============================================================
# PRODUCT SEARCH (fixed scoring to prefer best match)
# ============================================================

def _find_product_by_query(query: str) -> Optional[Dict]:
    """
    v15: Fixed best-match scoring.
    Strongly prefers products whose title/category directly match query words.
    """
    if not query:
        return None

    q       = query.lower().strip()
    q_clean = _ORDER_NOISE_PREFIXES.sub("", q).strip()

    # Collect candidates
    candidates: Dict[str, Dict] = {}

    def _add_candidate(p: Dict):
        candidates[str(p.get("_id", id(p)))] = p

    # Strip size/spice/extras noise to get clean product name
    product_query = q_clean
    for sh in sorted(SIZE_HINTS, key=len, reverse=True):
        product_query = product_query.replace(sh.lower(), "").strip()
    product_query = product_query.strip()

    for lookup in [product_query, q_clean, q]:
        if lookup in PRODUCT_KEYWORD_INDEX:
            for p in PRODUCT_KEYWORD_INDEX[lookup]:
                _add_candidate(p)

    words = re.findall(r'\w+', product_query)
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

        # Exact match — highest priority
        if product_query == title or q_clean == title:
            score += 100
        if product_query in title or title in product_query:
            score += 50

        # Word overlap with title (weighted highly)
        q_words = set(re.findall(r"\w+", product_query))
        t_words = set(re.findall(r"\w+", title))
        overlap = q_words & t_words
        score  += len(overlap) * 15

        # Category keyword match
        for cat, kws in CATEGORY_KEYWORDS.items():
            if cat == category and any(kw in q_clean for kw in kws):
                score += 20
        if category and category in q_clean:
            score += 25
        if category and any(w == category for w in q_words):
            score += 30

        # Description overlap (low weight)
        d_words = set(re.findall(r"\w+", desc))
        score  += len(q_words & d_words) * 1

        # Popularity boost (small)
        score += float(product.get("trending_score", 0)) * 0.3
        score += float(product.get("rating", 0)) * 0.2

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


def parse_price_range(query: str) -> Dict[str, float]:
    q = query.lower().replace("rs","").replace("pkr","").replace("$","").replace("€","")
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


def filter_products(query: str) -> List[Dict]:
    price_range = parse_price_range(query)
    scored = [{"p": p, "s": score_product(query, p, price_range)} for p in PRODUCTS_DATA]
    return [x["p"] for x in sorted(scored, key=lambda x: x["s"], reverse=True) if x["s"] > 0.0][:8]

# ============================================================
# MULTI-ITEM PARSER (v15 rewrite)
# ============================================================

_MULTI_ITEM_SEPARATORS = re.compile(
    r'\b(?:and|aur|or|also|پھر|اور|saath|ke\s+saath|plus)\b|[,;+\n]',
    re.IGNORECASE
)


def parse_multi_item_order(text: str) -> List[Dict]:
    """
    Parse multi-item orders. Each part gets smart_parse_single_item applied.
    Returns list of {product, parsed} dicts.
    """
    parts   = _MULTI_ITEM_SEPARATORS.split(text)
    results = []

    for part in parts:
        part = part.strip()
        if not part or len(part) < 3:
            continue
        product = _find_product_by_query(part)
        if not product:
            continue
        parsed = smart_parse_single_item(part, product)
        results.append({"product": product, "parsed": parsed, "raw": part})

    return results

# ============================================================
# CART BUILDING
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


def create_order_from_cart(user_id: str, cart_items: List[Dict], address: str, delivery_charge: float = 0.0) -> str:
    if orders_col is None:
        return "db_error"
    subtotal = sum(item["total_item_price"] for item in cart_items)
    total    = subtotal + delivery_charge
    order    = {
        "user_id": user_id, "items": cart_items,
        "dish":           cart_items[0]["title"] if cart_items else "Order",
        "quantity":       sum(i["quantity"] for i in cart_items),
        "subtotal":       subtotal, "delivery_charge": delivery_charge,
        "total_price":    total, "address": address.strip(),
        "status": "Pending", "timestamp": datetime.utcnow().isoformat(),
        "customization": {
            "size":   cart_items[0].get("size", "") if cart_items else "",
            "spice":  cart_items[0].get("spice", "") if cart_items else "",
            "extras": ", ".join(cart_items[0].get("extras", [])) if cart_items else "",
        },
    }
    result = orders_col.insert_one(order)
    inc = {"total_orders": 1}
    for item in cart_items:
        if item.get("size"):  inc[f"size_preference.{item['size']}"]   = 1
        if item.get("spice"): inc[f"spice_preference.{item['spice']}"] = 1
        for extra in item.get("extras", []):
            inc[f"extras_preference.{extra}"] = 1
    _track(inc)
    session = get_user_session(user_id)
    session["order_count"]      = session.get("order_count", 0) + 1
    session["last_order_items"] = cart_items
    return str(result.inserted_id)

# ============================================================
# FAQ & SUGGESTIONS
# ============================================================

def get_faq_response(query: str, lang: str) -> Optional[str]:
    faq = BOT_DATA.get("faq", {})
    q   = query.lower().strip()
    mapping = {
        "delivery": ["deliver","ship","ارسال","versand","kab ayega","delivery time","kitne time","kab milega"],
        "return":   ["return","refund","واپسی","rückgabe","exchange","wapas","change"],
        "track":    ["track","order status","ٹریک","verfolgen","kahan hai"],
        "quality":  ["quality","fresh","معیار","qualität","ingredients","halal"],
        "hours":    ["open","close","hours","timing","اوقات","öffnungszeiten","band","khula"],
        "payment":  ["pay","payment","cash","card","ادائیگی","zahlung","online pay","easypaisa","jazzcash"],
    }
    for key, keywords in mapping.items():
        if any(kw in q for kw in keywords):
            entry = faq.get(key, {})
            if isinstance(entry, dict):
                return entry.get(lang, entry.get("en"))
            return entry or None
    return None


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
    r"building|near|opposite|گلی|سڑک|گھر|مکان|بلاک|فیز).{5,})", re.IGNORECASE)


def extract_address(text: str) -> Optional[str]:
    m = ADDRESS_PATTERN.search(text)
    if m: return m.group(0).strip()
    text = text.strip()
    return text if len(text) >= 15 else None

# ============================================================
# WHATSAPP API
# ============================================================

async def send_whatsapp_text(to: str, body: str):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        logger.warning("WhatsApp not configured")
        return
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
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
    body_text    = {"en": "Tap an item or ask me anything! 🍽️", "ur": "کوئی آئٹم چنیں! 🍽️", "de": "Tippen Sie auf ein Element! 🍽️"}
    footer_text  = {"en": "Powered by AI Bot v15", "ur": "AI بوٹ v15", "de": "AI Bot v15"}
    button_text  = {"en": "View Menu", "ur": "مینو دیکھیں", "de": "Menü"}
    section_title= {"en": "Our Menu", "ur": "ہمارا مینو", "de": "Speisekarte"}
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header[:60]},
            "body":   {"text": body_text.get(lang, body_text["en"])},
            "footer": {"text": footer_text.get(lang, footer_text["en"])},
            "action": {"button": button_text.get(lang, button_text["en"]),
                       "sections": [{"title": section_title.get(lang, section_title["en"]), "rows": rows}]},
        },
    }
    headers_h = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(WHATSAPP_API_URL, json=payload, headers=headers_h)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"WhatsApp list failed: {e}")


async def send_whatsapp_buttons(to: str, body: str, buttons: List[str]):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        return
    btn_list = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": b[:20]}} for i, b in enumerate(buttons[:3])]
    payload  = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {"type": "button", "body": {"text": body[:1024]}, "action": {"buttons": btn_list}},
    }
    headers_h = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(WHATSAPP_API_URL, json=payload, headers=headers_h)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"WhatsApp buttons failed: {e}")

# ============================================================
# INTENT HELPERS
# ============================================================

def _detect_price_menu_intent(q: str) -> bool:
    price_phrases = ["all prices","all flavours","all flavors","all pizza","all burger","all karahi",
                     "price list","show all","menu prices","full menu","all items price","all rates","complete menu"]
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
    affirmatives = {"yes","sure","ok","okay","haan","theek hai","bilkul","zaroor",
                    "absolutely","go ahead","proceed","yep","yeah","finalize","done",
                    "correct","right","ji","ji haan"}
    return q.strip().lower() in affirmatives


def _is_pure_greeting(q: str) -> bool:
    greeting_only = {"hi","hello","hey","salam","assalam","aoa","aslam","hallo",
                     "guten tag","good morning","good evening","good afternoon",
                     "as salam","walaikum","start","begin"}
    q_stripped = q.strip().lower()
    if q_stripped in greeting_only:
        return True
    if any(kw in q_stripped for kw in greeting_only) and not _is_product_query(q_stripped):
        return True
    return False


def _is_order_now_button(q: str) -> bool:
    cleaned = re.sub(r'[✅📋🛒🔄]', '', q).strip().lower()
    return cleaned in {"order now","order again","reorder"}

# ============================================================
# SMART AI FALLBACK
# ============================================================

async def _smart_fallback(from_number: str, user_message: str, lang: str) -> str:
    if not ANTHROPIC_API_KEY:
        return _static_fallback(lang)
    product_list = ", ".join(p.get("title", "") for p in PRODUCTS_DATA[:20]) or "various dishes"
    system_prompt = (
        f"You are a friendly WhatsApp restaurant assistant. The restaurant serves: {product_list}. "
        f"Respond in {'Urdu' if lang == 'ur' else 'German' if lang == 'de' else 'English'}, "
        f"keep replies under 3 sentences. Be warm, helpful, concise like real restaurant staff. "
        f"If completely unrelated to food, politely say you only help with restaurant queries."
    )
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200, "system": system_prompt,
                      "messages": [{"role": "user", "content": user_message}]},
            )
            data    = resp.json()
            ai_text = data.get("content", [{}])[0].get("text", "").strip()
            if ai_text: return ai_text
    except Exception as e:
        logger.warning(f"AI fallback failed: {e}")
    return _static_fallback(lang)


def _static_fallback(lang: str) -> str:
    return {
        "en": "I didn't quite get that 🤔\n\n• *Show menu* — see all items\n• *Order [dish name]* — place an order\n• *All prices* — full price list\n\nJust type the dish name to start! 😊",
        "ur": "مجھے سمجھ نہیں آیا 🤔\n\n• *مینو دکھائیں* — سب آئٹم\n• *[ڈش کا نام] آرڈر* — آرڈر دیں\n\nڈش کا نام لکھیں! 😊",
        "de": "Das habe ich nicht verstanden 🤔\n\n• *Menü anzeigen* — alle Artikel\n• *[Gericht] bestellen* — bestellen\n\nGerichtnamen eingeben! 😊",
    }.get(lang, "I didn't quite get that 🤔\n\nType a dish name to order! 😊")

# ============================================================
# ★ RESOLUTION QUEUE HANDLER
# v15 key: drives the minimal-question flow.
# Works through pending_resolution items one at a time.
# ============================================================

async def _ask_next_missing(from_num: str, session: Dict, lang: str):
    """
    Looks at session["pending_resolution"] and asks for the next piece
    of missing info. When all resolved, shows cart summary.
    """
    queue = session.get("pending_resolution", [])
    if not queue:
        await _show_cart_confirm(from_num, session, lang)
        return

    current = queue[0]
    product = current["product"]
    missing = current.get("missing", [])
    name    = product.get("title", "Item").strip().title()

    if "size" in missing:
        variants  = product.get("variants", [])
        size_list = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants)
        msg = {"en": f"📏 Size for *{name}*?\n\n{size_list}",
               "ur": f"📏 *{name}* کا سائز؟\n\n{size_list}",
               "de": f"📏 Größe für *{name}*?\n\n{size_list}"}
        session["step"] = 10
        await send_whatsapp_text(from_num, msg.get(lang, msg["en"]))
        return

    if "spice" in missing:
        spice_levels = product.get("spice_levels", [])
        options      = " / ".join(s.strip().title() for s in spice_levels)
        msg = {"en": f"🌶️ Spice level for *{name}*?\n  {options}",
               "ur": f"🌶️ *{name}* کے لیے مسالے کی سطح؟\n  {options}",
               "de": f"🌶️ Schärfe für *{name}*?\n  {options}"}
        session["step"] = 20
        await send_whatsapp_text(from_num, msg.get(lang, msg["en"]))
        return

    if "extras" in missing:
        extras_options = product.get("extras", [])
        extras_list    = "\n".join(f"  • {e['name'].strip().title()} +PKR {e['price']}" for e in extras_options)
        msg = {"en": f"➕ Extras with *{name}*?\n{extras_list}\n\n(Type names or 'no')",
               "ur": f"➕ *{name}* کے ساتھ اضافی؟\n{extras_list}\n\n(نام لکھیں یا 'no')",
               "de": f"➕ Extras für *{name}*?\n{extras_list}\n\n(Namen oder 'nein')"}
        session["step"] = 30
        await send_whatsapp_text(from_num, msg.get(lang, msg["en"]))
        return

    # Nothing missing for this item — build and add to cart
    parsed      = current["parsed"]
    cart_item   = build_cart_item(product, parsed["size"], parsed["spice"], parsed["extras"], parsed["qty"])
    session["cart"].append(cart_item)
    queue.pop(0)
    session["pending_resolution"] = queue
    await _ask_next_missing(from_num, session, lang)


async def _show_cart_confirm(from_num: str, session: Dict, lang: str):
    """Show cart summary with confirm/add more/clear buttons."""
    cart  = session.get("cart", [])
    total = _recalc_cart(cart)
    if not cart:
        session["step"] = 0
        msg = {"en": "🛒 Cart is empty. What would you like to order? 🍽️",
               "ur": "🛒 ٹوکری خالی ہے۔ کیا آرڈر کرنا ہے؟ 🍽️",
               "de": "🛒 Warenkorb leer. Was möchten Sie bestellen? 🍽️"}
        await send_whatsapp_text(from_num, msg.get(lang, msg["en"]))
        return
    summary = _build_cart_summary(cart, total, lang)
    msg = {"en": f"{summary}\n\n✅ Confirm or add more?",
           "ur": f"{summary}\n\n✅ تصدیق کریں یا مزید شامل کریں؟",
           "de": f"{summary}\n\n✅ Bestätigen oder mehr?"}
    session["step"] = 2
    await send_whatsapp_buttons(from_num, msg.get(lang, msg["en"]),
                                ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])


async def _handle_full_price_display(from_num: str, q: str, lang: str):
    category = _detect_category_from_query(q)
    if category:
        products = _products_by_category(category) or filter_products(q)
        cat_name = category.capitalize()
        emoji_map = {"pizza":"🍕","burger":"🍔","biryani":"🍛","drinks":"🥤","karahi":"🥘",
                     "dessert":"🍰","rice":"🍚","rolls":"🌯","chicken":"🍗","beef":"🥩",
                     "mutton":"🍖","fish":"🐟","soup":"🍲","salad":"🥗","bread":"🫓",
                     "pasta":"🍝","steak":"🥩","shawarma":"🌯","sandwich":"🥪"}
        emoji = emoji_map.get(category, "🍽️")
        title = {"en": f"{cat_name} Menu & Prices", "ur": f"{cat_name} مینو اور قیمتیں", "de": f"{cat_name} Menü & Preise"}.get(lang, f"{cat_name} Menu & Prices")
    else:
        products = PRODUCTS_DATA[:15]
        emoji    = "🍽️"
        title    = {"en": "Full Menu & Prices", "ur": "مکمل مینو اور قیمتیں", "de": "Vollständiges Menü & Preise"}.get(lang, "Full Menu & Prices")
    if not products:
        msg = {"en": "No products found. Try again! 🙏", "ur": "کوئی آئٹم نہیں ملا۔ 🙏", "de": "Keine Produkte. 🙏"}
        await send_whatsapp_text(from_num, msg.get(lang, msg["en"]))
        return
    await send_whatsapp_text(from_num, _build_full_price_menu(products, emoji, title))

# ============================================================
# ADDRESS + ORDER FINALIZATION
# ============================================================

async def _finalize_order(from_num: str, session: Dict, address: str, lang: str):
    cart_items      = session.get("cart", [])
    subtotal        = _recalc_cart(cart_items)
    delivery_charge = calculate_delivery_charge(subtotal, address)
    grand_total     = subtotal + delivery_charge
    summary         = _build_cart_summary(cart_items, subtotal, lang, delivery_charge, show_delivery=True)
    order_id        = create_order_from_cart(from_num, cart_items, address, delivery_charge)

    session["last_address"] = address
    reset_cart_only(session)

    if order_id == "db_error":
        msg = {"en": "⚠️ Issue placing order. Please try again.",
               "ur": "⚠️ آرڈر میں مسئلہ۔ دوبارہ کوشش کریں۔",
               "de": "⚠️ Fehler bei der Bestellung. Bitte erneut."}
        await send_whatsapp_text(from_num, msg.get(lang, msg["en"]))
        return

    cart_cats     = [i.get("category", "") for i in cart_items]
    dominant_cat  = max(set(cart_cats), key=cart_cats.count) if cart_cats else ""
    delivery_time = get_delivery_time(dominant_cat)

    conf = {
        "en": (f"✅ *Order Confirmed!* 🎉\n\n{summary}\n\n"
               f"📍 {address}\n🔖 Order ID: #{order_id[-6:]}\n"
               f"⏱️ Delivery in {delivery_time}\n\nType *new order* to order again!"),
        "ur": (f"✅ *آرڈر تصدیق!* 🎉\n\n{summary}\n\n"
               f"📍 {address}\n🔖 نمبر: #{order_id[-6:]}\n"
               f"⏱️ ڈلیوری: {delivery_time}\n\nنیا آرڈر کے لیے *new order* لکھیں۔"),
        "de": (f"✅ *Bestätigt!* 🎉\n\n{summary}\n\n"
               f"📍 {address}\n🔖 Nr: #{order_id[-6:]}\n"
               f"⏱️ Lieferung: {delivery_time}\n\n*new order* für neue Bestellung."),
    }
    await send_whatsapp_text(from_num, conf.get(lang, conf["en"]))

# ============================================================
# MAIN WEBHOOK
# ============================================================

@app.get("/webhook")
async def verify_webhook(hub_mode: str = None, hub_verify_token: str = None, hub_challenge: str = None):
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

        lang = session_lang if is_button else detect_language(msg_text, session_lang)
        if not is_button:
            session["lang"] = lang

        q    = msg_text.lower().strip()
        step = session.get("step", 0)

        _track({"total_searches": 1, f"supported_languages.{lang}": 1})

        # ═══════════════════════════════════════════════════════
        # P0 — New order reset
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["new_order"]):
            reset_for_new_order(session)
            last_addr = session.get("last_address")
            hint = (f"\n📍 Last address: _{last_addr}_\n(type *same* to reuse)" if last_addr else "") if lang == "en" else (f"\n📍 پرانا پتہ: _{last_addr}_\n(*same* لکھیں)" if last_addr else "") if lang == "ur" else ""
            msg_new = {"en": f"🆕 Fresh start! What would you like? 🍽️{hint}",
                       "ur": f"🆕 نیا آرڈر! کیا آرڈر کرنا ہے؟ 🍽️{hint}",
                       "de": f"🆕 Neue Bestellung! Was möchten Sie? 🍽️"}
            await send_whatsapp_buttons(from_num, msg_new.get(lang, msg_new["en"]),
                                        ["View Menu 📋", "Order Again 🔄", "Contact Us 📞"])
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # P1 — Cancel
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["cancel"]):
            has_cart = session.get("cart") or session.get("pending_order") or session.get("pending_resolution")
            reset_cart_only(session)
            if has_cart:
                m = {"en": "🗑️ Order cancelled. What else can I get you? 🍽️",
                     "ur": "🗑️ آرڈر منسوخ۔ کیا آرڈر کرنا ہے؟ 🍽️",
                     "de": "🗑️ Storniert. Was möchten Sie? 🍽️"}
            else:
                m = {"en": "ℹ️ No active order. Ready to start? 🍽️",
                     "ur": "ℹ️ کوئی آرڈر نہیں۔ شروع کریں؟ 🍽️",
                     "de": "ℹ️ Keine Bestellung. Starten? 🍽️"}
            await send_whatsapp_buttons(from_num, m.get(lang, m["en"]), ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"])
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # Thanks (step 0 only, not product query)
        # ═══════════════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["thanks"]) and step == 0 and not _is_product_query(q):
            m = {"en": "You're welcome! 😊 Anything else?\n\n• *Show menu* — view items\n• *Place order* — order food",
                 "ur": "خوشی ہوئی! 😊 اور کچھ چاہیے؟",
                 "de": "Gern geschehen! 😊 Kann ich noch helfen?"}
            await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 10 — Waiting for SIZE
        # ═══════════════════════════════════════════════════════
        if step == 10:
            queue = session.get("pending_resolution", [])
            if queue:
                current  = queue[0]
                product  = current["product"]
                parsed   = current["parsed"]
                variants = product.get("variants", [])
                matched  = _match_variant(variants, msg_text)
                if not matched and variants:
                    sizes_str = " / ".join(v["size"] for v in variants)
                    m = {"en": f"⚠️ Please choose: *{sizes_str}*",
                         "ur": f"⚠️ یہ سائز چنیں: *{sizes_str}*",
                         "de": f"⚠️ Bitte wählen: *{sizes_str}*"}
                    await send_whatsapp_text(from_num, m.get(lang, m["en"]))
                    return JSONResponse({"status": "ok"})
                if matched:
                    parsed["size"]            = matched["size"]
                    parsed["matched_variant"] = matched
                    current["missing"] = [x for x in current["missing"] if x != "size"]
                    update_preferences(from_num, size=matched["size"])
            await _ask_next_missing(from_num, session, lang)
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 20 — Waiting for SPICE
        # ═══════════════════════════════════════════════════════
        if step == 20:
            queue = session.get("pending_resolution", [])
            if queue:
                current      = queue[0]
                product      = current["product"]
                parsed       = current["parsed"]
                spice_levels = product.get("spice_levels", [])
                matched_spice = next(
                    (s for s in sorted(spice_levels, key=len, reverse=True) if s.lower().strip() in q),
                    spice_levels[0] if spice_levels else ""
                )
                if matched_spice:
                    parsed["spice"]  = matched_spice.strip().title()
                    current["missing"] = [x for x in current["missing"] if x != "spice"]
                    update_preferences(from_num, spice=parsed["spice"])
            await _ask_next_missing(from_num, session, lang)
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 30 — Waiting for EXTRAS
        # ═══════════════════════════════════════════════════════
        if step == 30:
            queue = session.get("pending_resolution", [])
            if queue:
                current        = queue[0]
                product        = current["product"]
                parsed         = current["parsed"]
                extras_options = product.get("extras", [])
                skip_extras    = any(w in q for w in ["no","skip","nothing","nahi","nope","nein","nahin","plain","without"])
                if not skip_extras:
                    chosen, _ = _extract_extras_from_text(msg_text, extras_options)
                    parsed["extras"] = chosen
                else:
                    parsed["extras"] = []
                current["missing"] = [x for x in current["missing"] if x != "extras"]
            await _ask_next_missing(from_num, session, lang)
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 1 — Waiting for ADDRESS
        # ═══════════════════════════════════════════════════════
        if step == 1:
            # Handle "same" address
            if q.strip() in ["same", "same address", "same adress", "same add"]:
                address = session.get("last_address")
                if not address:
                    m = {"en": "⚠️ No previous address. Please type your full address.",
                         "ur": "⚠️ پرانا پتہ نہیں۔ مکمل پتہ لکھیں۔",
                         "de": "⚠️ Keine frühere Adresse."}
                    await send_whatsapp_text(from_num, m.get(lang, m["en"]))
                    return JSONResponse({"status": "ok"})
            else:
                address_candidate = extract_address(msg_text) or msg_text.strip()
                if not _is_valid_address(address_candidate):
                    m = {"en": "📍 Please share your *full* delivery address.\nExample: *House 12, Block B, Gulshan, Karachi*",
                         "ur": "📍 اپنا *مکمل* پتہ لکھیں۔\nمثال: *مکان 12، بلاک بی، گلشن، کراچی*",
                         "de": "📍 Bitte *vollständige* Adresse angeben."}
                    await send_whatsapp_text(from_num, m.get(lang, m["en"]))
                    return JSONResponse({"status": "ok"})
                address = address_candidate
            await _finalize_order(from_num, session, address, lang)
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 2 — Cart confirmation
        # ═══════════════════════════════════════════════════════
        if step == 2:
            if any(kw in q for kw in ["confirm","yes","okay","ok","haan","proceed","place","done","✅","theek","bilkul","zaroor","sure"]):
                last_addr = session.get("last_address")
                if last_addr:
                    m = {"en": f"📍 Deliver to last address?\n_{last_addr}_\n\nType *same* or enter new:",
                         "ur": f"📍 پرانے پتے پر؟\n_{last_addr}_\n\n*same* یا نیا پتہ:",
                         "de": f"📍 An letzte Adresse?\n_{last_addr}_\n\n*same* oder neue:"}
                else:
                    m = {"en": "📍 Share your full delivery address (house no., street, area, city):",
                         "ur": "📍 اپنا مکمل پتہ دیں:",
                         "de": "📍 Vollständige Lieferadresse:"}
                session["step"] = 1
                await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            elif any(kw in q for kw in ["add more","more","aur","➕","add"]):
                session["step"] = 0
                m = {"en": "Sure! What else would you like? 🍽️",
                     "ur": "بالکل! اور کیا شامل کرنا ہے؟ 🍽️",
                     "de": "Natürlich! Was noch? 🍽️"}
                await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            elif any(kw in q for kw in ["clear","reset","cancel","empty","🗑️"]):
                session["cart"] = []
                session["step"] = 0
                m = {"en": "🗑️ Cart cleared! What would you like to order?",
                     "ur": "🗑️ ٹوکری صاف! کیا آرڈر کرنا ہے؟",
                     "de": "🗑️ Warenkorb geleert!"}
                await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            else:
                await _show_cart_confirm(from_num, session, lang)
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # STEP 0 — Intent routing
        # ═══════════════════════════════════════════════════════

        # Order Now button
        if _is_order_now_button(q):
            last_items = session.get("last_order_items", [])
            if ("again" in q or "reorder" in q) and last_items:
                session["cart"] = list(last_items)
                await _show_cart_confirm(from_num, session, lang)
                return JSONResponse({"status": "ok"})
            last_product = session.get("last_shown_product")
            if last_product:
                reset_for_new_order(session)
                parsed   = smart_parse_single_item(last_product.get("title", ""), last_product)
                session["pending_resolution"] = [{"product": last_product, "parsed": parsed, "missing": parsed["missing"]}]
                await _ask_next_missing(from_num, session, lang)
                return JSONResponse({"status": "ok"})
            m = {"en": "What would you like to order? 🍽️", "ur": "کیا آرڈر کرنا ہے؟ 🍽️", "de": "Was möchten Sie? 🍽️"}
            await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            return JSONResponse({"status": "ok"})

        # Delivery charge inquiry
        if any(kw in q for kw in INTENT_KEYWORDS["delivery_charge"]):
            dc = BOT_DATA.get("delivery_charges", {})
            flat = float(dc.get("flat_charge", 0) or 0)
            free_above = float(dc.get("free_above", 0) or 0)
            per_area = dc.get("per_area", {})
            lines = []
            if free_above > 0:
                lines.append({"en": f"✅ Free delivery above PKR {int(free_above)}!", "ur": f"✅ PKR {int(free_above)} سے زیادہ پر مفت ڈلیوری!", "de": f"✅ Kostenlos ab PKR {int(free_above)}!"}.get(lang, f"Free delivery above PKR {int(free_above)}!"))
            if flat > 0 and not lines:
                lines.append({"en": f"🚚 Delivery: PKR {int(flat)}", "ur": f"🚚 ڈلیوری: PKR {int(flat)}", "de": f"🚚 Lieferung: PKR {int(flat)}"}.get(lang, f"Delivery: PKR {int(flat)}"))
            elif not lines:
                lines.append({"en": "🎉 We offer FREE delivery!", "ur": "🎉 مفت ڈلیوری!", "de": "🎉 KOSTENLOSE Lieferung!"}.get(lang, "FREE delivery!"))
            if per_area:
                area_lines = "\n".join(f"  • {k.title()}: PKR {int(v)}" for k, v in per_area.items())
                lines.append({"en": f"📍 Area charges:\n{area_lines}", "ur": f"📍 علاقہ چارجز:\n{area_lines}", "de": f"📍 Bereichsgebühren:\n{area_lines}"}.get(lang, f"Area charges:\n{area_lines}"))
            await send_whatsapp_text(from_num, "\n\n".join(lines))
            return JSONResponse({"status": "ok"})

        # Show cart
        if any(kw in q for kw in INTENT_KEYWORDS["cart"]):
            cart = session.get("cart", [])
            if cart:
                await _show_cart_confirm(from_num, session, lang)
            else:
                m = {"en": "🛒 Cart is empty. What would you like? 🍽️", "ur": "🛒 ٹوکری خالی۔ کیا آرڈر کرنا ہے؟ 🍽️", "de": "🛒 Leer. Was möchten Sie? 🍽️"}
                await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            return JSONResponse({"status": "ok"})

        # Clear cart
        if any(kw in q for kw in INTENT_KEYWORDS["clear"]):
            session["cart"] = []
            session["step"] = 0
            m = {"en": "🗑️ Cart cleared! What would you like?", "ur": "🗑️ ٹوکری صاف!", "de": "🗑️ Geleert!"}
            await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            return JSONResponse({"status": "ok"})

        # Confirm order (if cart exists)
        if any(kw in q for kw in INTENT_KEYWORDS["confirm"]) and session.get("cart"):
            last_addr = session.get("last_address")
            if last_addr:
                m = {"en": f"📍 Deliver to last address?\n_{last_addr}_\n\nType *same* or enter new:",
                     "ur": f"📍 پرانے پتے پر؟\n_{last_addr}_\n\n*same* یا نیا پتہ:",
                     "de": f"📍 Letzte Adresse?\n_{last_addr}_\n\n*same* oder neue:"}
            else:
                m = {"en": "📍 Full delivery address please:", "ur": "📍 مکمل پتہ دیں:", "de": "📍 Lieferadresse:"}
            session["step"] = 1
            await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            return JSONResponse({"status": "ok"})

        price_intent   = _detect_price_menu_intent(q)
        menu_intent    = any(kw in q for kw in INTENT_KEYWORDS["menu"])
        order_intent   = any(kw in q for kw in INTENT_KEYWORDS["order"])
        product_query  = _is_product_query(q)
        inquiry_intent = any(kw in q for kw in INTENT_KEYWORDS["inquiry"])
        is_multi       = bool(re.search(r'\b(?:and|aur|,|plus|also|اور)\b', q)) and product_query

        # Price display
        if price_intent:
            _track({"total_searches": 1})
            await _handle_full_price_display(from_num, q, lang)
            if not order_intent and not product_query:
                return JSONResponse({"status": "ok"})

        # Inquiry (not ordering)
        if inquiry_intent and not order_intent:
            product = _find_product_by_query(msg_text)
            if product:
                name       = product.get("title", "Item").strip().title()
                desc       = product.get("description", "")
                variants   = product.get("variants", [])
                spice_lvls = product.get("spice_levels", [])
                parts      = [f"🍽️ *{name}*"]
                if desc: parts.append(desc.strip())
                if variants:
                    size_list = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants)
                    parts.append(f"\n{'Sizes' if lang=='en' else 'سائز' if lang=='ur' else 'Größen'}:\n{size_list}")
                if spice_lvls:
                    parts.append(f"🌶️ {' / '.join(s.strip().title() for s in spice_lvls)}")
                parts.append({"en": "\nWant to order? 😊", "ur": "\nآرڈر کریں؟ 😊", "de": "\nBestellen? 😊"}.get(lang, "\nWant to order? 😊"))
                session["last_shown_product"] = product
                await send_whatsapp_buttons(from_num, "\n".join(parts), ["✅ Order Now", "📋 View Menu"])
                return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════════════
        # ★ MAIN ORDER HANDLER — v15 smart one-shot parsing
        # ═══════════════════════════════════════════════════════
        if order_intent or product_query:
            _track({"total_cart_additions": 1})

            if is_multi:
                # Multi-item order
                parsed_items = parse_multi_item_order(msg_text)
                if parsed_items:
                    resolution_queue = []
                    for pi in parsed_items:
                        product = pi["product"]
                        parsed  = pi["parsed"]
                        if not parsed["missing"]:
                            # All info available — add directly to cart
                            ci = build_cart_item(product, parsed["size"], parsed["spice"], parsed["extras"], parsed["qty"])
                            session["cart"].append(ci)
                        else:
                            resolution_queue.append({"product": product, "parsed": parsed, "missing": list(parsed["missing"])})
                    session["pending_resolution"] = resolution_queue
                    if resolution_queue:
                        await _ask_next_missing(from_num, session, lang)
                    else:
                        await _show_cart_confirm(from_num, session, lang)
                    return JSONResponse({"status": "ok"})

            # Single item order — smart parse
            product = _find_product_by_query(msg_text)
            if product:
                parsed = smart_parse_single_item(msg_text, product)
                session["last_shown_product"] = product
                name = product.get("title", "Item").strip().title()

                if not parsed["missing"]:
                    # ✅ ALL INFO EXTRACTED — add to cart immediately, ask address
                    ci = build_cart_item(product, parsed["size"], parsed["spice"], parsed["extras"], parsed["qty"])
                    session["cart"].append(ci)

                    # Build a nice confirmation of what was understood
                    size_label  = f" ({parsed['size']})"  if parsed["size"]   else ""
                    spice_label = f" 🌶️ {parsed['spice']}" if parsed["spice"]  else ""
                    extra_label = f" ➕ {', '.join(parsed['extras'])}" if parsed["extras"] else ""
                    qty_label   = f" ×{parsed['qty']}" if parsed["qty"] > 1 else ""
                    price       = ci["base_price"] + ci["extras_price"]

                    understood = {"en": f"✅ Got it! *{name}*{size_label}{spice_label}{extra_label}{qty_label} — PKR {int(price * parsed['qty'])}",
                                  "ur": f"✅ ٹھیک! *{name}*{size_label}{spice_label}{extra_label}{qty_label} — PKR {int(price * parsed['qty'])}",
                                  "de": f"✅ Verstanden! *{name}*{size_label}{spice_label}{extra_label}{qty_label} — PKR {int(price * parsed['qty'])}"}

                    # Check if there are more items in cart already
                    if len(session["cart"]) > 1:
                        await send_whatsapp_text(from_num, understood.get(lang, understood["en"]))
                        await _show_cart_confirm(from_num, session, lang)
                    else:
                        # Direct to address
                        session["step"] = 1
                        last_addr = session.get("last_address")
                        if last_addr:
                            addr_msg = {"en": f"{understood.get(lang, understood['en'])}\n\n📍 Deliver to last address?\n_{last_addr}_\n\nType *same* or enter new:",
                                        "ur": f"{understood.get('ur', understood['en'])}\n\n📍 پرانے پتے پر؟\n_{last_addr}_\n\n*same* یا نیا پتہ:",
                                        "de": f"{understood.get('de', understood['en'])}\n\n📍 Letzte Adresse?\n_{last_addr}_\n\n*same* oder neue:"}
                        else:
                            addr_msg = {"en": f"{understood.get(lang, understood['en'])}\n\n📍 Share your delivery address (house no., street, area, city):",
                                        "ur": f"{understood.get('ur', understood['en'])}\n\n📍 اپنا پتہ دیں:",
                                        "de": f"{understood.get('de', understood['en'])}\n\n📍 Lieferadresse:"}
                        await send_whatsapp_text(from_num, addr_msg.get(lang, addr_msg["en"]))
                else:
                    # Missing some info — enqueue and ask
                    session["pending_resolution"] = [{"product": product, "parsed": parsed, "missing": list(parsed["missing"])}]
                    await _ask_next_missing(from_num, session, lang)
                return JSONResponse({"status": "ok"})

        # Menu display
        if menu_intent:
            _track({"total_searches": 1})
            products = filter_products(msg_text) or PRODUCTS_DATA[:8]
            header   = {"en": "🍽️ Our Menu", "ur": "🍽️ ہمارا مینو", "de": "🍽️ Speisekarte"}.get(lang, "🍽️ Our Menu")
            if products:
                await send_whatsapp_list(from_num, header, products, lang)
            else:
                m = {"en": "Menu unavailable. Try again! 🙏", "ur": "مینو دستیاب نہیں۔ 🙏", "de": "Menü nicht verfügbar. 🙏"}
                await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            return JSONResponse({"status": "ok"})

        # FAQ
        faq_resp = get_faq_response(msg_text, lang)
        if faq_resp:
            await send_whatsapp_text(from_num, faq_resp)
            return JSONResponse({"status": "ok"})

        # Discount
        if any(kw in q for kw in INTENT_KEYWORDS["discount"]):
            disc = BOT_DATA.get("discount_message", {}).get(lang, BOT_DATA.get("discount_message", {}).get("en"))
            if disc:
                await send_whatsapp_text(from_num, disc)
                return JSONResponse({"status": "ok"})

        # Order status
        if any(kw in q for kw in INTENT_KEYWORDS["status"]) and orders_col:
            latest = orders_col.find_one({"user_id": from_num}, sort=[("timestamp", DESCENDING)])
            if latest:
                dish_name    = (latest.get("dish") or latest.get("items", [{}])[0].get("title", "Order")).strip().title()
                status       = latest.get("status", "Pending")
                status_emoji = {"Pending":"⏳","Accepted":"✅","Processing":"👨‍🍳","Delivered":"🚗","Rejected":"❌"}.get(status, "📦")
                m = {"en": f"{status_emoji} *{dish_name}*: {status} | ID: #{str(latest.get('_id',''))[-6:]}",
                     "ur": f"{status_emoji} *{dish_name}*: {status} | نمبر: #{str(latest.get('_id',''))[-6:]}",
                     "de": f"{status_emoji} *{dish_name}*: {status} | Nr: #{str(latest.get('_id',''))[-6:]}"}
                await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            else:
                m = {"en": "No orders yet. Place your first order! 🍽️", "ur": "ابھی تک کوئی آرڈر نہیں۔ 🍽️", "de": "Noch keine Bestellungen. 🍽️"}
                await send_whatsapp_text(from_num, m.get(lang, m["en"]))
            return JSONResponse({"status": "ok"})

        # Pure greeting (checked AFTER product detection)
        if _is_pure_greeting(q):
            greeting = BOT_DATA.get("initial_message", {}).get(lang, "Welcome! 🍽️")
            sugs     = get_suggestions(from_num, lang)
            reply    = greeting + ("\n\n💡 " + "\n• ".join(sugs) if sugs else "")
            await send_whatsapp_buttons(from_num, reply, ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"])
            return JSONResponse({"status": "ok"})

        # Product name found in index — show info + order button
        matched_product = _find_product_by_query(msg_text)
        if matched_product:
            name     = matched_product.get("title", "Item").strip().title()
            variants = matched_product.get("variants", [])
            session["last_shown_product"] = matched_product
            if variants:
                size_list = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants)
                m = {"en": f"🍽️ *{name}*\n\n{size_list}\n\nWould you like to order?",
                     "ur": f"🍽️ *{name}*\n\n{size_list}\n\nآرڈر کریں؟",
                     "de": f"🍽️ *{name}*\n\n{size_list}\n\nBestellen?"}
            else:
                price_str = f"PKR {matched_product.get('price', 'N/A')}"
                m = {"en": f"🍽️ *{name}* — {price_str}\n\nOrder?", "ur": f"🍽️ *{name}* — {price_str}\n\nآرڈر؟", "de": f"🍽️ *{name}* — {price_str}\n\nBestellen?"}
            await send_whatsapp_buttons(from_num, m.get(lang, m["en"]), ["✅ Order Now", "📋 View Menu"])
            return JSONResponse({"status": "ok"})

        # AI fallback
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
        try: body[field] = float(body.get(field, 0) or 0)
        except: body[field] = 0.0
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
        try: body[field] = float(body.get(field, 0) or 0)
        except: body[field] = 0.0
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
        return JSONResponse({"message": "Invalid JSON"}, status_code=400)
    user_id    = body.get("user_id")
    product_id = body.get("product_id")
    size       = body.get("size", "").strip()
    spice      = body.get("spice", "").strip()
    extras     = body.get("extras", [])
    quantity   = int(body.get("quantity", 1))
    if not user_id or not product_id:
        return JSONResponse({"message": "user_id and product_id required"}, status_code=400)
    if products_col is None or carts_col is None:
        return JSONResponse({"message": "DB not connected"}, status_code=500)
    try: product = products_col.find_one({"_id": ObjectId(product_id)})
    except Exception: product = products_col.find_one({"title": product_id})
    if not product:
        return JSONResponse({"message": "Product not found"}, status_code=404)
    cart_item = build_cart_item(product, size, spice, extras, quantity)
    cart      = carts_col.find_one({"user_id": user_id})
    if not cart:
        cart = {"user_id": user_id, "items": [], "total_price": 0, "created_at": datetime.utcnow().isoformat()}
    items    = cart.get("items", [])
    existing = next((i for i in items if i["product_id"] == str(product["_id"]) and i.get("size") == size), None)
    if existing:
        existing["quantity"] += quantity
        existing["total_item_price"] = (existing["base_price"] + existing["extras_price"]) * existing["quantity"]
    else:
        items.append(cart_item)
    total = _recalc_cart(items)
    carts_col.update_one({"user_id": user_id}, {"$set": {"items": items, "total_price": total, "updated_at": datetime.utcnow().isoformat()}}, upsert=True)
    _track({"total_cart_additions": 1})
    updated = carts_col.find_one({"user_id": user_id})
    return JSONResponse({"message": "Added to cart", "cart": _str_id(updated)})


@app.post("/api/cart/remove")
async def cart_remove(request: Request):
    try: body = await request.json()
    except: return JSONResponse({"message": "Invalid JSON"}, status_code=400)
    user_id = body.get("user_id"); product_id = body.get("product_id"); size = body.get("size", "").strip()
    if carts_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
    cart = carts_col.find_one({"user_id": user_id})
    if not cart: return JSONResponse({"message": "Cart not found"}, status_code=404)
    items = [i for i in cart.get("items", []) if not (i["product_id"] == product_id and i.get("size") == size)]
    total = _recalc_cart(items)
    carts_col.update_one({"user_id": user_id}, {"$set": {"items": items, "total_price": total}})
    return JSONResponse({"message": "Removed", "cart": _str_id(carts_col.find_one({"user_id": user_id}))})


@app.post("/api/cart/update")
async def cart_update(request: Request):
    try: body = await request.json()
    except: return JSONResponse({"message": "Invalid JSON"}, status_code=400)
    user_id = body.get("user_id"); product_id = body.get("product_id"); size = body.get("size","").strip(); quantity = int(body.get("quantity", 1))
    if carts_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
    cart = carts_col.find_one({"user_id": user_id})
    if not cart: return JSONResponse({"message": "Cart not found"}, status_code=404)
    items = cart.get("items", [])
    for it in items:
        if it["product_id"] == product_id and it.get("size") == size:
            if quantity <= 0: items.remove(it)
            else:
                it["quantity"] = quantity
                it["total_item_price"] = (it["base_price"] + it["extras_price"]) * quantity
            break
    total = _recalc_cart(items)
    carts_col.update_one({"user_id": user_id}, {"$set": {"items": items, "total_price": total}})
    return JSONResponse({"message": "Updated", "cart": _str_id(carts_col.find_one({"user_id": user_id}))})


@app.get("/api/cart/{user_id}")
async def cart_get(user_id: str):
    if carts_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
    cart = carts_col.find_one({"user_id": user_id})
    if not cart: return JSONResponse({"user_id": user_id, "items": [], "total_price": 0})
    return JSONResponse(_str_id(cart))

# ============================================================
# ORDERS API
# ============================================================

@app.get("/api/orders")
async def get_orders(status: Optional[str] = None):
    if orders_col is None: return {"orders": []}
    query  = {"status": status} if status else {}
    orders = [_str_id(o) for o in orders_col.find(query).sort("timestamp", DESCENDING).limit(100)]
    return {"orders": orders}


@app.post("/api/orders/{order_id}/status")
async def update_order_status(order_id: str, request: Request):
    if orders_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
    try: body = await request.json()
    except: return JSONResponse({"message": "Invalid JSON"}, status_code=400)
    new_status = body.get("status")
    valid = ["Pending","Accepted","Rejected","Processing","Delivered"]
    if new_status not in valid: return JSONResponse({"message": f"Invalid. Use: {valid}"}, status_code=400)
    result = orders_col.update_one({"_id": ObjectId(order_id)}, {"$set": {"status": new_status}})
    if result.matched_count:
        order = orders_col.find_one({"_id": ObjectId(order_id)})
        if order:
            dish_name = (order.get("dish") or order.get("items",[{}])[0].get("title","Order")).strip().title()
            status_msg = {
                "Pending":    f"⏳ Your order *{dish_name}* is pending.",
                "Accepted":   f"✅ Your *{dish_name}* order is accepted! Preparing now!",
                "Processing": f"👨‍🍳 Your *{dish_name}* is being prepared!",
                "Delivered":  f"🚗 Your *{dish_name}* is on its way!",
                "Rejected":   f"❌ Sorry, *{dish_name}* order was rejected. Contact support.",
            }
            asyncio.create_task(send_whatsapp_text(order["user_id"], status_msg.get(new_status, f"📦 {new_status}")))
        return JSONResponse({"message": f"Updated to {new_status}", "status": "success"})
    return JSONResponse({"message": "Not found."}, status_code=404)

# ============================================================
# FAQ API
# ============================================================

@app.get("/api/faqs")
async def get_faqs():
    load_data_realtime()
    return {"faqs": BOT_DATA.get("faq", {})}


@app.post("/api/faqs")
async def update_faqs(request: Request):
    if meta_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
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
    if meta_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
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
    return {"delivery_time": BOT_DATA.get("delivery_time", "35-45 mins"), "delivery_time_exceptions": BOT_DATA.get("delivery_time_exceptions", {})}


@app.post("/api/delivery-time")
async def update_delivery_time(request: Request):
    if meta_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
    try: body = await request.json()
    except: return JSONResponse({"message": "Invalid JSON"}, status_code=400)
    update_fields: Dict[str, Any] = {}
    if "delivery_time" in body: update_fields["delivery_time"] = str(body["delivery_time"]).strip()
    if "delivery_time_exceptions" in body:
        if not isinstance(body["delivery_time_exceptions"], dict): return JSONResponse({"message": "Must be object"}, status_code=400)
        update_fields["delivery_time_exceptions"] = {k.lower().strip(): str(v).strip() for k, v in body["delivery_time_exceptions"].items()}
    if not update_fields: return JSONResponse({"message": "No valid fields."}, status_code=400)
    meta_col.update_one({"type": "config"}, {"$set": update_fields}, upsert=True)
    load_data_realtime()
    return JSONResponse({"message": "Updated!", "status": "success", "delivery_time": BOT_DATA.get("delivery_time"), "delivery_time_exceptions": BOT_DATA.get("delivery_time_exceptions", {})})

# ============================================================
# DELIVERY CHARGES API
# ============================================================

@app.get("/api/delivery-charges")
async def get_delivery_charges():
    load_data_realtime()
    return {"delivery_charges": BOT_DATA.get("delivery_charges", {})}


@app.post("/api/delivery-charges")
async def update_delivery_charges(request: Request):
    if meta_col is None: return JSONResponse({"message": "DB not connected"}, status_code=500)
    try: body = await request.json()
    except: return JSONResponse({"message": "Invalid JSON"}, status_code=400)
    allowed = {"flat_charge","free_above","per_area","free_keywords"}
    if not any(k in body for k in allowed): return JSONResponse({"message": f"Need one of: {allowed}"}, status_code=400)
    existing = BOT_DATA.get("delivery_charges", {})
    updated_dc = {"flat_charge": float(existing.get("flat_charge", 0) or 0), "free_above": float(existing.get("free_above", 0) or 0), "per_area": existing.get("per_area", {}), "free_keywords": existing.get("free_keywords", [])}
    if "flat_charge" in body:
        try: updated_dc["flat_charge"] = float(body["flat_charge"])
        except: return JSONResponse({"message": "flat_charge must be number"}, status_code=400)
    if "free_above" in body:
        try: updated_dc["free_above"] = float(body["free_above"])
        except: return JSONResponse({"message": "free_above must be number"}, status_code=400)
    if "per_area" in body:
        if not isinstance(body["per_area"], dict): return JSONResponse({"message": "per_area must be object"}, status_code=400)
        updated_dc["per_area"] = {k.lower().strip(): float(v) for k, v in body["per_area"].items()}
    if "free_keywords" in body:
        if not isinstance(body["free_keywords"], list): return JSONResponse({"message": "free_keywords must be list"}, status_code=400)
        updated_dc["free_keywords"] = [str(kw).lower().strip() for kw in body["free_keywords"]]
    meta_col.update_one({"type": "config"}, {"$set": {"delivery_charges": updated_dc}}, upsert=True)
    load_data_realtime()
    return JSONResponse({"message": "Updated!", "status": "success", "delivery_charges": BOT_DATA.get("delivery_charges", {})})

# ============================================================
# ANALYTICS API
# ============================================================

@app.get("/api/analytics")
async def get_analytics():
    if analytics_col is None: return {}
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
# FULL DATA API
# ============================================================

@app.get("/api/data")
async def get_api_data():
    load_data_realtime()
    try:
        orders = []; analytics = {}
        if orders_col is not None:
            orders = [_str_id(o) for o in orders_col.find({}).sort("timestamp", DESCENDING).limit(50)]
        if analytics_col is not None:
            analytics = _str_id(analytics_col.find_one({"type": "analytics"}) or {})
        return {
            "products": PRODUCTS_DATA, "orders": orders, "analytics": analytics,
            "config": {"faq": BOT_DATA.get("faq", {}), "initial_message": BOT_DATA.get("initial_message", {}),
                       "discount_message": BOT_DATA.get("discount_message", {}),
                       "supported_languages": BOT_DATA.get("supported_languages", ["en","ur","de"]),
                       "smart_suggestions": BOT_DATA.get("smart_suggestions", {}),
                       "delivery_charges": BOT_DATA.get("delivery_charges", {})},
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
    logger.info("🚀 Restaurant Bot v15.0 started!")
    logger.info(f"   Products loaded    : {len(PRODUCTS_DATA)}")
    logger.info(f"   Keyword index size : {len(PRODUCT_KEYWORD_INDEX)}")
    logger.info(f"   Delivery time      : {get_delivery_time()}")
    logger.info(f"   Delivery charges   : {BOT_DATA.get('delivery_charges', {})}")
    logger.info(f"   WhatsApp connected : {'✅' if WHATSAPP_TOKEN else '❌'}")
    logger.info(f"   MongoDB connected  : {'✅' if products_col is not None else '❌'}")
    logger.info(f"   AI fallback        : {'✅' if ANTHROPIC_API_KEY else '⚠️ Static fallback'}")
