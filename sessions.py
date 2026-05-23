"""
sessions.py — Session management, language detection, address helpers,
              rate limiting, FAQ engine, smart suggestions,
              and Table Reservation flow helpers
WhatsApp AI Restaurant Bot v14.7 + Table Reservations
"""

import re
import time
import random
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta

from langdetect import detect

import config

logger = logging.getLogger("RestaurantBot.v14.7")

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
        # ── reservation state ────────────────────────────────
        "pending_reservation": {},
        "reservation_count":   0,
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


def reset_reservation_flow(session: Dict):
    """Clear pending reservation data and return to step 0."""
    session["pending_reservation"] = {}
    session["step"]                = 0


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
    return bool(_SAME_ADDRESS_PATTERN.search(text.strip()))


def _is_valid_address(text: str) -> bool:
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
# RESERVATION DATE / TIME HELPERS
# ============================================================

_DATE_PATTERNS = [
    # "25 may", "25 may 2026", "may 25", "may 25 2026"
    re.compile(
        r'\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        r'(?:\s+(\d{4}))?\b',
        re.IGNORECASE,
    ),
    re.compile(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        r'\s+(\d{1,2})(?:\s+(\d{4}))?\b',
        re.IGNORECASE,
    ),
    # "25/05/2026", "25-05-2026", "2026-05-25"
    re.compile(r'\b(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})\b'),
    re.compile(r'\b(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})\b'),
]

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

_TIME_PATTERN = re.compile(
    r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b',
    re.IGNORECASE,
)


def parse_reservation_date(text: str) -> Optional[str]:
    """
    Try to extract a date from free text and return it as 'YYYY-MM-DD'.
    Also handles 'today', 'tomorrow', 'day after tomorrow'.
    Returns None if nothing found.
    """
    t = text.lower().strip()
    now = datetime.utcnow()

    if "day after tomorrow" in t:
        d = now + timedelta(days=2)
        return d.strftime("%Y-%m-%d")
    if "tomorrow" in t:
        d = now + timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    if "today" in t:
        return now.strftime("%Y-%m-%d")

    # "DD month [YYYY]"
    m = _DATE_PATTERNS[0].search(text)
    if m:
        day   = int(m.group(1))
        month = _MONTH_MAP.get(m.group(2).lower()[:3], 0)
        year  = int(m.group(3)) if m.group(3) else now.year
        if 1 <= day <= 31 and month:
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # "month DD [YYYY]"
    m = _DATE_PATTERNS[1].search(text)
    if m:
        month = _MONTH_MAP.get(m.group(1).lower()[:3], 0)
        day   = int(m.group(2))
        year  = int(m.group(3)) if m.group(3) else now.year
        if 1 <= day <= 31 and month:
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # DD/MM/YYYY or DD-MM-YYYY
    m = _DATE_PATTERNS[2].search(text)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = c if c > 99 else 2000 + c
        try:
            return datetime(year, b, a).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # YYYY-MM-DD
    m = _DATE_PATTERNS[3].search(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def parse_reservation_time(text: str) -> Optional[str]:
    """
    Extract a time from free text and round to the nearest valid slot.
    Returns 'HH:MM' string or None.
    """
    m = _TIME_PATTERN.search(text)
    if not m:
        return None

    hour   = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm   = (m.group(3) or "").lower()

    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    # Round to nearest :00 or :30
    if minute < 15:
        minute = 0
    elif minute < 45:
        minute = 30
    else:
        hour  += 1
        minute = 0

    candidate = f"{hour:02d}:{minute:02d}"

    # Snap to nearest valid slot
    if candidate in config.RESERVATION_TIME_SLOTS:
        return candidate

    # Find closest slot
    def _slot_minutes(s: str) -> int:
        h, m_ = map(int, s.split(":"))
        return h * 60 + m_

    cand_mins = hour * 60 + minute
    closest   = min(config.RESERVATION_TIME_SLOTS, key=lambda s: abs(_slot_minutes(s) - cand_mins))
    return closest


def parse_guest_count(text: str) -> Optional[int]:
    """Extract a positive integer (guest count) from text."""
    # word-number mapping
    word_nums = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "ek": 1, "do": 2, "teen": 3, "char": 4, "panch": 5,
    }
    t = text.lower().strip()
    for w, n in word_nums.items():
        if re.search(r'\b' + w + r'\b', t):
            return n
    nums = re.findall(r'\b(\d+)\b', text)
    if nums:
        v = int(nums[0])
        if 1 <= v <= config.RESERVATION_MAX_GUESTS:
            return v
    return None


def is_reservation_date_valid(date_str: str) -> bool:
    """Return True if the date is today or in the future (not past)."""
    try:
        dt  = datetime.strptime(date_str, "%Y-%m-%d")
        now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return dt >= now
    except ValueError:
        return False


def format_reservation_summary(res: Dict, lang: str = "en") -> str:
    """Build a human-readable reservation summary block."""
    rid    = str(res.get("_id", ""))[-6:]
    name   = res.get("name", "—")
    date   = res.get("date", "—")
    time_  = res.get("time_slot", "—")
    guests = res.get("guests", "—")
    notes  = res.get("notes", "") or "—"
    status = res.get("status", "Pending")

    status_emoji = {
        "Pending":   "⏳",
        "Confirmed": "✅",
        "Cancelled": "❌",
        "Completed": "🎉",
        "No Show":   "🚫",
    }.get(status, "📋")

    if lang == "ur":
        return (
            f"🪑 *ریزرویشن کی تفصیل*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 نام: *{name}*\n"
            f"📅 تاریخ: *{date}*\n"
            f"🕐 وقت: *{time_}*\n"
            f"👥 مہمان: *{guests}*\n"
            f"📝 نوٹ: _{notes}_\n"
            f"{status_emoji} حالت: *{status}*\n"
            f"🔖 نمبر: *#{rid}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    elif lang == "de":
        return (
            f"🪑 *Reservierungsdetails*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Name: *{name}*\n"
            f"📅 Datum: *{date}*\n"
            f"🕐 Uhrzeit: *{time_}*\n"
            f"👥 Gäste: *{guests}*\n"
            f"📝 Notiz: _{notes}_\n"
            f"{status_emoji} Status: *{status}*\n"
            f"🔖 Ref: *#{rid}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        return (
            f"🪑 *Reservation Details*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Name: *{name}*\n"
            f"📅 Date: *{date}*\n"
            f"🕐 Time: *{time_}*\n"
            f"👥 Guests: *{guests}*\n"
            f"📝 Notes: _{notes}_\n"
            f"{status_emoji} Status: *{status}*\n"
            f"🔖 Ref: *#{rid}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )


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


def _is_reservation_intent(q: str) -> bool:
    """Return True when the message is clearly about booking a table."""
    return any(kw in q for kw in config.INTENT_KEYWORDS["reservation"])


def _is_my_reservations_intent(q: str) -> bool:
    """Return True when the user wants to view / cancel their reservations."""
    return any(kw in q for kw in config.INTENT_KEYWORDS["my_reservations"])


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
