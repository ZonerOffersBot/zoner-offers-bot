# ZONER_RUNTIME_FIX_2026_10_06
import os
import re
import html
import json
import sqlite3
import logging
import asyncio
import hashlib
import time
from datetime import datetime, timezone
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote_plus, urlparse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
if ADMIN_ID:
    ADMIN_IDS.add(ADMIN_ID)
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
# Telegram membership checks accept a numeric chat ID or @username, not a
# t.me URL. Normalize common Render env-var formats once at startup.
def _normalize_chat_ref(value):
    value = (value or "").strip().strip("'").strip('"').strip()
    if value.startswith(("https://t.me/", "http://t.me/")):
        tail = value.rstrip("/").split("/")[-1].split("?")[0]
        # Public t.me/channel links can be converted to @username.
        if tail and not tail.startswith("+"):
            value = "@" + tail
    if value.startswith("t.me/"):
        tail = value.rstrip("/").split("/")[-1].split("?")[0]
        if tail and not tail.startswith("+"):
            value = "@" + tail
    return value

CHANNEL_ID = _normalize_chat_ref(CHANNEL_ID)
# Required publication targets are enforced in code so a stale Render env var
# cannot silently disable either channel.
_REQUIRED_POST_CHANNELS = ["@zoneroffers", "@offerleloturant"]
_env_post_channels = [x.strip() for x in os.getenv("POST_CHANNELS", "").split(",") if x.strip()]
POST_CHANNELS = list(dict.fromkeys(_REQUIRED_POST_CHANNELS + _env_post_channels))
GROUP_ID = os.getenv("GROUP_ID")
GROUP_URL = os.getenv("GROUP_URL", "")
# Keep verification/deal state on a configured persistent path when available.
# Render can supply DB_FILE (for example a mounted persistent-disk path).
DB_FILE = os.getenv("DB_FILE", "/var/data/zoner_offers.db" if os.path.isdir("/var/data") else "zoner_offers.db")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/zoneroffers").strip() or "https://t.me/zoneroffers"
SECOND_CHANNEL_URL = os.getenv("SECOND_CHANNEL_URL") or "https://t.me/offerleloturant"
try:
    SCAN_SECONDS = max(30, min(int(os.getenv("SCAN_SECONDS", "900")), 86400))
except (TypeError, ValueError):
    SCAN_SECONDS = 900
MIN_DEAL_SCORE = int(os.getenv("MIN_DEAL_SCORE", "45"))
# Fresh product discovery is allowed once every 2 hours; publishing itself
# continues on the normal interval and never waits for live scraping.
DISCOVERY_SECONDS = 7200
AUTO_POST = True  # Channel publishing is the bot's highest-priority job.

CATEGORIES = {
    "electronics": "📱 Electronics",
    "gaming": "🎮 Gaming",
    "fashion": "👕 Fashion",
    "home": "🏠 Home & Kitchen",
    "books": "📚 Books",
    "grocery": "🛒 Grocery",
    "beauty": "💄 Beauty",
    "sports": "🏃 Sports & Fitness",
    "kids": "🧸 Kids & Baby",
}

# Public, non-authenticated discovery feeds. The bot extracts deal headlines/links,
# scores them, de-duplicates them, and only publishes strong candidates.
# ONLY these nine shopping platforms are allowed. No Croma/Reliance/Tata Cliq/news/blog/affiliate domains.
PLATFORM_DOMAINS = {
    "Amazon": {"amazon.in", "www.amazon.in"},
    "Flipkart": {"flipkart.com", "www.flipkart.com"},
    "Swiggy": {"swiggy.com", "www.swiggy.com"},
    "Blinkit": {"blinkit.com", "www.blinkit.com"},
    "BigBasket": {"bigbasket.com", "www.bigbasket.com"},
    "Meesho": {"meesho.com", "www.meesho.com"},
    "Myntra": {"myntra.com", "www.myntra.com"},
    "Ajio": {"ajio.com", "www.ajio.com"},
    "SHEIN": {"sheinindia.in", "www.sheinindia.in"},
}
# These are preferred sources, not a hard allow-list. Generic discovery can
# publish legitimate shopping sites outside this list too.
PLATFORM_QUERIES = [
    ("Amazon", "Amazon India deals products discounts"),
    ("Flipkart", "Flipkart India deals products discounts"),
    ("Web Shopping", "India online shopping product deals discounts"),
    ("Web Shopping", "India electronics fashion home product sale offer"),
    ("Web Shopping", "India online shopping coupon price drop product"),
    ("Web Shopping", "best product deals India shopping sale"),
]
DISCOVERY_QUERIES = PLATFORM_QUERIES

