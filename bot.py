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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# Add-offer conversation states
C_TITLE, C_PRICE, C_OLD, C_CATEGORY, C_URL = range(5)

CATEGORIES = {
    "electronics": "📱 Electronics",
    "gaming": "🎮 Gaming",
    "fashion": "👕 Fashion",
    "home": "🏠 Home & Kitchen",
    "books": "📚 Books",
}


# -------------------------
# DATABASE
# -------------------------

def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price TEXT NOT NULL,
            old_price TEXT,
            category TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)
    con.commit()
    con.close()


def save_subscriber(user_id, enabled):
    con = db()
    con.execute("""
        INSERT INTO subscribers(user_id, enabled)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET enabled=excluded.enabled
    """, (user_id, enabled))
    con.commit()
    con.close()


def subscriber_enabled(user_id):
    con = db()
    row = con.execute(
        "SELECT enabled FROM subscribers WHERE user_id=?", (user_id,)
    ).fetchone()
    con.close()
    return bool(row["enabled"]) if row else False


def get_offers(category=None, limit=20):
    con = db()
    if category:
        rows = con.execute(
            "SELECT * FROM offers WHERE category=? ORDER BY id DESC LIMIT ?",
            (category, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM offers ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    con.close()
    return rows


def get_offer(offer_id):
    con = db()
    row = con.execute(
        "SELECT * FROM offers WHERE id=?", (offer_id,)
    ).fetchone()
    con.close()
    return row


def delete_offer(offer_id):
    con = db()
    cur = con.execute("DELETE FROM offers WHERE id=?", (offer_id,))
    con.commit()
    deleted = cur.rowcount > 0
    con.close()
    return deleted


def offer_count():
    con = db()
    row = con.execute("SELECT COUNT(*) AS n FROM offers").fetchone()
    con.close()
    return row["n"]


def subscriber_count():
    con = db()
    row = con.execute(
        "SELECT COUNT(*) AS n FROM subscribers WHERE enabled=1"
    ).fetchone()
    con.close()
    return row["n"]


# -------------------------
# UI
# -------------------------

def main_menu(user_id=None):
    notify_label = "🔕 Notifications OFF"
    if user_id is not None and subscriber_enabled(user_id):
        notify_label = "🔔 Notifications ON"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛍️ Latest Deals", callback_data="offers"),
            InlineKeyboardButton("🏷️ Categories", callback_data="categories"),
        ],
        [
            InlineKeyboardButton(notify_label, callback_data="notifications"),
            InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL),
        ],
        [
            InlineKeyboardButton("🆘 Help", callback_data="help"),
        ],
    ])


def categories_menu():
    rows = [
        [InlineKeyboardButton(label, callback_data=f"cat_{key}")]
        for key, label in CATEGORIES.items()
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def offer_buttons(rows, back="offers"):
    buttons = []
    for row in rows:
        title = row["title"]
        if len(title) > 42:
            title = title[:42] + "…"
        buttons.append([
            InlineKeyboardButton(
                f"🔥 {title}", callback_data=f"offer_{row['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=back)])
    return InlineKeyboardMarkup(buttons)


def offer_text(row):
    title = html.escape(row["title"])
    price = html.escape(row["price"])
    category = html.escape(CATEGORIES.get(row["category"], row["category"]))

    text = (
        f"🔥 <b>{title}</b>\n\n"
        f"💰 <b>₹{price}</b>"
    )

    if row["old_price"]:
        text += f"  <s>₹{html.escape(row['old_price'])}</s>"

    text += (
        f"\n🏷️ {category}\n\n"
        "⚡ Grab this deal before it ends!"
    )
    return text


def offer_markup(row, back="offers"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", url=row["url"])],
        [InlineKeyboardButton("⬅️ Back to Deals", callback_data=back)],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back")],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Offer", callback_data="admin_add"),
            InlineKeyboardButton("📋 Offers", callback_data="admin_offers"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data="back"),
        ],
    ])


def is_admin(update):
    return bool(
        ADMIN_ID and update.effective_user
        and str(update.effective_user.id) == str(ADMIN_ID)
    )


# -------------------------
# CUSTOMER COMMANDS
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 <b>Welcome to Zoner Offers!</b>\n\n"
        "🛍️ Fresh Amazon & Flipkart deals\n"
        "💸 Discounts & price drops\n"
        "🔔 Optional instant deal alerts\n\n"
        "👇 Choose an option:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 <b>Zoner Offers Help</b>\n\n"
        "🛍️ <b>Latest Deals</b> — newest offers\n"
        "🏷️ <b>Categories</b> — find deals by type\n"
        "🔔 <b>Notifications</b> — turn deal alerts ON/OFF\n"
        "🛒 <b>Buy Now</b> — open the product link\n"
        "📢 <b>Join Channel</b> — follow all updates\n\n"
        "Simple. Fast. No unnecessary steps. ❤️",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )


async def show_offers(update, category=None):
    query = update.callback_query
    rows = get_offers(category)

    if category:
        title = CATEGORIES.get(category, "🏷️ Category")
        back = "categories"
    else:
        title = "🛍️ Latest Deals"
        back = "back"

    if not rows:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=back)]
        ])
        await query.edit_message_text(
            f"<b>{html.escape(title)}</b>\n\n"
            "😕 No offers here yet.\n"
            "Check back soon!",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return

    await query.edit_message_text(
        f"<b>{html.escape(title)}</b>\n\n"
        "👇 Select a deal:",
        parse_mode=ParseMode.HTML,
        reply_markup=offer_buttons(rows, back),
    )


# -------------------------
# CUSTOMER BUTTONS
# -------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "offers":
        await show_offers(update)
        return

    if data == "categories":
        await query.edit_message_text(
            "🏷️ <b>Offer Categories</b>\n\n"
            "Choose a category:",
            parse_mode=ParseMode.HTML,
            reply_markup=categories_menu(),
        )
        return

    if data.startswith("cat_"):
        await show_offers(update, data[4:])
        return

    if data.startswith("offer_"):
        try:
            offer_id = int(data[6:])
        except ValueError:
            await query.edit_message_text(
                "❌ Invalid offer.", reply_markup=main_menu(query.from_user.id)
            )
            return

        row = get_offer(offer_id)
        if not row:
            await query.edit_message_text(
                "❌ This offer is no longer available.",
                reply_markup=main_menu(query.from_user.id),
            )
            return

        await query.edit_message_text(
            offer_text(row),
            parse_mode=ParseMode.HTML,
            reply_markup=offer_markup(row),
        )
        return

    if data == "notifications":
        current = subscriber_enabled(query.from_user.id)
        new_value = 0 if current else 1
        save_subscriber(query.from_user.id, new_value)

        if new_value:
            message = (
                "🔔 <b>Notifications ON</b>\n\n"
                "You'll receive a message when a new deal is added."
            )
        else:
            message = (
                "🔕 <b>Notifications OFF</b>\n\n"
                "You can turn them back on anytime."
            )

        await query.edit_message_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(query.from_user.id),
        )
        return

    if data == "help":
        await query.edit_message_text(
            "🆘 <b>How it works</b>\n\n"
            "1️⃣ Open <b>Latest Deals</b>\n"
            "2️⃣ Select a product\n"
            "3️⃣ Tap <b>Buy Now</b>\n\n"
            "That's it! ❤️",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(query.from_user.id),
        )
        return

    if data == "back":
        await query.edit_message_text(
            "🔥 <b>Zoner Offers</b>\n\n"
            "Choose an option:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(query.from_user.id),
        )
        return

    # Admin-only buttons
    if data.startswith("admin_"):
        if not is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return

        if data == "admin_add":
            await query.message.reply_text(
                "➕ <b>Add Offer</b>\n\n"
                "1/5 Send the product/offer title:",
                parse_mode=ParseMode.HTML,
            )
            context.user_data["admin_from_button"] = True
            # The ConversationHandler owns the actual state.
            return

        if data == "admin_offers":
            rows = get_offers(limit=15)
            if not rows:
                await query.edit_message_text(
                    "📋 <b>Offers</b>\n\nNo offers yet.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_menu(),
                )
                return

            buttons = []
            for row in rows:
                title = row["title"][:35]
                buttons.append([
                    InlineKeyboardButton(
                        f"🗑️ {title}",
                        callback_data=f"delete_{row['id']}",
                    )
                ])
            buttons.append([
                InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")
            ])

            await query.edit_message_text(
                "📋 <b>Manage Offers</b>\n\n"
                "Tap an offer to delete it:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        if data == "admin_stats":
            await query.edit_message_text(
                "📊 <b>Zoner Offers Stats</b>\n\n"
                f"🛍️ Total offers: <b>{offer_count()}</b>\n"
                f"🔔 Active subscribers: <b>{subscriber_count()}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_panel":
            await query.edit_message_text(
                "🔐 <b>Admin Panel</b>\n\nChoose an action:",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu(),
            )
            return

    if data.startswith("delete_"):
        if not is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return

        try:
            offer_id = int(data[7:])
        except ValueError:
            return

        row = get_offer(offer_id)
        if not row:
            await query.edit_message_text(
                "❌ Offer already deleted.",
                reply_markup=admin_menu(),
            )
            return

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Yes, delete",
                    callback_data=f"confirmdelete_{offer_id}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data="admin_offers"
                ),
            ]
        ])
        await query.edit_message_text(
            f"🗑️ <b>Delete offer?</b>\n\n"
            f"{html.escape(row['title'])}",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return

    if data.startswith("confirmdelete_"):
        if not is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return

        try:
            offer_id = int(data[14:])
        except ValueError:
            return

        if delete_offer(offer_id):
            await query.edit_message_text(
                "✅ <b>Offer deleted.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu(),
            )
        else:
            await query.edit_message_text(
                "❌ Offer not found.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_menu(),
            )


# -------------------------
# ADMIN: ADD OFFER
# -------------------------

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    context.user_data.clear()
    await update.message.reply_text(
        "➕ <b>Add New Offer</b>\n\n"
        "1/5 Send the product/offer title:",
        parse_mode=ParseMode.HTML,
    )
    return C_TITLE


async def got_title(update, context):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("❌ Title cannot be empty. Try again:")
        return C_TITLE

    context.user_data["title"] = value[:200]
    await update.message.reply_text(
        "2/5 Send the current price.\n"
        "Example: <b>1299</b>",
        parse_mode=ParseMode.HTML,
    )
    return C_PRICE


async def got_price(update, context):
    value = update.message.text.strip().replace("₹", "").strip()
    if not value:
        await update.message.reply_text("❌ Price cannot be empty. Try again:")
        return C_PRICE

    context.user_data["price"] = value[:30]
    await update.message.reply_text(
        "3/5 Send old/MRP price, or type <b>skip</b>:",
        parse_mode=ParseMode.HTML,
    )
    return C_OLD


async def got_old(update, context):
    value = update.message.text.strip()
    context.user_data["old_price"] = (
        None if value.lower() in {"skip", "-", "no"} else value.replace("₹", "").strip()[:30]
    )

    keyboard = [
        [InlineKeyboardButton("📱 Electronics", callback_data="addcat_electronics")],
        [InlineKeyboardButton("🎮 Gaming", callback_data="addcat_gaming")],
        [InlineKeyboardButton("👕 Fashion", callback_data="addcat_fashion")],
        [InlineKeyboardButton("🏠 Home & Kitchen", callback_data="addcat_home")],
        [InlineKeyboardButton("📚 Books", callback_data="addcat_books")],
    ]
    await update.message.reply_text(
        "4/5 <b>Choose a category:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return C_CATEGORY


async def got_category(update, context):
    value = update.message.text.strip().lower()
    if value not in CATEGORIES:
        await update.message.reply_text(
            "❌ Invalid category. Use: electronics, gaming, fashion, home, books"
        )
        return C_CATEGORY

    context.user_data["category"] = value
    await update.message.reply_text(
        "5/5 Send the product URL:\n"
        "Example: https://amazon.in/...",
        parse_mode=ParseMode.HTML,
    )
    return C_URL


async def got_url(update, context):
    url = update.message.text.strip()

    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text(
            "❌ Invalid URL. It must start with http:// or https://"
        )
        return C_URL

    d = context.user_data
    con = db()
    cur = con.execute(
        """
        INSERT INTO offers(title, price, old_price, category, url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            d["title"],
            d["price"],
            d["old_price"],
            d["category"],
            url,
        ),
    )
    offer_id = cur.lastrowid
    con.commit()
    con.close()

    row = get_offer(offer_id)

    await update.message.reply_text(
        "✅ <b>Offer added successfully!</b>\n\n"
        + offer_text(row),
        parse_mode=ParseMode.HTML,
        reply_markup=offer_markup(row),
    )

    await notify_subscribers(context, row)

    if CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=offer_text(row),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛒 Buy Now", url=url)]
                ]),
            )
        except Exception as exc:
            log.warning("Channel post failed: %s", exc)

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ <b>Add offer cancelled.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu() if is_admin(update) else None,
    )
    return ConversationHandler.END


# -------------------------
# NOTIFICATIONS
# -------------------------

async def notify_subscribers(context, row):
    con = db()
    users = con.execute(
        "SELECT user_id FROM subscribers WHERE enabled=1"
    ).fetchall()
    con.close()

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", url=row["url"])]
    ])

    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user["user_id"],
                text="🔔 <b>New Zoner Deal!</b>\n\n" + offer_text(row),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception as exc:
            log.debug("Could not notify %s: %s", user["user_id"], exc)


# -------------------------
# ADMIN
# -------------------------

async def admin_help(update, context):
    if not is_admin(update):
        return

    await update.message.reply_text(
        "🔐 <b>Zoner Admin Panel</b>\n\n"
        "Manage your offers easily:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu(),
    )


# -------------------------
# RENDER HEALTH SERVER
# -------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Zoner Offers Bot is running!")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# -------------------------
# START BOT
# -------------------------

def run_bot():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    init_db()
    Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[CommandHandler("addoffer", admin_start)],
        states={
            C_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_title)
            ],
            C_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_price)
            ],
            C_OLD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_old)
            ],
            C_CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_category)
            ],
            C_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_url)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("🔥 Zoner Offers Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    run_bot()
