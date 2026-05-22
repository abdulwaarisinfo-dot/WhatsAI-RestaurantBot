"""
sessions.py — Session management, language detection, address helpers,
              rate limiting, FAQ engine, smart suggestions
"""

import re
import time
import random
import logging
from typing import Dict, List, Any, Optional

from langdetect import detect

import config

logger = logging.getLogger("RestaurantBot.v14.6")

# ============================================================
# RATE LIMITING
# ============================================================

def _is_rate_limited(user_id: str) -> bool:
    now = time.time()
    timestamps = [t for t in config._rate_store[user_id] if now - t < 60]
    config._rate_store[user_id] = timestamps
    if len(timestamps) >= config.RATE_LIMIT_PER_MINUTE:
        return True
    config._rate_store[user_id].append(now)
    return False


# ============================================================
# LANGUAGE DETECTION
# ============================================================

def detect_language(text: str, session_lang: str = "en") -> str:
    if not text or not text.strip():
        return session_lang
    stripped = text.strip()
    if any("\u0600" <= c <= "\u06FF" for c in stripped):
        return "ur"
    if len(stripped) <= 25 and stripped.lower() in config._BUTTON_TEXTS:
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
    if user_id not in config.USER_SESSIONS:
        config.USER_SESSIONS[user_id] = _default_session()
    session = config.USER_SESSIONS[user_id]
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
# ADDRESS HELPERS
# ============================================================

_ADDRESS_KEYWORDS = re.compile(
    r'\b(street|st|road|rd|avenue|ave|lane|block|sector|phase|house|flat|floor|'
    r'building|near|opposite|plot|no\.?|number|h\.?no|area|town|city|karachi|'
    r'lahore|islamabad|gulshan|clifton|defence|defense|dha|gulberg|johar|nazimabad|'
    r'گلی|سڑک|گھر|مکان|بلاک|فیز|شہر|پتہ)\b',
    re.IGNORECASE,
)

# v14.6 FIX 2: food/order words that disqualify a string from being an address
_ADDRESS_DISQUALIFY_WORDS = re.compile(
    r'\b(pizza|burger|biryani|karahi|order|add|plate|spicy|mild|medium|confirm|'
    r'confirming|before|want|please|send|give|chicken|beef|mutton|fish|rice|'
    r'naan|roti|drink|dessert|cake|shawarma|wrap|pasta|steak|sandwich|soup|'
    r'salad|bread|roll|tikka|seekh|bbq|zinger|smash|crispy|family\s*pack)\b',
    re.IGNORECASE,
)

_SHORT_WORDS = {
    "yes", "no", "ok", "okay", "sure", "fine", "yep", "yeah",
    "haan", "nahi", "done", "same", "correct", "right",
}

# v14.6 FIX 3: Broader "same address" pattern recognition
_SAME_ADDRESS_PATTERN = re.compile(
    r'\b(same|same\s+address|same\s+adress|same\s+add|same\s+wala|same\s+hi|'
    r'use\s+previous|used\s+previous|previous\s+address|purana\s+address|'
    r'pehla\s+address|pehle\s+wala\s+address|wahi\s+address|same\s+location|'
    r'deliver\s+to\s+same|same\s+as\s+before|same\s+as\s+last\s+time)\b',
    re.IGNORECASE,
)

ADDRESS_PATTERN = re.compile(
    r"(\d+.{3,}(?:street|st|road|rd|avenue|ave|lane|block|sector|phase|house|flat|floor|"
    r"building|near|opposite|گلی|سڑک|گھر|مکان|بلاک|فیز).{5,})",
    re.IGNORECASE,
)


def _is_same_address_request(text: str) -> bool:
    """
    v14.6 FIX 3: Returns True when the user is asking to reuse a previous address.
    """
    return bool(_SAME_ADDRESS_PATTERN.search(text.strip()))


def _is_valid_address(text: str) -> bool:
    """
    v14.6 FIX 2: Rejects food/order sentences that accidentally pass the
    length heuristic.
    """
    stripped = text.strip()
    if stripped.lower() in _SHORT_WORDS:
        return False
    if len(stripped) < 8:
        return False
    if _ADDRESS_DISQUALIFY_WORDS.search(stripped):
        if not _ADDRESS_KEYWORDS.search(stripped):
            return False
    if _ADDRESS_KEYWORDS.search(stripped):
        return True
    if len(stripped) >= 12 and (any(c.isdigit() for c in stripped) or ',' in stripped):
        return True
    return len(stripped) >= 18


def extract_address(text: str) -> Optional[str]:
    m = ADDRESS_PATTERN.search(text)
    if m:
        return m.group(0).strip()
    text = text.strip()
    if len(text) >= 15:
        return text
    return None


# ============================================================
# FAQ ENGINE
# ============================================================

def get_faq_response(query: str, lang: str) -> Optional[str]:
    faq = config.BOT_DATA.get("faq", {})
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
    sugs    = config.BOT_DATA.get("smart_suggestions", {}).get("greeting", {}).get(lang, [])
    shown   = session.get("shown", [])
    avail   = [s for s in sugs if s not in shown] or sugs
    sel     = random.sample(avail, min(4, len(avail)))
    session["shown"] = list(set(shown + sel))
    return sel


# ============================================================
# INTENT DETECTION HELPERS
# ============================================================

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
    from products import _is_product_query
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
    from products import _is_product_query
    if len(q_stripped) <= 15 and not _is_product_query(q_stripped):
        for word in small_talk_words:
            if word in q_stripped:
                return True
    return False
