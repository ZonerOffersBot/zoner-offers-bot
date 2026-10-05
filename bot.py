import os
import html
import sqlite3
import logging
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

C_TITLE, C_PRICE, C_OLD, C_CATEGORY, C_URL = range(5)
CATEGORIES = {
    "electronics": "📱 Electronics",
    "gaming": "🎮 Gaming",
    "fashion": "👕 Fashion",
    "home": "🏠 Home & Kitchen",
    "books": "📚 Books",
}

def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS offers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, price TEXT NOT NULL, old_price TEXT,
        category TEXT NOT NULL, url TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("""CREATE TABLE IF NOT EXISTS subscribers(
        user_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1)""")
    con.commit()
    con.close()

def save_subscriber(user_id, enabled=1):
    con = db()
    con.execute("INSERT INTO subscribers(user_id,enabled) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled", (user_id, enabled))
    con.commit(); con.close()

def get_offers(category=None, limit=20):
    con = db()
    if category:
        rows = con.execute("SELECT * FROM offers WHERE category=? ORDER BY id DESC LIMIT ?", (category, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM offers ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close(); return rows

def get_offer(offer_id):
    con = db(); row = con.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone(); con.close(); return row

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Latest Deals", callback_data="offers"),
         InlineKeyboardButton("🏷️ Categories", callback_data="categories")],
        [InlineKeyboardButton("🔔 Notifications", callback_data="subscribe"),
         InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("🆘 Help", callback_data="help")]
    ])

def categories_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(v, callback_data=f"cat_{k}")] for k,v in CATEGORIES.items()] +
        [[InlineKeyboardButton("⬅️ Back", callback_data="back")]]
    )

def offer_buttons(rows):
    buttons = [[InlineKeyboardButton(f"🔥 {r['title'][:48]}", callback_data=f"offer_{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(buttons)

def offer_text(r):
    old = f"\n~~₹{html.escape(r['old_price'])}~~" if r["old_price"] else ""
    cat = CATEGORIES.get(r["category"], r["category"])
    return (f"🔥 <b>{html.escape(r['title'])}</b>\n\n"
            f"💰 <b>₹{html.escape(r['price'])}</b>{old}\n"
            f"🏷️ {html.escape(cat)}\n\n"
            f"👇 Tap below to grab the deal before it ends!")

def is_admin(update):
    return ADMIN_ID and str(update.effective_user.id) == str(ADMIN_ID)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 <b>Welcome to Zoner Offers!</b>\n\n"
        "🛍️ Find the latest Amazon & Flipkart deals, discounts and price drops.\n\n"
        "👇 Choose what you want:",
        parse_mode=ParseMode.HTML, reply_markup=main_menu())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 <b>Zoner Offers Help</b>\n\n"
        "🛍️ Latest Deals — see newest offers\n"
        "🏷️ Categories — browse by category\n"
        "🔔 Notifications — get new-deal alerts\n"
        "📢 Join Channel — follow deal updates\n\n"
        "Need help? Contact the channel admin.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu())

async def show_offers(update, context, category=None):
    rows = get_offers(category)
    title = CATEGORIES.get(category, "🛍️ Latest Deals") if category else "🛍️ Latest Deals"
    if not rows:
        text = f"{title}\n\n😕 No offers here yet. Check back soon!"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]])
    else:
        text = f"<b>{html.escape(title)}</b>\n\nSelect a deal:"
        markup = offer_buttons(rows)
    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data
    if data == "offers":
        await show_offers(update, context)
    elif data == "categories":
        await q.edit_message_text("🏷️ <b>Offer Categories</b>\n\nChoose a category:", parse_mode=ParseMode.HTML, reply_markup=categories_menu())
    elif data.startswith("cat_"):
        await show_offers(update, context, data[4:])
    elif data.startswith("offer_"):
        r = get_offer(int(data[6:]))
        if not r:
            await q.edit_message_text("❌ This offer is no longer available.", reply_markup=main_menu())
            return
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Buy Now", url=r["url"])],
            [InlineKeyboardButton("⬅️ Back to Deals", callback_data="offers")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="back")]
        ])
        await q.edit_message_text(offer_text(r), parse_mode=ParseMode.HTML, reply_markup=markup)
    elif data == "subscribe":
        save_subscriber(update.effective_user.id, 1)
        await q.edit_message_text("🔔 <b>Notifications ON</b>\n\nYou'll get alerts when new deals are added.", parse_mode=ParseMode.HTML, reply_markup=main_menu())
    elif data == "help":
        await q.edit_message_text("🆘 <b>How it works</b>\n\nBrowse deals → open an offer → tap <b>Buy Now</b>.\n\nTurn notifications on to receive new-deal alerts.", parse_mode=ParseMode.HTML, reply_markup=main_menu())
    elif data == "back":
        await q.edit_message_text("🔥 <b>Zoner Offers</b>\n\nChoose an option:", parse_mode=ParseMode.HTML, reply_markup=main_menu())

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text("➕ <b>Add Offer</b>\n\n1/5 Send the offer title:", parse_mode=ParseMode.HTML)
    return C_TITLE

