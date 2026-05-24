"""
bot_flow.py — Core order flow handlers: single-item, multi-item, queue advance,
              finalise, handle_full_price_display, and Table Reservation flow
WhatsApp AI Restaurant Bot v14.7 + Table Reservations
"""

import re
import logging
from typing import Dict, List, Any, Optional

import config
from database import (
    calculate_delivery_charge, _delivery_charge_info_text,
    create_order_from_cart, get_delivery_time, _track,
    create_reservation, get_reservations_by_user,
    cancel_reservation_by_user, get_latest_active_reservation,
    check_slot_availability,
)
from sessions import (
    get_user_session, reset_cart_only, update_preferences,
    _is_same_address_request, _is_valid_address, extract_address,
    reset_reservation_flow,
    parse_reservation_date, parse_reservation_time, parse_guest_count,
    is_reservation_date_valid, format_reservation_summary,
)
from products import (
    _recalc_cart, _build_cart_summary, build_cart_item,
    _match_variant, _parse_multi_size_from_text, _extract_extras_from_text,
    _find_product_by_query, filter_products, _products_by_category,
    _detect_category_from_query, _build_full_example_menu, _build_text_menu,
    _build_full_price_menu, _extract_qty_from_size_response,
    parse_multi_item_order, _group_parsed_by_product,
)
from whatsapp import (
    send_whatsapp_text, send_whatsapp_buttons,
    _ask_size, _ask_spice, _ask_extras, _ask_multi_spice,
    _ask_reservation_name, _ask_reservation_date, _ask_reservation_time,
    _ask_reservation_guests, _ask_reservation_notes, _ask_reservation_confirm,
    send_reservation_confirmed,
)

logger = logging.getLogger("RestaurantBot.v14.7")


# ============================================================
# QUEUE ADVANCE
# ============================================================

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


# ============================================================
# FINALISE SINGLE ITEM
# ============================================================

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


# ============================================================
# SINGLE ITEM ORDER
# ============================================================

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


# ============================================================
# FULL PRICE DISPLAY
# ============================================================

async def _handle_full_price_display(from_number: str, q: str, lang: str):
    category = _detect_category_from_query(q)
    if category:
        products = _products_by_category(category) or filter_products(q)
        cat_name = category.capitalize()
        emoji    = config._CATEGORY_EMOJI_MAP.get(category, "🍽️")
        title_map = {
            "en": f"{emoji} {cat_name} Menu & Prices",
            "ur": f"{emoji} {cat_name} مینو اور قیمتیں",
            "de": f"{emoji} {cat_name} Menü & Preise",
        }
        title = title_map.get(lang, title_map["en"])
    else:
        products = config.PRODUCTS_DATA[:15]
        emoji    = "🍽️"
        title_map = {
            "en": "Full Menu & Prices",
            "ur": "مکمل مینو اور قیمتیں",
            "de": "Vollständiges Menü & Preise",
        }
        title = title_map.get(lang, title_map["en"])

    if not products:
        example_menu_text = _build_full_example_menu(lang)
        await send_whatsapp_text(from_number, example_menu_text)
        return

    menu_text = _build_full_price_menu(products, emoji, title)
    await send_whatsapp_text(from_number, menu_text)


# ============================================================
# MULTI ITEM ORDER
# ============================================================

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
# TABLE RESERVATION FLOW
# ============================================================

async def handle_reservation_start(from_number: str, session: Dict, lang: str):
    """
    Kick off the reservation flow:
    reset any in-progress reservation, set step, ask for name.
    """
    reset_reservation_flow(session)
    session["step"] = config.STEP_RESERVATION_NAME
    await _ask_reservation_name(from_number, lang)


