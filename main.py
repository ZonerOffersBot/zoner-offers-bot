"""Stable production entry point for Zoner Offers Bot."""
# ZONER_BROADCAST_FIX_2026_10_06
import asyncio
import bot
import runner
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import CommandHandler, MessageHandler, filters


def _patched_main_menu(user_id=None):
    # User-facing menu intentionally shows only the clearly labelled demo store.
    # Existing handlers/admin features remain registered and unchanged.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Swiggy Instamart Demo", callback_data="instamart_order")],
    ])

bot.main_menu = _patched_main_menu


async def _published_links(update, context):
    query = update.callback_query
    rows = bot.get_offers(limit=100)
    if not rows:
        await query.edit_message_text(
            "📚 <b>Published Links</b>\n\nNo successfully published links are stored yet.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="back")]]),
        )
        return
    buttons = []
    for row in rows:
        title = row["title"][:48] + ("…" if len(row["title"]) > 48 else "")
        buttons.append([InlineKeyboardButton(f"🔗 {title}", callback_data=f"offer_{row['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="back")])
    await query.edit_message_text(
        "📚 <b>Published Links</b>\n\nEvery successfully published link is stored in the publication ledger and remains available here and under its saved category.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


_original_button_impl = bot._button_handler_impl


async def _patched_button_impl(update, context):
    query = update.callback_query
    data = query.data
    if data == "instamart_order":
        user_id = query.from_user.id
        if not bot.is_user_verified(user_id):
            verified = await bot.membership_status(context.bot, user_id)
            if not verified:
                await query.edit_message_text(bot.join_gate_text(), parse_mode=ParseMode.HTML, reply_markup=bot.join_gate_markup())
                return
            bot.mark_user_verified(user_id)
        context.user_data.setdefault("instamart_order", {"items": []})
        await query.edit_message_text(
            "🛒 <b>INSTAMART ORDER ASSISTANT</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n"
            "🎮 <b>FUN DEMO MODE — NOT A REAL SWIGGY ORDER</b>\\n\\n"
            "Browse sample products, add them to a simulated cart, calculate an example discount, and preview a pretend checkout. No order or payment is sent.\\n\\n"
            "Prices and offers below are fictional demo values. For real shopping, open official Instamart.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ Browse Demo Products", callback_data="instamart_products")],
                [InlineKeyboardButton("🧮 Instamart Calculator (Demo)", callback_data="instamart_calc")],
                [InlineKeyboardButton("🛒 View Demo Cart", callback_data="instamart_view")],
                [InlineKeyboardButton("🧹 Clear Cart", callback_data="instamart_clear")],
                [InlineKeyboardButton("🔎 Open Official Instamart", url="https://www.swiggy.com/instamart")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )
        return
    if data == "instamart_products":
        context.user_data.setdefault("instamart_order", {"items": [], "cart": {}})
        products = [("milk", "🥛 Milk 1L — ₹68"), ("bread", "🍞 Bread — ₹40"), ("eggs", "🥚 Eggs (6) — ₹55"), ("bananas", "🍌 Bananas — ₹45"), ("chips", "🍟 Chips — ₹30"), ("juice", "🧃 Juice — ₹90")]
        rows = [[InlineKeyboardButton(label, callback_data=f"demo_add_{key}")] for key, label in products]
        rows += [[InlineKeyboardButton("🛒 View Demo Cart", callback_data="instamart_view")],
                 [InlineKeyboardButton("🧮 Discount Calculator", callback_data="instamart_calc")],
                 [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")]]
        await query.edit_message_text("🛍️ <b>DEMO STORE CATALOG</b>\\n<i>Sample products/prices only — not live Swiggy inventory.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith("demo_add_"):
        catalog = {"milk": ("Milk 1L", 68), "bread": ("Bread", 40), "eggs": ("Eggs (6)", 55), "bananas": ("Bananas", 45), "chips": ("Chips", 30), "juice": ("Juice", 90)}
        key = data[len("demo_add_"):]
        if key in catalog:
            state = context.user_data.setdefault("instamart_order", {"items": [], "cart": {}})
            state.setdefault("cart", {})
            name, price = catalog[key]
            item = state["cart"].setdefault(key, {"name": name, "price": price, "qty": 0})
            item["qty"] += 1
            await query.answer(f"Demo cart: {name} × {item['qty']}")
            await query.edit_message_text(f"✅ Added to <b>DEMO CART</b>: {name} × {item['qty']}\\nPrice: ₹{price} each\\n\\n<i>Simulation only; nothing added to Swiggy.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add more products", callback_data="instamart_products")],
                [InlineKeyboardButton("🛒 View Demo Cart", callback_data="instamart_view")],
                [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")],
            ]))
        return
    if data == "instamart_calc":
        context.user_data.setdefault("instamart_order", {"items": [], "cart": {}})["step"] = "calc_total"
        await query.edit_message_text("🧮 <b>INSTAMART DISCOUNT CALCULATOR — DEMO</b>\\n\\nSend your cart total in rupees (example: <code>649</code>).\\nDemo estimate: 10% off up to ₹100 on ₹499+; extra ₹50 off on ₹999+.\\n<i>Illustrative rules only, not verified or redeemable Swiggy offers.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")]]))
        return
    if data == "instamart_checkout":
        cart = context.user_data.get("instamart_order", {}).get("cart", {})
        subtotal = sum(x["price"] * x["qty"] for x in cart.values())
        if not cart:
            await query.answer("Your demo cart is empty.", show_alert=True)
            return
        discount = min(100, int(subtotal * 0.10)) if subtotal >= 499 else 0
        if subtotal >= 999:
            discount += 50
        total = max(0, subtotal - discount)
        await query.edit_message_text("🎉 <b>DEMO CHECKOUT PREVIEW</b>\\n━━━━━━━━━━━━━━━━━━\\n🧾 Subtotal: ₹" + str(subtotal) + "\\n🎟️ Sample discount: −₹" + str(discount) + "\\n<b>Demo total: ₹" + str(total) + "</b>\\n\\n✅ Pretend checkout complete! <b>No real order was placed and no payment was taken.</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ Continue Demo Shopping", callback_data="instamart_products")],
            [InlineKeyboardButton("🔎 Open Official Instamart", url="https://www.swiggy.com/instamart")],
            [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")],
        ]))
        return
    if data == "instamart_add":
        context.user_data.setdefault("instamart_order", {"items": []})
        context.user_data["instamart_order"]["step"] = "add_items"
        await query.edit_message_text(
            "➕ <b>Add products</b>\\n\\nSend product names and quantities, one per line. Example:\\n<code>Milk 1 litre x 2\\nBread x 1</code>\\n\\nThis is a checklist, not a live Swiggy cart.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Order menu", callback_data="instamart_order")]]),
        )
        return
    if data == "instamart_view":
        state = context.user_data.get("instamart_order", {})
        cart = state.get("cart", {})
        listing = "\\n".join(f"• {item['name']} × {item['qty']} — ₹{item['price'] * item['qty']}" for item in cart.values())
        subtotal = sum(item["price"] * item["qty"] for item in cart.values())
        if not listing:
            listing = "Your demo cart is empty."
        await query.edit_message_text("🛒 <b>MY DEMO CART</b>\\n\\n" + listing + "\\n\\n<b>Demo subtotal: ₹" + str(subtotal) + "</b>\\n<i>Sample prices only. This cart is not connected to Swiggy.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ Browse products", callback_data="instamart_products")],
            [InlineKeyboardButton("🧮 Discount Calculator", callback_data="instamart_calc")],
            [InlineKeyboardButton("🎉 Simulate Checkout", callback_data="instamart_checkout")],
            [InlineKeyboardButton("🧹 Clear Cart", callback_data="instamart_clear")],
            [InlineKeyboardButton("⬅️ Order menu", callback_data="instamart_order")],
        ]))
        return
    if data == "instamart_clear":
        context.user_data["instamart_order"] = {"items": [], "cart": {}}
        await query.edit_message_text("🧹 Demo cart cleared.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Order menu", callback_data="instamart_order")]]))
        return
    if data == "published_links":
        user_id = query.from_user.id
        if not bot.is_user_verified(user_id):
            verified = await bot.membership_status(context.bot, user_id)
            if not verified:
                await query.edit_message_text(
                    bot.join_gate_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=bot.join_gate_markup(),
                )
                return
            bot.mark_user_verified(user_id)
        await _published_links(update, context)
        return
    if data == "admin_broadcast":
        if not bot.is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return
        con = bot.db()
        try:
            user_count = con.execute("SELECT COUNT(*) AS n FROM subscribers WHERE enabled=1").fetchone()["n"]
        finally:
            con.close()
        await query.edit_message_text(
            "📝 <b>BROADCAST CENTER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Active recipients: <b>{user_count}</b>\n\n"
            "📢 <b>Send:</b> <code>/broadcast Your message</code>\n\n"
            "Example:\n<code>/broadcast 🔥 New deals are live!</code>\n\n"
            "Only users with Notifications ON receive it.\n"
            "One failed user will NOT stop the remaining broadcast.",
            parse_mode=ParseMode.HTML,
            reply_markup=bot.admin_menu(),
        )
        return
    return await _original_button_impl(update, context)