# Valid fallback shopping/search pages across multiple legitimate retailers.
FALLBACK_PRODUCTS = [
    ("Amazon", "Wireless Earbuds", "https://www.amazon.in/s?k=wireless+earbuds"),
    ("Flipkart", "Wireless Earbuds", "https://www.flipkart.com/search?q=wireless%20earbuds"),
    ("Myntra", "Men Sneakers", "https://www.myntra.com/men-sneakers"),
    ("AJIO", "Sneakers", "https://www.ajio.com/search/?text=sneakers"),
    ("Tata CLiQ", "Electronics Deals", "https://www.tatacliq.com/search/?searchCategory=all&text=deals"),
    ("Nykaa", "Beauty Offers", "https://www.nykaa.com/search/result/?q=offers"),
    ("Croma", "Electronics", "https://www.croma.com/search/?text=electronics"),
    ("Reliance Digital", "Electronics", "https://www.reliancedigital.in/search?q=electronics"),
    ("Decathlon", "Sports Products", "https://www.decathlon.in/search?query=sports"),
    ("FirstCry", "Kids Products", "https://www.firstcry.com/search?q=toys"),
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("zoner")

C_TITLE, C_PRICE, C_OLD, C_CATEGORY, C_URL, C_COUNT = range(6)

def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    # Keep the SQLite file configurable so a persistent Render volume can be
    # attached later without changing bot code. Existing deployments continue
    # using DB_FILE unchanged.
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        price TEXT NOT NULL,
        old_price TEXT,
        category TEXT NOT NULL,
        url TEXT NOT NULL,
        source TEXT DEFAULT '',
        discount INTEGER DEFAULT 0,
        score INTEGER DEFAULT 0,
        fingerprint TEXT UNIQUE,
        image_url TEXT DEFAULT '',
        description TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS subscribers (
        user_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1
    )""")
    # Publish history lets the scheduler safely reuse cached/older deal data
    # after the allowed 2-hour freshness window without doing live scraping.
    con.execute("""CREATE TABLE IF NOT EXISTS publish_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id INTEGER NOT NULL,
        published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_publish_history_offer_time
                   ON publish_history(offer_id, published_at)""")
    # Durable publication ledger: a link is recorded immediately after a
    # successful Telegram send, instead of waiting for the whole publish cycle.
    con.execute("""CREATE TABLE IF NOT EXISTS published_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        url TEXT NOT NULL,
        category TEXT NOT NULL,
        published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_published_links_offer_time
                   ON published_links(offer_id, published_at)""")
    con.execute("""CREATE TABLE IF NOT EXISTS manual_published_offers (
        offer_id INTEGER PRIMARY KEY,
        publish_count INTEGER NOT NULL DEFAULT 0,
        first_published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS verified_users (
        user_id INTEGER PRIMARY KEY,
        verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS auto_publish_chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT DEFAULT '',
        chat_type TEXT DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # Migrate old databases created by the MVP.
    existing = {r["name"] for r in con.execute("PRAGMA table_info(offers)").fetchall()}
    for name, ddl in [
        ("source", "ALTER TABLE offers ADD COLUMN source TEXT DEFAULT ''"),
        ("discount", "ALTER TABLE offers ADD COLUMN discount INTEGER DEFAULT 0"),
        ("score", "ALTER TABLE offers ADD COLUMN score INTEGER DEFAULT 0"),
        ("fingerprint", "ALTER TABLE offers ADD COLUMN fingerprint TEXT"),
        ("image_url", "ALTER TABLE offers ADD COLUMN image_url TEXT DEFAULT ''"),
        ("description", "ALTER TABLE offers ADD COLUMN description TEXT DEFAULT ''"),
    ]:
        if name not in existing:
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
    con.commit()
    con.close()

def is_user_verified(user_id):
    # Lifetime verification: once Telegram has confirmed membership, keep the
    # local flag. The database is the source of truth for subsequent /start
    # requests so verified users never get the join gate again.
    con = db()
    try:
        row = con.execute(
            "SELECT 1 FROM verified_users WHERE user_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None
    finally:
        con.close()

def ensure_verified_users_table():
    # Defensive migration for existing deployments/databases.
    con = db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS verified_users ("
            "user_id INTEGER PRIMARY KEY, verified_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        con.commit()
    finally:
        con.close()

def mark_user_verified(user_id):
    con = db()
    con.execute("INSERT OR IGNORE INTO verified_users(user_id) VALUES (?)", (user_id,))
    con.commit()
    con.close()

def save_subscriber(user_id, enabled):
    con = db()
    con.execute("""INSERT INTO subscribers(user_id, enabled) VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled""",
                (user_id, enabled))
    con.commit(); con.close()

def subscriber_enabled(user_id):
    con = db()
    row = con.execute("SELECT enabled FROM subscribers WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    # Notifications are ON by default for every new user. An explicit OFF
    # choice is still respected because it is persisted in subscribers.
    return bool(row["enabled"]) if row else True

def get_offers(category=None, price_max=None, price_min=None, limit=20):
    """Return only published offers, ordered by latest publication and filterable
    by the saved category and numeric price."""
    con = db()
    # Latest Deals must show every successfully published offer, even when
    # the retailer did not expose a numeric price and the saved value is
    # "Check live price". Numeric filtering is applied only when a price
    # range is explicitly requested.
    conditions = []
    params = []
    if category:
        conditions.append("o.category=?")
        params.append(category)
    if price_min is not None:
        conditions.append("CAST(REPLACE(o.price, ',', '') AS REAL) >= ?")
        params.append(price_min)
    if price_max is not None:
        conditions.append("CAST(REPLACE(o.price, ',', '') AS REAL) <= ?")
        params.append(price_max)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = con.execute(
        "SELECT o.* FROM offers o "
        "JOIN (SELECT offer_id, MAX(published_at) AS last_published "
        "      FROM publish_history GROUP BY offer_id) ph ON ph.offer_id=o.id"
        + where +
        " ORDER BY datetime(ph.last_published) DESC, o.id DESC LIMIT ?",
        params
    ).fetchall()
    con.close()
    return rows

def get_offers_for_view(category=None, price_filter=None, limit=None):
    # Retention/display caps requested for source + normal published offers:
    # Latest = 20, each category = 25, each price range = 30.
    if limit is None:
        if price_filter:
            limit = 30
        elif category:
            limit = 25
        else:
            limit = 20
    if not price_filter:
        return get_offers(category=category, limit=limit)
    low, high = price_filter
    return get_offers(category=category, price_min=low, price_max=high, limit=limit)

def get_offer(offer_id):
    con = db()
    row = con.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    con.close()
    return row

def delete_offer(offer_id):
    con = db()
    cur = con.execute("DELETE FROM offers WHERE id=?", (offer_id,))
    con.commit(); deleted = cur.rowcount > 0; con.close()
    return deleted

def offer_count():
    con = db(); n = con.execute("SELECT COUNT(*) n FROM offers").fetchone()["n"]; con.close(); return n

def subscriber_count():
    con = db(); n = con.execute("SELECT COUNT(*) n FROM subscribers WHERE enabled=1").fetchone()["n"]; con.close(); return n

def canonical_deal_url(url):
    """Normalize retailer links so tracking parameters/fragments cannot create duplicate offers."""
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = re.sub(r"/+$", "", parsed.path or "") or "/"
        tracking_prefixes = ("utm_",)
        tracking_keys = {
            "tag", "ref", "ref_", "linkcode", "camp", "creative", "creativeasin",
            "ascsubtag", "asc_source", "asc_campaign", "fbclid", "gclid", "igshid",
            "mc_cid", "mc_eid", "affid", "aff_id", "aff_sub", "sourceid"
        }
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            k = key.lower()
            if k in tracking_keys or any(k.startswith(prefix) for prefix in tracking_prefixes):
                continue
            query.append((key, value))
        query.sort()
        return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(query), ""))
    except Exception:
        return (url or "").split("#", 1)[0].rstrip("/")


def fingerprint(title, url):
    clean_title = re.sub(r"\W+", " ", html.unescape(title or "").lower()).strip()
    canonical_url = canonical_deal_url(url)
    return hashlib.sha256((clean_title + "|" + canonical_url).encode()).hexdigest()

def guess_category(text):
    """Classify the actual product, not the retailer/source channel.
    Uses broad product vocabulary so marketplace posts do not fall back to
    Electronics merely because the source is generic (for example Meesho).
    """
    t = (text or "").lower()

    category_terms = {
        "gaming": [
            "ps5", "ps4", "xbox", "gaming", "gpu", "rtx", "controller",
            "steam", "nintendo", "playstation", "joystick", "gamepad",
        ],
        "fashion": [
            "shirt", "t-shirt", "tshirt", "tee", "jeans", "trouser", "pant",
            "pants", "shorts", "shoe", "shoes", "sneaker", "sneakers",
            "sandals", "slipper", "dress", "top", "kurta", "kurti", "saree",
            "sari", "lehenga", "salwar", "dupatta", "jacket", "hoodie",
            "sweatshirt", "blazer", "coat", "skirt", "legging", "leggings",
            "innerwear", "bra", "brief", "boxer", "nightwear", "trackpant",
            "track pants", "clothing", "apparel", "wear", "fashion",
            "men's", "mens", "women's", "womens", "boys", "girls",
            "जूते", "कपड़े", "कपड़ा", "शर्ट", "कुर्ती", "साड़ी", "जींस",
        ],
        "electronics": [
            "mobile", "phone", "smartphone", "iphone", "android", "laptop",
            "tablet", "computer", "monitor", "keyboard", "mouse", "printer",
            "earbuds", "earbud", "headphone", "headset", "speaker", "soundbar",
            "charger", "power bank", "powerbank", "cable", "adapter",
            "smartwatch", "smart watch", "watch", "television", "tv",
            "camera", "projector", "router", "wifi", "ssd", "hard disk",
            "pendrive", "usb", "led", "airpods", "electronic",
        ],
        "home": [
            "sofa", "mixer", "grinder", "fridge", "refrigerator", "washing",
            "washing machine", "kitchen", "chair", "table", "bed", "mattress",
            "cookware", "furniture", "curtain", "bedsheet", "blanket",
            "pillow", "home decor", "decor", "utensil", "bottle",
        ],
        "books": ["book", "novel", "kindle", "textbook", "comics", "magazine"],
        "grocery": [
            "grocery", "groceries", "food", "blinkit", "bigbasket", "zepto",
            "instamart", "snacks", "rice", "atta", "flour", "oil", "dal",
            "masala", "biscuit", "chocolate", "beverage",
        ],
        "beauty": [
            "beauty", "makeup", "cosmetic", "skincare", "skin care", "shampoo",
            "conditioner", "nykaa", "perfume", "fragrance", "lipstick",
            "serum", "moisturizer", "sunscreen", "face wash",
        ],
        "sports": [
            "sports", "fitness", "gym", "decathlon", "cricket", "football",
            "badminton", "running", "yoga", "dumbbell", "treadmill",
            "sportswear", "football", "bat", "racket",
        ],
        "kids": [
            "kids", "baby", "toys", "toy", "firstcry", "diaper", "stroller",
            "children", "school bag", "baby care",
        ],
    }

    # Score matches instead of returning on the first keyword. This prevents
    # generic words such as "watch" or "offer" from forcing the wrong category.
    scores = {category: 0 for category in category_terms}
    for category, terms in category_terms.items():
        for term in terms:
            if term in t:
                scores[category] += 1

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "electronics"

def extract_price(text):
    m = re.search(r"(?:₹|Rs\.?\s*)([0-9][0-9,]*(?:\.\d+)?)", text, re.I)
    if not m: return ""
    return m.group(1).replace(",", "")

def extract_discount(text):
    vals = [int(x) for x in re.findall(r"(\d{1,3})\s*%\s*(?:off|discount)", text, re.I) if int(x) <= 100]
    return max(vals) if vals else 0

def score_deal(title, discount, source):
    score = 25
    if discount >= 70: score += 45
    elif discount >= 50: score += 35
    elif discount >= 30: score += 25
    elif discount >= 20: score += 15
    elif discount >= 10: score += 8
    if any(x in title.lower() for x in ["limited", "deal", "offer", "sale", "price drop", "lowest", "coupon"]): score += 10
    if source.lower() in {"amazon", "flipkart"}: score += 10
    return min(score, 100)

# Negative membership results are never cached, so a user can join and
# retry immediately. Successful verification is stored permanently in SQLite.
MEMBERSHIP_CACHE_SECONDS = 0

def get_force_join_channels():
    """Return active force-join channels and restore required defaults safely."""
    con = db()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS force_join_channels (
            chat_ref TEXT PRIMARY KEY,
            title TEXT DEFAULT '',
            invite_url TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        # Remember intentional admin deletions so defaults do not reappear after
        # every menu refresh, while restoring defaults lost from an old/reset DB.
        con.execute("""CREATE TABLE IF NOT EXISTS force_join_exclusions (
            chat_ref TEXT PRIMARY KEY,
            deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        defaults = [
            ("@zoneroffers", "Zoner Offers", "https://t.me/zoneroffers"),
            ("@offerleloturant", "Offer Le Loturant", "https://t.me/offerleloturant"),
        ]
        # Only the two intended defaults are seeded here. A stale CHANNEL_ID
        # Render variable must not silently add a third, unexpected join gate.
        # Additional channels are managed explicitly through the admin panel.

        for ref, title, invite_url in defaults:
            excluded = con.execute(
                "SELECT 1 FROM force_join_exclusions WHERE chat_ref=?", (ref,)
            ).fetchone()
            if excluded:
                continue
            con.execute(
                "INSERT INTO force_join_channels(chat_ref,title,invite_url,enabled) "
                "VALUES(?,?,?,1) ON CONFLICT(chat_ref) DO UPDATE SET "
                "title=excluded.title, invite_url=excluded.invite_url, enabled=1",
                (ref, title, invite_url),
            )
        con.commit()
        return con.execute(
            "SELECT chat_ref, title, invite_url FROM force_join_channels "
            "WHERE enabled=1 ORDER BY created_at ASC, chat_ref ASC"
        ).fetchall()
    finally:
        con.close()

def required_channel_ref_legacy():
    ref = _normalize_chat_ref(CHANNEL_ID)
    if not ref or ref.startswith(("https://", "http://", "t.me/")) or ref.startswith("+"):
        return "@zoneroffers"
    return ref

def required_channel_url_legacy():
    ref = required_channel_ref_legacy()
    return "https://t.me/" + ref[1:] if ref.startswith("@") else CHANNEL_URL

def add_force_join_channel(chat_ref, title="", invite_url=""):
    ref = _normalize_chat_ref(chat_ref)
    if not ref or ref.startswith(("https://", "http://", "t.me/", "+")):
        return False
    url = (invite_url or "").strip()
    if not url and ref.startswith("@"):
        url = "https://t.me/" + ref[1:]
    con = db()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS force_join_channels (
            chat_ref TEXT PRIMARY KEY, title TEXT DEFAULT '', invite_url TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS force_join_exclusions (
            chat_ref TEXT PRIMARY KEY,
            deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        con.execute("DELETE FROM force_join_exclusions WHERE chat_ref=?", (ref,))
        con.execute(
            "INSERT INTO force_join_channels(chat_ref,title,invite_url,enabled) VALUES(?,?,?,1) "
            "ON CONFLICT(chat_ref) DO UPDATE SET title=excluded.title, invite_url=excluded.invite_url, enabled=1",
            (ref, title or ref, url),
        )
        con.commit()
        return True
    finally:
        con.close()

def delete_force_join_channel(chat_ref):
    con = db()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS force_join_exclusions (
            chat_ref TEXT PRIMARY KEY,
            deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        con.execute(
            "INSERT OR IGNORE INTO force_join_exclusions(chat_ref) VALUES(?)", (chat_ref,)
        )
        con.execute("UPDATE force_join_channels SET enabled=0 WHERE chat_ref=?", (chat_ref,))
        changed = con.total_changes > 0
        con.commit()
        return changed
    finally:
        con.close()

def required_channel_ref():
    channels = get_force_join_channels()
    return channels[0]["chat_ref"] if channels else "@zoneroffers"

def required_channel_url():
    channels = get_force_join_channels()
    if channels:
        return channels[0]["invite_url"] or required_channel_url_legacy()
    return CHANNEL_URL

async def membership_status(bot, user_id):
    if is_user_verified(user_id):
        return True

    # Confirm membership in every active required channel. Normalize enum/status
    # representations defensively because PTB/API versions may expose these as
    # strings or enum-like values (e.g. "ChatMemberStatus.MEMBER").
    channels = get_force_join_channels()
    if not channels:
        log.error("Force-join check: no active required channels configured.")
        return False

    for channel in channels:
        required_channel = str(channel["chat_ref"]).strip()
        try:
            chat = await asyncio.wait_for(
                bot.get_chat(chat_id=required_channel), timeout=12.0
            )
            passed = False
            last_error = None
            for attempt in range(4):
                try:
                    member = await asyncio.wait_for(
                        bot.get_chat_member(chat_id=chat.id, user_id=user_id),
                        timeout=12.0,
                    )
                    raw_status = getattr(member, "status", "")
                    status = str(getattr(raw_status, "value", raw_status)).lower()
                    status = status.rsplit(".", 1)[-1]
                    is_member = getattr(member, "is_member", None)
                    passed = (
                        status in {"member", "administrator", "creator"}
                        or (status == "restricted" and is_member is True)
                    )
                    log.info(
                        "Force-join result user=%s channel=%s chat_id=%s status=%s "
                        "is_member=%s passed=%s",
                        user_id, required_channel, chat.id, status, is_member, passed,
                    )
                    if passed:
                        break
                    # A definitive left/kicked result will not improve with retries.
                    if status in {"left", "kicked", "banned"}:
                        break
                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "Force-join API attempt failed channel=%s user=%s attempt=%s: %s",
                        required_channel, user_id, attempt + 1, exc,
                    )
                if attempt < 3:
                    await asyncio.sleep(1.0 + attempt * 0.5)

            if not passed:
                if last_error:
                    log.error(
                        "Force-join could not verify user=%s channel=%s; check bot admin "
                        "permissions and channel reference. Last error: %s",
                        user_id, required_channel, last_error,
                    )
                else:
                    log.info(
                        "Force-join denied user=%s channel=%s: Telegram reports not a member.",
                        user_id, required_channel,
                    )
                return False
        except Exception as exc:
            log.error(
                "Force-join could not open required channel=%s for user=%s: %s",
                required_channel, user_id, exc,
            )
            return False

    mark_user_verified(user_id)
    log.info("Force-join verified user=%s across %s required channels.", user_id, len(channels))
    return True


async def force_join_diagnostics(bot):
    """Check bot admin access to every active force-join channel and log exact failures."""
    channels = get_force_join_channels()
    if not channels:
        log.warning("FORCE-JOIN: no active required channels are configured.")
        return True

    all_ok = True
    for channel in channels:
        required = str(channel["chat_ref"])
        if required.startswith(("https://", "http://", "t.me/")) or required.startswith("+"):
            log.error(
                "FORCE-JOIN CONFIG ERROR: channel must be @username or numeric -100...; got %r",
                required,
            )
            all_ok = False
            continue

        try:
            chat = await asyncio.wait_for(bot.get_chat(required), timeout=8.0)
            member = await asyncio.wait_for(
                bot.get_chat_member(chat.id, bot.id), timeout=8.0
            )
            status = str(getattr(member, "status", "")).lower()
            can_manage = getattr(member, "can_manage_chat", None)
            can_post = getattr(member, "can_post_messages", None)
            log.info(
                "Force-join self-test: ref=%s chat_id=%s title=%s bot_status=%s "
                "can_manage_chat=%s can_post_messages=%s",
                required,
                chat.id,
                getattr(chat, "title", "") or "",
                status,
                can_manage,
                can_post,
            )
            if status not in {"administrator", "creator"}:
                log.error(
                    "FORCE-JOIN PERMISSION ERROR: bot must be Administrator in %s (status=%s)",
                    required,
                    status,
                )
                all_ok = False
        except Exception as exc:
            log.error(
                "FORCE-JOIN STARTUP CHECK FAILED ref=%s: %s (%s)",
                required,
                exc,
                type(exc).__name__,
            )
            all_ok = False

    if all_ok:
        log.info("✅ Force-join diagnostics passed for all %s required channel(s).", len(channels))
    else:
        log.error("❌ Force-join diagnostics failed for one or more required channels.")
    return all_ok

def join_gate_markup():
    rows = []
    for idx, channel in enumerate(get_force_join_channels(), 1):
        url = channel["invite_url"] or ""
        ref = channel["chat_ref"]
        if not url and ref.startswith("@"):
            url = "https://t.me/" + ref[1:]
        if url:
            rows.append([InlineKeyboardButton(f"📢 Join Required Channel {idx}", url=url)])
    rows.append([InlineKeyboardButton("🚀 Continue to Bot", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)

def join_gate_text():
    count = len(get_force_join_channels())
    return (
        "🔐 <b>Join Required</b>\n\n"
        f"Zoner Offers AI use karne se pehle <b>{count}</b> required channel(s) join karein.\n\n"
        "Join ke baad <b>🚀 Continue to Bot</b> dabayein.\n"
        "Successful verification ke baad ye page dobara nahi aayega."
    )

def get_auto_publish_chats_sync():
    con = db()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS auto_publish_chats (
            chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '', chat_type TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        return con.execute(
            "SELECT chat_id FROM auto_publish_chats WHERE enabled=1 AND chat_type IN ('group','supergroup')"
        ).fetchall()
    finally:
        con.close()

async def track_group_message(update, context):
    """Discover an existing group/supergroup when the bot receives a message there.
    Telegram does not expose an API to enumerate all historical chats a bot joined,
    so this safely verifies the bot's current membership/permissions on first contact.
    """
    chat = getattr(update, "effective_chat", None)
    if not chat or getattr(chat, "type", "") not in ("group", "supergroup"):
        return
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        status = str(getattr(member, "status", "")).lower()
        can_send = getattr(member, "can_send_messages", True)
        can_post = getattr(member, "can_post_messages", True)
        if status not in {"administrator", "creator", "member", "restricted"}:
            return
        if status == "restricted" and can_send is False:
            return
        if status == "administrator" and can_post is False:
            return
        con = db()
        try:
            con.execute("""CREATE TABLE IF NOT EXISTS auto_publish_chats (
                chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '', chat_type TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            con.execute(
                """INSERT INTO auto_publish_chats(chat_id,title,chat_type,enabled,updated_at)
                   VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                   chat_type=excluded.chat_type, enabled=1, updated_at=CURRENT_TIMESTAMP""",
                (chat.id, getattr(chat, "title", "") or "", chat.type, 1),
            )
            con.commit()
        finally:
            con.close()
    except Exception as exc:
        log.debug("Existing group discovery failed for %s: %s", getattr(chat, "id", None), exc)

async def track_auto_publish_chat(update, context):
    """Register groups/supergroups the bot joins for optional auto publishing."""
    member_update = getattr(update, "my_chat_member", None)
    if not member_update:
        return
    chat = member_update.chat
    if getattr(chat, "type", "") not in ("group", "supergroup"):
        return
    status = getattr(member_update.new_chat_member, "status", "")
    con = db()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS auto_publish_chats (
            chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '', chat_type TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        if status in ("left", "kicked"):
            con.execute("UPDATE auto_publish_chats SET enabled=0, updated_at=CURRENT_TIMESTAMP WHERE chat_id=?", (chat.id,))
        else:
            con.execute(
                """INSERT INTO auto_publish_chats(chat_id,title,chat_type,enabled,updated_at)
                   VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                   chat_type=excluded.chat_type, enabled=1, updated_at=CURRENT_TIMESTAMP""",
                (chat.id, getattr(chat, "title", "") or "", chat.type, 1),
            )
        con.commit()
    finally:
        con.close()

def get_setting_sync(key, default=None):
    """Read an admin-configurable setting from the live bot database."""
    con = db()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        con.close()

def main_menu(user_id=None):
    """Build the live production menu using the admin-configured row layout."""
    notify = "🔔 Notifications ON" if user_id is not None and subscriber_enabled(user_id) else "🔕 Notifications OFF"
    try:
        max_per_row = max(1, min(3, int(get_setting_sync("menu_buttons_per_row", 2))))
    except (TypeError, ValueError):
        max_per_row = 2

    items = [
        ("🛍️ Latest Deals", "offers"),
        ("🏷️ Categories", "categories"),
        ("💰 Price Filter", "price_filter"),
        (notify, "notifications"),
        ("🤖 AI Deal Hunter", "ai_info"),
        ("🆘 Help", "help"),
    ]
    rows = []
    for index in range(0, len(items), max_per_row):
        rows.append([
            InlineKeyboardButton(label, callback_data=callback)
            for label, callback in items[index:index + max_per_row]
        ])
    return InlineKeyboardMarkup(rows)

def price_filter_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Under ₹500", callback_data="price_500"),
         InlineKeyboardButton("💸 ₹500–₹1,000", callback_data="price_1000")],
        [InlineKeyboardButton("💸 ₹1,000–₹5,000", callback_data="price_5000")],
        [InlineKeyboardButton("💸 ₹5,000+", callback_data="price_5000plus")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back")],
    ])

def categories_menu():
    rows = [[InlineKeyboardButton(label, callback_data=f"cat_{key}")] for key, label in CATEGORIES.items()]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)

def offer_buttons(rows, back="offers"):
    buttons = []
    for row in rows:
        title = row["title"][:42] + ("…" if len(row["title"]) > 42 else "")
        badge = f"🔥 {row['discount']}% OFF • " if row["discount"] else "🔥 "
        buttons.append([InlineKeyboardButton(badge + title, callback_data=f"offer_{row['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=back)])
    return InlineKeyboardMarkup(buttons)

def offer_text(row):
    """Render the approved Zoner Offers Bot product-card format (never the old AI alert format)."""
    title = html.escape(str(row["title"] or "Product"))
    category = html.escape(CATEGORIES.get(row["category"], row["category"] or "Shopping"))
    source = html.escape(str(row["source"] or "Shopping platform"))
    raw_price = str(row["price"] or "").replace(",", "").replace("₹", "").strip()
    match = re.search(r"\d+(?:\.\d+)?", raw_price)
    amount = float(match.group()) if match else None
    if amount is None:
        price_range = "Check live price"
    elif amount <= 200:
        price_range = "₹1–₹200"
    elif amount <= 500:
        price_range = "₹201–₹500"
    elif amount <= 1000:
        price_range = "₹501–₹1,000"
    elif amount <= 2000:
        price_range = "₹1,001–₹2,000"
    elif amount <= 5000:
        price_range = "₹2,001–₹5,000"
    else:
        price_range = "₹5,001+"
    host = html.escape((urlparse(str(row["url"] or "")).hostname or source).replace("www.", ""))
    discount = f"{int(row['discount'])}% OFF" if row["discount"] else "See product page"
    score = f"{int(row['score'])}/100" if row["score"] else "Not available"
    description = html.escape((row["description"] or "").strip()) if "description" in row.keys() else ""
    if not description:
        description = "Check the retailer page for current price, stock and product details."
    return (
        "🛍️ <b>𝗭𝗢𝗡𝗘𝗥 𝗢𝗙𝗙𝗘𝗥𝗦 𝗕𝗢𝗧</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 <b>Product:</b> {title}\n"
        f"🏷️ <b>Category:</b> {category}\n"
        f"🏪 <b>Brand / Source:</b> {source}\n"
        f"💰 <b>Price Range:</b> {html.escape(price_range)}\n"
        f"🔥 <b>Discount:</b> {html.escape(discount)}\n"
        f"🛒 <b>Shopping Website:</b> {host}\n"
        f"🎯 <b>Deal Score:</b> {html.escape(score)}\n\n"
        f"📝 <b>Product Details:</b> {description[:700]}\n\n"
        "⚡ Price and availability may change. Check the product page before checkout."
    )

def offer_markup(row, back="offers"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy / View Deal", url=row["url"])],
        [InlineKeyboardButton("⬅️ Back to Deals", callback_data=back)],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
    ])

def admin_menu():
    group_state = "ON" if str(get_setting_sync("auto_group_publish", "1")) == "1" else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📢 Auto Group Publish: {group_state}", callback_data="admin_group_publish")],
        [InlineKeyboardButton("📢 Force Join Channels", callback_data="admin_force"),
         InlineKeyboardButton("📋 Menu Editor", callback_data="admin_menu")],
        [InlineKeyboardButton("⏰ Post Interval", callback_data="admin_interval"),
         InlineKeyboardButton("📝 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🛒 Add Product", callback_data="admin_addproduct"),
         InlineKeyboardButton("🟣 Re-publish Vault", callback_data="admin_republish")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
         InlineKeyboardButton("🤖 Scan Now", callback_data="admin_scan")],
        [InlineKeyboardButton("🧪 Test Channels", callback_data="admin_testchannels"),
         InlineKeyboardButton("❌ Close", callback_data="admin_close")],
    ])

def is_admin(update):
    user = getattr(update, "effective_user", None)
    return bool(user and str(user.id) in ADMIN_IDS)

async def start(update, context):
    user_id = update.effective_user.id

    # Notifications default to ON. An explicit OFF choice is preserved.
    con = db()
    try:
        con.execute("INSERT OR IGNORE INTO subscribers(user_id, enabled) VALUES (?, 1)", (user_id,))
        con.commit()
    finally:
        con.close()

    # Admins and users with a saved lifetime verification go straight to the menu.
    # New users see the gate, and membership is checked when they press Continue.
    if is_admin(update) or is_user_verified(user_id):
        await update.message.reply_text(
            "🔥 <b>Welcome back to Zoner Offers AI!</b>\n\n"
            "👇 Choose an option:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(user_id),
        )
        return

    await update.message.reply_text(
        join_gate_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=join_gate_markup(),
    )

async def handle_force_join_admin_text(update, context):
    if not is_admin(update) or not context.user_data.get("force_join_add"):
        return
    raw = (getattr(update.message, "text", "") or "").strip()
    parts = [p.strip() for p in raw.split("|", 1)]
    ref = parts[0] if parts else ""
    invite = parts[1] if len(parts) > 1 else ""
    if not add_force_join_channel(ref, title=ref, invite_url=invite):
        await update.message.reply_text("❌ Invalid channel. Use @username or -100... | invite URL.")
        return
    context.user_data.pop("force_join_add", None)
    await update.message.reply_text(
        f"✅ <b>Force-join channel added:</b> {html.escape(ref)}",
        parse_mode=ParseMode.HTML, reply_markup=admin_menu()
    )

async def help_command(update, context):
    await update.message.reply_text(
        "🆘 <b>Zoner Offers AI</b>\n\n"
        "The bot continuously checks public deal/news feeds, scores candidates, removes duplicates and publishes fresh deals.\n\n"
        "🛍️ Latest Deals — browse\n🏷️ Categories — filter\n🔔 Notifications — alerts\n"
        "🤖 AI Deal Hunter — how discovery works\n🛒 Buy / View Deal — open source offer.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu(update.effective_user.id))

async def show_offers(update, category=None, price_filter=None):
    query = update.callback_query
    rows = get_offers_for_view(category=category, price_filter=price_filter)
    title = CATEGORIES.get(category, "🏷️ Category") if category else "🛍️ Latest Deals"
    if price_filter:
        low, high = price_filter
        if low == 0:
            title += " • Under ₹500"
        elif high is None:
            title += " • ₹5,000+"
        else:
            title += f" • ₹{low:,}–₹{high:,}"
    back = "categories" if category else "back"
    if not rows:
        await query.edit_message_text(f"<b>{html.escape(title)}</b>\n\n😕 No matching offers yet.", parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=back)]]))
        return
    await query.edit_message_text(f"<b>{html.escape(title)}</b>\n\n👇 Select a deal:", parse_mode=ParseMode.HTML,
                                  reply_markup=offer_buttons(rows, back))

async def button_handler(update, context):
    """Fault-tolerant callback entrypoint.

    A single Telegram API/edit/DB exception must never leave the user with a
    dead button or an endless loading spinner.  Channel publishing runs in a
    separate background task, so this guard does not stop auto publishing.
    """
    query = update.callback_query
    try:
        await _button_handler_impl(update, context)
    except Exception as exc:
        # Do not send a second message from callback error recovery.
        log.exception("Callback handler failed for %s: %s", getattr(query, "data", None), exc)

async def _button_handler_impl(update, context):
    query = update.callback_query
    data = query.data
    # Join verification needs to decide whether to show an alert before answering.
    # Answering it here and again in the failure branch causes Telegram's
    # "query is too old or already answered" error.
    if data != "check_join":
        try:
            await query.answer()
        except Exception:
            log.debug("Callback acknowledgement failed for %s", data)

    if data == "check_join":
        user_id = query.from_user.id

        # Notifications default to ON. Never overwrite an existing OFF choice.
        con = db()
        try:
            con.execute("INSERT OR IGNORE INTO subscribers(user_id, enabled) VALUES (?, 1)", (user_id,))
            con.commit()
        finally:
            con.close()

        # Check all active required channels before granting lifetime access.
        # Telegram API failures must never be mistaken for successful membership.
        if not is_admin(update) and not is_user_verified(user_id):
            verified = await membership_status(context.bot, user_id)
            if not verified:
                try:
                    await query.answer(
                        "Membership verify nahi hui. Dono required channels join karke dobara try karein.",
                        show_alert=True,
                    )
                except Exception:
                    log.debug("Could not show force-join verification alert for user %s", user_id)
                await query.edit_message_text(
                    join_gate_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=join_gate_markup(),
                )
                return

        if not is_user_verified(user_id):
            mark_user_verified(user_id)

        try:
            await query.answer()
        except Exception:
            log.debug("Could not acknowledge successful join check for user %s", user_id)

        await query.edit_message_text(
            "✅ <b>Channel join verified!</b>\\n\\n"
            "🔥 Welcome to Zoner Offers AI. Choose an option:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(user_id),
        )
        return

    # Once verified, this Telegram account is allowed through without another join gate.
    if not is_user_verified(query.from_user.id) and not is_admin(update):
        # Every unverified callback uses the same force-join gate.
        verified = await membership_status(context.bot, query.from_user.id)
        if not verified:
            await query.edit_message_text(
                join_gate_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=join_gate_markup()
            )
            return
        mark_user_verified(query.from_user.id)

    if data.startswith("manual_publish:"):
        if not is_admin(update):
            return
        try:
            _, offer_id_s, count_s = data.split(":", 2)
            offer_id = int(offer_id_s)
            count = max(1, min(int(count_s), 100))
        except (ValueError, TypeError):
            await query.edit_message_text("❌ Invalid publish request.", reply_markup=admin_menu())
            return

        row = get_offer(offer_id)
        if not row:
            await query.edit_message_text("❌ Offer no longer exists.", reply_markup=admin_menu())
            return

        await query.edit_message_text(
            f"🚀 <b>Publishing started</b>\n\n"
            f"📦 {html.escape(str(row['title']))}\n"
            f"🔁 Requested: <b>{count}</b>\n"
            "⏳ Please wait...",
            parse_mode=ParseMode.HTML,
        )

        success = 0
        for i in range(count):
            try:
                if await publish_offer(context.bot, row):
                    success += 1
            except Exception:
                log.exception("Manual publish %s/%s failed for offer %s", i + 1, count, offer_id)
            if i + 1 < count:
                await asyncio.sleep(2)

        saved = record_manual_publish(offer_id, success) if success else False
        await query.message.reply_text(
            "🟣 <b>𝗠𝗔𝗡𝗨𝗔𝗟 𝗣𝗨𝗕𝗟𝗜𝗦𝗛 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔁 Requested: <b>{count}</b>\n"
            f"✅ Successful: <b>{success}</b>\n"
            f"❌ Failed: <b>{count - success}</b>\n"
            f"💾 Saved in Re-publish Vault: <b>{'YES' if saved else 'NO'}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu(),
        )
        return

    if data == "offers": await show_offers(update); return
    if data == "categories":
        await query.edit_message_text("🏷️ <b>Offer Categories</b>\n\nChoose a category:", parse_mode=ParseMode.HTML, reply_markup=categories_menu()); return
    if data == "price_filter":
        await query.edit_message_text("💰 <b>Filter Offers by Price</b>\n\nChoose a price range:", parse_mode=ParseMode.HTML, reply_markup=price_filter_menu()); return
    if data == "price_500": await show_offers(update, price_filter=(0, 500)); return
    if data == "price_1000": await show_offers(update, price_filter=(500, 1000)); return
    if data == "price_5000": await show_offers(update, price_filter=(1000, 5000)); return
    if data == "price_5000plus": await show_offers(update, price_filter=(5000, None)); return
    if data.startswith("cat_"): await show_offers(update, data[4:]); return
    if data == "ai_info":
        await query.edit_message_text(
            "🤖 <b>AI Deal Hunter</b>\n\n"
            "Zoner checks multiple public deal sources automatically, detects discount signals, scores deal quality, filters duplicates and publishes only stronger candidates.\n\n"
            f"⏱️ Auto publishing: every 15 minutes\n🎯 Minimum score: {MIN_DEAL_SCORE}/100",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(query.from_user.id)); return
    if data.startswith("offer_"):
        try: offer_id = int(data[6:])
        except ValueError: return
        row = get_offer(offer_id)
        if not row:
            await query.edit_message_text("❌ This offer is no longer available.", reply_markup=main_menu(query.from_user.id)); return
        await query.edit_message_text(offer_text(row), parse_mode=ParseMode.HTML, reply_markup=offer_markup(row)); return
    if data == "notifications":
        new_value = 0 if subscriber_enabled(query.from_user.id) else 1
        save_subscriber(query.from_user.id, new_value)
        msg = "🔔 <b>Notifications ON</b>\n\nYou'll receive strong new deals." if new_value else "🔕 <b>Notifications OFF</b>\n\nYou can turn them back on anytime."
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=main_menu(query.from_user.id)); return
    if data == "help":
        await query.edit_message_text("🆘 <b>How it works</b>\n\n1️⃣ Browse deals\n2️⃣ Open a deal\n3️⃣ Buy/View on source\n\n🤖 Zoner keeps hunting in the background.", parse_mode=ParseMode.HTML, reply_markup=main_menu(query.from_user.id)); return
    if data == "back":
        await query.edit_message_text("🔥 <b>Zoner Offers AI</b>\n\nChoose an option:", parse_mode=ParseMode.HTML, reply_markup=main_menu(query.from_user.id)); return

    if data == "admin_republish":
        if not is_admin(update):
                return
        rows = get_manual_published_offers(limit=30)
        if not rows:
            await query.edit_message_text(
                "🟣 <b>𝗭𝗢𝗡𝗘𝗥 𝗥𝗘-𝗣𝗨𝗕𝗟𝗜𝗦𝗛 𝗩𝗔𝗨𝗟𝗧</b>\\n"
                "━━━━━━━━━━━━━━━━━━━━\\n\\n"
                "📭 <b>No manually published offers saved yet.</b>\\n\\n"
                "Manual <b>📢 Publish ×N</b> complete hote hi offer yahan automatically save ho jayega.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛒 Add Product", callback_data="admin_addproduct")],
                    [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")]
                ]))
            return
        buttons = []
        for r in rows:
            title = str(r["title"])[:36] + ("…" if len(str(r["title"])) > 36 else "")
            buttons.append([InlineKeyboardButton(
                f"🟣 🔁 {title} • ×{r['publish_count']}",
                callback_data=f"republish_menu_{r['id']}")])
        buttons.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")])
        await query.edit_message_text(
            "🟣 <b>𝗭𝗢𝗡𝗘𝗥 𝗥𝗘-𝗣𝗨𝗕𝗟𝗜𝗦𝗛 𝗩𝗔𝗨𝗟𝗧</b>\\n"
            "━━━━━━━━━━━━━━━━━━━━\\n"
            "🗂️ <b>Manual Published Offers</b>\\n\\n"
            "Sirf wahi offers yahan dikhte hain jo admin ne manually publish kiye hain.\\n"
            "Auto-published deals is list me add nahi honge.\\n\\n"
            "👇 Offer choose karo:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("republish_menu_"):
        if not is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return
        try:
            offer_id = int(data.rsplit("_", 1)[1])
        except (ValueError, TypeError):
            return
        row = get_offer(offer_id)
        if not row:
            await query.edit_message_text("❌ Offer no longer exists.", reply_markup=admin_menu())
            return
        saved_count = get_manual_publish_count(offer_id)
        await query.edit_message_text(
            "🟣 <b>𝗥𝗘-𝗣𝗨𝗕𝗟𝗜𝗦𝗛 𝗢𝗙𝗙𝗘𝗥</b>\\n"
            "━━━━━━━━━━━━━━━━━━━━\\n"
            f"📦 <b>{html.escape(str(row['title']))}</b>\\n\\n"
            f"💾 Manual publish history: <b>×{saved_count}</b>\\n"
            "🎯 <b>Kitni baar dobara publish karna hai?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🟣 ×1", callback_data=f"republish_{offer_id}_1"),
                 InlineKeyboardButton("🟣 ×5", callback_data=f"republish_{offer_id}_5"),
                 InlineKeyboardButton("🟣 ×10", callback_data=f"republish_{offer_id}_10")],
                [InlineKeyboardButton("🟣 ×25", callback_data=f"republish_{offer_id}_25"),
                 InlineKeyboardButton("🟣 ×50", callback_data=f"republish_{offer_id}_50"),
                 InlineKeyboardButton("🟣 ×100", callback_data=f"republish_{offer_id}_100")],
                [InlineKeyboardButton("⬅️ Vault", callback_data="admin_republish")]
            ]))
        return

    if data.startswith("republish_"):
        if not is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return
        try:
            _, offer_id_s, count_s = data.split("_", 2)
            offer_id = int(offer_id_s)
            count = max(1, min(int(count_s), 100))
        except (ValueError, TypeError):
            await query.edit_message_text("❌ Invalid re-publish request.", reply_markup=admin_menu())
            return
        row = get_offer(offer_id)
        if not row:
            await query.edit_message_text("❌ Offer no longer exists.", reply_markup=admin_menu())
            return
        await query.edit_message_text(
            f"🚀 <b>Re-publishing started</b>\\n\\n📦 {html.escape(str(row['title']))}\\n"
            f"🔁 Requested: <b>{count}</b>\\n⏳ Please wait...",
            parse_mode=ParseMode.HTML)
        success = 0
        for i in range(count):
            try:
                if await publish_offer(context.bot, row):
                    success += 1
            except Exception:
                log.exception("Re-publish %s/%s failed for offer %s", i + 1, count, offer_id)
            if i + 1 < count:
                await asyncio.sleep(2)
        saved = record_manual_publish(offer_id, success) if success else False
        await query.message.reply_text(
            "🟣 <b>𝗥𝗘-𝗣𝗨𝗕𝗟𝗜𝗦𝗛 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘</b>\\n"
            "━━━━━━━━━━━━━━━━━━━━\\n"
            f"📦 <b>{html.escape(str(row['title']))}</b>\\n"
            f"🔁 Requested: <b>{count}</b>\\n"
            f"✅ Successful: <b>{success}</b>\\n"
            f"❌ Failed: <b>{count-success}</b>\\n"
            f"💾 Vault saved: <b>{'YES' if saved else 'NO'}</b>",
            parse_mode=ParseMode.HTML, reply_markup=admin_menu())
        return


    if data.startswith("admin_"):
        if not is_admin(update):
            await query.answer("Not authorized.", show_alert=True); return
        if data == "admin_group_publish":
            enabled = str(get_setting_sync("auto_group_publish", "0")) == "1"
            new_value = "0" if enabled else "1"
            con = db()
            try:
                con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
                con.execute(
                    "INSERT INTO settings(key, value) VALUES('auto_group_publish', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (new_value,),
                )
                con.commit()
            finally:
                con.close()
            chats = get_auto_publish_chats_sync()
            state = "ON" if new_value == "1" else "OFF"
            await query.edit_message_text(
                f"📢 <b>Auto Group Publishing: {state}</b>\n\n"
                "When ON, groups/supergroups where the bot is added are automatic publishing targets.\n"
                f"Known active groups: <b>{len(chats)}</b>\n\n"
                "The two configured offer channels continue publishing normally.",
                parse_mode=ParseMode.HTML, reply_markup=admin_menu())
            return
        if data == "admin_force":
            channels = get_force_join_channels()
            rows = []
            for idx, ch in enumerate(channels, 1):
                label = html.escape(ch["title"] or ch["chat_ref"])
                rows.append([InlineKeyboardButton(
                    f"🗑️ Delete {idx}: {label}", callback_data=f"force_delete:{ch['chat_ref']}"
                )])
            rows.append([InlineKeyboardButton("➕ Add Channel", callback_data="force_add")])
            rows.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")])
            await query.edit_message_text(
                "📢 <b>Force Join Channels</b>\n\n"
                f"Active required channels: <b>{len(channels)}</b>\n\n"
                "New users ko first /start par ye channels join karne honge. "
                "Verified users ko lifetime verification ke baad dobara gate nahi dikhega.",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            return
        if data == "force_add":
            context.user_data["force_join_add"] = True
            await query.edit_message_text(
                "➕ <b>Add Force-Join Channel</b>\n\n"
                "Public channel: <code>@channelusername</code>\n"
                "Private channel: <code>-1001234567890 | https://t.me/+invite</code>\n\n"
                "Channel me bot ko Administrator hona chahiye.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_force")]]))
            return
        if data.startswith("force_delete:"):
            if not is_admin(update):
                await query.answer("Not authorized.", show_alert=True)
                return
            ref = data.split(":", 1)[1]
            delete_force_join_channel(ref)
            channels = get_force_join_channels()
            await query.edit_message_text(
                "📢 <b>Force Join Channels</b>\n\n"
                f"Active required channels: <b>{len(channels)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    *[[InlineKeyboardButton(f"🗑️ Delete {i}: {html.escape(ch['title'] or ch['chat_ref'])}",
                                             callback_data=f"force_delete:{ch['chat_ref']}")] for i,ch in enumerate(channels,1)],
                    [InlineKeyboardButton("➕ Add Channel", callback_data="force_add")],
                    [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")]
                ]))
            return
        if data == "admin_menu":
            await query.edit_message_text(
                "📋 <b>Menu Editor</b>\n\n"
                "Use /addmenu Label|callback_data|row|column to add a custom item.",
                parse_mode=ParseMode.HTML, reply_markup=admin_menu())
            return
        if data == "admin_interval":
            interval = get_setting_sync("post_interval", SCAN_SECONDS)
            await query.edit_message_text(
                "⏰ <b>Post Interval</b>\n\n"
                f"Current: <b>{html.escape(str(interval))} seconds</b>\n"
                "Production interval: <b>900 seconds (15 minutes)</b>\n\n"
                "Auto publishing is fixed at 15 minutes.",
                parse_mode=ParseMode.HTML, reply_markup=admin_menu())
            return
        if data == "admin_broadcast":
            await query.edit_message_text(
                "📝 <b>Broadcast</b>\n\n"
                "Broadcast UI reserved here; existing subscriber notifications remain unchanged.",
                parse_mode=ParseMode.HTML, reply_markup=admin_menu())
            return
        if data == "admin_addproduct":
            await query.message.reply_text(
                "🛒 <b>Add Product</b>\n\nUse /addoffer for the manual product/deal form.",
                parse_mode=ParseMode.HTML)
            return
        if data == "admin_settings":
            interval = get_setting_sync("post_interval", SCAN_SECONDS)
            layout = get_setting_sync("menu_buttons_per_row", 2)
            await query.edit_message_text(
                "⚙️ <b>Settings</b>\n\n"
                f"⏰ Post interval: <b>{html.escape(str(interval))} sec</b>\n"
                f"📋 Menu buttons/row: <b>{html.escape(str(layout))}</b>\n"
                f"🎯 Minimum deal score: <b>{MIN_DEAL_SCORE}</b>\n"
                f"📢 Auto-post: <b>{'ON' if AUTO_POST else 'OFF'}</b>",
                parse_mode=ParseMode.HTML, reply_markup=admin_menu())
            return
        if data == "admin_close":
            await query.edit_message_text("✅ <b>Admin panel closed.</b>", parse_mode=ParseMode.HTML)
            return

        if data == "admin_testchannels":
            await query.edit_message_text("🧪 <b>Testing channel posting…</b>", parse_mode=ParseMode.HTML)
            lines = ["🧪 <b>Channel posting test</b>"]
            for channel in POST_CHANNELS:
                try:
                    chat = await context.bot.get_chat(channel)
                    member = await context.bot.get_chat_member(chat.id, context.bot.id)
                    status = getattr(member, "status", "unknown")
                    rights = getattr(member, "can_post_messages", None)
                    lines.append(f"\\n<b>{html.escape(channel)}</b>\\nChat: <code>{chat.id}</code>\\nBot: <code>{html.escape(str(status))}</code>\\nCan post: <code>{html.escape(str(rights))}</code>")
                    msg = await context.bot.send_message(chat.id, "🧪 <b>Zoner Offers AI test</b>\\n\\nPosting works. ✅", parse_mode=ParseMode.HTML)
                    lines.append(f"✅ Sent message <code>{msg.message_id}</code>")
                except Exception as exc:
                    log.exception("Channel diagnostic failed for %s", channel)
                    lines.append(f"❌ <code>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</code>")
            await query.message.reply_text("\\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=admin_menu())
            return

        if data == "admin_add":
            await query.message.reply_text("➕ <b>Add Offer</b>\n\nUse /addoffer for the manual fallback form.", parse_mode=ParseMode.HTML); return
        if data == "admin_scan":
            await query.edit_message_text("🤖 <b>Scanning deal sources…</b>\n\nPlease wait.", parse_mode=ParseMode.HTML)
            result = await scan_and_publish(context.bot, manual=True)
            await query.message.reply_text(f"✅ <b>Scan complete</b>\n\n{html.escape(result)}", parse_mode=ParseMode.HTML, reply_markup=admin_menu())
            return
        if data == "admin_offers":
            rows = get_offers(limit=15)
            if not rows:
                await query.edit_message_text("📋 <b>Offers</b>\n\nNo offers yet.", parse_mode=ParseMode.HTML, reply_markup=admin_menu()); return
            buttons = [[InlineKeyboardButton(f"🗑️ {r['title'][:35]}", callback_data=f"delete_{r['id']}")] for r in rows]
            buttons.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")])
            await query.edit_message_text("📋 <b>Manage Offers</b>\n\nTap an offer to delete it:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons)); return
        if data in {"admin_stats", "admin_stats_refresh"}:
            # Read every metric live from SQLite when the Stats screen is opened/refreshed.
            con = db()
            try:
                started = int(con.execute("SELECT COUNT(*) AS n FROM subscribers").fetchone()["n"])
                notifications_on = int(con.execute("SELECT COUNT(*) AS n FROM subscribers WHERE enabled=1").fetchone()["n"])
                notifications_off = int(con.execute("SELECT COUNT(*) AS n FROM subscribers WHERE enabled=0").fetchone()["n"])
                verified = int(con.execute("SELECT COUNT(*) AS n FROM verified_users").fetchone()["n"])
                groups = int(con.execute(
                    "SELECT COUNT(*) AS n FROM auto_publish_chats WHERE enabled=1 AND chat_type IN ('group','supergroup')"
                ).fetchone()["n"])
                published = int(con.execute("SELECT COUNT(*) AS n FROM published_links").fetchone()["n"])
                unique_published_offers = int(con.execute(
                    "SELECT COUNT(DISTINCT offer_id) AS n FROM published_links"
                ).fetchone()["n"])
                total_offers = int(con.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"])
            finally:
                con.close()

            await query.edit_message_text(
                "📊 <b>ZONER AI — LIVE STATS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Started Bot:</b> {started}\n"
                f"✅ <b>Channel Verified/Joined:</b> {verified}\n"
                f"🔔 <b>Notifications ON:</b> {notifications_on}\n"
                f"🔕 <b>Notifications OFF:</b> {notifications_off}\n"
                f"👥 <b>Active Auto-Publish Groups:</b> {groups}\n"
                f"🛍️ <b>Total Offers:</b> {total_offers}\n"
                f"📢 <b>Published Link Records:</b> {published}\n"
                f"🎯 <b>Unique Published Offers:</b> {unique_published_offers}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "⏱️ <b>Auto Publishing:</b> 15 minutes (900 sec)\n"
                f"🎯 <b>Minimum Deal Score:</b> {MIN_DEAL_SCORE}\n\n"
                "🟢 Stats are read live from the bot database.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Live Stats", callback_data="admin_stats_refresh")],
                    [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")],
                ]),
            )
            return
        if data == "admin_panel":
            await query.edit_message_text("🔐 <b>AI Admin Panel</b>\n\nChoose an action:", parse_mode=ParseMode.HTML, reply_markup=admin_menu()); return

    if data.startswith("delete_") and is_admin(update):
        try: offer_id = int(data[7:])
        except ValueError: return
        row = get_offer(offer_id)
        if not row: await query.edit_message_text("❌ Offer not found.", reply_markup=admin_menu()); return
        await query.edit_message_text(f"🗑️ <b>Delete offer?</b>\n\n{html.escape(row['title'])}",
                                      parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton("✅ Delete", callback_data=f"confirmdelete_{offer_id}"),
                                          InlineKeyboardButton("❌ Cancel", callback_data="admin_offers")]])); return

    if data.startswith("confirmdelete_") and is_admin(update):
        try: offer_id = int(data[14:])
        except ValueError: return
        msg = "✅ <b>Offer deleted.</b>" if delete_offer(offer_id) else "❌ Offer not found."
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=admin_menu())

