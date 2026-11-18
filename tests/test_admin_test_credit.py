from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import BalanceChanged, Store


class AdminTestCreditStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "shop.sqlite3",
                           Fernet.generate_key().decode("ascii"))
        self.store.ensure_user(1001, "tridpt1", "Admin")

    def tearDown(self):
        self.temp.cleanup()

    def test_credit_is_atomic_audited_and_not_a_bank_topup(self):
        self.assertEqual(self.store.find_users_by_username("@TRIDPT1")[0]["telegram_id"],
                         1001)
        result = self.store.credit_test_balance(1001, 500_000, 1001, 0,
                                                "test-request-1234")
        self.assertEqual(result["balance_after"], 500_000)
        self.assertFalse(result["already_applied"])
        self.assertEqual(self.store.get_balance(1001), 500_000)
        self.assertEqual(self.store.get_user_summary(1001)["total_received"], 0)
        self.assertEqual(self.store.get_user_summary(1001)["total_test_credit"], 500_000)
        self.assertEqual(self.store.list_test_credits(1001)[0]["admin_id"], 1001)
        replay = self.store.credit_test_balance(1001, 500_000, 1001, 0,
                                                "test-request-1234")
        self.assertTrue(replay["already_applied"])
        self.assertEqual(self.store.get_balance(1001), 500_000)
        self.assertEqual(len(self.store.list_test_credits(1001)), 1)

    def test_changed_balance_or_invalid_amount_cannot_credit(self):
        self.store.credit_test_balance(1001, 1_000, 1001, 0, "first-request-1234")
        with self.assertRaises(BalanceChanged):
            self.store.credit_test_balance(1001, 500_000, 1001, 0,
                                           "second-request-1234")
        for amount in (0, -1, 10_000_001):
            with self.assertRaises(ValueError):
                self.store.credit_test_balance(1001, amount, 1001, 1_000,
                                               "invalid-request-1234")
        self.assertEqual(self.store.get_balance(1001), 1_000)


class AdminTestCreditBotTests(IsolatedAsyncioTestCase):
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
        self.shop.store.ensure_user(1, "adminname", "Admin")
        self.shop.store.ensure_user(1001, "tridpt1", "Buyer")
        self.message = SimpleNamespace(reply_text=AsyncMock(), text="")
        self.query = SimpleNamespace(answer=AsyncMock(), message=self.message, data="")
        self.update = SimpleNamespace(
            callback_query=self.query, message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def tap(self, data):
        self.query.data = data
        await self.shop.callbacks(self.update, self.context)

    async def test_admin_can_find_username_preview_and_credit_once(self):
        await self.tap("admin:balancecredit:home")
        self.assertIn("CỘNG TIỀN THỬ NGHIỆM", self.message.reply_text.await_args.args[0])
        await self.tap("admin:balancecredit:lookup")
        self.assertTrue(self.context.user_data["awaiting_admin_credit_target"])
        await self.shop.lookup_admin_test_credit_target(self.message, self.context,
                                                        "@tridpt1")
        self.assertIn("ID 1001", self.message.reply_text.await_args.args[0])
        await self.tap("admin:balancecredit:amount:1001:500000")
        pending = self.context.user_data["pending_admin_test_credit"]
        self.assertIn("500.000đ", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_balance(1001), 0)
        await self.tap(f"admin:balancecredit:confirm:{pending[0]}")
        self.assertEqual(self.shop.store.get_balance(1001), 500_000)
        self.assertEqual(len(self.shop.store.list_test_credits(1001)), 1)
        await self.tap(f"admin:balancecredit:confirm:{pending[0]}")
        self.assertEqual(self.shop.store.get_balance(1001), 500_000)

    async def test_customer_cannot_open_admin_credit_controls(self):
        self.update.effective_user.id = 1001
        await self.tap("admin:balancecredit:amount:1001:500000")
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_balance(1001), 0)

    async def test_admin_balance_change_invalidates_preview(self):
        await self.tap("admin:balancecredit:amount:1001:500000")
        nonce = self.context.user_data["pending_admin_test_credit"][0]
        self.shop.store.credit_test_balance(1001, 1_000, 1, 0,
                                            "external-change-1234")
        await self.tap(f"admin:balancecredit:confirm:{nonce}")
        self.assertEqual(self.shop.store.get_balance(1001), 1_000)
        self.assertIn("Số dư đã đổi", self.message.reply_text.await_args_list[-2].args[0])


if __name__ == "__main__":
    import unittest
    unittest.main()
