"""Stable production entry point for Zoner Offers Bot."""
import bot
import runner
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CommandHandler


def _patched_main_menu(user_id=None):
    notify = "🔔 Notifications ON" if user_id is not None and bot.subscriber_enabled(user_id) else "🔕 Notifications OFF"
    items = [
        ("🛍️ Latest Deals", "offers"),
        ("📚 Published Links", "published_links"),
        ("🏷️ Categories", "categories"),
        ("💰 Price Filter", "price_filter"),
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
    rows = bot.get_offers(limit=30)
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
        "📚 <b>Published Links</b>\n\nEvery item below has a successful publication record and is saved in the bot database.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


_original_button_impl = bot._button_handler_impl


async def _patched_button_impl(update, context):
    data = update.callback_query.data
    if data == "published_links":
        await update.callback_query.answer()
        user_id = update.callback_query.from_user.id
        if not bot.is_user_verified(user_id):
            verified = await bot.membership_status(context.bot, user_id)
            if not verified:
                await update.callback_query.edit_message_text(
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
            await update.callback_query.answer("Not authorized.", show_alert=True)
            return
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "📝 <b>Broadcast</b>\n\nSend <code>/broadcast your message</code> to broadcast to all users who have notifications enabled.\n\nExample:\n<code>/broadcast 🔥 New deals are live!</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=bot.admin_menu(),
        )
        return
    return await _original_button_impl(update, context)


bot._button_handler_impl = _patched_button_impl


async def broadcast_command(update, context):
    if not bot.is_admin(update):
        return
    args = getattr(context, "args", []) or []
    message = " ".join(args).strip()
    if not message:
        await update.message.reply_text("❌ Use: <code>/broadcast your message</code>", parse_mode=ParseMode.HTML)
        return

    con = bot.db()
    users = con.execute("SELECT user_id FROM subscribers WHERE enabled=1").fetchall()
    con.close()
    sent = failed = 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user["user_id"], text=message)
            sent += 1
        except Exception as exc:
            failed += 1
            bot.log.warning("Broadcast failed for %s: %s", user["user_id"], exc)
    await update.message.reply_text(
        f"✅ <b>Broadcast complete</b>\n\n📨 Sent: <b>{sent}</b>\n⚠️ Failed: <b>{failed}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=bot.admin_menu(),
    )


_original_build_app = runner.build_app


def build_app():
    app = _original_build_app()
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    return app


runner.build_app = build_app


def run():
    return runner.run()


init_db = bot.init_db


if __name__ == "__main__":
    run()
