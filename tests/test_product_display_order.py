from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
import sqlite3

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import NotFound, Store


class ProductDisplayOrderStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.store = Store(self.path, self.key)
        self.archived = self.store.add_product("Old demo", 10_000)
        self.first = self.store.add_product("Account A", 20_000)
        self.second = self.store.add_product("Account B", 30_000)
        self.third = self.store.add_product("Account C", 40_000)
        self.store.set_deleted(self.archived, True)

    def tearDown(self):
        self.temp.cleanup()

    def test_swapping_visible_ids_preserves_internal_references(self):
        stock_id = self.store.add_stock(self.first, "private-account")
        result = self.store.set_product_display_id(self.first, 1)
        self.assertEqual(result["swapped_product_id"], self.archived)
        self.assertEqual(self.store.get_product(self.first)["display_id"], 1)
        self.assertEqual(self.store.get_product(self.archived)["display_id"], 2)
        self.assertEqual(self.store.product_id_for_display_id(1), self.first)
        self.assertEqual(self.store.product_id_for_display_id(2), self.archived)
        self.assertEqual(self.store.list_stock_items(self.first)[0]["id"], stock_id)
        self.assertEqual(self.store.get_product(self.first)["id"], self.first)
        self.assertEqual(self.store.add_product("New", 50_000), 5)
        restarted = Store(self.path, self.key)
        self.assertEqual(restarted.get_product(self.first)["display_id"], 1)
        self.assertEqual(restarted.get_product(self.archived)["display_id"], 2)

    def test_position_is_independent_of_visible_id_and_persists(self):
        self.store.set_product_display_id(self.first, 9)
        self.assertEqual([p["id"] for p in self.store.list_products()],
                         [self.first, self.second, self.third])
        self.assertEqual(self.store.move_product(self.third, 1)["position"], 1)
        self.assertEqual([p["id"] for p in self.store.list_products()],
                         [self.third, self.first, self.second])
        self.assertEqual(self.store.shift_product(self.first, 1)["position"], 3)
        restarted = Store(self.path, self.key)
        self.assertEqual([p["id"] for p in restarted.list_products()],
                         [self.third, self.second, self.first])
        self.assertEqual(restarted.get_product(self.first)["display_id"], 9)
        with self.assertRaises(ValueError):
            restarted.move_product(self.first, 0)
        with self.assertRaises(NotFound):
            restarted.shift_product(self.archived, 1)
        self.assertEqual([p["id"] for p in restarted.list_products()],
                         [self.third, self.second, self.first])

    def test_existing_database_gets_distinct_display_ids_and_order(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY, name TEXT, description TEXT, price INTEGER,
                    active INTEGER, created_at TEXT
                );
                INSERT INTO products VALUES (2, 'A', '', 10000, 1, '2026-01-01');
                INSERT INTO products VALUES (3, 'B', '', 10000, 1, '2026-01-01');
                CREATE TABLE bundles (
                    id INTEGER PRIMARY KEY, name TEXT, description TEXT, price INTEGER,
                    active INTEGER, deleted INTEGER, created_at TEXT
                );
                INSERT INTO bundles VALUES (1, 'Old bundle', '', 15000, 1, 0, '2026-01-01');
            """)
        migrated = Store(legacy_path, self.key)
        self.assertEqual([(p["id"], p["display_id"]) for p in migrated.list_products()],
                         [(2, 2), (3, 3)])
        migrated.set_product_display_id(2, 1)
        self.assertIsNone(migrated.get_product(2)["previous_price"])
        self.assertIsNone(migrated.get_bundle(1)["previous_price"])
        migrated.set_price(2, 9_000)
        self.assertEqual(migrated.get_product(2)["previous_price"], 10_000)
        Store(legacy_path, self.key)
        self.assertEqual(migrated.get_product(2)["display_id"], 1)


class ProductDisplayOrderBotTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        key = Fernet.generate_key().decode("ascii")
        config = Config(
            bot_token="test", admin_ids=frozenset({1}), bank_name="MB Bank",
            bank_bin="970422", bank_account_number="0123456789",
            bank_account_holder="TEST USER", sepay_webhook_secret="test-secret-long-enough",
            inventory_encryption_key=key,
            database_path=Path(self.temp.name) / "shop.sqlite3",
            webhook_host="127.0.0.1", webhook_port=8080,
            min_topup=10_000, max_topup=100_000_000, support_contact="",
        )
        self.shop = ShopBot(config, Store(config.database_path, key))
        self.archived = self.shop.store.add_product("Old demo", 10_000)
        self.first = self.shop.store.add_product("Account A", 20_000)
        self.second = self.shop.store.add_product("Account B", 30_000)
        self.shop.store.set_deleted(self.archived, True)
        self.message = SimpleNamespace(reply_text=AsyncMock(), text="")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_customer_sees_new_number_and_internal_callback_stays_stable(self):
        self.shop.store.set_product_display_id(self.first, 1)
        self.shop.store.move_product(self.second, 1)
        await self.shop.send_shop(self.message, 0)
        text = self.message.reply_text.await_args.args[0]
        self.assertEqual(text, "🛍 SẢN PHẨM\nChạm vào sản phẩm để xem chi tiết.")
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertEqual(
            [button.text.split()[0] for button in buttons
             if button.callback_data and button.callback_data.startswith("product:")],
            ["#1", "#3"],
        )
        self.assertTrue(any(button.text.startswith("#1 Account A") and
                            button.callback_data == f"product:{self.first}"
                            for button in buttons))
        await self.shop.send_product_detail(self.message, 1001, self.first)
        self.assertIn("#1 Account A", self.message.reply_text.await_args.args[0])

    async def test_customer_catalog_shows_all_products_in_one_vertical_list(self):
        added = [self.shop.store.add_product(f"Account {number}", 10_000)
                 for number in range(4, 11)]
        self.shop.store.set_product_display_id(added[-1], 1)

        await self.shop.send_shop(self.message, 0)
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row
            if button.callback_data]
        product_buttons = [button for button in buttons
                           if button.callback_data.startswith("product:")]
        self.assertEqual([button.text.split()[0] for button in product_buttons],
                         [f"#{number}" for number in range(1, 10)])
        self.assertEqual(product_buttons[0].callback_data, f"product:{added[-1]}")
        self.assertFalse(any(button.callback_data.startswith("shop:") for button in buttons))

    async def test_customer_catalog_keeps_navigation_when_limit_is_reached(self):
        added = [self.shop.store.add_product(f"Account {number}", 10_000)
                 for number in range(4, 11)]
        self.shop.store.set_product_display_id(added[-1], 1)

        with patch("bot.SHOP_CATALOG_MAX_PRODUCTS", 3):
            await self.shop.send_shop(self.message, 0)
            first_buttons = [button for row in self.message.reply_text.await_args.kwargs[
                "reply_markup"].inline_keyboard for button in row]
            self.assertEqual(
                [button.text.split()[0] for button in first_buttons
                 if button.callback_data.startswith("product:")], ["#1", "#2", "#3"],
            )
            self.assertTrue(any(button.callback_data == "shop:1" for button in first_buttons))

            await self.shop.send_shop(self.message, 2)
            last_buttons = [button for row in self.message.reply_text.await_args.kwargs[
                "reply_markup"].inline_keyboard for button in row]
            self.assertEqual(
                [button.text.split()[0] for button in last_buttons
                 if button.callback_data.startswith("product:")], ["#7", "#8", "#9"],
            )
            self.assertTrue(any(button.callback_data == "shop:1" for button in last_buttons))

    async def test_admin_can_change_number_and_move_with_buttons(self):
        context = SimpleNamespace(user_data={})
        query = SimpleNamespace(answer=AsyncMock(), message=self.message,
                                data=f"admin:displayid:{self.first}")
        admin = SimpleNamespace(
            callback_query=query, message=self.message,
            effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.callbacks(admin, context)
        self.assertEqual(context.user_data["awaiting_display_id"], self.first)
        self.message.text = "1"
        await self.shop.text_message(admin, context)
        self.assertNotIn("awaiting_display_id", context.user_data)
        self.assertEqual(self.shop.store.get_product(self.first)["display_id"], 1)
        query.data = f"admin:moveproduct:{self.second}:up"
        await self.shop.callbacks(admin, context)
        self.assertEqual([p["id"] for p in self.shop.store.list_products()],
                         [self.second, self.first])

        customer = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.shop.admin_move_product(customer,
                                           SimpleNamespace(args=[str(self.first), "1"]))
        self.assertEqual([p["id"] for p in self.shop.store.list_products()],
                         [self.second, self.first])
        await self.shop.admin_move_product(admin,
                                           SimpleNamespace(args=["#1", "1"]))
        self.assertEqual([p["id"] for p in self.shop.store.list_products()],
                         [self.first, self.second])

    async def test_price_command_uses_visible_number_without_touching_other_product(self):
        self.shop.store.set_product_display_id(self.first, 1)
        admin = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.set_price(admin, SimpleNamespace(args=["1", "15k"]))
        self.assertIn("Hãy nhập #1", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_product(self.first)["price"], 20_000)
        self.assertEqual(self.shop.store.get_product(self.archived)["price"], 10_000)

        await self.shop.set_price(admin, SimpleNamespace(args=["#1", "15k"]))
        self.assertIn("#1 Account A", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_product(self.first)["price"], 15_000)
        self.assertEqual(self.shop.store.get_product(self.archived)["price"], 10_000)

        await self.shop.send_admin_product(self.message, self.first)
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == f"admin:catalog:edit:{self.first}:price"
                            for button in buttons))

    async def test_bulk_stock_uses_visible_number_and_rejects_ambiguous_number(self):
        self.shop.store.set_product_display_id(self.first, 3)
        self.message.delete = AsyncMock()
        admin = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(user_data={}, bot=bot)

        self.message.text = "/bulkstock 3 demo-account"
        await self.shop.bulk_stock(admin, context)
        self.assertIn("Hãy nhập #3", bot.send_message.await_args.args[1])
        self.assertEqual(self.shop.store.stock_counts(self.first)["available"], 0)
        self.assertEqual(self.shop.store.stock_counts(self.second)["available"], 0)

        self.message.text = "/bulkstock #3 demo-account"
        await self.shop.bulk_stock(admin, context)
        self.assertEqual(self.shop.store.stock_counts(self.first)["available"], 1)
        self.assertEqual(self.shop.store.stock_counts(self.second)["available"], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
