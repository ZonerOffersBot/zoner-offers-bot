import html
import logging
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
        CREATE TABLE IF NOT EXISTS shop_users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, referral_code TEXT UNIQUE, referred_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS user_wallets(user_id INTEGER PRIMARY KEY, balance_paise INTEGER NOT NULL DEFAULT 0, loyalty_points INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS wallet_transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount_paise INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS digital_products(id INTEGER PRIMARY KEY AUTOINCREMENT, category_id TEXT DEFAULT 'general', name TEXT NOT NULL, price_paise INTEGER NOT NULL DEFAULT 0, stock_count INTEGER NOT NULL DEFAULT 0, file_id TEXT, description TEXT, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS shop_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'paid', delivered_file_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS referrals(id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL, referred_id INTEGER NOT NULL UNIQUE, commission_paise INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS daily_rewards(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, reward_paise INTEGER NOT NULL DEFAULT 0, claimed_on TEXT NOT NULL, UNIQUE(user_id, claimed_on));
        CREATE TABLE IF NOT EXISTS support_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, full_name TEXT, query TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', admin_reply TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS shop_feature_flags(feature_key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
        """)
        con.executemany("INSERT OR IGNORE INTO shop_feature_flags(feature_key,enabled) VALUES(?,1)",[(item["key"],) for item in FEATURES])
        con.commit()
    finally:
        con.close()

def register_user(user):
    con=connect()
    try:
        con.execute("INSERT INTO shop_users(user_id,username,first_name,referral_code) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name",
                    (user.id,user.username,user.first_name,"Z"+format(user.id,"X")))
        con.execute("INSERT OR IGNORE INTO user_wallets(user_id) VALUES(?)",(user.id,))
        con.commit()
    finally: con.close()

def is_admin(update):
    import bot
    return bot.is_admin(update)

def home_markup():
    return InlineKeyboardMarkup([
      [InlineKeyboardButton("🛍️ Shop Products",callback_data="shop:products"),InlineKeyboardButton("👛 My Wallet",callback_data="shop:wallet")],
      [InlineKeyboardButton("🎁 Daily Reward",callback_data="shop:reward"),InlineKeyboardButton("📦 My Orders",callback_data="shop:orders")],
      [InlineKeyboardButton("🤝 Refer & Earn",callback_data="shop:referral"),InlineKeyboardButton("🆘 Support Ticket",callback_data="shop:support")]])

async def allowed(update,context):
    import bot
    uid=update.effective_user.id
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
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Product",callback_data="shopadmin:help_product"),InlineKeyboardButton("👥 Users",callback_data="shopadmin:users")],[InlineKeyboardButton("📦 Products",callback_data="shopadmin:products"),InlineKeyboardButton("🧾 Orders",callback_data="shopadmin:orders")],[InlineKeyboardButton("🎟 Support",callback_data="shopadmin:tickets"),InlineKeyboardButton("⚙️ Feature Flags",callback_data="shopadmin:flags")]])
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

async def handle_callback(update,context):
    q=update.callback_query; data=q.data or ""
    if not data.startswith(("shop:","shopadmin:")): return False
    if not await allowed(update,context): return True
    await q.answer(); uid=q.from_user.id; register_user(q.from_user)
    con=connect()
    try:
        if data=="shop:products":
            rows=con.execute("SELECT * FROM digital_products WHERE is_active=1 ORDER BY id DESC LIMIT 20").fetchall()
            kb=[[InlineKeyboardButton("%s · ₹%.2f · stock %s"%(r["name"][:30],r["price_paise"]/100,r["stock_count"]),callback_data="shop:buy:%s"%r["id"])] for r in rows]
            kb.append([InlineKeyboardButton("⬅️ Home",callback_data="shop:home")])
            await q.edit_message_text("🛍️ <b>Products</b>\nSelect a product:" if rows else "No products available.",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return True
        if data.startswith("shop:buy:"):
            pid=int(data.rsplit(":",1)[1]); con.execute("BEGIN IMMEDIATE")
            p=con.execute("SELECT * FROM digital_products WHERE id=? AND is_active=1",(pid,)).fetchone()
            w=con.execute("SELECT balance_paise FROM user_wallets WHERE user_id=?",(uid,)).fetchone()
            if not p or p["stock_count"]<1 or not p["file_id"]:
                con.rollback(); await q.edit_message_text("Product unavailable or delivery file missing.",reply_markup=home_markup()); return True
            if not w or w["balance_paise"]<p["price_paise"]:
                con.rollback(); await q.edit_message_text("👛 Insufficient wallet balance. Contact admin for a wallet top-up.",reply_markup=home_markup()); return True
            con.execute("UPDATE user_wallets SET balance_paise=balance_paise-? WHERE user_id=?",(p["price_paise"],uid))
            con.execute("UPDATE digital_products SET stock_count=stock_count-1 WHERE id=? AND stock_count>0",(pid,))
            cur=con.execute("INSERT INTO shop_orders(user_id,product_id,amount_paise,delivered_file_id) VALUES(?,?,?,?)",(uid,pid,p["price_paise"],p["file_id"]))
            con.execute("INSERT INTO wallet_transactions(user_id,amount_paise,reason) VALUES(?,?,?)",(uid,-p["price_paise"],"purchase:%s"%cur.lastrowid)); con.commit()
            try: await context.bot.send_document(uid,p["file_id"],caption="Purchase delivered · Order #%s"%cur.lastrowid)
            except Exception: log.exception("Delivery failed for order %s",cur.lastrowid)
            await q.edit_message_text("✅ Order #%s created. Delivery was attempted in your private chat."%cur.lastrowid,reply_markup=home_markup()); return True
        if data=="shop:wallet":
            w=con.execute("SELECT balance_paise,loyalty_points FROM user_wallets WHERE user_id=?",(uid,)).fetchone()
            await q.edit_message_text("👛 <b>Wallet</b>\nBalance: ₹%.2f\nPoints: %s\nTop-ups are admin-controlled for now."%((w["balance_paise"] if w else 0)/100,(w["loyalty_points"] if w else 0)),parse_mode=ParseMode.HTML,reply_markup=home_markup()); return True
        if data=="shop:wallet_history":
            rows=con.execute("SELECT amount_paise,reason FROM wallet_transactions WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
            body="\n".join("%+0.2f · %s"%(r["amount_paise"]/100,r["reason"]) for r in rows) or "No transactions."
            await q.edit_message_text("📜 Wallet history\n"+body,reply_markup=home_markup()); return True
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
            await q.edit_message_text("🤝 <b>Refer & Earn</b>\nCode: <code>%s</code>\nShare: <code>/start %s</code>\nReferral reward crediting is not enabled yet."%(row["referral_code"],row["referral_code"]),parse_mode=ParseMode.HTML,reply_markup=home_markup()); return True
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

def install(app):
    app.add_handler(CommandHandler("shop",shop_command))
    app.add_handler(CommandHandler("shopadmin",admin_command))
    app.add_handler(CommandHandler("shopproduct",add_product_command))
    app.add_handler(CommandHandler("shopwallet",wallet_adjust_command))
    app.add_handler(CommandHandler("shopreply",reply_command))
    app.add_handler(CommandHandler("mytickets",mytickets_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router),group=2)
