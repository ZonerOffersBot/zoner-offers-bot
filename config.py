import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

ADMIN_IDS = []
for value in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).split(","):
    value = value.strip()
    if value:
        try:
            ADMIN_IDS.append(int(value))
        except ValueError:
            pass

DB_PATH = os.getenv("DB_PATH", os.getenv("DB_FILE", "zoner_offers.db"))

DEFAULT_POST_INTERVAL = 120
MAX_SEND_RETRIES = 3
RETRY_DELAY = 2
