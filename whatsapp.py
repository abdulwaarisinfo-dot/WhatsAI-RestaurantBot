import httpx

import config
from products import (
    _recalc_cart, _build_cart_summary, _match_variant,
    build_cart_item, _parse_multi_size_from_text,
    _extract_extras_from_text,
)

logger = logging.getLogger("RestaurantBot.v14.6")

# ============================================================
# WHATSAPP API HELPERS
# ============================================================

async def send_whatsapp_text(to: str, body: str):
    if not config.WHATSAPP_TOKEN or not config.WHATSAPP_PHONE_ID:
        logger.warning("WhatsApp credentials not configured — skipping send.")
        return
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
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
            r = await c.post(config.WHATSAPP_API_URL, json=payload, headers=headers)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"WhatsApp send failed: {e}")


async def send_whatsapp_list(to: str, header: str, items: List[Dict[str, Any]], lang: str = "en"):
    from products import _build_text_menu
    menu_text = _build_text_menu(items, lang)
    await send_whatsapp_text(to, menu_text)


async def send_whatsapp_buttons(to: str, body: str, buttons: List[str]):
    """Sends WhatsApp interactive button message. Max 3 buttons."""
    if not config.WHATSAPP_TOKEN or not config.WHATSAPP_PHONE_ID:
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
    headers_h = {
        "Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
        "Content-Type":  "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(config.WHATSAPP_API_URL, json=payload, headers=headers_h)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"WhatsApp buttons send failed: {e}")


# ============================================================
# SMART FALLBACK (Claude AI)
# ============================================================

async def _smart_fallback(from_number: str, user_message: str, lang: str) -> str:
    if not config.ANTHROPIC_API_KEY:
        return _static_fallback(lang)

    product_names = [p.get("title", "") for p in config.PRODUCTS_DATA[:20]]
    product_list  = ", ".join(product_names) if product_names else "various delicious dishes"

    system_prompt = (
        f"You are Zara, a warm, friendly, and professional WhatsApp restaurant assistant. "
        f"You speak like a real human restaurant staff member — conversational, caring, and enthusiastic about food. "
        f"The restaurant serves: {product_list}. "
        f"Respond in {'Urdu' if lang == 'ur' else 'German' if lang == 'de' else 'English'}, "
        f"keeping replies under 3 sentences. "
        f"Use relevant food emojis naturally. "
        f"If someone asks about a dish, describe it with genuine enthusiasm and gently guide them to order. "
        f"If confused, apologise warmly and redirect to food ordering. "
        f"Never sound robotic or use formal language. "
        f"If completely unrelated to food/restaurant, say warmly that you specialise in food only."
    )

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":          config.ANTHROPIC_API_KEY,
                    "anthropic-version":  "2023-06-01",
                    "content-type":       "application/json",
                },
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "system":     system_prompt,
                    "messages":   [{"role": "user", "content": user_message}],
                },
            )
            data    = resp.json()
            ai_text = data.get("content", [{}])[0].get("text", "").strip()
            if ai_text:
                return ai_text
    except Exception as e:
        logger.warning(f"AI fallback failed: {e}")

    return _static_fallback(lang)


def _static_fallback(lang: str) -> str:
    """Warm, human-friendly static fallback messages."""
    fallback = {
        "en": (
            "Hmm, I'm not quite sure I caught that — sorry about that! 😅\n\n"
            "Here's how I can help you:\n\n"
            "🍽️ *Show menu* — browse everything we've got\n"
            "📦 *Order [dish]* — e.g. _'Zinger Burger'_ or _'1kg Karahi'_\n"
            "💰 *All prices* — see our full price list\n"
            "📍 *Order status* — track your latest order\n\n"
            "Just tell me what you're craving and I'll sort it out! 😊"
        ),
        "ur": (
            "معذرت، سمجھ نہیں آیا 😅 میں ان چیزوں میں مدد کر سکتا ہوں:\n\n"
            "🍽️ *مینو دکھائیں* — سب آئٹم دیکھیں\n"
            "📦 *آرڈر [ڈش]* — جیسے _'بریانی'_ یا _'1kg کڑاہی'_\n"
            "💰 *تمام قیمتیں* — قیمت کی فہرست\n"
            "📍 *آرڈر اسٹیٹس* — ٹریکنگ\n\n"
            "بس بتائیں، میں مدد کروں گا! 😊"
        ),
        "de": (
            "Das habe ich leider nicht verstanden — entschuldigung! 😅\n\n"
            "So kann ich helfen:\n\n"
            "🍽️ *Menü anzeigen* — alle Gerichte\n"
            "📦 *[Gericht] bestellen* — z.B. _'Zinger Burger'_\n"
            "💰 *Alle Preise* — komplette Preisliste\n"
            "📍 *Bestellstatus* — verfolgen\n\n"
            "Einfach eingeben, was Sie möchten! 😊"
        ),
    }
    return fallback.get(lang, fallback["en"])


