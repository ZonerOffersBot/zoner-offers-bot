"""Stable production entry point for Zoner Offers Bot."""
# ZONER_BROADCAST_FIX_2026_10_06
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
import bot
import runner
import shop_features
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import CommandHandler, MessageHandler, filters


_original_init_db = bot.init_db

def _zoner_init_db_with_shop():
    _original_init_db()
    shop_features.init_db(bot.DB_FILE)

bot.init_db = _zoner_init_db_with_shop
_zoner_admin_menu = bot.admin_menu

def _patched_admin_menu():
    markup = _zoner_admin_menu()
    rows = list(markup.inline_keyboard)
    rows.append([InlineKeyboardButton("🛍️ Shop Management", callback_data="shopadmin:home")])
    return InlineKeyboardMarkup(rows)

bot.admin_menu = _patched_admin_menu


_zoner_main_menu = bot.main_menu


def _patched_main_menu(user_id=None):
    # Preserve every existing deal-discovery button and append the SQLite shop entry.
    markup = _zoner_main_menu(user_id)
    rows = list(markup.inline_keyboard)
    rows.append([InlineKeyboardButton("🛍️ Zoner Shop", callback_data="shop:home")])
    return InlineKeyboardMarkup(rows)

bot.main_menu = _patched_main_menu


