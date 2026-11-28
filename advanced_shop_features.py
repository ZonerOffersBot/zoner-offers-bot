"""Additive advanced Zoner Shop features (PTB 21.9 + SQLite).
This module intentionally uses the existing bot database and digital_products table.
No payment provider is simulated; payment-related records remain manual/admin controlled.
"""
import asyncio
import html
import logging
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, MessageHandler, filters

log = logging.getLogger("zoner.advanced")
DB_FILE = None
_RATE = {}

def init_db(path):
    global DB_FILE
    DB_FILE = path
    con = sqlite3.connect(DB_FILE, timeout=30)
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shop_categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS shop_cart(
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,product_id)
        );
        CREATE TABLE IF NOT EXISTS shop_wishlist(
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,product_id)
        );
        CREATE TABLE IF NOT EXISTS shop_recent_views(
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, viewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,product_id)
        );
        CREATE TABLE IF NOT EXISTS shop_reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL UNIQUE, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            review TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS shop_coupons(
            code TEXT PRIMARY KEY, discount_percent INTEGER NOT NULL CHECK(discount_percent BETWEEN 1 AND 90),
            max_uses INTEGER NOT NULL DEFAULT 1, used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT, is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS shop_coupon_uses(
            code TEXT NOT NULL, user_id INTEGER NOT NULL, order_id INTEGER,
            used_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(code,user_id)
        );
        CREATE TABLE IF NOT EXISTS shop_disputes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS shop_faq(
            id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, answer TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS shop_texts(
            text_key TEXT PRIMARY KEY, text_value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS shop_audit_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
            details TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS shop_rate_limits(
            user_id INTEGER NOT NULL, action TEXT NOT NULL, window_start INTEGER NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,action)
        );
        CREATE TABLE IF NOT EXISTS shop_stock_waitlist(
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,product_id)
        );
        CREATE TABLE IF NOT EXISTS shop_payment_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount_paise INTEGER NOT NULL,
            method TEXT NOT NULL DEFAULT 'manual', reference TEXT, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, reviewed_by INTEGER, reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS shop_withdraw_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount_paise INTEGER NOT NULL,
            payout_details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, reviewed_by INTEGER, reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS shop_admin_roles(
            user_id INTEGER PRIMARY KEY, role TEXT NOT NULL DEFAULT 'support',
            permissions TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS shop_custom_commands(
            trigger TEXT PRIMARY KEY, response TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
        );
        """)
        con.commit()
    finally:
        con.close()

def db():
    if not DB_FILE:
        raise RuntimeError("Advanced shop database is not initialized")
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def _admin(update):
    import bot
    return bot.is_admin(update)

def _audit(con, admin_id, action, details=""):
    con.execute("INSERT INTO shop_audit_logs(admin_id,action,details) VALUES(?,?,?)",
                (int(admin_id), action[:80], details[:1000]))

def _rate_ok(user_id, action, limit=8, seconds=10):
    now = int(time.time())
    key = (int(user_id), action)
    start, hits = _RATE.get(key, (now, 0))
    if now - start >= seconds:
        start, hits = now, 0
    if hits >= limit:
        _RATE[key] = (start, hits)
        return False
    _RATE[key] = (start, hits + 1)
    return True

async def _deny_rate(update):
    if update.message:
        await update.message.reply_text("⏳ Thoda slow karein aur kuch seconds baad try karein.")
    return

async def search_command(update, context):
    if not _rate_ok(update.effective_user.id, "search", 6, 10):
        return await _deny_rate(update)
    term = " ".join(context.args or []).strip()
    if len(term) < 2:
        await update.message.reply_text("Use: /shopsearch <product name>")
        return
    con = db()
    try:
        rows = con.execute("""SELECT id,name,price_paise,stock_count,description FROM digital_products
            WHERE is_active=1 AND stock_count>0 AND (name LIKE ? OR description LIKE ?)
            ORDER BY id DESC LIMIT 15""", (f"%{term[:60]}%", f"%{term[:60]}%")).fetchall()
        if not rows:
            await update.message.reply_text("🔎 Koi matching in-stock product nahi mila.")
            return
        buttons = []
        for row in rows:
            con.execute("INSERT INTO shop_recent_views(user_id,product_id) VALUES(?,?) "
                        "ON CONFLICT(user_id,product_id) DO UPDATE SET viewed_at=CURRENT_TIMESTAMP",
                        (update.effective_user.id, row["id"]))
            buttons.append([InlineKeyboardButton(
                f'{row["name"][:32]} · ₹{row["price_paise"]/100:.2f}',
                callback_data=f'adv:view:{row["id"]}')])
        con.commit()
        await update.message.reply_text("🔎 <b>Search results</b>", parse_mode=ParseMode.HTML,
                                        reply_markup=InlineKeyboardMarkup(buttons))
    finally:
        con.close()

async def add_cart_command(update, context):
    if len(context.args) not in (1, 2):
        await update.message.reply_text("Use: /shopadd PRODUCT_ID [QUANTITY]")
        return
    try:
        pid = int(context.args[0]); qty = int(context.args[1]) if len(context.args) == 2 else 1
        if qty < 1 or qty > 20: raise ValueError
    except ValueError:
        await update.message.reply_text("Product ID / quantity invalid hai.")
        return
    con = db()
    try:
        product = con.execute("SELECT id,stock_count,is_active FROM digital_products WHERE id=?", (pid,)).fetchone()
        if not product or not product["is_active"] or product["stock_count"] < qty:
            await update.message.reply_text("Product unavailable ya stock kam hai.")
            return
        con.execute("""INSERT INTO shop_cart(user_id,product_id,quantity) VALUES(?,?,?)
            ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=MIN(shop_cart.quantity+excluded.quantity,20),
            updated_at=CURRENT_TIMESTAMP""", (update.effective_user.id, pid, qty))
        con.commit()
        await update.message.reply_text("🛒 Cart mein add ho gaya. /shopcart se dekhein.")
    finally:
        con.close()

async def cart_command(update, context):
    con = db()
    try:
        rows = con.execute("""SELECT c.product_id,c.quantity,p.name,p.price_paise,p.stock_count
            FROM shop_cart c JOIN digital_products p ON p.id=c.product_id
            WHERE c.user_id=? ORDER BY c.updated_at DESC""", (update.effective_user.id,)).fetchall()
        if not rows:
            await update.message.reply_text("🛒 Cart empty hai. /shopsearch se products dhoondhein.")
            return
        total = 0; lines = []
        for row in rows:
            line = row["price_paise"] * row["quantity"]; total += line
            lines.append(f'• {html.escape(row["name"])} × {row["quantity"]} — ₹{line/100:.2f}')
        await update.message.reply_text("🛒 <b>Your cart</b>\n" + "\n".join(lines) +
            f"\n\n<b>Subtotal: ₹{total/100:.2f}</b>\nCheckout: individual product Buy buttons se karein.",
            parse_mode=ParseMode.HTML)
    finally:
        con.close()

async def wishlist_command(update, context):
    con = db()
    try:
        rows = con.execute("""SELECT p.id,p.name,p.price_paise,p.stock_count FROM shop_wishlist w
            JOIN digital_products p ON p.id=w.product_id WHERE w.user_id=? ORDER BY w.created_at DESC LIMIT 20""",
            (update.effective_user.id,)).fetchall()
        if not rows:
            await update.message.reply_text("♡ Wishlist empty hai. Product ke liye /shopwish PRODUCT_ID use karein.")
            return
        await update.message.reply_text("♡ <b>Wishlist</b>\n" + "\n".join(
            f'#{r["id"]} {html.escape(r["name"])} · ₹{r["price_paise"]/100:.2f} · stock {r["stock_count"]}'
            for r in rows), parse_mode=ParseMode.HTML)
    finally:
        con.close()

async def wish_add_command(update, context):
    try: pid = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text("Use: /shopwish PRODUCT_ID"); return
    con = db()
    try:
        exists = con.execute("SELECT 1 FROM digital_products WHERE id=? AND is_active=1", (pid,)).fetchone()
        if not exists:
            await update.message.reply_text("Product nahi mila."); return
        con.execute("INSERT OR IGNORE INTO shop_wishlist(user_id,product_id) VALUES(?,?)",
                    (update.effective_user.id, pid)); con.commit()
        await update.message.reply_text("♡ Wishlist updated. /shopwishlist se dekhein.")
    finally: con.close()

async def recent_command(update, context):
    con = db()
    try:
        rows = con.execute("""SELECT p.id,p.name,p.price_paise FROM shop_recent_views v
            JOIN digital_products p ON p.id=v.product_id WHERE v.user_id=?
            ORDER BY v.viewed_at DESC LIMIT 10""", (update.effective_user.id,)).fetchall()
        await update.message.reply_text("🕘 <b>Recently viewed</b>\n" + ("\n".join(
            f'#{r["id"]} {html.escape(r["name"])} · ₹{r["price_paise"]/100:.2f}' for r in rows) or "No history."),
            parse_mode=ParseMode.HTML)
    finally: con.close()

async def review_command(update, context):
    if len(context.args) < 2:
        await update.message.reply_text("Use: /shopreview ORDER_ID 1-5 [review text]"); return
    try:
        oid = int(context.args[0]); rating = int(context.args[1])
        if rating < 1 or rating > 5: raise ValueError
    except ValueError:
        await update.message.reply_text("Order ID aur rating 1–5 valid honi chahiye."); return
    body = " ".join(context.args[2:])[:500]
    con = db()
    try:
        order = con.execute("SELECT product_id,status FROM shop_orders WHERE id=? AND user_id=?",
                            (oid, update.effective_user.id)).fetchone()
        if not order or order["status"] not in ("paid", "delivered"):
            await update.message.reply_text("Review sirf apne delivered order par de sakte hain."); return
        con.execute("INSERT INTO shop_reviews(user_id,product_id,order_id,rating,review) VALUES(?,?,?,?,?)",
                    (update.effective_user.id, order["product_id"], oid, rating, body))
        con.commit()
        await update.message.reply_text("⭐ Review saved. Thank you!")
    except sqlite3.IntegrityError:
        await update.message.reply_text("Is order ka review pehle submit ho chuka hai.")
    finally: con.close()

async def dispute_command(update, context):
    if len(context.args) < 2:
        await update.message.reply_text("Use: /shopdispute ORDER_ID reason"); return
    try: oid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid order ID."); return
    reason = " ".join(context.args[1:])[:1000]
    con = db()
    try:
        order = con.execute("SELECT id FROM shop_orders WHERE id=? AND user_id=?", (oid, update.effective_user.id)).fetchone()
        if not order:
            await update.message.reply_text("Order nahi mila."); return
        con.execute("INSERT INTO shop_disputes(order_id,user_id,reason) VALUES(?,?,?)",
                    (oid, update.effective_user.id, reason))
        con.commit()
        await update.message.reply_text(f"🆘 Dispute #{oid} admin review ke liye submit ho gaya.")
        import bot
        for admin_id in bot.ADMIN_IDS:
            try:
                await context.bot.send_message(int(admin_id),
                    f"🆘 Shop dispute #{oid}\nUser: {update.effective_user.id}\n{html.escape(reason)}",
                    parse_mode=ParseMode.HTML)
            except Exception: log.exception("Dispute admin notification failed")
    except sqlite3.IntegrityError:
        await update.message.reply_text("Is order par dispute already open/submitted hai.")
    finally: con.close()

async def faq_command(update, context):
    con = db()
    try:
        rows = con.execute("SELECT id,question FROM shop_faq WHERE is_active=1 ORDER BY id LIMIT 20").fetchall()
        if not rows:
            await update.message.reply_text("FAQ abhi configure nahi hua. Support ke liye /shop use karein."); return
        kb = [[InlineKeyboardButton(r["question"][:60], callback_data=f"adv:faq:{r['id']}")] for r in rows]
        await update.message.reply_text("❓ <b>Frequently asked questions</b>", parse_mode=ParseMode.HTML,
                                        reply_markup=InlineKeyboardMarkup(kb))
    finally: con.close()

async def status_command(update, context):
    import bot
    started = getattr(bot, "STARTED_AT", None)
    con = db()
    try:
        con.execute("SELECT 1").fetchone()
        products = con.execute("SELECT COUNT(*) FROM digital_products").fetchone()[0]
        orders = con.execute("SELECT COUNT(*) FROM shop_orders").fetchone()[0]
        db_state = "connected"
    except Exception:
        db_state = "error"; products = orders = 0
    finally: con.close()
    await update.message.reply_text(f"🩺 <b>Zoner Shop Status</b>\nDatabase: <b>{db_state}</b>\nProducts: <b>{products}</b>\nOrders: <b>{orders}</b>\nRuntime: <code>PTB + SQLite</code>",
                                    parse_mode=ParseMode.HTML)

async def admin_coupon_command(update, context):
    if not _admin(update): return
    if len(context.args) < 3:
        await update.message.reply_text("Use: /shopcouponadd CODE DISCOUNT_PERCENT MAX_USES [YYYY-MM-DD]")
        return
    try:
        code = context.args[0].upper()[:32]
        percent = int(context.args[1]); max_uses = int(context.args[2])
        expiry = context.args[3] if len(context.args) > 3 else None
        if not code.isalnum() or not 1 <= percent <= 90 or not 1 <= max_uses <= 100000: raise ValueError
        if expiry: datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Coupon values invalid hain."); return
    con = db()
    try:
        con.execute("INSERT INTO shop_coupons(code,discount_percent,max_uses,expires_at) VALUES(?,?,?,?)",
                    (code, percent, max_uses, expiry))
        _audit(con, update.effective_user.id, "coupon_create", code)
        con.commit()
        await update.message.reply_text(f"✅ Coupon {code} created ({percent}% · {max_uses} uses).")
    except sqlite3.IntegrityError:
        await update.message.reply_text("Coupon code already exists.")
    finally: con.close()

async def admin_faq_command(update, context):
    if not _admin(update): return
    raw = " ".join(context.args or [])
    if "|" not in raw:
        await update.message.reply_text("Use: /shopfaqadd QUESTION|ANSWER"); return
    question, answer = [x.strip() for x in raw.split("|", 1)]
    if not question or not answer:
        await update.message.reply_text("Question aur answer dono required."); return
    con = db()
    try:
        con.execute("INSERT INTO shop_faq(question,answer) VALUES(?,?)", (question[:250], answer[:1500]))
        _audit(con, update.effective_user.id, "faq_add", question[:200]); con.commit()
    finally: con.close()
    await update.message.reply_text("✅ FAQ added.")

async def admin_text_command(update, context):
    if not _admin(update): return
    raw = " ".join(context.args or [])
    if "|" not in raw:
        await update.message.reply_text("Use: /shoptext KEY|New text"); return
    key, value = [x.strip() for x in raw.split("|", 1)]
    if not key or not value:
        await update.message.reply_text("Key aur text required."); return
    con = db()
    try:
        con.execute("INSERT INTO shop_texts(text_key,text_value) VALUES(?,?) ON CONFLICT(text_key) DO UPDATE SET text_value=excluded.text_value,updated_at=CURRENT_TIMESTAMP",
                    (key[:80], value[:3000]))
        _audit(con, update.effective_user.id, "text_update", key[:80]); con.commit()
    finally: con.close()
    await update.message.reply_text("✅ Shop text saved. (Existing hardcoded messages are not globally replaced yet.)")

async def admin_audit_command(update, context):
    if not _admin(update): return
    con = db()
    try:
        rows = con.execute("SELECT admin_id,action,details,created_at FROM shop_audit_logs ORDER BY id DESC LIMIT 15").fetchall()
        body = "\n".join(f'{r["created_at"]} · {r["admin_id"]} · {r["action"]} · {r["details"][:70]}' for r in rows) or "No admin actions logged yet."
    finally: con.close()
    await update.message.reply_text("🧾 <b>Recent Shop Admin Audit</b>\n" + html.escape(body), parse_mode=ParseMode.HTML)

async def callback_handler(update, context):
    q = update.callback_query
    data = q.data or ""
    if not data.startswith("adv:"): return
    await q.answer()
    parts = data.split(":")
    con = db()
    try:
        if len(parts) == 3 and parts[1] == "view":
            pid = int(parts[2])
            p = con.execute("SELECT id,name,description,price_paise,stock_count FROM digital_products WHERE id=? AND is_active=1", (pid,)).fetchone()
            if not p:
                await q.edit_message_text("Product unavailable."); return
            con.execute("INSERT INTO shop_recent_views(user_id,product_id) VALUES(?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET viewed_at=CURRENT_TIMESTAMP",
                        (q.from_user.id, pid))
            con.commit()
            text = f'<b>{html.escape(p["name"])}</b>\n{html.escape(p["description"] or "")}\nPrice: ₹{p["price_paise"]/100:.2f}\nStock: {p["stock_count"]}'
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("♡ Wishlist", callback_data=f"adv:wish:{pid}")],
                                       [InlineKeyboardButton("🛒 Add to cart", callback_data=f"adv:cart:{pid}")]])
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif len(parts) == 3 and parts[1] in ("wish", "cart"):
            pid = int(parts[2])
            p = con.execute("SELECT id,stock_count,is_active FROM digital_products WHERE id=?", (pid,)).fetchone()
            if not p or not p["is_active"]:
                await q.edit_message_text("Product unavailable."); return
            if parts[1] == "wish":
                con.execute("INSERT OR IGNORE INTO shop_wishlist(user_id,product_id) VALUES(?,?)", (q.from_user.id,pid))
                msg = "♡ Added to wishlist."
            else:
                if p["stock_count"] < 1:
                    await q.edit_message_text("Out of stock."); return
                con.execute("INSERT INTO shop_cart(user_id,product_id,quantity) VALUES(?,?,1) ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=MIN(shop_cart.quantity+1,20),updated_at=CURRENT_TIMESTAMP",
                            (q.from_user.id,pid))
                msg = "🛒 Added to cart. Use /shopcart to review."
            con.commit()
            await q.edit_message_text(msg)
        elif len(parts) == 3 and parts[1] == "faq":
            faq = con.execute("SELECT question,answer FROM shop_faq WHERE id=? AND is_active=1", (int(parts[2]),)).fetchone()
            await q.edit_message_text(f'<b>{html.escape(faq["question"])}</b>\n\n{html.escape(faq["answer"])}' if faq else "FAQ not found.",
                                      parse_mode=ParseMode.HTML)
    except (ValueError, sqlite3.Error):
        log.exception("Advanced shop callback failed")
        await q.edit_message_text("Action process nahi ho paya. Dobara try karein.")
    finally:
        con.close()

def install(app):
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^adv:"), group=1)
    app.add_handler(CommandHandler("shopsearch", search_command))
    app.add_handler(CommandHandler("shopadd", add_cart_command))
    app.add_handler(CommandHandler("shopcart", cart_command))
    app.add_handler(CommandHandler("shopwishlist", wishlist_command))
    app.add_handler(CommandHandler("shopwish", wish_add_command))
    app.add_handler(CommandHandler("shoprecent", recent_command))
    app.add_handler(CommandHandler("shopreview", review_command))
    app.add_handler(CommandHandler("shopdispute", dispute_command))
    app.add_handler(CommandHandler("shopfaq", faq_command))
    app.add_handler(CommandHandler("shopstatus", status_command))
    app.add_handler(CommandHandler("shopcouponadd", admin_coupon_command))
    app.add_handler(CommandHandler("shopfaqadd", admin_faq_command))
    app.add_handler(CommandHandler("shoptext", admin_text_command))
    app.add_handler(CommandHandler("shopaudit", admin_audit_command))
