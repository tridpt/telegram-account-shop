from pathlib import Path
from contextlib import closing
from datetime import datetime
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
import sqlite3

from cryptography.fernet import Fernet
from telegram import InputFile
from telegram.error import TelegramError

from bot import (ShopBot, account_delivery_file,
                 account_delivery_payload, account_pre_entities, telegram_text_units)
from config import Config
from export_reports import build_export_archive
from payments import process_sepay_payload
from store import SHOP_TIMEZONE, Store, WarrantyAlreadyOpen


class AccountCopyButtonTests(TestCase):
    def test_account_code_blocks_preserve_special_characters_and_utf16_offsets(self):
        first = "email+🔑@example.com|<password>&`value`"
        second = "two\nlines"
        content = (f"✅ Đơn hàng\nThông tin tài khoản:\n{first}\n\n"
                   f"Đơn khác\nThông tin tài khoản:\n{second}")
        entities = account_pre_entities(content, "vi", [first, second])
        self.assertEqual([entity.type for entity in entities], ["pre", "pre"])
        encoded = content.encode("utf-16-le")
        self.assertEqual([
            encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")
            for entity in entities
        ], [first, second])

    def test_long_accounts_use_one_short_message_and_complete_text_file(self):
        orders = [
            {"id": index, "name": "Account", "customer_email": None,
             "secret": f"user-{index}:" + "🔑" * 1800}
            for index in (36, 37)
        ]
        content, entities, markup = account_delivery_payload(orders, "vi", "Đã thanh toán")
        self.assertLessEqual(telegram_text_units(content), 3900)
        self.assertIn("Đơn hàng #36 · 2 tài khoản", content)
        self.assertNotIn("Đơn #37", content)
        self.assertIn("file .txt", content)
        self.assertNotIn("user-36", content)
        self.assertEqual(entities, [])
        self.assertEqual(len(markup.inline_keyboard), 3)  # Copy, file, and warranty.
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "copyorder:36")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "orderfile:36")
        self.assertEqual(markup.inline_keyboard[2][0].callback_data, "warranty:start:36")
        text_file = account_delivery_file(orders, "vi")
        self.assertTrue(text_file.startswith(b"\xef\xbb\xbf"))
        decoded = text_file.decode("utf-8-sig")
        self.assertIn(orders[0]["secret"], decoded)
        self.assertIn(orders[1]["secret"], decoded)
        self.assertEqual(decoded.count("ĐƠN HÀNG #"), 1)


