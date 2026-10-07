"""
Public Telegram channel copier.

Copies recent posts from configured PUBLIC Telegram channels using their public
web previews. Private/invite-only links are skipped. Source-copy polling is
independent of the normal 15-minute deal scanner.
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

DEFAULT_SOURCES = [
    "https://t.me/Flipkartdj",
    "https://t.me/WomenOfferUpdates",
    "https://t.me/viratloot",
    "https://t.me/Meesho9loot",
    "https://t.me/Lootunboxing",
]

# Optional comma/newline separated SOURCE_CHANNELS env var.
_raw_sources = os.getenv("SOURCE_CHANNELS", "").strip()
if _raw_sources:
    SOURCE_CHANNELS = [
        x.strip() for x in re.split(r"[,
]+", _raw_sources) if x.strip()
    ]
else:
    SOURCE_CHANNELS = DEFAULT_SOURCES

# Keep old single-source env var compatible, but never add private invite links.
legacy = os.getenv("SOURCE_CHANNEL", "").strip()
if legacy and legacy not in SOURCE_CHANNELS:
    SOURCE_CHANNELS.insert(0, legacy)

COPY_ENABLED = os.getenv("SOURCE_COPY_ENABLED", "1").strip().lower() not in {
    "0", "false", "off", "no"
}
# Fast source polling; this does NOT change the normal 15-minute deal scanner.
COPY_INTERVAL = max(5, int(os.getenv("SOURCE_COPY_INTERVAL", "10")))


def _username(source):
    value = (source or "").strip()
    if value.startswith(("https://t.me/", "http://t.me/")):
        value = value.rstrip("/").split("/")[-1]
    value = value.lstrip("@").split("?")[0]
    # Invite/private links such as t.me/+AbCd... cannot be read through
    # Telegram's public channel preview.
    if not value or value.startswith("+"):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", value):
        return ""
    return value


def _source_url(source):
    username = _username(source)
    return f"https://t.me/s/{username}" if username else ""


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


def _already_copied(bot, source, message_id):
    con = bot.db()
    try:
        return con.execute(
            """SELECT 1 FROM source_copy_history
               WHERE source_channel=? AND source_message_id=? LIMIT 1""",
            (_username(source).lower(), str(message_id)),
        ).fetchone() is not None
    finally:
        con.close()


def _mark_copied(bot, source, message_id):
    con = bot.db()
    try:
        con.execute(
            """INSERT OR IGNORE INTO source_copy_history
               (source_channel, source_message_id) VALUES(?,?)""",
            (_username(source).lower(), str(message_id)),
        )
        con.commit()
    finally:
        con.close()


def _extract_posts(page):
    soup = BeautifulSoup(page, "html.parser")
    posts = []
    for node in soup.select(".tgme_widget_message"):
        data_post = node.get("data-post", "")
        match = re.search(r"/(\d+)$", data_post)
        if not match:
            continue

        message_id = match.group(1)
        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""

        media = []
        for photo in node.select(".tgme_widget_message_photo_wrap"):
            style = photo.get("style", "")
            m = re.search(r'url\([\'"]?([^\'")]+)', style)
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


def _fetch(source):
    url = _source_url(source)
    if not url:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 ZonerOffersBot public-channel-monitor/1.0"
    }
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return _extract_posts(response.text)


async def _send_post(bot, app, source, post):
    destinations = list(dict.fromkeys(bot.POST_CHANNELS))

    try:
        con = bot.db()
        try:
            rows = con.execute(
                "SELECT chat_id FROM auto_publish_chats WHERE enabled=1"
            ).fetchall()
        finally:
            con.close()
        destinations.extend(str(r["chat_id"]) for r in rows)
    except Exception:
        log.exception("Could not read auto-publish group registry")

    destinations = list(dict.fromkeys(destinations))
    caption = post["text"][:1024] if post["text"] else "📢 New post from source channel"
    sent_any = False

    for target in destinations:
        try:
            if post["media"]:
                await app.bot.send_photo(
                    chat_id=target,
                    photo=post["media"][0],
                    caption=caption,
                )
            else:
                await app.bot.send_message(chat_id=target, text=caption)
            sent_any = True
        except Exception as exc:
            log.warning(
                "Source-copy failed for @%s post %s -> %s: %s",
                _username(source), post["id"], target, exc,
            )

    return sent_any


async def source_copy_loop(app):
    if not COPY_ENABLED:
        log.info("Source-copy is disabled.")
        return

    bot = __import__("bot")
    _ensure_table(bot)

    active_sources = []
    for source in SOURCE_CHANNELS:
        username = _username(source)
        if not username:
            log.warning("⏭️ Skipping inaccessible/private source: %s", source)
            continue
        active_sources.append(source)

    if not active_sources:
        log.warning("📥 No accessible public source channels configured.")
        return

    log.info(
        "📥 Fast raw source-copy monitor started: %s",
        ", ".join("@" + _username(s) for s in active_sources),
    )

    while True:
        try:
            # Fetch sources independently so one broken channel never blocks others.
            results = await asyncio.gather(
                *[asyncio.to_thread(_fetch, source) for source in active_sources],
                return_exceptions=True,
            )

            for source, result in zip(active_sources, results):
                if isinstance(result, Exception):
                    log.warning(
                        "Source unavailable/skipped @%s: %s",
                        _username(source), result,
                    )
                    continue

                for post in result:
                    if _already_copied(bot, source, post["id"]):
                        continue
                    if await _send_post(bot, app, source, post):
                        _mark_copied(bot, source, post["id"])
                        log.info(
                            "📥 Copied raw source post @%s/%s",
                            _username(source), post["id"],
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Source-copy cycle failed; retrying")

        await asyncio.sleep(COPY_INTERVAL)
