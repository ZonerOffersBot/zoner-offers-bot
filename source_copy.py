"""
Public Telegram channel copier.
"""
import asyncio
import html
import logging
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("zoner")

SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "@Flipkartdj").strip()
COPY_ENABLED = os.getenv("SOURCE_COPY_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}
COPY_INTERVAL = 30


def _username():
    value = SOURCE_CHANNEL.strip()
    if value.startswith(("https://t.me/", "http://t.me/")):
        value = value.rstrip("/").split("/")[-1]
    return value.lstrip("@").split("?")[0]


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
        match = re.search(r"/(\d+)$", node.get("data-post", ""))
        if not match:
            continue
        message_id = match.group(1)
        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""
        media = []
        for photo in node.select(".tgme_widget_message_photo_wrap"):
            m = re.search(r'url\([\'"]?([^\'")]+)', photo.get("style", ""))
            if m:
                media.append(urljoin("https://t.me/", html.unescape(m.group(1))))
        if not media:
            for img in node.select("img"):
                src = img.get("src")
                if src and "telegram" in src:
                    media.append(urljoin("https://t.me/", src))
                    break
        posts.append({"id": message_id, "text": text, "media": media})
    return posts


def _fetch():
    response = requests.get(
        _source_url(),
        headers={"User-Agent": "Mozilla/5.0 ZonerOffersBot public-channel-monitor/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    return _extract_posts(response.text)


async def _send_post(bot, app, post):
    destinations = list(dict.fromkeys(bot.POST_CHANNELS))
    try:
        con = bot.db()
        try:
            rows = con.execute("SELECT chat_id FROM auto_publish_chats WHERE enabled=1").fetchall()
        finally:
            con.close()
        destinations.extend(str(r["chat_id"]) for r in rows)
    except Exception:
        log.exception("Could not read auto-publish group registry")

    sent_any = False
    caption = post["text"][:1024] if post["text"] else "📢 New post from source channel"
    for target in dict.fromkeys(destinations):
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

    bot = __import__("bot")
    _ensure_table(bot)
    log.info("📥 Public source-copy monitor started: %s", _source_url())

    while True:
        try:
            posts = await asyncio.to_thread(_fetch)
            for post in posts:
                if _already_copied(bot, post["id"]):
                    continue
                if await _send_post(bot, app, post):
                    _mark_copied(bot, post["id"])
                    log.info("📥 Copied source post %s from @%s", post["id"], _username())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Source-copy cycle failed; retrying")
        await asyncio.sleep(COPY_INTERVAL)
