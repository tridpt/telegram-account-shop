from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import SHOP_TIMEZONE, Store


class ProductViewStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(
            Path(self.temp.name) / "shop.sqlite3", Fernet.generate_key().decode("ascii"),
        )
        for user_id in (1, 1001, 1002):
            self.store.ensure_user(user_id)

    def tearDown(self):
        self.temp.cleanup()

    def test_counts_clicks_and_distinct_customers_by_product_and_period(self):
        first = self.store.add_product("First", 10_000)
        second = self.store.add_product("Second", 20_000)
        now = datetime.now(SHOP_TIMEZONE)
        self.assertTrue(self.store.record_product_view(1001, first, now))
        self.assertTrue(self.store.record_product_view(1001, first, now))
        self.assertTrue(self.store.record_product_view(1002, first, now))
        self.assertTrue(self.store.record_product_view(1, first, now))
        self.assertTrue(self.store.record_product_view(1001, second, now - timedelta(days=8)))
        self.assertFalse(self.store.record_product_view(1001, 999999, now))

        week = self.store.product_view_report(7, frozenset({1}), now=now)
        self.assertEqual(week["products"][0]["id"], first)
        self.assertEqual((week["products"][0]["view_count"],
                          week["products"][0]["viewer_count"]), (3, 2))
        self.assertEqual(len(week["products"]), 1)
        month = self.store.product_view_report(30, frozenset({1}), now=now)
        self.assertEqual(month["products"][1]["view_count"], 1)
        customers = self.store.product_view_customers(
            first, 7, frozenset({1}), now=now,
        )["customers"]
        self.assertEqual([(item["user_id"], item["view_count"]) for item in customers],
                         [(1001, 2), (1002, 1)])

        viewed = self.store.list_user_product_views(1001)
        self.assertEqual([(item["name"], item["view_count"]) for item in viewed],
                         [("First", 2), ("Second", 1)])
        self.assertEqual(self.store.list_user_product_views(1001, 1, 1)[0]["name"],
                         "Second")
        self.assertEqual(self.store.list_user_product_views(1002)[0]["view_count"], 1)


class ProductViewBotTests(IsolatedAsyncioTestCase):
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
        self.shop.store.ensure_user(1001, "buyer", "Buyer")
        self.shop.store.ensure_user(1)
        self.product_id = self.shop.store.add_product("Viewed account", 10_000)
        self.shop.store.add_stock(self.product_id, "private-login")
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.query = SimpleNamespace(answer=AsyncMock(), data="", message=self.message)
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def tap(self, data: str, user_id: int = 1001):
        self.query.data = data
        update = SimpleNamespace(
            callback_query=self.query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=user_id),
        )
        await self.shop.callbacks(update, self.context)

    async def test_product_open_counts_but_follow_refresh_and_admin_do_not(self):
        await self.tap(f"product:{self.product_id}")
        await self.tap(f"follow:{self.product_id}")
        await self.tap(f"product:{self.product_id}")
        await self.tap(f"product:{self.product_id}", user_id=1)
        report = self.shop.store.product_view_report(7, frozenset({1}))
        self.assertEqual(report["products"][0]["view_count"], 2)
        self.assertEqual(report["products"][0]["viewer_count"], 1)

        await self.tap("admin:views:7:0", user_id=1)
        self.assertIn("2 lượt mở · 1 khách", self.message.reply_text.await_args.args[0])
        await self.tap(f"admin:viewproduct:{self.product_id}:7:0", user_id=1)
        self.assertIn("@buyer", self.message.reply_text.await_args.args[0])
        self.assertIn("2 lượt", self.message.reply_text.await_args.args[0])
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])

        self.message.reply_text.reset_mock()
        await self.tap("admin:views:7:0", user_id=1001)
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])

    async def test_user_management_shows_viewed_products_and_paged_history(self):
        await self.tap(f"product:{self.product_id}")
        await self.tap(f"product:{self.product_id}")
        for index in range(8):
            product_id = self.shop.store.add_product(f"Viewed {index}", 10_000)
            self.shop.store.record_product_view(1001, product_id)
        await self.shop.send_admin_users(self.message)
        self.assertIn("Đã xem:", self.message.reply_text.await_args.args[0])
        await self.shop.send_admin_user(self.message, 1001)
        detail = self.message.reply_text.await_args
        self.assertIn("Sản phẩm đã xem (lượt mở chi tiết):", detail.args[0])
        self.assertIn("lượt mở · gần nhất", detail.args[0])
        buttons = [button.callback_data for row in detail.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn("admin:userviews:1001:0", buttons)

        await self.tap("admin:userviews:1001:0", user_id=1)
        first_page = self.message.reply_text.await_args
        self.assertIn("SẢN PHẨM KHÁCH ĐÃ XEM", first_page.args[0])
        self.assertIn("admin:userviews:1001:1", [
            button.callback_data for row in first_page.kwargs[
                "reply_markup"].inline_keyboard for button in row
        ])
        await self.tap("admin:userviews:1001:1", user_id=1)
        self.assertIn("Viewed account · 2 lượt mở",
                      self.message.reply_text.await_args.args[0])
        await self.tap("admin:userviews:1001:0", user_id=1001)
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])

    async def test_hidden_product_is_not_counted(self):
        self.shop.store.set_active(self.product_id, False)
        await self.tap(f"product:{self.product_id}")
        report = self.shop.store.product_view_report(7, frozenset({1}))
        self.assertEqual(report["products"], [])

    async def test_ten_viewed_products_fit_in_one_report(self):
        for index in range(9):
            product_id = self.shop.store.add_product(f"Other {index}", 10_000)
            self.shop.store.record_product_view(1001, product_id)
        await self.tap(f"product:{self.product_id}")
        await self.tap("admin:views:7:0", user_id=1)
        buttons = [button.callback_data
                   for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertEqual(sum(button.startswith("admin:viewproduct:") for button in buttons), 10)
        self.assertNotIn("admin:views:7:1", buttons)

    async def test_admin_report_pages_more_than_twenty_viewed_products(self):
        for index in range(20):
            product_id = self.shop.store.add_product(f"Other {index}", 10_000)
            self.shop.store.record_product_view(1001, product_id)
        await self.tap(f"product:{self.product_id}")
        await self.tap("admin:views:7:0", user_id=1)
        buttons = [button.callback_data
                   for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:views:7:1", buttons)
        await self.tap("admin:views:7:1", user_id=1)
        buttons = [button.callback_data
                   for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:views:7:0", buttons)
        self.assertEqual(sum(button.startswith("admin:viewproduct:") for button in buttons), 1)
