from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import SHOP_TIMEZONE, Store


class OfferAdminUiTests(IsolatedAsyncioTestCase):
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
        self.message = SimpleNamespace(text="", reply_text=AsyncMock())
        self.query = SimpleNamespace(answer=AsyncMock(), data="", message=self.message)
        self.update = SimpleNamespace(
            message=self.message, callback_query=self.query,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def tap(self, data: str):
        self.query.data = data
        await self.shop.callbacks(self.update, self.context)

    async def send(self, value: str):
        self.message.text = value
        await self.shop.text_message(self.update, self.context)

    async def test_coupon_buttons_create_edit_and_toggle_once(self):
        self.update.effective_user.id = 1001
        await self.tap("admin:offer:coupon:new")
        self.assertNotIn("admin_offer_flow", self.context.user_data)
        self.update.effective_user.id = 1

        await self.tap("admin:offer:coupon:new")
        await self.send("WELCOME")
        await self.send("10%")
        await self.send("100")
        token = self.context.user_data["admin_offer_flow"]["token"]
        await self.tap(f"admin:offer:c:all:{token}")
        await self.tap(f"admin:offer:c:none:{token}")
        await self.tap(f"admin:offer:confirm:{token}")
        coupon = self.shop.store.list_coupons()[0]
        self.assertEqual((coupon["code"], coupon["value"], coupon["max_uses"]),
                         ("WELCOME", 10, 100))
        self.assertIsNone(coupon["product_id"])
        await self.tap(f"admin:offer:confirm:{token}")
        self.assertEqual(len(self.shop.store.list_coupons()), 1)

        await self.tap("admin:offer:coupon:toggle:WELCOME")
        self.assertFalse(self.shop.store.list_coupons()[0]["active"])
        await self.tap("admin:offer:coupon:edit:WELCOME:discount")
        await self.send("5000")
        coupon = self.shop.store.list_coupons()[0]
        self.assertEqual((coupon["kind"], coupon["value"]), ("fixed", 5000))
        await self.tap("admin:offer:coupon:edit:WELCOME:uses")
        await self.send("25")
        self.assertEqual(self.shop.store.list_coupons()[0]["max_uses"], 25)

    async def test_coupon_scope_and_expiry_buttons(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        await self.tap("admin:offer:coupon:new")
        await self.send("ONLYONE")
        await self.send("10%")
        await self.send("5")
        token = self.context.user_data["admin_offer_flow"]["token"]
        await self.tap(f"admin:offer:c:pick:{product_id}:{token}")
        await self.tap(f"admin:offer:c:date:{token}")
        expires = (datetime.now(SHOP_TIMEZONE) + timedelta(days=2)).date().isoformat()
        await self.send(expires)
        await self.tap(f"admin:offer:confirm:{token}")
        coupon = self.shop.store.list_coupons()[0]
        self.assertEqual((coupon["product_id"], coupon["expires_on"]), (product_id, expires))
        await self.tap("admin:offer:coupon:edit:ONLYONE:expiry")
        await self.send("-")
        self.assertIsNone(self.shop.store.list_coupons()[0]["expires_on"])

    async def test_old_buttons_cannot_cancel_or_confirm_new_operation(self):
        with patch("bot.secrets.token_hex", side_effect=["11111111", "22222222"]):
            await self.tap("admin:offer:coupon:new")
            old_token = self.context.user_data["admin_offer_flow"]["token"]
            await self.tap("admin:offer:coupon:new")
            current_token = self.context.user_data["admin_offer_flow"]["token"]
        await self.tap(f"admin:offer:cancel:{old_token}")
        self.assertEqual(self.context.user_data["admin_offer_flow"]["token"], current_token)
        await self.tap(f"admin:offer:confirm:{old_token}")
        self.assertEqual(self.context.user_data["admin_offer_flow"]["token"], current_token)
        await self.tap(f"admin:offer:cancel:{current_token}")
        self.assertNotIn("admin_offer_flow", self.context.user_data)
        self.assertEqual(self.shop.store.list_coupons(), [])

    async def test_bundle_buttons_create_edit_archive_and_restore(self):
        products = [self.shop.store.add_product(f"Account {number}", 20_000)
                    for number in range(1, 10)]
        self.shop.store.add_stock(products[0], "stock-first")
        self.shop.store.add_stock(products[-1], "stock-last")
        await self.tap("admin:offer:bundle:new")
        await self.send("Combo")
        token = self.context.user_data["admin_offer_flow"]["token"]
        await self.tap(f"admin:offer:b:page:1:{token}")
        await self.tap(f"admin:offer:b:add:{products[-1]}:{token}")
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn(f"admin:offer:b:remove:{products[-1]}:{token}", buttons)
        await self.tap(f"admin:offer:b:page:0:{token}")
        await self.tap(f"admin:offer:b:add:{products[0]}:{token}")
        await self.tap(f"admin:offer:b:next:{token}")
        await self.send("30k")
        await self.tap(f"admin:offer:b:skip:{token}")
        await self.tap(f"admin:offer:confirm:{token}")
        bundle = self.shop.store.get_bundle(1)
        self.assertEqual(bundle["price"], 30_000)
        self.assertEqual({item["product_id"]: item["quantity"] for item in bundle["items"]},
                         {products[0]: 1, products[-1]: 1})

        await self.tap("admin:offer:bundle:edit:1:price")
        await self.send("25k")
        self.assertEqual(self.shop.store.get_bundle(1)["price"], 25_000)
        await self.tap("admin:offer:bundle:toggle:1")
        self.assertFalse(self.shop.store.get_bundle(1)["active"])
        await self.tap("admin:offer:bundle:archive:1")
        self.assertFalse(self.shop.store.get_bundle(1)["deleted"])
        archive_token = self.context.user_data["admin_offer_flow"]["token"]
        await self.tap(f"admin:offer:confirm:{archive_token}")
        self.assertTrue(self.shop.store.get_bundle(1)["deleted"])
        await self.tap("admin:offer:bundle:restore:1")
        self.assertFalse(self.shop.store.get_bundle(1)["deleted"])
        self.assertFalse(self.shop.store.get_bundle(1)["active"])
