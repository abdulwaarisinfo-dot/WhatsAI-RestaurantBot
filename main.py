"""
main.py — FastAPI application, webhook handler, CRM/admin panel, and all REST APIs
WhatsApp AI Restaurant Bot v14.7
"""

import re
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pymongo import DESCENDING
from bson import ObjectId

import config
from database import (
    products_col, meta_col, analytics_col, orders_col, carts_col,
    load_data_realtime, init_analytics, _track, _str_id, _parse_json_field,
    calculate_delivery_charge, _delivery_charge_info_text,
    create_order_from_cart, get_delivery_time,
)
from sessions import (
    get_user_session, reset_cart_only, reset_for_new_order,
    update_preferences, detect_language, _is_rate_limited,
    _is_same_address_request, _is_valid_address, extract_address,
    get_faq_response, get_suggestions,
    _is_pure_greeting, _is_order_now_button, _is_post_order_small_talk,
)
from products import (
    _recalc_cart, _build_cart_summary, build_cart_item,
    _match_variant, _parse_multi_size_from_text, _extract_extras_from_text,
    _find_product_by_query, filter_products, _products_by_category,
    _detect_category_from_query, _detect_price_menu_intent,
    _build_full_example_menu, _build_text_menu, _build_full_price_menu,
    _extract_qty_from_size_response, _is_product_query,
)
from whatsapp import (
    send_whatsapp_text, send_whatsapp_buttons,
    _ask_size, _ask_spice, _ask_extras, _ask_multi_spice,
    _smart_fallback,
)
from bot_flow import (
    _advance_product_queue, _finalise_single_item,
    _handle_single_item_order, _handle_full_price_display,
    handle_multi_item_order,
)

logger = logging.getLogger("RestaurantBot.v14.7")

# ============================================================
# APP SETUP
# ============================================================

app = FastAPI(
    title="WhatsApp AI Restaurant Bot v14.7",
    version="14.7",
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
# WEBHOOK
# ============================================================

@app.get("/webhook")
async def verify_webhook(
    hub_mode: str          = None,
    hub_verify_token: str  = None,
    hub_challenge: str     = None,
):
    if hub_mode == "subscribe" and hub_verify_token == config.VERIFY_TOKEN:
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
        if any(kw in q for kw in config.INTENT_KEYWORDS["new_order"]):
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
        if any(kw in q for kw in config.INTENT_KEYWORDS["cancel"]):
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
        if any(kw in q for kw in config.INTENT_KEYWORDS["thanks"]) and step == 0 and not _is_product_query(q):
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
        # v14.6 FIX 1: Extract quantity from size response
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

            # v14.6 FIX 1: extract and store quantity from the size response
            extracted_qty = _extract_qty_from_size_response(msg_text)
            if extracted_qty > 1:
                po["qty"] = extracted_qty

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

            if any(kw in q for kw in config.INTENT_KEYWORDS["show_total"]):
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
        # v14.6 FIX 3: Check _is_same_address_request FIRST
        # ═══════════════════════════════════════════════════════
        if step == 4:
            po = session.get("pending_order", {})

            if _is_same_address_request(msg_text):
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
        # v14.6 FIX 3: Check _is_same_address_request FIRST
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

            if _is_same_address_request(msg_text):
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
        # v14.4 FIX 2 & 3
        # ═══════════════════════════════════════════════════════
        if step == 20:
            multi_queue  = session.get("multi_size_queue", [])
            product      = session.get("pending_order", {}).get("product_ref", {})
            spice_levels = product.get("spice_levels", []) if product else []
            cart_items   = list(session.get("cart", []))

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
        if any(kw in q for kw in config.INTENT_KEYWORDS["delivery_charge"]):
            dc         = config.BOT_DATA.get("delivery_charges", {})
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
        if any(kw in q for kw in config.INTENT_KEYWORDS["cart"]):
            cart = session.get("cart", [])
            if cart:
                total   = _recalc_cart(cart)
                summary = _build_cart_summary(cart, total, lang)
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
        if any(kw in q for kw in config.INTENT_KEYWORDS["clear"]):
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
        if any(kw in q for kw in config.INTENT_KEYWORDS["confirm"]) and session.get("cart"):
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
        order_intent   = any(kw in q for kw in config.INTENT_KEYWORDS["order"])
        price_intent   = _detect_price_menu_intent(q)
        menu_intent    = any(kw in q for kw in config.INTENT_KEYWORDS["menu"])
        inquiry_intent = any(kw in q for kw in config.INTENT_KEYWORDS["inquiry"])
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

            # ── v14.7 FIX: pre-fill size/qty if already stated ──────
            # When the user includes the size (and optionally qty) in
            # their order message — e.g. "Add 5 family pack Biryani" —
            # _handle_single_item_order parks at step 1 awaiting size.
            # If we can already match a variant from the original text,
            # skip the size prompt and advance the flow immediately.
            if handled and session.get("step") == 1:
                po       = session.get("pending_order", {})
                variants = po.get("variants", [])
                if variants:
                    pre_matched = _match_variant(variants, msg_text)
                    if pre_matched:
                        po["size"]  = pre_matched["size"]
                        po["price"] = pre_matched["price"]
                        pre_qty = _extract_qty_from_size_response(msg_text)
                        if pre_qty > 1:
                            po["qty"] = pre_qty
                        update_preferences(from_num, size=pre_matched["size"])
                        _track({f"size_preference.{pre_matched['size']}": 1})

                        spice_levels = po.get("spice_levels", [])
                        if spice_levels:
                            session["step"] = 2
                            product_ref = po.get("product_ref", {
                                "title": po.get("dish", ""),
                                "spice_levels": spice_levels,
                            })
                            await _ask_spice(from_num, product_ref, lang)
                        else:
                            po["spice"] = ""
                            product_ref = po.get("product_ref", {
                                "title": po.get("dish", ""),
                                "extras": po.get("extras_options", []),
                            })
                            has_extras      = await _ask_extras(from_num, product_ref, lang)
                            session["step"] = 3 if has_extras else 4
                            if not has_extras:
                                if session.get("cart"):
                                    cart_item = build_cart_item(
                                        po.get("product_ref", {}),
                                        po.get("size", ""), po.get("spice", ""),
                                        po.get("extras", []), po.get("qty", 1),
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
            # ── end v14.7 FIX ────────────────────────────────────────

            if handled:
                return JSONResponse({"status": "ok"})

        # ── Menu display ───────────────────────────────────────
        if menu_intent:
            _track({"total_searches": 1})
            category = _detect_category_from_query(q)
            if category:
                products = _products_by_category(category) or filter_products(q)
            else:
                products = config.PRODUCTS_DATA or []

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
        if any(kw in q for kw in config.INTENT_KEYWORDS["discount"]):
            disc = config.BOT_DATA.get("discount_message", {}).get(lang, config.BOT_DATA.get("discount_message", {}).get("en"))
            if disc:
                await send_whatsapp_text(from_num, disc)
                return JSONResponse({"status": "ok"})

        # ── Order status ───────────────────────────────────────
        if any(kw in q for kw in config.INTENT_KEYWORDS["status"]) and orders_col:
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

        # ── Greeting ───────────────────────────────────────────
        if _is_pure_greeting(q):
            greeting = config.BOT_DATA.get("initial_message", {}).get(lang, "Hey! 👋 Welcome. What would you like today? 🍽️")
            sugs     = get_suggestions(from_num, lang)

            top_items = config.PRODUCTS_DATA[:3] if config.PRODUCTS_DATA else []
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

        # ── Product name search ────────────────────────────────
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
    if username == config.CRM_USERNAME and password == config.SECRET_PASSWORD:
        return JSONResponse({"success": True})
    return JSONResponse({"success": False, "message": "Invalid credentials"}, status_code=401)


# ============================================================
# PRODUCTS API
# ============================================================

@app.get("/api/products")
async def get_products():
    load_data_realtime()
    return {"products": config.PRODUCTS_DATA}


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
    return {"faqs": config.BOT_DATA.get("faq", {})}


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
    return {"smart_suggestions": config.BOT_DATA.get("smart_suggestions", {})}


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
        "delivery_time":            config.BOT_DATA.get("delivery_time", "35-45 mins"),
        "delivery_time_exceptions": config.BOT_DATA.get("delivery_time_exceptions", {}),
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
        "delivery_time":            config.BOT_DATA.get("delivery_time", "35-45 mins"),
        "delivery_time_exceptions": config.BOT_DATA.get("delivery_time_exceptions", {}),
    })


