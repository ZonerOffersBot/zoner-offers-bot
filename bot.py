import os
import re
import html
import json
import sqlite3
import logging
import asyncio
import hashlib
from datetime import datetime, timezone
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
DB_FILE = os.getenv("DB_FILE", "zoner_offers.db")
CHANNEL_URL = "https://t.me/ZonerOffers"
SCAN_MINUTES = int(os.getenv("SCAN_MINUTES", "30"))
MIN_DEAL_SCORE = int(os.getenv("MIN_DEAL_SCORE", "45"))
AUTO_POST = os.getenv("AUTO_POST", "1") == "1"

CATEGORIES = {
    "electronics": "📱 Electronics",
    "gaming": "🎮 Gaming",
    "fashion": "👕 Fashion",
    "home": "🏠 Home & Kitchen",
    "books": "📚 Books",
}

# Public, non-authenticated discovery feeds. The bot extracts deal headlines/links,
# scores them, de-duplicates them, and only publishes strong candidates.
DISCOVERY_QUERIES = [
    ("Amazon", "Amazon India deal discount"),
    ("Flipkart", "Flipkart India deal discount"),
    ("Myntra", "Myntra India sale discount"),
    ("Croma", "Croma India deal discount"),
    ("Reliance Digital", "Reliance Digital India deal discount"),
    ("Tata CLiQ", "Tata CLiQ India deal discount"),
    ("Ajio", "AJIO India deal discount"),
    ("Gaming", "India gaming deal discount PS5 Xbox GPU"),
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("zoner")

C_TITLE, C_PRICE, C_OLD, C_CATEGORY, C_URL = range(5)

def db():
    con = sqlite3.connect(DB_FILE, timeout=20)
    con.row_factory = sqlite3.Row
    return con

def init_db():
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS subscribers (
        user_id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1
    )""")
    # Migrate old databases created by the MVP.
    existing = {r["name"] for r in con.execute("PRAGMA table_info(offers)").fetchall()}
    for name, ddl in [
        ("source", "ALTER TABLE offers ADD COLUMN source TEXT DEFAULT ''"),
        ("discount", "ALTER TABLE offers ADD COLUMN discount INTEGER DEFAULT 0"),
        ("score", "ALTER TABLE offers ADD COLUMN score INTEGER DEFAULT 0"),
        ("fingerprint", "ALTER TABLE offers ADD COLUMN fingerprint TEXT"),
    ]:
        if name not in existing:
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
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
    return bool(row["enabled"]) if row else False

def get_offers(category=None, limit=20):
    con = db()
    if category:
        rows = con.execute("SELECT * FROM offers WHERE category=? ORDER BY id DESC LIMIT ?", (category, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM offers ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return rows

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

def fingerprint(title, url):
    return hashlib.sha256((re.sub(r"\W+", " ", title.lower()).strip() + "|" + url.split("?")[0]).encode()).hexdigest()

def guess_category(text):
    t = text.lower()
    if any(x in t for x in ["ps5", "ps4", "xbox", "gaming", "gpu", "rtx", "controller", "steam"]): return "gaming"
    if any(x in t for x in ["shirt", "jeans", "shoe", "sneaker", "dress", "fashion", "myntra", "ajio"]): return "fashion"
    if any(x in t for x in ["sofa", "mixer", "fridge", "refrigerator", "washing", "kitchen", "chair", "home"]): return "home"
    if any(x in t for x in ["book", "novel", "kindle"]): return "books"
    return "electronics"

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

def main_menu(user_id=None):
    notify = "🔔 Notifications ON" if user_id is not None and subscriber_enabled(user_id) else "🔕 Notifications OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Latest Deals", callback_data="offers"), InlineKeyboardButton("🏷️ Categories", callback_data="categories")],
        [InlineKeyboardButton(notify, callback_data="notifications"), InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("🤖 AI Deal Hunter", callback_data="ai_info")],
        [InlineKeyboardButton("🆘 Help", callback_data="help")],
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
    title = html.escape(row["title"])
    price = html.escape(row["price"])
    category = html.escape(CATEGORIES.get(row["category"], row["category"]))
    text = f"🔥 <b>{title}</b>\n\n💰 <b>₹{price}</b>"
    if row["old_price"]:
        text += f"  <s>₹{html.escape(str(row['old_price']))}</s>"
    if row["discount"]:
        text += f"  <b>({row['discount']}% OFF)</b>"
    if row["score"]:
        text += f"\n🤖 Deal Score: <b>{row['score']}/100</b>"
    if row["source"]:
        text += f"\n🔎 Source: {html.escape(row['source'])}"
    return text + f"\n🏷️ {category}\n\n⚡ Check price before checkout; offers can change."

def offer_markup(row, back="offers"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy / View Deal", url=row["url"])],
        [InlineKeyboardButton("⬅️ Back to Deals", callback_data=back)],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
    ])

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Scan Deals Now", callback_data="admin_scan"), InlineKeyboardButton("➕ Add Offer", callback_data="admin_add")],
        [InlineKeyboardButton("📋 Offers", callback_data="admin_offers"), InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
    ])

def is_admin(update):
    return bool(ADMIN_ID and update.effective_user and str(update.effective_user.id) == str(ADMIN_ID))

async def start(update, context):
    await update.message.reply_text(
        "🔥 <b>Welcome to Zoner Offers AI!</b>\n\n"
        "🤖 AI-style deal discovery\n💸 Discounts & price drops\n🔔 Smart deal alerts\n"
        "🌐 Multiple shopping sources\n\n👇 Choose an option:",
        parse_mode=ParseMode.HTML, reply_markup=main_menu(update.effective_user.id))

async def help_command(update, context):
    await update.message.reply_text(
        "🆘 <b>Zoner Offers AI</b>\n\n"
        "The bot continuously checks public deal/news feeds, scores candidates, removes duplicates and publishes strong deals.\n\n"
        "🛍️ Latest Deals — browse\n🏷️ Categories — filter\n🔔 Notifications — alerts\n"
        "🤖 AI Deal Hunter — how discovery works\n🛒 Buy / View Deal — open source offer.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu(update.effective_user.id))

async def show_offers(update, category=None):
    query = update.callback_query
    rows = get_offers(category)
    title = CATEGORIES.get(category, "🏷️ Category") if category else "🛍️ Latest Deals"
    back = "categories" if category else "back"
    if not rows:
        await query.edit_message_text(f"<b>{html.escape(title)}</b>\n\n😕 No offers here yet.", parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=back)]]))
        return
    await query.edit_message_text(f"<b>{html.escape(title)}</b>\n\n👇 Select a deal:", parse_mode=ParseMode.HTML,
                                  reply_markup=offer_buttons(rows, back))

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "offers": await show_offers(update); return
    if data == "categories":
        await query.edit_message_text("🏷️ <b>Offer Categories</b>\n\nChoose a category:", parse_mode=ParseMode.HTML, reply_markup=categories_menu()); return
    if data.startswith("cat_"): await show_offers(update, data[4:]); return
    if data == "ai_info":
        await query.edit_message_text(
            "🤖 <b>AI Deal Hunter</b>\n\n"
            "Zoner checks multiple public deal sources automatically, detects discount signals, scores deal quality, filters duplicates and publishes only stronger candidates.\n\n"
            f"⏱️ Scan interval: every {SCAN_MINUTES} min\n🎯 Minimum score: {MIN_DEAL_SCORE}/100",
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

    if data.startswith("admin_"):
        if not is_admin(update):
            await query.answer("Not authorized.", show_alert=True); return
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
        if data == "admin_stats":
            await query.edit_message_text(
                "📊 <b>Zoner AI Stats</b>\n\n"
                f"🛍️ Offers: <b>{offer_count()}</b>\n🔔 Subscribers: <b>{subscriber_count()}</b>\n"
                f"⏱️ Auto scan: <b>{SCAN_MINUTES} min</b>\n🎯 Min score: <b>{MIN_DEAL_SCORE}</b>",
                parse_mode=ParseMode.HTML, reply_markup=admin_menu()); return
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
    if not is_admin(update): return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("➕ <b>Add New Offer</b>\n\n1/5 Send title:", parse_mode=ParseMode.HTML)
    return C_TITLE

async def got_title(update, context):
    v = update.message.text.strip()
    if not v: await update.message.reply_text("❌ Title cannot be empty."); return C_TITLE
    context.user_data["title"] = v[:200]
    await update.message.reply_text("2/5 Send current price. Example: <b>1299</b>", parse_mode=ParseMode.HTML); return C_PRICE

async def got_price(update, context):
    v = update.message.text.strip().replace("₹", "").strip()
    if not v: await update.message.reply_text("❌ Price cannot be empty."); return C_PRICE
    context.user_data["price"] = v[:30]
    await update.message.reply_text("3/5 Send old/MRP price, or <b>skip</b>:", parse_mode=ParseMode.HTML); return C_OLD

async def got_old(update, context):
    v = update.message.text.strip()
    context.user_data["old_price"] = None if v.lower() in {"skip","-","no"} else v.replace("₹","").strip()[:30]
    await update.message.reply_text("4/5 Type category: electronics / gaming / fashion / home / books"); return C_CATEGORY

async def got_category(update, context):
    v = update.message.text.strip().lower()
    if v not in CATEGORIES: await update.message.reply_text("❌ Invalid category."); return C_CATEGORY
    context.user_data["category"] = v
    await update.message.reply_text("5/5 Send product/deal URL:"); return C_URL

async def got_url(update, context):
    url = update.message.text.strip()
    if not url.startswith(("http://","https://")): await update.message.reply_text("❌ Invalid URL."); return C_URL
    d = context.user_data
    offer_id = insert_offer(d["title"], d["price"], d["old_price"], d["category"], url, "Manual", 0, 100)
    row = get_offer(offer_id)
    await update.message.reply_text("✅ <b>Offer added.</b>\n\n" + offer_text(row), parse_mode=ParseMode.HTML, reply_markup=offer_markup(row))
    await publish_offer(context.bot, row)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ <b>Cancelled.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_menu() if is_admin(update) else None)
    return ConversationHandler.END

def insert_offer(title, price, old_price, category, url, source, discount, score):
    fp = fingerprint(title, url)
    con = db()
    try:
        cur = con.execute("""INSERT INTO offers(title,price,old_price,category,url,source,discount,score,fingerprint)
                             VALUES (?,?,?,?,?,?,?,?,?)""",
                          (title,price,old_price,category,url,source,discount,score,fp))
        con.commit(); return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        con.close()

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

def discover_candidates():
    found = []
    for source, query in DISCOVERY_QUERIES:
        try:
            found.extend(fetch_feed(source, query))
        except Exception as exc:
            log.warning("Discovery failed for %s: %s", source, exc)
    return found

def normalize_candidate(source, title, url):
    clean = re.sub(r"\s+", " ", html.unescape(title)).strip()
    discount = extract_discount(clean)
    price = extract_price(clean)
    score = score_deal(clean, discount, source)
    return {
        "title": clean[:200],
        "price": price or "Check live price",
        "old_price": None,
        "category": guess_category(clean),
        "url": url,
        "source": source,
        "discount": discount,
        "score": score,
    }

async def publish_offer(bot, row):
    text = "🤖 <b>AI Deal Alert</b>\n\n" + offer_text(row)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy / View Deal", url=row["url"])]])
    con = db(); users = con.execute("SELECT user_id FROM subscribers WHERE enabled=1").fetchall(); con.close()
    for user in users:
        try: await bot.send_message(user["user_id"], text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as exc: log.debug("Notify failed %s: %s", user["user_id"], exc)
    if CHANNEL_ID and AUTO_POST:
        try: await bot.send_message(CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as exc: log.warning("Channel post failed: %s", exc)

async def scan_and_publish(bot, manual=False):
    candidates = await asyncio.to_thread(discover_candidates)
    added = 0; skipped = 0
    seen = set()
    for raw in candidates:
        c = normalize_candidate(*raw)
        key = fingerprint(c["title"], c["url"])
        if key in seen: skipped += 1; continue
        seen.add(key)
        if c["score"] < MIN_DEAL_SCORE:
            skipped += 1; continue
        oid = insert_offer(c["title"], c["price"], c["old_price"], c["category"], c["url"], c["source"], c["discount"], c["score"])
        if not oid:
            skipped += 1; continue
        row = get_offer(oid)
        await publish_offer(bot, row)
        added += 1
        if added >= 8: break
    return f"Added: {added}\nFiltered/duplicate: {skipped}\nCandidates checked: {len(candidates)}"

async def auto_scan_loop(app):
    await asyncio.sleep(15)
    while True:
        try:
            result = await scan_and_publish(app.bot)
            log.info("AI scan: %s", result.replace("\n"," | "))
        except Exception:
            log.exception("AI scan failed")
        await asyncio.sleep(max(10, SCAN_MINUTES * 60))

async def admin_help(update, context):
    if not is_admin(update): return
    await update.message.reply_text("🔐 <b>Zoner AI Admin</b>\n\n🤖 Auto-scanning is enabled. Use the panel to scan manually or manage offers.",
                                    parse_mode=ParseMode.HTML, reply_markup=admin_menu())

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
        entry_points=[CommandHandler("addoffer", admin_start)],
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
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("🔥 Zoner Offers AI is running.")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
