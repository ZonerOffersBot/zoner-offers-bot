import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def main_menu():
    keyboard = [
        [
            InlineKeyboardButton("🛍️ Latest Offers", callback_data="offers"),
            InlineKeyboardButton("🏷️ Categories", callback_data="categories"),
        ],
        [
            InlineKeyboardButton("🔔 Notifications", callback_data="subscribe"),
            InlineKeyboardButton("📢 Join Channel", url="https://t.me/ZonerOffers"),
        ],
        [
            InlineKeyboardButton("🆘 Help", callback_data="help"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔥 *Welcome to Zoner Offers!*\n\n"
        "Discover the latest deals, discounts and price drops.\n\n"
        "👇 Choose an option:"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "offers":
        await query.edit_message_text(
            "🛍️ *Latest Offers*\n\n"
            "No offers have been added yet.\n\n"
            "🔥 New deals will appear here soon!",
            parse_mode="Markdown",
        )

    elif query.data == "categories":
        keyboard = [
            [InlineKeyboardButton("📱 Electronics", callback_data="cat_electronics")],
            [InlineKeyboardButton("🎮 Gaming", callback_data="cat_gaming")],
            [InlineKeyboardButton("👕 Fashion", callback_data="cat_fashion")],
            [InlineKeyboardButton("🏠 Home & Kitchen", callback_data="cat_home")],
            [InlineKeyboardButton("📚 Books", callback_data="cat_books")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")],
        ]

        await query.edit_message_text(
            "🏷️ *Offer Categories*\n\nChoose a category:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif query.data == "subscribe":
        await query.edit_message_text(
            "🔔 *Notifications*\n\n"
            "Notification system will be available soon.",
            parse_mode="Markdown",
        )

    elif query.data == "help":
        await query.edit_message_text(
            "🆘 *Zoner Offers Help*\n\n"
            "Use the buttons to browse offers and categories.\n\n"
            "More features are coming soon!",
            parse_mode="Markdown",
        )

    elif query.data == "back":
        await query.edit_message_text(
            "🔥 *Zoner Offers*\n\nChoose an option:",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )

    else:
        await query.edit_message_text(
            "📌 This category is currently empty.",
            parse_mode="Markdown",
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 *Zoner Offers Help*\n\n"
        "Use /start to open the main menu.",
        parse_mode="Markdown",
    )


def run_bot():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("🔥 Zoner Offers Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    run_bot()
