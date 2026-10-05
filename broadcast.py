"""Publishing facade. Uses the existing live publisher to avoid disrupting channels."""
import bot

publish_offer = bot.publish_offer
POST_CHANNELS = getattr(bot, "POST_CHANNELS", [])
