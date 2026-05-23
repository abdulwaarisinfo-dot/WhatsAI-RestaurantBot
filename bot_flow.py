"""
bot_flow.py — Core bot flow logic: single/multi-item ordering, price display,
              product queue advancement, and table reservation flow.
WhatsApp AI Restaurant Bot v14.7 + Table Reservations
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

import config
from database import (
    create_order_from_cart, calculate_delivery_charge,
    _delivery_charge_info_text, get_delivery_time,
    save_reservation, get_all_reservations,
)
from sessions import get_user_session, reset_cart_only, update_preferences
from products import (
    _recalc_cart, _build_cart_summary, build_cart_item,
    _match_variant, _parse_multi_size_from_text, _extract_extras_from_text,
    _find_product_by_query, filter_products, _products_by_category,
    _detect_category_from_query, _build_full_price_menu,
    _extract_qty_from_size_response, _is_product_query,
)
from whatsapp import (
    send_whatsapp_text, send_whatsapp_buttons,
    _ask_size, _ask_spice, _ask_extras, _ask_multi_spice,
    _ask_reservation_name, _ask_reservation_date, _ask_reservation_time,
    _ask_reservation_guests, _ask_reservation_notes, _ask_reservation_confirm,
)

logger = logging.getLogger("RestaurantBot.v14.7")


# ============================================================
# PRODUCT QUEUE ADVANCEMENT
# ============================================================

async def _advance_product_queue(from_num: str, session: Dict, lang: str):
    """
    Process the next item in the product_queue.
    If the queue is empty, move to step 5 (cart confirmation).
    """
    pq = session.get("product_queue", [])

    if not pq:
        # Queue exhausted — show cart summary for confirmation
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
        return

    current = pq[0]
    product = current.get("product")
    qty     = current.get("qty", 1)

    if not product:
        # Corrupt queue entry — skip it
        pq.pop(0)
        session["product_queue"] = pq
        await _advance_product_queue(from_num, session, lang)
        return

    variants     = product.get("variants", [])
    spice_levels = product.get("spice_levels", [])
    extras       = product.get("extras", [])

    # If product has only one variant, no size needed — add directly
    if len(variants) == 1:
        size  = variants[0]["size"]
        price = variants[0]["price"]

        if spice_levels:
            # Need spice
            session["pending_order"] = {
                "product_ref":    product,
                "size":           size,
                "price":          price,
                "qty":            qty,
                "spice_levels":   spice_levels,
                "extras_options": extras,
                "dish":           product.get("title", ""),
            }
            session["step"] = 2
            await _ask_spice(from_num, product, lang)
        elif extras:
            # Need extras
            cart_item = build_cart_item(product, size, "", [], qty)
            session["cart"].append(cart_item)
            session["pending_order"] = {"product_ref": product, "extras_options": extras}
            session["step"] = 30
            await _ask_extras(from_num, product, lang)
        else:
            # Fully resolved — add to cart
            cart_item = build_cart_item(product, size, "", [], qty)
            session.setdefault("cart", []).append(cart_item)
            pq.pop(0)
            session["product_queue"] = pq
            await _advance_product_queue(from_num, session, lang)
        return

    if variants:
        # Multiple sizes — ask
        session["pending_order"] = {
            "product_ref":    product,
            "variants":       variants,
            "spice_levels":   spice_levels,
            "extras_options": extras,
            "qty":            qty,
            "dish":           product.get("title", ""),
        }
        session["step"] = 1
        await _ask_size(from_num, product, lang)
    else:
        # No variants at all — use flat price
        price     = product.get("price", 0)
        cart_item = build_cart_item(product, "", "", [], qty)
        session.setdefault("cart", []).append(cart_item)
        pq.pop(0)
        session["product_queue"] = pq
        await _advance_product_queue(from_num, session, lang)


# ============================================================
# FINALISE SINGLE ITEM (add to cart → confirm or ask address)
# ============================================================

async def _finalise_single_item(
    from_num: str, session: Dict, cart_item: Dict, lang: str
):
    """
    Append cart_item to session cart, then either:
    - If there's a product_queue → advance it
    - If cart already has multiple items → step 5 (confirm)
    - Else → step 5 (confirm)
    """
    cart = session.setdefault("cart", [])
    cart.append(cart_item)
    session["cart"] = cart

    pq = session.get("product_queue", [])
    if pq:
        pq.pop(0)
        session["product_queue"] = pq
        await _advance_product_queue(from_num, session, lang)
        return

    session["step"] = 5
    total   = _recalc_cart(cart)
    summary = _build_cart_summary(cart, total, lang)
    confirm_msgs = {
        "en": f"{summary}\n\n✨ Looking great! Want to confirm? 😊",
        "ur": f"{summary}\n\n✨ تصدیق کریں یا مزید شامل کریں؟",
        "de": f"{summary}\n\n✨ Bestätigen oder mehr hinzufügen?",
    }
    await send_whatsapp_buttons(
        from_num,
        confirm_msgs.get(lang, confirm_msgs["en"]),
        ["✅ Confirm Order", "➕ Add More", "🗑️ Clear Cart"],
    )


# ============================================================
# SINGLE ITEM ORDER HANDLER
# ============================================================

async def _handle_single_item_order(from_num: str, msg_text: str, lang: str) -> bool:
    """
    Detect and begin ordering a single product from msg_text.
    Returns True if a product was matched and flow was started, False otherwise.
    """
    session = get_user_session(from_num)
    product = _find_product_by_query(msg_text)
    if not product:
        return False

    name         = product.get("title", "Item").strip().title()
    variants     = product.get("variants", [])
    spice_levels = product.get("spice_levels", [])
    extras       = product.get("extras", [])
    qty          = _extract_qty_from_size_response(msg_text)
    if qty < 1:
        qty = 1

    session["last_shown_product"] = product

    if not variants:
        # Single flat-price product
        price     = product.get("price", 0)
        cart_item = build_cart_item(product, "", "", [], qty)
        session["pending_order"] = {
            "product_ref":  product,
            "dish":         name,
            "size":         "",
            "price":        price,
            "spice":        "",
            "extras":       [],
            "qty":          qty,
        }
        if spice_levels:
            session["pending_order"]["spice_levels"] = spice_levels
            session["step"] = 2
            await _ask_spice(from_num, product, lang)
        elif extras:
            session["step"] = 3
            session["pending_order"]["extras_options"] = extras
            await _ask_extras(from_num, product, lang)
        else:
            session["step"] = 4
            ask_addr = {
                "en": (
                    f"✅ *{name}* added!\n\n"
                    "📍 What's your delivery address?\n"
                    "_(House no., street, area, city)_"
                ),
                "ur": f"✅ *{name}* شامل ہوگیا!\n\n📍 اپنا مکمل پتہ دیں:",
                "de": f"✅ *{name}* hinzugefügt!\n\n📍 Lieferadresse angeben:",
            }
            await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
        return True

    if len(variants) == 1:
        size  = variants[0]["size"]
        price = variants[0]["price"]
        session["pending_order"] = {
            "product_ref":    product,
            "dish":           name,
            "size":           size,
            "price":          price,
            "variants":       variants,
            "spice_levels":   spice_levels,
            "extras_options": extras,
            "qty":            qty,
        }
        if spice_levels:
            session["step"] = 2
            await _ask_spice(from_num, product, lang)
        elif extras:
            session["step"] = 3
            await _ask_extras(from_num, product, lang)
        else:
            session["step"] = 4
            ask_addr = {
                "en": (
                    f"✅ *{name}* ({size}) added!\n\n"
                    "📍 What's your delivery address?\n"
                    "_(House no., street, area, city)_"
                ),
                "ur": f"✅ *{name}* ({size}) شامل ہوگیا!\n\n📍 اپنا مکمل پتہ دیں:",
                "de": f"✅ *{name}* ({size}) hinzugefügt!\n\n📍 Lieferadresse angeben:",
            }
            await send_whatsapp_text(from_num, ask_addr.get(lang, ask_addr["en"]))
        return True

    # Multiple variants — ask size
    session["pending_order"] = {
        "product_ref":    product,
        "dish":           name,
        "variants":       variants,
        "spice_levels":   spice_levels,
        "extras_options": extras,
        "qty":            qty,
    }
    session["step"] = 1
    await _ask_size(from_num, product, lang)
    return True


# ============================================================
# FULL PRICE DISPLAY
# ============================================================

async def _handle_full_price_display(from_num: str, q: str, lang: str):
    """
    Detect and display pricing info. Handles category-filtered or full menu pricing.
    """
    category = _detect_category_from_query(q)
    if category:
        products = _products_by_category(category) or filter_products(q)
    else:
        products = config.PRODUCTS_DATA or []

    if not products:
        no_products = {
            "en": "😔 No products found matching your query. Try *show menu* to see everything!",
            "ur": "😔 کوئی آئٹم نہیں ملا۔ *مینو دکھائیں* آزمائیں!",
            "de": "😔 Keine Produkte gefunden. Versuchen Sie *Menü anzeigen*!",
        }
        await send_whatsapp_text(from_num, no_products.get(lang, no_products["en"]))
        return

    price_menu = _build_full_price_menu(products, lang)
    header_map = {
        "en": "💰 *Our Prices* — here's what we've got for you:\n\n",
        "ur": "💰 *قیمتوں کی فہرست:*\n\n",
        "de": "💰 *Unsere Preise:*\n\n",
    }
    await send_whatsapp_text(from_num, header_map.get(lang, header_map["en"]) + price_menu)


# ============================================================
# MULTI-ITEM ORDER HANDLER
# ============================================================

async def handle_multi_item_order(from_num: str, msg_text: str, lang: str) -> bool:
    """
    Parse multiple products from a single message, add resolved ones directly
    to the cart, and queue items that need size/spice/extras clarification.
    Returns True if at least one product was matched.
    """
    session = get_user_session(from_num)

    # Split on common multi-item separators
    separators  = r'\band\b|\baur\b|\+|,|\bplus\b|\balso\b|\bke saath\b|اور'
    raw_parts   = re.split(separators, msg_text, flags=re.IGNORECASE)
    parts       = [p.strip() for p in raw_parts if p.strip()]

    if len(parts) <= 1:
        return False

    matched_products: List[Dict] = []
    for part in parts:
        product = _find_product_by_query(part)
        if product:
            qty = _extract_qty_from_size_response(part)
            if qty < 1:
                qty = 1
            matched_products.append({"product": product, "qty": qty, "raw": part})

    if not matched_products:
        return False

    cart          = session.setdefault("cart", [])
    product_queue: List[Dict] = []
    missing_queue: List[Dict] = []

    for mp in matched_products:
        product  = mp["product"]
        qty      = mp["qty"]
        raw_part = mp["raw"]
        variants = product.get("variants", [])

        if not variants:
            # Flat price — add directly
            cart_item = build_cart_item(product, "", "", [], qty)
            cart.append(cart_item)
            continue

        if len(variants) == 1:
            # Only one size — add directly, queue for spice/extras later
            cart_item = build_cart_item(product, variants[0]["size"], "", [], qty)
            spice_levels = product.get("spice_levels", [])
            extras       = product.get("extras", [])
            if spice_levels or extras:
                product_queue.append({"product": product, "qty": qty})
            else:
                cart.append(cart_item)
            continue

        # Try to match size from the raw text
        matched_variant = _match_variant(variants, raw_part)
        if matched_variant:
            spice_levels = product.get("spice_levels", [])
            extras       = product.get("extras", [])
            if spice_levels or extras:
                product_queue.append({"product": product, "qty": qty})
            else:
                cart_item = build_cart_item(product, matched_variant["size"], "", [], qty)
                cart.append(cart_item)
        else:
            # Need to ask size
            missing_queue.append({"product": product, "qty": qty})

    session["cart"] = cart

    if not matched_products:
        return False

    if missing_queue:
        session["missing_info_queue"] = missing_queue
        session["product_queue"]      = product_queue
        session["step"] = 10
        await _ask_size(from_num, missing_queue[0]["product"], lang)
        return True

    if product_queue:
        session["product_queue"] = product_queue
        await _advance_product_queue(from_num, session, lang)
        return True

    # Everything resolved
    if cart:
        session["step"] = 5
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
        return True

    return False


# ============================================================
# RESERVATION — PARSE DATE HELPER
# ============================================================

def _parse_reservation_date(text: str) -> str:
    """
    Attempt to parse a human-readable date into YYYY-MM-DD.
    Supports: 'tomorrow', 'today', DD Month, YYYY-MM-DD, DD/MM/YYYY.
    Returns the input string unchanged if parsing fails.
    """
    text = text.strip().lower()
    today = datetime.now().date()

    if text in ("today", "آج", "heute"):
        return today.strftime("%Y-%m-%d")
    if text in ("tomorrow", "kal", "کل", "morgen"):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")

    # ISO format
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', text)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$', text)
    if m:
        try:
            d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # "25 may" or "may 25"
    month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }
    m = re.match(r'^(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?$', text)
    if m:
        day   = int(m.group(1))
        mon   = month_map.get(m.group(2)[:3])
        year  = int(m.group(3)) if m.group(3) else today.year
        if mon:
            try:
                d = datetime(year, mon, day).date()
                return d.strftime("%Y-%m-%d")
            except ValueError:
                pass

    m = re.match(r'^([a-z]+)\s+(\d{1,2})(?:\s+(\d{4}))?$', text)
    if m:
        mon  = month_map.get(m.group(1)[:3])
        day  = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if mon:
            try:
                d = datetime(year, mon, day).date()
                return d.strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Return original text capitalised as a best-effort display value
    return text.strip().title()


def _parse_reservation_time(text: str) -> str:
    """
    Normalise a time string to a slot label from config.RESERVATION_TIME_SLOTS,
    or return a tidy HH:MM / 12-hour label if no slot matches.
    """
    text = text.strip().lower()

    # Try direct slot match first
    for slot in config.RESERVATION_TIME_SLOTS:
        if slot.lower() in text or text in slot.lower():
            return slot

    # Parse 7pm / 7:30pm / 19:00
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text)
    if m:
        hour   = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        meridiem = m.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        # Find nearest slot
        candidate = f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
        for slot in config.RESERVATION_TIME_SLOTS:
            if candidate.lower() in slot.lower() or slot.lower() in candidate.lower():
                return slot
        return candidate

    return text.strip().title()


# ============================================================
# RESERVATION — START
# ============================================================

async def handle_reservation_start(from_num: str, session: Dict, lang: str):
    """
    Begin a new reservation flow. Sets step = -1 and asks for name.
    Clears any previous pending_reservation.
    """
    session["pending_reservation"] = {}
    session["step"] = -1
    await _ask_reservation_name(from_num, lang)


# ============================================================
# RESERVATION — STEP HANDLER
# ============================================================

async def handle_reservation_step(
    from_num: str, session: Dict, msg_text: str, lang: str
) -> bool:
    """
    Handle one step of the reservation flow (step < 0).
    Returns True if the message was consumed by the reservation flow.
    """
    step    = session.get("step", 0)
    pending = session.setdefault("pending_reservation", {})
    q       = msg_text.strip().lower()

    # ── Cancel at any point ─────────────────────────────────
    if any(kw in q for kw in ["cancel", "منسوخ", "abbrechen", "❌ cancel reservation"]):
        session["step"]                = 0
        session["pending_reservation"] = {}
        cancel_msg = {
            "en": "No problem — reservation cancelled! 😊 Let me know if you'd like to book again.",
            "ur": "ریزرویشن منسوخ کر دی گئی! 😊 جب چاہیں دوبارہ بک کریں۔",
            "de": "Kein Problem — Reservierung abgebrochen! 😊 Buchen Sie jederzeit erneut.",
        }
        await send_whatsapp_buttons(
            from_num,
            cancel_msg.get(lang, cancel_msg["en"]),
            ["🪑 Book a Table", "View Menu 📋", "Place Order 🛒"],
        )
        return True

    # ── Step -1: Collect name ────────────────────────────────
    if step == -1:
        if len(msg_text.strip()) < 2:
            err = {
                "en": "⚠️ Please enter a valid name for the reservation.",
                "ur": "⚠️ براہ کرم درست نام درج کریں۔",
                "de": "⚠️ Bitte einen gültigen Namen eingeben.",
            }
            await send_whatsapp_text(from_num, err.get(lang, err["en"]))
            return True
        pending["name"] = msg_text.strip().title()
        session["step"] = -2
        await _ask_reservation_date(from_num, lang)
        return True

    # ── Step -2: Collect date ────────────────────────────────
    if step == -2:
        parsed_date = _parse_reservation_date(msg_text)
        # Reject past dates (if we got a proper ISO date)
        if re.match(r'^\d{4}-\d{2}-\d{2}$', parsed_date):
            try:
                dt = datetime.strptime(parsed_date, "%Y-%m-%d").date()
                if dt < datetime.now().date():
                    err = {
                        "en": "⚠️ That date is in the past! Please enter a future date 📅",
                        "ur": "⚠️ یہ تاریخ گزر چکی ہے! آنے والی تاریخ دیں۔",
                        "de": "⚠️ Dieses Datum liegt in der Vergangenheit! Bitte ein zukünftiges Datum eingeben.",
                    }
                    await send_whatsapp_text(from_num, err.get(lang, err["en"]))
                    return True
            except ValueError:
                pass
        pending["date"] = parsed_date
        session["step"] = -3
        await _ask_reservation_time(from_num, lang)
        return True

    # ── Step -3: Collect time ────────────────────────────────
    if step == -3:
        parsed_time = _parse_reservation_time(msg_text)
        pending["time_slot"] = parsed_time
        session["step"] = -4
        await _ask_reservation_guests(from_num, lang)
        return True

    # ── Step -4: Collect guest count ─────────────────────────
    if step == -4:
        nums = re.findall(r'\d+', msg_text)
        if not nums:
            err = {
                "en": "⚠️ Please enter a number — e.g. *2* or *4 people*.",
                "ur": "⚠️ تعداد لکھیں — جیسے *2* یا *4 افراد*۔",
                "de": "⚠️ Bitte eine Zahl eingeben — z.B. *2* oder *4 Personen*.",
            }
            await send_whatsapp_text(from_num, err.get(lang, err["en"]))
            return True

        guests = int(nums[0])
        max_g  = getattr(config, "RESERVATION_MAX_GUESTS", 20)
        if guests < 1 or guests > max_g:
            err = {
                "en": f"⚠️ Please enter between 1 and {max_g} guests.",
                "ur": f"⚠️ 1 سے {max_g} کے درمیان مہمانوں کی تعداد لکھیں۔",
                "de": f"⚠️ Bitte zwischen 1 und {max_g} Gäste angeben.",
            }
            await send_whatsapp_text(from_num, err.get(lang, err["en"]))
            return True

        pending["guests"] = guests
        session["step"]   = -5
        await _ask_reservation_notes(from_num, lang)
        return True

    # ── Step -5: Collect notes ───────────────────────────────
    if step == -5:
        if any(skip in q for skip in ["no", "nope", "skip", "nahi", "nein", "nahin", "none", "nothing"]):
            pending["notes"] = ""
        else:
            pending["notes"] = msg_text.strip()
        session["step"] = -6
        await _ask_reservation_confirm(from_num, pending, lang)
        return True

    # ── Step -6: Confirm or cancel ───────────────────────────
    if step == -6:
        confirm_keywords = [
            "confirm", "yes", "okay", "ok", "haan", "ja",
            "✅", "✅ confirm reservation", "confirm reservation",
        ]
        if any(kw in q for kw in confirm_keywords):
            # Save to DB
            pending["user_id"]    = from_num
            pending["status"]     = "Pending"
            pending["created_at"] = datetime.utcnow().isoformat()
            pending["lang"]       = lang

            reservation_id = save_reservation(pending)

            session["step"]                = 0
            session["pending_reservation"] = {}

            if reservation_id and reservation_id != "db_error":
                ref  = str(reservation_id)[-6:]
                name = pending.get("name", "")
                date = pending.get("date", "")
                time = pending.get("time_slot", "")

                success_msg = {
                    "en": (
                        f"✅ *Reservation Confirmed!* 🎉\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 Name: *{name}*\n"
                        f"📅 Date: *{date}*\n"
                        f"🕐 Time: *{time}*\n"
                        f"👥 Guests: *{pending.get('guests', '—')}*\n"
                        f"📝 Notes: _{pending.get('notes', '') or 'None'}_\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"🔖 Ref: *#{ref}*\n\n"
                        f"We'll send you a confirmation shortly! See you soon 😊"
                    ),
                    "ur": (
                        f"✅ *ریزرویشن تصدیق ہوگئی!* 🎉\n\n"
                        f"👤 نام: *{name}*\n"
                        f"📅 تاریخ: *{date}*\n"
                        f"🕐 وقت: *{time}*\n"
                        f"👥 مہمان: *{pending.get('guests', '—')}*\n\n"
                        f"🔖 نمبر: *#{ref}*\n\n"
                        f"جلد تصدیق ملے گی! آپ کا انتظار رہے گا 😊"
                    ),
                    "de": (
                        f"✅ *Reservierung bestätigt!* 🎉\n\n"
                        f"👤 Name: *{name}*\n"
                        f"📅 Datum: *{date}*\n"
                        f"🕐 Uhrzeit: *{time}*\n"
                        f"👥 Gäste: *{pending.get('guests', '—')}*\n\n"
                        f"🔖 Ref: *#{ref}*\n\n"
                        f"Wir freuen uns auf Ihren Besuch! 😊"
                    ),
                }
                await send_whatsapp_buttons(
                    from_num,
                    success_msg.get(lang, success_msg["en"]),
                    ["View Menu 📋", "Place Order 🛒", "📅 My Reservations"],
                )
            else:
                db_err = {
                    "en": "⚠️ Sorry, there was an issue saving your reservation. Please try again!",
                    "ur": "⚠️ معذرت، ریزرویشن محفوظ نہیں ہوئی۔ دوبارہ کوشش کریں!",
                    "de": "⚠️ Fehler beim Speichern der Reservierung. Bitte erneut versuchen!",
                }
                await send_whatsapp_text(from_num, db_err.get(lang, db_err["en"]))
            return True

        # Cancel from confirmation screen
        session["step"]                = 0
        session["pending_reservation"] = {}
        cancel_msg = {
            "en": "Reservation cancelled — no worries! 😊 Feel free to book again any time.",
            "ur": "ریزرویشن منسوخ! 😊 جب چاہیں دوبارہ بک کریں۔",
            "de": "Reservierung abgebrochen! 😊 Jederzeit erneut buchen.",
        }
        await send_whatsapp_buttons(
            from_num,
            cancel_msg.get(lang, cancel_msg["en"]),
            ["🪑 Book a Table", "View Menu 📋", "Place Order 🛒"],
        )
        return True

    return False


# ============================================================
# MY RESERVATIONS
# ============================================================

async def handle_my_reservations(
    from_num: str, session: Dict, msg_text: str, lang: str
):
    """
    Show the user's existing reservations.
    """
    try:
        reservations = get_all_reservations(user_id=from_num, limit=5)
    except TypeError:
        # Older database.py may not accept user_id kwarg — filter manually
        all_res      = get_all_reservations(limit=50)
        reservations = [r for r in all_res if r.get("user_id") == from_num][:5]

    if not reservations:
        no_res = {
            "en": (
                "📅 You don't have any reservations yet!\n\n"
                "Would you like to book a table? 🪑"
            ),
            "ur": "📅 آپ کی کوئی ریزرویشن نہیں ہے۔\n\nکیا میز بک کرنی ہے؟ 🪑",
            "de": "📅 Sie haben noch keine Reservierungen.\n\nMöchten Sie einen Tisch reservieren? 🪑",
        }
        await send_whatsapp_buttons(
            from_num,
            no_res.get(lang, no_res["en"]),
            ["🪑 Book a Table", "View Menu 📋", "Place Order 🛒"],
        )
        return

    lines = []
    for res in reservations:
        ref    = str(res.get("_id", ""))[-6:]
        name   = res.get("name", "—")
        date   = res.get("date", "—")
        time   = res.get("time_slot", "—")
        guests = res.get("guests", "—")
        status = res.get("status", "Pending")
        status_emoji = {
            "Pending":   "⏳",
            "Confirmed": "✅",
            "Cancelled": "❌",
            "Completed": "🎉",
            "No Show":   "😔",
        }.get(status, "📋")
        lines.append(
            f"{status_emoji} *#{ref}* — {name}\n"
            f"   📅 {date}  🕐 {time}  👥 {guests}\n"
            f"   Status: *{status}*"
        )

    header = {
        "en": f"📅 *Your Reservations* ({len(reservations)}):\n\n",
        "ur": f"📅 *آپ کی ریزرویشنز* ({len(reservations)}):\n\n",
        "de": f"📅 *Ihre Reservierungen* ({len(reservations)}):\n\n",
    }.get(lang, f"📅 *Your Reservations* ({len(reservations)}):\n\n")

    body = header + "\n\n".join(lines)
    await send_whatsapp_buttons(
        from_num,
        body,
        ["🪑 Book a Table", "View Menu 📋", "Place Order 🛒"],
    )
