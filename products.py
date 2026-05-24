"""
products.py — Product helpers, cart building, menu display, multi-item parsing
WhatsApp AI Restaurant Bot v14.7 + Table Reservations
"""

import re
import logging
from typing import Dict, List, Any, Optional
from difflib import SequenceMatcher

import config

logger = logging.getLogger("RestaurantBot.v14.7")

# ============================================================
# SIZE / VARIANT HELPERS
# ============================================================

_ORDER_NOISE_PREFIXES = re.compile(
    r'^(i\s+want\s+to\s+order|i\s+want\s+to|i\s+want|want\s+to\s+order|'
    r'please\s+give\s+me|please|kindly|mujhe\s+chahiye|mujhe|chahiye|'
    r'dena|lena|please\s+give|give\s+me|add\s+(?=\d)|add\s+(?=[a-zA-Z])|'
    r'can\s+i\s+get|get\s+me|send\s+me|i\'ll\s+have|i\s+would\s+like|i\'d\s+like|'
    r'bhai\s+dena|yaar\s+dena|bhai|yaar|lao|la\s+do|mangwao|order\s+karo|'
    r'mujhe\s+ek|mujhe\s+do|ek|do|teen)\s+',
    re.IGNORECASE,
)


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


def _extract_quantity(token: str) -> int:
    t       = token.strip().lower()
    t_clean = re.sub(r'(st|nd|rd|th)$', '', t)
    if t_clean.isdigit():
        val = int(t_clean)
        if re.search(r'(st|nd|rd|th)$', t):
            return 1
        return val
    return config.QUANTITY_WORDS.get(t, config.QUANTITY_WORDS.get(t_clean, 1))


def _extract_qty_from_size_response(msg_text: str) -> int:
    """
    v14.6 FIX 1: Extract quantity when user replies to a size prompt with
    something like '5 large', '2 xl', 'large x3', etc.
    """
    q = msg_text.lower().strip()

    qty_pattern = re.compile(
        r'^(\d+(?:st|nd|rd|th)?|' +
        '|'.join(re.escape(k) for k in sorted(config.QUANTITY_WORDS.keys(), key=len, reverse=True)) +
        r')\s+',
        re.IGNORECASE,
    )
    m = qty_pattern.match(q)
    if m:
        return _extract_quantity(m.group(1))

    trailing = re.search(r'[x×]\s*(\d+)\s*$', q)
    if trailing:
        return int(trailing.group(1))

    return 1


# ============================================================
# PRODUCT SEARCH
# ============================================================

def _is_product_query(q: str) -> bool:
    q_clean = _ORDER_NOISE_PREFIXES.sub("", q.lower().strip()).strip()
    words   = re.findall(r'\w+', q_clean)

    for word in words:
        if len(word) > 2 and word in config.PRODUCT_KEYWORD_INDEX:
            return True

    if q_clean in config.PRODUCT_KEYWORD_INDEX:
        return True

    for kws in config.CATEGORY_KEYWORDS.values():
        if any(kw in q_clean for kw in kws):
            return True

    return False


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
        if lookup in config.PRODUCT_KEYWORD_INDEX:
            for p in config.PRODUCT_KEYWORD_INDEX[lookup]:
                _add_candidate(p)

    words = re.findall(r'\w+', q_clean)
    for word in words:
        if len(word) > 2 and word in config.PRODUCT_KEYWORD_INDEX:
            for p in config.PRODUCT_KEYWORD_INDEX[word]:
                _add_candidate(p)

    if not candidates:
        for p in config.PRODUCTS_DATA:
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

        for cat, kws in config.CATEGORY_KEYWORDS.items():
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


def _products_by_category(category_key: str) -> List[Dict]:
    return [p for p in config.PRODUCTS_DATA if p.get("category", "").lower() == category_key.lower()]


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
    for cat, kws in config.CATEGORY_KEYWORDS.items():
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
    scored      = [{"p": p, "s": score_product(query, p, price_range)} for p in config.PRODUCTS_DATA]
    return [x["p"] for x in sorted(scored, key=lambda x: x["s"], reverse=True) if x["s"] > 0.0][:8]


# ============================================================
# CART HELPERS
# ============================================================

def _recalc_cart(cart_items: List[Dict]) -> float:
    return sum(
        (item.get("base_price", 0) + item.get("extras_price", 0)) * item.get("quantity", 1)
        for item in cart_items
    )


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


# ============================================================
# MENU DISPLAY
# ============================================================

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


def _build_full_example_menu(lang: str = "en") -> str:
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

    for cat, items in config.EXAMPLE_MENU.items():
        emoji    = config._CATEGORY_EMOJI_MAP.get(cat, "🍽️")
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
    if not products:
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
        emoji       = config._CATEGORY_EMOJI_MAP.get(cat, "🍽️")
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


def _detect_category_from_query(q: str) -> Optional[str]:
    for cat, kws in config.CATEGORY_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return cat
    return None


def _detect_price_menu_intent(q: str) -> bool:
    """
    v14.4 FIX 5: Guard against firing on pure multi-size order strings.
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
        any(kw in q for kw in config.INTENT_KEYWORDS["price"]) and
        any(kw in q for kw in config.INTENT_KEYWORDS["menu"])
    )


# ============================================================
# MULTI-ITEM PARSERS
# ============================================================

def _parse_multi_size_from_text(text: str, product: Dict) -> List[Dict]:
    """
    v14.4 FIX 1: Quantity tokens at the start of each part are stripped
    before size-hint matching.
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

        qty         = 1
        qty_pattern = re.compile(
            r'^(\d+(?:st|nd|rd|th)?|' +
            '|'.join(re.escape(k) for k in sorted(config.QUANTITY_WORDS.keys(), key=len, reverse=True)) +
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

        for sh in sorted(config.SIZE_HINTS, key=len, reverse=True):
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
            r'^(\d+(?:st|nd|rd|th)?|' + '|'.join(re.escape(k) for k in config.QUANTITY_WORDS.keys()) + r')\s+',
            part, re.IGNORECASE
        )
        if qty_match:
            qty         = _extract_quantity(qty_match.group(1))
            part_no_qty = part[qty_match.end():].strip()
        else:
            part_no_qty = part

        size_hint  = ""
        part_clean = part_no_qty
        for sh in sorted(config.SIZE_HINTS, key=len, reverse=True):
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
