from pathlib import Path
from contextlib import closing
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
import json
import sqlite3

from cryptography.fernet import Fernet

from bot import ShopBot, account_delivery_payload, order_delivery_file
from config import Config
from payments import process_sepay_payload
from store import Store


class FileProductTests(IsolatedAsyncioTestCase):
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
        self.shop.store.ensure_user(1001)
        self.message = SimpleNamespace(
            text="", document=None, reply_text=AsyncMock(), reply_photo=AsyncMock(),
            reply_document=AsyncMock(), delete=AsyncMock(),
        )
        self.query = SimpleNamespace(answer=AsyncMock(), data="", message=self.message)
        self.update = SimpleNamespace(
            message=self.message, callback_query=self.query,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        self.context = SimpleNamespace(user_data={}, bot=self.bot)
        self.shop.application = SimpleNamespace(bot=self.bot)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def tap(self, data, user_id=1001):
        self.update.effective_user.id = user_id
        self.query.data = data
        await self.shop.callbacks(self.update, self.context)

    async def test_admin_creates_file_product_and_uploads_utf8_json(self):
        await self.shop.send_admin_products(self.message)
        callbacks = [button.callback_data for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn("admin:catalog:newfile", callbacks)
        await self.tap("admin:catalog:newfile", user_id=1)
        self.assertEqual(self.context.user_data["admin_product_flow"]["delivery_type"], "file")
        for value in ("Ebook", "30000", "Nội dung bằng file"):
            await self.shop.admin_catalog_text(self.message, self.context, value)
        token = self.context.user_data["admin_product_flow"]["token"]
        await self.tap(f"admin:catalog:confirm:{token}", user_id=1)
        product = self.shop.store.list_products()[0]
        self.assertEqual(product["delivery_type"], "file")
        self.assertEqual(product["file_only_delivery"], 1)
        with self.assertRaises(ValueError):
            self.shop.store.set_file_only_delivery(product["id"], False)
        await self.tap(f"admin:catalog:stock:{product['id']}", user_id=1)
        self.assertEqual(self.context.user_data["awaiting_file_stock"], product["id"])
        content = '{"lesson":"Bài học số 1","password":"Mật khẩu riêng"}'.encode("utf-8")
        remote = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(content)))
        self.message.document = SimpleNamespace(
            file_name="lesson.json", file_size=len(content),
            get_file=AsyncMock(return_value=remote),
        )
        await self.shop.document_message(self.update, self.context)
        self.message.delete.assert_awaited_once()
        stock = self.shop.store.list_stock_items(product["id"], include_secret=True)
        self.assertEqual(stock[0]["secret"], content.decode("utf-8"))
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            encrypted = conn.execute("SELECT secret_ciphertext FROM stock WHERE id=?",
                                     (stock[0]["id"],)).fetchone()[0]
        self.assertNotIn("Mật khẩu riêng", encrypted)
        self.assertNotIn("Mật khẩu riêng", self.bot.send_message.await_args.args[1])
        self.assertEqual(self.context.user_data["awaiting_file_stock"], product["id"])
        self.message.reply_text.reset_mock()
        await self.shop.send_admin_inventory(self.message, product["id"])
        self.assertNotIn("Mật khẩu riêng", self.message.reply_text.await_args.args[0])
        self.assertIn(f"admin:stock:{stock[0]['id']}", [
            button.callback_data for row in self.message.reply_text.await_args.kwargs[
                "reply_markup"].inline_keyboard for button in row])
        await self.shop.send_admin_stock_item(self.message, stock[0]["id"])
        admin_document = self.message.reply_document.await_args.kwargs["document"]
        self.assertTrue(admin_document.filename.endswith(".json"))
        self.assertEqual(json.loads(admin_document.input_file_content)["password"],
                         "Mật khẩu riêng")

    async def test_file_product_rejects_wrong_type_and_unauthorized_upload(self):
        product = self.shop.store.add_product("File", 10_000, delivery_type="file")
        with self.assertRaises(ValueError):
            self.shop.store.add_stock(product, "plain account text")
        with self.assertRaises(ValueError):
            self.shop.store.add_file_stock(product, b"\xff\xfe")
        with self.assertRaises(ValueError):
            self.shop.store.add_file_stock(product, b"not json")
        with self.assertRaises(ValueError):
            self.shop.store.add_file_stock(product, b'{"number":NaN}')
        with self.assertRaises(ValueError):
            self.shop.store.add_file_stock(product, b"x" * 1_000_001)
        self.context.user_data["awaiting_file_stock"] = product
        self.message.document = SimpleNamespace(file_name="payload.txt", file_size=5)
        await self.shop.document_message(self.update, self.context)
        self.assertEqual(self.shop.store.stock_counts(product)["available"], 0)
        invalid = b'{"broken":}'
        remote = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(invalid)))
        self.message.document = SimpleNamespace(
            file_name="payload.json", file_size=len(invalid),
            get_file=AsyncMock(return_value=remote),
        )
        await self.shop.document_message(self.update, self.context)
        self.assertEqual(self.shop.store.stock_counts(product)["available"], 0)
        self.update.effective_user.id = 1
        self.message.document.file_name = "payload.pdf"
        await self.shop.document_message(self.update, self.context)
        self.assertEqual(self.shop.store.stock_counts(product)["available"], 0)

    async def test_mixed_order_is_valid_json_and_old_file_can_still_download(self):
        orders = [
            {"id": 10, "name": "Tài khoản", "delivery_type": "account",
             "file_only_delivery": 0, "secret": "login: private",
             "customer_email": None},
            {"id": 11, "name": "Tệp JSON", "delivery_type": "file",
             "file_only_delivery": 1, "secret": '{"key":"private-json"}',
             "customer_email": None},
        ]
        message, _, _ = account_delivery_payload(orders, "vi", "Đã thanh toán")
        self.assertNotIn("login: private", message)
        self.assertNotIn("private-json", message)
        contents, extension = order_delivery_file(orders, "vi")
        self.assertEqual(extension, ".json")
        parsed = json.loads(contents)
        self.assertEqual(parsed["items"][0]["content"], "login: private")
        self.assertEqual(parsed["items"][1]["content"], {"key": "private-json"})
        orders[1]["secret"] = "old text file"
        _, extension = order_delivery_file(orders, "vi")
        self.assertEqual(extension, ".txt")

    async def test_wallet_file_delivery_never_puts_contents_in_chat(self):
        product = self.shop.store.add_product("Ebook", 10_000, delivery_type="file")
        self.shop.store.add_file_stock(product, b'{"account":"TOP-SECRET","line":"first"}')
        topup = self.shop.store.create_topup(1001, 10_000)
        process_sepay_payload(self.shop.store, {
            "id": 501, "accountNumber": "0123456789", "transferAmount": 10_000,
            "transferType": "in", "content": topup["code"], "code": None,
        }, "0123456789")
        await self.tap(f"buyqty:{product}:1")
        token = self.context.user_data["product_checkout"]["token"]
        await self.tap(f"buyflow:wallet:{token}")
        self.assertEqual(self.shop.store.get_balance(1001), 0)
        self.assertNotIn("TOP-SECRET", "\n".join(str(call.args) for call in
                                               self.message.reply_text.await_args_list))
        delivery = self.message.reply_document.await_args.kwargs["document"]
        self.assertTrue(delivery.filename.endswith(".json"))
        self.assertEqual(delivery.input_file_content,
                         b'{"account":"TOP-SECRET","line":"first"}')
        self.assertEqual(json.loads(delivery.input_file_content)["account"], "TOP-SECRET")
        purchase = self.shop.store.list_purchases(1001)[0]
        self.assertEqual(purchase["items"][0]["delivery_type"], "file")
        self.message.reply_text.reset_mock()
        await self.tap(f"copyorder:{purchase['id']}")
        self.assertNotIn("TOP-SECRET", self.message.reply_text.await_args.args[0])
        self.message.reply_document.reset_mock()
        await self.tap(f"orderfile:{purchase['id']}")
        self.message.reply_document.assert_awaited_once()
        self.assertEqual(json.loads(self.message.reply_document.await_args.kwargs[
            "document"].input_file_content)["account"], "TOP-SECRET")
        self.assertIsNone(self.shop.store.get_purchase(9999, purchase["id"]))

    async def test_multiple_file_units_are_delivered_in_one_json(self):
        product = self.shop.store.add_product("Ebook", 10_000, delivery_type="file")
        self.shop.store.add_file_stock(product, b'{"account":"FIRST-PRIVATE-FILE"}')
        self.shop.store.add_file_stock(product, b'{"account":"SECOND-PRIVATE-FILE"}')
        topup = self.shop.store.create_topup(1001, 20_000)
        process_sepay_payload(self.shop.store, {
            "id": 503, "accountNumber": "0123456789", "transferAmount": 20_000,
            "transferType": "in", "content": topup["code"], "code": None,
        }, "0123456789")
        await self.tap(f"buyqty:{product}:2")
        token = self.context.user_data["product_checkout"]["token"]
        await self.tap(f"buyflow:wallet:{token}")
        self.message.reply_document.assert_awaited_once()
        document = self.message.reply_document.await_args.kwargs["document"]
        self.assertTrue(document.filename.endswith(".json"))
        payload = json.loads(document.input_file_content)
        self.assertEqual([item["content"]["account"] for item in payload["items"]],
                         ["FIRST-PRIVATE-FILE", "SECOND-PRIVATE-FILE"])
        self.assertNotIn("FIRST-PRIVATE-FILE", "\n".join(
            str(call.args) for call in self.message.reply_text.await_args_list))
        self.assertNotIn("SECOND-PRIVATE-FILE", "\n".join(
            str(call.args) for call in self.message.reply_text.await_args_list))
        purchases = self.shop.store.list_purchases(1001)
        self.assertEqual(len(purchases), 1)
        self.assertEqual(len(purchases[0]["items"]), 2)

    async def test_bank_payment_sends_file_without_contents_in_message(self):
        product = self.shop.store.add_product("Ebook", 20_000, delivery_type="file")
        self.shop.store.add_file_stock(product, b'{"account":"PRIVATE FILE CONTENT"}')
        await self.tap(f"buyqty:{product}:1")
        token = self.context.user_data["product_checkout"]["token"]
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.tap(f"buyflow:bank:{token}")
        checkout = self.shop.store.recent_direct_checkouts(1001)[0]
        result = process_sepay_payload(self.shop.store, {
            "id": 502, "accountNumber": "0123456789", "transferAmount": 20_000,
            "transferType": "in", "content": checkout["code"], "code": None,
        }, "0123456789", notify_admin_ids=frozenset({1}))
        self.assertEqual(result["direct_status"], "fulfilled")
        await self.shop.send_pending_direct_checkout_orders(self.bot)
        self.assertNotIn("PRIVATE FILE CONTENT", "\n".join(str(call.args) for call in
                                                         self.bot.send_message.await_args_list))
        self.bot.send_document.assert_awaited_once()
        document = self.bot.send_document.await_args.kwargs["document"]
        self.assertTrue(document.filename.endswith(".json"))
        self.assertEqual(json.loads(document.input_file_content)["account"],
                         "PRIVATE FILE CONTENT")
        self.assertEqual(self.shop.store.list_due_direct_checkout_batches(), [])

    async def test_account_product_can_choose_file_only_and_keep_it_on_old_orders(self):
        product = self.shop.store.add_product("Tài khoản", 10_000)
        self.shop.store.add_stock(product, "PRIVATE-ACCOUNT-ONE")
        self.shop.store.add_stock(product, "PRIVATE-ACCOUNT-TWO")
        await self.shop.send_admin_product(self.message, product)
        buttons = [button.callback_data for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:catalog:delivery:{product}:file", buttons)
        await self.tap(f"admin:catalog:delivery:{product}:file", user_id=1)
        self.assertEqual(self.shop.store.get_product(product)["file_only_delivery"], 1)

        topup = self.shop.store.create_topup(1001, 10_000)
        process_sepay_payload(self.shop.store, {
            "id": 504, "accountNumber": "0123456789", "transferAmount": 10_000,
            "transferType": "in", "content": topup["code"], "code": None,
        }, "0123456789")
        self.message.reply_text.reset_mock()
        await self.tap(f"buyqty:{product}:1")
        token = self.context.user_data["product_checkout"]["token"]
        await self.tap(f"buyflow:wallet:{token}")
        self.assertNotIn("PRIVATE-ACCOUNT-ONE", "\n".join(
            str(call.args) for call in self.message.reply_text.await_args_list))
        self.assertIn("PRIVATE-ACCOUNT-ONE", self.message.reply_document.await_args.kwargs[
            "document"].input_file_content.decode("utf-8-sig"))
        purchase = self.shop.store.get_purchase(1001, self.shop.store.list_purchases(1001)[0]["id"])
        self.assertEqual(purchase["items"][0]["file_only_delivery"], 1)
        await self.tap(f"copyorder:{purchase['id']}")
        self.assertNotIn("PRIVATE-ACCOUNT-ONE", self.message.reply_text.await_args.args[0])

        await self.tap(f"admin:catalog:delivery:{product}:chat", user_id=1)
        self.assertEqual(self.shop.store.get_product(product)["file_only_delivery"], 0)
        self.message.reply_text.reset_mock()
        await self.shop.send_purchase_detail(self.message, 1001, purchase["id"])
        self.assertNotIn("PRIVATE-ACCOUNT-ONE", "\n".join(
            str(call.args) for call in self.message.reply_text.await_args_list))

    async def test_bank_account_file_only_delivery_hides_details(self):
        product = self.shop.store.add_product("Tài khoản", 20_000)
        self.shop.store.add_stock(product, "PRIVATE-BANK-ACCOUNT")
        self.shop.store.set_file_only_delivery(product, True)
        await self.tap(f"buyqty:{product}:1")
        token = self.context.user_data["product_checkout"]["token"]
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.tap(f"buyflow:bank:{token}")
        checkout = self.shop.store.recent_direct_checkouts(1001)[0]
        process_sepay_payload(self.shop.store, {
            "id": 505, "accountNumber": "0123456789", "transferAmount": 20_000,
            "transferType": "in", "content": checkout["code"], "code": None,
        }, "0123456789")
        await self.shop.send_pending_direct_checkout_orders(self.bot)
        self.assertNotIn("PRIVATE-BANK-ACCOUNT", "\n".join(
            str(call.args) for call in self.bot.send_message.await_args_list))
        self.assertIn("PRIVATE-BANK-ACCOUNT", self.bot.send_document.await_args.kwargs[
            "document"].input_file_content.decode("utf-8-sig"))
