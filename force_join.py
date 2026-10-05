"""Force-join helper using the channels already configured in the live bot.

The existing publication channels are reused here so no new force_channels table
or separate bot.db is required.
"""
import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

FORCE_CHANNELS = [
    (
        os.getenv("CHANNEL_ID") or "@zoneroffers",
        "Zoner Offers",
        os.getenv("CHANNEL_URL") or "https://t.me/zoneroffers",
    ),
    (
        "@offerleloturant",
        "Offerleloturant",
        os.getenv("SECOND_CHANNEL_URL") or "https://t.me/offerleloturant",
    ),
]

def get_force_channels():
    """Return the already configured channels, without creating a new DB table."""
    seen = set()
    result = []
    for channel_id, channel_name, invite_link in FORCE_CHANNELS:
        key = str(channel_id)
        if key not in seen and invite_link:
            seen.add(key)
            result.append((channel_id, channel_name, invite_link))
    return result

async def check_force_join(user_id, bot):
    """Check whether the user has joined every configured force-join channel."""
    not_joined = []
    for ch_id, ch_name, link in get_force_channels():
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                not_joined.append((ch_name, link))
        except Exception:
            # If Telegram cannot verify membership, keep the channel in the
            # join list rather than incorrectly marking the user as verified.
            not_joined.append((ch_name, link))
    return not_joined

def build_force_join_keyboard(channels=None, buttons_per_row=2):
    """Build a dynamic join keyboard plus a single Verify button."""
    channels = channels if channels is not None else [
        (name, link) for _, name, link in get_force_channels()
    ]

    buttons_per_row = max(1, int(buttons_per_row))
    keyboard = []
    row = []

    for name, link in channels:
        row.append(InlineKeyboardButton(f"📢 Join {name}", url=link))
        if len(row) >= buttons_per_row:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton("✅ Maine join kar liya", callback_data="verify_join")
    ])
    return InlineKeyboardMarkup(keyboard)