bot._button_handler_impl = _patched_button_impl


async def instamart_order_input(update, context):
    """Collect checklist text only while the user is in the add-items flow."""
    state = context.user_data.get("instamart_order")
    if not state or state.get("step") not in ("add_items", "calc_total") or not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text.startswith("/"):
        return
    if state.get("step") == "calc_total":
        try:
            subtotal = float(text.replace("₹", "").replace(",", "").strip())
            if subtotal < 0 or subtotal > 1000000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a valid cart total, e.g. 649.")
            return
        discount = min(100, int(subtotal * 0.10)) if subtotal >= 499 else 0
        if subtotal >= 999:
            discount += 50
        state["step"] = ""
        await update.message.reply_text("🧮 INSTAMART CALCULATOR — DEMO RESULT\\n━━━━━━━━━━━━━━━━━━\\n" + f"Cart total: ₹{subtotal:.0f}\\nSample discount: ₹{discount}\\nEstimated demo total: ₹{max(0, subtotal-discount):.0f}\\n\\n⚠️ Illustrative rules only; not a live or redeemable Swiggy offer.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ Browse Demo Products", callback_data="instamart_products")],
            [InlineKeyboardButton("🧮 Calculate Again", callback_data="instamart_calc")],
            [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")],
        ]))
        return
    lines = [line.strip(" •-\\t") for line in text.splitlines() if line.strip(" •-\\t")]
    if not lines:
        await update.message.reply_text("Send product names and quantities, one per line.")
        return
    state.setdefault("items", []).extend(lines[:30])
    state["step"] = ""
    await update.message.reply_text(
        "✅ Added to your checklist.\\n\\n" + "\\n".join("• " + line for line in lines[:30]) +
        "\\n\\nThis is not synced to Swiggy. Open the official Instamart page to place the real order.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 View checklist", callback_data="instamart_view")],
            [InlineKeyboardButton("🚀 Open Instamart", url="https://www.swiggy.com/instamart")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
        ]),
    )


