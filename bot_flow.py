"""
bot_flow.py — Core order flow handlers: single-item, multi-item, queue advance,
              finalise, handle_full_price_display
"""

import re
import logging
from typing import Dict, List, Any, Optional

import config
from database import (
    calculate_delivery_charge, _delivery_charge_info_text,
    create_order_from_cart, get_delivery_time, _track,
)
from sessions import (
    get_user_session, reset_cart_only, update_preferences,
    _is_same_address_request, _is_valid_address, extract_address,
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
)

logger = logging.getLogger("RestaurantBot.v14.6")


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