async def handle_reservation_flow(from_number: str, msg_text: str, lang: str, session: Dict) -> bool:
    """
    Central dispatcher for all reservation steps (steps -1 through -6).
    Returns True if the message was consumed by the reservation flow.
    """
    step = session.get("step", 0)

    # ── STEP -1: collect name ──────────────────────────────────
    if step == config.STEP_RESERVATION_NAME:
        name = msg_text.strip()
        if len(name) < 2:
            err = {
                "en": "Please enter your full name (at least 2 characters).",
                "ur": "براہ کرم اپنا پورا نام لکھیں (کم از کم 2 حروف)۔",
                "de": "Bitte geben Sie Ihren vollständigen Namen ein (mindestens 2 Zeichen).",
            }
            await send_whatsapp_text(from_number, err.get(lang, err["en"]))
            return True

        session["pending_reservation"]["name"] = name.title()
        session["step"] = config.STEP_RESERVATION_DATE
        await _ask_reservation_date(from_number, lang)
        return True

    # ── STEP -2: collect date ──────────────────────────────────
    if step == config.STEP_RESERVATION_DATE:
        date_str = parse_reservation_date(msg_text)
        if not date_str:
            err = {
                "en": "I couldn't understand that date 🤔\nPlease try: *tomorrow*, *25 May*, or *2026-05-25*",
                "ur": "تاریخ سمجھ نہیں آئی۔ کوشش کریں: *کل*, *25 مئی*, یا *2026-05-25*",
                "de": "Datum nicht erkannt 🤔\nBitte versuchen: *morgen*, *25. Mai*, oder *2026-05-25*",
            }
            await send_whatsapp_text(from_number, err.get(lang, err["en"]))
            return True

        if not is_reservation_date_valid(date_str):
            err = {
                "en": "⚠️ That date has already passed! Please choose today or a future date.",
                "ur": "⚠️ یہ تاریخ گزر چکی ہے! آج یا آنے والی تاریخ منتخب کریں۔",
                "de": "⚠️ Dieses Datum liegt in der Vergangenheit! Bitte wählen Sie heute oder ein zukünftiges Datum.",
            }
            await send_whatsapp_text(from_number, err.get(lang, err["en"]))
            return True

        session["pending_reservation"]["date"] = date_str
        session["step"] = config.STEP_RESERVATION_TIME
        await _ask_reservation_time(from_number, lang)
        return True

    # ── STEP -3: collect time ──────────────────────────────────
    if step == config.STEP_RESERVATION_TIME:
        time_str = parse_reservation_time(msg_text)
        if not time_str:
            err = {
                "en": "I didn't catch that time 🤔\nPlease try: *7pm*, *19:00*, or *7:30 pm*",
                "ur": "وقت سمجھ نہیں آیا۔ کوشش کریں: *7pm*, *19:00*",
                "de": "Uhrzeit nicht erkannt 🤔\nBitte versuchen: *19:00*, *7pm*",
            }
            await send_whatsapp_text(from_number, err.get(lang, err["en"]))
            return True

        # Check slot availability
        date_str = session["pending_reservation"].get("date", "")
        if not check_slot_availability(date_str, time_str):
            err = {
                "en": (
                    f"⚠️ Sorry, the *{time_str}* slot on *{date_str}* is fully booked.\n\n"
                    f"Please choose a different time:"
                ),
                "ur": f"⚠️ معذرت، *{date_str}* کو *{time_str}* کا وقت بھرا ہوا ہے۔ دوسرا وقت چنیں:",
                "de": f"⚠️ Der Slot *{time_str}* am *{date_str}* ist leider ausgebucht.\n\nBitte wählen Sie eine andere Uhrzeit:",
            }
            await send_whatsapp_text(from_number, err.get(lang, err["en"]))
            return True

        session["pending_reservation"]["time_slot"] = time_str
        session["step"] = config.STEP_RESERVATION_GUESTS
        await _ask_reservation_guests(from_number, lang)
        return True

    # ── STEP -4: collect guest count ──────────────────────────
    if step == config.STEP_RESERVATION_GUESTS:
        guests = parse_guest_count(msg_text)
        if not guests:
            err = {
                "en": f"Please enter a number of guests between 1 and {config.RESERVATION_MAX_GUESTS}.",
                "ur": f"براہ کرم 1 سے {config.RESERVATION_MAX_GUESTS} کے درمیان نمبر لکھیں۔",
                "de": f"Bitte eine Zahl zwischen 1 und {config.RESERVATION_MAX_GUESTS} eingeben.",
            }
            await send_whatsapp_text(from_number, err.get(lang, err["en"]))
            return True

        session["pending_reservation"]["guests"] = guests
        session["step"] = config.STEP_RESERVATION_NOTES
        await _ask_reservation_notes(from_number, lang)
        return True

    # ── STEP -5: collect notes (optional) ─────────────────────
    if step == config.STEP_RESERVATION_NOTES:
        skip_words = {"no", "skip", "none", "nothing", "nahi", "nope", "nein", "nahin", "—", "-"}
        notes_text = msg_text.strip()
        if notes_text.lower() in skip_words:
            notes_text = ""
        session["pending_reservation"]["notes"] = notes_text
        session["step"] = config.STEP_RESERVATION_CONFIRM
        await _ask_reservation_confirm(from_number, session["pending_reservation"], lang)
        return True

    # ── STEP -6: confirm or edit ───────────────────────────────
    if step == config.STEP_RESERVATION_CONFIRM:
        q = msg_text.lower().strip()

        # Confirmed
        if any(kw in q for kw in ["confirm", "yes", "ok", "okay", "haan", "bilkul",
                                    "zaroor", "sure", "proceed", "✅", "booking",
                                    "confirm booking", "ji", "theek"]):
            pr = session["pending_reservation"]
            reservation_id = create_reservation(
                user_id   = from_number,
                name      = pr.get("name", ""),
                date      = pr.get("date", ""),
                time_slot = pr.get("time_slot", ""),
                guests    = pr.get("guests", 1),
                notes     = pr.get("notes", ""),
            )

            if reservation_id == "db_error":
                db_err = {
                    "en": "⚠️ Something went wrong saving your reservation. Please try again!",
                    "ur": "⚠️ ریزرویشن محفوظ کرنے میں مسئلہ ہوا۔ دوبارہ کوشش کریں!",
                    "de": "⚠️ Fehler beim Speichern. Bitte erneut versuchen!",
                }
                await send_whatsapp_text(from_number, db_err.get(lang, db_err["en"]))
                reset_reservation_flow(session)
                return True

            session["reservation_count"] = session.get("reservation_count", 0) + 1
            await send_reservation_confirmed(from_number, reservation_id, pr, lang)
            reset_reservation_flow(session)
            return True

        # Edit — restart flow but preserve name if possible
        if any(kw in q for kw in ["edit", "change", "modify", "update", "✏️", "no",
                                    "nahi", "nein", "start over", "restart"]):
            session["step"] = config.STEP_RESERVATION_NAME
            edit_msg = {
                "en": "No problem! Let's start over. What name should the reservation be under?",
                "ur": "ٹھیک ہے! دوبارہ شروع کرتے ہیں۔ ریزرویشن کس نام پر ہو؟",
                "de": "Kein Problem! Fangen wir von vorne an. Auf welchen Namen?",
            }
            await send_whatsapp_text(from_number, edit_msg.get(lang, edit_msg["en"]))
            return True

        # Cancel flow
        if any(kw in q for kw in ["cancel", "❌", "band karo", "nahi chahiye"]):
            reset_reservation_flow(session)
            cancel_msg = {
                "en": "Reservation cancelled — no problem at all! 😊\nFeel free to book a table anytime.",
                "ur": "ریزرویشن منسوخ! جب چاہیں دوبارہ بک کریں۔ 😊",
                "de": "Reservierung abgebrochen. Kein Problem! 😊",
            }
            await send_whatsapp_buttons(
                from_number,
                cancel_msg.get(lang, cancel_msg["en"]),
                ["🪑 Book A Table", "View Menu 📋", "Place Order 🛒"],
            )
            return True

        # Unrecognised — re-show confirmation
        await _ask_reservation_confirm(from_number, session["pending_reservation"], lang)
        return True

    return False


