from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import Store


class ShareLinksTests(IsolatedAsyncioTestCase):
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
        self.shop.bot_username = "example_shop_bot"
        self.message = SimpleNamespace(reply_text=AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def open_shared_link(self, share_button, kind: str, item_id: int,
                               receiver_id: int):
        share_url = urlparse(share_button.url)
        self.assertEqual((share_url.scheme, share_url.netloc, share_url.path),
                         ("https", "t.me", "/share/url"))
        deep_link = parse_qs(share_url.query)["url"][0]
        self.assertEqual(deep_link,
                         f"https://t.me/example_shop_bot?start={kind}_{item_id}")
        payload = parse_qs(urlparse(deep_link).query)["start"][0]
        update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=receiver_id, username="friend",
                                            full_name="Friend"),
        )
        await self.shop.start(update, SimpleNamespace(args=[payload], user_data={}))
        return self.message.reply_text.await_args

    async def test_product_share_opens_current_detail_and_stops_when_hidden(self):
        product_id = self.shop.store.add_product("Tài khoản thử", 20_000)
        self.shop.store.add_stock(product_id, "private-login:secret")
        self.shop.store.ensure_user(1001)
        query = SimpleNamespace(answer=AsyncMock(), data=f"product:{product_id}",
                                message=self.message)
        await self.shop.callbacks(SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        ), SimpleNamespace(user_data={}))
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        share = next(button for button in buttons if button.text == "📤 Chia sẻ")
        self.assertNotIn("private-login", share.url)

        response = await self.open_shared_link(share, "p", product_id, 1002)
        self.assertIn("Tài khoản thử", response.args[0])
        self.assertIn("20.000đ", response.args[0])
        self.assertTrue(any(button.callback_data == f"buyqty:{product_id}"
                            for row in response.kwargs["reply_markup"].inline_keyboard
                            for button in row))

        self.shop.store.set_active(product_id, False)
        response = await self.open_shared_link(share, "p", product_id, 1003)
        self.assertIn("Sản phẩm không còn bán", response.args[0])
        self.assertFalse(any(button.callback_data == f"buyqty:{product_id}"
                             for row in response.kwargs["reply_markup"].inline_keyboard
                             for button in row))

    async def test_bundle_share_opens_current_detail_in_receivers_language(self):
        first = self.shop.store.add_product("Account A", 40_000)
        second = self.shop.store.add_product("Account B", 30_000)
        self.shop.store.add_stock(first, "secret-a")
        self.shop.store.add_stock(second, "secret-b")
        bundle_id = self.shop.store.create_bundle(
            "Combo", 55_000, {first: 1, second: 1}, "Two accounts",
        )
        await self.shop.send_bundle_detail(self.message, 1001, bundle_id)
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        share = next(button for button in buttons if button.text == "📤 Chia sẻ")
        self.shop.store.set_user_language(1002, "en")
        response = await self.open_shared_link(share, "b", bundle_id, 1002)
        self.assertIn("Bundle price: 55.000đ", response.args[0])
        self.assertTrue(any(button.callback_data == f"buybundle:{bundle_id}"
                            for row in response.kwargs["reply_markup"].inline_keyboard
                            for button in row))
        self.assertTrue(any(button.text == "📤 Share"
                            for row in response.kwargs["reply_markup"].inline_keyboard
                            for button in row))

    async def test_admin_can_create_and_share_product_post_with_current_sale(self):
        product_id = self.shop.store.add_product(
            "Tài khoản thử", 50_000, "Dùng trong 30 ngày",
        )
        self.shop.store.add_stock(product_id, "private-login:secret")
        self.shop.store.set_requires_email(product_id, True)
        self.shop.store.start_sale(product_id, 40_000, 1)

        await self.shop.send_admin_product(self.message, product_id)
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == f"admin:postproduct:{product_id}"
                            for button in buttons))

        admin = SimpleNamespace(
            callback_query=SimpleNamespace(
                answer=AsyncMock(), data=f"admin:postproduct:{product_id}",
                message=self.message,
            ),
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.callbacks(admin, SimpleNamespace(user_data={}))
        response = self.message.reply_text.await_args
        post = response.args[0]
        deep_link = f"https://t.me/example_shop_bot?start=p_{product_id}"
        self.assertIn("Tài khoản thử", post)
        self.assertIn("Dùng trong 30 ngày", post)
        self.assertIn("Giá gốc: <s>50.000đ</s>", post)
        self.assertIn("Giá sale: 40.000đ", post)
        self.assertEqual(response.kwargs["parse_mode"], "HTML")
        self.assertIn("Cần nhập email", post)
        self.assertIn(deep_link, post)
        self.assertNotIn("private-login", post)
        share = next(button for row in response.kwargs["reply_markup"].inline_keyboard
                     for button in row if button.text == "📤 Chia sẻ vào nhóm")
        query = parse_qs(urlparse(share.url).query)
        self.assertEqual(query["url"], [deep_link])
        self.assertIn("Giá sale: 40.000đ", query["text"][0])
        self.assertNotIn("private-login", share.url)

        self.shop.store.end_sale(product_id)
        await self.shop.send_admin_product_post(self.message, product_id)
        current_post = self.message.reply_text.await_args.args[0]
        self.assertIn("Giá: 50.000đ", current_post)
        self.assertNotIn("Giá sale:", current_post)

    async def test_product_post_rejects_unavailable_items_and_non_admins(self):
        product_id = self.shop.store.add_product("Account", 20_000)
        await self.shop.send_admin_product_post(self.message, product_id)
        self.assertIn("còn hàng", self.message.reply_text.await_args.args[0])
        self.shop.store.add_stock(product_id, "secret")
        self.shop.store.set_active(product_id, False)
        await self.shop.send_admin_product_post(self.message, product_id)
        self.assertIn("đang bán", self.message.reply_text.await_args.args[0])

        self.shop.store.set_active(product_id, True)
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1002),
        )
        await self.shop.admin_post_product(
            update, SimpleNamespace(args=[str(product_id)]),
        )
        self.assertIn("chỉ dành cho quản trị viên",
                      self.message.reply_text.await_args.args[0])
        self.message.reply_text.reset_mock()
        query = SimpleNamespace(answer=AsyncMock(),
                                data=f"admin:postproduct:{product_id}",
                                message=self.message)
        await self.shop.callbacks(SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1002),
        ), SimpleNamespace(user_data={}))
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])

    async def test_lower_product_price_is_struck_in_customer_views_and_post(self):
        product_id = self.shop.store.add_product(
            "A <B> & C", 50_000, "Good < 3",
        )
        self.shop.store.add_stock(product_id, "secret-login")
        self.shop.store.ensure_user(1001)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.set_price(product_id, 40_000)

        await self.shop.send_shop(self.message, 0)
        catalog = self.message.reply_text.await_args
        self.assertIn("Chạm vào sản phẩm", catalog.args[0])
        product_button = next(
            button for row in catalog.kwargs["reply_markup"].inline_keyboard
            for button in row if button.callback_data == f"product:{product_id}"
        )
        self.assertIn("A <B> & C", product_button.text)
        self.assertIn("40.000đ", product_button.text)

        await self.shop.send_product_detail(self.message, 1001, product_id)
        detail = self.message.reply_text.await_args
        self.assertIn("Giá: <s>50.000đ</s> 40.000đ", detail.args[0])
        self.assertIn("Good &lt; 3", detail.args[0])
        self.assertEqual(detail.kwargs["parse_mode"], "HTML")

        await self.shop.send_following(self.message, 1001)
        self.assertIn("<s>50.000đ</s> 40.000đ",
                      self.message.reply_text.await_args.args[0])

        await self.shop.send_admin_product_post(self.message, product_id)
        post = self.message.reply_text.await_args
        self.assertIn("Giá cũ: <s>50.000đ</s>", post.args[0])
        self.assertIn("Giá mới: 40.000đ", post.args[0])
        self.assertNotIn("secret-login", post.args[0])
        share = next(button for row in post.kwargs["reply_markup"].inline_keyboard
                     for button in row if button.text == "📤 Chia sẻ vào nhóm")
        shared_text = parse_qs(urlparse(share.url).query)["text"][0]
        self.assertIn("Giá cũ: 50.000đ", shared_text)
        self.assertNotIn("<s>", shared_text)

        self.shop.store.start_sale(product_id, 30_000, 1)
        await self.shop.send_product_detail(self.message, 1001, product_id)
        sale_detail = self.message.reply_text.await_args.args[0]
        self.assertIn("Giá gốc: <s>40.000đ</s>", sale_detail)
        self.assertNotIn("50.000đ", sale_detail)
        self.shop.store.end_sale(product_id)

        self.shop.store.set_price(product_id, 60_000)
        await self.shop.send_shop(self.message, 0)
        raised_catalog = self.message.reply_text.await_args
        raised_button = next(
            button for row in raised_catalog.kwargs["reply_markup"].inline_keyboard
            for button in row if button.callback_data == f"product:{product_id}"
        )
        self.assertIn("60.000đ", raised_button.text)
        self.assertNotIn("<s>", raised_button.text)

    async def test_bundle_price_drop_is_struck_and_raise_is_not(self):
        first = self.shop.store.add_product("A & B", 60_000)
        second = self.shop.store.add_product("C < D", 40_000)
        self.shop.store.add_stock(first, "secret-a")
        self.shop.store.add_stock(second, "secret-b")
        bundle_id = self.shop.store.create_bundle(
            "Combo <VIP>", 80_000, {first: 1, second: 1}, "Two & more",
        )
        self.shop.store.update_bundle(bundle_id, price=70_000)

        await self.shop.send_bundles(self.message, 0)
        catalog = self.message.reply_text.await_args
        self.assertIn("<s>80.000đ</s> 70.000đ", catalog.args[0])
        self.assertIn("Combo &lt;VIP&gt;", catalog.args[0])
        self.assertEqual(catalog.kwargs["parse_mode"], "HTML")

        await self.shop.send_bundle_detail(self.message, 1001, bundle_id)
        detail = self.message.reply_text.await_args
        self.assertIn("Giá gói: <s>80.000đ</s> 70.000đ", detail.args[0])
        self.assertIn("Two &amp; more", detail.args[0])
        self.assertIn("A &amp; B", detail.args[0])

        self.shop.store.update_bundle(bundle_id, price=85_000)
        await self.shop.send_bundles(self.message, 0)
        raised_catalog = self.message.reply_text.await_args.args[0]
        self.assertIn("85.000đ", raised_catalog)
        self.assertNotIn("<s>", raised_catalog)
