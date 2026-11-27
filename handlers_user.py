import logging
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import EVENT_NAME
from db import db
from menu_art import user_menu_photo
from theme_service import screen_photo
from utils import get_setting

log = logging.getLogger(__name__)
router = Router()

async def build_home_kb():
    kb = InlineKeyboardBuilder()
    for label, data in [
        ("🟢 🛍 Shop Products", "menu:shop"), ("🔵 👛 My Wallet", "menu:wallet"),
        ("🟡 🎁 Daily Rewards", "menu:rewards"), ("🔵 📦 My Orders", "menu:myorders"),
        ("🟢 🤝 Refer & Earn", "menu:referral"), ("🟡 🎟 My Tickets", "menu:myticket"),
        ("🔴 🆘 Help / Support", "menu:help")]:
        kb.button(text=label, callback_data=data)
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()

@router.message(CommandStart())
async def start(msg: Message):
    user = msg.from_user
    await db().execute(
        """INSERT INTO users(telegram_id, username, first_name, full_name)
           VALUES($1,$2,$3,$4)
           ON CONFLICT(telegram_id) DO UPDATE SET username=EXCLUDED.username,
           first_name=EXCLUDED.first_name, full_name=EXCLUDED.full_name""",
        user.id, user.username, user.first_name, user.full_name)
    welcome = await get_setting("welcome_msg", "Welcome to Zoner Shop! Explore premium digital products.")
    event = await get_setting("event_name", EVENT_NAME)
    await msg.answer("⌨️ Menu updated.", reply_markup=ReplyKeyboardRemove())
    await msg.answer_photo(photo=await screen_photo('user_menu_image', user_menu_photo()),
        caption=f"{welcome}\n\n🎭 <b>{event}</b>\nNeeche diye gaye buttons se shopping shuru karein.",
        reply_markup=await build_home_kb())

@router.callback_query(F.data.startswith("menu:"))
async def user_menu_navigation(cb: CallbackQuery):
    section = cb.data.split(":", 1)[1]
    kb = InlineKeyboardBuilder()
    if section == "shop":
        text = "🛍 <b>Shop Categories</b>\n\nSelect a category:"
        for label, data in [("🔵 Software & Tools","cat:software"),("🟢 E-Books","cat:ebooks"),("🟡 Gaming Keys","cat:gaming")]:
            kb.button(text=label, callback_data=data)
    elif section == "wallet":
        text = "👛 <b>My Wallet</b>\n\nWallet balance integration pending."
        kb.button(text="🟢 Add Funds", callback_data="wallet:add")
        kb.button(text="🔵 History", callback_data="wallet:history")
    elif section == "rewards":
        text = "🎁 <b>Daily Rewards</b>\n\nRewards integration pending."
        kb.button(text="🟡 Claim Daily Bonus", callback_data="reward:claim")
        kb.button(text="🟢 Spin the Wheel", callback_data="reward:spin")
    elif section == "help":
        text = "🆘 <b>Support Centre</b>\nDirect admin support integration pending."
        kb.button(text="🔴 Live Chat with Support", callback_data="support:new")
    else:
        await cb.answer("Feature coming soon!", show_alert=True)
        return
    kb.button(text="⬅️ Main Menu", callback_data="user:home")
    kb.adjust(2, 1)
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb.as_markup())

@router.callback_query(F.data == "user:home")
async def home(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("Main menu:", reply_markup=await build_home_kb())
