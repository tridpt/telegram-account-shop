from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock
import sqlite3

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import NotFound, Store


class ProductFollowStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.store = Store(self.path, self.key)
        self.store.ensure_user(1001)
        self.store.ensure_user(1002)
        self.first = self.store.add_product("Account A", 20_000)
        self.second = self.store.add_product("Account B", 30_000)
        self.store.add_stock(self.first, "first-account")

    def tearDown(self):
        self.temp.cleanup()

    def test_followed_products_use_visible_number_order(self):
        self.store.set_product_following(1001, self.first, True)
        self.store.set_product_following(1001, self.second, True)
        self.store.move_product(self.second, 1)
        self.assertEqual(
            [item["display_id"] for item in self.store.list_followed_products(1001)],
            [1, 2],
        )
        self.store.set_product_display_id(self.second, 1)
        self.assertEqual(
            [item["id"] for item in self.store.list_followed_products(1001)],
            [self.second, self.first],
        )

    def test_only_followers_are_queued_for_that_products_stock_and_sale(self):
        empty = self.store.bulk_add_stock(
            self.first, ["second-account"], announce_except=frozenset(),
        )
        self.assertEqual(empty["recipients"], 0)
        self.store.set_product_following(1001, self.first, True)
        self.store.set_product_following(1002, self.second, True)
        batch = self.store.bulk_add_stock(
            self.first, ["third-account"], announce_except=frozenset(),
        )
        sale = self.store.start_sale(
            self.first, 19_000, 2, announce_except=frozenset(),
        )
        self.assertEqual((batch["recipients"], sale["recipients"]), (1, 1))
        self.assertEqual([x["user_id"] for x in self.store.list_due_stock_announcements()],
                         [1001])
        self.assertEqual([x["user_id"] for x in self.store.list_due_sale_announcements()],
                         [1001])
        self.assertEqual([x["id"] for x in self.store.list_followed_products(1002)],
                         [self.second])
        self.assertTrue(self.store.is_following_product(1001, self.first))
        restarted = Store(self.path, self.key)
        self.assertTrue(restarted.is_following_product(1001, self.first))

    def test_admin_follower_report_counts_current_customers_and_pages(self):
        self.store.ensure_user(1)
        for user_id in (1, 1001, 1002):
            self.store.set_product_following(user_id, self.first, True)
        self.store.set_product_following(1002, self.second, True)
        self.store.set_stock_notifications_enabled(1002, False)

        report = self.store.product_followers(self.first, frozenset({1}), limit=1)
        next_page = self.store.product_followers(
            self.first, frozenset({1}), limit=1, offset=1,
        )
        self.assertEqual(report["total"], 2)
        self.assertTrue(report["has_more"])
        self.assertFalse(next_page["has_more"])
        self.assertEqual(
            {item["user_id"] for item in report["followers"] + next_page["followers"]},
            {1001, 1002},
        )
        self.assertFalse(next(item for item in report["followers"] + next_page["followers"]
                              if item["user_id"] == 1002)["stock_notifications_enabled"])
        self.assertEqual(self.store.product_followers(self.second, frozenset({1}))["total"], 1)
        self.store.set_product_following(1001, self.first, False)
        self.assertEqual(self.store.product_followers(self.first, frozenset({1}))["total"], 1)

    def test_unfollow_cancels_queued_alerts_and_mute_only_affects_stock(self):
        self.store.set_product_following(1001, self.first, True)
        batch = self.store.bulk_add_stock(
            self.first, ["second-account"], announce_except=frozenset(),
        )
        sale = self.store.start_sale(
            self.first, 19_000, 1, announce_except=frozenset(),
        )
        self.store.set_product_following(1001, self.first, False)
        self.assertFalse(self.store.stock_announcement_sendable(batch["announcement_id"], 1001))
        self.assertEqual(self.store.list_due_stock_announcements(), [])
        self.assertEqual(self.store.list_due_sale_announcements(), [])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM sale_announcement_deliveries WHERE sale_id=?",
                (sale["id"],),
            ).fetchone()[0], "skipped")
        self.store.end_sale(self.first)
        self.store.set_product_following(1001, self.first, True)
        self.store.set_stock_notifications_enabled(1001, False)
        muted = self.store.bulk_add_stock(
            self.first, ["third-account"], announce_except=frozenset(),
        )
        new_sale = self.store.start_sale(
            self.first, 18_000, 1, announce_except=frozenset(),
        )
        self.assertEqual((muted["recipients"], new_sale["recipients"]), (0, 1))

    def test_old_broad_pending_deliveries_are_skipped_on_restart(self):
        batch = self.store.bulk_add_stock(
            self.first, ["second-account"], announce_except=frozenset(),
        )
        sale = self.store.start_sale(
            self.first, 19_000, 1, announce_except=frozenset(),
        )
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "INSERT INTO stock_announcement_deliveries(batch_id, user_id) VALUES (?, ?)",
                (batch["announcement_id"], 1001),
            )
            conn.execute(
                "INSERT INTO sale_announcement_deliveries(sale_id, user_id) VALUES (?, ?)",
                (sale["id"], 1001),
            )
            conn.commit()
        restarted = Store(self.path, self.key)
        self.assertEqual(restarted.list_due_stock_announcements(), [])
        self.assertEqual(restarted.list_due_sale_announcements(), [])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM stock_announcement_deliveries WHERE batch_id=?",
                (batch["announcement_id"],),
            ).fetchone()[0], "skipped")
            self.assertEqual(conn.execute(
                "SELECT status FROM sale_announcement_deliveries WHERE sale_id=?",
                (sale["id"],),
            ).fetchone()[0], "skipped")

    def test_hidden_product_cannot_gain_new_followers_but_can_be_unfollowed(self):
        self.store.set_product_following(1001, self.first, True)
        self.store.set_active(self.first, False)
        with self.assertRaises(NotFound):
            self.store.set_product_following(1002, self.first, True)
        self.store.set_product_following(1001, self.first, False)
        self.assertEqual(self.store.list_followed_products(1001), [])


