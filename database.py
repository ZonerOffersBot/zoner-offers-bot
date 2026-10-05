import aiosqlite
from config import DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS force_channels (
                channel_id TEXT PRIMARY KEY, channel_name TEXT, invite_link TEXT,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, callback_data TEXT,
                row_position INTEGER, col_position INTEGER, is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS connected_channels (
                channel_id TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, price TEXT, mrp TEXT,
                image TEXT, source TEXT, url TEXT, brand TEXT, deal_score INTEGER,
                fingerprint TEXT UNIQUE, is_posted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('post_interval','120')")
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('buttons_per_row','2')")
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('menu_per_row','2')")
        await db.commit()

async def get_setting(key, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await db.commit()
