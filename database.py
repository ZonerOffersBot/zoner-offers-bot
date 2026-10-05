"""Database compatibility layer for the modular Zoner Offers Bot.
The production schema remains owned by bot.py so the live database is not reset.
"""
import bot

init_db = bot.init_db
get_conn = bot.get_conn
fingerprint = bot.fingerprint
insert_offer = bot.insert_offer
get_offers = getattr(bot, "get_offers", None)