class ProductFollowBotTests(IsolatedAsyncioTestCase):
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
        self.product_id = self.shop.store.add_product("Account", 20_000)
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.query = SimpleNamespace(answer=AsyncMock(), message=self.message, data="")
        self.update = SimpleNamespace(
            callback_query=self.query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_out_of_stock_customer_can_follow_and_unfollow(self):
        await self.shop.send_product_detail(self.message, 1001, self.product_id)
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"follow:{self.product_id}", [button.callback_data for button in buttons])
        self.query.data = f"follow:{self.product_id}"
        await self.shop.callbacks(self.update, self.context)
        self.assertTrue(self.shop.store.is_following_product(1001, self.product_id))
        self.query.data = "following:0"
        await self.shop.callbacks(self.update, self.context)
        self.assertIn("Account", self.message.reply_text.await_args.args[0])
        self.query.data = f"unfollow:{self.product_id}"
        await self.shop.callbacks(self.update, self.context)
        self.assertFalse(self.shop.store.is_following_product(1001, self.product_id))

    async def test_admin_can_view_followers_but_customer_cannot(self):
        self.shop.store.ensure_user(1001, "buyer", "Buyer")
        self.shop.store.set_product_following(1001, self.product_id, True)
        self.update.effective_user.id = 1
        await self.shop.send_admin_product(self.message, self.product_id)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn(f"admin:followers:{self.product_id}:0", buttons)
        self.assertIn("Khách theo dõi: 1", self.message.reply_text.await_args.args[0])

        self.query.data = f"admin:followers:{self.product_id}:0"
        await self.shop.callbacks(self.update, self.context)
        self.assertIn("@buyer (ID 1001)", self.message.reply_text.await_args.args[0])
        self.assertIn("Tổng: 1 khách", self.message.reply_text.await_args.args[0])
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])

        self.update.effective_user.id = 1001
        await self.shop.callbacks(self.update, self.context)
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])

    async def test_follower_list_has_next_page_only_after_twenty_customers(self):
        for user_id in range(1001, 1022):
            self.shop.store.ensure_user(user_id)
            self.shop.store.set_product_following(user_id, self.product_id, True)
        self.update.effective_user.id = 1
        self.query.data = f"admin:followers:{self.product_id}:0"
        await self.shop.callbacks(self.update, self.context)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn(f"admin:followers:{self.product_id}:1", buttons)
        self.query.data = f"admin:followers:{self.product_id}:1"
        await self.shop.callbacks(self.update, self.context)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn(f"admin:followers:{self.product_id}:0", buttons)
        self.assertEqual(sum(button.startswith("admin:user:") for button in buttons), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