# ============================================================
# DELIVERY CHARGES API
# ============================================================

@app.get("/api/delivery-charges")
async def get_delivery_charges():
    load_data_realtime()
    return {"delivery_charges": config.BOT_DATA.get("delivery_charges", {})}


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

    existing   = config.BOT_DATA.get("delivery_charges", {})
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
        "delivery_charges": config.BOT_DATA.get("delivery_charges", {}),
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
            "products":  config.PRODUCTS_DATA,
            "orders":    orders,
            "analytics": analytics,
            "config": {
                "faq":                 config.BOT_DATA.get("faq", {}),
                "initial_message":     config.BOT_DATA.get("initial_message", {}),
                "discount_message":    config.BOT_DATA.get("discount_message", {}),
                "supported_languages": config.BOT_DATA.get("supported_languages", ["en", "ur", "de"]),
                "smart_suggestions":   config.BOT_DATA.get("smart_suggestions", {}),
                "delivery_charges":    config.BOT_DATA.get("delivery_charges", {}),
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
    logger.info("🚀 Restaurant Bot v14.7 started!")
    logger.info(f"   Products loaded    : {len(config.PRODUCTS_DATA)}")
    logger.info(f"   Keyword index size : {len(config.PRODUCT_KEYWORD_INDEX)}")
    logger.info(f"   FAQ keys           : {list(config.BOT_DATA.get('faq', {}).keys())}")
    logger.info(f"   Delivery time      : {get_delivery_time()}")
    logger.info(f"   Delivery charges   : {config.BOT_DATA.get('delivery_charges', {})}")
    logger.info(f"   WhatsApp connected : {'✅' if config.WHATSAPP_TOKEN else '❌'}")
    logger.info(f"   MongoDB connected  : {'✅' if products_col is not None else '❌'}")
    logger.info(f"   AI fallback        : {'✅' if config.ANTHROPIC_API_KEY else '⚠️  Static fallback active'}")