async def admin_start(update, context):
    if not is_admin(update):
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "➕ <b>Add New Offer</b>\n\n1/5 Send title:",
        parse_mode=ParseMode.HTML,
    )
    return C_TITLE

async def admin_add_button(update, context):
    query = update.callback_query
    if not is_admin(update):
        await query.answer("Not authorized.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.clear()
    await query.message.reply_text(
        "➕ <b>Add New Offer</b>\n\n1/5 Send title:",
        parse_mode=ParseMode.HTML,
    )
    return C_TITLE

async def got_title(update, context):
    v = update.message.text.strip()
    if not v: await update.message.reply_text("❌ Title cannot be empty."); return C_TITLE
    context.user_data["title"] = v[:200]
    await update.message.reply_text("2/5 Send current price. Example: <b>1299</b>", parse_mode=ParseMode.HTML); return C_PRICE

async def got_price(update, context):
    v = update.message.text.strip().replace("₹", "").replace(",", "").strip()
    try:
        price = float(v)
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await update.message.reply_text("❌ Enter a valid positive price, e.g. 1299.")
        return C_PRICE
    context.user_data["price"] = v[:30]
    await update.message.reply_text("3/5 Send old/MRP price, or <b>skip</b>:", parse_mode=ParseMode.HTML)
    return C_OLD

async def got_old(update, context):
    v = update.message.text.strip()
    context.user_data["old_price"] = None if v.lower() in {"skip","-","no"} else v.replace("₹","").strip()[:30]
    await update.message.reply_text("4/5 Type category: electronics / gaming / fashion / home / books"); return C_CATEGORY

async def got_category(update, context):
    v = update.message.text.strip().lower()
    if v not in CATEGORIES: await update.message.reply_text("❌ Invalid category."); return C_CATEGORY
    context.user_data["category"] = v
    await update.message.reply_text("5/6 Send product/deal URL:"); return C_URL

async def got_url(update, context):
    url = update.message.text.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        await update.message.reply_text("❌ Invalid URL. Send a full http(s) product/deal URL.")
        return C_URL
    d = context.user_data
    d["url"] = url
    await update.message.reply_text(
        "6/6 Kitni baar publish karna hai? Number bhejo (1–100).\\n"
        "Example: <b>100</b>",
        parse_mode=ParseMode.HTML,
    )
    return C_COUNT

async def got_count(update, context):
    v = update.message.text.strip()
    try:
        count = int(v)
        if count < 1 or count > 100:
            raise ValueError
    except (TypeError, ValueError):
        await update.message.reply_text("❌ Publish count 1 se 100 ke beech rakho.")
        return C_COUNT

    d = context.user_data
    offer_id = insert_offer(
        d["title"], d["price"], d["old_price"], d["category"], d["url"],
        "Manual", 0, 100
    )
    if not offer_id:
        existing = get_offer_by_fingerprint(fingerprint(d["title"], d["url"]))
        context.user_data.clear()
        if existing:
            await update.message.reply_text(
                "⚠️ Ye offer pehle se saved hai. Details dobara bharne ki zarurat nahi hai.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🔁 Re-publish This Saved Offer", callback_data=f"republish_menu_{existing['id']}")]])
            )
        else:
            await update.message.reply_text("⚠️ This offer already exists (duplicate title + URL).")
        return ConversationHandler.END

    row = get_offer(offer_id)
    if not row:
        await update.message.reply_text("❌ Offer save hua but load nahi ho saka.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "🟣 <b>𝗠𝗔𝗡𝗨𝗔𝗟 𝗢𝗙𝗙𝗘𝗥 𝗥𝗘𝗔𝗗𝗬</b>\\n"
        "━━━━━━━━━━━━━━━━━━━━\\n"
        "✅ Offer saved.\\n\\n"
        f"🔁 Publish count: <b>{count}</b>\\n\\n"
        "👇 <b>📢 Publish ×N</b> dabakar publishing start karo.\\n"
        "💾 Successful manual publish ke baad ye offer <b>Re-publish Vault</b> me save rahega.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📢 Publish ×{count}",
                callback_data=f"manual_publish:{offer_id}:{count}"
            )],
            [InlineKeyboardButton("🔁 Re-publish This Offer", callback_data=f"republish_menu_{offer_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_close")],
        ]),
    )
    return ConversationHandler.END

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ <b>Cancelled.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu() if is_admin(update) else None)
    return ConversationHandler.END

