"""
Public Telegram deal-source copier for Zoner Offers.

- Four public sources by default.
- Scans every 30 seconds.
- Publishes one new eligible post per 30-second cycle.
- Includes posts from the previous 24 hours.
- Prevents duplicate source posts with SQLite.
- Converts source text into the Zoner Offers card format.
- Downloads public preview images before uploading them to Telegram, avoiding
  Telegram URL-fetch failures when possible.
"""
import asyncio
import html
import io
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

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
PRICE_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*[0-9][0-9,]*(?:\.\d{1,2})?",
    re.IGNORECASE,
)
DISCOUNT_RE = re.compile(
    r"\b[0-9]{1,3}\s*%\s*(?:off|discount)?\b|\bdiscount\s*[:\-]?\s*[^\n]+",
    re.IGNORECASE,
)
BRAND_RE = re.compile(r"\bbrand\s*[:\-]\s*([^\n|]+)", re.IGNORECASE)


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
                pass

        if published_at is not None and published_at < cutoff:
            continue

        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""

        media = []
        for photo in node.select(".tgme_widget_message_photo_wrap"):
            match_media = re.search(
                r'url\([\'"]?([^\'")]+)',
                photo.get("style", ""),
            )
            if match_media:
                media.append(
                    urljoin(
                        "https://t.me/",
                        html.unescape(match_media.group(1)),
                    )
                )

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
            "source": _username_from_node(node) or "",
        })

    posts.sort(
        key=lambda p: p.get("published_at")
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    return posts


def _username_from_node(node):
    data_post = node.get("data-post", "")
    match = re.match(r"([^/]+)/\d+$", data_post)
    return match.group(1) if match else ""


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


def _first_url(text):
    match = URL_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(").,>]")


def _clean_line(line):
    line = re.sub(r"https?://\S+", "", line)
    line = re.sub(r"[\u200b\ufeff]", "", line)
    return line.strip(" -–—|•\t")


def _product_name(text):
    for raw in (text or "").splitlines():
        line = _clean_line(raw)
        if not line:
            continue
        if PRICE_RE.search(line) or DISCOUNT_RE.search(line) or BRAND_RE.search(line):
            continue
        if len(line) >= 3:
            return line[:180]
    return "Latest Deal"


def _brand(text):
    match = BRAND_RE.search(text or "")
    return match.group(1).strip()[:80] if match else ""


def _price(text):
    match = PRICE_RE.search(text or "")
    return match.group(0).replace("  ", " ").strip() if match else ""


def _discount(text):
    match = DISCOUNT_RE.search(text or "")
    return match.group(0).strip()[:100] if match else ""


def _source_label(source):
    username = _username(source)
    labels = {
        "WomenOfferUpdates": "Women Offer Updates",
        "viratloot": "Virat Loot",
        "Meesho9loot": "Meesho Loot",
        "Lootunboxing": "Loot Unboxing",
    }
    return labels.get(username, username or "Telegram Source")


def _format_post(post, source):
    original = (post.get("text") or "").strip()
    product = _product_name(original)
    brand = _brand(original)
    price = _price(original)
    discount = _discount(original)
    link = _first_url(original)

    lines = [
        "🔥 ZONER OFFERS",
        "",
        f"🛍️ Product Name: {product}",
    ]

    if brand:
        lines.append(f"🏷️ Brand: {brand}")
    if price:
        lines.append(f"💰 Price: {price}")
    if discount:
        lines.append(f"📉 Discount: {discount}")

    if link:
        lines.extend(["", "🛒 Buy Now", f"👉 {link}"])

    lines.extend([
        "",
        f"⚡ Source: {_source_label(source)}",
    ])

    # Keep the original source text available without inventing price/details.
    details = original
    if details and len(details) < 1800:
        lines.extend(["", "📝 Details:", details])

    formatted = "\n".join(lines).strip()
    return formatted[:4096]


def _destinations(bot):
    destinations = list(dict.fromkeys(bot.POST_CHANNELS))
    try:
        con = bot.db()
        try:
            rows = con.execute(
                "SELECT chat_id FROM auto_publish_chats WHERE enabled=1"
            ).fetchall()
        finally:
            con.close()
        destinations.extend(str(row["chat_id"]) for row in rows)
    except Exception:
        log.exception("Could not read auto-publish group registry")
    return list(dict.fromkeys(destinations))


def _download_image(url):
    if not url:
        return None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 ZonerOffersBot image-fetcher/1.0"},
            timeout=20,
        )
        response.raise_for_status()
        if not response.content:
            return None
        image = io.BytesIO(response.content)
        image.name = "source.jpg"
        image.seek(0)
        return image
    except Exception as exc:
        log.warning("Could not download source image: %s", exc)
        return None


def _chunks(text, limit=4096):
    text = text or ""
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


async def _send_post(bot, app, post, source):
    formatted = _format_post(post, source)
    image_url = post.get("media", [""])[0] if post.get("media") else ""
    image = await asyncio.to_thread(_download_image, image_url)

    sent_any = False

    for target in _destinations(bot):
        try:
            if image is not None:
                image.seek(0)
                caption = formatted[:1024]
                await app.bot.send_photo(
                    chat_id=target,
                    photo=image,
                    caption=caption,
                )
                remainder = formatted[1024:].strip()
                if remainder:
                    for chunk in _chunks(remainder):
                        await app.bot.send_message(chat_id=target, text=chunk)
            else:
                for chunk in _chunks(formatted):
                    await app.bot.send_message(chat_id=target, text=chunk)

            sent_any = True
        except Exception as exc:
            log.warning(
                "Source-copy failed for @%s/%s -> %s: %s",
                _username(source),
                post["id"],
                target,
                exc,
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
        "📥 Zoner source-copy started: %s | one post every %ss | backlog %sh",
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

            candidates = []
            for source, result in zip(active_sources, results):
                if isinstance(result, Exception):
                    log.warning(
                        "Source unavailable @%s: %s",
                        _username(source),
                        result,
                    )
                    continue

                for post in result:
                    if not _already_copied(bot, source, post["id"]):
                        candidates.append((post.get("published_at"), source, post))

            candidates.sort(
                key=lambda item: item[0]
                or datetime.min.replace(tzinfo=timezone.utc)
            )

            if candidates:
                _, source, post = candidates[0]
                if await _send_post(bot, app, post, source):
                    _mark_copied(bot, source, post["id"])
                    log.info(
                        "📥 Published formatted source post @%s/%s",
                        _username(source),
                        post["id"],
                    )

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Source-copy cycle failed; retrying")

        await asyncio.sleep(COPY_INTERVAL)