# ============================================================
# BOT FLOW HELPERS
# ============================================================

async def _ask_size(to: str, product: Dict, lang: str):
    variants  = product.get("variants", [])
    size_list = "\n".join(f"  ▸ *{v['size']}*  —  PKR {v['price']}" for v in variants)
    name      = product.get("title", "Item").strip().title()
    msgs = {
        "en": (
            f"📏 Great choice! *{name}* comes in these sizes:\n\n"
            f"{size_list}\n\n"
            f"Which size works for you? Just type it — e.g. _'Large'_ or _'1kg'_ 😊"
        ),
        "ur": (
            f"📏 بہترین انتخاب! *{name}* کے سائز:\n\n"
            f"{size_list}\n\n"
            f"کون سا سائز چاہیے؟ لکھیں!"
        ),
        "de": (
            f"📏 Tolle Wahl! *{name}* gibt es in diesen Größen:\n\n"
            f"{size_list}\n\n"
            f"Welche Größe darf es sein?"
        ),
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))


async def _ask_spice(to: str, product: Dict, lang: str) -> bool:
    spice_levels = product.get("spice_levels", [])
    if not spice_levels:
        return False
    options = "  •  ".join(s.strip().title() for s in spice_levels)
    name    = product.get("title", "Item").strip().title()
    msgs = {
        "en": (
            f"🌶️ Love it! Now, how spicy would you like your *{name}*?\n\n"
            f"  {options}\n\n"
            f"Pick your heat level! 🔥"
        ),
        "ur": (
            f"🌶️ *{name}* کے لیے مسالے کی سطح بتائیں:\n"
            f"  {options}"
        ),
        "de": (
            f"🌶️ Schärfegrad für *{name}*:\n"
            f"  {options}"
        ),
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _ask_extras(to: str, product: Dict, lang: str) -> bool:
    extras = product.get("extras", [])
    if not extras:
        return False
    extras_list = "\n".join(f"  ▸ *{e['name'].strip().title()}*  +PKR {e['price']}" for e in extras)
    name        = product.get("title", "Item").strip().title()
    msgs = {
        "en": (
            f"➕ Almost done! Would you like to add anything to your *{name}*?\n\n"
            f"{extras_list}\n\n"
            f"_(Type the name(s) to add, or just say *no* to skip)_"
        ),
        "ur": (
            f"➕ *{name}* کے ساتھ کچھ اضافی چاہیے؟\n"
            f"{extras_list}\n\n"
            f"(نام لکھیں یا *no* لکھیں)"
        ),
        "de": (
            f"➕ Extras für *{name}*?\n"
            f"{extras_list}\n\n"
            f"(Namen oder *nein* eingeben)"
        ),
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
    return True


async def _ask_multi_spice(to: str, items_needing_spice: List[Dict], product: Dict, lang: str):
    spice_levels = product.get("spice_levels", [])
    options      = "  •  ".join(s.strip().title() for s in spice_levels)
    name         = product.get("title", "Item").strip().title()

    lines = []
    for item in items_needing_spice:
        mv        = item["matched_variant"]
        size      = mv.get("size", "")
        qty       = item.get("qty", 1)
        qty_label = f" ×{qty}" if qty > 1 else ""
        lines.append(f"  ▸ *{size}*{qty_label}")

    body = "\n".join(lines)
    msgs = {
        "en": (
            f"🌶️ Almost ready! What spice level for your *{name}*?\n\n"
            f"{body}\n\n"
            f"Options: {options}\n\n"
            f"_(e.g. 'Small Spicy, Medium Mild' — or just 'Spicy' to apply to all)_"
        ),
        "ur": (
            f"🌶️ *{name}* کے لیے مسالے کی سطح:\n"
            f"{body}\n\n"
            f"آپشن: {options}\n"
            f"(جیسے: 'Small Spicy, Medium Mild' یا سب کے لیے 'Spicy')"
        ),
        "de": (
            f"🌶️ Schärfegrad für *{name}*:\n"
            f"{body}\n\n"
            f"Optionen: {options}\n"
            f"(z.B. 'Small Spicy, Medium Mild' oder 'Spicy' für alle)"
        ),
    }
    await send_whatsapp_text(to, msgs.get(lang, msgs["en"]))