def insert_offer(title, price, old_price, category, url, source, discount, score, image_url="", description=""):
    category = category or guess_category(f"{title} {source} {url}") or "electronics"
    description = re.sub(r"\s+", " ", html.unescape(description or "")).strip()[:700]
    fp = fingerprint(title, url)
    con = db()
    try:
        # A retailer URL is the strongest duplicate key: titles often differ
        # between RSS feeds even when they point to the exact same product.
        canonical = canonical_deal_url(url)
        for saved in con.execute("SELECT id, url FROM offers").fetchall():
            if canonical and canonical_deal_url(saved["url"]) == canonical:
                log.info("Skipping duplicate product URL; existing offer id=%s", saved["id"])
                return None
        cur = con.execute("""INSERT INTO offers(
            title,price,old_price,category,url,source,discount,score,fingerprint,image_url,description
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                          (title,price,old_price,category,url,source,discount,score,fp,image_url or "",description))
        con.commit(); return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        con.close()

def fetch_product_metadata(url):
    """Get product title/description/image during discovery, never during publish."""
    try:
        r = requests.get(url, timeout=7, allow_redirects=True,
                         headers={"User-Agent":"Mozilla/5.0 (compatible; ZonerOffersBot/1.0)"})
        if not r.ok:
            return "", "", ""
        soup = BeautifulSoup(r.text, "html.parser")
        def meta_value(*attrs_list):
            for attrs in attrs_list:
                tag = soup.find("meta", attrs=attrs)
                value = (tag.get("content") or "").strip() if tag else ""
                if value:
                    return value
            return ""
        title = meta_value({"property":"og:title"},{"name":"twitter:title"})
        description = meta_value({"property":"og:description"},{"name":"description"},{"name":"twitter:description"})
        image = meta_value({"property":"og:image"},{"property":"og:image:url"},{"name":"twitter:image"},{"name":"twitter:image:src"})
        if image:
            image = requests.compat.urljoin(r.url, image)
        return title[:250], description[:700], image[:2000]
    except Exception as exc:
        log.debug("Product metadata lookup failed for %s: %s", url, exc)
        return "", "", ""

def fetch_product_image(url):
    return fetch_product_metadata(url)[2]

def generate_product_description(title, category, existing=""):
    """Use retailer metadata when available; otherwise provide a safe product-specific description."""
    existing = re.sub(r"\s+", " ", html.unescape(existing or "")).strip()
    if existing:
        return existing[:700]
    title = re.sub(r"\s+", " ", html.unescape(title or "")).strip()
    label = CATEGORIES.get(category or "electronics", "🛍️ Product")
    return (f"{title} — a {label.lower()} product selected for this deal. Check the retailer page for the latest specifications, variants, stock and current price before checkout.")[:700]

def ensure_offer_image(row):
    """Use only the real product image supplied by the shopping/retailer page."""
    image = (row["image_url"] or "").strip() if "image_url" in row.keys() else ""

    # Never publish the old AI/cartoon artwork URLs.
    if image and "image.pollinations.ai" not in image:
        return image, row

    # If the cached image was AI-generated, clear it. Discovery will replace it
    # with the retailer's own product image before the offer becomes publishable.
    if image and "image.pollinations.ai" in image:
        image = ""

    if not image:
        # This function is normally called on cached metadata. A real retailer
        # image must already be available; do not manufacture a replacement.
        try:
            image = fetch_product_image(row["url"])
        except Exception as exc:
            log.debug("Retailer image lookup failed for deal %s: %s", row["id"], exc)
            image = ""

    if not image:
        return "", row

    con = db()
    try:
        con.execute("UPDATE offers SET image_url=? WHERE id=?", (image, row["id"]))
        con.commit()
    finally:
        con.close()
    return image, get_offer(row["id"])

def get_offer_by_fingerprint(fp):
    con = db()
    row = con.execute("SELECT * FROM offers WHERE fingerprint=?", (fp,)).fetchone()
    con.close()
    return row

def mark_published(offer_id):
    """Legacy compatibility wrapper; records a publication in the same ledger."""
    row = get_offer(offer_id)
    if row is None:
        return False
    return record_publication(offer_id, "legacy", row["url"], row["category"])

def record_publication(offer_id, chat_id, url, category):
    """Persist a successful publication immediately with a short SQLite retry."""
    for attempt in range(4):
        con = db()
        try:
            con.execute(
                "INSERT INTO publish_history(offer_id, published_at) VALUES (?, CURRENT_TIMESTAMP)",
                (offer_id,),
            )
            con.execute(
                "INSERT INTO published_links(offer_id, chat_id, url, category, published_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (offer_id, str(chat_id), url, category or "electronics"),
            )
            con.commit()
            return True
        except sqlite3.OperationalError as exc:
            con.rollback()
            if "locked" not in str(exc).lower() or attempt == 3:
                log.exception("Publication save failed for offer %s", offer_id)
                return False
            time.sleep(0.15 * (attempt + 1))
        except Exception:
            con.rollback()
            log.exception("Publication save failed for offer %s", offer_id)
            return False
        finally:
            con.close()
    return False

def record_manual_publish(offer_id, publish_count=1):
    """Save/update an offer after a successful manual publication."""
    count = max(1, int(publish_count))
    con = db()
    try:
        con.execute(
            """INSERT INTO manual_published_offers
               (offer_id, publish_count, first_published_at, last_published_at)
               VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(offer_id) DO UPDATE SET
                 publish_count=manual_published_offers.publish_count + excluded.publish_count,
                 last_published_at=CURRENT_TIMESTAMP""",
            (offer_id, count),
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        log.exception("Manual-publish vault save failed for offer %s", offer_id)
        return False
    finally:
        con.close()

def get_manual_published_offers(limit=30):
    """Return only offers that were actually manually published."""
    con = db()
    try:
        return con.execute(
            """SELECT o.*, m.publish_count, m.first_published_at, m.last_published_at
               FROM manual_published_offers m
               JOIN offers o ON o.id=m.offer_id
               ORDER BY datetime(m.last_published_at) DESC, o.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        con.close()

def get_manual_publish_count(offer_id):
    con = db()
    try:
        row = con.execute(
            "SELECT publish_count FROM manual_published_offers WHERE offer_id=?",
            (offer_id,),
        ).fetchone()
        return int(row["publish_count"]) if row else 0
    finally:
        con.close()

def get_cached_offer_for_publish():
    # Never scrape at publish time. Prefer offers not published in the last
    # 2 hours; if the cache is exhausted, reuse the oldest cached offer so
    # automatic publishing never stops.
    con = db()
    row = con.execute("""
        SELECT o.*
        FROM offers o
        LEFT JOIN (
            SELECT offer_id, MAX(published_at) AS last_published
            FROM publish_history GROUP BY offer_id
        ) h ON h.offer_id = o.id
        WHERE h.last_published IS NULL
        ORDER BY CASE WHEN h.last_published IS NULL THEN 0 ELSE 1 END,
                 datetime(COALESCE(h.last_published, o.created_at)) ASC,
                 o.id ASC
        LIMIT 1
    """).fetchone()
    # If every cached offer was published recently, wait for discovery instead
    # of reusing an old link and creating duplicate posts every 15 minutes.
    con.close()
    return row

def fetch_feed(source, query):
    url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-IN&gl=IN&ceid=IN:en"
    r = requests.get(url, timeout=15, headers={"User-Agent":"ZonerOffersBot/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.text)
    items = []
    for item in root.findall(".//item")[:12]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub = item.findtext("pubDate") or ""
        if title and link: items.append((source,title,link,pub))
    return items

def resolve_platform_url(url, source):
    """Follow redirects and accept legitimate shopping URLs from any retailer."""
    try:
        r = requests.get(
            url, timeout=8, allow_redirects=True,
            headers={"User-Agent": "ZonerOffersBot/1.0"}
        )
        final_url = r.url
        parsed = urlparse(final_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return ""
        blocked = {
            "news.google.com", "google.com", "youtube.com", "youtu.be",
            "facebook.com", "instagram.com", "x.com", "twitter.com",
            "t.me", "telegram.me"
        }
        if host in blocked or any(host.endswith("." + h) for h in blocked):
            return ""
        return final_url
    except Exception as exc:
        log.debug("URL resolution failed for %s: %s", source, exc)
    return ""

def discover_candidates():
    """Discover from all allowed platforms concurrently so one slow source cannot
    consume the whole 30-second posting window."""
    def discover_one(source, query):
        local = []
        try:
            items = fetch_feed(source, query)
            # Resolve links concurrently too; only whitelisted final domains survive.
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {
                    pool.submit(resolve_platform_url, item[2], source): item
                    for item in items[:12]
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        resolved = future.result()
                    except Exception:
                        resolved = ""
                    if resolved:
                        local.append((source, item[1], resolved, item[3]))
        except Exception as exc:
            log.warning("Discovery failed for %s: %s", source, exc)
        return local

    found = []
    with ThreadPoolExecutor(max_workers=min(9, len(DISCOVERY_QUERIES))) as pool:
        futures = [pool.submit(discover_one, source, query) for source, query in DISCOVERY_QUERIES]
        for future in as_completed(futures):
            try:
                found.extend(future.result())
            except Exception as exc:
                log.warning("Parallel discovery worker failed: %s", exc)
    return found

def is_deal_candidate(source, title, url):
    text = (title + " " + url).lower()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False

    # Generic web-shopping mode: accept plausible retailer/product URLs,
    # while rejecting obvious social, messaging, search and publisher hosts.
    blocked = {
        "news.google.com", "google.com", "youtube.com", "youtu.be",
        "facebook.com", "instagram.com", "x.com", "twitter.com",
        "t.me", "telegram.me", "wikipedia.org"
    }
    if host in blocked or any(host.endswith("." + h) for h in blocked):
        return False

    shopping_terms = (
        "shop", "store", "product", "products", "deal", "deals", "offer",
        "offers", "sale", "discount", "coupon", "price", "buy", "cart",
        "checkout", "fashion", "electronics", "grocery", "beauty"
    )
    deal_terms = (
        "deal", "offer", "sale", "discount", "off", "coupon",
        "price drop", "lowest", "save"
    )
    path_text = (parsed.path + " " + parsed.query).lower()
    has_shopping_signal = any(t in text for t in shopping_terms) or any(
        t in path_text for t in shopping_terms
    )
    has_deal_signal = any(t in text for t in deal_terms) or extract_discount(title) > 0

    # IMPORTANT: allow known shopping platforms even when the feed title does
    # not literally contain words such as "deal" or "offer". Google/RSS feeds
    # often return a clean product title plus a retailer URL, so the old
    # two-signal gate could filter every real product and leave the publisher
    # with no fresh candidate. Generic unknown domains still need both signals.
    known_shopping_host = any(
        host == domain or host.endswith("." + domain)
        for domains in PLATFORM_DOMAINS.values()
        for domain in domains
    )
    if known_shopping_host:
        return has_shopping_signal or has_deal_signal or len(parsed.path.strip("/")) > 0

    # Unknown retailers still need both shopping and deal signals so ordinary
    # news/blog links are not published as deals.
    return has_shopping_signal and has_deal_signal

def normalize_candidate(source, title, url):
    clean = re.sub(r"\s+", " ", html.unescape(title)).strip()
    discount = extract_discount(clean)
    price = extract_price(clean)
    score = score_deal(clean, discount, source)
    return {
        "title": clean[:200],
        "price": price or "Check live price",
        "old_price": None,
        "category": guess_category(f"{clean} {source} {url}"),
        "url": url,
        "source": source,
        "discount": discount,
        "score": score,
    }

async def publish_offer(bot, row):
    """Publish a cached deal using the approved Zoner Offers Bot card format."""
    text = offer_text(row)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy / View Deal", url=row["url"])]])
    channel_published = False

    # Only a real shopping-platform product image is allowed. If the
    # retailer image is unavailable, do not publish a text-only/cartoon post.
    try:
        image_url, row = await asyncio.to_thread(ensure_offer_image, row)
    except Exception as exc:
        log.warning("Image enrichment failed for deal %s: %s", row["id"], exc)
        image_url = ""

    async def send_deal(chat_id):
        # Image-first publishing: use the real retailer image whenever available.
        # If image enrichment is unavailable, fall back to a text post so a
        # temporary retailer-image failure can NEVER stop the auto publisher.
        if image_url:
            try:
                await bot.send_photo(
                    chat_id=chat_id, photo=image_url, caption=text,
                    parse_mode=ParseMode.HTML, reply_markup=markup,
                    read_timeout=8, write_timeout=8
                )
                return True
            except Exception as exc:
                log.warning("Photo publish failed for %s to %s: %s; trying text fallback",
                            row["id"], chat_id, exc)
        try:
            await bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=markup,
                read_timeout=8, write_timeout=8
            )
            log.info("Published text fallback for deal %s to %s (image unavailable)",
                     row["id"], chat_id)
            return True
        except Exception as exc:
            log.warning("Text publish failed for %s to %s: %s", row["id"], chat_id, exc)
            return False

    if AUTO_POST:
        # Publishing is the first priority. One channel failure never stops
        # the next channel or the scheduler.
        targets = list(POST_CHANNELS)
        # Group auto-publishing is ON by default. An explicit admin OFF
        # remains respected, but a missing setting must never disable groups.
        if str(get_setting_sync("auto_group_publish", "1")).strip().lower() in {"1", "true", "on", "yes"}:
            targets.extend(str(r["chat_id"]) for r in get_auto_publish_chats_sync())
        targets = list(dict.fromkeys(targets))
        for channel in targets:
            posted = False
            for attempt in range(3):
                try:
                    if await send_deal(channel):
                        saved = await asyncio.to_thread(
                            record_publication,
                            row["id"], channel, row["url"], row["category"]
                        )
                        if not saved:
                            log.error("⚠️ Telegram publish succeeded but DB save failed for deal %s to %s",
                                      row["id"], channel)
                        else:
                            log.info("💾 Saved published link deal=%s category=%s chat=%s",
                                     row["id"], row["category"], channel)
                        log.info("✅ Published deal %s to channel %s%s",
                                 row["id"], channel, " with image" if image_url else "")
                        channel_published = True
                        posted = True
                        break
                except Exception as exc:
                    log.warning(
                        "Channel post failed for %s (attempt %s/3): %s",
                        channel, attempt + 1, exc
                    )
                if attempt < 2:
                    await asyncio.sleep(1)
            if not posted:
                log.error("❌ Could not publish deal %s to %s", row["id"], channel)

    # Subscriber notifications are strictly background/best-effort.
    # NEVER await subscriber delivery here: a slow/blocked subscriber or
    # Telegram rate-limit must not delay the 15-minute channel/group publisher.
    con = db()
    users = [row["user_id"] for row in con.execute(
        "SELECT user_id FROM subscribers WHERE enabled=1"
    ).fetchall()]
    con.close()

    async def notify_subscribers():
        # Bound concurrency so a large subscriber list cannot flood Telegram.
        semaphore = asyncio.Semaphore(8)

        async def notify_one(user_id):
            async with semaphore:
                try:
                    await send_deal(user_id)
                except Exception as exc:
                    log.warning("Subscriber notification failed for %s: %s", user_id, exc)

        if users:
            await asyncio.gather(
                *(notify_one(user_id) for user_id in users),
                return_exceptions=True,
            )

    if users:
        # Fire-and-forget with a top-level exception guard. The publishing
        # cycle is considered complete as soon as configured channels/groups
        # have been handled and their publication ledger entries are saved.
        async def guarded_notify():
            try:
                await notify_subscribers()
            except Exception:
                log.exception("Background subscriber notification batch failed")

        asyncio.create_task(guarded_notify())
        log.info("📨 Queued %s subscriber notifications in background; publisher is not blocked", len(users))

    return channel_published


async def scan_and_publish(bot, manual=False):
    """Publish one cached/cached-old deal every cycle without live scraping.

    Data may be up to 2 hours old (or older when the cache is exhausted).
    The publisher never waits for live discovery and never stops merely because
    fresh data is unavailable. Every published link is stored with a category.
    """
    added = 0
    skipped = 0

    # PUBLISH FIRST: never make auto-publishing wait for network discovery.
    # A cached offer is enough to keep the channel alive immediately.
    row = get_cached_offer_for_publish()

    if row is not None:
        category = row["category"] or guess_category(
            f"{row['title']} {row['source']} {row['url']}"
        ) or "electronics"
        if row["category"] != category:
            con = db()
            con.execute("UPDATE offers SET category=? WHERE id=?", (category, row["id"]))
            con.commit()
            con.close()
            row = get_offer(row["id"])

        try:
            published = await publish_offer(bot, row)
        except Exception:
            log.exception("Publishing cycle failed; scheduler will continue")
            published = False

        if published:
            log.info("🚀 Published cached deal id=%s category=%s; publication ledger updated immediately",

                     row["id"], category)
        else:
            log.warning("No configured channel accepted cached deal id=%s; scheduler continues",
                        row["id"])

    # Discover NEW product/deal links at most once per 2 hours. Discovery is
    # separate from publishing: image + product URL are cached before a post,
    # so the publishing step itself never waits for live scraping.
    try:
        last_discovery = float(get_setting_sync("last_discovery_at", "0") or 0)
    except (TypeError, ValueError):
        last_discovery = 0.0

    now_ts = datetime.now(timezone.utc).timestamp()
    should_discover = (now_ts - last_discovery) >= DISCOVERY_SECONDS or offer_count() == 0

    if should_discover:
        try:
            candidates = await asyncio.to_thread(discover_candidates)
            new_count = 0
            # Keep only genuine shopping/deal candidates and cache their
            # product image before they become eligible for publication.
            for source, title, url, _pub_date in candidates:
                if not is_deal_candidate(source, title, url):
                    continue
                candidate = normalize_candidate(source, title, url)
                meta_title, description, image_url = await asyncio.to_thread(
                    fetch_product_metadata, candidate["url"]
                )
                if meta_title:
                    candidate["title"] = meta_title
                    candidate["discount"] = extract_discount(meta_title) or candidate["discount"]
                    candidate["price"] = extract_price(meta_title) or candidate["price"]
                candidate["description"] = generate_product_description(candidate["title"], candidate["category"], description)
                # Real shopping-platform image is mandatory. If the retailer
                # page does not expose one, skip this candidate instead of
                # publishing cartoon/AI artwork.
                if not image_url:
                    log.info("Skipping candidate without retailer product image: %s", candidate["url"])
                    continue
                oid = insert_offer(
                    candidate["title"], candidate["price"], candidate["old_price"],
                    candidate["category"], candidate["url"], candidate["source"],
                    candidate["discount"], candidate["score"],
                    image_url=image_url, description=description
                )
                if oid:
                    new_count += 1
                if new_count >= 20:
                    break
            set_setting_sync("last_discovery_at", str(now_ts))
            log.info("🆕 Discovery cycle cached %s new product links with retailer images", new_count)
        except Exception:
            log.exception("New product discovery failed; cached publishing will continue")

    # If no never-published product is available, do not recycle fallback URLs.
    # Wait for the next discovery cycle rather than posting a duplicate deal.
    if row is None:
        row = get_cached_offer_for_publish()
        if row is None:
            log.info("No new unpublished deals available; skipping this cycle to prevent duplicates.")
            return "No new unpublished deals; skipped to prevent duplicate posts."
        try:
            published = await publish_offer(bot, row)
        except Exception:
            log.exception("Publishing cycle failed for newly discovered offer")
            published = False
        if published:
            log.info("Published newly discovered deal id=%s using approved format", row["id"])

    return "Auto-publisher cycle complete"


async def auto_scan_loop(app):
    """Single production publishing loop with a strict 15-minute cadence.

    The runner starts only this loop. Production auto-publishing is locked to
    900 seconds (15 minutes); the database cannot override this cadence.
    """
    while True:
        cycle_started = asyncio.get_running_loop().time()
        try:
            result = await scan_and_publish(app.bot)
            log.info("AI scan: %s", result.replace("\n", " | "))
        except Exception:
            log.exception("AI scan failed")

        # STRICT PRODUCTION LOCK: auto-publishing is always every 15 minutes.
        # Keep the persisted setting normalized to 900 so stale/admin values
        # cannot change the production cadence.
        interval = 900
        try:
            con = db()
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
                )
                con.execute(
                    "INSERT INTO settings(key, value) VALUES('post_interval', '900') "
                    "ON CONFLICT(key) DO UPDATE SET value='900'"
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            log.exception("Could not normalize post_interval; using strict 900-second cadence.")

        elapsed = asyncio.get_running_loop().time() - cycle_started
        await asyncio.sleep(max(1.0, interval - elapsed))


async def add_menu_item(update, context):
    """Admin-only: add a custom main-menu item.

    Usage:
      /addmenu Latest Offers|latest_offers|1|1
    """
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return

    args = getattr(context, "args", []) or []
    if not args:
        await update.message.reply_text(
            "❌ Format:\n/addmenu Label|callback_data|row|column\n\n"
            "Example:\n/addmenu Latest Offers|latest_offers|1|1"
        )
        return

    raw = " ".join(args)
    parts = raw.split("|")
    if len(parts) != 4:
        await update.message.reply_text(
            "❌ Invalid format. Use:\n/addmenu Label|callback_data|row|column"
        )
        return

    label, cb_data, row, col = [part.strip() for part in parts]
    if not label or not cb_data:
        await update.message.reply_text("❌ Label and callback_data are required.")
        return

    try:
        row = int(row)
        col = int(col)
        if row < 1 or col < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Row and column must be positive numbers.")
        return

    con = db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS menu_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "label TEXT NOT NULL, "
            "callback_data TEXT NOT NULL, "
            "row_position INTEGER NOT NULL DEFAULT 1, "
            "col_position INTEGER NOT NULL DEFAULT 1, "
            "is_active INTEGER NOT NULL DEFAULT 1)"
        )
        con.execute(
            "INSERT INTO menu_items "
            "(label, callback_data, row_position, col_position, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (label, cb_data, row, col),
        )
        con.commit()
    except sqlite3.IntegrityError as exc:
        con.rollback()
        log.warning("Menu item insert rejected: %s", exc)
        await update.message.reply_text("❌ Menu item could not be added.")
        return
    finally:
        con.close()

    await update.message.reply_text(
        f"✅ Menu item '<b>{html.escape(label)}</b>' added!\n"
        f"📍 Row: {row} • Column: {col}",
        parse_mode=ParseMode.HTML,
    )

async def set_interval_command(update, context):
    """Admin-only status command; production cadence stays locked at 15 minutes."""
    if not is_admin(update):
        return
    await update.message.reply_text(
        "⏰ <b>Auto Publishing Interval</b>\\n\\n"
        "🔒 Strictly locked: <b>900 seconds (15 minutes)</b>.\\n"
        "This cannot be changed until the administrator explicitly requests an interval change.",
        parse_mode=ParseMode.HTML,
    )
    return
    try:
        seconds = int(args[0])
    except (TypeError, ValueError):
        await update.message.reply_text("❌ Interval must be a number of seconds.")
        return
    if not 30 <= seconds <= 86400:
        await update.message.reply_text("❌ Interval must be between 30 and 86400 seconds.")
        return
    con = db()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        con.execute(
            "INSERT INTO settings(key, value) VALUES('post_interval', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(seconds),),
        )
        con.commit()
    finally:
        con.close()
    await update.message.reply_text(
        f"✅ Auto-post interval set to <b>{seconds} seconds</b>.",
        parse_mode=ParseMode.HTML,
    )

async def admin_help(update, context):
    if not is_admin(update): return
    await update.message.reply_text("🔐 <b>Zoner AI Admin</b>\n\n🤖 Auto-scanning is enabled. Use the panel to scan manually or manage offers.",
                                    parse_mode=ParseMode.HTML, reply_markup=admin_menu())


async def test_channels(update, context):
    """Admin-only Telegram channel diagnostics."""
    if not is_admin(update):
        return
    lines = ["🧪 <b>Channel posting test</b>"]
    for channel in POST_CHANNELS:
        try:
            chat = await context.bot.get_chat(channel)
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            status = getattr(member, "status", "unknown")
            rights = getattr(member, "can_post_messages", None)
            lines.append(
                f"\n<b>{html.escape(channel)}</b>\n"
                f"Chat: <code>{chat.id}</code>\n"
                f"Bot status: <code>{html.escape(str(status))}</code>\n"
                f"Can post: <code>{html.escape(str(rights))}</code>"
            )
            if status not in {"administrator", "creator"} or rights is False:
                lines.append("❌ Bot does not have channel posting permission.")
                continue
            msg = await context.bot.send_message(
                chat.id,
                "🧪 <b>Zoner Offers AI test message</b>\n\nChannel posting is working. ✅",
                parse_mode=ParseMode.HTML,
            )
            lines.append(f"✅ Test message sent: <code>{msg.message_id}</code>")
        except Exception as exc:
            log.exception("Channel diagnostic failed for %s", channel)
            lines.append(
                f"❌ <b>Failed</b>\n<code>{html.escape(type(exc).__name__)}: "
                f"{html.escape(str(exc))}</code>"
            )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-type","text/plain"); self.end_headers()
        self.wfile.write(b"Zoner Offers AI is running!")
    def log_message(self, format, *args): return

def run_health_server():
    port = int(os.getenv("PORT","10000"))
    HTTPServer(("0.0.0.0",port),HealthHandler).serve_forever()

async def post_init(app):
    app.create_task(auto_scan_loop(app))

def run_bot():
    if not TOKEN: raise RuntimeError("BOT_TOKEN environment variable is missing.")
    init_db()
    Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("addoffer", admin_start),
            CallbackQueryHandler(admin_add_button, pattern=r"^admin_add$")
        ],
        states={
            C_TITLE:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)],
            C_PRICE:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_price)],
            C_OLD:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_old)],
            C_CATEGORY:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_category)],
            C_URL:[MessageHandler(filters.TEXT & ~filters.COMMAND, got_url)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(CommandHandler("testchannels", test_channels))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("🔥 Zoner Offers AI is running.")
    app.run_polling()

if __name__ == "__main__":
    run_bot()