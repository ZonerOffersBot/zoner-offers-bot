"""One-time force-join verification facade."""
import bot

membership_status = bot.membership_status

async def is_verified(user_id: int) -> bool:
    return bool(membership_status(user_id))

async def ensure_verified(user_id: int) -> bool:
    return await is_verified(user_id)
