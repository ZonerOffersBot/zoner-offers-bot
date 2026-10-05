import asyncio
import logging
import bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters

log = logging.getLogger("zoner")

async def fast_membership_status(telegram_bot, user_id):
    # Persist successful verification so a user is verified only once.
    if bot.is_user_verified(user_id):
        return True
    now = asyncio.get_running_loop().time()
    cached = bot._membership_cache.get(user_id)
    if cached and now - cached[0] < bot.MEMBERSHIP_CACHE_SECONDS:
        return cached[1]
    required = [("@zoneroffers", "channel 1"), ("@offerleloturant", "channel 2")]
    if bot.GROUP_ID:
        required.append((bot.GROUP_ID, "group"))

    async def check(chat_id, label):
        try:
            member = await asyncio.wait_for(telegram_bot.get_chat_member(chat_id, user_id), timeout=5.0)
            # Telegram can report restricted users with is_member=True.
            if member.status in {"member", "administrator", "creator"}:
                return True
            if member.status == "restricted" and getattr(member, "is_member", False):
                return True
            log.warning("Membership rejected for %s: status=%s user=%s", label, member.status, user_id)
            return False
        except Exception as exc:
            log.warning("Fast membership check failed for %s (bot must be admin to reliably check members): %s", label, exc)
            return False

    checks = await asyncio.gather(*(check(chat_id, label) for chat_id, label in required))
    result = bool(checks) and all(checks)
    bot._membership_cache[user_id] = (now, result)
    if result:
        bot.mark_user_verified(user_id)
    return result

bot.membership_status = fast_membership_status

async def polling_error_handler(update, context):
    # Never let a Telegram polling/handler exception silently kill the bot.
    exc = context.error
    log.error("Telegram runtime error: %r", exc, exc_info=exc)


async def managed_post_init(app):
    # Ensure stale webhook configuration cannot block long-polling updates.
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
        me = await app.bot.get_me()
        log.info("✅ Telegram connection OK: @%s", me.username or me.id)
    except Exception:
        log.exception("❌ Telegram startup check failed")
        raise

    task = asyncio.create_task(bot.auto_scan_loop(app), name="zoner-auto-scan")
    app.bot_data["auto_scan_task"] = task
    log.info("🤖 Autonomous deal scanner started (120s cycle).")

async def managed_post_stop(app):
    task = app.bot_data.pop("auto_scan_task", None)
    if task and not task.done():
        log.info("🛑 Stopping autonomous deal scanner...")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    log.info("✅ Autonomous deal scanner stopped cleanly.")

def build_app():
    app = (
        Application.builder()
        .token(bot.TOKEN)
        .connection_pool_size(32)
        .pool_timeout(3)
        .get_updates_connection_pool_size(8)
        .get_updates_pool_timeout(2)
        .get_updates_connect_timeout(5)
        .get_updates_read_timeout(15)
        .get_updates_write_timeout(5)
        .post_init(managed_post_init)
        .post_stop(managed_post_stop)
        .build()
    )
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("addoffer", bot.admin_start),
            CallbackQueryHandler(bot.admin_add_button, pattern=r"^admin_add$")
        ],
        states={
            bot.C_TITLE:[MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_title)],
            bot.C_PRICE:[MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_price)],
            bot.C_OLD:[MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_old)],
            bot.C_CATEGORY:[MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_category)],
            bot.C_URL:[MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_url)],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel)],
    )
    app.add_error_handler(polling_error_handler)
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help_command))
    app.add_handler(CommandHandler("admin", bot.admin_help))
    app.add_handler(CommandHandler("testchannels", bot.test_channels))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(bot.button_handler))
    return app

def run():
    if not bot.TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    bot.init_db()
    bot.Thread(target=bot.run_health_server, daemon=True).start()
    app = build_app()
    log.info("🔥 Zoner Offers AI fast runner is starting.")
    app.run_polling(poll_interval=0.5, timeout=20, bootstrap_retries=-1, drop_pending_updates=False)

if __name__ == "__main__":
    run()
