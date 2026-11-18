from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sqlite3
import unittest

from cryptography.fernet import Fernet

from backup_shop import backup_and_verify, inspect_database
import backup_shop
from store import Store


class BackupTests(unittest.TestCase):
    def test_backup_failure_notifies_admin_and_keeps_failure_exit(self):
        with TemporaryDirectory() as temp:
            with patch.object(backup_shop, "ROOT", Path(temp)), patch.object(
                backup_shop, "load_config", side_effect=FileNotFoundError("missing database")
            ), patch.object(backup_shop, "alert_backup_failure") as alert, patch.object(
                backup_shop.logging, "basicConfig"
            ), patch.object(backup_shop.logging, "exception"):
                with self.assertRaises(FileNotFoundError):
                    backup_shop.main()
        alert.assert_called_once_with("FileNotFoundError")

    def test_backup_alert_uses_shared_admin_notifier(self):
        with patch.object(backup_shop, "send_admin_message") as send:
            backup_shop.alert_backup_failure("DatabaseError")
        send.assert_called_once()
        self.assertIn("DatabaseError", send.call_args.args[0])

    def test_online_backup_restores_a_consistent_snapshot(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            key = Fernet.generate_key().decode("ascii")
            database = root / "shop.sqlite3"
            shop = Store(database, key)
            product_id = shop.add_product("Account", 10_000)
            shop.add_stock(product_id, "private-login:password")
            shop.ensure_user(1001)
            topup = shop.create_topup(1001, 20_000)
            shop.record_bank_event(
                "backup-test-transaction", "0123456789", 20_000, topup["code"],
                "in", topup["code"], "0123456789",
            )

            backup = backup_and_verify(database, root / "backups", key)
            shop.purchase(1001, product_id)

            self.assertTrue(backup.is_file())
            self.assertEqual(inspect_database(backup, key)["orders"], 0)
            with closing(sqlite3.connect(backup)) as conn:
                self.assertEqual(conn.execute("SELECT balance FROM users WHERE telegram_id=1001").fetchone()[0], 20_000)
            self.assertEqual(shop.get_balance(1001), 10_000)


if __name__ == "__main__":
    unittest.main()
