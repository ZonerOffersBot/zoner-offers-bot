"""Dynamic main-menu helpers.

Uses the live bot database and preserves the existing callback-based menu
handlers. Layout can be changed by the admin through settings.
"""
import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import bot

DB_FILE = getattr(bot, "DB_FILE", "zoner_offers.db")

async def get_setting(key, default=None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default

async def get_menu_items():
    """Fetch active custom menu items when the optional table exists."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='menu_items'"
        )
        exists = await cursor.fetchone()
        if not exists:
            return []

        cursor = await db.execute(
            "SELECT id, label, callback_data, row_position, col_position "
            "FROM menu_items WHERE is_active=1 "
            "ORDER BY row_position, col_position"
        )
        return await cursor.fetchall()

def build_main_menu(menu_items, max_per_row=2):
    """Dynamic menu builder supporting 1, 2, 3... buttons per row."""
    try:
        max_per_row = max(1, min(3, int(max_per_row)))
    except (TypeError, ValueError):
        max_per_row = 2

    keyboard = []
    current_row = []

    for item in menu_items:
        _, label, cb_data, _, _ = item
        current_row.append(
            InlineKeyboardButton(str(label), callback_data=str(cb_data))
        )
        if len(current_row) >= max_per_row:
            keyboard.append(current_row)
            current_row = []

    if current_row:
        keyboard.append(current_row)

    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update, context):
    menu_items = await get_menu_items()
    max_per_row = await get_setting("menu_buttons_per_row", default=2)

    # Fall back to the current production menu when no custom menu items exist.
    if not menu_items:
        reply_markup = bot.main_menu(
            update.effective_user.id if update.effective_user else None
        )
    else:
        reply_markup = build_main_menu(menu_items, max_per_row)

    text = (
        "🔥 <b>DEAL HUNTER BOT</b> 🔥\n\n"
        "India ke shopping platforms se best deals aur offers paayein!\n\n"
        "👇 Neeche se option chunein:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )
