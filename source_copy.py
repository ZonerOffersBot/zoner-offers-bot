"""
Fast public Telegram source copier.

Copies posts from configured public channels through their public web preview.
The four default sources are scanned every 30 seconds. Posts up to 24 hours old
are eligible, and each source/message is stored in SQLite to prevent duplicates.
Private/invite links are ignored because they have no public preview.
"""
import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("zoner")

DEFAULT_SOURCES = [
    "https://t.me/WomenOfferUpdates",
    "https://t.me/viratloot",
    "https://t.me/Meesho9loot",
    "https://t.me/Lootunboxing",
]
SOURCE_CHANNELS = [
    x.strip()
    for x in re.split(r"[,\n]+", os.getenv("SOURCE_CHANNELS", "").strip())
    if x.strip()
] or DEFAULT_SOURCES

COPY_ENABLED = os.getenv("SOURCE_COPY_ENABLED", "1").strip().lower() not in {
    "0", "false", "off", "no"
}
COPY_INTERVAL = 30
BACKLOG_HOURS = 24


def _username(source):
    value = (source or "").strip()
    if value.startswith(("https://t.me/", "http://t.me/")):
        value = value.rstrip("/").split("/")[-1]
    value = value.lstrip("@").split("?")[0]
    if not value or value.startswith("+"):
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9_]{3,64}", value) else ""


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
            "SELECT 1 FROM source_copy_history WHERE source_channel=? AND source_message_id=? LIMIT 1",
            (_username(source).lower(), str(message_id)),
        ).fetchone() is not None
    finally:
        con.close()


def _mark_copied(bot, source, message_id):
    con = bot.db()
    try:
        con.execute(
            "INSERT OR IGNORE INTO source_copy_history(source_channel, source_message_id) VALUES(?,?)",
            (_username(source).lower(), str(message_id)),
        )
        con.commit()
    finally:
        con.close()


def _extract_posts(page):
    soup = BeautifulSoup(page, "html.parser")
    posts = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=BACKLOG_HOURS)

    for node in soup.select(".tgme_widget_message"):
        match = re.search(r"/(\d+)$", node.get("data-post", ""))
        if not match:
            continue

        message_id = match.group(1)
        date_node = node.select_one("time[datetime]")
        published_at = None
        if date_node:
            raw_date = date_node.get("datetime", "").strip()
            try:
                published_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                published_at = None

        # If Telegram exposes a timestamp, enforce the 24-hour backlog window.
        if published_at is not None and published_at < cutoff:
            continue

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

        posts.append({
            "id": message_id,
            "text": text,
            "media": media,
            "published_at": published_at,
        })

    posts.sort(key=lambda p: p.get("published_at") or datetime.min.replace(tzinfo=timezone.utc))
    return posts


def _fetch(source):
    url = _source_url(source)
    if not url:
        return []

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 ZonerOffersBot public-channel-monitor/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    return _extract_posts(response.text)


def _destinations(bot):
    destinations = list(dict.fromkeys(bot.POST_CHANNELS))
    con = bot.db()
    try:
        rows = con.execute(
            "SELECT chat_id FROM auto_publish_chats WHERE enabled=1"
        ).fetchall()
        destinations.extend(str(row["chat_id"]) for row in rows)
    finally:
        con.close()
    return list(dict.fromkeys(destinations))


async def _send_post(bot, app, post):
    sent_any = False
    caption = post["text"][:1024] if post["text"] else "📢 New post from source channel"

    for target in _destinations(bot):
        try:
            if post["media"]:
                await app.bot.send_photo(
                    chat_id=target,
                    photo=post["media"][0],
                    caption=caption,
                )
            else:
                await app.bot.send_message(
                    chat_id=target,
                    text=caption[:4096],
                )
            sent_any = True
        except Exception as exc:
            log.warning(
                "Source-copy failed for post %s -> %s: %s",
                post["id"], target, exc
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
            log.warning("Skipping inaccessible/private source: %s", source)
            continue
        active_sources.append(source)

    if not active_sources:
        log.warning("No public source channels configured.")
        return

    log.info(
        "📥 Source-copy started: %s | every %ss | backlog %sh",
        ", ".join("@" + _username(s) for s in active_sources),
        COPY_INTERVAL,
        BACKLOG_HOURS,
    )

    while True:
        try:
            results = await asyncio.gather(
                *[
                    asyncio.to_thread(_fetch, source)
                    for source in active_sources
                ],
                return_exceptions=True,
            )

            for source, result in zip(active_sources, results):
                if isinstance(result, Exception):
                    log.warning(
                        "Source unavailable @%s: %s",
                        _username(source),
                        result,
                    )
                    continue

                for post in result:
                    if _already_copied(bot, source, post["id"]):
                        continue

                    if await _send_post(bot, app, post):
                        _mark_copied(bot, source, post["id"])
                        log.info(
                            "📥 Copied @%s/%s",
                            _username(source),
                            post["id"],
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Source-copy cycle failed; retrying")

        await asyncio.sleep(COPY_INTERVAL)
