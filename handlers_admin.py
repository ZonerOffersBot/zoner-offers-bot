import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import EVENT_NAME
from menu_art import admin_menu_photo
from theme_service import screen_photo
from db import db
from utils import is_admin

log = logging.getLogger(__name__)
router = Router()

def admin_keyboard():
    kb = InlineKeyboardBuilder()
    buttons = [
        ("🟢 Status: Online", "admin:status_online"), ("🔴 Turn Bot OFF", "admin:status_off"),
        ("🔵 📊 Stats", "admin:stats"), ("🟡 📦 Stock Info", "admin:stock_info"),
        ("🔵 👥 All Users", "admin:users"), ("🟢 💰 All Wallets", "admin:wallet_home"),
        ("🔵 ✏️ Edit Wallet", "admin:edit_wallet"), ("🟢 🎁 Bulk Reward", "admin:bulk_reward"),
        ("🔵 ⏳ Active Rewards", "admin:active_rewards"), ("🟢 🔍 Search User", "admin:search_user"),
        ("🟢 📤 Upload Stock", "admin:wallet_inventory"), ("🟢 📢 Broadcast", "admin:broadcast"),
        ("🟢 🛍 Products", "admin:products"), ("🟢 🤝 Referrals", "admin:referrals"),
        ("🔵 👑 Admins", "admin:admins"), ("🔵 📸 Screenshots", "admin:screenshots"),
        ("🔵 📜 History", "admin:history"), ("🔵 📈 Revenue", "admin:revenue"),
        ("🔵 📋 All Orders", "admin:orders"), ("🔵 📊 CSV Export", "admin:csv_export"),
        ("🔵 🖼 Change QR", "admin:change_qr"), ("🔵 🆘 Support ID", "admin:support"),
        ("🔵 📢 Channels/Groups", "admin:force"), ("🟢 ✏️ Edit Texts", "admin:edit_texts"),
        ("🟢 🔓 Admin Withdraw (Free)", "admin:withdraw"), ("🔴 ❌ Close Panel", "admin:close_panel"),
    ]
    for label, data in buttons:
        kb.button(text=label, callback_data=data)
    kb.adjust(2)
    return kb.as_markup()

async def admin_home_text():
    users = await db().fetchval("SELECT COUNT(*) FROM users")
    orders = await db().fetchval("SELECT COUNT(*) FROM orders")
    paid = await db().fetchval("SELECT COUNT(*) FROM orders WHERE status='paid'")
    tickets = await db().fetchval("SELECT COUNT(*) FROM tickets")
    return (f"🛠️ <b>{EVENT_NAME} Admin Panel</b>\n\n👥 Users: {users}\n🧾 Orders: {orders}\n"
            f"✅ Paid orders: {paid}\n🎟 Tickets issued: {tickets}\n\nNeeche kisi section ko select karein.")

@router.message(Command("admin"))
async def admin_panel(msg: Message):
    if not await is_admin(msg.from_user.id):
        await msg.answer("⛔ Admin access nahi hai.")
        return
    try:
        await msg.answer_photo(photo=await screen_photo('admin_menu_image', admin_menu_photo()),
                               caption=await admin_home_text(), reply_markup=admin_keyboard())
    except Exception:
        log.exception("Admin panel failed")
        await msg.answer("⚠️ Admin panel database error.")

@router.callback_query(F.data.startswith("admin:"))
async def admin_navigation(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        await cb.answer("Admin only.", show_alert=True)
        return
    section = cb.data.split(":", 1)[1]
    if section == "status_online":
        await cb.answer("Bot is already online!", show_alert=True)
        return
    if section == "status_off":
        await cb.answer("Maintenance mode is not connected yet.", show_alert=True)
        return
    if section == "close_panel":
        await cb.message.delete()
        await cb.answer()
        return
    text = await admin_home_text() if section in ("stats", "home") else (
        f"⚙️ <b>{section.replace('_', ' ').title()} Section</b>\n\nBackend integration pending.")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Back to Admin Panel", callback_data="admin:home")
    await cb.answer()
    if cb.message:
        await cb.message.answer(text, reply_markup=kb.as_markup())
