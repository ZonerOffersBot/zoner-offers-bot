"""
Public Telegram deal-source copier for Zoner Offers.

Four public sources, one formatted post every 5 minutes, 48-hour backlog,
duplicate protection, image upload with text fallback, and destination
permission diagnostics.
"""
import asyncio
import html
import io
import logging
import os
import re
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("zoner")

# One publisher critical section per running process. The duplicate gate and
# Telegram send must happen in the same serialized section so two concurrent
# source-copy workers cannot both pass the public-channel check and then send.
PUBLICATION_LOCK = asyncio.Lock()

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
COPY_INTERVAL = 300
BACKLOG_HOURS = 48
RUNTIME_LOCK_LEASE_SECONDS = 600
RUNTIME_LOCK_OWNER = uuid.uuid4().hex

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*[0-9][0-9,]*(?:\.\d{1,2})?", re.I)
DISCOUNT_RE = re.compile(
    r"\b[0-9]{1,3}\s*%\s*(?:off|discount)?\b|\bdiscount\s*[:\-]?\s*[^\n]+",
    re.I,
)
BRAND_RE = re.compile(r"\bbrand\s*[:\-]\s*([^\n|]+)", re.I)


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
                product_link TEXT DEFAULT '',
                copied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source_channel, source_message_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS source_copy_link_history (
                product_link TEXT PRIMARY KEY,
                copied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS source_copy_content_history (
                content_key TEXT PRIMARY KEY,
                copied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Authoritative one-time publication guard for exact normalized links.
        con.execute("""
            CREATE TABLE IF NOT EXISTS publication_guard (
                normalized_link TEXT PRIMARY KEY,
                source_channel TEXT DEFAULT '',
                source_message_id TEXT DEFAULT '',
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS source_copy_runtime_lock (
                lock_name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                heartbeat REAL NOT NULL
            )
        """)
        columns = {
            row[1] for row in con.execute("PRAGMA table_info(source_copy_history)").fetchall()
        }
        if "product_link" not in columns:
            con.execute("ALTER TABLE source_copy_history ADD COLUMN product_link TEXT DEFAULT ''")
        con.commit()
    finally:
        con.close()


def _acquire_runtime_lock(bot):
    con = bot.db()
    try:
        con.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).timestamp()
        row = con.execute(
            "SELECT owner, heartbeat FROM source_copy_runtime_lock WHERE lock_name='publisher'"
        ).fetchone()
        if row and row["owner"] != RUNTIME_LOCK_OWNER and now - float(row["heartbeat"]) < RUNTIME_LOCK_LEASE_SECONDS:
            con.rollback()
            return False
        con.execute(
            """INSERT INTO source_copy_runtime_lock(lock_name, owner, heartbeat)
               VALUES('publisher', ?, ?)
               ON CONFLICT(lock_name) DO UPDATE SET owner=excluded.owner, heartbeat=excluded.heartbeat""",
            (RUNTIME_LOCK_OWNER, now),
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        log.exception("Could not acquire source-copy singleton lock")
        return False
    finally:
        con.close()


def _release_runtime_lock(bot):
    con = bot.db()
    try:
        con.execute(
            "DELETE FROM source_copy_runtime_lock WHERE lock_name='publisher' AND owner=?",
            (RUNTIME_LOCK_OWNER,),
        )
        con.commit()
    finally:
        con.close()


def _normalize_link(link):
    """Normalize a URL for one-time publication protection."""
    value = (link or "").strip()
    return value.rstrip("/").lower() if value else ""

def _public_channel_has_link(target, product_link):
    """Check the public Telegram preview for a link already published in a destination.

    This is an extra cross-process/restart safety net. SQLite history can be lost
    when a Render instance is restarted without persistent storage, but the public
    destination channel itself is the durable publication record.
    """
    normalized = _normalize_link(product_link)
    target = str(target or "").strip()
    if not normalized or not target.startswith("@"):
        return False

    username = target.lstrip("@").split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", username):
        return False

    try:
        response = requests.get(
            f"https://t.me/s/{username}",
            headers={"User-Agent": "Mozilla/5.0 ZonerOffersBot duplicate-check/1.0"},
            timeout=12,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup.select(".tgme_widget_message"):
            text = node.get_text(" ", strip=True)
            for raw in URL_RE.findall(text):
                if _normalize_link(raw.rstrip(").,>]]")) == normalized:
                    return True
            for anchor in node.select("a[href]"):
                href = html.unescape(anchor.get("href", "")).strip()
                if _normalize_link(href) == normalized:
                    return True
        return False
    except Exception as exc:
        log.warning("Public destination duplicate check failed target=%s: %s", target, exc)
        return False



def _save_published_offer(bot, post, source):
    """Save a successfully published source post into the bot's normal offer store."""
    original = (post.get("text") or "").strip()
    link = _first_url(original)
    if not link:
        return None

    title = _product_name(original)
    price = _price(original)
    price_value = re.sub(r"[^0-9.]", "", price) if price else ""
    if not price_value:
        price_value = "Check live price"

    brand = _brand(original)
    discount_text = _discount(original)
    discount_match = re.search(r"(\d{1,3})\s*%", discount_text or "")
    discount = min(int(discount_match.group(1)), 100) if discount_match else 0
    category = bot.guess_category(" ".join([title, original, brand, _username(source)]))
    source_name = _username(source) or "Telegram"
    image_url = (post.get("media") or [""])[0]
    fingerprint = bot.fingerprint(title, link)

    con = bot.db()
    try:
        existing = con.execute(
            "SELECT id FROM offers WHERE fingerprint=? OR url=? LIMIT 1",
            (fingerprint, link),
        ).fetchone()
        if existing:
            offer_id = existing["id"]
            con.execute(
                "UPDATE offers SET title=?, price=?, category=?, source=?, discount=?, "
                "image_url=?, description=?, url=? WHERE id=?",
                (title, price_value, category, source_name, discount, image_url,
                 original[:1000], link, offer_id),
            )
        else:
            score = bot.score_deal(title, discount, source_name)
            cur = con.execute(
                "INSERT INTO offers(title,price,old_price,category,url,source,discount,score,"
                "fingerprint,image_url,description) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (title, price_value, "", category, link, source_name, discount, score,
                 fingerprint, image_url, original[:1000]),
            )
            offer_id = cur.lastrowid

        con.execute(
            "INSERT INTO publish_history(offer_id,published_at) VALUES(?,CURRENT_TIMESTAMP)",
            (offer_id,),
        )
        con.commit()
        return offer_id
    except Exception:
        con.rollback()
        log.exception("Could not save source-copy offer into Latest Offers")
        return None
    finally:
        con.close()


def _content_key(post):
    text = (post.get("text") or "").strip().lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""

def _already_copied(bot, source, message_id, product_link="", post=None):
    con = bot.db()
    try:
        if con.execute(
            "SELECT 1 FROM source_copy_history WHERE source_channel=? AND source_message_id=? LIMIT 1",
            (_username(source).lower(), str(message_id)),
        ).fetchone() is not None:
            return True
        normalized = _normalize_link(product_link)
        if normalized:
            if con.execute(
                "SELECT 1 FROM publication_guard WHERE normalized_link=? LIMIT 1",
                (normalized,),
            ).fetchone() is not None:
                return True
            if con.execute(
                "SELECT 1 FROM source_copy_link_history WHERE product_link=? LIMIT 1",
                (normalized,),
            ).fetchone() is not None:
                return True
            # Durable publication ledger: once this exact normalized URL
            # has ever been published, never publish it again.
            if con.execute(
                "SELECT 1 FROM published_links WHERE lower(rtrim(url, '/'))=? LIMIT 1",
                (normalized,),
            ).fetchone() is not None:
                return True
            # Also block URLs already stored in the offer database.
            if con.execute(
                "SELECT 1 FROM offers WHERE lower(rtrim(url, '/'))=? LIMIT 1",
                (normalized,),
            ).fetchone() is not None:
                return True
        content_key = _content_key(post or {})
        if content_key and con.execute(
            "SELECT 1 FROM source_copy_content_history WHERE content_key=? LIMIT 1",
            (content_key,),
        ).fetchone() is not None:
            return True
        return False
    finally:
        con.close()


def _mark_copied(bot, source, message_id, product_link="", post=None):
    con = bot.db()
    try:
        normalized = _normalize_link(product_link)
        con.execute(
            "INSERT OR IGNORE INTO source_copy_history(source_channel, source_message_id, product_link) VALUES(?,?,?)",
            (_username(source).lower(), str(message_id), normalized),
        )
        if normalized:
            con.execute(
                "INSERT OR IGNORE INTO source_copy_link_history(product_link) VALUES(?)",
                (normalized,),
            )
        content_key = _content_key(post or {})
        if content_key:
            con.execute(
                "INSERT OR IGNORE INTO source_copy_content_history(content_key) VALUES(?)",
                (content_key,),
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

        published_at = None
        date_node = node.select_one("time[datetime]")
        if date_node:
            try:
                published_at = datetime.fromisoformat(
                    date_node.get("datetime", "").replace("Z", "+00:00")
                )
            except ValueError:
                pass
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


def _first_url(text):
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(").,>]") if match else ""


def _clean_line(line):
    return re.sub(r"[\u200b\ufeff]", "", re.sub(r"https?://\S+", "", line)).strip(" -–—|•\t")


def _product_name(text):
    for raw in (text or "").splitlines():
        line = _clean_line(raw)
        if line and not (PRICE_RE.search(line) or DISCOUNT_RE.search(line) or BRAND_RE.search(line)):
            return line[:180]
    return "Latest Deal"


def _brand(text):
    match = BRAND_RE.search(text or "")
    return match.group(1).strip()[:80] if match else ""


def _price(text):
    match = PRICE_RE.search(text or "")
    return match.group(0).strip() if match else ""


def _discount(text):
    match = DISCOUNT_RE.search(text or "")
    return match.group(0).strip()[:100] if match else ""


def _source_label(source):
    return {
        "WomenOfferUpdates": "Women Offer Updates",
        "viratloot": "Virat Loot",
        "Meesho9loot": "Meesho Loot",
        "Lootunboxing": "Loot Unboxing",
    }.get(_username(source), _username(source) or "Telegram Source")


def _format_post(post, source):
    original = (post.get("text") or "").strip()
    product = _product_name(original)
    brand = _brand(original)
    price = _price(original)
    discount = _discount(original)
    link = _first_url(original)

    lines = ["🔥 ZONER OFFERS", "", f"🛍️ Product Name: {product}"]
    if brand:
        lines.append(f"🏷️ Brand: {brand}")
    if price:
        lines.append(f"💰 Price: {price}")
    if discount:
        lines.append(f"📉 Discount: {discount}")
    if link:
        lines.extend(["", "🛒 Buy Now", f"👉 {link}"])
    lines.extend(["", "@zoneroffers", "@offerleloturant"])
    return "\n".join(lines)[:4096]


def _destinations(bot):
    destinations = list(dict.fromkeys(bot.POST_CHANNELS))
    try:
        con = bot.db()
        try:
            rows = con.execute("SELECT chat_id FROM auto_publish_chats WHERE enabled=1").fetchall()
        finally:
            con.close()
        destinations.extend(str(row["chat_id"]) for row in rows)
    except Exception:
        log.exception("Could not read auto-publish group registry")
    return list(dict.fromkeys(destinations))


async def _diagnose_target(app, target):
    try:
        me = await app.bot.get_me()
        member = await app.bot.get_chat_member(chat_id=target, user_id=me.id)
        status = getattr(member, "status", "unknown")
        rights = getattr(member, "can_post_messages", None)
        log.info(
            "SOURCE-COPY TARGET CHECK target=%s bot=%s status=%s can_post_messages=%s",
            target, me.username, status, rights,
        )
    except Exception as exc:
        log.warning("SOURCE-COPY TARGET CHECK failed target=%s: %s", target, exc)


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
    return [text[i:i + limit] for i in range(0, len(text or ""), limit)] or [""]


async def _send_one_destination(app, target, formatted, image):
    """Publish one source post to one destination; runs independently of others."""
    try:
        if image is not None:
            image.seek(0)
            try:
                await app.bot.send_photo(
                    chat_id=target,
                    photo=image,
                    caption=formatted[:1024],
                )
                remainder = formatted[1024:].strip()
                if remainder:
                    for chunk in _chunks(remainder):
                        await app.bot.send_message(chat_id=target, text=chunk)
            except Exception as photo_exc:
                log.warning(
                    "Photo publish failed -> %s; falling back to formatted text: %s",
                    target, photo_exc,
                )
                for chunk in _chunks(formatted):
                    await app.bot.send_message(chat_id=target, text=chunk)
        else:
            for chunk in _chunks(formatted):
                await app.bot.send_message(chat_id=target, text=chunk)
        return True
    except Exception as exc:
        log.warning("Source-copy destination failed -> %s: %s", target, exc)
        return False


def _claim_post(bot, source, message_id, product_link="", post=None):
    """Atomically reserve a post/link before sending so concurrent copier loops
    cannot publish the same deal repeatedly."""
    con = bot.db()
    try:
        con.execute("BEGIN IMMEDIATE")
        normalized = _normalize_link(product_link)
        if normalized:
            # Final pre-publish gate: the PRIMARY KEY makes this an atomic
            # one-time lock for the exact normalized product URL.
            cur = con.execute(
                """INSERT OR IGNORE INTO publication_guard(
                       normalized_link, source_channel, source_message_id
                   ) VALUES(?,?,?)""",
                (normalized, _username(source).lower(), str(message_id)),
            )
            if cur.rowcount != 1:
                con.rollback()
                log.warning("🚫 PRE-PUBLISH BLOCK: exact link already claimed: %s", normalized)
                return False

            if con.execute(
                "SELECT 1 FROM source_copy_link_history WHERE product_link=? LIMIT 1",
                (normalized,),
            ).fetchone() is not None:
                con.rollback()
                return False
            if con.execute(
                "SELECT 1 FROM published_links WHERE lower(rtrim(url, '/'))=? LIMIT 1",
                (normalized,),
            ).fetchone() is not None:
                con.rollback()
                return False
            if con.execute(
                "SELECT 1 FROM offers WHERE lower(rtrim(url, '/'))=? LIMIT 1",
                (normalized,),
            ).fetchone() is not None:
                con.rollback()
                return False

        if con.execute(
            "SELECT 1 FROM source_copy_history WHERE source_channel=? AND source_message_id=? LIMIT 1",
            (_username(source).lower(), str(message_id)),
        ).fetchone() is not None:
            con.rollback()
            return False
        key = _content_key(post or {})
        if key and con.execute(
            "SELECT 1 FROM source_copy_content_history WHERE content_key=? LIMIT 1",
            (key,),
        ).fetchone() is not None:
            con.rollback()
            return False

        con.execute(
            "INSERT INTO source_copy_history(source_channel, source_message_id, product_link) VALUES(?,?,?)",
            (_username(source).lower(), str(message_id), normalized),
        )
        if normalized:
            con.execute(
                "INSERT INTO source_copy_link_history(product_link) VALUES(?)",
                (normalized,),
            )
        if key:
            con.execute(
                "INSERT INTO source_copy_content_history(content_key) VALUES(?)",
                (key,),
            )
        con.commit()
        return True
    except Exception:
        con.rollback()
        log.exception("Could not atomically claim source post")
        return False
    finally:
        con.close()


def _release_claim(bot, source, message_id, product_link="", post=None):
    # Never release a link claim. Telegram can partially succeed (one destination
    # succeeds while another fails), and releasing here would allow the next cycle
    # to publish the exact same link again. Duplicate prevention is higher priority
    # than retrying a failed source post.
    return


async def _send_post(bot, app, post, source):
    formatted = _format_post(post, source)
    image_url = post.get("media", [""])[0] if post.get("media") else ""
    image = await asyncio.to_thread(_download_image, image_url)
    targets = _destinations(bot)

    if not targets:
        return False

    # Start every destination at once so a slow/failing chat does not block the others.
    results = await asyncio.gather(
        *(
            _send_one_destination(app, target, formatted, image)
            for target in targets
        ),
        return_exceptions=True,
    )

    sent_any = False
    for target, result in zip(targets, results):
        if result is True:
            sent_any = True
            log.info(
                "Source-copy published @%s/%s -> %s",
                _username(source), post["id"], target,
            )
        elif isinstance(result, Exception):
            log.warning(
                "Source-copy unexpected destination error @%s/%s -> %s: %s",
                _username(source), post["id"], target, result,
            )

    return sent_any


async def source_copy_loop(app):
    if not COPY_ENABLED:
        log.info("Source-copy is disabled.")
        return

    bot = __import__("bot")
    _ensure_table(bot)

    if not _acquire_runtime_lock(bot):
        log.error("🛑 DUPLICATE-PROCESS BLOCK: another source-copy publisher is already running; this instance will not publish.")
        return

    active_sources = [s for s in SOURCE_CHANNELS if _username(s)]
    for source in SOURCE_CHANNELS:
        if not _username(source):
            log.warning("Skipping inaccessible/private source: %s", source)

    destinations = _destinations(bot)
    for target in destinations:
        await _diagnose_target(app, target)

    if not active_sources:
        log.warning("No public source channels configured.")
        return

    log.info(
        "📥 Zoner source-copy started: %s | one post every %ss | backlog %sh",
        ", ".join("@" + _username(s) for s in active_sources),
        COPY_INTERVAL, BACKLOG_HOURS,
    )

    while True:
        try:
            results = await asyncio.gather(
                *[asyncio.to_thread(_fetch, source) for source in active_sources],
                return_exceptions=True,
            )
            candidates = []
            for source, result in zip(active_sources, results):
                if isinstance(result, Exception):
                    log.warning("Source unavailable @%s: %s", _username(source), result)
                    continue
                for post in result:
                    link = _first_url(post.get("text") or "")
                    if link and not _already_copied(bot, source, post["id"], link, post):
                        candidates.append((post.get("published_at"), source, post, link))

            candidates.sort(
                key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc)
            )

            if candidates:
                _, source, post, link = candidates[0]

                # The duplicate check, atomic claim, and Telegram send are
                # serialized inside one process. This closes the race where two
                # source-copy workers both check the public channel before either
                # message becomes visible there.
                async with PUBLICATION_LOCK:
                    # Telegram itself is the final durable publication ledger for
                    # the public Zoner channel. This catches links published by an
                    # older deployment/process whose SQLite history is missing/reset.
                    already_on_zoner = await asyncio.to_thread(
                        _public_channel_has_link, "@zoneroffers", link
                    )
                    if already_on_zoner:
                        log.warning(
                            "🚫 CHANNEL PRE-PUBLISH BLOCK: exact link already exists in @zoneroffers: %s",
                            _normalize_link(link),
                        )
                        _mark_copied(bot, source, post["id"], link, post)
                        continue

                    # Re-check the local durable guard after entering the lock.
                    # Another worker may have claimed the candidate while this
                    # worker was building the candidate list.
                    if _already_copied(bot, source, post["id"], link, post):
                        log.warning(
                            "🚫 LOCKED PRE-PUBLISH BLOCK: exact link already recorded: %s",
                            _normalize_link(link),
                        )
                        continue

                    # Claim immediately before Telegram send. Never release the
                    # claim on failure: a partial Telegram send must not be retried
                    # as a duplicate link.
                    if not _claim_post(bot, source, post["id"], link, post):
                        continue
                    try:
                        sent = await _send_post(bot, app, post, source)
                    except Exception:
                        sent = False
                        log.exception(
                            "⚠️ Source post processing failed; skipping without stopping publisher: @%s/%s",
                            _username(source), post["id"],
                        )

                if sent:
                    try:
                        offer_id = _save_published_offer(bot, post, source)
                    except Exception:
                        offer_id = None
                        log.exception(
                            "⚠️ Published post could not be saved; continuing publisher"
                        )
                    log.info(
                        "📥 Published + permanently blocked source post @%s/%s offer_id=%s",
                        _username(source), post["id"], offer_id,
                    )
                else:
                    log.warning(
                        "⏭️ Source post skipped after send/copy problem; publisher continues: %s",
                        _normalize_link(link),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Source-copy cycle failed; retrying")

        await asyncio.sleep(COPY_INTERVAL)
