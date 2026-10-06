"""Stable production entry point for Zoner Offers Bot."""
# ZONER_BROADCAST_FIX_2026_10_06
import asyncio
import bot
import runner
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter
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
    data = update.callback_query.data
    if data == "published_links":
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
        con = bot.db()
        try:
            user_count = con.execute("SELECT COUNT(*) AS n FROM subscribers WHERE enabled=1").fetchone()["n"]
        finally:
            con.close()
        await update.callback_query.edit_message_text(
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

        # Small pacing gap prevents a large subscriber list from hitting Telegram's rate limit.
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
