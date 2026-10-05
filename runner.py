import asyncio
import logging
import bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters

log = logging.getLogger("zoner")

async def fast_membership_status(telegram_bot, user_id):
    now = asyncio.get_running_loop().time()
    cached = bot._membership_cache.get(user_id)
    if cached and now - cached[0] < bot.MEMBERSHIP_CACHE_SECONDS:
        return cached[1]
    required = [("@zoneroffers", "channel 1"), ("@offerleloturant", "channel 2")]
    if bot.GROUP_ID:
        required.append((bot.GROUP_ID, "group"))

    async def check(chat_id, label):
        try:
            member = await asyncio.wait_for(telegram_bot.get_chat_member(chat_id, user_id), timeout=2.0)
            return member.status in {"member", "administrator", "creator"}
        except Exception as exc:
            log.warning("Fast membership check failed for %s: %s", label, exc)
            return False

    checks = await asyncio.gather(*(check(chat_id, label) for chat_id, label in required))
    result = bool(checks) and all(checks)
    bot._membership_cache[user_id] = (now, result)
    return result

bot.membership_status = fast_membership_status

async def managed_post_init(app):
    task = asyncio.create_task(bot.auto_scan_loop(app), name="zoner-auto-scan")
    app.bot_data["auto_scan_task"] = task
    log.info("🤖 Autonomous deal scanner started.")

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
    app.run_polling(poll_interval=0.0, timeout=10, bootstrap_retries=-1)

if __name__ == "__main__":
    run()
