"""Auto-publish scheduler facade.
The existing scan/publish engine remains the source of truth.
"""
import asyncio
import bot

SCAN_SECONDS = getattr(bot, "SCAN_SECONDS", 90)

async def scan_once(app):
    return await bot.scan_and_publish(app)

async def run(app, interval=None):
    interval = interval or SCAN_SECONDS
    return await bot.auto_scan_loop(app)

auto_scan_loop = run
