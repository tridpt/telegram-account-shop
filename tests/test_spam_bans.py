from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from telegram.ext import ApplicationHandlerStop

from bot import ShopBot
from config import Config
from store import Store


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SpamBanStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.key = Fernet.generate_key().decode("ascii")
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.store = Store(self.path, self.key)

    def tearDown(self):
        self.temp.cleanup()

    def test_three_warnings_within_an_hour_ban_and_unban_resets(self):
        first = self.store.record_spam_warning(1001, BASE)
        repeated = self.store.record_spam_warning(1001, BASE + timedelta(seconds=1))
        second = self.store.record_spam_warning(1001, BASE + timedelta(seconds=31))
        third = self.store.record_spam_warning(1001, BASE + timedelta(seconds=62))

        self.assertEqual(first, {"recorded": True, "count": 1, "banned": False})
        self.assertEqual(repeated, {"recorded": False, "count": 1, "banned": False})
        self.assertEqual(second, {"recorded": True, "count": 2, "banned": False})
        self.assertEqual(third, {"recorded": True, "count": 3, "banned": True})
        self.assertEqual(Store(self.path, self.key).banned_user_ids(), {1001})
        self.assertEqual(self.store.list_spam_bans()[0]["user_id"], 1001)
        self.assertFalse(self.store.record_spam_warning(
            1001, BASE + timedelta(seconds=93),
        )["recorded"])

        self.assertTrue(self.store.unban_user(1001))
        self.assertFalse(self.store.unban_user(1001))
        self.assertEqual(self.store.banned_user_ids(), set())
        self.assertEqual(self.store.record_spam_warning(
            1001, BASE + timedelta(seconds=94),
        )["count"], 1)

    def test_warnings_outside_rolling_hour_do_not_ban(self):
        self.store.record_spam_warning(1001, BASE)
        self.store.record_spam_warning(1001, BASE + timedelta(seconds=31))
        result = self.store.record_spam_warning(1001, BASE + timedelta(hours=1))
        self.assertEqual(result, {"recorded": True, "count": 2, "banned": False})
        self.assertEqual(self.store.banned_user_ids(), set())

    def test_banned_customer_is_excluded_from_announcements(self):
        self.store.ensure_user(1001)
        pending = self.store.create_broadcast(1, "Hello", frozenset({1}))
        self.assertEqual(pending["recipients"], 1)
        for seconds in (0, 31, 62):
            self.store.record_spam_warning(1001, BASE + timedelta(seconds=seconds))
        self.assertEqual(self.store.get_broadcast_status(pending["id"])["failed"], 1)
        self.assertEqual(self.store.count_broadcast_recipients(frozenset({1})), 0)
        self.assertEqual(self.store.create_broadcast(1, "Again", frozenset({1}))["recipients"], 0)
        self.assertEqual(self.store.queue_presence_broadcast(
            "online", "Online", frozenset({1}),
        )["recipients"], 0)
        product_id = self.store.add_product("Account", 10_000)
        self.assertEqual(self.store.bulk_add_stock(
            product_id, ["login:password"], announce_except=frozenset({1}),
        )["recipients"], 0)
        self.assertEqual(self.store.start_sale(
            product_id, 5_000, 1, announce_except=frozenset({1}),
        )["recipients"], 0)

    def test_admin_ban_is_persistent_and_stops_pending_broadcast(self):
        self.store.ensure_user(1001)
        pending = self.store.create_broadcast(1, "Hello", frozenset({1}))
        self.assertFalse(self.store.ban_user(2002))
        self.assertTrue(self.store.ban_user(1001))
        self.assertFalse(self.store.ban_user(1001))
        self.assertEqual(self.store.get_spam_ban(1001)["reason"], "admin")
        self.assertEqual(Store(self.path, self.key).banned_user_ids(), {1001})
        self.assertEqual(self.store.get_broadcast_status(pending["id"])["failed"], 1)
        self.assertEqual(self.store.count_broadcast_recipients(frozenset({1})), 0)
        self.assertTrue(self.store.unban_user(1001))
        self.assertIsNone(self.store.get_spam_ban(1001))


class SpamBanBotTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        key = Fernet.generate_key().decode("ascii")
        self.config = Config(
            bot_token="test", admin_ids=frozenset({1}), bank_name="MB Bank",
            bank_bin="970422", bank_account_number="0123456789",
            bank_account_holder="TEST USER", sepay_webhook_secret="test-secret-long-enough",
            inventory_encryption_key=key,
            database_path=Path(self.temp.name) / "shop.sqlite3",
            webhook_host="127.0.0.1", webhook_port=8080,
            min_topup=10_000, max_topup=100_000_000, support_contact="",
        )
        self.shop = ShopBot(self.config, Store(self.config.database_path, key))
        self.message = SimpleNamespace(reply_text=AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_customer_is_banned_after_three_warnings_and_admin_can_unban(self):
        now = [0.0]
        self.shop.limiter.clock = lambda: now[0]
        original_record = self.shop.store.record_spam_warning

        def record_at_fake_time(user_id):
            return original_record(user_id, BASE + timedelta(seconds=now[0]))

        update = SimpleNamespace(
            callback_query=None, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001, username="alice", full_name="Alice"),
            effective_message=self.message,
        )
        with patch.object(self.shop.store, "record_spam_warning", side_effect=record_at_fake_time):
            for warning_number in (1, 2, 3):
                for _ in range(10):
                    await self.shop.spam_guard(update, None)
                with self.assertRaises(ApplicationHandlerStop):
                    await self.shop.spam_guard(update, None)
                self.assertIn(
                    "bị cấm" if warning_number == 3 else f"{warning_number}/3",
                    self.message.reply_text.await_args.args[0],
                )
                now[0] += 31

        self.assertIn(1001, self.shop.banned_user_ids)
        self.assertEqual(self.shop.store.banned_user_ids(), {1001})
        reply_count = self.message.reply_text.await_count
        with self.assertRaises(ApplicationHandlerStop):
            await self.shop.spam_guard(update, None)
        self.assertEqual(self.message.reply_text.await_count, reply_count)

        restarted = ShopBot(self.config, Store(
            self.config.database_path, self.config.inventory_encryption_key,
        ))
        with self.assertRaises(ApplicationHandlerStop):
            await restarted.spam_guard(update, None)

        admin_update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.admin_bans(admin_update, None)
        self.assertIn("alice (ID 1001)", self.message.reply_text.await_args.args[0])
        await self.shop.admin_unban(admin_update, SimpleNamespace(args=["1001"]))
        self.assertEqual(self.shop.store.banned_user_ids(), set())
        self.assertNotIn(1001, self.shop.banned_user_ids)
        await self.shop.spam_guard(update, None)

    async def test_non_admin_cannot_use_ban_management(self):
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.shop.admin_bans(update, None)
        self.assertIn("chỉ dành cho quản trị viên", self.message.reply_text.await_args.args[0])
        await self.shop.admin_unban(update, SimpleNamespace(args=["1001"]))
        self.assertEqual(self.shop.store.banned_user_ids(), set())

    async def test_admin_menu_can_unban_and_admin_is_exempt(self):
        self.shop.store.ensure_user(1001)
        for seconds in (0, 31, 62):
            self.shop.store.record_spam_warning(1001, BASE + timedelta(seconds=seconds))
        self.shop.banned_user_ids.add(1001)
        query = SimpleNamespace(
            data="admin:bans:0", answer=AsyncMock(), message=self.message,
        )
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertIn("KHÁCH BỊ CẤM", self.message.reply_text.await_args.args[0])
        query.data = "admin:unban:1001"
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertEqual(self.shop.store.banned_user_ids(), set())
        self.assertNotIn(1001, self.shop.banned_user_ids)
        self.assertIn("Đã bỏ cấm", self.message.reply_text.await_args.args[0])

        for _ in range(100):
            await self.shop.spam_guard(update, None)
        self.assertEqual(self.shop.store.banned_user_ids(), set())

    async def test_admin_ban_button_requires_confirmation_and_blocks_customer(self):
        self.shop.store.ensure_user(1001, username="alice", display_name="Alice")
        await self.shop.send_admin_user(self.message, 1001)
        buttons = self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertIn("admin:ban:1001", [button.callback_data for row in buttons for button in row])

        query = SimpleNamespace(data="admin:ban:1001", answer=AsyncMock(), message=self.message)
        admin_update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.callbacks(admin_update, SimpleNamespace(user_data={}))
        self.assertEqual(self.shop.store.banned_user_ids(), set())
        buttons = self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertIn("admin:banconfirm:1001",
                      [button.callback_data for row in buttons for button in row])

        query.data = "admin:banconfirm:1001"
        await self.shop.callbacks(admin_update, SimpleNamespace(user_data={}))
        self.assertEqual(self.shop.store.banned_user_ids(), {1001})
        self.assertIn(1001, self.shop.banned_user_ids)
        self.assertEqual(self.shop.store.get_spam_ban(1001)["reason"], "admin")
        buttons = self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertIn("admin:unban:1001",
                      [button.callback_data for row in buttons for button in row])

        customer_update = SimpleNamespace(
            callback_query=None, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001), effective_message=self.message,
        )
        with self.assertRaises(ApplicationHandlerStop):
            await self.shop.spam_guard(customer_update, None)

    async def test_only_admin_can_ban_and_admin_cannot_ban_self(self):
        self.shop.store.ensure_user(1001)
        query = SimpleNamespace(
            data="admin:banconfirm:1001", answer=AsyncMock(), message=self.message,
        )
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertEqual(self.shop.store.banned_user_ids(), set())

        self.shop.store.ensure_user(1)
        update.effective_user.id = 1
        query.data = "admin:banconfirm:1"
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertEqual(self.shop.store.banned_user_ids(), set())
        self.assertIn("Không thể ban tài khoản quản trị viên",
                      self.message.reply_text.await_args.args[0])


if __name__ == "__main__":
    import unittest
    unittest.main()
