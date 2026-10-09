import asyncio
import logging
from threading import Thread

import bot
import source_copy
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

log = logging.getLogger("zoner")


async def polling_error_handler(update, context):
    # Log polling/runtime exceptions only. Never inject unsolicited Telegram
    # messages into the user's normal UI flow.
    exc = context.error
    log.error("Telegram runtime error: %r", exc, exc_info=exc)


async def managed_post_init(app):
    # Keep polling as the single Telegram update transport and prevent stale
    # webhook configuration from blocking updates.
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
        me = await app.bot.get_me()
        log.info("✅ Telegram connection OK: @%s", me.username or me.id)
    except Exception:
        log.exception("❌ Telegram startup check failed")
        raise

    # Legacy deal auto-publishing remains disabled; source-copy is independent.
    source_task = asyncio.create_task(source_copy.source_copy_loop(app), name="zoner-source-copy")
    app.bot_data["source_copy_task"] = source_task
    try:
        await bot.force_join_diagnostics(app.bot)
    except Exception:
        log.exception("⚠️ Force-join diagnostics failed; publisher remains active")
    log.info("⛔ Autonomous deal scanner is DISABLED. Source-copy remains active.")


async def managed_post_stop(app):
    source_task = app.bot_data.pop("source_copy_task", None)
    if source_task and not source_task.done():
        source_task.cancel()
        try:
            await source_task
        except asyncio.CancelledError:
            pass
    task = app.bot_data.pop("auto_scan_task", None)
    if task and not task.done():
        log.info("🛑 Stopping autonomous deal scanner...")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    log.info("✅ Legacy autonomous deal scanner remains disabled.")


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

    app.add_handler(ChatMemberHandler(bot.track_auto_publish_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, bot.track_group_message), group=1)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_force_join_admin_text),
        group=1,
    )

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("addoffer", bot.admin_start),
            CallbackQueryHandler(bot.admin_add_button, pattern=r"^admin_add$"),
        ],
        states={
            bot.C_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_title)],
            bot.C_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_price)],
            bot.C_OLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_old)],
            bot.C_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_category)],
            bot.C_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_url)],
            bot.C_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.got_count)],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel)],
    )

    app.add_error_handler(polling_error_handler)
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help_command))
    app.add_handler(CommandHandler("admin", bot.admin_help))
    app.add_handler(CommandHandler("setinterval", bot.set_interval_command))
    app.add_handler(CommandHandler("addmenu", bot.add_menu_item))
    app.add_handler(CommandHandler("testchannels", bot.test_channels))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(bot.button_handler))
    return app


def run():
    if not bot.TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    # Bind Render's public port immediately, before DB initialization or Telegram
    # network calls, so Render's web-service port scan can succeed during startup.
    health_thread = Thread(target=bot.run_health_server, name="zoner-health", daemon=True)
    health_thread.start()
    log.info("🌐 Health server starting on Render PORT=%s", __import__("os").getenv("PORT", "10000"))

    bot.init_db()
    app = build_app()
    log.info("🔥 Zoner Offers runner is starting.")
    app.run_polling(
        poll_interval=0.5,
        timeout=20,
        bootstrap_retries=-1,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    run()
