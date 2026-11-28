import os
import sqlite3
import tempfile
import unittest

# The production entry point reads DB_FILE at import time.
DB_PATH = os.path.join(tempfile.gettempdir(), "zoner_shop_ci_test.sqlite3")
os.environ["DB_FILE"] = DB_PATH
try:
    os.remove(DB_PATH)
except FileNotFoundError:
    pass

import main
import bot
import shop_features
import advanced_shop_features


class ZonerDatabaseSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bot.DB_FILE = DB_PATH
        main.init_db()

    def test_core_and_shop_tables_exist(self):
        con = sqlite3.connect(DB_PATH)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        finally:
            con.close()
        expected = {
            "offers", "subscribers", "publish_history", "published_links",
            "verified_users", "shop_users", "user_wallets", "digital_products",
            "shop_orders", "shop_categories", "shop_cart", "shop_wishlist",
            "shop_reviews", "shop_coupons", "shop_disputes", "shop_faq",
            "shop_audit_logs", "shop_stock_waitlist", "shop_custom_commands",
        }
        self.assertTrue(expected.issubset(names), expected - names)

    def test_additive_shop_schema_uses_same_database(self):
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute(
                "INSERT INTO shop_categories(name,description) VALUES(?,?)",
                ("CI category", "smoke test"),
            )
            con.execute(
                "INSERT INTO digital_products(name,price_paise,stock_count,category_id) VALUES(?,?,?,?)",
                ("CI product", 100, 2, "1"),
            )
            con.commit()
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM shop_categories WHERE name='CI category'"
            ).fetchone()[0], 1)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM digital_products WHERE name='CI product'"
            ).fetchone()[0], 1)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
