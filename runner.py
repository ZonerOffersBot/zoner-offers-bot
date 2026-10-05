import asyncio
import logging
import bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters

log = logging.getLogger("zoner")

async def polling_error_handler(update, context):
    # Log the full exception, but also give the user a visible recovery path.
    # Handler exceptions must never make the bot appear unresponsive.
    exc = context.error
    log.error("Telegram runtime error: %r", exc, exc_info=exc)
    try:
        if update and update.callback_query:
            try:
                await update.callback_query.answer("Temporary error — please try again.", show_alert=True)
            except Exception:
                pass
        elif update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Temporary error. The bot is still running — please try again."
            )
    except Exception:
        log.exception("Could not send runtime-error recovery message")


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
    log.info("🤖 Autonomous deal scanner started (90s cycle).")

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
    app.add_handler(CommandHandler("admin", bot.admin_help))\n    app.add_handler(CommandHandler("setinterval", bot.set_interval_command))
    app.add_handler(CommandHandler("addmenu", bot.add_menu_item))
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
