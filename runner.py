import asyncio
import logging
import bot

log = logging.getLogger("zoner")

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

bot.post_init = managed_post_init
bot.post_stop = managed_post_stop

# bot.run_bot() builds the Application using the patched lifecycle hooks.
bot.run_bot()