async def _published_links(update, context):
    query = update.callback_query
    rows = bot.get_offers(limit=100)
    if not rows:
        await query.edit_message_text(
            "📚 <b>Published Links</b>\n\nNo successfully published links are stored yet.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data="back")]]),
        )
        return
    buttons = []
    for row in rows:
        title = row["title"][:48] + ("…" if len(row["title"]) > 48 else "")
        buttons.append([InlineKeyboardButton(f"🔗 {title}", callback_data=f"offer_{row['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Main Menu", callback_data="back")])
    await query.edit_message_text(
        "📚 <b>Published Links</b>\n\nEvery successfully published link is stored in the publication ledger and remains available here and under its saved category.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


_original_button_impl = bot._button_handler_impl


async def _patched_button_impl(update, context):
    query = update.callback_query
    data = query.data
    if data.startswith(("shop:", "shopadmin:")):
        if await shop_features.handle_callback(update, context):
            return
    if data == "instamart_order":
        user_id = query.from_user.id
        if not bot.is_user_verified(user_id):
            verified = await bot.membership_status(context.bot, user_id)
            if not verified:
                await query.edit_message_text(bot.join_gate_text(), parse_mode=ParseMode.HTML, reply_markup=bot.join_gate_markup())
                return
            bot.mark_user_verified(user_id)
        context.user_data.setdefault("instamart_order", {"items": []})
        await query.edit_message_text(
            "🛍️ <b>SWIGGY INSTAMART</b>\\n"
            "🔐 Login: Official Swiggy\\n"
            "🛒 Cart: Local only\\n"
            "📦 Live stock: Not connected\\n"
            "💳 Order / Payment: Not connected\\n"
            "⚠️ Real shopping: Use official Swiggy.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍️ Products", callback_data="instamart_products")],
                [InlineKeyboardButton("🧮 Savings Calculator", callback_data="instamart_calc")],
                [InlineKeyboardButton("🛒 My Cart", callback_data="instamart_view")],
                [InlineKeyboardButton("🧹 Clear Cart", callback_data="instamart_clear")],
                [InlineKeyboardButton("🔐 Login / Shop Swiggy", url="https://www.swiggy.com/instamart")],
                [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
            ]),
        )
        return
    if data == "instamart_products":
        context.user_data.setdefault("instamart_order", {"items": [], "cart": {}})
        products = [("milk", "🥛 Milk 1L — ₹68"), ("bread", "🍞 Bread — ₹40"), ("eggs", "🥚 Eggs (6) — ₹55"), ("bananas", "🍌 Bananas — ₹45"), ("chips", "🍟 Chips — ₹30"), ("juice", "🧃 Juice — ₹90")]
        rows = [[InlineKeyboardButton(label, callback_data=f"demo_add_{key}")] for key, label in products]
        rows += [[InlineKeyboardButton("🛒 My Cart", callback_data="instamart_view")],
                 [InlineKeyboardButton("🧮 Discount Calculator", callback_data="instamart_calc")],
                 [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")]]
        await query.edit_message_text("🛍️ <b>PRODUCT LIST</b>\\n<i>Sample products/prices only — not live Swiggy inventory.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith("demo_add_"):
        catalog = {"milk": ("Milk 1L", 68), "bread": ("Bread", 40), "eggs": ("Eggs (6)", 55), "bananas": ("Bananas", 45), "chips": ("Chips", 30), "juice": ("Juice", 90)}
        key = data[len("demo_add_"):]
        if key in catalog:
            state = context.user_data.setdefault("instamart_order", {"items": [], "cart": {}})
            state.setdefault("cart", {})
            name, price = catalog[key]
            item = state["cart"].setdefault(key, {"name": name, "price": price, "qty": 0})
            item["qty"] += 1
            await query.answer(f"Cart: {name} × {item['qty']}")
            await query.edit_message_text(f"✅ Added to <b>CART</b>: {name} × {item['qty']}\\nPrice: ₹{price} each\\n\\n<i>Simulation only; nothing added to Swiggy.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add more products", callback_data="instamart_products")],
                [InlineKeyboardButton("🛒 My Cart", callback_data="instamart_view")],
                [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")],
            ]))
        return
    if data == "instamart_calc":
        context.user_data.setdefault("instamart_order", {"items": [], "cart": {}})["step"] = "calc_total"
        await query.edit_message_text("🧮 <b>SAVINGS CALCULATOR</b>\\n\\nSend your cart total in rupees (example: <code>649</code>).\\nExample estimate: 10% off up to ₹100 on ₹499+; extra ₹50 off on ₹999+.\\n<i>Illustrative rules only, not verified or redeemable Swiggy offers.</i>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")]]))
        return
    if data == "instamart_checkout":
        cart = context.user_data.get("instamart_order", {}).get("cart", {})
        subtotal = sum(x["price"] * x["qty"] for x in cart.values())
        if not cart:
            await query.answer("Your sample cart is empty.", show_alert=True)
            return
        discount = min(100, int(subtotal * 0.10)) if subtotal >= 499 else 0
        if subtotal >= 999:
            discount += 50
        total = max(0, subtotal - discount)
        await query.edit_message_text("🎉 <b>CART SUMMARY</b>\\n━━━━━━━━━━━━━━━━━━\\n🧾 Subtotal: ₹" + str(subtotal) + "\\n🎟️ Sample discount: −₹" + str(discount) + "\\n<b>Sample total: ₹" + str(total) + "</b>\\n\\n✅ Estimate only. <b>No real order was placed and no payment was taken.</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ Continue", callback_data="instamart_products")],
            [InlineKeyboardButton("🔐 Login / Shop Swiggy", url="https://www.swiggy.com/instamart")],
            [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")],
        ]))
        return
    if data == "instamart_add":
        context.user_data.setdefault("instamart_order", {"items": []})
        context.user_data["instamart_order"]["step"] = "add_items"
        await query.edit_message_text(
            "➕ <b>Add products</b>\\n\\nSend product names and quantities, one per line. Example:\\n<code>Milk 1 litre x 2\\nBread x 1</code>\\n\\nThis is a checklist, not a live Swiggy cart.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Order menu", callback_data="instamart_order")]]),
        )
        return
    if data == "instamart_view":
        state = context.user_data.get("instamart_order", {})
        cart = state.get("cart", {})
        listing = "\\n".join(f"• {item['name']} × {item['qty']} — ₹{item['price'] * item['qty']}" for item in cart.values())
        subtotal = sum(item["price"] * item["qty"] for item in cart.values())
        if not listing:
            listing = "Your sample cart is empty."
        await query.edit_message_text("🛒 <b>MY CART</b>\\n\\n" + listing + "\\n\\n<b>Subtotal: ₹" + str(subtotal) + "</b>\\n⚠️ Sample prices · Not synced to Swiggy", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ Browse products", callback_data="instamart_products")],
            [InlineKeyboardButton("🧮 Discount Calculator", callback_data="instamart_calc")],
            [InlineKeyboardButton("🧾 Cart Summary", callback_data="instamart_checkout")],
            [InlineKeyboardButton("🧹 Clear Cart", callback_data="instamart_clear")],
            [InlineKeyboardButton("⬅️ Order menu", callback_data="instamart_order")],
        ]))
        return
    if data == "instamart_clear":
        context.user_data["instamart_order"] = {"items": [], "cart": {}}
        await query.edit_message_text("🧹 Sample cart cleared.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Order menu", callback_data="instamart_order")]]))
        return
    if data == "published_links":
        user_id = query.from_user.id
        if not bot.is_user_verified(user_id):
            verified = await bot.membership_status(context.bot, user_id)
            if not verified:
                await query.edit_message_text(
                    bot.join_gate_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=bot.join_gate_markup(),
                )
                return
            bot.mark_user_verified(user_id)
        await _published_links(update, context)
        return
    if data == "admin_broadcast":
        if not bot.is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return
        con = bot.db()
        try:
            user_count = con.execute("SELECT COUNT(*) AS n FROM subscribers WHERE enabled=1").fetchone()["n"]
        finally:
            con.close()
        await query.edit_message_text(
            "📝 <b>BROADCAST CENTER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Active recipients: <b>{user_count}</b>\n\n"
            "📢 <b>Send:</b> <code>/broadcast Your message</code>\n\n"
            "Example:\n<code>/broadcast 🔥 New deals are live!</code>\n\n"
            "Only users with Notifications ON receive it.\n"
            "One failed user will NOT stop the remaining broadcast.",
            parse_mode=ParseMode.HTML,
            reply_markup=bot.admin_menu(),
        )
        return
    if data.startswith("admin_") and data in {
        "admin_tools", "admin_health", "admin_analytics", "admin_backup", "admin_audience"
    }:
        if not bot.is_admin(update):
            await query.answer("Not authorized.", show_alert=True)
            return

        if data == "admin_tools":
            await query.edit_message_text(
                "🧰 <b>EXTRA ADMIN TOOLS</b>\\n━━━━━━━━━━━━━━━━━━━━\\n"
                "Choose a control:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🩺 System Diagnostics", callback_data="admin_health")],
                    [InlineKeyboardButton("📈 Publishing Analytics", callback_data="admin_analytics")],
                    [InlineKeyboardButton("💾 Download DB Backup", callback_data="admin_backup")],
                    [InlineKeyboardButton("👥 Audience Overview", callback_data="admin_audience")],
                    [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")],
                ]),
            )
            return

        if data == "admin_health":
            checks = []
            try:
                con = bot.db()
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                table_count = con.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
                offer_count = con.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
                subscriber_count = con.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
                con.close()
                checks.append(f"🗄️ Database: <b>{'OK' if integrity == 'ok' else 'CHECK'}</b>")
                checks.append(f"📚 Tables: <b>{table_count}</b>")
                checks.append(f"🛍️ Offers: <b>{offer_count}</b>")
                checks.append(f"👥 Subscribers: <b>{subscriber_count}</b>")
                checks.append(f"📁 DB path configured: <code>{'YES' if os.path.exists(bot.DB_FILE) else 'NO'}</code>")
            except Exception as exc:
                checks.append(f"❌ Database: <code>{type(exc).__name__}: {str(exc)[:180]}</code>")
            checks.append(f"⏱️ Publish interval: <b>{bot.SCAN_SECONDS}s (5 min target)</b>")
            for target in bot.POST_CHANNELS:
                try:
                    chat = await context.bot.get_chat(target)
                    member = await context.bot.get_chat_member(chat.id, context.bot.id)
                    rights = getattr(member, "can_post_messages", None)
                    status = getattr(member, "status", "unknown")
                    checks.append(f"📣 <code>{target}</code>: <b>{status}</b> · can post <b>{rights}</b>")
                except Exception as exc:
                    checks.append(f"⚠️ <code>{target}</code>: <code>{type(exc).__name__}</code>")
            await query.edit_message_text(
                "🩺 <b>SYSTEM DIAGNOSTICS</b>\\n━━━━━━━━━━━━━━━━━━━━\\n" + "\\n".join(checks),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Run Again", callback_data="admin_health")],
                    [InlineKeyboardButton("⬅️ Extra Tools", callback_data="admin_tools")],
                ]),
            )
            return

        if data == "admin_analytics":
            con = bot.db()
            try:
                today = con.execute(
                    "SELECT COUNT(*) FROM published_links WHERE date(published_at)=date('now')"
                ).fetchone()[0]
                last7 = con.execute(
                    "SELECT COUNT(*) FROM published_links WHERE datetime(published_at)>=datetime('now','-7 days')"
                ).fetchone()[0]
                total = con.execute("SELECT COUNT(*) FROM published_links").fetchone()[0]
                by_channel = con.execute(
                    "SELECT chat_id, COUNT(*) AS n FROM published_links GROUP BY chat_id ORDER BY n DESC LIMIT 5"
                ).fetchall()
                by_source = con.execute(
                    "SELECT COALESCE(source,'Unknown') AS source, COUNT(*) AS n FROM offers GROUP BY source ORDER BY n DESC LIMIT 5"
                ).fetchall()
            finally:
                con.close()
            channel_lines = [f"• <code>{str(r['chat_id'])}</code>: {r['n']}" for r in by_channel] or ["• No channel records yet"]
            source_lines = [f"• {str(r['source'])[:35]}: {r['n']}" for r in by_source] or ["• No source records yet"]
            await query.edit_message_text(
                "📈 <b>PUBLISHING ANALYTICS</b>\\n━━━━━━━━━━━━━━━━━━━━\\n"
                f"📅 Today (UTC): <b>{today}</b>\\n"
                f"🗓️ Last 7 days: <b>{last7}</b>\\n"
                f"📢 All-time link records: <b>{total}</b>\\n\\n"
                "<b>Top destination records</b>\\n" + "\\n".join(channel_lines) +
                "\\n\\n<b>Top saved offer sources</b>\\n" + "\\n".join(source_lines),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="admin_analytics")],
                    [InlineKeyboardButton("⬅️ Extra Tools", callback_data="admin_tools")],
                ]),
            )
            return

        if data == "admin_backup":
            source = sqlite3.connect(bot.DB_FILE)
            temp = tempfile.NamedTemporaryFile(prefix="zoner-backup-", suffix=".sqlite3", delete=False)
            temp_path = temp.name
            temp.close()
            try:
                destination = sqlite3.connect(temp_path)
                source.backup(destination)
                destination.close()
            finally:
                source.close()
            try:
                with open(temp_path, "rb") as backup_file:
                    await context.bot.send_document(
                        chat_id=query.from_user.id,
                        document=backup_file,
                        filename=f"zoner-offers-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.sqlite3",
                        caption="💾 Zoner Offers database backup. Keep this file private.",
                    )
                await query.answer("Backup sent to your private chat.")
            except Exception as exc:
                await query.answer(f"Backup failed: {type(exc).__name__}", show_alert=True)
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return

        if data == "admin_audience":
            con = bot.db()
            try:
                total = con.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
                on = con.execute("SELECT COUNT(*) FROM subscribers WHERE enabled=1").fetchone()[0]
                off = total - on
                verified = con.execute("SELECT COUNT(*) FROM verified_users").fetchone()[0]
                groups = con.execute(
                    "SELECT COUNT(*) FROM auto_publish_chats WHERE enabled=1"
                ).fetchone()[0]
            finally:
                con.close()
            await query.edit_message_text(
                "👥 <b>AUDIENCE OVERVIEW</b>\\n━━━━━━━━━━━━━━━━━━━━\\n"
                f"👤 Registered users: <b>{total}</b>\\n"
                f"🔔 Notifications ON: <b>{on}</b>\\n"
                f"🔕 Notifications OFF: <b>{off}</b>\\n"
                f"✅ Verified users: <b>{verified}</b>\\n"
                f"👥 Active registered groups: <b>{groups}</b>\\n\\n"
                "Only aggregate counts are shown here; no user IDs are exposed.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="admin_audience")],
                    [InlineKeyboardButton("⬅️ Extra Tools", callback_data="admin_tools")],
                ]),
            )
            return

    return await _original_button_impl(update, context)


