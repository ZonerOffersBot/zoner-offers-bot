"""Deal scoring and Telegram post formatting helpers.

This module is standalone and compatible with the live bot. The live publishing
engine remains in bot.py, so changing these helpers does not interrupt scanning
or channel publishing.
"""

SOURCE_SCORES = {
    "Amazon": 15, "Flipkart": 14, "Myntra": 12, "Ajio": 11,
    "Nykaa": 10, "Meesho": 8, "Croma": 13, "JioMart": 12,
    "Blinkit": 11, "BigBasket": 11, "Swiggy": 10,
}


def _number(value):
    try:
        if value is None:
            return 0.0
        return float(str(value).replace("₹", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def calculate_deal_score(product):
    """Calculate a 0-100 deal score from discount, price, source and stock."""
    price = _number(product.get("price", 0))
    mrp = _number(product.get("mrp", product.get("old_price", 0)))

    if mrp <= 0 or price <= 0:
        # Still give source/stock-aware products a neutral score.
        return 50

    discount_pct = max(0.0, ((mrp - price) / mrp) * 100.0)
    discount_score = min(discount_pct * (40.0 / 30.0), 40.0)

    # Preserve the requested simple price-quality curve.
    price_score = max(0.0, min(30.0, 30.0 - (price / 10000.0) * 30.0))

    source = str(product.get("source", "")).strip()
    source_score = SOURCE_SCORES.get(source, 5)

    availability_score = 15 if product.get("in_stock", True) else 0

    total = discount_score + price_score + source_score + availability_score
    return round(max(0.0, min(total, 100.0)))


def _discount_percent(product):
    price = _number(product.get("price", 0))
    mrp = _number(product.get("mrp", product.get("old_price", 0)))
    if price <= 0 or mrp <= 0:
        return 0
    return round(max(0.0, ((mrp - price) / mrp) * 100))


def format_deal_post(product):
    """Return (caption, image_url) in Telegram MarkdownV2-safe-ish format.

    The live bot currently renders its own HTML post; this helper is intended
    for callers that want the richer deal-score presentation.
    """
    score = calculate_deal_score(product)
    discount = _discount_percent(product)

    if score >= 80:
        score_emoji, score_label = "🔥🔥🔥", "MEGA DEAL"
    elif score >= 60:
        score_emoji, score_label = "🔥🔥", "HOT DEAL"
    elif score >= 40:
        score_emoji, score_label = "🔥", "GOOD DEAL"
    else:
        score_emoji, score_label = "⚡", "AVERAGE"

    def safe(value, default="N/A"):
        return str(value if value not in (None, "") else default).replace("*", "\*").replace("_", "\_")

    title = safe(product.get("title"), "Deal")
    brand = safe(product.get("brand") or product.get("source"), "Unknown")
    source = safe(product.get("source"), "Unknown")
    mrp = safe(product.get("mrp") or product.get("old_price"), "N/A")
    price = safe(product.get("price"), "N/A")
    url = str(product.get("url") or "").replace(")", "%29").replace("(", "%28")

    caption = (
        f"🏷️ *{score_label}* {score_emoji}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *{title[:80]}*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏪 *Brand/Source:* {brand}\n"
        f"🛒 *Platform:* {source}\n"
        f"💰 *MRP:* ₹{mrp}\n"
        f"💵 *Deal Price:* ₹{price}\n"
        f"🎯 *Discount:* {discount}% OFF\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⭐ *Deal Score:* {score}/100 {score_emoji}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [🛒 BUY NOW]({url})"
    )
    return caption, product.get("image")


# Backward-compatible live-engine helpers.
def is_deal_candidate(*args, **kwargs):
    import bot
    return bot.is_deal_candidate(*args, **kwargs)


def normalize_candidate(*args, **kwargs):
    import bot
    return bot.normalize_candidate(*args, **kwargs)


def score_deal(candidate):
    """Use the live engine's score when available; otherwise use this scorer."""
    try:
        import bot
        fn = getattr(bot, "score_candidate", None)
        if fn:
            return fn(candidate)
    except Exception:
        pass
    return calculate_deal_score(candidate if isinstance(candidate, dict) else {})


def fingerprint(title, url):
    import bot
    return bot.fingerprint(title, url)
