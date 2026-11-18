from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import hmac
import http.client
import json
import socket
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from config import Config
from payments import process_sepay_payload
from store import (
    DuplicateStock, InsufficientBalance, NotFound, OutOfStock, PriceChanged,
    SCHEMA, Store, WarrantyAlreadyOpen, WarrantyClosed,
    TopupLimitReached,
)
from webhook import start_webhook_server


ACCOUNT = "0123456789"


def payload(transaction_id, content, amount=50_000, account=ACCOUNT, transfer_type="in"):
    return {
        "id": transaction_id,
        "accountNumber": account,
        "transferAmount": amount,
        "transferType": transfer_type,
        "content": content,
        "code": None,
    }


class PaymentTests(unittest.TestCase):
    def test_inventory_audit_claims_low_stock_once_and_skips_empty_demo(self):
        self.store.add_product("Empty demo", 10_000)
        product_id = self.store.add_product("Low stock", 10_000)
        self.store.add_stock(product_id, "only-account")
        alerts = self.store.list_low_stock_alerts()
        self.assertEqual([(item["product_id"], item["available"]) for item in alerts],
                         [(product_id, 1)])
        self.assertTrue(self.store.claim_low_stock_alert(product_id))
        self.assertFalse(self.store.claim_low_stock_alert(product_id))
        self.assertEqual(self.store.list_low_stock_alerts(), [])
        self.store.reset_low_stock_alert(product_id)
        self.assertEqual(len(self.store.list_low_stock_alerts()), 1)
        self.store.bulk_add_stock(product_id, ["second", "third"])
        self.assertEqual(self.store.list_low_stock_alerts(), [])

    def test_webhook_alerts_on_rejected_and_failed_signed_requests(self):
        config = SimpleNamespace(
            webhook_host="127.0.0.1", webhook_port=0,
            sepay_webhook_secret="test-secret-long-enough-to-be-safe",
            bank_account_number=ACCOUNT,
        )
        alerts = []

        def failed_notification(result):
            raise RuntimeError("notification queue unavailable")

        server, thread = start_webhook_server(config, self.store, failed_notification, alerts.append)
        try:
            data = json.dumps(payload(999, self.topup["code"])).encode()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/webhooks/sepay", data)
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()

            timestamp = str(int(time.time()))
            signature = "sha256=" + hmac.new(
                config.sepay_webhook_secret.encode(), timestamp.encode() + b"." + data,
                hashlib.sha256,
            ).hexdigest()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            with patch("webhook.process_sepay_payload", side_effect=RuntimeError("storage failed")):
                connection.request("POST", "/webhooks/sepay", data, {
                    "X-SePay-Timestamp": timestamp, "X-SePay-Signature": signature,
                })
                response = connection.getresponse()
                self.assertEqual(response.status, 500)
                response.read()
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            with patch("webhook.process_sepay_payload", return_value={"status": "credited"}):
                connection.request("POST", "/webhooks/sepay", data, {
                    "X-SePay-Timestamp": timestamp, "X-SePay-Signature": signature,
                })
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            connection.close()
            self.assertEqual(alerts, ["rejected", "processing", "notification"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_status_marks_only_successfully_processed_webhooks_even_on_retry(self):
        transaction = payload(12345, self.topup["code"])
        process_sepay_payload(self.store, transaction, ACCOUNT)
        self.assertIsNone(self.store.get_operational_status(ACCOUNT)["last_webhook_at"])
        config = SimpleNamespace(
            webhook_host="127.0.0.1", webhook_port=0,
            sepay_webhook_secret="test-secret-long-enough-to-be-safe",
            bank_account_number=ACCOUNT,
        )
        server, thread = start_webhook_server(config, self.store, lambda result: None)
        try:
            data = json.dumps(transaction).encode()
            timestamp = str(int(time.time()))
            signature = "sha256=" + hmac.new(
                config.sepay_webhook_secret.encode(), timestamp.encode() + b"." + data,
                hashlib.sha256,
            ).hexdigest()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/webhooks/sepay", data, {
                "X-SePay-Timestamp": timestamp, "X-SePay-Signature": "sha256=" + "0" * 64,
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            self.assertIsNone(self.store.get_operational_status(ACCOUNT)["last_webhook_at"])

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/webhooks/sepay", data, {
                "X-SePay-Timestamp": timestamp, "X-SePay-Signature": signature,
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertIsNotNone(self.store.get_operational_status(ACCOUNT)["last_webhook_at"])
            self.assertEqual(self.store.get_balance(1001), 50_000)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_failed_credit_does_not_claim_a_successful_webhook(self):
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("""CREATE TRIGGER fail_ledger BEFORE INSERT ON ledger
                            BEGIN SELECT RAISE(ABORT, 'ledger unavailable'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            process_sepay_payload(
                self.store, payload(54321, self.topup["code"]), ACCOUNT,
                via_webhook=True,
            )
        self.assertIsNone(self.store.get_operational_status(ACCOUNT)["last_webhook_at"])
        self.assertEqual(self.store.get_balance(1001), 0)

    def test_status_counts_stock_after_alert_and_excludes_unstocked_products(self):
        self.store.add_product("Unstocked demo", 10_000)
        low_id = self.store.add_product("Low stock", 10_000)
        self.store.add_stock(low_id, "one account")
        self.assertTrue(self.store.claim_low_stock_alert(low_id))
        high_id = self.store.add_product("Enough stock", 10_000)
        self.store.bulk_add_stock(high_id, ["account 1", "account 2", "account 3"])
        self.store.record_bank_event(
            "unknown-code", ACCOUNT, 20_000, "wrong code", "in", None, ACCOUNT,
        )
        status = self.store.get_operational_status(ACCOUNT)
        self.assertEqual(status["unmatched_count"], 1)
        self.assertEqual([(p["id"], p["available"]) for p in status["low_stock"]],
                         [(low_id, 1)])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.encryption_key = Fernet.generate_key().decode("ascii")
        self.store = Store(Path(self.temp.name) / "shop.sqlite3", self.encryption_key)
        self.store.ensure_user(1001)
        self.topup = self.store.create_topup(1001, 50_000)

    def tearDown(self):
        self.temp.cleanup()

    def test_auto_credit_uses_real_amount_and_deduplicates_retries(self):
        event = payload(501, f"CK {self.topup['code']}", amount=49_000)
        first = process_sepay_payload(self.store, event, ACCOUNT)
        retry = process_sepay_payload(self.store, event, ACCOUNT)
        second_transfer = process_sepay_payload(
            self.store, payload(502, self.topup["code"], amount=20_000), ACCOUNT
        )

        self.assertEqual(first["status"], "credited")
        self.assertEqual(retry["status"], "duplicate")
        self.assertEqual(second_transfer["status"], "credited")
        self.assertEqual(self.store.get_balance(1001), 69_000)
        self.assertEqual(self.store.list_topups(1001)[0]["paid_amount"], 69_000)

    def test_one_user_cannot_create_unlimited_topup_codes(self):
        for _ in range(11):
            self.store.create_topup(1001, 10_000)
        with self.assertRaises(TopupLimitReached):
            self.store.create_topup(1001, 10_000)
        self.assertEqual(len(self.store.list_topups(1001, 20)), 12)
        self.assertEqual(self.store.get_balance(1001), 0)

    def test_paid_topup_does_not_use_the_open_code_limit(self):
        process_sepay_payload(self.store, payload(503, self.topup["code"]), ACCOUNT)
        for _ in range(12):
            self.store.create_topup(1001, 10_000)
        with self.assertRaises(TopupLimitReached):
            self.store.create_topup(1001, 10_000)

    def test_wrong_account_outgoing_and_unknown_code_never_credit(self):
        wrong_account = process_sepay_payload(
            self.store, payload(601, self.topup["code"], account="999999"), ACCOUNT
        )
        outgoing = process_sepay_payload(
            self.store, payload(602, self.topup["code"], transfer_type="out"), ACCOUNT
        )
        unknown = process_sepay_payload(
            self.store, payload(603, "NAP000000000000"), ACCOUNT
        )
        ambiguous = process_sepay_payload(
            self.store, payload(604, self.topup["code"] + " NAP000000000000"), ACCOUNT
        )
        self.assertEqual(wrong_account["status"], "ignored")
        self.assertEqual(outgoing["status"], "ignored")
        self.assertEqual(unknown["status"], "unmatched")
        self.assertEqual(ambiguous["status"], "unmatched")
        self.assertEqual(self.store.get_balance(1001), 0)
        self.assertEqual(len(self.store.list_unmatched_events()), 2)

    def test_invalid_payload_is_rejected(self):
        for change in ({"id": True}, {"transferAmount": "50000"}, {"content": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                process_sepay_payload(
                    self.store, {**payload(701, self.topup["code"]), **change}, ACCOUNT
                )
        self.assertEqual(self.store.get_balance(1001), 0)

    def test_purchase_is_atomic_and_order_is_private(self):
        product_id = self.store.add_product("Test account", 40_000)
        self.store.add_stock(product_id, "login:secret")
        self.store.ensure_user(1002)
        process_sepay_payload(self.store, payload(801, self.topup["code"]), ACCOUNT)
        second_topup = self.store.create_topup(1002, 50_000)
        process_sepay_payload(self.store, payload(802, second_topup["code"]), ACCOUNT)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda user: self._try_purchase(user, product_id), [1001, 1002]))

        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["bought", "out_of_stock"])
        winner = next(outcome for outcome in outcomes if outcome[0] == "bought")
        loser = 1002 if winner[1] == 1001 else 1001
        self.assertEqual(self.store.get_balance(winner[1]), 10_000)
        self.assertEqual(self.store.get_balance(loser), 50_000)
        self.assertIsNone(self.store.get_order(loser, winner[2]))
        self.assertEqual(self.store.get_order(winner[1], winner[2])["secret"], "login:secret")
        with self.assertRaises(OutOfStock):
            self.store.purchase(loser, product_id)

    def test_insufficient_balance_does_not_consume_stock(self):
        product_id = self.store.add_product("Test account", 40_000)
        self.store.add_stock(product_id, "login:secret")
        with self.assertRaises(InsufficientBalance):
            self.store.purchase(1001, product_id)
        self.assertEqual(self.store.get_product(product_id)["available"], 1)

    def test_price_change_requires_new_confirmation(self):
        product_id = self.store.add_product("Test account", 40_000)
        self.store.add_stock(product_id, "login:secret")
        process_sepay_payload(self.store, payload(851, self.topup["code"]), ACCOUNT)
        self.store.set_price(product_id, 45_000)
        with self.assertRaises(PriceChanged):
            self.store.purchase(1001, product_id, expected_price=40_000)
        self.assertEqual(self.store.get_balance(1001), 50_000)
        self.assertEqual(self.store.get_product(product_id)["available"], 1)

    def test_inventory_is_encrypted_and_duplicate_accounts_are_blocked(self):
        product_id = self.store.add_product("Test account", 40_000)
        self.store.add_stock(product_id, "private-login:password")
        with closing(sqlite3.connect(self.store.path)) as conn:
            saved = conn.execute("SELECT secret_ciphertext FROM stock").fetchone()[0]
        self.assertNotIn("private-login:password", saved)
        with self.assertRaises(DuplicateStock):
            self.store.add_stock(product_id, "private-login:password")

    def test_bulk_stock_imports_duplicate_accounts_even_if_sold_or_in_another_product(self):
        product_id = self.store.add_product("Test account", 10_000)
        other_product = self.store.add_product("Other account", 10_000)
        self.store.add_stock(product_id, "sold")
        self.store.add_stock(product_id, "already")
        self.store.add_stock(other_product, "other-product")
        process_sepay_payload(self.store, payload(1099, self.topup["code"]), ACCOUNT)
        self.store.purchase(1001, product_id)

        result = self.store.bulk_add_stock(
            product_id,
            ["fresh-1", "fresh-1", "already", "other-product", "sold", "fresh-2"],
            announce_except=frozenset(),
        )
        self.assertEqual(len(result["ids"]), 6)
        self.assertEqual(result["duplicate_lines"], [2, 3, 4, 5])
        self.assertEqual(result["available"], 7)
        self.assertIsNotNone(result["announcement_id"])
        with closing(sqlite3.connect(self.store.path)) as conn:
            added_count = conn.execute(
                "SELECT added_count FROM stock_announcement_batches WHERE id=?",
                (result["announcement_id"],),
            ).fetchone()[0]
        self.assertEqual(added_count, 6)

        duplicate_only = self.store.bulk_add_stock(
            product_id, ["fresh-1", "sold"], announce_except=frozenset(),
        )
        self.assertEqual(len(duplicate_only["ids"]), 2)
        self.assertEqual(duplicate_only["duplicate_lines"], [1, 2])
        self.assertIsNotNone(duplicate_only["announcement_id"])
        self.assertEqual(self.store.stock_counts(product_id),
                         {"total": 10, "available": 9, "sold": 1})

    def test_duplicate_bulk_stock_units_can_each_be_purchased(self):
        product_id = self.store.add_product("Shared account", 10_000)
        result = self.store.bulk_add_stock(product_id, ["shared-login", "shared-login"])
        self.assertEqual(len(result["ids"]), 2)
        self.assertEqual(result["duplicate_lines"], [2])
        process_sepay_payload(self.store, payload(1100, self.topup["code"]), ACCOUNT)
        self.assertEqual(self.store.purchase(1001, product_id)["secret"], "shared-login")
        self.assertEqual(self.store.purchase(1001, product_id)["secret"], "shared-login")
        self.assertEqual(self.store.stock_counts(product_id)["sold"], 2)

    def test_bulk_stock_validates_batch_and_low_stock_alert_rearms_after_restock(self):
        product_id = self.store.add_product("Test account", 10_000)
        result = self.store.bulk_add_stock(product_id, [f"login-{i}" for i in range(5)])
        self.assertEqual((len(result["ids"]), result["available"]), (5, 5))
        with self.assertRaises(ValueError):
            self.store.bulk_add_stock(product_id, ["new-login", ""])
        with self.assertRaises(ValueError):
            self.store.bulk_add_stock(product_id, [f"extra-{i}" for i in range(101)])
        self.assertEqual(self.store.stock_counts(product_id)["total"], 5)

        process_sepay_payload(self.store, payload(1101, self.topup["code"], amount=100_000), ACCOUNT)
        alerts = [self.store.purchase(1001, product_id)["low_stock_alert"] for _ in range(5)]
        self.assertEqual(alerts, [False, False, True, False, False])
        self.assertEqual(self.store.get_product(product_id)["available"], 0)
        self.store.bulk_add_stock(product_id, ["fresh-1", "fresh-2", "fresh-3"])
        order = self.store.purchase(1001, product_id)
        self.assertTrue(order["low_stock_alert"])
        self.assertEqual(order["available"], 2)

    def test_warranty_is_private_encrypted_and_can_reopen_after_resolution(self):
        product_id = self.store.add_product("Test account", 10_000)
        self.store.add_stock(product_id, "login:secret")
        process_sepay_payload(self.store, payload(1201, self.topup["code"]), ACCOUNT)
        order = self.store.purchase(1001, product_id)
        self.store.ensure_user(1002)

        with self.assertRaises(NotFound):
            self.store.create_warranty(1002, order["id"], "Cannot log in")
        ticket = self.store.create_warranty(1001, order["id"], "Cannot log in")
        with self.assertRaises(WarrantyAlreadyOpen):
            self.store.create_warranty(1001, order["id"], "Again")
        self.assertIsNone(self.store.get_latest_warranty(1002, order["id"]))
        self.assertEqual(len(self.store.list_warranties()), 1)
        with closing(sqlite3.connect(self.store.path)) as conn:
            saved = conn.execute("SELECT note FROM warranty_requests WHERE id=?", (ticket["id"],)).fetchone()[0]
        self.assertNotIn("Cannot log in", saved)

        self.store.resolve_warranty(ticket["id"], "Replacement sent")
        self.assertEqual(self.store.get_latest_warranty(1001, order["id"])["reply"], "Replacement sent")
        self.assertEqual(self.store.list_warranties(), [])
        with self.assertRaises(WarrantyClosed):
            self.store.resolve_warranty(ticket["id"], "Double reply")
        new_ticket = self.store.create_warranty(1001, order["id"], "Still failing")
        self.assertNotEqual(new_ticket["id"], ticket["id"])

    def test_daily_report_uses_actual_credits_and_vietnam_day_boundary(self):
        product_id = self.store.add_product("Test account", 10_000)
        self.store.bulk_add_stock(product_id, ["account-1", "account-2"])
        first = self.store.record_bank_event(
            "report-1", ACCOUNT, 20_000, self.topup["code"], "in", self.topup["code"], ACCOUNT,
        )
        second = self.store.record_bank_event(
            "report-2", ACCOUNT, 30_000, self.topup["code"], "in", self.topup["code"], ACCOUNT,
        )
        self.assertEqual((first["status"], second["status"]), ("credited", "credited"))
        order1 = self.store.purchase(1001, product_id)
        order2 = self.store.purchase(1001, product_id)
        with closing(sqlite3.connect(self.store.path)) as conn:
            for transaction_id, timestamp in (
                ("report-1", "2026-09-25T16:59:59.000Z"),
                ("report-2", "2026-09-25T17:00:00.000Z"),
            ):
                conn.execute(
                    """UPDATE ledger SET created_at=? WHERE kind='topup' AND ref_id=(
                       SELECT id FROM bank_events WHERE provider_txn_id=?)""",
                    (timestamp, transaction_id),
                )
            conn.execute("UPDATE orders SET created_at=? WHERE id=?",
                         ("2026-09-25T16:59:59.000Z", order1["id"]))
            conn.execute("UPDATE orders SET created_at=? WHERE id=?",
                         ("2026-09-25T17:00:00.000Z", order2["id"]))
            conn.commit()

        self.assertEqual(self.store.daily_report(date(2026, 9, 25)), {
            "date": "2026-09-25", "topups": 20_000, "revenue": 10_000, "orders": 1,
        })
        self.assertEqual(self.store.daily_report(date(2026, 9, 26)), {
            "date": "2026-09-26", "topups": 30_000, "revenue": 10_000, "orders": 1,
        })

    def test_archive_keeps_orders_stock_and_original_order_name(self):
        product_id = self.store.add_product("Original name", 10_000, "Original description")
        sold_stock_id = self.store.add_stock(product_id, "sold-login:password")
        unsold_stock_id = self.store.add_stock(product_id, "unsold-login:password")
        process_sepay_payload(self.store, payload(1001, self.topup["code"]), ACCOUNT)
        order = self.store.purchase(1001, product_id)

        self.store.update_product_details(product_id, "New name", "New description")
        self.store.set_deleted(product_id, True)

        self.assertNotIn(product_id, [p["id"] for p in self.store.list_products(include_inactive=True)])
        self.assertEqual(self.store.list_orders(1001)[0]["name"], "Original name")
        self.assertEqual(self.store.get_order(1001, order["id"])["secret"], "sold-login:password")
        self.assertEqual(self.store.get_user_summary(1001)["total_spent"], 10_000)
        self.assertEqual(self.store.list_all_orders()[0]["name"], "Original name")
        self.assertEqual(self.store.stock_counts(product_id), {"total": 2, "available": 1, "sold": 1})
        self.assertEqual(self.store.list_stock_items(product_id)[0]["id"], unsold_stock_id)
        self.assertIsNone(self.store.get_stock_item(sold_stock_id))
        self.assertEqual(self.store.get_stock_item(unsold_stock_id)["secret"], "unsold-login:password")
        with self.assertRaises(NotFound):
            self.store.purchase(1001, product_id)

        self.store.set_deleted(product_id, False)
        self.assertFalse(self.store.get_product(product_id)["active"])
        self.store.set_active(product_id, True)
        self.assertEqual(self.store.purchase(1001, product_id)["secret"], "unsold-login:password")

    def test_admin_views_show_actual_credits_and_buyers(self):
        product_id = self.store.add_product("Test account", 10_000)
        self.store.add_stock(product_id, "login:password")
        process_sepay_payload(
            self.store, payload(1002, self.topup["code"], amount=30_000), ACCOUNT
        )
        self.store.purchase(1001, product_id)

        self.assertEqual(self.store.get_user_summary(1001)["total_received"], 30_000)
        self.assertEqual(self.store.list_users()[0]["order_count"], 1)
        self.assertEqual(self.store.list_all_orders()[0]["user_id"], 1001)
        self.assertEqual(self.store.list_all_topups()[0]["paid_amount"], 30_000)
        self.assertEqual(self.store.list_bank_events()[0]["status"], "credited")
        self.assertEqual(self.store.list_bank_events()[0]["user_id"], 1001)

    def test_existing_database_migrates_without_losing_order_names(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript("""
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY, balance INTEGER, created_at TEXT
                );
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY, name TEXT, description TEXT, price INTEGER,
                    active INTEGER, created_at TEXT
                );
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER,
                    stock_id INTEGER, price INTEGER, created_at TEXT
                );
                INSERT INTO users VALUES (1001, 0, '2026-01-01');
                INSERT INTO products VALUES (1, 'Old name', '', 10000, 1, '2026-01-01');
                INSERT INTO orders VALUES (1, 1001, 1, 1, 10000, '2026-01-01');
            """)

        migrated = Store(legacy_path, self.encryption_key)
        self.assertEqual(migrated.get_product(1)["deleted"], 0)
        self.assertTrue(migrated.stock_notifications_enabled(1001))
        migrated.ensure_user(1001, "alice", "Alice")
        self.assertEqual(migrated.get_user_summary(1001)["username"], "alice")
        migrated.update_product_details(1, name="New name")
        self.assertEqual(migrated.list_all_orders()[0]["name"], "Old name")
        Store(legacy_path, self.encryption_key)
        self.assertEqual(migrated.list_all_orders()[0]["name"], "Old name")
        with closing(sqlite3.connect(legacy_path)) as conn:
            self.assertIn("sale_id", [row[1] for row in conn.execute("PRAGMA table_info(orders)")])

    def test_existing_stock_migrates_to_allow_duplicates_without_losing_orders(self):
        legacy_path = Path(self.temp.name) / "legacy-stock.sqlite3"
        old_schema = SCHEMA.replace(
            "secret_hash TEXT NOT NULL,", "secret_hash TEXT NOT NULL UNIQUE,", 1,
        )
        secret = "legacy-login:password"
        ciphertext = Fernet(self.encryption_key.encode("ascii")).encrypt(
            secret.encode("utf-8")
        ).decode("ascii")
        hash_key = hashlib.sha256(
            b"stock-dedupe:" + self.encryption_key.encode("ascii")
        ).digest()
        secret_hash = hmac.new(hash_key, secret.encode("utf-8"), hashlib.sha256).hexdigest()
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript(old_schema)
            conn.execute("INSERT INTO users(telegram_id) VALUES (1001)")
            conn.execute("INSERT INTO products(id, name, price) VALUES (1, 'Legacy', 10000)")
            conn.execute(
                """INSERT INTO stock(id, product_id, secret_ciphertext, secret_hash, sold_at)
                   VALUES (7, 1, ?, ?, '2026-01-01T00:00:00Z')""",
                (ciphertext, secret_hash),
            )
            conn.execute(
                """INSERT INTO orders(id, user_id, product_id, stock_id, price, product_name)
                   VALUES (8, 1001, 1, 7, 10000, 'Legacy')"""
            )
            conn.commit()

        migrated = Store(legacy_path, self.encryption_key)
        self.assertEqual(migrated.get_order(1001, 8)["secret"], secret)
        result = migrated.bulk_add_stock(1, [secret, secret])
        self.assertEqual(len(result["ids"]), 2)
        self.assertEqual(result["duplicate_lines"], [1, 2])
        self.assertEqual(migrated.stock_counts(1),
                         {"total": 3, "available": 2, "sold": 1})
        Store(legacy_path, self.encryption_key)
        with closing(sqlite3.connect(legacy_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_existing_stock_announcements_gain_batch_quantity(self):
        legacy_path = Path(self.temp.name) / "legacy-announcements.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript("""
                CREATE TABLE stock_announcement_batches (
                    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                INSERT INTO stock_announcement_batches
                    VALUES (1, 1, '2026-01-01T00:00:00Z');
            """)

        migrated = Store(legacy_path, self.encryption_key)
        Store(legacy_path, self.encryption_key)
        with closing(sqlite3.connect(legacy_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT added_count FROM stock_announcement_batches WHERE id=1"
                ).fetchone()[0],
                0,
            )

        migrated.ensure_user(1001)
        product_id = migrated.add_product("New product", 10_000)
        migrated.set_product_following(1001, product_id, True)
        result = migrated.bulk_add_stock(
            product_id, ["stock-1", "stock-2"], announce_except=frozenset(),
        )
        self.assertEqual(
            migrated.list_due_stock_announcements()[0]["added_count"], 2,
        )
        self.assertIsNotNone(result["announcement_id"])

    def _try_purchase(self, user_id, product_id):
        try:
            order = self.store.purchase(user_id, product_id)
            return "bought", user_id, order["id"]
        except OutOfStock:
            return "out_of_stock", user_id, None

    def test_http_webhook_requires_valid_signature_and_acks_duplicate(self):
        config = Config(
            bot_token="test", admin_ids=frozenset({1}), bank_name="Test",
            bank_bin="", bank_account_number=ACCOUNT, bank_account_holder="Test",
            sepay_webhook_secret="test-secret-long-enough-to-be-safe",
            inventory_encryption_key=self.encryption_key,
            database_path=Path(self.temp.name) / "shop.sqlite3",
            webhook_host="127.0.0.1", webhook_port=0, min_topup=10_000,
            max_topup=100_000_000, support_contact="",
        )
        notifications = []
        server, thread = start_webhook_server(config, self.store, notifications.append)
        try:
            data = json.dumps(payload(901, self.topup["code"])).encode()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/webhooks/sepay", data, {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            self.assertEqual(self.store.get_balance(1001), 0)

            timestamp = str(int(time.time()))
            signature = "sha256=" + hmac.new(
                config.sepay_webhook_secret.encode(), timestamp.encode() + b"." + data,
                hashlib.sha256,
            ).hexdigest()
            signed_headers = {
                "Content-Type": "application/json",
                "X-SePay-Timestamp": timestamp,
                "X-SePay-Signature": signature,
            }
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/webhooks/sepay", data + b" ", signed_headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            self.assertEqual(self.store.get_balance(1001), 0)

            old_timestamp = str(int(time.time()) - 301)
            old_signature = "sha256=" + hmac.new(
                config.sepay_webhook_secret.encode(), old_timestamp.encode() + b"." + data,
                hashlib.sha256,
            ).hexdigest()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/webhooks/sepay", data, {
                "X-SePay-Timestamp": old_timestamp,
                "X-SePay-Signature": old_signature,
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()
            self.assertEqual(self.store.get_balance(1001), 0)

            for _ in range(2):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/webhooks/sepay", data, signed_headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["success"], True)
                connection.close()
            self.assertEqual(self.store.get_balance(1001), 50_000)
            self.assertEqual(len(notifications), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_slow_webhook_body_times_out_without_blocking_health(self):
        config = SimpleNamespace(
            webhook_host="127.0.0.1", webhook_port=0,
            sepay_webhook_secret="test-secret-long-enough-to-be-safe",
            bank_account_number=ACCOUNT,
        )
        server, thread = start_webhook_server(config, self.store, lambda result: None)
        server.request_timeout = 0.2
        try:
            request = (
                "POST /webhooks/sepay HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Length: 10\r\n"
                f"X-SePay-Timestamp: {int(time.time())}\r\n"
                f"X-SePay-Signature: sha256={'0' * 64}\r\n"
                "Connection: close\r\n\r\n{}"
            ).encode("ascii")
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as client:
                client.sendall(request)
                response = client.recv(512)
            self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_webhook_rejects_overload_and_recovers(self):
        config = SimpleNamespace(
            webhook_host="127.0.0.1", webhook_port=0,
            sepay_webhook_secret="test-secret-long-enough-to-be-safe",
            bank_account_number=ACCOUNT,
        )
        alerts = []
        server, thread = start_webhook_server(config, self.store, lambda result: None, alerts.append)
        acquired = 0
        try:
            for _ in range(server.max_active_requests):
                self.assertTrue(server.active_requests.acquire(blocking=False))
                acquired += 1
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("GET", "/healthz")
                try:
                    response = connection.getresponse()
                except OSError:
                    pass  # Windows may reset a rejected connection before 503 arrives.
                else:
                    self.assertEqual(response.status, 503)
                    response.read()
            finally:
                connection.close()
            self.assertEqual(alerts, ["overload"])
            for _ in range(acquired):
                server.active_requests.release()
            acquired = 0
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        finally:
            for _ in range(acquired):
                server.active_requests.release()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
