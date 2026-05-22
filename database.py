"""
database.py — MongoDB connection, data loading, and analytics helpers
"""

import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

import certifi
from bson import ObjectId
from pymongo import MongoClient, DESCENDING

import config

logger = logging.getLogger("RestaurantBot.v14.6")

# ============================================================
# DATABASE CONNECTION
# ============================================================

try:
    client = MongoClient(
        config.MONGO_URI,
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


# ============================================================
# DATA LOADER
# ============================================================

def _build_product_keyword_index():
    config.PRODUCT_KEYWORD_INDEX = {}

    def _add(key: str, product: Dict):
        key = key.lower().strip()
        if not key or len(key) < 2:
            return
        if key not in config.PRODUCT_KEYWORD_INDEX:
            config.PRODUCT_KEYWORD_INDEX[key] = []
        pid = str(product.get("_id", id(product)))
        if not any(str(p.get("_id", id(p))) == pid for p in config.PRODUCT_KEYWORD_INDEX[key]):
            config.PRODUCT_KEYWORD_INDEX[key].append(product)

    for product in config.PRODUCTS_DATA:
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
        for cat_key, aliases in config._UNIVERSAL_CATEGORY_ALIASES.items():
            if cat_key == category or cat_key in title.lower() or cat_key in desc.lower():
                for alias in aliases:
                    _add(alias, product)
                    for tok in re.findall(r'\w+', alias.lower()):
                        if len(tok) > 2:
                            _add(tok, product)
        for word in re.findall(r'\w+', desc.lower()):
            if len(word) > 3:
                _add(word, product)

    logger.info(f"Product keyword index built: {len(config.PRODUCT_KEYWORD_INDEX)} keys "
                f"across {len(config.PRODUCTS_DATA)} products")


def load_data_realtime():
    if products_col is None or meta_col is None:
        return
    try:
        config.PRODUCTS_DATA = [_str_id(p) for p in products_col.find({})]
        _build_product_keyword_index()

        merged: Dict[str, Any] = {}
        for doc in meta_col.find({}):
            _str_id(doc)
            merged.update({k: v for k, v in doc.items() if k != "_id"})

        config.BOT_DATA = {
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
        config.BOT_DATA.update(merged)

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
                    config.BOT_DATA[k] = config_doc[k]

        dc = config.BOT_DATA.get("delivery_charges", {})
        if not isinstance(dc, dict):
            dc = {}
        config.BOT_DATA["delivery_charges"] = {
            "flat_charge":   float(dc.get("flat_charge", 0) or 0),
            "free_above":    float(dc.get("free_above", 0) or 0),
            "per_area":      dc.get("per_area", {}) if isinstance(dc.get("per_area"), dict) else {},
            "free_keywords": dc.get("free_keywords", []) if isinstance(dc.get("free_keywords"), list) else [],
        }

        logger.info(
            f"Data synced | Products: {len(config.PRODUCTS_DATA)} | "
            f"FAQ keys: {list(config.BOT_DATA.get('faq', {}).keys())} | "
            f"Delivery time: {config.BOT_DATA['delivery_time']} | "
            f"Delivery charges: {config.BOT_DATA['delivery_charges']}"
        )
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
# DELIVERY TIME
# ============================================================

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

    exceptions  = config.BOT_DATA.get("delivery_time_exceptions", {})
    default_raw = config.BOT_DATA.get("delivery_time", "35-45 mins")
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
    dc            = config.BOT_DATA.get("delivery_charges", {})
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


# ============================================================
# ORDER CREATION
# ============================================================

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

    from sessions import get_user_session
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
