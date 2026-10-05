"""Central configuration for Zoner Offers Bot."""
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
GROUP_ID = os.getenv("GROUP_ID", "")
GROUP_URL = os.getenv("GROUP_URL", "")
SECOND_CHANNEL_URL = os.getenv("SECOND_CHANNEL_URL", "https://t.me/offerleloturant")
POST_CHANNELS = [x.strip() for x in os.getenv("POST_CHANNELS", "@zoneroffers,@offerleloturant").split(",") if x.strip()]
AUTO_POST = os.getenv("AUTO_POST", "1").lower() not in ("0", "false", "no", "off")
SCAN_SECONDS = max(30, int(os.getenv("SCAN_SECONDS", "90")))
MIN_DEAL_SCORE = int(os.getenv("MIN_DEAL_SCORE", "45"))
DB_FILE = os.getenv("DB_FILE", "offers.db")
