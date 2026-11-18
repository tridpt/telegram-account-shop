from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import sqlite3

from cryptography.fernet import Fernet

from service_status import CustomerStatusNotifier, OUTAGE_SECONDS, send_status_message
from store import Store


class CustomerStatusTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.store = Store(self.path, self.key)
        for user_id in (1, 1001, 1002):
            self.store.ensure_user(user_id)
        self.now = [0.0]
        self.healthy = [True]
        self.sender = Mock(return_value=("sent", 0))
        self.notifier = CustomerStatusNotifier(
            self.store, "test-token", frozenset({1}),
            probe=lambda: self.healthy[0], sender=self.sender,
            clock=lambda: self.now[0], sleep=lambda _seconds: None,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_online_busy_recovery_each_notify_customers_once(self):
        batch = self.notifier.observe(True)
        self.assertEqual((batch["state"], batch["recipients"]), ("online", 2))
        self.notifier.send_due()
        self.assertEqual(self.sender.call_count, 2)
        self.assertTrue(all("Bot đã hoạt động" in call.args[2]
                            for call in self.sender.call_args_list))
        self.assertIsNone(self.notifier.observe(True))

        self.healthy[0] = False
        self.now[0] = 10
        self.assertIsNone(self.notifier.observe(False))
        self.now[0] = 10 + OUTAGE_SECONDS - 1
        self.assertIsNone(self.notifier.observe(False))
        self.now[0] = 10 + OUTAGE_SECONDS
        self.assertEqual(self.notifier.observe(False)["state"], "busy")
        self.notifier.send_due()
        self.assertEqual(self.sender.call_count, 4)
        self.assertTrue(all("Chủ shop có việc bận" in call.args[2]
                            for call in self.sender.call_args_list[2:]))
        self.assertIsNone(self.notifier.observe(False))

        self.healthy[0] = True
        self.now[0] += 1
        self.assertEqual(self.notifier.observe(True)["state"], "online")
        self.notifier.send_due()
        self.assertEqual(self.sender.call_count, 6)
        self.assertEqual(self.store.get_presence_state(), "online")
        self.assertIsNone(self.store.get_broadcast_status())

    def test_presence_notice_uses_saved_customer_language(self):
        self.store.set_user_language(1001, "en")
        self.notifier.observe(True)
        self.notifier.send_due()
        notices = {call.args[1]: call.args[2] for call in self.sender.call_args_list}
        self.assertIn("back online", notices[1001])
        self.assertIn("Bot đã hoạt động", notices[1002])

    def test_recovery_cancels_unsent_busy_messages(self):
        self.notifier.observe(True)
        self.healthy[0] = False
        self.notifier.observe(False)
        self.now[0] = OUTAGE_SECONDS
        busy = self.notifier.observe(False)
        self.assertEqual(busy["recipients"], 2)

        self.healthy[0] = True
        self.notifier.observe(True)
        pending = self.store.list_due_presence_deliveries()
        self.assertEqual({item["state"] for item in pending}, {"online"})
        with closing(sqlite3.connect(self.path)) as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM broadcast_deliveries WHERE batch_id=?",
                (busy["id"],),
            )]
        self.assertEqual(statuses, ["failed", "failed"])

    def test_blocked_customer_and_rate_limit_do_not_duplicate_sent_messages(self):
        batch = self.notifier.observe(True)
        self.sender.side_effect = [("failed", 0), ("rate_limited", 3)]
        self.notifier.send_due()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.store.list_due_presence_deliveries(), [])
        self.now[0] = 2
        self.notifier.send_due()
        self.assertEqual(self.sender.call_count, 2)

        self.now[0] = 4
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                """UPDATE broadcast_deliveries
                   SET next_attempt_at='2000-01-01T00:00:00.000Z'
                   WHERE batch_id=? AND status='pending'""",
                (batch["id"],),
            )
            conn.commit()
        self.sender.side_effect = None
        self.sender.return_value = ("sent", 0)
        restarted = CustomerStatusNotifier(
            Store(self.path, self.key), "test-token", frozenset({1}),
            probe=lambda: True, sender=self.sender,
            clock=lambda: self.now[0], sleep=lambda _seconds: None,
        )
        restarted.send_due()
        self.assertEqual(self.sender.call_count, 3)
        with closing(sqlite3.connect(self.path)) as conn:
            counts = dict(conn.execute(
                "SELECT status, COUNT(*) FROM broadcast_deliveries WHERE batch_id=? GROUP BY status",
                (batch["id"],),
            ))
        self.assertEqual(counts, {"failed": 1, "sent": 1})

    def test_existing_admin_broadcasts_are_migrated(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.execute(
                """CREATE TABLE broadcast_batches (
                    id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL,
                    content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '2026-01-01'
                )"""
            )
            conn.execute(
                "INSERT INTO broadcast_batches(admin_id, content) VALUES (1, 'old message')"
            )
            conn.commit()
        migrated = Store(legacy_path, self.key)
        self.assertEqual(migrated.get_broadcast_status()["id"], 1)
        self.assertIsNone(migrated.get_presence_state())

    def test_existing_customers_keep_balance_and_default_to_vietnamese(self):
        legacy_path = Path(self.temp.name) / "legacy-users.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.execute(
                """CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    stock_notifications_enabled INTEGER NOT NULL DEFAULT 1,
                    username TEXT, display_name TEXT,
                    created_at TEXT NOT NULL DEFAULT '2026-01-01'
                )"""
            )
            conn.execute("INSERT INTO users(telegram_id, balance) VALUES (1001, 25000)")
            conn.commit()

        migrated = Store(legacy_path, self.key)
        self.assertEqual(migrated.get_user_language(1001), "vi")
        self.assertEqual(migrated.get_balance(1001), 25_000)
        migrated.set_user_language(1001, "en")
        self.assertEqual(Store(legacy_path, self.key).get_user_language(1001), "en")

    def test_direct_telegram_sender_handles_success_block_and_rate_limit(self):
        with patch("service_status.http.client.HTTPSConnection") as connection_factory:
            connection = connection_factory.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"ok":true}'
            self.assertEqual(send_status_message("test-token", 1001, "hello"), ("sent", 0))
            self.assertIn("sendMessage", connection.request.call_args.args[1])

            response.status = 403
            self.assertEqual(send_status_message("test-token", 1001, "hello"), ("failed", 0))

            response.status = 429
            response.read.return_value = b'{"parameters":{"retry_after":4}}'
            self.assertEqual(send_status_message("test-token", 1001, "hello"),
                             ("rate_limited", 5))
