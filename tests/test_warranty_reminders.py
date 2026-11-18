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
        "warranty-reminder-topup", "0123456789", 100_000, topup["code"], "in",
        topup["code"], "0123456789",
    )
    product_id = shop.add_product("Demo account", 10_000)
    shop.bulk_add_stock(product_id, ["first-secret", "second-secret"])
    return shop, product_id


def backdate_ticket(path: Path, ticket_id: int, when: datetime) -> None:
    stamp = when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE warranty_requests SET created_at=? WHERE id=?", (stamp, ticket_id))
        conn.commit()


class WarrantyReminderStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.shop, self.product_id = make_shop(self.path, self.key)

    def tearDown(self):
        self.temp.cleanup()

    def ticket(self, note: str = "Private account issue") -> dict:
        order = self.shop.purchase(1001, self.product_id)
        return self.shop.create_warranty(1001, order["id"], note)

    def test_config_persists_and_open_tickets_are_oldest_first(self):
        first = self.ticket()
        second = self.ticket()
        backdate_ticket(self.path, second["id"], BASE)
        self.assertEqual(self.shop.get_warranty_reminder_minutes(), 60)
        self.assertEqual(self.shop.count_open_warranties(), 2)
        self.assertEqual([t["id"] for t in self.shop.list_warranties()],
                         [second["id"], first["id"]])
        self.shop.set_warranty_reminder_minutes(45)
        restarted = Store(self.path, self.key)
        self.assertEqual(restarted.get_warranty_reminder_minutes(), 45)
        for invalid in (-1, 1, 1441, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                restarted.set_warranty_reminder_minutes(invalid)
        self.shop.resolve_warranty(second["id"], "Fixed")
        self.assertEqual(self.shop.count_open_warranties(), 1)

    def test_due_digest_is_bounded_and_resolved_tickets_drop_out(self):
        first = self.ticket()
        backdate_ticket(self.path, first["id"], BASE)
        self.assertIsNone(self.shop.due_warranty_reminder(1, BASE + timedelta(minutes=59)))
        due = self.shop.due_warranty_reminder(1, BASE + timedelta(minutes=60))
        self.assertEqual((due["count"], due["max_ticket_id"]), (1, first["id"]))
        self.shop.finish_warranty_reminder(1, first["id"], BASE + timedelta(minutes=60))
        self.assertIsNone(self.shop.due_warranty_reminder(1, BASE + timedelta(minutes=119)))

        second = self.ticket()
        backdate_ticket(self.path, second["id"], BASE + timedelta(minutes=45))
        self.assertIsNone(self.shop.due_warranty_reminder(1, BASE + timedelta(minutes=104)))
        due = self.shop.due_warranty_reminder(1, BASE + timedelta(minutes=105))
        self.assertEqual((due["count"], due["max_ticket_id"]), (2, second["id"]))
        self.shop.finish_warranty_reminder(1, second["id"], BASE + timedelta(minutes=105))
        self.assertIsNone(self.shop.due_warranty_reminder(1, BASE + timedelta(minutes=164)))
        self.assertEqual(self.shop.due_warranty_reminder(
            1, BASE + timedelta(minutes=165),
        )["count"], 2)

        self.shop.resolve_warranty(first["id"], "Done")
        self.assertEqual(self.shop.due_warranty_reminder(
            1, BASE + timedelta(minutes=165),
        )["count"], 1)
        self.shop.resolve_warranty(second["id"], "Done")
        self.assertIsNone(self.shop.due_warranty_reminder(1, BASE + timedelta(days=1)))

    def test_retry_and_disable(self):
        ticket = self.ticket()
        backdate_ticket(self.path, ticket["id"], BASE)
        now = BASE + timedelta(minutes=60)
        self.shop.retry_warranty_reminder(1, 90, now)
        self.assertIsNone(self.shop.due_warranty_reminder(1, now + timedelta(seconds=89)))
        self.assertEqual(self.shop.due_warranty_reminder(
            1, now + timedelta(seconds=90),
        )["attempts"], 1)
        self.shop.set_warranty_reminder_minutes(0)
        self.assertIsNone(self.shop.due_warranty_reminder(1, now + timedelta(days=1)))
        self.shop.set_warranty_reminder_minutes(30)
        self.assertEqual(self.shop.due_warranty_reminder(
            1, now + timedelta(seconds=90),
        )["attempts"], 0)


class WarrantyReminderBotTests(IsolatedAsyncioTestCase):
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

    def ticket(self) -> dict:
        order = self.store.purchase(1001, self.product_id)
        return self.store.create_warranty(1001, order["id"], "Private account issue")

    async def test_admin_controls_are_private(self):
        ticket = self.ticket()
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
        await self.bot.admin_warranty_remind(customer, SimpleNamespace(args=["0"]))
        self.assertEqual(self.store.get_warranty_reminder_minutes(), 60)
        await self.bot.admin_warranties(admin, SimpleNamespace(args=[]))
        self.assertIn(f"#{ticket['id']}", self.message.reply_text.await_args.args[0])
        self.assertNotIn("Private account issue", self.message.reply_text.await_args.args[0])
        await self.bot.admin_warranty_remind(admin, SimpleNamespace(args=["45"]))
        self.assertEqual(self.store.get_warranty_reminder_minutes(), 45)
        query = SimpleNamespace(answer=AsyncMock(), data="admin:warrantyremind:0",
                                message=self.message)
        admin.callback_query = query
        await self.bot.callbacks(admin, SimpleNamespace(user_data={}))
        self.assertEqual(self.store.get_warranty_reminder_minutes(), 0)

    async def test_reminder_retries_and_stops_after_resolution(self):
        ticket = self.ticket()
        backdate_ticket(self.path, ticket["id"], datetime.now(timezone.utc) - timedelta(hours=2))
        sender = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        await self.bot.send_due_warranty_reminders(sender)
        sender.send_message.assert_awaited_once()
        self.assertEqual(sender.send_message.await_args.args[0], 1)
        self.assertNotIn("Private account issue", sender.send_message.await_args.args[1])

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("UPDATE warranty_reminder_state SET next_attempt_at='2000-01-01T00:00:00Z'")
            conn.commit()
        restarted = ShopBot(self.config, Store(self.path, self.config.inventory_encryption_key))
        recovered = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_due_warranty_reminders(recovered)
        recovered.send_message.assert_awaited_once()
        self.assertEqual(recovered.send_message.await_args.kwargs[
            "reply_markup"].inline_keyboard[0][0].callback_data, "admin:warranties:0")
        await restarted.send_due_warranty_reminders(recovered)
        recovered.send_message.assert_awaited_once()
        self.store.resolve_warranty(ticket["id"], "Done")
        await restarted.send_due_warranty_reminders(recovered)
        recovered.send_message.assert_awaited_once()


if __name__ == "__main__":
    import unittest
    unittest.main()