bot._button_handler_impl = _patched_button_impl


async def instamart_order_input(update, context):
    """Collect checklist text only while the user is in the add-items flow."""
    state = context.user_data.get("instamart_order")
    if not state or state.get("step") not in ("add_items", "calc_total") or not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text.startswith("/"):
        return
    if state.get("step") == "calc_total":
        try:
            subtotal = float(text.replace("₹", "").replace(",", "").strip())
            if subtotal < 0 or subtotal > 1000000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a valid cart total, e.g. 649.")
            return
        discount = min(100, int(subtotal * 0.10)) if subtotal >= 499 else 0
        if subtotal >= 999:
            discount += 50
        state["step"] = ""
        await update.message.reply_text("🧮 INSTAMART CALCULATOR — SAMPLE RESULT\\n━━━━━━━━━━━━━━━━━━\\n" + f"Cart total: ₹{subtotal:.0f}\\nSample discount: ₹{discount}\\nEstimated sample total: ₹{max(0, subtotal-discount):.0f}\\n\\n⚠️ Illustrative rules only; not a live or redeemable Swiggy offer.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ Products", callback_data="instamart_products")],
            [InlineKeyboardButton("🧮 Calculate Again", callback_data="instamart_calc")],
            [InlineKeyboardButton("⬅️ Order Menu", callback_data="instamart_order")],
        ]))
        return
    lines = [line.strip(" •-\\t") for line in text.splitlines() if line.strip(" •-\\t")]
    if not lines:
        await update.message.reply_text("Send product names and quantities, one per line.")
        return
    state.setdefault("items", []).extend(lines[:30])
    state["step"] = ""
    await update.message.reply_text(
        "✅ Added to your checklist.\\n\\n" + "\\n".join("• " + line for line in lines[:30]) +
        "\\n\\nThis is not synced to Swiggy. Open the official Instamart page to place the real order.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 View checklist", callback_data="instamart_view")],
            [InlineKeyboardButton("🚀 Open Instamart", url="https://www.swiggy.com/instamart")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="back")],
        ]),
    )


