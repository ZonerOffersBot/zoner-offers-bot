import logging
import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import DB_PATH, ADMIN_IDS

logger = logging.getLogger(__name__)

async def init_force_join_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS verified_users (
            user_id INTEGER PRIMARY KEY,
            verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()

async def is_verified(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM verified_users WHERE user_id=? LIMIT 1", (user_id,))
        return await cursor.fetchone() is not None

async def mark_verified(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO verified_users(user_id) VALUES(?)", (user_id,))
        await db.commit()

async def get_force_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT channel_id, channel_name, invite_link FROM force_channels WHERE is_active=1"
        )
        return await cursor.fetchall()

async def check_force_join(user_id, bot):
    if user_id in ADMIN_IDS or await is_verified(user_id):
        return []
    channels = await get_force_channels()
    not_joined = []
    for ch_id, ch_name, link in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                not_joined.append((ch_name, link))
        except Exception as exc:
            logger.warning("Force-join check failed for %s: %s", ch_id, exc)
            not_joined.append((ch_name, link))
    if not not_joined:
        await mark_verified(user_id)
    return not_joined

def build_force_join_keyboard(channels, buttons_per_row=2):
    buttons_per_row = max(1, min(3, int(buttons_per_row)))
    keyboard, row = [], []
    for name, link in channels:
        row.append(InlineKeyboardButton(f"📢 Join {name}", url=link))
        if len(row) >= buttons_per_row:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✅ Maine join kar liya", callback_data="verify_join")])
    return InlineKeyboardMarkup(keyboard)
