"""Menu facade for the existing stable Telegram UI."""
import bot

main_menu = getattr(bot, "main_menu", None)
