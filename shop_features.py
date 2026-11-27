import asyncio
import html
import logging
import random
import sqlite3
from feature_controls import FEATURES
from datetime import date
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, MessageHandler, filters

log = logging.getLogger("zoner.shop")
DB_FILE = None

def connect():
    if not DB_FILE:
        raise RuntimeError("Shop SQLite is not initialized")
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db(path):
    global DB_FILE
    DB_FILE = path
    con = connect()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shop_users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, referral_code TEXT UNIQUE, referred_by INTEGER, is_blocked INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS user_wallets(user_id INTEGER PRIMARY KEY, balance_paise INTEGER NOT NULL DEFAULT 0, loyalty_points INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS wallet_transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount_paise INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS digital_products(id INTEGER PRIMARY KEY AUTOINCREMENT, category_id TEXT DEFAULT 'general', name TEXT NOT NULL, price_paise INTEGER NOT NULL DEFAULT 0, stock_count INTEGER NOT NULL DEFAULT 0, file_id TEXT, description TEXT, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS shop_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'paid', delivered_file_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS referrals(id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL, referred_id INTEGER NOT NULL UNIQUE, commission_paise INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS daily_rewards(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reward_paise INTEGER NOT NULL DEFAULT 0, claimed_on TEXT NOT NULL, UNIQUE(user_id, claimed_on));
        CREATE TABLE IF NOT EXISTS support_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, full_name TEXT, query TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', admin_reply TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS shop_feature_flags(feature_key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS shop_spin_history(user_id INTEGER NOT NULL, spin_date TEXT NOT NULL, prize_paise INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id,spin_date));
        """)
        user_columns={r["name"] for r in con.execute("PRAGMA table_info(shop_users)").fetchall()}
        if "is_blocked" not in user_columns:
            con.execute("ALTER TABLE shop_users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0")
        con.executemany("INSERT OR IGNORE INTO shop_feature_flags(feature_key,enabled) VALUES(?,1)",[(item["key"],) for item in FEATURES])
        con.commit()
    finally:
        con.close()

def register_user(user,referrer_id=None):
    con=connect()
    try:
        existing=con.execute("SELECT user_id FROM shop_users WHERE user_id=?",(user.id,)).fetchone()
        con.execute("INSERT INTO shop_users(user_id,username,first_name,referral_code) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name",
                    (user.id,user.username,user.first_name,"Z"+format(user.id,"X")))
        con.execute("INSERT OR IGNORE INTO user_wallets(user_id) VALUES(?)",(user.id,))
        if not existing and referrer_id and int(referrer_id)!=int(user.id):
            ref=con.execute("SELECT user_id FROM shop_users WHERE user_id=?",(int(referrer_id),)).fetchone()
            if ref:
                cur=con.execute("INSERT OR IGNORE INTO referrals(referrer_id,referred_id,commission_paise) VALUES(?,?,1000)",(int(referrer_id),user.id))
                if cur.rowcount:
                    con.execute("UPDATE user_wallets SET balance_paise=balance_paise+1000 WHERE user_id=?",(int(referrer_id),))
                    con.execute("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",(int(referrer_id),1000,"referral_bonus:%s"%user.id))
        con.commit()
    finally: con.close()

def is_admin(update):
    import bot
    return bot.is_admin(update)

def home_markup():
    return InlineKeyboardMarkup([
      [InlineKeyboardButton("🛍️ Shop Products",callback_data="shop:products"),InlineKeyboardButton("👛 My Wallet",callback_data="shop:wallet")],
      [InlineKeyboardButton("🎁 Daily Reward",callback_data="shop:reward"),InlineKeyboardButton("📦 My Orders",callback_data="shop:orders")],
      [InlineKeyboardButton("🤝 Refer & Earn",callback_data="shop:referral"),InlineKeyboardButton("🎟️ Spin & Win",callback_data="shop:spin")],
      [InlineKeyboardButton("🆘 Help / Support",callback_data="shop:support")]])

async def allowed(update,context):
    import bot
    uid=update.effective_user.id
    con=connect()
    try:
        blocked=con.execute("SELECT is_blocked FROM shop_users WHERE user_id=?",(uid,)).fetchone()
    finally: con.close()
    if blocked and blocked["is_blocked"] and not bot.is_admin(update):
        if update.callback_query: await update.callback_query.answer("🚫 Shop access blocked by admin.",show_alert=True)
        else: await update.message.reply_text("🚫 Shop access blocked by admin.")
        return False
    if bot.is_user_verified(uid) or bot.is_admin(update):
        return True
    try:
        verified=await bot.membership_status(context.bot,uid)
    except Exception:
        verified=False
    if verified:
        bot.mark_user_verified(uid)
        return True
    if update.callback_query:
        await update.callback_query.edit_message_text(bot.join_gate_text(),parse_mode=ParseMode.HTML,reply_markup=bot.join_gate_markup())
    else:
        await update.message.reply_text(bot.join_gate_text(),parse_mode=ParseMode.HTML,reply_markup=bot.join_gate_markup())
    return False

async def shop_command(update,context):
    if not await allowed(update,context): return
    register_user(update.effective_user)
    await update.message.reply_text("🛍️ <b>Zoner Shop</b>\nChoose a section. Existing deal browsing remains available with /start.",parse_mode=ParseMode.HTML,reply_markup=home_markup())

async def admin_command(update,context):
    if not await allowed(update,context): return
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only."); return
    con=connect()
    try:
        counts=[con.execute("SELECT COUNT(*) FROM shop_users").fetchone()[0],con.execute("SELECT COUNT(*) FROM digital_products WHERE is_active=1").fetchone()[0],con.execute("SELECT COUNT(*) FROM shop_orders").fetchone()[0],con.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0],con.execute("SELECT COALESCE(SUM(amount_paise),0) FROM shop_orders WHERE status='paid'").fetchone()[0]]
    finally: con.close()
    kb=admin_markup()
    await update.message.reply_text("🛠️ <b>Zoner Shop Admin</b>\nUsers: %s\nProducts: %s\nOrders: %s\nOpen tickets: %s\nRecorded revenue: ₹%.2f\n\n/shopproduct Name|Price|Stock|Description|FileID\n/shopwallet USER_ID AMOUNT_RUPEES"%(counts[0],counts[1],counts[2],counts[3],counts[4]/100),parse_mode=ParseMode.HTML,reply_markup=kb)

async def add_product_command(update,context):
    if not is_admin(update): return
    parts=[x.strip() for x in " ".join(context.args or []).split("|")]
    if len(parts)<3:
        await update.message.reply_text("Format: /shopproduct Name|Price|Stock|Description|TelegramFileID"); return
    try:
        name=parts[0][:120]; price=round(float(parts[1].replace("₹","").replace(",",""))*100); stock=max(0,int(parts[2]))
        if price<0: raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid price or stock."); return
    desc=parts[3] if len(parts)>3 else ""; file_id=parts[4] if len(parts)>4 else None
    con=connect()
    try:
        cur=con.execute("INSERT INTO digital_products(name,price_paise,stock_count,description,file_id) VALUES(?,?,?,?,?)",(name,price,stock,desc,file_id)); con.commit()
    finally: con.close()
    await update.message.reply_text("✅ Product #%s added: %s"%(cur.lastrowid,html.escape(name)))

async def wallet_adjust_command(update,context):
    if not is_admin(update): return
    try: uid=int(context.args[0]); amount=round(float(context.args[1])*100)
    except (ValueError,IndexError):
        await update.message.reply_text("Use /shopwallet USER_ID AMOUNT_RUPEES"); return
    con=connect()
    try:
        con.execute("INSERT OR IGNORE INTO user_wallets(user_id) VALUES(?)",(uid,))
        con.execute("UPDATE user_wallets SET balance_paise=balance_paise+? WHERE user_id=?",(amount,uid))
        con.execute("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",(uid,amount,"admin_adjustment")); con.commit()
        bal=con.execute("SELECT balance_paise FROM user_wallets WHERE user_id=?",(uid,)).fetchone()[0]
    finally: con.close()
    await update.message.reply_text("✅ Wallet updated. Balance: ₹%.2f"%(bal/100))

async def deliver_order_command(update,context):
    if not is_admin(update): return
    try: oid=int(context.args[0])
    except (ValueError,IndexError):
        await update.message.reply_text("Use /shopdeliver ORDER_ID"); return
    con=connect()
    try:
        row=con.execute("SELECT user_id,delivered_file_id FROM shop_orders WHERE id=? AND status='delivery_pending'",(oid,)).fetchone()
        if not row:
            await update.message.reply_text("Pending delivery order not found."); return
        await context.bot.send_document(row["user_id"],row["delivered_file_id"],caption="Purchase delivered · Order #%s"%oid)
        con.execute("UPDATE shop_orders SET status='paid' WHERE id=?",(oid,)); con.commit()
    except Exception as exc:
        log.exception("Manual delivery retry failed for order %s",oid)
        await update.message.reply_text("Delivery retry failed: %s"%type(exc).__name__); return
    finally: con.close()
    await update.message.reply_text("✅ Order #%s delivered."%oid)

async def reply_command(update,context):
    if not is_admin(update): return
    try: tid=int(context.args[0])
    except (ValueError,IndexError):
        await update.message.reply_text("Use /shopreply TICKET_ID"); return
    con=connect()
    try: row=con.execute("SELECT user_id FROM support_tickets WHERE id=? AND status='open'",(tid,)).fetchone()
    finally: con.close()
    if not row:
        await update.message.reply_text("Open ticket not found."); return
    context.user_data["shop_reply_ticket"]=tid
    await update.message.reply_text("Send the reply message now.")

async def text_router(update,context):
    if not update.message or not update.message.text: return
    if context.user_data.get("shop_support_wait"):
        body=update.message.text.strip()[:3000]
        con=connect()
        try:
            cur=con.execute("INSERT INTO support_tickets(user_id,full_name,query) VALUES(?,?,?)",(update.effective_user.id,update.effective_user.full_name,body)); con.commit(); tid=cur.lastrowid
        finally: con.close()
        context.user_data.pop("shop_support_wait",None)
        await update.message.reply_text("🎟️ Support ticket #%s created. Use /mytickets to track it."%tid)
        try:
            import bot as zoner_bot
            for admin_id in zoner_bot.ADMIN_IDS:
                await context.bot.send_message(int(admin_id),"🆘 <b>New private support ticket #%s</b>\nUser: <code>%s</code>\n%s"%(tid,update.effective_user.id,html.escape(body[:2500])),parse_mode=ParseMode.HTML)
        except Exception:
            log.exception("Could not notify shop admin about ticket #%s",tid)
        return
    tid=context.user_data.get("shop_reply_ticket")
    if tid and is_admin(update):
        body=update.message.text.strip()[:3000]; con=connect()
        try:
            row=con.execute("SELECT user_id FROM support_tickets WHERE id=?",(tid,)).fetchone()
            if row:
                con.execute("UPDATE support_tickets SET admin_reply=?,status='answered',updated_at=CURRENT_TIMESTAMP WHERE id=?",(body,tid)); con.commit()
                await context.bot.send_message(row["user_id"],"💬 Support reply for ticket #%s:\n%s"%(tid,body))
        finally: con.close()
        context.user_data.pop("shop_reply_ticket",None)
        await update.message.reply_text("✅ Reply sent.")

async def mytickets_command(update,context):
    con=connect()
    try: rows=con.execute("SELECT id,status,query,admin_reply FROM support_tickets WHERE user_id=? ORDER BY id DESC LIMIT 10",(update.effective_user.id,)).fetchall()
    finally: con.close()
    if not rows:
        await update.message.reply_text("No support tickets yet."); return
    lines=["#%s · %s\n%s%s"%(r["id"],r["status"],r["query"][:160],("\nAdmin: "+r["admin_reply"][:160]) if r["admin_reply"] else "") for r in rows]
    await update.message.reply_text("\n\n".join(lines))


def admin_markup():
    return InlineKeyboardMarkup([
      [InlineKeyboardButton("📊 Stats",callback_data="shopadmin:stats"),InlineKeyboardButton("👥 Users",callback_data="shopadmin:users")],
      [InlineKeyboardButton("📦 Stock / Products",callback_data="shopadmin:products"),InlineKeyboardButton("➕ Upload Product",callback_data="shopadmin:add_product")],
      [InlineKeyboardButton("💰 Wallets",callback_data="shopadmin:wallets"),InlineKeyboardButton("✏️ Edit Wallet",callback_data="shopadmin:edit_wallet")],
      [InlineKeyboardButton("🎁 Bulk ₹10 Reward",callback_data="shopadmin:bulk"),InlineKeyboardButton("📋 Orders",callback_data="shopadmin:orders")],
      [InlineKeyboardButton("📢 Broadcast",callback_data="shopadmin:broadcast"),InlineKeyboardButton("📊 Export CSV",callback_data="shopadmin:csv")],
      [InlineKeyboardButton("🆘 Support Tickets",callback_data="shopadmin:tickets"),InlineKeyboardButton("⚙️ Feature Flags",callback_data="shopadmin:flags")],
      [InlineKeyboardButton("🟢/🔴 Shop Online / OFF",callback_data="shopadmin:toggle"),InlineKeyboardButton("⬅️ Main Admin",callback_data="shopadmin:close")]
    ])

def _shop_is_online(con):
    row=con.execute("SELECT value FROM settings WHERE key='shop_online'").fetchone()
    return row is None or row["value"] != "0"

def _admin_text_state(update, context):
    return context.user_data.get("shop_admin_state")

async def admin_message_router(update,context):
    msg=update.message
    if not msg or not update.effective_user or not is_admin(update):
        return
    state=_admin_text_state(update,context)
    if not state:
        return
    if state=="product_file":
        if not msg.document:
            await msg.reply_text("Please Telegram Document bhejein.")
            return
        draft=context.user_data.get("shop_product_draft",{})
        draft["file_id"]=msg.document.file_id
        context.user_data["shop_product_draft"]=draft
        context.user_data["shop_admin_state"]="product_stock"
        await msg.reply_text("📦 Available stock quantity bhejein (0 bhi allowed hai).")
        return
    text=(msg.text or "").strip()
    if text.lower()=="/cancel":
        context.user_data.pop("shop_admin_state",None)
        context.user_data.pop("shop_product_draft",None)
        await msg.reply_text("❎ Cancelled.",reply_markup=admin_markup())
        return
    if state=="product_name":
        if not text or len(text)>120:
            await msg.reply_text("Product name 1–120 characters ka bhejein.")
            return
        context.user_data["shop_product_draft"]={"name":text}
        context.user_data["shop_admin_state"]="product_price"
        await msg.reply_text("💰 Product price ₹ mein bhejein (example: 49.00).")
    elif state=="product_price":
        try:
            price=round(float(text.replace("₹","").replace(",",""))*100)
            if price<0 or price>100000000: raise ValueError()
        except ValueError:
            await msg.reply_text("Valid positive price bhejein, example 49.00.")
            return
        context.user_data["shop_product_draft"]["price_paise"]=price
        context.user_data["shop_admin_state"]="product_file"
        await msg.reply_text("📤 Ab product ki delivery file Telegram Document ke roop mein bhejein.")
    elif state=="product_stock":
        if not text.isdigit() or int(text)>1000000:
            await msg.reply_text("Stock 0–1000000 ke beech integer bhejein.")
            return
        draft=context.user_data.get("shop_product_draft",{})
        con=connect()
        try:
            cur=con.execute("INSERT INTO digital_products(name,price_paise,stock_count,file_id,description,is_active) VALUES(?,?,?,?,?,1)",
                (draft["name"],draft["price_paise"],int(text),draft["file_id"],draft.get("description","")))
            con.commit(); pid=cur.lastrowid
        finally: con.close()
        context.user_data.pop("shop_product_draft",None)
        context.user_data.pop("shop_admin_state",None)
        await msg.reply_text("✅ Product #%s added successfully.\nName: %s\nPrice: ₹%.2f\nStock: %s"%(pid,html.escape(draft["name"]),draft["price_paise"]/100,int(text)),parse_mode=ParseMode.HTML,reply_markup=admin_markup())
    elif state=="wallet_user":
        if not text.isdigit():
            await msg.reply_text("Valid Telegram User ID bhejein.")
            return
        context.user_data["shop_wallet_uid"]=int(text)
        context.user_data["shop_admin_state"]="wallet_amount"
        await msg.reply_text("Amount ₹ mein bhejein. Credit ke liye positive, debit ke liye negative (example -25).")
    elif state=="wallet_amount":
        try:
            amount=round(float(text.replace("₹","").replace(",",""))*100)
            if amount==0 or abs(amount)>100000000: raise ValueError()
        except ValueError:
            await msg.reply_text("Non-zero valid amount bhejein, example 50 ya -25.")
            return
        uid=context.user_data.pop("shop_wallet_uid")
        con=connect()
        try:
            con.execute("INSERT OR IGNORE INTO user_wallets(user_id) VALUES(?)",(uid,))
            con.execute("UPDATE user_wallets SET balance_paise=balance_paise+? WHERE user_id=?",(amount,uid))
            con.execute("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",(uid,amount,"admin_adjustment"))
            bal=con.execute("SELECT balance_paise FROM user_wallets WHERE user_id=?",(uid,)).fetchone()[0]
            con.commit()
        finally: con.close()
        context.user_data.pop("shop_admin_state",None)
        await msg.reply_text("✅ Wallet updated for %s. New balance: ₹%.2f"%(uid,bal/100),reply_markup=admin_markup())
    elif state=="broadcast":
        context.user_data.pop("shop_admin_state",None)
        con=connect()
        try:
            recipients=[r["user_id"] for r in con.execute("SELECT user_id FROM subscribers WHERE enabled=1").fetchall()]
        finally: con.close()
        status=await msg.reply_text("📢 Broadcast started for %s subscribed users..."%len(recipients))
        sent=failed=0
        for uid in recipients:
            try:
                await context.bot.copy_message(chat_id=uid,from_chat_id=msg.chat_id,message_id=msg.message_id)
                sent+=1
            except Exception:
                failed+=1
            await asyncio.sleep(0.05)
        await status.edit_text("✅ Broadcast complete\n📨 Sent: %s\n❌ Failed: %s\n👥 Targeted: %s"%(sent,failed,len(recipients)),reply_markup=admin_markup())
    elif state.startswith("ticket_reply:"):
        tid=int(state.split(":",1)[1])
        con=connect()
        try:
            row=con.execute("SELECT user_id FROM support_tickets WHERE id=? AND status='open'",(tid,)).fetchone()
            if not row:
                await msg.reply_text("Open ticket nahi mila."); return
            con.execute("UPDATE support_tickets SET admin_reply=?,status='answered',updated_at=CURRENT_TIMESTAMP WHERE id=?",(text[:3000],tid))
            con.commit()
            target=row["user_id"]
        finally: con.close()
        context.user_data.pop("shop_admin_state",None)
        try: await context.bot.send_message(target,"💬 <b>Support reply · Ticket #%s</b>\n%s"%(tid,html.escape(text[:3000])),parse_mode=ParseMode.HTML)
        except Exception: await msg.reply_text("Ticket updated, lekin user ko DM deliver nahi hua.")
        await msg.reply_text("✅ Ticket #%s ka reply save kar diya."%tid,reply_markup=admin_markup())

async def admin_document_router(update,context):
    msg=update.message
    if not msg or not is_admin(update) or context.user_data.get("shop_admin_state")!="product_file":
        return
    if not msg.document:
        await msg.reply_text("Please Telegram Document bhejein.")
        return
    draft=context.user_data.get("shop_product_draft",{})
    draft["file_id"]=msg.document.file_id
    context.user_data["shop_product_draft"]=draft
    context.user_data["shop_admin_state"]="product_stock"
    await msg.reply_text("📦 Available stock quantity bhejein (0 bhi allowed hai).")

async def admin_callback(update,context):
    q=update.callback_query
    data=q.data or ""
    if not is_admin(update):
        await q.answer("Admin only.",show_alert=True); return True
    await q.answer()
    action=data.split(":",1)[1]
    if action in ("home","close"):
        await q.edit_message_text("🛠️ <b>Zoner Shop Controls</b>\nChoose a control:",parse_mode=ParseMode.HTML,reply_markup=admin_markup() if action=="home" else None)
        return True
    con=connect()
    try:
        if action=="toggle":
            new="0" if _shop_is_online(con) else "1"
            con.execute("INSERT INTO settings(key,value) VALUES('shop_online',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(new,))
            con.commit()
            await q.edit_message_text("🔴 Shop is OFF. Deal publishing and existing Zoner Offers features remain unchanged." if new=="0" else "🟢 Shop is ONLINE.",reply_markup=admin_markup())
        elif action=="stats":
            users=con.execute("SELECT COUNT(*) FROM shop_users").fetchone()[0]
            orders=con.execute("SELECT COUNT(*) FROM shop_orders").fetchone()[0]
            revenue=con.execute("SELECT COALESCE(SUM(amount_paise),0) FROM shop_orders WHERE status='paid'").fetchone()[0]
            wallets=con.execute("SELECT COALESCE(SUM(balance_paise),0) FROM user_wallets").fetchone()[0]
            tickets=con.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0]
            await q.edit_message_text("📊 <b>Shop Statistics</b>\n👥 Users: %s\n🧾 Orders: %s\n💵 Recorded revenue: ₹%.2f\n👛 Wallet liability: ₹%.2f\n🆘 Open tickets: %s"%(users,orders,revenue/100,wallets/100,tickets),parse_mode=ParseMode.HTML,reply_markup=admin_markup())
        elif action=="users":
            rows=con.execute("SELECT user_id,username,first_name,is_blocked FROM shop_users ORDER BY created_at DESC LIMIT 25").fetchall()
            buttons=[[InlineKeyboardButton(("🚫 Block " if not r["is_blocked"] else "✅ Unblock ")+"%s · %s"%(r["user_id"],(r["first_name"] or "")[:18]),callback_data="shopadmin:block:%s"%r["user_id"])] for r in rows]
            buttons.append([InlineKeyboardButton("⬅️ Admin Controls",callback_data="shopadmin:home")])
            await q.edit_message_text("👥 <b>Recent Shop Users</b>\nTap to block/unblock shop access.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons))
        elif action=="products":
            rows=con.execute("SELECT id,name,price_paise,stock_count,is_active FROM digital_products ORDER BY id DESC LIMIT 25").fetchall()
            buttons=[[InlineKeyboardButton(("🟢" if r["is_active"] else "🔴")+" #%s %s · ₹%.2f · stock %s"%(r["id"],r["name"][:24],r["price_paise"]/100,r["stock_count"]),callback_data="shopadmin:product_toggle:%s"%r["id"])] for r in rows]
            buttons.append([InlineKeyboardButton("⬅️ Admin Controls",callback_data="shopadmin:home")])
            await q.edit_message_text("📦 <b>Product stock</b>\nTap a product to activate/deactivate it.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons))
        elif action.startswith("block:"):
            target=int(action.split(":")[1])
            con.execute("UPDATE shop_users SET is_blocked=CASE WHEN is_blocked=1 THEN 0 ELSE 1 END WHERE user_id=?",(target,))
            con.commit()
            row=con.execute("SELECT is_blocked FROM shop_users WHERE user_id=?",(target,)).fetchone()
            await q.edit_message_text("🚫 User %s shop access blocked."%target if row and row["is_blocked"] else "✅ User %s shop access restored."%target,reply_markup=admin_markup())
        elif action.startswith("product_toggle:"):
            pid=int(action.split(":")[1])
            con.execute("UPDATE digital_products SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",(pid,)); con.commit()
            await q.edit_message_text("✅ Product status updated.",reply_markup=admin_markup())
        elif action=="add_product":
            context.user_data["shop_admin_state"]="product_name"
            context.user_data["shop_product_draft"]={}
            await q.edit_message_text("📦 Product ka naam bhejein. Cancel: /cancel")
        elif action=="wallets":
            rows=con.execute("SELECT user_id,balance_paise FROM user_wallets ORDER BY balance_paise DESC LIMIT 20").fetchall()
            body="\n".join("• %s — ₹%.2f"%(r["user_id"],r["balance_paise"]/100) for r in rows) or "No wallets."
            await q.edit_message_text("👛 <b>Top Wallets</b>\n"+body,parse_mode=ParseMode.HTML,reply_markup=admin_markup())
        elif action=="edit_wallet":
            context.user_data["shop_admin_state"]="wallet_user"
            await q.edit_message_text("✏️ Wallet update ke liye Telegram User ID bhejein. Cancel: /cancel")
        elif action=="bulk":
            con.execute("BEGIN IMMEDIATE")
            ids=[r["user_id"] for r in con.execute("SELECT user_id FROM shop_users").fetchall()]
            con.execute("UPDATE user_wallets SET balance_paise=balance_paise+1000")
            con.executemany("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",[(uid,1000,"admin_bulk_reward") for uid in ids])
            con.commit()
            await q.edit_message_text("🎁 ₹10 reward credited to %s shop wallets."%len(ids),reply_markup=admin_markup())
        elif action=="orders":
            rows=con.execute("SELECT id,user_id,product_id,amount_paise,status,created_at FROM shop_orders ORDER BY id DESC LIMIT 30").fetchall()
            body="\n".join("#%s · U:%s · P:%s · ₹%.2f · %s"%(r["id"],r["user_id"],r["product_id"],r["amount_paise"]/100,r["status"]) for r in rows) or "No orders yet."
            await q.edit_message_text("📋 <b>Recent Orders</b>\n"+body,parse_mode=ParseMode.HTML,reply_markup=admin_markup())
        elif action=="csv":
            import csv, tempfile, os
            fd,path=tempfile.mkstemp(prefix="zoner-shop-orders-",suffix=".csv")
            os.close(fd)
            try:
                rows=con.execute("SELECT id,user_id,product_id,amount_paise,status,created_at FROM shop_orders ORDER BY id DESC").fetchall()
                with open(path,"w",newline="",encoding="utf-8") as out:
                    writer=csv.writer(out); writer.writerow(["order_id","user_id","product_id","amount_paise","status","created_at"])
                    writer.writerows([tuple(r) for r in rows])
                with open(path,"rb") as export_file:
                    await context.bot.send_document(chat_id=q.from_user.id,document=export_file,filename="zoner-shop-orders.csv",caption="📊 Shop orders export")
            finally:
                try: os.unlink(path)
                except OSError: pass
            await q.edit_message_text("📊 CSV export private chat mein bhej diya.",reply_markup=admin_markup())
        elif action=="broadcast":
            context.user_data["shop_admin_state"]="broadcast"
            await q.edit_message_text("📢 Ab broadcast ke liye message bhejein. Text, photo ya document supported hai. Cancel: /cancel")
        elif action=="tickets":
            rows=con.execute("SELECT id,user_id,query,created_at FROM support_tickets WHERE status='open' ORDER BY id DESC LIMIT 20").fetchall()
            buttons=[[InlineKeyboardButton("🎟 #%s · %s"%(r["id"],str(r["query"])[:32]),callback_data="shopadmin:reply:%s"%r["id"])] for r in rows]
            buttons.append([InlineKeyboardButton("⬅️ Admin Controls",callback_data="shopadmin:home")])
            await q.edit_message_text("🆘 <b>Open support tickets</b>\nSelect a ticket to reply." if rows else "No open tickets.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons))
        elif action.startswith("reply:"):
            tid=int(action.split(":")[1])
            exists=con.execute("SELECT 1 FROM support_tickets WHERE id=? AND status='open'",(tid,)).fetchone()
            if not exists:
                await q.edit_message_text("Ticket no longer open.",reply_markup=admin_markup())
            else:
                context.user_data["shop_admin_state"]="ticket_reply:%s"%tid
                await q.edit_message_text("💬 Ticket #%s ka private reply bhejein. Cancel: /cancel"%tid)
        elif action=="flags":
            rows=con.execute("SELECT feature_key,enabled FROM shop_feature_flags ORDER BY feature_key").fetchall()
            buttons=[[InlineKeyboardButton(("🟢 ON " if r["enabled"] else "🔴 OFF ")+r["feature_key"][:42],callback_data="shopadmin:flag:%s"%r["feature_key"])] for r in rows]
            buttons.append([InlineKeyboardButton("⬅️ Admin Controls",callback_data="shopadmin:home")])
            await q.edit_message_text("⚙️ <b>Feature flags</b>\nTap to toggle. Master emergency switch is included.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons))
        elif action.startswith("flag:"):
            key=action.split(":",1)[1]
            row=con.execute("SELECT enabled FROM shop_feature_flags WHERE feature_key=?",(key,)).fetchone()
            if row:
                con.execute("UPDATE shop_feature_flags SET enabled=? WHERE feature_key=?",(0 if row["enabled"] else 1,key)); con.commit()
            await q.edit_message_text("✅ Feature flag updated.",reply_markup=admin_markup())
        else:
            await q.edit_message_text("Unknown admin action.",reply_markup=admin_markup())
    finally:
        con.close()
    return True

async def referral_start_capture(update,context):
    if not update.effective_user:
        return
    ref=None
    if context.args and str(context.args[0]).isdigit():
        ref=int(context.args[0])
    register_user(update.effective_user,referrer_id=ref)

async def handle_callback(update,context):
    q=update.callback_query; data=q.data or ""
    if not data.startswith(("shop:","shopadmin:")): return False
    if data.startswith("shopadmin:"):
        return await admin_callback(update,context)
    if not await allowed(update,context): return True
    await q.answer(); uid=q.from_user.id; register_user(q.from_user)
    con=connect()
    try:
        if data=="shop:products":
            if not _shop_is_online(con):
                await q.edit_message_text("🔴 Zoner Shop is temporarily offline.",reply_markup=home_markup()); return True
            rows=con.execute("SELECT * FROM digital_products WHERE is_active=1 AND stock_count>0 ORDER BY id DESC LIMIT 20").fetchall()
            kb=[[InlineKeyboardButton("%s · ₹%.2f · stock %s"%(r["name"][:30],r["price_paise"]/100,r["stock_count"]),callback_data="shop:buy:%s"%r["id"])] for r in rows]
            kb.append([InlineKeyboardButton("⬅️ Home",callback_data="shop:home")])
            await q.edit_message_text("🛍️ <b>Products</b>\nSelect a product:" if rows else "No products available.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return True
        if data.startswith("shop:buy:"):
            if not _shop_is_online(con):
                await q.edit_message_text("🔴 Zoner Shop is temporarily offline.",reply_markup=home_markup()); return True
            pid=int(data.rsplit(":",1)[1]); con.execute("BEGIN IMMEDIATE")
            p=con.execute("SELECT * FROM digital_products WHERE id=? AND is_active=1",(pid,)).fetchone()
            w=con.execute("SELECT balance_paise FROM user_wallets WHERE user_id=?",(uid,)).fetchone()
            if not p or p["stock_count"]<1 or not p["file_id"]:
                con.rollback(); await q.edit_message_text("Product unavailable or delivery file missing.",reply_markup=home_markup()); return True
            if not w or w["balance_paise"]<p["price_paise"]:
                con.rollback(); await q.edit_message_text("👛 Insufficient wallet balance. Contact admin for a wallet top-up.",reply_markup=home_markup()); return True
            con.execute("UPDATE user_wallets SET balance_paise=balance_paise-? WHERE user_id=?",(p["price_paise"],uid))
            con.execute("UPDATE digital_products SET stock_count=stock_count-1 WHERE id=? AND stock_count>0",(pid,))
            cur=con.execute("INSERT INTO shop_orders(user_id,product_id,amount_paise,delivered_file_id,status) VALUES(?,?,?,?,?)",(uid,pid,p["price_paise"],p["file_id"],"delivery_pending"))
            con.execute("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",(uid,-p["price_paise"],"purchase:%s"%cur.lastrowid)); con.commit()
            try:
                await context.bot.send_document(uid,p["file_id"],caption="Purchase delivered · Order #%s"%cur.lastrowid)
                con.execute("UPDATE shop_orders SET status='paid' WHERE id=?",(cur.lastrowid,)); con.commit()
                delivery="File delivered successfully."
            except Exception:
                log.exception("Delivery failed for order %s",cur.lastrowid)
                delivery="Delivery is pending. Please contact support with this order ID."
            await q.edit_message_text("✅ Order #%s created. %s"%(cur.lastrowid,delivery),reply_markup=home_markup()); return True
        if data=="shop:wallet":
            w=con.execute("SELECT balance_paise,loyalty_points FROM user_wallets WHERE user_id=?",(uid,)).fetchone()
            await q.edit_message_text("👛 <b>Wallet</b>\nBalance: ₹%.2f\nPoints: %s\nTop-ups are admin-controlled for now."%((w["balance_paise"] if w else 0)/100,(w["loyalty_points"] if w else 0)),parse_mode=ParseMode.HTML,reply_markup=home_markup()); return True
        if data=="shop:wallet_history":
            rows=con.execute("SELECT amount_paise,reason FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
            body="\n".join("%+0.2f · %s"%(r["amount_paise"]/100,r["reason"]) for r in rows) or "No transactions."
            await q.edit_message_text("📜 Wallet history\n"+body,reply_markup=home_markup()); return True
        if data=="shop:spin":
            today=date.today().isoformat()
            prize=random.choice([0,500,1000,2000,5000])
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("INSERT INTO shop_spin_history(user_id,spin_date,prize_paise) VALUES(?,?,?)",(uid,today,prize))
                con.execute("INSERT OR IGNORE INTO user_wallets(user_id) VALUES(?)",(uid,))
                if prize:
                    con.execute("UPDATE user_wallets SET balance_paise=balance_paise+? WHERE user_id=?",(prize,uid))
                    con.execute("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",(uid,prize,"daily_spin"))
                con.commit()
                msg="🎉 You won ₹%.2f!"%(prize/100) if prize else "🙂 Better luck next time! Try again tomorrow."
            except sqlite3.IntegrityError:
                con.rollback()
                msg="⏳ Aaj ka spin already use ho chuka hai. Kal dobara try karein."
            await q.edit_message_text(msg,reply_markup=home_markup()); return True
        if data=="shop:reward":
            try:
                con.execute("INSERT INTO daily_rewards(user_id,reward_paise,claimed_on) VALUES(?,?,?)",(uid,100,date.today().isoformat()))
                con.execute("INSERT OR IGNORE INTO user_wallets(user_id) VALUES(?)",(uid,))
                con.execute("UPDATE user_wallets SET balance_paise=balance_paise+100,loyalty_points=loyalty_points+1 WHERE user_id=?",(uid,))
                con.execute("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",(uid,100,"daily_reward")); con.commit()
                msg="🎁 Daily reward claimed: ₹1.00"
            except sqlite3.IntegrityError: con.rollback(); msg="Already claimed today. Come back tomorrow."
            await q.edit_message_text(msg,reply_markup=home_markup()); return True
        if data=="shop:orders":
            rows=con.execute("SELECT o.id,p.name,o.amount_paise,o.status FROM shop_orders o JOIN digital_products p ON p.id=o.product_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10",(uid,)).fetchall()
            body="\n".join("#%s · %s · ₹%.2f · %s"%(r["id"],r["name"],r["amount_paise"]/100,r["status"]) for r in rows) or "No orders yet."
            await q.edit_message_text("📦 My Orders\n"+body,reply_markup=home_markup()); return True
        if data=="shop:referral":
            row=con.execute("SELECT referral_code FROM shop_users WHERE user_id=?",(uid,)).fetchone()
            count=con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(uid,)).fetchone()[0]
            me=await context.bot.get_me()
            await q.edit_message_text("🤝 <b>Refer & Earn</b>\nYour personal link: <code>https://t.me/%s?start=%s</code>\nSuccessful referrals: <b>%s</b>\nReward: ₹10 per new referred user."%(me.username,uid,count),parse_mode=ParseMode.HTML,reply_markup=home_markup()); return True
        if data=="shop:support":
            context.user_data["shop_support_wait"]=True
            await q.edit_message_text("🆘 Send your issue in one message. This creates a private support ticket, not a public live chat."); return True
        if data=="shop:home":
            await q.edit_message_text("🛍️ <b>Zoner Shop</b>\nChoose a section:",parse_mode=ParseMode.HTML,reply_markup=home_markup()); return True
        if data.startswith("shopadmin:"):
            if not is_admin(update): await q.answer("Admin only",show_alert=True); return True
            section=data.split(":",1)[1]
            if section=="help_product": msg="Use /shopproduct Name|Price|Stock|Description|TelegramFileID"
            elif section=="users": msg="Shop users: %s"%con.execute("SELECT COUNT(*) FROM shop_users").fetchone()[0]
            elif section=="products":
                rs=con.execute("SELECT id,name,stock_count,is_active FROM digital_products ORDER BY id DESC LIMIT 20").fetchall()
                msg="Products:\n"+"\n".join("#%s %s · stock %s · active %s"%(r["id"],r["name"],r["stock_count"],r["is_active"]) for r in rs)
            elif section=="orders":
                rs=con.execute("SELECT id,user_id,amount_paise,status FROM shop_orders ORDER BY id DESC LIMIT 20").fetchall()
                msg="Orders:\n"+"\n".join("#%s user %s · ₹%.2f · %s"%(r["id"],r["user_id"],r["amount_paise"]/100,r["status"]) for r in rs)
            elif section=="tickets":
                rs=con.execute("SELECT id,user_id,query FROM support_tickets WHERE status='open' ORDER BY id DESC LIMIT 20").fetchall()
                msg="Open tickets:\n"+"\n".join("#%s user %s · %s"%(r["id"],r["user_id"],r["query"][:100]) for r in rs)+"\nReply using /shopreply TICKET_ID."
            elif section=="flags":
                rs=con.execute("SELECT feature_key,enabled FROM shop_feature_flags ORDER BY feature_key").fetchall()
                msg="Feature flags:\n"+"\n".join("%s: %s"%(r["feature_key"],"ON" if r["enabled"] else "OFF") for r in rs) or "No feature flags initialized."
            else: msg="Section not implemented yet."
            await q.edit_message_text(msg); return True
    finally: con.close()
    return False


async def cancel_command(update,context):
    context.user_data.pop("shop_admin_state",None)
    context.user_data.pop("shop_product_draft",None)
    context.user_data.pop("shop_wallet_uid",None)
    context.user_data.pop("shop_support_wait",None)
    context.user_data.pop("shop_reply_ticket",None)
    await update.message.reply_text("❎ Current input cancelled.")

def install(app):
    app.add_handler(CommandHandler("start",referral_start_capture),group=-1)
    app.add_handler(CommandHandler("shop",shop_command))
    app.add_handler(CommandHandler("shopdeliver",deliver_order_command))
    app.add_handler(CommandHandler("shopadmin",admin_command))
    app.add_handler(CommandHandler("shopproduct",add_product_command))
    app.add_handler(CommandHandler("shopwallet",wallet_adjust_command))
    app.add_handler(CommandHandler("shopreply",reply_command))
    app.add_handler(CommandHandler("mytickets",mytickets_command))
    app.add_handler(CommandHandler("cancel",cancel_command))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND,admin_message_router),group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router),group=3)