class ProductCheckoutBotTests(IsolatedAsyncioTestCase):
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
            text="", reply_text=AsyncMock(), reply_photo=AsyncMock(),
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

    async def tap(self, data):
        self.query.data = data
        await self.shop.callbacks(self.update, self.context)

    async def test_quantity_buttons_and_other_quantity(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product, [f"account-{i}" for i in range(4)])
        await self.tap(f"buyqty:{product}")
        buttons = [button.callback_data
                   for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertEqual(buttons, [f"buyqty:{product}:1", f"buyqty:{product}:2",
                                   f"buyqty:{product}:3", f"buyqty:{product}:other",
                                   f"product:{product}"])
        await self.tap(f"buyqty:{product}:other")
        self.assertEqual(self.context.user_data["awaiting_buy_quantity"], product)
        self.message.text = "5"
        await self.shop.text_message(self.update, self.context)
        self.assertIn("kho không đủ", self.message.reply_text.await_args.args[0])
        self.message.text = "4"
        await self.shop.text_message(self.update, self.context)
        self.assertEqual(self.context.user_data["product_checkout"]["quantity"], 4)
        self.assertIn("40.000đ", self.message.reply_text.await_args.args[0])

    async def test_pasted_table_borders_are_removed_when_stock_is_imported(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product, [
            "| buyer@example.com|password|",
            "| another-account",
            "|literal-prefix|password",
        ])
        secrets = [item["secret"] for item in self.shop.store.list_stock_items(
            product, include_secret=True,
        )]
        self.assertEqual(secrets, [
            "buyer@example.com|password",
            "another-account",
            "|literal-prefix|password",
        ])

    async def test_bank_payment_delivers_selected_quantity_once(self):
        product = self.shop.store.add_product("Account", 20_000)
        self.shop.store.bulk_add_stock(product, ["first", "second", "third"])
        await self.tap(f"buyqty:{product}:2")
        token = self.context.user_data["product_checkout"]["token"]
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage") as make_qr:
            await self.tap(f"buyflow:bank:{token}")
        self.message.reply_photo.assert_awaited_once()
        checkout = self.shop.store.recent_direct_checkouts(1001)[0]
        self.assertEqual(checkout["expected_amount"], 40_000)
        self.assertEqual(make_qr.call_args.args[2:], (40_000, checkout["code"]))
        await self.tap(f"buyflow:bank:{token}")
        self.assertEqual(len(self.shop.store.recent_direct_checkouts(1001)), 1)
        result = process_sepay_payload(
            self.shop.store,
            {"id": 1, "accountNumber": "0123456789", "transferAmount": 40_000,
             "transferType": "in", "content": checkout["code"], "code": None},
            "0123456789", notify_admin_ids=frozenset({1}),
        )
        await self.shop.notify_payment(result)
        self.assertEqual(result["direct_status"], "fulfilled")
        self.assertEqual(len(self.shop.store.list_orders(1001)), 2)
        sent = "\n".join(call.args[1] for call in self.bot.send_message.await_args_list)
        self.assertIn("first", sent)
        self.assertIn("second", sent)
        self.assertNotIn("third", sent)
        deliveries = [call for call in self.bot.send_message.await_args_list
                      if "Thông tin tài khoản:" in call.args[1]]
        self.assertEqual(len(deliveries), 1)
        self.assertTrue(deliveries[0].kwargs["protect_content"])
        self.assertEqual(len(deliveries[0].kwargs["entities"]), 2)
        self.assertEqual(deliveries[0].kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text,
                         "first\nsecond")
        self.bot.send_document.assert_awaited_once()
        document = self.bot.send_document.await_args.kwargs["document"]
        self.assertIsInstance(document, InputFile)
        self.assertEqual(document.filename, f"don-chuyen-khoan-{checkout['id']}.txt")
        self.assertIn("first", document.input_file_content.decode("utf-8-sig"))
        self.assertIn("second", document.input_file_content.decode("utf-8-sig"))
        self.assertEqual(self.shop.store.list_due_direct_checkout_batches(), [])

    async def test_email_quantity_and_coupon_on_bank_order(self):
        product = self.shop.store.add_product("Email product", 10_000)
        self.shop.store.bulk_add_stock(product, ["one", "two"])
        self.shop.store.set_requires_email(product, True)
        self.shop.store.create_coupon("WELCOME", "percent", 10, 10)
        await self.tap(f"buyqty:{product}:2")
        self.assertTrue(self.context.user_data["awaiting_buy_email"])
        self.message.text = "one@example.com"
        await self.shop.text_message(self.update, self.context)
        self.assertTrue(self.context.user_data["awaiting_buy_email"])
        self.message.text = "one@example.com, two@example.com"
        await self.shop.text_message(self.update, self.context)
        token = self.context.user_data["product_checkout"]["token"]
        await self.tap(f"buyflow:coupon:{token}")
        self.message.text = "WELCOME"
        await self.shop.text_message(self.update, self.context)
        token = self.context.user_data["product_checkout"]["token"]
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.tap(f"buyflow:bank:{token}")
        checkout = self.shop.store.recent_direct_checkouts(1001)[0]
        self.assertEqual(checkout["expected_amount"], 18_000)
        result = process_sepay_payload(
            self.shop.store,
            {"id": 2, "accountNumber": "0123456789", "transferAmount": 18_000,
             "transferType": "in", "content": checkout["code"], "code": None},
            "0123456789",
        )
        self.assertEqual(result["direct_status"], "fulfilled")
        orders = self.shop.store.list_orders(1001)
        self.assertEqual({self.shop.store.get_order(1001, order["id"])["customer_email"]
                          for order in orders},
                         {"one@example.com", "two@example.com"})

    async def test_wallet_charges_selected_quantity_once(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product, ["one", "two", "three"])
        topup = self.shop.store.create_topup(1001, 30_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 3, "accountNumber": "0123456789", "transferAmount": 30_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        await self.tap(f"buyqty:{product}:3")
        token = self.context.user_data["product_checkout"]["token"]
        await self.tap(f"buyflow:wallet:{token}")
        self.assertEqual(len(self.shop.store.list_orders(1001)), 3)
        self.assertEqual(self.shop.store.get_balance(1001), 0)
        deliveries = [call for call in self.message.reply_text.await_args_list
                      if "Thông tin tài khoản:" in call.args[0]]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(len(deliveries[0].kwargs["entities"]), 3)
        self.assertEqual(deliveries[0].kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text,
                         "one\ntwo\nthree")
        self.message.reply_document.assert_awaited_once()
        file_content = self.message.reply_document.await_args.kwargs[
            "document"].input_file_content.decode("utf-8-sig")
        self.assertTrue(all(value in file_content for value in ("one", "two", "three")))
        await self.tap(f"buyflow:wallet:{token}")
        self.assertEqual(len(self.shop.store.list_orders(1001)), 3)
        self.message.reply_document.assert_awaited_once()

    async def test_bank_file_retry_does_not_repeat_account_message(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product, ["one", "two"])
        await self.tap(f"buyqty:{product}:2")
        token = self.context.user_data["product_checkout"]["token"]
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.tap(f"buyflow:bank:{token}")
        checkout = self.shop.store.recent_direct_checkouts(1001)[0]
        process_sepay_payload(
            self.shop.store,
            {"id": 4, "accountNumber": "0123456789", "transferAmount": 20_000,
             "transferType": "in", "content": checkout["code"], "code": None},
            "0123456789",
        )
        self.bot.send_document.side_effect = TelegramError("temporary")
        await self.shop.send_pending_direct_checkout_orders(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.bot.send_document.await_count, 1)
        pending = self.shop.store.list_due_direct_checkout_orders()
        self.assertEqual(pending, [])  # Retry is scheduled for later.
        self.bot.send_document.side_effect = None
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE direct_checkout_orders SET next_attempt_at='2000-01-01T00:00:00Z' "
                    "WHERE checkout_id=?", (checkout["id"],),
                )
        await self.shop.send_pending_direct_checkout_orders(self.bot)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.bot.send_document.await_count, 2)
        self.assertEqual(self.shop.store.list_due_direct_checkout_batches(), [])

    async def test_bank_checkout_with_two_products_sends_one_delivery(self):
        first = self.shop.store.add_product("First", 10_000)
        second = self.shop.store.add_product("Second", 20_000)
        self.shop.store.add_stock(first, "first-login")
        self.shop.store.add_stock(second, "second-login")
        self.shop.store.cart_add(1001, first)
        self.shop.store.cart_add(1001, second)
        checkout = self.shop.store.create_direct_checkout(1001, 100_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 5, "accountNumber": "0123456789", "transferAmount": 30_000,
             "transferType": "in", "content": checkout["code"], "code": None},
            "0123456789", notify_admin_ids=frozenset({1}),
        )
        await self.shop.send_pending_direct_checkout_orders(self.bot)
        deliveries = [call for call in self.bot.send_message.await_args_list
                      if "Thông tin tài khoản:" in call.args[1]]
        self.assertEqual(len(deliveries), 1)
        self.assertIn("first-login", deliveries[0].args[1])
        self.assertIn("second-login", deliveries[0].args[1])
        self.assertIn("Đơn hàng #1 · 2 tài khoản", deliveries[0].args[1])
        self.assertNotIn("Đơn #2", deliveries[0].args[1])
        self.bot.send_document.assert_awaited_once()
        text_file = self.bot.send_document.await_args.kwargs[
            "document"].input_file_content.decode("utf-8-sig")
        self.assertIn("first-login", text_file)
        self.assertIn("second-login", text_file)
        purchases = self.shop.store.list_purchases(1001)
        self.assertEqual(len(purchases), 1)
        self.assertEqual(len(purchases[0]["items"]), 2)
        self.assertEqual(purchases[0]["price"], 30_000)
        await self.shop.send_pending_purchase_notifications(self.bot)
        alerts = [call.args[1] for call in self.bot.send_message.await_args_list
                  if "CÓ ĐƠN MỚI" in call.args[1]]
        self.assertEqual(len(alerts), 1)
        self.assertIn("First", alerts[0])
        self.assertIn("Second", alerts[0])
        self.assertIn("Thanh toán: 30.000đ", alerts[0])

    async def test_purchase_history_groups_items_and_checks_owner(self):
        first = self.shop.store.add_product("First", 10_000)
        second = self.shop.store.add_product("Second", 20_000)
        self.shop.store.bulk_add_stock(first, ["first-a", "first-b", "first-c"])
        self.shop.store.add_stock(second, "second-a")
        topup = self.shop.store.create_topup(1001, 50_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 21, "accountNumber": "0123456789", "transferAmount": 50_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        first_purchase = self.shop.store.purchase_product_quantity(1001, first, 2, 20_000)
        second_purchase = self.shop.store.purchase_product_quantity(1001, second, 1, 20_000)
        reopened = Store(self.shop.store.path, self.shop.config.inventory_encryption_key)
        purchases = reopened.list_purchases(1001)
        self.assertEqual([purchase["id"] for purchase in purchases],
                         [second_purchase["orders"][0]["id"], first_purchase["orders"][0]["id"]])
        self.assertEqual(len(purchases[1]["items"]), 2)
        self.assertEqual(purchases[1]["price"], 20_000)
        self.assertEqual(len(reopened.list_all_purchases()), 2)
        self.assertEqual(len(reopened.get_admin_purchase(purchases[1]["id"])["items"]), 2)
        self.assertEqual(reopened.list_purchases(1001, 1)[0]["id"], purchases[0]["id"])
        bought = reopened.list_purchased_products(1001)
        self.assertEqual([(item["name"], item["quantity"]) for item in bought],
                         [("Second", 1), ("First", 2)])
        self.assertEqual(reopened.list_purchased_products(1001, 1, 1)[0]["name"], "First")
        self.assertIsNone(reopened.get_purchase(9999, purchases[1]["id"]))
        self.assertIsNone(reopened.get_purchase(1001, first_purchase["orders"][1]["id"]))
        today = datetime.now(SHOP_TIMEZONE).date()
        report = reopened.daily_report(today)
        self.assertEqual((report["orders"], report["revenue"]), (2, 40_000))
        exported = build_export_archive(reopened, today, today)
        self.assertEqual((exported["orders"], exported["items"]), (2, 3))
        await self.shop.send_orders(self.message, 1001)
        history = self.message.reply_text.await_args
        self.assertIn("10 đơn hàng gần nhất", history.args[0])
        self.assertEqual(len([button for row in history.kwargs["reply_markup"].inline_keyboard
                              for button in row if button.callback_data.startswith("purchase:")]), 2)
        self.assertIn("First ×2", history.args[0])
        await self.tap(f"purchase:{purchases[1]['id']}")
        detail = self.message.reply_text.await_args
        self.assertIn("first-a", detail.args[0])
        self.assertIn("first-b", detail.args[0])
        self.assertEqual(detail.args[0].count("Đơn hàng #"), 1)
        self.assertNotIn("second-a", detail.args[0])
        self.assertEqual(len(detail.kwargs["entities"]), 2)
        self.message.reply_document.assert_awaited_once()
        await self.shop.send_admin_sales(self.message)
        admin_history = self.message.reply_text.await_args
        self.assertEqual(len([button for row in admin_history.kwargs["reply_markup"].inline_keyboard
                              for button in row if button.callback_data.startswith("admin:purchase:")]), 2)
        await self.shop.send_admin_purchase(self.message, purchases[1]["id"])
        admin_detail = self.message.reply_text.await_args
        self.assertIn("First", admin_detail.args[0])
        self.assertEqual(len([button for row in admin_detail.kwargs["reply_markup"].inline_keyboard
                              for button in row if button.callback_data.startswith("admin:order:")]), 2)
        await self.shop.send_admin_users(self.message)
        self.assertIn("Đã mua: Second ×1, First ×2",
                      self.message.reply_text.await_args.args[0])
        await self.shop.send_admin_user(self.message, 1001)
        user_detail = self.message.reply_text.await_args
        self.assertIn("Sản phẩm đã mua:\nSecond ×1\nFirst ×2", user_detail.args[0])
        self.assertIn(f"#{purchases[1]['id']} · First ×2", user_detail.args[0])
        user_buttons = [button.callback_data for row in user_detail.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn("admin:userproducts:1001:0", user_buttons)
        self.assertIn(f"admin:purchase:{purchases[1]['id']}", user_buttons)
        self.shop.store.ensure_user(1)
        self.update.effective_user.id = 1
        await self.tap("admin:userproducts:1001:0")
        self.assertIn("SẢN PHẨM KHÁCH ĐÃ MUA", self.message.reply_text.await_args.args[0])
        self.assertIn("First ×2", self.message.reply_text.await_args.args[0])

    async def test_admin_user_purchased_products_paginate_without_exposing_to_customer(self):
        product_ids = []
        for number in range(1, 10):
            product_id = self.shop.store.add_product(f"Item {number}", 10_000)
            self.shop.store.add_stock(product_id, f"private-{number}")
            product_ids.append(product_id)
        topup = self.shop.store.create_topup(1001, 90_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 210, "accountNumber": "0123456789", "transferAmount": 90_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        for product_id in product_ids:
            self.shop.store.purchase_product_quantity(1001, product_id, 1, 10_000)
        await self.shop.send_admin_user_products(self.message, 1001)
        first_page = self.message.reply_text.await_args
        self.assertIn("Item 9 ×1", first_page.args[0])
        self.assertNotIn("Item 1 ×1", first_page.args[0])
        self.assertEqual(first_page.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                         "admin:userproducts:1001:1")
        await self.shop.send_admin_user_products(self.message, 1001, 1)
        last_page = self.message.reply_text.await_args
        self.assertIn("Item 1 ×1", last_page.args[0])
        self.assertEqual(last_page.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                         "admin:userproducts:1001:0")
        await self.tap("admin:userproducts:1001:0")
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])

    async def test_one_copy_and_one_warranty_cover_whole_purchase(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product, ["first-login", "second-login"])
        topup = self.shop.store.create_topup(1001, 20_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 22, "accountNumber": "0123456789", "transferAmount": 20_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        purchase = self.shop.store.purchase_product_quantity(1001, product, 2, 20_000)
        first_id, second_id = (item["id"] for item in purchase["orders"])
        await self.shop.send_purchase_detail(self.message, 1001, first_id)
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0].copy_text.text, "first-login\nsecond-login")
        self.assertEqual(buttons[1].callback_data, f"orderfile:{first_id}")
        self.assertEqual(buttons[2].callback_data, f"warranty:start:{first_id}")
        await self.tap(f"warranty:start:{second_id}")  # Old item links resolve to the order.
        self.assertEqual(self.context.user_data["awaiting_warranty"], first_id)
        self.message.text = "One account does not work"
        await self.shop.text_message(self.update, self.context)
        ticket = self.shop.store.get_latest_warranty(1001, second_id)
        self.assertEqual(ticket["order_id"], first_id)
        self.assertEqual(ticket["id"], self.shop.store.get_latest_warranty(1001, first_id)["id"])
        with self.assertRaises(WarrantyAlreadyOpen):
            self.shop.store.create_warranty(1001, second_id, "Another request")
        await self.tap(f"warranty:status:{second_id}")
        self.assertIn(f"đơn #{first_id}", self.message.reply_text.await_args.args[0])
        self.shop.store.resolve_warranty(ticket["id"], "Fixed both accounts")
        await self.tap(f"warranty:status:{first_id}")
        self.assertIn("Fixed both accounts", self.message.reply_text.await_args.args[0])
        await self.tap(f"order:{second_id}")
        old_link_detail = self.message.reply_text.await_args.args[0]
        self.assertIn("first-login", old_link_detail)
        self.assertIn("second-login", old_link_detail)

    async def test_legacy_item_warranty_blocks_new_order_warranty(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product, ["one", "two"])
        topup = self.shop.store.create_topup(1001, 20_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 23, "accountNumber": "0123456789", "transferAmount": 20_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        purchase = self.shop.store.purchase_product_quantity(1001, product, 2, 20_000)
        first_id, second_id = (item["id"] for item in purchase["orders"])
        ticket = self.shop.store.create_warranty(1001, first_id, "Old issue")
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            conn.execute("UPDATE warranty_requests SET order_id=? WHERE id=?",
                         (second_id, ticket["id"]))
            conn.commit()
        reopened = Store(self.shop.store.path, self.shop.config.inventory_encryption_key)
        self.assertEqual(reopened.get_latest_warranty(1001, first_id)["id"], ticket["id"])
        with self.assertRaises(WarrantyAlreadyOpen):
            reopened.create_warranty(1001, first_id, "New issue")

    async def test_long_order_copy_opens_one_grouped_code_block(self):
        product = self.shop.store.add_product("Account", 10_000)
        first, second = "A" * 180, "B" * 180
        self.shop.store.bulk_add_stock(product, [first, second])
        topup = self.shop.store.create_topup(1001, 20_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 24, "accountNumber": "0123456789", "transferAmount": 20_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        purchase = self.shop.store.purchase_product_quantity(1001, product, 2, 20_000)
        purchase_id = purchase["orders"][0]["id"]
        await self.shop.send_purchase_detail(self.message, 1001, purchase_id)
        buttons = [button for row in self.message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertEqual([button.callback_data for button in buttons],
                         [f"copyorder:{purchase_id}", f"orderfile:{purchase_id}",
                          f"warranty:start:{purchase_id}"])
        await self.tap(f"copyorder:{purchase_id}")
        copied = self.message.reply_text.await_args
        self.assertEqual(copied.args[0], first + "\n" + second)
        self.assertEqual(len(copied.kwargs["entities"]), 1)
        self.assertTrue(copied.kwargs["protect_content"])
        self.update.effective_user.id = 1002
        await self.tap(f"copyorder:{purchase_id}")
        self.assertNotIn(first, self.message.reply_text.await_args.args[0])

    async def test_very_long_order_copy_resends_one_file(self):
        product = self.shop.store.add_product("Account", 10_000)
        first, second = "A" * 2000, "B" * 2000
        self.shop.store.bulk_add_stock(product, [first, second])
        topup = self.shop.store.create_topup(1001, 20_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 25, "accountNumber": "0123456789", "transferAmount": 20_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        purchase = self.shop.store.purchase_product_quantity(1001, product, 2, 20_000)
        purchase_id = purchase["orders"][0]["id"]
        await self.tap(f"copyorder:{purchase_id}")
        document = self.message.reply_document.await_args.kwargs["document"]
        content = document.input_file_content.decode("utf-8-sig")
        self.assertIn(first, content)
        self.assertIn(second, content)
        self.assertEqual(content.count("ĐƠN HÀNG #"), 1)

    async def test_order_file_button_sends_complete_purchase_and_checks_owner(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product, ["first-login", "second-login"])
        topup = self.shop.store.create_topup(1001, 20_000)
        process_sepay_payload(
            self.shop.store,
            {"id": 26, "accountNumber": "0123456789", "transferAmount": 20_000,
             "transferType": "in", "content": topup["code"], "code": None},
            "0123456789",
        )
        purchase = self.shop.store.purchase_product_quantity(1001, product, 2, 20_000)
        purchase_id = purchase["orders"][0]["id"]
        await self.tap(f"orderfile:{purchase_id}")
        self.message.reply_document.assert_awaited_once()
        document = self.message.reply_document.await_args.kwargs["document"]
        self.assertEqual(document.filename, f"don-hang-{purchase_id}.txt")
        content = document.input_file_content.decode("utf-8-sig")
        self.assertIn("first-login", content)
        self.assertIn("second-login", content)
        self.assertEqual(content.count("ĐƠN HÀNG #"), 1)
        self.message.reply_document.reset_mock()
        self.message.reply_document.side_effect = TelegramError("temporary upload failure")
        await self.tap(f"orderfile:{purchase_id}")
        self.assertIn("Chưa gửi được file .txt", self.message.reply_text.await_args.args[0])
        self.message.reply_document.side_effect = None
        self.message.reply_document.reset_mock()
        await self.tap(f"orderfile:{purchase_id}")
        self.message.reply_document.assert_awaited_once()
        self.update.effective_user.id = 1002
        await self.tap(f"orderfile:{purchase_id}")
        self.message.reply_document.assert_awaited_once()
        self.assertIn("Không tìm thấy đơn hàng", self.message.reply_text.await_args.args[0])

    async def test_insufficient_wallet_keeps_bank_payment_available(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.add_stock(product, "login")
        await self.tap(f"buyqty:{product}:1")
        old_token = self.context.user_data["product_checkout"]["token"]
        await self.tap(f"buyflow:wallet:{old_token}")
        self.assertIn("Số dư chưa đủ", self.message.reply_text.await_args_list[-2].args[0])
        new_token = self.context.user_data["product_checkout"]["token"]
        self.assertNotEqual(old_token, new_token)
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.tap(f"buyflow:bank:{new_token}")
        self.assertEqual(self.shop.store.recent_direct_checkouts(1001)[0]["expected_amount"],
                         10_000)

    async def test_old_cart_button_opens_quantity_and_old_bank_order_remains_accessible(self):
        product = self.shop.store.add_product("Account", 10_000)
        self.shop.store.add_stock(product, "login")
        await self.tap(f"cart:add:{product}")
        self.assertIn("Chọn số lượng", self.message.reply_text.await_args.args[0])
        await self.tap("cart:bank")
        self.assertEqual(self.shop.store.recent_direct_checkouts(1001), [])
