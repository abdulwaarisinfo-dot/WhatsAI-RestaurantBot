"""
WhatsApp AI Restaurant Bot — FastAPI Backend (Production v7.0)
==============================================================
v7.0 Upgrades over v6.0:
  ✅ CART RESET after address → order confirmed (clean slate per order)
  ✅ "new order" intent → hard reset cart + step + pending_order
  ✅ Address tracked per-user in session (reused on new orders if unchanged)
  ✅ Orders persisted in MongoDB with full cart snapshot
  ✅ Full menu pricing display: ALL variants per product (not just first)
  ✅ Mixed intent handling: "order X and show all prices" → both handled
  ✅ Multi-item order: "1kg karahi + 2 burgers + 3 pepsi" fully supported
  ✅ Session continuity: step tracking never lost mid-flow
  ✅ ZERO hardcoded prices — 100% database-driven
  ✅ Analytics: size, spice, extras, language, cart additions tracked
  ✅ Admin CRM API fully compatible
  ✅ Order status webhook → WhatsApp notification
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

# ============================================================
# INITIAL SETUP
# ============================================================

load_dotenv()
DetectorFactory.seed = 0
logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RestaurantBot.v7")

BOT_DATA: Dict[str, Any] = {}
PRODUCTS_DATA: List[Dict[str, Any]] = []

# In-memory session store: { phone_number: SessionDict }
USER_SESSIONS: Dict[str, Dict[str, Any]] = {}

# ── FIX #2: In-memory rate limiter ──────────────────────────
_rate_store: Dict[str, list] = defaultdict(list)
RATE_LIMIT_PER_MINUTE = 10  # max messages per user per minute

def _is_rate_limited(user_id: str) -> bool:
    now = time.time()
    timestamps = [t for t in _rate_store[user_id] if now - t < 60]
    _rate_store[user_id] = timestamps
    if len(timestamps) >= RATE_LIMIT_PER_MINUTE:
        return True
    _rate_store[user_id].append(now)
    return False
# ────────────────────────────────────────────────────────────

app = FastAPI(
    title="WhatsApp AI Restaurant Bot v7.0",
    version="7.0",
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

# ── FIX #4: Updated to Graph API v22.0 ──────────────────────
WHATSAPP_API_URL = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_ID}/messages"
# ────────────────────────────────────────────────────────────

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
    """Convert MongoDB ObjectId to string in-place."""
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
    """
    Normalize user size input → DB format.
      'half kg' → '0.5kg'  |  '250g' → '0.25kg'  |  'large' → 'Large'
    """
    s = raw.lower().strip()
    s = re.sub(r'half\s*kg?', '0.5kg', s)
    s = re.sub(r'quarter\s*kg?', '0.25kg', s)
    s = re.sub(r'(\d+\.?\d*)\s*gram[s]?', lambda m: f"{float(m.group(1))/1000}kg", s)
    s = re.sub(r'(\d+\.?\d*)\s*g\b',      lambda m: f"{float(m.group(1))/1000}kg", s)
    s = re.sub(r'(\d+\.?\d*)\s*kg',       lambda m: f"{float(m.group(1))}kg",      s)
    s = re.sub(r'(\d+\.?\d*)\s*ml',       lambda m: f"{int(float(m.group(1)))}ml", s)
    s = re.sub(r'(\d+\.?\d*)\s*l\b',      lambda m: f"{float(m.group(1))}l",       s)
    size_map = {
        'small': 'Small', 'medium': 'Medium', 'large': 'Large',
        'regular': 'Regular', 'xl': 'XL', 'xxl': 'XXL',
        'full': '1kg', 'half': '0.5kg',
    }
    for k, v in size_map.items():
        if k in s:
            return v
    return s.strip()


def _match_variant(variants: List[Dict], size_hint: str) -> Optional[Dict]:
    """Fuzzy-match user size hint to a DB variant dict."""
    if not variants or not size_hint:
        return None
    normalized = _normalize_size(size_hint)
    for v in variants:
        if v.get("size", "").lower() == normalized.lower():
            return v
    for v in variants:
        vs = v.get("size", "").lower()
        if normalized.lower() in vs or vs in normalized.lower():
            return v
    return None


def _recalc_cart(cart_items: List[Dict]) -> float:
    return sum(
        (item.get("base_price", 0) + item.get("extras_price", 0)) * item.get("quantity", 1)
        for item in cart_items
    )


def _build_cart_summary(items: List[Dict], total: float) -> str:
    lines = ["🛒 *Your Cart:*\n"]
    for item in items:
        name    = item.get("title", "Item")
        size    = item.get("size", "")
        qty     = item.get("quantity", 1)
        price   = (item.get("base_price", 0) + item.get("extras_price", 0)) * qty
        extras  = ", ".join(item.get("extras", []))
        spice   = item.get("spice", "")
        line    = f"• *{name}*"
        if size:   line += f" ({size})"
        if qty > 1: line += f" ×{qty}"
        line += f" — PKR {int(price)}"
        if extras: line += f"\n  ➕ Extras: {extras}"
        if spice:  line += f"\n  🌶️ Spice: {spice}"
        lines.append(line)
    lines.append(f"\n💰 *Total: PKR {int(total)}*")
    return "\n".join(lines)


def _build_full_price_menu(products: List[Dict], category_emoji: str = "🍽️", title: str = "Menu & Prices") -> str:
    """
    Build FULL pricing display — ALL variants per product.
    Never shows only first variant. Always shows complete breakdown.
    """
    lines = [f"{category_emoji} *{title}*\n"]
    for product in products:
        lines.append(f"• *{product.get('title', 'Item')}*")
        variants = product.get("variants", [])
        if variants:
            for v in variants:
                lines.append(f"  ‣ {v.get('size', 'N/A')} — PKR {v.get('price', '?')}")
        else:
            price = product.get("price", "N/A")
            lines.append(f"  ‣ PKR {price}")
        lines.append("")  # blank line between products
    return "\n".join(lines).strip()

# ============================================================
# DATA LOADER
# ============================================================

def load_data_realtime():
    global PRODUCTS_DATA, BOT_DATA
    if products_col is None or meta_col is None:
        return
    try:
        PRODUCTS_DATA = [_str_id(p) for p in products_col.find({})]
        meta = meta_col.find_one({"type": "config"}) or meta_col.find_one({})
        if meta:
            BOT_DATA = _str_id(meta)
        else:
            BOT_DATA = {
                "supported_languages": ["en", "ur", "de"],
                "initial_message": {"en": "Welcome! 🍽️ How can I help you today?"},
                "faq": {},
                "smart_suggestions": {},
            }
        logger.info(f"Data synced | Products: {len(PRODUCTS_DATA)}")
    except Exception as e:
        logger.error(f"Data load error: {e}")

# ============================================================
# ANALYTICS
# ============================================================

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

def detect_language(text: str) -> str:
    try:
        if not text or not text.strip():
            return "en"
        if any("\u0600" <= c <= "\u06FF" for c in text):
            return "ur"
        lang = detect(text)
        if lang.startswith("ur"): return "ur"
        if lang.startswith("de"): return "de"
        return "en"
    except Exception:
        return "en"

# ============================================================
# KEYWORD DATABASES
# ============================================================

CATEGORY_KEYWORDS = {
    "burger":  ["burger", "برگر", "brgr", "cheeseburger", "double burger", "zinger"],
    "pizza":   ["pizza", "پیزا", "margherita", "pepperoni", "pizza slice", "tikka pizza"],
    "biryani": ["biryani", "بریانی", "dum biryani", "chicken biryani", "beef biryani"],
    "drinks":  ["drink", "مشروب", "juice", "cola", "water", "سافٹ ڈرنک", "lassi",
                "coke", "pepsi", "7up", "sprite", "fanta", "soda", "cold drink"],
    "dessert": ["dessert", "مٹھائی", "cake", "kheer", "halwa", "gulab jamun", "brownie"],
    "karahi":  ["karahi", "کڑاہی", "chicken karahi", "beef karahi", "mutton karahi"],
    "rice":    ["rice", "چاول", "pulao", "plov", "fried rice"],
    "rolls":   ["roll", "رول", "shawarma", "wrap", "paratha roll"],
}

INTENT_KEYWORDS = {
    "discount":  ["discount", "sale", "deal", "offer", "cheap", "سستا", "رعایت", "rabatt"],
    "order":     ["order", "آرڈر", "buy", "place order", "i want", "مجھے چاہیے", "bestellen",
                  "chahiye", "dena", "lena", "add", "mujhe"],
    "menu":      ["menu", "مینو", "menü", "what do you have", "show menu", "list", "items",
                  "all items", "show all"],
    "price":     ["price", "قیمت", "preis", "cost", "how much", "kitna", "rate",
                  "all prices", "all flavours", "all flavors", "price list", "rates"],
    "greeting":  ["hi", "hello", "hey", "assalam", "السلام", "hallo", "guten tag", "سلام",
                  "start", "begin", "aoa", "aslam"],
    "address":   ["address", "پتہ", "adresse", "location", "deliver to", "my address"],
    "status":    ["status", "where", "order status", "track", "delivered", "pending",
                  "where is my order", "track order"],
    "cancel":    ["cancel", "منسوخ", "stornieren", "nahi chahiye", "remove order"],
    "cart":      ["cart", "basket", "my order", "show cart", "view cart", "what did i order",
                  "my cart", "mera cart"],
    "confirm":   ["confirm", "yes", "okay", "ok", "haan", "ہاں", "proceed", "place", "done",
                  "confirm order", "place order"],
    "clear":     ["clear cart", "empty cart", "start over", "restart", "reset cart"],
    "new_order": ["new order", "naya order", "start new", "nayi order", "fresh order",
                  "order again", "reorder", "new aaorder", "new ordar", "new aorder"],
}

# ============================================================
# SESSION MANAGEMENT (v7.0)
# ============================================================

def _default_session() -> Dict[str, Any]:
    return {
        "lang":             "en",
        "shown":            [],
        "step":             0,
        "pending_order":    {},    # single-item flow state
        "cart":             [],    # multi-item cart (cleared after order confirmed)
        "missing_info_queue": [],
        "preferred_size":   None,
        "preferred_spice":  None,
        "frequent_items":   [],
        "last_address":     None,  # persisted address from last order
        "order_count":      0,     # how many orders placed on this number
    }


def get_user_session(user_id: str) -> Dict:
    if user_id not in USER_SESSIONS:
        USER_SESSIONS[user_id] = _default_session()
    return USER_SESSIONS[user_id]


def reset_cart_only(session: Dict):
    """
    Called after an order is placed (address provided).
    Clears cart + order flow state, preserves preferences & address.
    """
    session["step"]               = 0
    session["cart"]               = []
    session["pending_order"]      = {}
    session["missing_info_queue"] = []


def reset_for_new_order(session: Dict):
    """
    Called when user explicitly says 'new order'.
    Clears cart/flow but keeps address and preferences.
    """
    session["step"]               = 0
    session["cart"]               = []
    session["pending_order"]      = {}
    session["missing_info_queue"] = []
    # intentionally keep: last_address, preferred_size, preferred_spice, frequent_items


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
}

SIZE_HINTS = [
    "0.5kg", "1kg", "2kg", "0.25kg", "half kg", "half", "1 kg", "2 kg",
    "500ml", "1.5l", "1.5L", "1l", "small", "medium", "large", "regular", "xl",
]


def _extract_quantity(token: str) -> int:
    t = token.strip().lower()
    if t.isdigit():
        return int(t)
    return QUANTITY_WORDS.get(t, 1)


def _find_product_by_query(query: str) -> Optional[Dict]:
    """Fuzzy-score products and return the best match, or None."""
    q = query.lower().strip()
    best_score, best_product = 0, None

    for product in PRODUCTS_DATA:
        title    = product.get("title", "").lower()
        category = product.get("category", "").lower()
        score    = 0

        if q in title or title in q:
            score += 10
        q_words = set(re.findall(r"\w+", q))
        t_words = set(re.findall(r"\w+", title))
        score  += len(q_words & t_words) * 3

        for cat, kws in CATEGORY_KEYWORDS.items():
            if cat == category and any(kw in q for kw in kws):
                score += 5

        for word in t_words:
            if word in q and len(word) > 3:
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


def parse_multi_item_order(text: str) -> List[Dict]:
    """
    Parse free-text → list of order items.
    "1kg karahi and 2 burgers and 3 pepsi" → [{qty,size_hint,product},...]
    """
    parts   = re.split(r'\b(?:and|aur|or|also|\+|,|پھر|اور)\b', text, flags=re.IGNORECASE)
    results = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        qty = 1
        qty_match = re.match(
            r'^(\d+|' + '|'.join(QUANTITY_WORDS.keys()) + r')\s+',
            part, re.IGNORECASE
        )
        if qty_match:
            qty            = _extract_quantity(qty_match.group(1))
            part_no_qty    = part[qty_match.end():].strip()
        else:
            part_no_qty = part

        size_hint = ""
        for sh in sorted(SIZE_HINTS, key=len, reverse=True):
            if sh.lower() in part_no_qty.lower():
                size_hint  = sh
                part_no_qty = re.sub(re.escape(sh), "", part_no_qty, flags=re.IGNORECASE).strip()
                break

        size_match = re.match(
            r'^(\d+\.?\d*\s*kg|\d+\.?\d*\s*g\b|\d+\s*ml|\d+\.?\d*\s*l\b)',
            part_no_qty, re.IGNORECASE
        )
        if size_match and not size_hint:
            size_hint   = size_match.group(1).strip()
            part_no_qty = part_no_qty[size_match.end():].strip()

        product = _find_product_by_query(part_no_qty) or _find_product_by_query(part)
        if product:
            results.append({"raw": part, "qty": qty, "size_hint": size_hint, "product": product})

    return results

# ============================================================
# CART & ORDER BUILDING
# ============================================================

def build_cart_item(product: Dict, size: str, spice: str, extras: List[str], quantity: int) -> Dict:
    """Build a cart item with ALL prices from database — never hardcoded."""
    variants        = product.get("variants", [])
    matched_variant = _match_variant(variants, size) if size else (variants[0] if variants else None)

    if matched_variant:
        base_price = matched_variant["price"]
        final_size = matched_variant["size"]
    else:
        base_price = variants[0]["price"] if variants else float(str(product.get("price", 0)).replace(",", "") or 0)
        final_size = variants[0]["size"] if variants else size

    extras_options = product.get("extras", [])
    extras_price   = sum(e["price"] for e in extras_options if e["name"] in extras)

    return {
        "product_id":       str(product.get("_id", "")),
        "title":            product.get("title", "Item"),
        "category":         product.get("category", ""),
        "size":             final_size,
        "quantity":         quantity,
        "spice":            spice,
        "extras":           extras,
        "base_price":       base_price,
        "extras_price":     extras_price,
        "total_item_price": (base_price + extras_price) * quantity,
    }


def create_order_from_cart(user_id: str, cart_items: List[Dict], address: str) -> str:
    """
    Persist order to MongoDB. Increments session order_count.
    Returns order_id string or 'db_error'.
    """
    if orders_col is None:
        return "db_error"

    total = sum(item["total_item_price"] for item in cart_items)
    order = {
        "user_id":    user_id,
        "items":      cart_items,
        "dish":       cart_items[0]["title"] if cart_items else "Order",
        "quantity":   sum(i["quantity"] for i in cart_items),
        "total_price": total,
        "address":    address,
        "status":     "Pending",
        "timestamp":  datetime.utcnow().isoformat(),
        "customization": {
            "size":   cart_items[0].get("size", "") if cart_items else "",
            "spice":  cart_items[0].get("spice", "") if cart_items else "",
            "extras": ", ".join(cart_items[0].get("extras", [])) if cart_items else "",
        },
    }
    result = orders_col.insert_one(order)

    # Analytics
    inc = {"total_orders": 1}
    for item in cart_items:
        if item.get("size"):   inc[f"size_preference.{item['size']}"]   = inc.get(f"size_preference.{item['size']}", 0) + 1
        if item.get("spice"):  inc[f"spice_preference.{item['spice']}"] = inc.get(f"spice_preference.{item['spice']}", 0) + 1
        for extra in item.get("extras", []):
            inc[f"extras_preference.{extra}"] = inc.get(f"extras_preference.{extra}", 0) + 1
    _track(inc)

    # Update session order count
    session = get_user_session(user_id)
    session["order_count"] = session.get("order_count", 0) + 1

    return str(result.inserted_id)


def create_order_from_session(user_id: str, session: Dict, address: str) -> str:
    """Build a cart item from single-item session state and persist."""
    po    = session.get("pending_order", {})
    items = po.get("items", [])
    if not items:
        items = [{
            "product_id":       po.get("product_id", ""),
            "title":            po.get("dish", "Item"),
            "size":             po.get("size", ""),
            "spice":            po.get("spice", ""),
            "extras":           po.get("extras", []),
            "quantity":         po.get("qty", 1),
            "base_price":       po.get("price", 0),
            "extras_price":     0,
            "total_item_price": po.get("price", 0),
        }]
    return create_order_from_cart(user_id, items, address)

# ============================================================
# FAQ ENGINE
# ============================================================

def get_faq_response(query: str, lang: str) -> Optional[str]:
    faq = BOT_DATA.get("faq", {})
    q   = query.lower()
    mapping = {
        "delivery": ["deliver", "ship", "ارسال", "versand", "kab ayega", "delivery time"],
        "return":   ["return", "refund", "واپسی", "rückgabe", "exchange", "cancel"],
        "track":    ["track", "order status", "ٹریک", "verfolgen", "kahan hai"],
        "quality":  ["quality", "fresh", "معیار", "qualität", "ingredients"],
        "hours":    ["open", "close", "hours", "timing", "اوقات", "öffnungszeiten"],
        "payment":  ["pay", "payment", "cash", "card", "ادائیگی", "zahlung"],
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
    # Fallback: if the text is at least 10 chars, treat whole message as address
    text = text.strip()
    if len(text) >= 10:
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


async def send_whatsapp_list(to: str, header: str, items: List[Dict[str, Any]]):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        return
    rows = []
    for i, item in enumerate(items[:10]):
        variants  = item.get("variants", [])
        price_str = f"PKR {variants[0]['price']}" if variants else f"PKR {item.get('price', '')}"
        rows.append({
            "id":          f"item_{i}",
            "title":       item.get("title", "Item")[:24],
            "description": f"{item.get('description', '')[:50]} — {price_str}",
        })
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "interactive",
        "interactive": {
            "type":   "list",
            "header": {"type": "text", "text": header[:60]},
            "body":   {"text": "Tap an item to order or ask me anything! 🍽️"},
            "footer": {"text": "Powered by AI Restaurant Bot v7"},
            "action": {
                "button":   "View Menu",
                "sections": [{"title": "Our Menu", "rows": rows}],
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
    """Return True if the user is asking for full menu/price list."""
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
    """Return category name if user is asking for a specific category price list."""
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return cat
    return None

# ============================================================
# BOT FLOW HELPERS
# ============================================================

async def _ask_size(to: str, product: Dict, lang: str):
    variants  = product.get("variants", [])
    size_list = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants)
    msgs = {
        "en": f"📏 Choose a size for *{product['title']}*:\n{size_list}",
        "ur": f"📏 *{product['title']}* کا سائز چنیں:\n{size_list}",
        "de": f"📏 Größe wählen für *{product['title']}*:\n{size_list}",
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))


async def _ask_spice(to: str, product: Dict, lang: str) -> bool:
    spice_levels = product.get("spice_levels", [])
    if not spice_levels:
        return False
    options = " / ".join(spice_levels)
    msgs = {
        "en": f"🌶️ Choose spice level for *{product['title']}*:\n  {options}",
        "ur": f"🌶️ مسالے کی سطح چنیں *{product['title']}* کے لیے:\n  {options}",
        "de": f"🌶️ Schärfegrad wählen für *{product['title']}*:\n  {options}",
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _ask_extras(to: str, product: Dict, lang: str) -> bool:
    extras = product.get("extras", [])
    if not extras:
        return False
    extras_list = "\n".join(f"  • {e['name']} +PKR {e['price']}" for e in extras)
    msgs = {
        "en": f"➕ Add extras for *{product['title']}*? (type names or 'no')\n{extras_list}",
        "ur": f"➕ *{product['title']}* کے ساتھ کچھ اضافی؟ (نام لکھیں یا 'no')\n{extras_list}",
        "de": f"➕ Extras für *{product['title']}*? (Namen tippen oder 'nein')\n{extras_list}",
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _handle_single_item_order(from_number: str, text: str, lang: str) -> bool:
    """
    Initiate single-item order flow.
    Steps: size(1) → spice(2) → extras(3) → address(4)
    """
    session  = get_user_session(from_number)
    products = filter_products(text)
    if not products:
        return False

    p              = products[0]
    variants       = p.get("variants", [])
    spice_levels   = p.get("spice_levels", [])
    extras_options = p.get("extras", [])
    base_price     = variants[0]["price"] if variants else float(str(p.get("price", 0)).replace(",", "") or 0)

    session["pending_order"] = {
        "product_id":    p.get("_id", ""),
        "dish":          p.get("title", "Item"),
        "price":         base_price,
        "qty":           1,
        "variants":      variants,
        "spice_levels":  spice_levels,
        "extras_options": extras_options,
        "size":          "",
        "spice":         "",
        "extras":        [],
        "product_ref":   p,
    }

    if variants:
        session["step"] = 1
        await _ask_size(from_number, p, lang)
    else:
        session["step"] = 4
        msgs = {
            "en": f"🎉 Great choice!\n🍽️ *{p['title']}* — PKR {int(base_price)}\n\n📍 Share your delivery address:",
            "ur": f"🎉 بہترین انتخاب!\n🍽️ *{p['title']}* — PKR {int(base_price)}\n\n📍 ڈلیوری کا پتہ دیں:",
            "de": f"🎉 Tolle Wahl!\n🍽️ *{p['title']}* — PKR {int(base_price)}\n\n📍 Lieferadresse angeben:",
        }
        await send_whatsapp_text(from_number, msgs.get(lang, msgs["en"]))
    return True


async def _handle_full_price_display(from_number: str, q: str, lang: str):
    """
    Display ALL products with ALL size/variant prices.
    Handles: "all pizza prices", "all flavours", "price list", etc.
    """
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
        title = f"{cat_name} Menu & Prices"
    else:
        products = PRODUCTS_DATA[:15]
        emoji    = "🍽️"
        title    = "Full Menu & Prices"

    if not products:
        await send_whatsapp_text(from_number, "No products found at the moment. Please try again! 🙏")
        return

    menu_text = _build_full_price_menu(products, emoji, title)
    await send_whatsapp_text(from_number, menu_text)


async def handle_multi_item_order(from_number: str, text: str, lang: str) -> bool:
    """
    Parse and process a multi-item order.
    Handles partial-info resolution via missing_info_queue.
    """
    session      = get_user_session(from_number)
    parsed_items = parse_multi_item_order(text)

    if not parsed_items:
        return False

    cart_items   = list(session.get("cart", []))  # carry over existing cart items
    missing_info = []

    for parsed in parsed_items:
        product   = parsed["product"]
        qty       = parsed["qty"]
        size_hint = parsed["size_hint"]
        variants  = product.get("variants", [])

        matched_variant = None
        if size_hint:
            matched_variant = _match_variant(variants, size_hint)
        elif session.get("preferred_size") and variants:
            matched_variant = _match_variant(variants, session["preferred_size"])

        if variants and not matched_variant:
            missing_info.append({"type": "size", "product": product, "qty": qty})
            continue

        spice       = ""
        spice_levels = product.get("spice_levels", [])
        if spice_levels and session.get("preferred_spice") in spice_levels:
            spice = session["preferred_spice"]

        size      = matched_variant["size"] if matched_variant else ""
        cart_item = build_cart_item(product, size, spice, [], qty)
        cart_items.append(cart_item)

    if missing_info:
        session["cart"]               = cart_items
        session["missing_info_queue"] = missing_info
        session["step"]               = 10
        await _ask_size(from_number, missing_info[0]["product"], lang)
        return True

    if not cart_items:
        return False

    session["cart"] = cart_items
    total           = _recalc_cart(cart_items)
    summary         = _build_cart_summary(cart_items, total)

    confirm_msgs = {
        "en": f"{summary}\n\n👉 Confirm order or add more items?",
        "ur": f"{summary}\n\n👉 آرڈر تصدیق کریں یا مزید شامل کریں؟",
        "de": f"{summary}\n\n👉 Bestätigen oder mehr hinzufügen?",
    }
    await send_whatsapp_buttons(
        from_number,
        confirm_msgs.get(lang, confirm_msgs["en"]),
        ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"]
    )
    session["step"] = 5
    return True

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
    # ── FIX #3: Return 200 on bare health-check (no params) ──
    if hub_mode is None:
        return PlainTextResponse("Webhook active", status_code=200)
    # ─────────────────────────────────────────────────────────
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_message(request: Request):
    try:
        data    = await request.json()
        entry   = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return JSONResponse({"status": "ok"})

        msg      = messages[0]
        from_num = msg.get("from", "")
        msg_type = msg.get("type", "text")

        # ── FIX #2: Rate limit check ─────────────────────────
        if _is_rate_limited(from_num):
            logger.warning(f"Rate limited: {from_num}")
            return JSONResponse({"status": "rate_limited"})
        # ─────────────────────────────────────────────────────

        # Interactive button / list replies
        if msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                msg_text = interactive["button_reply"].get("title", "")
            elif interactive.get("type") == "list_reply":
                msg_text = interactive["list_reply"].get("title", "")
            else:
                msg_text = ""
        else:
            msg_text = msg.get("text", {}).get("body", "").strip()

        if not msg_text:
            return JSONResponse({"status": "ok"})

        lang    = detect_language(msg_text)
        session = get_user_session(from_num)
        session["lang"] = lang
        q    = msg_text.lower().strip()
        step = session.get("step", 0)

        _track({"total_searches": 1, f"supported_languages.{lang}": 1})

        # ═══════════════════════════════════════════════
        # PRIORITY 0 — "new order" → reset cart immediately
        # ═══════════════════════════════════════════════
        if any(kw in q for kw in INTENT_KEYWORDS["new_order"]):
            reset_for_new_order(session)
            last_addr = session.get("last_address")
            addr_hint = f"\n📍 Last address: _{last_addr}_\n(type 'same' to reuse)" if last_addr else ""
            new_order_msg = {
                "en": f"🆕 Starting a fresh order! Your previous order has been saved.\n{addr_hint}\n\nWhat would you like to order? 🍽️",
                "ur": f"🆕 نیا آرڈر شروع! پرانا آرڈر محفوظ ہوگیا۔\n{addr_hint}\n\nکیا آرڈر کرنا ہے؟ 🍽️",
                "de": f"🆕 Neue Bestellung gestartet! Die vorherige wurde gespeichert.\n{addr_hint}\n\nWas möchten Sie bestellen?",
            }
            await send_whatsapp_buttons(
                from_num,
                new_order_msg.get(lang, new_order_msg["en"]),
                ["View Menu 📋", "Order Again 🔄", "Contact Us 📞"]
            )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 1 — User picks SIZE (single item flow)
        # ═══════════════════════════════════════════════
        if step == 1:
            po       = session.get("pending_order", {})
            variants = po.get("variants", [])
            matched  = _match_variant(variants, q)
            if not matched and variants:
                sizes_str = " / ".join(v["size"] for v in variants)
                await send_whatsapp_text(from_num, f"⚠️ Please choose a valid size: *{sizes_str}*")
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
                    ask_addr = {
                        "en": "📍 Please share your full delivery address (house no., street, area, city):",
                        "ur": "📍 براہ کرم مکمل پتہ دیں (مکان نمبر، گلی، علاقہ، شہر):",
                        "de": "📍 Bitte vollständige Lieferadresse angeben:",
                    }
                    await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 2 — User picks SPICE
        # ═══════════════════════════════════════════════
        if step == 2:
            po           = session.get("pending_order", {})
            spice_levels = po.get("spice_levels", [])
            matched_spice = next((s for s in spice_levels if s.lower() in q), spice_levels[0] if spice_levels else "")
            po["spice"]  = matched_spice
            update_preferences(from_num, spice=matched_spice)
            _track({f"spice_preference.{matched_spice}": 1})

            product_ref = po.get("product_ref", {"title": po.get("dish", ""), "extras": po.get("extras_options", [])})
            has_extras  = await _ask_extras(from_num, product_ref, lang)
            session["step"] = 3 if has_extras else 4
            if not has_extras:
                ask_addr = {
                    "en": "📍 Please share your full delivery address:",
                    "ur": "📍 براہ کرم اپنا مکمل پتہ دیں:",
                    "de": "📍 Bitte deine Lieferadresse angeben:",
                }
                await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 3 — User picks EXTRAS
        # ═══════════════════════════════════════════════
        if step == 3:
            po             = session.get("pending_order", {})
            extras_options = po.get("extras_options", [])
            chosen         = []
            if not any(skip in q for skip in ["no", "skip", "nothing", "nahi", "nope", "nein"]):
                for e in extras_options:
                    if e["name"].lower() in q:
                        chosen.append(e["name"])
                        _track({f"extras_preference.{e['name']}": 1})

            po["extras"]        = chosen
            extras_price        = sum(e["price"] for e in extras_options if e["name"] in chosen)
            po["price"]         = po.get("price", 0) + extras_price
            session["step"]     = 4
            ask_addr = {
                "en": "📍 Please share your full delivery address (house no., street, area, city):",
                "ur": "📍 براہ کرم مکمل پتہ دیں (مکان نمبر، گلی، علاقہ، شہر):",
                "de": "📍 Bitte vollständige Lieferadresse angeben:",
            }
            await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 4 — User provides ADDRESS (single item)
        # ═══════════════════════════════════════════════
        if step == 4:
            po = session.get("pending_order", {})

            # Handle "same" address reuse
            if q.strip() in ["same", "same address", "same adress", "same add"]:
                address = session.get("last_address")
                if not address:
                    await send_whatsapp_text(from_num, "⚠️ No previous address found. Please type your full address.")
                    return JSONResponse({"status": "ok"})
            else:
                address = extract_address(msg_text) or msg_text.strip()

            order_id = create_order_from_session(from_num, session, address)

            # ✅ Save address + reset cart (keep preferences)
            session["last_address"] = address
            update_preferences(from_num, product_title=po.get("dish", ""))
            reset_cart_only(session)

            if order_id == "db_error":
                await send_whatsapp_text(from_num, "⚠️ Sorry, issue placing order. Please try again later.")
                return JSONResponse({"status": "ok"})

            extras_text = ", ".join(po.get("extras", [])) or "None"
            conf = {
                "en": (
                    f"✅ *Order Confirmed!*\n\n"
                    f"🍽️ *{po.get('dish', 'Item')}*\n"
                    f"📏 Size: {po.get('size', 'N/A')}\n"
                    f"🌶️ Spice: {po.get('spice', '') or 'Default'}\n"
                    f"➕ Extras: {extras_text}\n"
                    f"💰 Total: PKR {int(po.get('price', 0))}\n"
                    f"📍 Address: {address}\n"
                    f"🔖 Order ID: #{order_id[-6:]}\n\n"
                    f"⏱️ Estimated delivery: 35-45 mins\n"
                    f"📲 Type *new order* anytime to order again!"
                ),
                "ur": (
                    f"✅ *آرڈر تصدیق ہوگیا!*\n\n"
                    f"🍽️ *{po.get('dish', 'Item')}* | PKR {int(po.get('price', 0))}\n"
                    f"📍 پتہ: {address}\n"
                    f"🔖 آرڈر نمبر: #{order_id[-6:]}\n\n"
                    f"📲 نیا آرڈر دینے کے لیے *new order* لکھیں۔"
                ),
                "de": (
                    f"✅ *Bestellung bestätigt!*\n\n"
                    f"🍽️ *{po.get('dish', 'Item')}* | PKR {int(po.get('price', 0))}\n"
                    f"📍 Adresse: {address}\n"
                    f"🔖 Bestellnr: #{order_id[-6:]}\n\n"
                    f"📲 Tippen Sie *new order* für eine neue Bestellung."
                ),
            }
            await send_whatsapp_text(from_num, conf.get(lang, conf["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 5 — Cart CONFIRMATION (multi-item)
        # ═══════════════════════════════════════════════
        if step == 5:
            if any(kw in q for kw in ["confirm", "yes", "okay", "ok", "haan", "proceed", "place", "done", "✅"]):
                session["step"] = 6
                # Check for saved address
                last_addr = session.get("last_address")
                if last_addr:
                    addr_prompt = {
                        "en": f"📍 Deliver to your last address?\n_{last_addr}_\n\nType *same* to confirm or enter a new address:",
                        "ur": f"📍 پرانے پتے پر ڈلیوری کریں؟\n_{last_addr}_\n\nتصدیق کے لیے *same* لکھیں یا نیا پتہ دیں:",
                        "de": f"📍 An letzte Adresse liefern?\n_{last_addr}_\n\nGeben Sie *same* ein oder neue Adresse:",
                    }
                else:
                    addr_prompt = {
                        "en": "📍 Please share your full delivery address (house no., street, area, city):",
                        "ur": "📍 براہ کرم مکمل پتہ دیں (مکان نمبر، گلی، علاقہ، شہر):",
                        "de": "📍 Bitte vollständige Lieferadresse angeben:",
                    }
                await send_whatsapp_text(from_num, addr_prompt.get(lang, addr_prompt["en"]))

            elif any(kw in q for kw in ["add more", "more", "aur", "add", "➕"]):
                session["step"] = 0
                await send_whatsapp_text(from_num, "Sure! What else would you like to add? 🍽️")

            elif any(kw in q for kw in ["clear", "reset", "cancel", "empty", "🗑️"]):
                session["cart"] = []
                session["step"] = 0
                await send_whatsapp_text(from_num, "🗑️ Cart cleared! What would you like to order?")

            else:
                cart  = session.get("cart", [])
                total = _recalc_cart(cart)
                summary = _build_cart_summary(cart, total)
                await send_whatsapp_buttons(
                    from_num,
                    f"{summary}\n\n👉 Confirm or add more?",
                    ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"]
                )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 6 — ADDRESS for cart order
        # ═══════════════════════════════════════════════
        if step == 6:
            cart_items = session.get("cart", [])
            if not cart_items:
                session["step"] = 0
                await send_whatsapp_text(from_num, "🛒 Your cart is empty. What would you like to order?")
                return JSONResponse({"status": "ok"})

            # Handle "same" address reuse
            if q.strip() in ["same", "same address", "same adress", "same add"]:
                address = session.get("last_address")
                if not address:
                    await send_whatsapp_text(from_num, "⚠️ No previous address found. Please type your full address.")
                    return JSONResponse({"status": "ok"})
            else:
                address = extract_address(msg_text) or msg_text.strip()

            order_id = create_order_from_cart(from_num, cart_items, address)
            total    = _recalc_cart(cart_items)
            summary  = _build_cart_summary(cart_items, total)

            # ✅ Save address + reset cart (keep preferences)
            session["last_address"] = address
            reset_cart_only(session)

            if order_id == "db_error":
                await send_whatsapp_text(from_num, "⚠️ Sorry, issue placing order. Please try again.")
                return JSONResponse({"status": "ok"})

            conf = {
                "en": (
                    f"✅ *Order Confirmed!*\n\n"
                    f"{summary}\n\n"
                    f"📍 Address: {address}\n"
                    f"🔖 Order ID: #{order_id[-6:]}\n"
                    f"⏱️ Estimated delivery: 35-45 mins\n\n"
                    f"📲 Type *new order* anytime to order again!"
                ),
                "ur": (
                    f"✅ *آرڈر تصدیق ہوگیا!*\n\n"
                    f"💰 کل: PKR {int(total)}\n"
                    f"📍 پتہ: {address}\n"
                    f"🔖 نمبر: #{order_id[-6:]}\n\n"
                    f"📲 نیا آرڈر دینے کے لیے *new order* لکھیں۔"
                ),
                "de": (
                    f"✅ *Bestellung bestätigt!*\n\n"
                    f"💰 Gesamt: PKR {int(total)}\n"
                    f"📍 Adresse: {address}\n"
                    f"🔖 Nr: #{order_id[-6:]}\n\n"
                    f"📲 Tippen Sie *new order* für eine neue Bestellung."
                ),
            }
            await send_whatsapp_text(from_num, conf.get(lang, conf["en"]))
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 10 — Multi-item MISSING INFO QUEUE
        # ═══════════════════════════════════════════════
        if step == 10:
            missing_queue = session.get("missing_info_queue", [])
            if missing_queue:
                first    = missing_queue[0]
                product  = first["product"]
                qty      = first["qty"]
                variants = product.get("variants", [])
                matched  = _match_variant(variants, q)

                if not matched and variants:
                    sizes_str = " / ".join(v["size"] for v in variants)
                    await send_whatsapp_text(from_num, f"⚠️ Please choose a valid size: *{sizes_str}*")
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
                    session["step"]               = 5
                    cart    = session.get("cart", [])
                    total   = _recalc_cart(cart)
                    summary = _build_cart_summary(cart, total)
                    await send_whatsapp_buttons(
                        from_num,
                        f"{summary}\n\n👉 Confirm order or add more?",
                        ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"]
                    )
            return JSONResponse({"status": "ok"})

        # ═══════════════════════════════════════════════
        # STEP 0 — Normal intent routing
        # ═══════════════════════════════════════════════

        # ── Greeting ──────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["greeting"]):
            greeting = BOT_DATA.get("initial_message", {}).get(lang, "Welcome! 🍽️ How can I help you?")
            sugs     = get_suggestions(from_num, lang)
            reply    = greeting
            if sugs:
                reply += "\n\n💡 " + "\n• ".join(sugs)
            await send_whatsapp_buttons(from_num, reply, ["View Menu 📋", "Place Order 🛒", "Contact Us 📞"])
            return JSONResponse({"status": "ok"})

        # ── Show cart ─────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["cart"]):
            cart = session.get("cart", [])
            if cart:
                total   = _recalc_cart(cart)
                summary = _build_cart_summary(cart, total)
                await send_whatsapp_buttons(from_num, summary, ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"])
                session["step"] = 5
            else:
                await send_whatsapp_text(from_num, "🛒 Your cart is empty. What would you like to order?")
            return JSONResponse({"status": "ok"})

        # ── Clear cart ────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["clear"]):
            session["cart"] = []
            session["step"] = 0
            await send_whatsapp_text(from_num, "🗑️ Cart cleared! What would you like to order?")
            return JSONResponse({"status": "ok"})

        # ── Confirm order (catch-all for step=5) ─────
        if any(kw in q for kw in INTENT_KEYWORDS["confirm"]) and session.get("cart"):
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
                    "en": "📍 Please share your full delivery address:",
                    "ur": "📍 براہ کرم اپنا مکمل پتہ دیں:",
                    "de": "📍 Bitte deine Lieferadresse angeben:",
                }
            await send_whatsapp_text(from_num, addr_prompt.get(lang, addr_prompt["en"]))
            return JSONResponse({"status": "ok"})

        # ── MIXED INTENT: order + price display ───────
        # e.g. "I want tikka pizza and show all pizza prices"
        order_intent  = any(kw in q for kw in INTENT_KEYWORDS["order"])
        price_intent  = _detect_price_menu_intent(q)
        menu_intent   = any(kw in q for kw in INTENT_KEYWORDS["menu"])
        multi_signals = ["and", "aur", "+", "also", "ke saath", "اور"]
        is_multi      = any(s in q for s in multi_signals)

        # Handle price display (possibly combined with order)
        if price_intent:
            _track({"total_searches": 1})
            await _handle_full_price_display(from_num, q, lang)
            # If there's also an order intent in the same message, continue below
            if not order_intent:
                return JSONResponse({"status": "ok"})

        # ── Order intent ──────────────────────────────
        if order_intent or is_multi or re.search(r'\d+\s*(?:kg|ml|l\b|g\b)', q):
            _track({"total_cart_additions": 1})
            if is_multi or re.search(r'\d+\s*(?:kg|ml|l\b|g\b)', q):
                handled = await handle_multi_item_order(from_num, msg_text, lang)
                if handled:
                    return JSONResponse({"status": "ok"})
            handled = await _handle_single_item_order(from_num, msg_text, lang)
            if handled:
                return JSONResponse({"status": "ok"})

        # ── Menu display ──────────────────────────────
        if menu_intent:
            _track({"total_searches": 1})
            products = filter_products(msg_text) or PRODUCTS_DATA[:8]
            if products:
                header = {"en": "🍽️ Our Menu", "ur": "🍽️ ہمارا مینو", "de": "🍽️ Unsere Speisekarte"}.get(lang, "🍽️ Our Menu")
                await send_whatsapp_list(from_num, header, products)
            else:
                await send_whatsapp_text(from_num, "Menu not available right now. Try again soon! 🙏")
            return JSONResponse({"status": "ok"})

        # ── FAQ ───────────────────────────────────────
        faq_resp = get_faq_response(msg_text, lang)
        if faq_resp:
            await send_whatsapp_text(from_num, faq_resp)
            return JSONResponse({"status": "ok"})

        # ── Discount ──────────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["discount"]):
            disc = BOT_DATA.get("discount_message", {}).get(lang, BOT_DATA.get("discount_message", {}).get("en"))
            if disc:
                await send_whatsapp_text(from_num, disc)
                return JSONResponse({"status": "ok"})

        # ── Order status ──────────────────────────────
        if any(kw in q for kw in INTENT_KEYWORDS["status"]) and orders_col:
            latest = orders_col.find_one({"user_id": from_num}, sort=[("timestamp", DESCENDING)])
            if latest:
                dish_name     = latest.get("dish") or (latest.get("items", [{}])[0].get("title", "Order"))
                status        = latest.get("status", "Pending")
                status_emoji  = {"Pending": "⏳", "Accepted": "✅", "Processing": "👨‍🍳", "Delivered": "🚗", "Rejected": "❌"}.get(status, "📦")
                st = {
                    "en": f"{status_emoji} Latest order (*{dish_name}*): *{status}*\n🔖 ID: #{str(latest.get('_id', ''))[-6:]}",
                    "ur": f"{status_emoji} آپ کے آخری آرڈر کی حالت: *{status}*",
                    "de": f"{status_emoji} Letzter Auftragsstatus: *{status}*",
                }
                await send_whatsapp_text(from_num, st.get(lang, st["en"]))
            else:
                no_order = {
                    "en": "No orders found. Place your first order! 🍽️",
                    "ur": "کوئی آرڈر نہیں ملا۔",
                    "de": "Keine Bestellungen gefunden.",
                }
                await send_whatsapp_text(from_num, no_order.get(lang, no_order["en"]))
            return JSONResponse({"status": "ok"})

        # ── Product name search (fallback) ────────────
        matched_product = _find_product_by_query(msg_text)
        if matched_product:
            variants = matched_product.get("variants", [])
            if variants:
                size_list = "\n".join(f"  • {v['size']} — PKR {v['price']}" for v in variants)
                reply = {
                    "en": f"🍽️ *{matched_product['title']}*\n\nAvailable sizes:\n{size_list}\n\nWould you like to order?",
                    "ur": f"🍽️ *{matched_product['title']}*\n\nدستیاب سائز:\n{size_list}\n\nکیا آرڈر کرنا ہے؟",
                    "de": f"🍽️ *{matched_product['title']}*\n\nVerfügbare Größen:\n{size_list}\n\nMöchten Sie bestellen?",
                }
                await send_whatsapp_buttons(from_num, reply.get(lang, reply["en"]), ["✅ Order Now", "📋 View Menu"])
            else:
                price_str = f"PKR {matched_product.get('price', 'N/A')}"
                await send_whatsapp_buttons(
                    from_num,
                    f"🍽️ *{matched_product['title']}* — {price_str}\n\nWould you like to order?",
                    ["✅ Order Now", "📋 View Menu"]
                )
            return JSONResponse({"status": "ok"})

        # ── Generic fallback ──────────────────────────
        fallback = {
            "en": (
                "I couldn't understand that 🤔 Try:\n"
                "• *Show menu* — see all items\n"
                "• *1kg chicken karahi* — place an order\n"
                "• *2 burgers + 3 pepsi* — multi-item order\n"
                "• *All pizza prices* — full price list\n"
                "• *Order status* — track your order\n"
                "• *New order* — start fresh"
            ),
            "ur": (
                "سمجھ نہیں آیا 🤔 کوشش کریں:\n"
                "• *مینو دکھائیں* — سب آئٹم\n"
                "• *1 کلو کڑاہی* — آرڈر\n"
                "• *آرڈر اسٹیٹس* — ٹریکنگ\n"
                "• *new order* — نیا آرڈر"
            ),
            "de": (
                "Nicht verstanden 🤔 Versuchen:\n"
                "• *Menü anzeigen*\n"
                "• *1kg Karahi bestellen*\n"
                "• *Bestellstatus*\n"
                "• *new order*"
            ),
        }
        await send_whatsapp_text(from_num, fallback.get(lang, fallback["en"]))
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
    size       = body.get("size", "")
    spice      = body.get("spice", "")
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
    size       = body.get("size", "")

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
    size       = body.get("size", "")
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
            status_msg = {
                "Pending":    f"⏳ Your order *{dish_name}* is pending.",
                "Accepted":   f"✅ Great news! *{dish_name}* order accepted. Preparing now!",
                "Processing": f"👨‍🍳 *{dish_name}* is being prepared!",
                "Delivered":  f"🚗 *{dish_name}* is on its way!",
                "Rejected":   f"❌ Sorry, *{dish_name}* order was rejected. Please contact support.",
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
    _track({f"supported_languages.{language.lower()}": 1})
    return {"status": "tracked", "language": language}


@app.post("/track_size")
async def track_size(size: str = Form(...)):
    _track({f"size_preference.{size}": 1})
    return {"status": "tracked"}


@app.post("/track_spice")
async def track_spice(spice: str = Form(...)):
    _track({f"spice_preference.{spice}": 1})
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
                "faq":                BOT_DATA.get("faq", {}),
                "initial_message":    BOT_DATA.get("initial_message", {}),
                "discount_message":   BOT_DATA.get("discount_message", {}),
                "supported_languages": BOT_DATA.get("supported_languages", ["en", "ur", "de"]),
                "smart_suggestions":  BOT_DATA.get("smart_suggestions", {}),
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
    logger.info("🚀 Restaurant Bot v7.0 started!")
    logger.info(f"   Products loaded   : {len(PRODUCTS_DATA)}")
    logger.info(f"   WhatsApp connected: {'✅' if WHATSAPP_TOKEN else '❌'}")
    logger.info(f"   MongoDB connected : {'✅' if products_col is not None else '❌'}")
