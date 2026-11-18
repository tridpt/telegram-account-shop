from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import Store


class AdminAuditStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.store = Store(self.path, Fernet.generate_key().decode("ascii"))

    def tearDown(self):
        self.temp.cleanup()

    def test_audit_rows_are_ordered_and_details_are_sanitized(self):
        self.store.ensure_user(1, "owner", "Owner")
        first = self.store.log_admin_action(
            1, "ban_user", "user", 1001, "line 1\nline 2",
        )
        second = self.store.log_admin_action(
            1, "command", "command", "/audit",
        )
        rows = self.store.list_admin_audit()
        self.assertEqual([row["id"] for row in rows], [second, first])
        self.assertEqual(rows[1]["details"], "line 1 line 2")
        self.assertEqual(rows[1]["username"], "owner")
        exported = self.store.export_admin_audit_rows()
        self.assertEqual(len(exported), 2)


class AdminAuditBotTests(IsolatedAsyncioTestCase):
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
        self.shop.store.log_admin_action(1, "ban_user", "user", 1001)
        self.message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        self.query = SimpleNamespace(answer=AsyncMock(), message=self.message, data="admin:audit:0")
        self.update = SimpleNamespace(
            callback_query=self.query,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_admin_can_view_and_export_audit(self):
        await self.shop.callbacks(self.update, SimpleNamespace(user_data={}))
        self.assertIn("NHẬT KÝ QUẢN TRỊ", self.message.reply_text.await_args.args[0])
        buttons = [button.callback_data
                   for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:exportaudit", buttons)

        self.query.data = "admin:exportaudit"
        await self.shop.callbacks(self.update, SimpleNamespace(user_data={}))
        self.message.reply_document.assert_awaited_once()
        self.assertTrue(
            self.message.reply_document.await_args.kwargs["document"].filename.endswith(".csv")
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