async def got_title(update, context):
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text("2/5 Send current price (example: 1299):")
    return C_PRICE

async def got_price(update, context):
    context.user_data["price"] = update.message.text.strip()
    await update.message.reply_text("3/5 Send old price, or type <b>skip</b>:", parse_mode=ParseMode.HTML)
    return C_OLD

async def got_old(update, context):
    v = update.message.text.strip()
    context.user_data["old_price"] = None if v.lower() == "skip" else v
    await update.message.reply_text("4/5 Choose category: electronics / gaming / fashion / home / books")
    return C_CATEGORY

async def got_category(update, context):
    v = update.message.text.strip().lower()
    if v not in CATEGORIES:
        await update.message.reply_text("❌ Invalid category. Use: electronics, gaming, fashion, home, books")
        return C_CATEGORY
    context.user_data["category"] = v
    await update.message.reply_text("5/5 Send the product URL:")
    return C_URL

async def got_url(update, context):
    url = update.message.text.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text("❌ Please send a valid URL starting with http:// or https://")
        return C_URL
    d = context.user_data
    con = db()
    cur = con.execute("INSERT INTO offers(title,price,old_price,category,url) VALUES(?,?,?,?,?)",
                      (d["title"], d["price"], d["old_price"], d["category"], url))
    offer_id = cur.lastrowid; con.commit(); con.close()
    r = get_offer(offer_id)
    await update.message.reply_text("✅ <b>Offer added successfully!</b>\n\n" + offer_text(r), parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy Now", url=url)]]))
    await notify_subscribers(context, r)
    if CHANNEL_ID:
        try:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=offer_text(r), parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy Now", url=url)]]))
        except Exception as e:
            log.warning("Channel post failed: %s", e)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Add offer cancelled.")
    return ConversationHandler.END

async def notify_subscribers(context, r):
    con = db(); users = con.execute("SELECT user_id FROM subscribers WHERE enabled=1").fetchall(); con.close()
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Buy Now", url=r["url"])]])
    for row in users:
        try:
            await context.bot.send_message(chat_id=row["user_id"], text="🔔 <b>New Zoner Deal!</b>\n\n"+offer_text(r), parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            pass

async def admin_help(update, context):
    if is_admin(update):
        await update.message.reply_text("🔐 <b>Admin</b>\n/addoffer — add a new deal\n/cancel — cancel current entry\n/admin — this help", parse_mode=ParseMode.HTML)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-type","text/plain"); self.end_headers(); self.wfile.write(b"Zoner Offers Bot is running!")
    def log_message(self, format, *args): return

def run_health_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT","10000"))), HealthHandler).serve_forever()

def run_bot():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    init_db()
    Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
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
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    print("🔥 Zoner Offers Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
