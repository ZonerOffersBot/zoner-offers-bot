"""Production scheduler facade.

The live bot owns the single publishing loop. This module exposes admin-safe
interval helpers and delegates scanning to bot.py. It intentionally does not
start a second APScheduler job, preventing duplicate channel posts.
"""
import bot

MIN_INTERVAL = 30
MAX_INTERVAL = 86400
DEFAULT_INTERVAL = getattr(bot, "SCAN_SECONDS", 90)

async def get_interval():
    """Return the configured posting interval in seconds."""
    value = bot.get_setting_sync("post_interval", DEFAULT_INTERVAL)
    try:
        return max(MIN_INTERVAL, min(int(value), MAX_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL

async def set_interval(seconds):
    """Persist the posting interval in the live bot database."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        raise ValueError("Interval must be an integer number of seconds.")
    if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
        raise ValueError(f"Interval must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds.")

    con = bot.db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        con.execute(
            "INSERT INTO settings(key, value) VALUES('post_interval', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(seconds),),
        )
        con.commit()
    finally:
        con.close()
    return seconds

async def get_connected_channels():
    """Return active publication channels.

    Production channels are controlled by bot.POST_CHANNELS; no second database
    or connected_channels table is required.
    """
    return list(dict.fromkeys(getattr(bot, "POST_CHANNELS", [])))

async def auto_post_job(app):
    """Run one normal production scan/publish cycle."""
    return await bot.scan_and_publish(app.bot if hasattr(app, "bot") else app)

async def start_scheduler(app):
    """Compatibility entry point; the runner already starts bot.auto_scan_loop."""
    return await bot.auto_scan_loop(app)

async def change_interval(update, context):
    """Admin command handler: /interval <seconds>."""
    if not bot.is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return

    args = getattr(context, "args", []) or []
    if not args:
        current = await get_interval()
        await update.message.reply_text(
            f"⚙️ Current auto-post interval: {current} seconds\n"
            f"Use: /interval <seconds>\n"
            f"Allowed: {MIN_INTERVAL}-{MAX_INTERVAL} seconds"
        )
        return

    try:
        seconds = int(args[0])
    except (TypeError, ValueError):
        await update.message.reply_text("❌ Interval must be a number of seconds.")
        return

    try:
        seconds = await set_interval(seconds)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    await update.message.reply_text(
        f"✅ Auto-post interval set to {seconds} seconds.\n"
        "It will apply on the next publishing cycle."
    )

# Backward-compatible aliases.
run = start_scheduler
auto_scan_loop = start_scheduler
