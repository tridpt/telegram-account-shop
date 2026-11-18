from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from telegram.error import TelegramError

from bot import ShopBot
from config import Config
from payments import process_sepay_payload
from store import Store


class OrderMessagingTests(IsolatedAsyncioTestCase):
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
        for user_id in (1, 1001, 1002):
            self.shop.store.ensure_user(user_id)
        product_id = self.shop.store.add_product("Account", 10_000)
        self.product_id = product_id
        self.shop.store.add_stock(product_id, "private-account")
        topup = self.shop.store.create_topup(1001, 10_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 1, "accountNumber": "0123456789", "transferAmount": 10_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        purchase = self.shop.store.purchase_product_quantity(1001, product_id, 1, 10_000)
        self.purchase_id = purchase["orders"][0]["id"]
        self.message = SimpleNamespace(text="", caption=None, document=None,
                                       reply_text=AsyncMock())
        self.query = SimpleNamespace(answer=AsyncMock(), data="", message=self.message)
        self.update = SimpleNamespace(
            message=self.message, callback_query=self.query,
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(type="private"),
        )
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        self.context = SimpleNamespace(user_data={}, bot=self.bot)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def tap(self, data):
        self.query.data = data
        await self.shop.callbacks(self.update, self.context)

    async def test_admin_and_buyer_can_reply_to_one_purchase(self):
        await self.shop.send_admin_purchase(self.message, self.purchase_id)
        buttons = [button.callback_data for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:message:{self.purchase_id}", buttons)
        await self.tap(f"admin:message:{self.purchase_id}")
        self.assertIn("awaiting_admin_order_message", self.context.user_data)
        self.message.text = "Đơn của bạn đã sẵn sàng."
        await self.shop.text_message(self.update, self.context)
        sent = self.bot.send_message.await_args
        self.assertEqual(sent.args[0], 1001)
        self.assertIn(self.message.text, sent.args[1])
        reply_button = sent.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(reply_button.callback_data,
                         f"buyerreply:{self.purchase_id}:1")
        self.assertNotIn("awaiting_admin_order_message", self.context.user_data)

        contact = self.shop.store.get_admin_purchase(self.purchase_id)
        self.assertEqual(contact["admin_contact_count"], 1)
        self.assertIsNotNone(contact["admin_contact_last_sent_at"])

        self.update.effective_user.id = 1001
        self.context.user_data = {}
        await self.tap(reply_button.callback_data)
        self.message.text = "Cảm ơn shop, tôi đã nhận được."
        await self.shop.text_message(self.update, self.context)
        sent = self.bot.send_message.await_args
        self.assertEqual(sent.args[0], 1)
        self.assertIn(self.message.text, sent.args[1])
        self.assertEqual(sent.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                         f"admin:message:{self.purchase_id}")
        self.assertNotIn("awaiting_customer_order_reply", self.context.user_data)

    async def test_only_admin_and_purchase_owner_can_message(self):
        self.update.effective_user.id = 1002
        await self.tap(f"admin:message:{self.purchase_id}")
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])
        await self.tap(f"buyerreply:{self.purchase_id}:1")
        self.assertIn("Không tìm thấy đơn hàng", self.message.reply_text.await_args.args[0])
        self.assertNotIn("awaiting_customer_order_reply", self.context.user_data)
        self.bot.send_message.assert_not_awaited()

    async def test_failed_send_keeps_prompt_for_retry_and_cancel_clears_it(self):
        await self.tap(f"admin:message:{self.purchase_id}")
        self.message.text = "Hi"
        self.bot.send_message.side_effect = TelegramError("temporarily unavailable")
        await self.shop.text_message(self.update, self.context)
        self.assertIn("Chưa gửi được tin", self.message.reply_text.await_args.args[0])
        self.assertIn("awaiting_admin_order_message", self.context.user_data)
        self.assertEqual(self.shop.store.get_admin_purchase(self.purchase_id)[
            "admin_contact_count"], 0)
        self.bot.send_message.side_effect = None
        await self.shop.text_message(self.update, self.context)
        self.assertNotIn("awaiting_admin_order_message", self.context.user_data)
        await self.tap(f"admin:message:{self.purchase_id}")
        await self.shop.cancel(self.update, self.context)
        self.assertNotIn("awaiting_admin_order_message", self.context.user_data)

    async def test_admin_can_send_txt_and_json_to_purchase_owner(self):
        for filename, caption in (("huong-dan.txt", None),
                                  ("thong-tin.json", "Thông tin bổ sung")):
            with self.subTest(filename=filename):
                await self.tap(f"admin:message:{self.purchase_id}")
                self.assertIn("file dưới dạng tài liệu",
                              self.message.reply_text.await_args.args[0])
                self.message.document = SimpleNamespace(
                    file_id=f"telegram-file-{filename}", file_name=filename,
                )
                self.message.caption = caption
                await self.shop.document_message(self.update, self.context)
                sent = self.bot.send_document.await_args
                self.assertEqual(sent.args[0], 1001)
                self.assertEqual(sent.kwargs["document"], self.message.document.file_id)
                self.assertIn(f"ĐƠN #{self.purchase_id}", sent.kwargs["caption"])
                if caption:
                    self.assertIn(caption, sent.kwargs["caption"])
                self.assertEqual(
                    sent.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                    f"buyerreply:{self.purchase_id}:1",
                )
                self.assertNotIn("awaiting_admin_order_message", self.context.user_data)
                self.assertEqual(self.shop.store.get_admin_purchase(self.purchase_id)[
                    "admin_contact_count"], 1 if filename.endswith(".txt") else 2)
                self.bot.send_message.assert_not_awaited()
                self.bot.send_document.reset_mock()

    async def test_admin_file_send_failure_retry_and_expiry(self):
        await self.tap(f"admin:message:{self.purchase_id}")
        self.message.document = SimpleNamespace(file_id="file-id", file_name="note.txt")
        self.bot.send_document.side_effect = TelegramError("temporarily unavailable")
        await self.shop.document_message(self.update, self.context)
        self.assertIn("Chưa gửi được tin hoặc file",
                      self.message.reply_text.await_args.args[0])
        self.assertIn("awaiting_admin_order_message", self.context.user_data)
        self.assertEqual(self.shop.store.get_admin_purchase(self.purchase_id)[
            "admin_contact_count"], 0)
        self.bot.send_document.side_effect = None
        await self.shop.document_message(self.update, self.context)
        self.assertNotIn("awaiting_admin_order_message", self.context.user_data)
        self.bot.send_document.reset_mock()

        await self.tap(f"admin:message:{self.purchase_id}")
        self.context.user_data["awaiting_admin_order_message"] = (
            self.purchase_id, time.monotonic() - 1,
        )
        await self.shop.document_message(self.update, self.context)
        self.bot.send_document.assert_not_awaited()

    async def test_contact_indicator_is_per_purchase_and_survives_restart(self):
        self.shop.store.add_stock(self.product_id, "second-account")
        topup = self.shop.store.create_topup(1001, 10_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 2, "accountNumber": "0123456789", "transferAmount": 10_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        second = self.shop.store.purchase_product_quantity(
            1001, self.product_id, 1, 10_000,
        )["orders"][0]["id"]

        await self.shop.send_admin_purchase(self.message, self.purchase_id)
        self.assertIn("chưa ghi nhận tin nhắn từ shop",
                      self.message.reply_text.await_args.args[0])
        await self.tap(f"admin:message:{self.purchase_id}")
        self.message.text = "Xin chào"
        await self.shop.text_message(self.update, self.context)

        reopened = Store(self.shop.config.database_path,
                         self.shop.config.inventory_encryption_key)
        self.assertEqual(reopened.get_admin_purchase(self.purchase_id)[
            "admin_contact_count"], 1)
        self.assertEqual(reopened.get_admin_purchase(second)["admin_contact_count"], 0)
        await self.shop.send_admin_sales(self.message)
        sales = self.message.reply_text.await_args
        self.assertIn(f"#{self.purchase_id} ·", sales.args[0])
        self.assertIn("💬 Đã nhắn khách", sales.args[0])
        buttons = [button for row in sales.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        first_button = next(button for button in buttons if button.callback_data ==
                            f"admin:purchase:{self.purchase_id}")
        second_button = next(button for button in buttons if button.callback_data ==
                             f"admin:purchase:{second}")
        self.assertIn("💬", first_button.text)
        self.assertNotIn("💬", second_button.text)
        await self.shop.send_admin_purchase(self.message, self.purchase_id)
        self.assertIn("💬 Đã nhắn khách: 1 lần",
                      self.message.reply_text.await_args.args[0])
        self.assertNotIn("awaiting_admin_order_message", self.context.user_data)

    async def test_non_admin_cannot_send_document_to_customer(self):
        self.update.effective_user.id = 1002
        self.context.user_data["awaiting_admin_order_message"] = (
            self.purchase_id, time.monotonic() + 600,
        )
        self.message.document = SimpleNamespace(file_id="file-id", file_name="note.txt")
        await self.shop.document_message(self.update, self.context)
        self.bot.send_document.assert_not_awaited()
