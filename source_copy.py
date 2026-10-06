"""
Public Telegram channel copier.

Reads the public preview of a channel (t.me/s/<username>) without requiring
admin access to the source channel, then republishes new posts through the
existing bot. It stores source message IDs so a restart does not duplicate
posts.
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("zoner")

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "@Flipkartdj").strip()
COPY_ENABLED = os.getenv("SOURCE_COPY_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
COPY_INTERVAL = 60

def _username():
    value = SOURCE_CHANNEL.strip()
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        value = value.rstrip("/").split("/")[-1]
    value = value.lstrip("@").split("?")[0]
    return value

def _source_url():
    return f"https://t.me/s/{_username()}"

def _ensure_table(bot):
    con = bot.db()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS source_copy_history (
                source_channel TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                copied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source_channel, source_message_id)
            )
        """)
        con.commit()
    finally:
        con.close()

def _already_copied(bot, message_id):
    con = bot.db()
    try:
        return con.execute(
            "SELECT 1 FROM source_copy_history WHERE source_channel=? AND source_message_id=? LIMIT 1",
            (_username().lower(), str(message_id)),
        ).fetchone() is not None
    finally:
        con.close()

def _mark_copied(bot, message_id):
    con = bot.db()
    try:
        con.execute(
            "INSERT OR IGNORE INTO source_copy_history(source_channel, source_message_id) VALUES(?,?)",
            (_username().lower(), str(message_id)),
        )
        con.commit()
    finally:
        con.close()

def _extract_posts(page):
    soup = BeautifulSoup(page, "html.parser")
    posts = []
    for node in soup.select(".tgme_widget_message"):
        data_post = node.get("data-post", "")
        match = re.search(r"/(d+)$", data_post)
        if not match:
            continue
        message_id = match.group(1)

        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""

        # Telegram's public preview exposes media as background images on
        # tgme_widget_message_photo_wrap. Use the absolute URL as send_photo input.
        media = []
        for photo in node.select(".tgme_widget_message_photo_wrap"):
            style = photo.get("style", "")
            m = re.search(r'url\([\'"]?([^\'")]+)', style)
            if m:
                media.append(urljoin("https://t.me/", html.unescape(m.group(1))))
        # Video previews can expose a direct thumbnail. Sending the thumbnail
        # is safer than attempting to download arbitrary media from the source.
        if not media:
            for img in node.select("img"):
                src = img.get("src")
                if src and "telegram" in src:
                    media.append(urljoin("https://t.me/", src))
                    break

        posts.append({"id": message_id, "text": text, "media": media})
    return posts

def _fetch():
    headers = {"User-Agent": "Mozilla/5.0 ZonerOffersBot public-channel-monitor/1.0"}
    response = requests.get(_source_url(), headers=headers, timeout=20)
    response.raise_for_status()
    return _extract_posts(response.text)

async def _send_post(bot, app, post):
    destinations = list(dict.fromkeys(bot.POST_CHANNELS))
    # Also reuse the existing auto-publish group registry.
    try:
        con = bot.db()
        try:
            rows = con.execute("SELECT chat_id FROM auto_publish_chats WHERE enabled=1").fetchall()
        finally:
            con.close()
        destinations.extend([str(r["chat_id"]) for r in rows])
    except Exception:
        log.exception("Could not read auto-publish group registry")

    destinations = list(dict.fromkeys(destinations))
    caption = post["text"][:1024] if post["text"] else "📢 New post from source channel"
    sent_any = False

    for target in destinations:
        try:
            if post["media"]:
                await app.bot.send_photo(chat_id=target, photo=post["media"][0], caption=caption)
            else:
                await app.bot.send_message(chat_id=target, text=caption)
            sent_any = True
        except Exception as exc:
            log.warning("Source-copy failed for %s -> %s: %s", post["id"], target, exc)

    return sent_any

async def source_copy_loop(app):
    if not COPY_ENABLED:
        log.info("Source-copy is disabled.")
        return

    _ensure_table(__import__("bot"))
    bot = __import__("bot")
    log.info("📥 Public source-copy monitor started: %s", _source_url())

    while True:
        try:
            posts = await asyncio.to_thread(_fetch)
            # Process oldest first. The preview normally contains only recent posts.
            for post in posts:
                if _already_copied(bot, post["id"]):
                    continue
                # Mark only after at least one successful destination send.
                if await _send_post(bot, app, post):
                    _mark_copied(bot, post["id"])
                    log.info("📥 Copied source post %s from @%s", post["id"], _username())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Source-copy cycle failed; retrying")
        await asyncio.sleep(COPY_INTERVAL)
