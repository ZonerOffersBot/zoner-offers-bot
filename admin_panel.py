"""Admin-only settings helpers."""
import aiosqlite
import bot

ADMIN_ID = getattr(bot, "ADMIN_ID", "")
DB_FILE = getattr(bot, "DB_FILE", "zoner_offers.db")

async def set_layout(update, context):
    query = update.callback_query

    # Admin-only: never allow normal users to change bot settings.
    user = getattr(query, "from_user", None)
    if not user or str(getattr(user, "id", "")) != str(ADMIN_ID):
        await query.answer("❌ Admin only.", show_alert=True)
        return

    try:
        layout = int(str(query.data).split("_")[-1])
    except (ValueError, AttributeError):
        await query.answer("❌ Invalid layout.", show_alert=True)
        return

    if layout not in (1, 2, 3):
        await query.answer("❌ Layout must be 1, 2, or 3.", show_alert=True)
        return

    # Keep the existing database name/configuration; do not create bot.db.
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        await db.execute(
            "INSERT INTO settings(key, value) VALUES('buttons_per_row', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(layout),),
        )
        await db.commit()

    await query.answer("Layout updated ✅")
    await query.edit_message_text(
        f"Layout set to {layout} buttons per row ✅"
    )