async def handle_my_reservations(from_number: str, msg_text: str, lang: str, session: Dict):
    """
    Show the user's active reservations and let them cancel one.
    Triggered by 'my reservations' / 'cancel reservation' intent.
    """
    q = msg_text.lower().strip()

    # ── Cancel a specific reservation ─────────────────────────
    cancel_match = re.search(r'cancel.*?#?([a-f0-9]{6,24})', q, re.IGNORECASE)
    if cancel_match:
        rid_fragment = cancel_match.group(1)
        from database import reservations_col, _str_id
        if reservations_col:
            # Try to find by last-6 of ID
            all_user_res = [_str_id(d) for d in reservations_col.find({"user_id": from_number})]
            matched = next(
                (r for r in all_user_res if str(r.get("_id", "")).endswith(rid_fragment)),
                None,
            )
            if matched:
                success = cancel_reservation_by_user(from_number, matched["_id"])
                if success:
                    ok = {
                        "en": f"✅ Reservation *#{rid_fragment}* has been cancelled. See you next time! 😊",
                        "ur": f"✅ ریزرویشن *#{rid_fragment}* منسوخ ہوگئی۔ اگلی بار ملیں گے! 😊",
                        "de": f"✅ Reservierung *#{rid_fragment}* wurde storniert. Bis zum nächsten Mal! 😊",
                    }
                    await send_whatsapp_text(from_number, ok.get(lang, ok["en"]))
                    return
                else:
                    fail = {
                        "en": "⚠️ Could not cancel that reservation — it may already be cancelled or completed.",
                        "ur": "⚠️ یہ ریزرویشن منسوخ نہیں ہوسکی — شاید پہلے ہی منسوخ یا مکمل ہوچکی ہے۔",
                        "de": "⚠️ Diese Reservierung konnte nicht storniert werden.",
                    }
                    await send_whatsapp_text(from_number, fail.get(lang, fail["en"]))
                    return

    # ── Show list of reservations ──────────────────────────────
    reservations = get_reservations_by_user(from_number, limit=5)

    if not reservations:
        no_res = {
            "en": (
                "📋 You don't have any reservations yet!\n\n"
                "Type *book a table* to make one. 🪑"
            ),
            "ur": "📋 ابھی کوئی ریزرویشن نہیں ہے!\n\n*میز بک کریں* لکھ کر بک کریں۔ 🪑",
            "de": "📋 Sie haben noch keine Reservierungen!\n\nTippen Sie *Tisch reservieren*. 🪑",
        }
        await send_whatsapp_buttons(
            from_number,
            no_res.get(lang, no_res["en"]),
            ["🪑 Book A Table", "View Menu 📋", "Place Order 🛒"],
        )
        return

    header = {
        "en": "🪑 *Your Reservations:*\n━━━━━━━━━━━━━━━━━━━━━━━━\n",
        "ur": "🪑 *آپ کی ریزرویشنز:*\n━━━━━━━━━━━━━━━━━━━━━━━━\n",
        "de": "🪑 *Ihre Reservierungen:*\n━━━━━━━━━━━━━━━━━━━━━━━━\n",
    }
    lines = [header.get(lang, header["en"])]

    status_emoji = {
        "Pending":   "⏳", "Confirmed": "✅",
        "Cancelled": "❌", "Completed": "🎉", "No Show": "🚫",
    }

    for res in reservations:
        rid    = str(res.get("_id", ""))[-6:]
        date   = res.get("date", "—")
        time_  = res.get("time_slot", "—")
        guests = res.get("guests", "—")
        status = res.get("status", "Pending")
        emoji  = status_emoji.get(status, "📋")
        lines.append(
            f"{emoji} *#{rid}* — {date} at {time_} — {guests} guests — _{status}_"
        )

    cancel_hint = {
        "en": "\n\nTo cancel one, type: *cancel #[ref]*\nE.g. _cancel #abc123_",
        "ur": "\n\nمنسوخ کرنے کے لیے: *cancel #[نمبر]* لکھیں",
        "de": "\n\nZum Stornieren: *cancel #[Ref]* eingeben",
    }
    lines.append(cancel_hint.get(lang, cancel_hint["en"]))

    await send_whatsapp_buttons(
        from_number,
        "\n".join(lines),
        ["🪑 Book A Table", "View Menu 📋", "Place Order 🛒"],
    )
