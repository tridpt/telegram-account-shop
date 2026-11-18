from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import Store


class SoldStockTests(IsolatedAsyncioTestCase):
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
        topup = self.shop.store.create_topup(1001, 100_000)
        self.shop.store.record_bank_event(
            "sold-stock-test", "0123456789", 100_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.query = SimpleNamespace(answer=AsyncMock(), data="", message=self.message)
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def tap(self, data: str, user_id: int = 1):
        self.query.data = data
        update = SimpleNamespace(
            callback_query=self.query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=user_id),
        )
        await self.shop.callbacks(update, self.context)

    async def test_admin_can_check_sold_account_of_archived_product(self):
        product_id = self.shop.store.add_product("Archived account", 10_000)
        sold_id = self.shop.store.add_stock(product_id, "sold-login:secret")
        self.shop.store.add_stock(product_id, "unsold-login:secret")
        self.shop.store.set_requires_email(product_id, True)
        order = self.shop.store.purchase(
            1001, product_id, customer_email="buyer@example.com",
        )
        self.shop.store.set_deleted(product_id, True)

        await self.tap(f"admin:product:{product_id}")
        buttons = [button.callback_data for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:sold:{product_id}:0", buttons)

        await self.tap(f"admin:sold:{product_id}:0")
        listing = self.message.reply_text.await_args
        self.assertIn(f"Kho #{sold_id} · đơn #{order['id']}", listing.args[0])
        self.assertIn("@buyer", listing.args[0])
        self.assertNotIn("sold-login:secret", listing.args[0])
        self.assertNotIn("unsold-login:secret", listing.args[0])

        await self.tap(f"admin:solditem:{sold_id}:0")
        detail = self.message.reply_text.await_args
        self.assertIn("sold-login:secret", detail.args[0])
        self.assertIn(f"Đơn #{order['id']}", detail.args[0])
        self.assertIn("buyer@example.com", detail.args[0])
        self.assertTrue(detail.kwargs["protect_content"])

        await self.tap(f"admin:order:{order['id']}")
        buttons = [button.callback_data for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:solditem:{sold_id}:0", buttons)

        self.message.reply_text.reset_mock()
        await self.tap(f"admin:solditem:{sold_id}:0", user_id=1002)
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])
        self.assertNotIn("sold-login:secret", self.message.reply_text.await_args.args[0])

    async def test_sold_account_list_has_pagination(self):
        product_id = self.shop.store.add_product("Slots", 10_000)
        self.shop.store.bulk_add_stock(product_id, [f"slot-{index}" for index in range(9)])
        for _ in range(9):
            self.shop.store.purchase(1001, product_id)

        await self.tap(f"admin:sold:{product_id}:0")
        first_buttons = [button.callback_data for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:sold:{product_id}:1", first_buttons)
        self.assertEqual(sum(value.startswith("admin:solditem:") for value in first_buttons), 8)

        await self.tap(f"admin:sold:{product_id}:1")
        second_buttons = [button.callback_data for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:sold:{product_id}:0", second_buttons)
        self.assertEqual(sum(value.startswith("admin:solditem:") for value in second_buttons), 1)