_original_build_app = runner.build_app


def build_app():
    app = _original_build_app()
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, instamart_order_input), group=0)
    return app


async def broadcast_command(update, context):
    if not bot.is_admin(update):
        return

    args = getattr(context, "args", []) or []
    message = " ".join(args).strip()
    if not message:
        await update.message.reply_text(
            "📝 <b>Broadcast Center</b>\n\n"
            "Use:\n<code>/broadcast Your message here</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=bot.admin_menu(),
        )
        return

    con = bot.db()
    try:
        users = con.execute("SELECT user_id FROM subscribers WHERE enabled=1").fetchall()
    finally:
        con.close()

    sent = 0
    failed = 0
    retry_count = 0

    await update.message.reply_text(
        f"📢 <b>Broadcast started</b>\n\n👥 Recipients: <b>{len(users)}</b>\n⏳ Sending safely...",
        parse_mode=ParseMode.HTML,
    )

    for user in users:
        chat_id = user["user_id"]
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            sent += 1
        except RetryAfter as exc:
            retry_count += 1
            wait_for = max(1.0, float(getattr(exc, "retry_after", 1)))
            await asyncio.sleep(wait_for)
            try:
                await context.bot.send_message(chat_id=chat_id, text=message)
                sent += 1
            except Exception as retry_exc:
                failed += 1
                bot.log.warning("Broadcast retry failed for %s: %s", chat_id, retry_exc)
        except Exception as exc:
            failed += 1
            bot.log.warning("Broadcast failed for %s: %s", chat_id, exc)

        await asyncio.sleep(0.05)

    await update.message.reply_text(
        "✅ <b>BROADCAST COMPLETE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📨 Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
        f"🔁 Rate-limit retries: <b>{retry_count}</b>\n"
        f"👥 Total targeted: <b>{len(users)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=bot.admin_menu(),
    )


runner.build_app = build_app


def run():
    return runner.run()


init_db = bot.init_db


if __name__ == "__main__":
    run()