_original_build_app = runner.build_app


def build_app():
    app = _original_build_app()
    shop_features.install(app)
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, instamart_order_input), group=0)
    return app


async def broadcast_command(update, context):
    if not bot.is_admin(update):
        return

    args = getattr(context, "args", []) or []
    message = " ".join(args).strip()
    if not message:
        await update.message.reply_text(
            "📝 <b>Broadcast Center</b>\n\n"
            "Use:\n<code>/broadcast Your message here</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=bot.admin_menu(),
        )
        return

    con = bot.db()
    try:
        users = con.execute("SELECT user_id FROM subscribers WHERE enabled=1").fetchall()
    finally:
        con.close()

    sent = 0
    failed = 0
    retry_count = 0

    await update.message.reply_text(
        f"📢 <b>Broadcast started</b>\n\n👥 Recipients: <b>{len(users)}</b>\n⏳ Sending safely...",
        parse_mode=ParseMode.HTML,
    )

    for user in users:
        chat_id = user["user_id"]
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            sent += 1
        except RetryAfter as exc:
            retry_count += 1
            wait_for = max(1.0, float(getattr(exc, "retry_after", 1)))
            await asyncio.sleep(wait_for)
            try:
                await context.bot.send_message(chat_id=chat_id, text=message)
                sent += 1
            except Exception as retry_exc:
                failed += 1
                bot.log.warning("Broadcast retry failed for %s: %s", chat_id, retry_exc)
        except Exception as exc:
            failed += 1
            bot.log.warning("Broadcast failed for %s: %s", chat_id, exc)

        await asyncio.sleep(0.05)

    await update.message.reply_text(
        "✅ <b>BROADCAST COMPLETE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📨 Sent: <b>{sent}</b>\n"
        f"❌ Failed: <b>{failed}</b>\n"
        f"🔁 Rate-limit retries: <b>{retry_count}</b>\n"
        f"👥 Total targeted: <b>{len(users)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=bot.admin_menu(),
    )


runner.build_app = build_app


def run():
    return runner.run()


init_db = bot.init_db


if __name__ == "__main__":
    run()
