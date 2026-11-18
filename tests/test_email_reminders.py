from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock
import sqlite3

from cryptography.fernet import Fernet
from telegram.error import TelegramError

from bot import ShopBot
from config import Config
from store import Store


BASE = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)


def make_shop(path: Path, key: str) -> tuple[Store, int]:
    shop = Store(path, key)
    shop.ensure_user(1001, "alice", "Alice")
    topup = shop.create_topup(1001, 100_000)
    shop.record_bank_event(
        "reminder-test-topup", "0123456789", 100_000, topup["code"], "in",
        topup["code"], "0123456789",
    )
    product_id = shop.add_product("Email account", 10_000)
    shop.bulk_add_stock(product_id, ["first-secret", "second-secret"])
    shop.set_requires_email(product_id, True)
    return shop, product_id


def backdate(path: Path, order_id: int, when: datetime) -> None:
    stamp = when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE orders SET created_at=? WHERE id=?", (stamp, order_id))
        conn.commit()


class EmailReminderStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.shop, self.product_id = make_shop(self.path, self.key)

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_list_and_config_survive_restart(self):
        first = self.shop.purchase(1001, self.product_id,
                                   customer_email="first@example.com")
        second = self.shop.purchase(1001, self.product_id,
                                    customer_email="second@example.com")
        self.assertEqual(self.shop.get_email_reminder_minutes(), 30)
        self.assertEqual(self.shop.count_pending_email_orders(), 2)
        self.assertEqual([item["id"] for item in self.shop.list_pending_email_orders()],
                         [first["id"], second["id"]])
        self.shop.complete_email_order(first["id"], 1)
        self.assertEqual([item["id"] for item in self.shop.list_pending_email_orders()],
                         [second["id"]])
        self.shop.set_email_reminder_minutes(45)
        restarted = Store(self.path, self.key)
        self.assertEqual(restarted.get_email_reminder_minutes(), 45)
        self.assertEqual(restarted.count_pending_email_orders(), 1)
        for invalid in (-1, 1, 1441, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                restarted.set_email_reminder_minutes(invalid)

    def test_overdue_digest_is_bounded_and_new_overdue_orders_trigger_later(self):
        first = self.shop.purchase(1001, self.product_id,
                                   customer_email="first@example.com")
        backdate(self.path, first["id"], BASE)
        self.assertIsNone(self.shop.due_email_reminder(1, BASE + timedelta(minutes=29)))
        due = self.shop.due_email_reminder(1, BASE + timedelta(minutes=30))
        self.assertEqual((due["count"], due["max_order_id"]), (1, first["id"]))
        self.shop.finish_email_reminder(1, first["id"], BASE + timedelta(minutes=30))
        self.assertIsNone(self.shop.due_email_reminder(1, BASE + timedelta(minutes=44)))

        second = self.shop.purchase(1001, self.product_id,
                                    customer_email="second@example.com")
        backdate(self.path, second["id"], BASE + timedelta(minutes=15))
        self.assertIsNone(self.shop.due_email_reminder(1, BASE + timedelta(minutes=44)))
        newer = self.shop.due_email_reminder(1, BASE + timedelta(minutes=45))
        self.assertEqual((newer["count"], newer["max_order_id"]), (2, second["id"]))
        self.shop.finish_email_reminder(1, second["id"], BASE + timedelta(minutes=45))
        self.assertIsNone(self.shop.due_email_reminder(1, BASE + timedelta(minutes=89)))
        self.assertEqual(self.shop.due_email_reminder(
            1, BASE + timedelta(minutes=105),
        )["count"], 2)

        self.shop.set_email_reminder_minutes(0)
        self.assertIsNone(self.shop.due_email_reminder(1, BASE + timedelta(days=2)))
        self.shop.set_email_reminder_minutes(60)
        self.shop.complete_email_order(first["id"], 1)
        self.assertIsNone(self.shop.due_email_reminder(1, BASE + timedelta(minutes=74)))
        self.assertEqual(self.shop.due_email_reminder(
            1, BASE + timedelta(minutes=75),
        )["count"], 1)

    def test_failed_reminder_respects_retry_backoff(self):
        order = self.shop.purchase(1001, self.product_id,
                                   customer_email="first@example.com")
        backdate(self.path, order["id"], BASE)
        now = BASE + timedelta(minutes=30)
        self.shop.retry_email_reminder(1, 90, now)
        self.assertIsNone(self.shop.due_email_reminder(1, now + timedelta(seconds=89)))
        self.assertEqual(self.shop.due_email_reminder(
            1, now + timedelta(seconds=90),
        )["attempts"], 1)
        self.shop.finish_email_reminder(1, order["id"], now + timedelta(seconds=90))
        self.assertIsNone(self.shop.due_email_reminder(1, now + timedelta(minutes=59)))


class EmailReminderBotTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        key = Fernet.generate_key().decode("ascii")
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.store, self.product_id = make_shop(self.path, key)
        self.config = Config(
            bot_token="test", admin_ids=frozenset({1}), bank_name="MB Bank",
            bank_bin="970422", bank_account_number="0123456789",
            bank_account_holder="TEST USER", sepay_webhook_secret="test-secret-long-enough",
            inventory_encryption_key=key, database_path=self.path,
            webhook_host="127.0.0.1", webhook_port=8080,
            min_topup=10_000, max_topup=100_000_000, support_contact="",
        )
        self.bot = ShopBot(self.config, self.store)
        self.message = SimpleNamespace(reply_text=AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_admin_list_and_config_controls_are_private(self):
        order = self.store.purchase(1001, self.product_id,
                                    customer_email="first@example.com")
        admin = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        customer = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.bot.admin_pending_emails(customer, None)
        self.assertIn("chỉ dành cho quản trị viên", self.message.reply_text.await_args.args[0])
        await self.bot.admin_pending_emails(admin, None)
        self.assertIn(f"#{order['id']}", self.message.reply_text.await_args.args[0])
        self.assertNotIn("first@example.com", self.message.reply_text.await_args.args[0])
        await self.bot.admin_email_remind(admin, SimpleNamespace(args=["45"]))
        self.assertEqual(self.store.get_email_reminder_minutes(), 45)
        self.assertIn("45 phút", self.message.reply_text.await_args.args[0])
        await self.bot.admin_email_remind(customer, SimpleNamespace(args=["0"]))
        self.assertEqual(self.store.get_email_reminder_minutes(), 45)

        query = SimpleNamespace(answer=AsyncMock(), data="admin:emailremind:0",
                                message=self.message)
        admin.callback_query = query
        await self.bot.callbacks(admin, SimpleNamespace(user_data={}))
        self.assertEqual(self.store.get_email_reminder_minutes(), 0)
        query.data = "admin:pendingemails:0"
        await self.bot.callbacks(admin, SimpleNamespace(user_data={}))
        self.assertIn("ĐƠN EMAIL CHỜ", self.message.reply_text.await_args.args[0])

    async def test_reminder_digest_retries_and_does_not_repeat_after_restart(self):
        order = self.store.purchase(1001, self.product_id,
                                    customer_email="first@example.com")
        backdate(self.path, order["id"], datetime.now(timezone.utc) - timedelta(hours=2))
        sender = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        await self.bot.send_due_email_reminders(sender)
        sender.send_message.assert_awaited_once()
        self.assertEqual(sender.send_message.await_args.args[0], 1)
        self.assertNotIn("first@example.com", sender.send_message.await_args.args[1])

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE email_reminder_state SET next_attempt_at='2000-01-01T00:00:00Z'")
            conn.commit()
        restarted = ShopBot(self.config, Store(self.path, self.config.inventory_encryption_key))
        recovered = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_due_email_reminders(recovered)
        recovered.send_message.assert_awaited_once()
        self.assertEqual(recovered.send_message.await_args.kwargs[
            "reply_markup"].inline_keyboard[0][0].callback_data,
            "admin:pendingemails:0")
        await restarted.send_due_email_reminders(recovered)
        recovered.send_message.assert_awaited_once()
        self.store.complete_email_order(order["id"], 1)
        self.assertEqual(self.store.count_pending_email_orders(), 0)
        await restarted.send_due_email_reminders(recovered)
        recovered.send_message.assert_awaited_once()


if __name__ == "__main__":
    import unittest
    unittest.main()
