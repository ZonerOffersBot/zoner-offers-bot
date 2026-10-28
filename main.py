"""Stable production entry point for Zoner Offers Bot."""
# ZONER_BROADCAST_FIX_2026_10_06
import asyncio
import bot
import runner
import instamart_calculator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import CommandHandler, MessageHandler, filters


def _patched_main_menu(user_id=None):
    notify = "🔔 Notifications ON" if user_id is not None and bot.subscriber_enabled(user_id) else "🔕 Notifications OFF"
    items = [
        ("🛍️ Latest Deals", "offers"),
        ("📚 Published Links", "published_links"),
        ("🏷️ Categories", "categories"),
        ("💰 Price Filter", "price_filter"),
        ("🧮 Instamart Calculator", "instamart_calc"),
        (notify, "notifications"),
        ("🤖 AI Deal Hunter", "ai_info"),
        ("🆘 Help", "help"),
    ]
    try:
        max_per_row = max(1, min(3, int(bot.get_setting_sync("menu_buttons_per_row", 2))))
    except (TypeError, ValueError):
        max_per_row = 2
    rows = []
    for i in range(0, len(items), max_per_row):
        rows.append([InlineKeyboardButton(label, callback_data=cb) for label, cb in items[i:i + max_per_row]])
    return InlineKeyboardMarkup(rows)


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
    if data == "instamart_calc":
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
        context.user_data["instamart_calc"] = {"step": "cart", "values": {}}
        await query.edit_message_text(
            "🧮 <b>Instamart ₹300 Discount Calculator</b>\n\n"
            "Step 1/4: Enter your cart total in rupees (example: <code>599</code>).\n\n"
            "<i>This estimates only; it does not verify or apply a Swiggy offer.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel / Main Menu", callback_data="back")]]),
        )
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


async def instamart_calculator_input(update, context):
    """Handle calculator replies only while this user is in the calculator flow."""
    state = context.user_data.get("instamart_calc")
    if not state or not update.message or not update.message.text:
        return

    step = state.get("step")
    values = state.setdefault("values", {})
    text = update.message.text.strip()

    prompts = {
        "cart": "Step 1/4: Enter cart total in rupees (example: 599).",
        "minimum": "Step 2/4: Enter the offer's minimum order value in rupees.",
        "maximum": "Step 3/4: Enter the offer's maximum discount in rupees (up to 300 for this calculator).",
    }

    if step in ("cart", "minimum", "maximum"):
        try:
            amount = instamart_calculator.parse_rupees(text)
        except (TypeError, ValueError):
            await update.message.reply_text("Please enter a valid non-negative amount, such as 599 or 499.50.")
            await update.message.reply_text(prompts[step])
            return

        if step == "cart":
            values["cart_total"] = amount
            state["step"] = "minimum"
        elif step == "minimum":
            values["minimum_order"] = amount
            state["step"] = "maximum"
        else:
            values["maximum_discount"] = amount
            state["step"] = "eligible"

        if state["step"] == "eligible":
            await update.message.reply_text(
                "Step 4/4: Does the official Swiggy app show this offer as eligible for your cart? Reply <code>yes</code> or <code>no</code>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(prompts[state["step"]])
        return

    if step == "eligible":
        answer = text.lower()
        if answer not in ("y", "yes", "n", "no"):
            await update.message.reply_text("Please reply only yes or no. Does the official app show the offer as eligible?")
            return

        result = instamart_calculator.calculate(
            cart_total=values["cart_total"],
            minimum_order=values["minimum_order"],
            maximum_discount=values["maximum_discount"],
            offer_eligible=answer in ("y", "yes"),
        )
        lines = [
            "🧮 <b>INSTAMART CALCULATION</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🛒 Cart total: <b>₹{result['cart_total']:.2f}</b>",
            f"📌 Minimum order: <b>₹{result['minimum_order']:.2f}</b>",
            f"🏷️ Requested discount: <b>₹{result['requested_discount']:.2f}</b>",
            f"🔒 Maximum discount entered: <b>₹{result['maximum_discount']:.2f}</b>",
            f"💸 Calculated discount: <b>₹{result['calculated_discount']:.2f}</b>",
            f"🧾 Estimated payable: <b>₹{result['calculated_payable']:.2f}</b>",
            "",
            "Local calculation: " + ("<b>PASS</b>" if result["eligible_in_local_test"] else "<b>FAIL</b>"),
        ]
        if result["validation_errors"]:
            lines.append("")
            lines.extend("• " + __import__("html").escape(error) for error in result["validation_errors"])
        lines.extend([
            "",
            "⚠️ <i>Estimate only. Zoner does not connect to Swiggy's offer system. Verify the coupon and final payable amount in the official app checkout.</i>",
        ])
        context.user_data.pop("instamart_calc", None)
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧮 Calculate Again", callback_data="instamart_calc")],
                                               [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")]]),
        )


_original_build_app = runner.build_app


def build_app():
    app = _original_build_app()
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, instamart_calculator_input), group=0)
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
