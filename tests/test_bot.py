from pathlib import Path
from datetime import date, datetime, timezone
from dataclasses import replace
from contextlib import closing
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
import asyncio
import os
import sqlite3

from cryptography.fernet import Fernet
from telegram import BotCommandScopeAllPrivateChats, InputFile
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop

from bot import INVENTORY_TEXT_MAX_UNITS, ShopBot, parse_amount, telegram_text_units
from backup_shop import backup_and_verify
from config import Config
from store import Store


class ShopBotMessageTests(IsolatedAsyncioTestCase):
    async def test_webhook_operational_alert_is_rate_limited(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        self.shop.application = SimpleNamespace(bot=bot)
        self.shop.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.shop.schedule_webhook_alert, "processing")
        await asyncio.to_thread(self.shop.schedule_webhook_alert, "processing")
        await asyncio.sleep(0)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args[0], 1)
        self.assertIn("Webhook SePay", bot.send_message.await_args.args[1])

    async def test_inventory_audit_notifies_once_and_retries_failed_alert(self):
        product_id = self.shop.store.add_product("Small product", 10_000)
        self.shop.store.add_stock(product_id, "first-account")
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        self.shop.application = SimpleNamespace(bot=bot)
        await self.shop.check_inventory_alerts()
        self.assertEqual(len(self.shop.store.list_low_stock_alerts()), 1)
        bot.send_message.side_effect = None
        await self.shop.check_inventory_alerts()
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(self.shop.store.list_low_stock_alerts(), [])

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
        self.message = SimpleNamespace(reply_photo=AsyncMock(), reply_text=AsyncMock(),
                                       reply_document=AsyncMock())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_amount_sends_qr_as_photo_immediately(self):
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage") as make_qr:
            await self.shop.create_topup(self.message, 1001, 10_000)

        self.message.reply_photo.assert_awaited_once()
        self.message.reply_text.assert_not_awaited()
        kwargs = self.message.reply_photo.await_args.kwargs
        self.assertIsInstance(kwargs["photo"], InputFile)
        self.assertIn("Nội dung chuyển khoản: NAP", kwargs["caption"])
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].text, "Xem số dư")
        args = make_qr.call_args.args
        self.assertEqual(args[:3], ("970422", "0123456789", 10_000))
        self.assertTrue(args[3].startswith("NAP"))

    async def test_virtual_account_is_displayed_and_encoded_in_qr(self):
        self.shop.config = replace(
            self.shop.config, bank_transfer_account_number="VQRQAMHLG6528",
        )
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage") as make_qr:
            await self.shop.create_topup(self.message, 1001, 10_000)

        kwargs = self.message.reply_photo.await_args.kwargs
        self.assertEqual(make_qr.call_args.args[1], "VQRQAMHLG6528")
        self.assertIsInstance(kwargs["photo"], InputFile)
        self.assertIn("Số tài khoản: VQRQAMHLG6528", kwargs["caption"])

    async def test_telegram_photo_failure_keeps_transfer_details(self):
        self.message.reply_photo.side_effect = TelegramError("Photo failed")
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.shop.create_topup(self.message, 1001, 10_000)

        self.message.reply_photo.assert_awaited_once()
        self.message.reply_text.assert_awaited_once()
        self.assertIn("Nội dung chuyển khoản: NAP", self.message.reply_text.await_args.args[0])

    async def test_qr_creation_failure_keeps_transfer_details(self):
        with patch("bot.vietqr_png", side_effect=ValueError("bad image")):
            await self.shop.create_topup(self.message, 1001, 10_000)

        self.message.reply_photo.assert_not_awaited()
        self.message.reply_text.assert_awaited_once()
        args, kwargs = self.message.reply_text.await_args
        self.assertIn("Nội dung chuyển khoản: NAP", args[0])
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].text, "Xem số dư")

    async def test_repeated_topup_does_not_create_more_codes_or_fetch_qr(self):
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage") as make_qr:
            await self.shop.create_topup(self.message, 1001, 10_000)
            await self.shop.create_topup(self.message, 1001, 10_000)
        self.assertEqual(len(self.shop.store.list_topups(1001)), 1)
        self.message.reply_photo.assert_awaited_once()
        make_qr.assert_called_once()
        self.assertIn("quá nhanh", self.message.reply_text.await_args.args[0])

    async def test_topup_menu_offers_presets_and_other_amount(self):
        update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(args=[], user_data={})
        await self.shop.topup(update, context)

        buttons = [
            button for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(
            [button.callback_data for button in buttons],
            ["topup:amount:10000", "topup:amount:20000", "topup:amount:50000",
             "topup:amount:100000", "topup:amount:200000", "topup:amount:500000",
             "topup:amount:1000000", "topup:other", "menu"],
        )
        self.assertNotIn("awaiting_topup", context.user_data)

        update.callback_query = SimpleNamespace(
            answer=AsyncMock(), data="topup", message=self.message,
        )
        await self.shop.callbacks(update, context)
        self.assertIn("Chọn số tiền", self.message.reply_text.await_args.args[0])
        self.assertNotIn("awaiting_topup", context.user_data)

        self.shop.config = replace(self.shop.config, min_topup=20_000, max_topup=50_000)
        await self.shop.send_topup_options(self.message)
        callbacks = [
            button.callback_data
            for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["topup:amount:20000", "topup:amount:50000",
                                     "topup:other", "menu"])

    async def test_preset_amount_sends_qr_without_typing(self):
        query = SimpleNamespace(
            answer=AsyncMock(), data="topup:amount:20000", message=self.message,
        )
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={"awaiting_topup": True})
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage") as make_qr:
            await self.shop.callbacks(update, context)

        query.answer.assert_awaited_once()
        self.message.reply_photo.assert_awaited_once()
        self.assertNotIn("awaiting_topup", context.user_data)
        self.assertEqual(self.shop.store.list_topups(1001)[0]["requested_amount"], 20_000)
        self.assertEqual(make_qr.call_args.args[2], 20_000)

    async def test_other_amount_prompts_then_sends_qr(self):
        query = SimpleNamespace(answer=AsyncMock(), data="topup:other", message=self.message)
        update = SimpleNamespace(
            message=self.message, callback_query=query,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={})
        await self.shop.callbacks(update, context)
        self.assertTrue(context.user_data["awaiting_topup"])
        self.assertIn("Gửi số tiền", self.message.reply_text.await_args.args[0])

        self.message.text = "5000"
        await self.shop.text_message(update, context)
        self.assertTrue(context.user_data["awaiting_topup"])
        self.assertEqual(self.shop.store.list_topups(1001), [])

        self.message.text = "75000"
        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.shop.text_message(update, context)
        self.message.reply_photo.assert_awaited_once()
        self.assertNotIn("awaiting_topup", context.user_data)
        self.assertEqual(self.shop.store.list_topups(1001)[0]["requested_amount"], 75_000)

    async def test_spam_guard_stops_update_and_warns_only_once(self):
        query = SimpleNamespace(answer=AsyncMock())
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001), effective_message=self.message,
        )
        for _ in range(10):
            await self.shop.spam_guard(update, None)
        with self.assertRaises(ApplicationHandlerStop):
            await self.shop.spam_guard(update, None)
        query.answer.assert_awaited_once()
        with self.assertRaises(ApplicationHandlerStop):
            await self.shop.spam_guard(update, None)
        query.answer.assert_awaited_once()

    async def test_group_updates_are_discarded_without_reply(self):
        update = SimpleNamespace(
            callback_query=None, effective_chat=SimpleNamespace(type="group"),
            effective_user=SimpleNamespace(id=1001), effective_message=self.message,
        )
        with self.assertRaises(ApplicationHandlerStop):
            await self.shop.spam_guard(update, None)
        self.message.reply_text.assert_not_awaited()

    async def test_menu_offers_customer_actions(self):
        await self.shop.send_menu(self.message, 1001)

        args, kwargs = self.message.reply_text.await_args
        self.assertIn("CỬA HÀNG TÀI KHOẢN", args[0])
        callbacks = [button.callback_data for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(callbacks, ["shop:0", "payments", "bundles:0", "topup", "balance", "orders",
                                     "deposits", "following:0", "stocknotify", "language", "help"])

    async def test_language_selection_persists_and_changes_customer_menu(self):
        self.shop.store.ensure_user(1001)
        query = SimpleNamespace(data="language:en", answer=AsyncMock(), message=self.message)
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))

        self.assertEqual(self.shop.store.get_user_language(1001), "en")
        self.assertIn("ACCOUNT SHOP", self.message.reply_text.await_args.args[0])
        buttons = self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].text, "🛍 Products")
        self.assertEqual(buttons[-2][0].callback_data, "language")
        restarted = Store(self.shop.config.database_path,
                          self.shop.config.inventory_encryption_key)
        self.assertEqual(restarted.get_user_language(1001), "en")

        query.data = "language:vi"
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertEqual(self.shop.store.get_user_language(1001), "vi")
        self.assertIn("CỬA HÀNG TÀI KHOẢN", self.message.reply_text.await_args.args[0])

    async def test_english_product_checkout_and_qr_instructions(self):
        self.shop.store.set_user_language(1001, "en")
        self.message.chat_id = 1001
        product_id = self.shop.store.add_product("Example account", 10_000)
        self.shop.store.add_stock(product_id, "demo-login:secret")

        await self.shop.send_shop(self.message, 0)
        self.assertIn("PRODUCTS", self.message.reply_text.await_args.args[0])
        self.assertNotIn("Sold 0", self.message.reply_text.await_args.args[0])

        context = SimpleNamespace(user_data={})
        await self.shop.send_purchase_confirmation(
            self.message, 1001, context, "product", product_id,
        )
        self.assertIn("Buy Example account?", self.message.reply_text.await_args.args[0])
        self.assertEqual(
            self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text,
            "Confirm purchase",
        )

        with patch("bot.vietqr_png", return_value=b"\x89PNG\r\n\x1a\nimage"):
            await self.shop.create_topup(self.message, 1001, 20_000)
        caption = self.message.reply_photo.await_args.kwargs["caption"]
        self.assertIn("Top-up request", caption)
        self.assertIn("Transfer note: NAP", caption)
        self.assertEqual(
            self.message.reply_photo.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text,
            "View balance",
        )

        topup = self.shop.store.list_topups(1001)[0]
        self.shop.store.record_bank_event(
            "language-test-payment", "0123456789", 20_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        token = context.user_data["pending_purchase"][0]
        query = SimpleNamespace(answer=AsyncMock(), data=f"confirm:{token}",
                                message=self.message)
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context.bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.callbacks(update, context)
        self.assertIn("Purchase successful!", self.message.reply_text.await_args.args[0])
        self.assertIn("demo-login:secret", self.message.reply_text.await_args.args[0])
        self.assertEqual(
            self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text,
            "demo-login:secret",
        )
        self.assertEqual(self.message.reply_text.await_args.kwargs["entities"][0].type, "pre")

        await self.shop.send_orders(self.message, 1001)
        self.assertIn("most recent orders", self.message.reply_text.await_args.args[0])
        order_id = self.shop.store.list_orders(1001)[0]["id"]
        query.data = f"order:{order_id}"
        await self.shop.callbacks(update, context)
        self.assertIn("Account details:", self.message.reply_text.await_args.args[0])
        self.assertEqual(
            self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text,
            "demo-login:secret",
        )
        self.assertEqual(self.message.reply_text.await_args.kwargs["entities"][0].type, "pre")
        query.data = f"warranty:start:{order_id}"
        await self.shop.callbacks(update, context)
        self.assertIn("Describe the issue", self.message.reply_text.await_args.args[0])

    async def test_announcements_and_payment_use_each_customers_language(self):
        self.shop.store.set_user_language(1001, "en")
        self.shop.store.ensure_user(1002)
        product_id = self.shop.store.add_product("Example account", 20_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.set_product_following(1002, product_id, True)
        self.shop.store.bulk_add_stock(
            product_id, ["stock-one", "stock-two"], announce_except=frozenset(),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_stock_announcements(bot)
        by_user = {call.args[0]: call for call in bot.send_message.await_args_list}
        self.assertIn("NEW STOCK!", by_user[1001].args[1])
        self.assertIn("HÀNG MỚI VỀ!", by_user[1002].args[1])
        self.assertEqual(by_user[1001].kwargs["reply_markup"].inline_keyboard[0][0].text,
                         "🛒 Buy now")

        self.shop.application = SimpleNamespace(bot=bot)
        await self.shop.notify_payment({
            "status": "credited", "user_id": 1001, "amount": 20_000,
            "code": "NAP123456789ABC", "balance": 20_000,
        })
        self.assertIn("Top-up successful", bot.send_message.await_args.args[1])
        self.assertEqual(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text,
                         "🛍 Products")

    async def test_sale_announcement_uses_english_for_english_customer(self):
        self.shop.store.set_user_language(1001, "en")
        product_id = self.shop.store.add_product("Example <Pro>", 20_000)
        self.shop.store.add_stock(product_id, "stock-one")
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.start_sale(product_id, 19_000, 1,
                                   announce_except=frozenset())

        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_sale_announcements(bot)
        text = bot.send_message.await_args.args[1]
        self.assertIn("FLASH SALE!", text)
        self.assertIn("Regular price:", text)
        self.assertIn("Sale price:", text)
        self.assertIn("Example &lt;Pro&gt;", text)
        self.assertEqual(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text,
                         "🛒 Buy now")

    async def test_start_records_customer_name_for_admin_views(self):
        update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001, username="alice", full_name="Alice B"),
        )
        await self.shop.start(update, SimpleNamespace(user_data={}))

        user = self.shop.store.get_user_summary(1001)
        self.assertEqual((user["username"], user["display_name"]), ("alice", "Alice B"))

    async def test_first_customer_interaction_notifies_admin_only_once(self):
        update = SimpleNamespace(
            callback_query=None, message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001, username="alice", full_name="Alice B"),
        )
        await self.shop.spam_guard(update, None)
        await self.shop.start(update, SimpleNamespace(user_data={}))
        await self.shop.start(update, SimpleNamespace(user_data={}))
        await self.shop.spam_guard(update, None)

        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_new_customer_notifications(bot)
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], 1)
        self.assertIn("@alice (ID 1001)", args[1])
        self.assertIn("Tương tác đầu tiên:", args[1])
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                         "admin:user:1001")
        self.assertTrue(kwargs["protect_content"])
        await self.shop.send_pending_new_customer_notifications(bot)
        bot.send_message.assert_awaited_once()

    async def test_existing_customer_and_admin_do_not_trigger_new_customer_alert(self):
        self.shop.store.ensure_user(1001)
        for user_id in (1001, 1):
            update = SimpleNamespace(
                callback_query=None, effective_message=self.message,
                effective_chat=SimpleNamespace(type="private"),
                effective_user=SimpleNamespace(id=user_id, username=None,
                                               full_name="Returning user"),
            )
            await self.shop.spam_guard(update, None)
        self.assertEqual(self.shop.store.list_due_new_customer_notifications(), [])

    async def test_failed_new_customer_alert_retries_after_bot_restart(self):
        update = SimpleNamespace(
            callback_query=None, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001, username=None, full_name="New customer"),
        )
        await self.shop.spam_guard(update, None)
        failed_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        await self.shop.send_pending_new_customer_notifications(failed_bot)
        with closing(sqlite3.connect(self.shop.config.database_path)) as conn:
            status, attempts = conn.execute(
                "SELECT status, attempts FROM new_customer_notification_deliveries"
            ).fetchone()
            self.assertEqual((status, attempts), ("pending", 1))
            conn.execute(
                "UPDATE new_customer_notification_deliveries "
                "SET next_attempt_at='2000-01-01T00:00:00Z'"
            )
            conn.commit()

        restarted = ShopBot(self.shop.config, Store(
            self.shop.config.database_path, self.shop.config.inventory_encryption_key,
        ))
        recovered_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_pending_new_customer_notifications(recovered_bot)
        recovered_bot.send_message.assert_awaited_once()
        self.assertEqual(restarted.store.list_due_new_customer_notifications(), [])

    async def test_new_customer_alert_skips_removed_admin(self):
        update = SimpleNamespace(
            callback_query=None, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001, username="alice", full_name="Alice"),
        )
        await self.shop.spam_guard(update, None)
        self.shop.config = replace(self.shop.config, admin_ids=frozenset({2}))

        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_new_customer_notifications(bot)
        bot.send_message.assert_not_awaited()
        self.assertEqual(self.shop.store.list_due_new_customer_notifications(), [])

    async def test_private_chat_command_menu_is_registered(self):
        application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))
        with patch("bot.start_webhook_server", return_value=(None, None)):
            await self.shop.post_init(application)

        args, kwargs = application.bot.set_my_commands.await_args
        self.assertEqual(args[0][0].command, "menu")
        self.assertIsInstance(kwargs["scope"], BotCommandScopeAllPrivateChats)

    async def test_shop_shows_out_of_stock_demo_and_menu(self):
        product_id = self.shop.store.add_product("[DEMO] Tài khoản mẫu", 10_000, "Chưa mở bán")

        await self.shop.send_shop(self.message, 0)

        args, kwargs = self.message.reply_text.await_args
        self.assertEqual(args[0], "🛍 SẢN PHẨM\nChạm vào sản phẩm để xem chi tiết.")
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        product_button = next(button for button in buttons
                              if button.callback_data == f"product:{product_id}")
        self.assertIn("10.000đ", product_button.text)
        self.assertIn("Hết hàng", product_button.text)
        self.assertEqual(product_button.style, "danger")
        callbacks = [button.callback_data for button in buttons]
        self.assertIn(f"product:{product_id}", callbacks)
        self.assertIn("menu", callbacks)
        self.assertNotIn("bundles:0", callbacks)

    async def test_demo_detail_has_no_buy_button(self):
        product_id = self.shop.store.add_product("[DEMO] Tài khoản mẫu", 10_000, "Chưa mở bán")
        query = SimpleNamespace(
            answer=AsyncMock(), data=f"product:{product_id}", message=self.message,
        )
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={"awaiting_topup": True})

        await self.shop.callbacks(update, context)

        args, kwargs = self.message.reply_text.await_args
        self.assertIn("chưa thể mua", args[0])
        self.assertNotIn("Đã bán:", args[0])
        callbacks = [button.callback_data for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertNotIn(f"buy:{product_id}", callbacks)
        self.assertIn("shop:0", callbacks)
        self.assertIn("menu", callbacks)
        self.assertNotIn("awaiting_topup", context.user_data)

    async def test_add_product_accepts_10k_price(self):
        message = SimpleNamespace(
            text="/addproduct Tài khoản mẫu 1 tháng | 10k | Bảo hành FULL",
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )

        await self.shop.add_product(update, None)

        product = self.shop.store.list_products()[0]
        self.assertEqual(product["price"], 10_000)
        self.assertEqual(product["description"], "Bảo hành FULL")
        self.assertIn("10.000đ", message.reply_text.await_args.args[0])
        self.assertEqual(parse_amount("10K"), 10_000)
        with self.assertRaises(ValueError):
            parse_amount("10.5k")

    async def test_new_product_waits_for_successful_bulk_stock_to_notify_customers(self):
        for user_id in (1, 1001, 1002):
            self.shop.store.ensure_user(user_id)
        message = SimpleNamespace(
            text="/addproduct Tài khoản mới <Pro> | 49k | Dùng 30 ngày",
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )

        await self.shop.add_product(update, None)

        product = self.shop.store.list_products()[0]
        self.assertIn("Chưa thông báo cho khách", message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.list_due_stock_announcements(), [])
        self.shop.store.set_product_following(1001, product["id"], True)
        self.shop.store.set_product_following(1002, product["id"], True)
        stock_message = SimpleNamespace(
            text=f"/bulkstock {product['id']}", reply_text=AsyncMock(), delete=AsyncMock(),
        )
        stock_update = SimpleNamespace(
            message=stock_message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        admin_bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(user_data={}, bot=admin_bot)
        await self.shop.bulk_stock(stock_update, context)
        self.assertEqual(self.shop.store.list_due_stock_announcements(), [])
        stock_message.text = "login-one:secret\nlogin-two:secret"
        await self.shop.text_message(stock_update, context)
        self.assertIn("2 khách", admin_bot.send_message.await_args.args[1])

        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_stock_announcements(bot)
        self.assertEqual({call.args[0] for call in bot.send_message.await_args_list}, {1001, 1002})
        for call in bot.send_message.await_args_list:
            self.assertTrue(call.args[1].startswith("<b>🆕 HÀNG MỚI VỀ!"))
            self.assertIn("Tài khoản mới", call.args[1])
            self.assertIn("&lt;Pro&gt;", call.args[1])
            self.assertEqual(call.kwargs["parse_mode"], "HTML")
            self.assertIn("➕ Thêm: 2", call.args[1])
            self.assertIn("🗄️ Tồn kho hiện tại: 2", call.args[1])
            self.assertIn("💰 Giá: 49.000đ", call.args[1])
            button = call.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(button.text, "🛒 Mua ngay")
            self.assertEqual(
                button.callback_data, f"buy:{product['id']}",
            )
            mute_button = call.kwargs["reply_markup"].inline_keyboard[1][0]
            self.assertEqual(
                (mute_button.text, mute_button.callback_data),
                ("🔕 Bỏ theo dõi sản phẩm", f"unfollow:{product['id']}"),
            )
        await self.shop.send_pending_stock_announcements(bot)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_customer_can_mute_and_reenable_stock_announcements(self):
        for user_id in (1001, 1002):
            self.shop.store.ensure_user(user_id)
        product_id = self.shop.store.add_product("Account", 20_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.set_product_following(1002, product_id, True)
        self.shop.store.bulk_add_stock(
            product_id, ["stock-1"], announce_except=frozenset(),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_stock_announcements(bot)
        self.assertEqual({call.args[0] for call in bot.send_message.await_args_list},
                         {1001, 1002})

        second = self.shop.store.bulk_add_stock(
            product_id, ["stock-2"], announce_except=frozenset(),
        )
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(data="stocknotify:off", answer=AsyncMock(), message=message)
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={})

        await self.shop.callbacks(update, context)

        self.assertFalse(self.shop.store.stock_notifications_enabled(1001))
        self.assertIn("đang tắt", message.reply_text.await_args.args[0])
        self.assertEqual(
            message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
            "stocknotify:on",
        )
        self.assertFalse(
            self.shop.store.stock_announcement_sendable(second["announcement_id"], 1001)
        )
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            status = conn.execute(
                """SELECT status FROM stock_announcement_deliveries
                   WHERE batch_id=? AND user_id=1001""",
                (second["announcement_id"],),
            ).fetchone()[0]
        self.assertEqual(status, "skipped")
        self.assertFalse(
            Store(self.shop.store.path, self.shop.config.inventory_encryption_key)
            .stock_notifications_enabled(1001)
        )
        await self.shop.send_pending_stock_announcements(bot)
        self.assertEqual(bot.send_message.await_args.args[0], 1002)

        muted_batch = self.shop.store.bulk_add_stock(
            product_id, ["stock-3"], announce_except=frozenset(),
        )
        self.assertEqual(muted_batch["recipients"], 1)
        query.data = "stocknotify:on"
        await self.shop.callbacks(update, context)
        self.assertTrue(self.shop.store.stock_notifications_enabled(1001))
        self.assertIn("đang bật", message.reply_text.await_args.args[0])
        query.data = "stocknotify"
        await self.shop.callbacks(update, context)
        self.assertEqual(
            message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
            "stocknotify:off",
        )
        future_batch = self.shop.store.bulk_add_stock(
            product_id, ["stock-4"], announce_except=frozenset(),
        )
        self.assertEqual(future_batch["recipients"], 2)
        self.assertEqual(
            {item["user_id"] for item in self.shop.store.list_due_stock_announcements()},
            {1001, 1002},
        )

    async def test_each_bulk_stock_imports_duplicates_and_notifies_customers(self):
        self.shop.store.ensure_user(1)
        self.shop.store.ensure_user(1001)
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.add_stock(product_id, "single-stock")
        self.assertEqual(self.shop.store.list_due_stock_announcements(), [])
        message = SimpleNamespace(
            text=f"/bulkstock {product_id} first-stock", reply_text=AsyncMock(),
            delete=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        customer_bot = SimpleNamespace(send_message=AsyncMock())

        await self.shop.bulk_stock(update, context)
        self.assertEqual(len(self.shop.store.list_due_stock_announcements()), 1)
        await self.shop.send_pending_stock_announcements(customer_bot)
        self.assertIn("➕ Thêm: 1", customer_bot.send_message.await_args.args[1])
        self.assertIn("🗄️ Tồn kho hiện tại: 2", customer_bot.send_message.await_args.args[1])

        message.text = f"/bulkstock {product_id} second-stock\nthird-stock"
        await self.shop.bulk_stock(update, context)
        self.assertEqual(len(self.shop.store.list_due_stock_announcements()), 1)
        await self.shop.send_pending_stock_announcements(customer_bot)
        self.assertEqual(customer_bot.send_message.await_count, 2)
        self.assertIn("➕ Thêm: 2", customer_bot.send_message.await_args.args[1])
        self.assertIn("🗄️ Tồn kho hiện tại: 4", customer_bot.send_message.await_args.args[1])

        message.text = f"/bulkstock {product_id} second-stock\nthird-stock"
        await self.shop.bulk_stock(update, context)
        self.assertEqual(len(self.shop.store.list_due_stock_announcements()), 1)
        duplicate_response = context.bot.send_message.await_args.args[1]
        self.assertIn("Đã nhập 2", duplicate_response)
        self.assertIn("2 dòng trùng vẫn đã nhập", duplicate_response)
        self.assertIn("1, 2", duplicate_response)
        await self.shop.send_pending_stock_announcements(customer_bot)
        self.assertEqual(customer_bot.send_message.await_count, 3)
        self.assertIn("➕ Thêm: 2", customer_bot.send_message.await_args.args[1])
        self.assertIn("🗄️ Tồn kho hiện tại: 6", customer_bot.send_message.await_args.args[1])

        message.text = (
            f"/bulkstock {product_id} first-stock\nfourth-stock\n"
            "fourth-stock\nsingle-stock"
        )
        await self.shop.bulk_stock(update, context)
        mixed_response = context.bot.send_message.await_args.args[1]
        self.assertIn("Đã nhập 4", mixed_response)
        self.assertIn("3 dòng trùng vẫn đã nhập", mixed_response)
        self.assertIn("1, 3, 4", mixed_response)
        self.assertNotIn("fourth-stock", mixed_response)
        await self.shop.send_pending_stock_announcements(customer_bot)
        self.assertEqual(customer_bot.send_message.await_count, 4)
        self.assertIn("➕ Thêm: 4", customer_bot.send_message.await_args.args[1])
        self.assertIn("🗄️ Tồn kho hiện tại: 10", customer_bot.send_message.await_args.args[1])
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM stock_announcement_batches").fetchone()[0]
        self.assertEqual(count, 4)

    async def test_blocked_customer_does_not_stop_other_product_notifications(self):
        self.shop.store.ensure_user(1001)
        self.shop.store.ensure_user(1002)
        product_id = self.shop.store.add_product("Tài khoản mới", 49_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.set_product_following(1002, product_id, True)
        batch = self.shop.store.bulk_add_stock(
            product_id, ["stock-1"], announce_except=frozenset({1}),
        )

        async def send(chat_id, *_args, **_kwargs):
            if chat_id == 1001:
                raise Forbidden("bot was blocked by the user")

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        await self.shop.send_pending_stock_announcements(bot)

        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(self.shop.store.list_due_stock_announcements(), [])
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            statuses = dict(conn.execute(
                "SELECT user_id, status FROM stock_announcement_deliveries WHERE batch_id=?",
                (batch["announcement_id"],),
            ))
        self.assertEqual(statuses, {1001: "skipped", 1002: "sent"})

    async def test_sold_out_product_is_not_advertised_with_buy_button(self):
        self.shop.store.ensure_user(1001)
        product_id = self.shop.store.add_product("Tài khoản mới", 49_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.bulk_add_stock(
            product_id, ["stock-1"], announce_except=frozenset(),
        )
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            conn.execute(
                "UPDATE stock SET sold_at='2026-01-01T00:00:00Z' WHERE product_id=?",
                (product_id,),
            )
            conn.commit()

        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_stock_announcements(bot)

        bot.send_message.assert_not_awaited()
        self.assertEqual(self.shop.store.list_due_stock_announcements(), [])

    async def test_admin_sale_announces_like_example_and_buy_opens_sale_price(self):
        for user_id in (1, 1001, 1002):
            self.shop.store.ensure_user(user_id)
        product_id = self.shop.store.add_product("Link Duo <Pro>", 20_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.set_product_following(1002, product_id, True)
        self.shop.store.bulk_add_stock(
            product_id, [f"stock-{index}" for index in range(45)],
        )
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(args=[str(product_id), "19000", "40"], user_data={})

        await self.shop.start_sale(update, context)

        self.assertIn("2 khách", message.reply_text.await_args.args[0])
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_sale_announcements(bot)
        await self.shop.send_pending_sale_announcements(bot)
        self.assertEqual({call.args[0] for call in bot.send_message.await_args_list}, {1001, 1002})
        for call in bot.send_message.await_args_list:
            text = call.args[1]
            self.assertIn("<b>🔥 SALE SỐC!", text)
            self.assertIn("Link Duo &lt;Pro&gt;", text)
            self.assertIn("Giá gốc: <s>20.000đ</s>", text)
            self.assertIn("Giá sale: 19.000đ (-5%)", text)
            self.assertIn("40 suất đầu tiên", text)
            self.assertIn("Tồn kho: 45", text)
            self.assertEqual(call.kwargs["parse_mode"], "HTML")
            button = call.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual((button.text, button.callback_data),
                             ("🛒 Mua ngay", f"buy:{product_id}"))

        customer_query = SimpleNamespace(
            answer=AsyncMock(), data=f"buy:{product_id}", message=self.message,
        )
        customer_update = SimpleNamespace(
            callback_query=customer_query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        customer_context = SimpleNamespace(user_data={})
        await self.shop.callbacks(customer_update, customer_context)
        self.assertIn("Chọn số lượng", self.message.reply_text.await_args.args[0])
        customer_query.data = f"buyqty:{product_id}:1"
        await self.shop.callbacks(customer_update, customer_context)
        confirmation = self.message.reply_text.await_args
        self.assertIn("Cần thanh toán: 19.000đ", confirmation.args[0])
        self.assertNotIn("Nhập mã giảm giá", str(confirmation.kwargs["reply_markup"]))

    async def test_stock_announcement_uses_current_sale_price(self):
        self.shop.store.ensure_user(1001)
        product_id = self.shop.store.add_product("Account", 20_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.bulk_add_stock(
            product_id, ["stock-1"], announce_except=frozenset(),
        )
        self.shop.store.start_sale(product_id, 19_000, 1)

        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_stock_announcements(bot)

        self.assertIn("🔥 Giá sale: <s>20.000đ</s> 19.000đ",
                      bot.send_message.await_args.args[1])
        self.assertNotIn("💰 Giá: 20.000đ", bot.send_message.await_args.args[1])

    async def test_stock_announcement_shows_old_price_after_manual_drop(self):
        self.shop.store.ensure_user(1001)
        product_id = self.shop.store.add_product("Account", 20_000)
        self.shop.store.set_product_following(1001, product_id, True)
        self.shop.store.set_price(product_id, 15_000)
        self.shop.store.bulk_add_stock(
            product_id, ["private-stock"], announce_except=frozenset(),
        )

        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_stock_announcements(bot)
        notice = bot.send_message.await_args
        self.assertIn("💰 Giá: <s>20.000đ</s> 15.000đ", notice.args[1])
        self.assertNotIn("private-stock", notice.args[1])
        self.assertEqual(notice.kwargs["parse_mode"], "HTML")

    async def test_ending_sale_cancels_failed_and_waiting_announcements(self):
        self.shop.store.ensure_user(1001)
        product_id = self.shop.store.add_product("Account", 20_000)
        self.shop.store.add_stock(product_id, "stock-1")
        self.shop.store.set_product_following(1001, product_id, True)
        sale = self.shop.store.start_sale(
            product_id, 19_000, 1, announce_except=frozenset(),
        )
        failed_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        await self.shop.send_pending_sale_announcements(failed_bot)
        failed_bot.send_message.assert_awaited_once()

        self.shop.store.end_sale(product_id)

        self.assertEqual(self.shop.store.list_due_sale_announcements(), [])
        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            status = conn.execute(
                "SELECT status FROM sale_announcement_deliveries WHERE sale_id=?",
                (sale["id"],),
            ).fetchone()[0]
        self.assertEqual(status, "skipped")
        working_bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_sale_announcements(working_bot)
        working_bot.send_message.assert_not_awaited()

    async def test_failed_product_notification_retries_after_restart(self):
        self.shop.store.ensure_user(1001)
        product_id = self.shop.store.add_product("Tài khoản mới", 49_000)
        self.shop.store.set_product_following(1001, product_id, True)
        batch = self.shop.store.bulk_add_stock(
            product_id, ["stock-1"], announce_except=frozenset(),
        )
        failed_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        await self.shop.send_pending_stock_announcements(failed_bot)
        failed_bot.send_message.assert_awaited_once()
        self.assertEqual(self.shop.store.list_due_stock_announcements(), [])

        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            conn.execute(
                """UPDATE stock_announcement_deliveries
                   SET next_attempt_at='2000-01-01T00:00:00.000Z'
                   WHERE batch_id=?""",
                (batch["announcement_id"],),
            )
            conn.commit()
        restarted_shop = ShopBot(self.shop.config, self.shop.store)
        working_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted_shop.send_pending_stock_announcements(working_bot)
        working_bot.send_message.assert_awaited_once()
        self.assertEqual(self.shop.store.list_due_stock_announcements(), [])

    async def test_admin_previews_and_confirms_broadcast_without_including_admins(self):
        for user_id in (1, 1001, 1002):
            self.shop.store.ensure_user(user_id)
        self.message.text = "/broadcast Ưu đãi hôm nay\nGiảm 10%"
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={}, args=[])

        await self.shop.broadcast(update, context)
        preview = self.message.reply_text.await_args
        self.assertIn("2 khách", preview.args[0])
        self.assertIn("Ưu đãi hôm nay\nGiảm 10%", preview.args[0])
        self.assertIsNone(self.shop.store.get_broadcast_status())
        button = preview.kwargs["reply_markup"].inline_keyboard[0][0]
        query = SimpleNamespace(data=button.callback_data, answer=AsyncMock(), message=self.message)
        update.callback_query = query
        await self.shop.callbacks(update, context)

        status = self.shop.store.get_broadcast_status()
        self.assertEqual((status["recipients"], status["pending"], status["sent"]), (2, 2, 0))
        self.assertEqual({item["user_id"] for item in self.shop.store.list_due_broadcast_deliveries()},
                         {1001, 1002})
        await self.shop.callbacks(update, context)
        self.assertIn("không hợp lệ", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_broadcast_status()["id"], status["id"])

        await self.shop.broadcast_status(update, context)
        self.assertIn("Chưa gửi: 2", self.message.reply_text.await_args.args[0])

    async def test_broadcast_requires_admin_and_can_be_cancelled(self):
        self.shop.store.ensure_user(1001)
        self.message.text = "/broadcast Không được gửi"
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={}, args=[])
        await self.shop.broadcast(update, context)
        self.assertIn("chỉ dành cho quản trị viên", self.message.reply_text.await_args.args[0])
        self.assertIsNone(self.shop.store.get_broadcast_status())

        update.effective_user.id = 1
        query = SimpleNamespace(data="admin:broadcast", answer=AsyncMock(), message=self.message)
        update.callback_query = query
        await self.shop.callbacks(update, context)
        self.assertTrue(context.user_data["awaiting_broadcast"])
        self.message.text = "Thông báo thử"
        await self.shop.text_message(update, context)
        send_button = self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        query.data = "admin:broadcast:cancel"
        await self.shop.callbacks(update, context)
        query.data = send_button.callback_data
        await self.shop.callbacks(update, context)
        self.assertIsNone(self.shop.store.get_broadcast_status())

    async def test_broadcast_rejects_oversized_text_and_expired_confirmation(self):
        self.shop.store.ensure_user(1001)
        self.message.text = "/broadcast " + "😀" * 1800
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={})
        await self.shop.broadcast(update, context)
        self.assertIn("3500", self.message.reply_text.await_args.args[0])
        self.assertIsNone(self.shop.store.get_broadcast_status())

        self.message.text = "Tin hợp lệ"
        await self.shop.text_message(update, context)
        nonce, content, _ = context.user_data["pending_broadcast"]
        context.user_data["pending_broadcast"] = (nonce, content, 0)
        await self.shop.confirm_broadcast(self.message, context, 1, nonce)
        self.assertIsNone(self.shop.store.get_broadcast_status())

    async def test_broadcast_delivery_tracks_blocked_customers(self):
        for user_id in (1, 1001, 1002):
            self.shop.store.ensure_user(user_id)
        batch = self.shop.store.create_broadcast(1, "Hàng mới đã về", frozenset({1}))

        async def send(chat_id, *_args):
            if chat_id == 1001:
                raise Forbidden("bot was blocked")

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        await self.shop.send_pending_broadcasts(bot)
        status = self.shop.store.get_broadcast_status(batch["id"])
        self.assertEqual((status["pending"], status["sent"], status["failed"]), (0, 1, 1))
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertTrue(all("Hàng mới đã về" in call.args[1]
                            for call in bot.send_message.await_args_list))
        await self.shop.send_pending_broadcasts(bot)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_broadcast_retries_after_restart_and_respects_telegram_pause(self):
        self.shop.store.ensure_user(1001)
        batch = self.shop.store.create_broadcast(1, "Tin thử", frozenset({1}))
        limited_bot = SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(2)))
        await self.shop.send_pending_broadcasts(limited_bot)
        self.assertGreater(self.shop.announcement_pause_until, 0)
        self.assertEqual(self.shop.store.get_broadcast_status(batch["id"])["pending"], 1)

        with closing(sqlite3.connect(self.shop.store.path)) as conn:
            conn.execute(
                """UPDATE broadcast_deliveries
                   SET next_attempt_at='2000-01-01T00:00:00.000Z'
                   WHERE batch_id=?""",
                (batch["id"],),
            )
            conn.commit()
        restarted_store = Store(
            self.shop.config.database_path, self.shop.config.inventory_encryption_key,
        )
        restarted_shop = ShopBot(self.shop.config, restarted_store)
        working_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted_shop.send_pending_broadcasts(working_bot)
        working_bot.send_message.assert_awaited_once()
        self.assertEqual(restarted_store.get_broadcast_status(batch["id"])["sent"], 1)

    async def test_admin_menu_button_is_hidden_and_inventory_reveal_is_private(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        stock_id = self.shop.store.add_stock(product_id, "private-login:password")
        await self.shop.send_menu(self.message, 1)
        buttons = [
            button.callback_data
            for row in self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertNotIn("admin:home", buttons)

        self.message.reply_text.reset_mock()
        await self.shop.send_admin_inventory(self.message, product_id)
        inventory_text = self.message.reply_text.await_args.args[0]
        self.assertIn(f"Kho #{stock_id}", inventory_text)
        self.assertIn("private-login:password", inventory_text)
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])
        self.assertTrue(self.message.reply_text.await_args.kwargs[
            "link_preview_options"].is_disabled)

        self.message.reply_text.reset_mock()
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(args=[str(stock_id)])
        await self.shop.admin_stock_item(update, context)
        self.assertNotIn("private-login:password", self.message.reply_text.await_args.args[0])

        self.message.reply_text.reset_mock()
        update.callback_query = SimpleNamespace(
            answer=AsyncMock(), data=f"admin:inventory:{product_id}:0", message=self.message,
        )
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertNotIn("private-login:password", self.message.reply_text.await_args.args[0])

        update.effective_user.id = 1
        await self.shop.admin_stock_item(update, context)
        self.assertIn("private-login:password", self.message.reply_text.await_args.args[0])
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])

    async def test_admin_can_edit_and_archive_product(self):
        product_id = self.shop.store.add_product("Old name", 10_000, "Old description")
        message = SimpleNamespace(
            text=f"/editproduct {product_id} | New name | New description",
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.edit_product(update, None)
        product = self.shop.store.get_product(product_id)
        self.assertEqual((product["name"], product["description"]), ("New name", "New description"))

        await self.shop.delete_product(update, SimpleNamespace(args=[str(product_id)]))
        self.assertTrue(self.shop.store.get_product(product_id)["deleted"])
        await self.shop.restore_product(update, SimpleNamespace(args=[str(product_id)]))
        restored = self.shop.store.get_product(product_id)
        self.assertFalse(restored["deleted"])
        self.assertFalse(restored["active"])

    async def test_admin_views_report_sales_and_bank_credits_without_secrets(self):
        product_id = self.shop.store.add_product("Account", 10_000, "One month")
        self.shop.store.add_stock(product_id, "private-login:password")
        topup = self.shop.store.create_topup(1001, 20_000)
        self.shop.store.record_bank_event(
            "bank-1001", "0123456789", 20_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        self.shop.store.purchase(1001, product_id)

        await self.shop.send_shop(self.message, 0)
        self.assertNotIn("Đã bán", self.message.reply_text.await_args.args[0])
        await self.shop.send_product_detail(self.message, 1001, product_id)
        self.assertIn("Đã bán: 1", self.message.reply_text.await_args.args[0])

        for sender, expected in (
            (self.shop.send_admin_users, "ID 1001"),
            (self.shop.send_admin_sales, "ID 1001 · 1 tài khoản"),
            (self.shop.send_admin_topups, "đã nhận 20.000đ"),
            (self.shop.send_admin_transactions, "đã cộng"),
            (self.shop.send_admin_products, "Account · 10.000đ · còn 0 · đã bán 1"),
        ):
            self.message.reply_text.reset_mock()
            await sender(self.message)
            output = self.message.reply_text.await_args.args[0]
            self.assertIn(expected, output)
            self.assertNotIn("private-login:password", output)

        self.message.reply_text.reset_mock()
        await self.shop.send_admin_product(self.message, product_id)
        self.assertIn("1 đã bán", self.message.reply_text.await_args.args[0])

        self.message.reply_text.reset_mock()
        await self.shop.send_admin_user(self.message, 1001)
        self.assertIn("Tổng đã mua: 10.000đ", self.message.reply_text.await_args.args[0])

    async def test_users_shows_total_unique_customers_on_every_page(self):
        self.shop.config = replace(self.shop.config, admin_ids=frozenset({1, 2}))
        for admin_id in (1, 2):
            self.shop.store.ensure_user(admin_id)
        await self.shop.send_admin_users(self.message)
        self.assertIn("Tổng khách từng truy cập: 0", self.message.reply_text.await_args.args[0])

        for customer_id in range(1001, 1010):
            self.shop.store.ensure_user(customer_id)
        self.assertEqual(self.shop.store.count_users(frozenset({1, 2})), 9)
        self.assertEqual(len(self.shop.store.list_users(20, 0, frozenset({1, 2}))), 9)

        await self.shop.send_admin_users(self.message, 0)
        first_page = self.message.reply_text.await_args.args[0]
        self.assertIn("Tổng khách từng truy cập: 9", first_page)
        self.assertNotIn("ID 1 ·", first_page)
        self.assertNotIn("ID 2 ·", first_page)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:users:1", buttons)

        await self.shop.send_admin_users(self.message, 1)
        second_page = self.message.reply_text.await_args.args[0]
        self.assertIn("Tổng khách từng truy cập: 9", second_page)
        self.assertEqual(sum(" · Số dư " in line for line in second_page.splitlines()), 1)

    async def test_user_search_matches_id_username_and_vietnamese_name(self):
        self.shop.store.ensure_user(1, "alice_admin", "Nguyễn Admin")
        self.shop.store.ensure_user(1001, "AliceShop", "Nguyễn Văn An")
        self.shop.store.ensure_user(1002, "other", "Alice Customer")
        excluded = self.shop.config.admin_ids

        self.assertEqual(
            [user["telegram_id"] for user in
             self.shop.store.search_users("1001", exclude_ids=excluded)["users"]],
            [1001],
        )
        self.assertEqual(
            [user["telegram_id"] for user in
             self.shop.store.search_users("@ALICE", exclude_ids=excluded)["users"]],
            [1001],
        )
        self.assertEqual(
            [user["telegram_id"] for user in
             self.shop.store.search_users("nguyen van", exclude_ids=excluded)["users"]],
            [1001],
        )
        self.assertEqual(
            self.shop.store.search_users("admin", exclude_ids=excluded)["total"], 0,
        )
        with self.assertRaises(ValueError):
            self.shop.store.search_users("@", exclude_ids=excluded)

    async def test_user_search_paginates_and_opens_customer(self):
        self.shop.store.ensure_user(1, "buyer_admin", "Shop admin")
        for user_id in range(1001, 1011):
            self.shop.store.ensure_user(user_id, f"buyer{user_id}", "Khách hàng")
        first = self.shop.store.search_users("@buyer", 8, 0, self.shop.config.admin_ids)
        second = self.shop.store.search_users("@buyer", 8, 8, self.shop.config.admin_ids)
        self.assertEqual((first["total"], second["total"]), (10, 10))
        self.assertEqual((len(first["users"]), len(second["users"])), (8, 2))
        self.assertFalse({user["telegram_id"] for user in first["users"]}
                         & {user["telegram_id"] for user in second["users"]})

        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                answer=AsyncMock(), data="admin:usersearch", message=self.message,
            ),
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
            message=self.message,
        )
        await self.shop.callbacks(update, context)
        self.assertTrue(context.user_data["awaiting_admin_user_search"])
        self.message.text = "@buyer"
        await self.shop.text_message(update, context)
        self.assertIn("Tìm thấy: 10 khách", self.message.reply_text.await_args.args[0])
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:usersearch:1", buttons)
        self.assertIn("admin:user:1010", buttons)

        update.callback_query.data = "admin:usersearch:1"
        await self.shop.callbacks(update, context)
        self.assertIn("Tìm thấy: 10 khách", self.message.reply_text.await_args.args[0])
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:user:1002", buttons)
        self.assertNotIn("admin:usersearch:2", buttons)

        update.callback_query.data = "admin:user:1002"
        await self.shop.callbacks(update, context)
        self.assertIn("1002", self.message.reply_text.await_args.args[0])

    async def test_non_admin_cannot_search_users(self):
        self.shop.store.ensure_user(1001, "alice", "Alice")
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                answer=AsyncMock(), data="admin:usersearch", message=self.message,
            ),
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={})
        await self.shop.callbacks(update, context)
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])
        self.assertNotIn("awaiting_admin_user_search", context.user_data)

    async def test_sales_search_finds_buyer_and_keeps_multi_item_order_together(self):
        self.shop.store.ensure_user(1, "shop_admin", "Shop")
        self.shop.store.ensure_user(1001, "AliceShop", "Nguyễn Văn An")
        self.shop.store.ensure_user(1002, "other", "Khách khác")
        for user_id in (1001, 1002):
            self.shop.store.credit_test_balance(
                user_id, 50_000, 1, 0, f"search-credit-{user_id}",
            )
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product_id, ["first", "second", "third"])
        self.shop.store.set_requires_email(product_id, True)
        alice = self.shop.store.purchase(
            1001, product_id, customer_email="one@example.com, two@example.com",
        )
        other = self.shop.store.purchase(
            1002, product_id, customer_email="three@example.com",
        )

        for term in ("1001", "@ALICE", "nguyen van"):
            self.assertEqual(self.shop.store.count_purchases_by_buyer(term), 1)
            matches = self.shop.store.list_all_purchases(buyer_query=term)
            self.assertEqual([(order["id"], len(order["items"])) for order in matches],
                             [(alice["id"], 2)])
        self.assertEqual(self.shop.store.count_purchases_by_buyer("1002"), 1)
        self.assertEqual(self.shop.store.list_all_purchases(buyer_query="1002")[0]["id"],
                         other["id"])
        self.assertEqual(self.shop.store.count_purchases_by_buyer("nobody"), 0)
        with self.assertRaises(ValueError):
            self.shop.store.list_all_purchases(buyer_query="@")

        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                answer=AsyncMock(), data="admin:sales:0", message=self.message,
            ),
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
            message=self.message,
        )
        await self.shop.callbacks(update, context)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:salessearch", buttons)

        update.callback_query.data = "admin:salessearch"
        await self.shop.callbacks(update, context)
        self.assertTrue(context.user_data["awaiting_admin_sales_search"])
        self.message.text = "@"
        await self.shop.text_message(update, context)
        self.assertIn("Từ khóa không hợp lệ", self.message.reply_text.await_args.args[0])
        self.assertTrue(context.user_data["awaiting_admin_sales_search"])
        self.message.text = "nguyen van"
        await self.shop.text_message(update, context)
        output = self.message.reply_text.await_args.args[0]
        self.assertIn("Tìm thấy: 1 đơn", output)
        self.assertIn("2 tài khoản", output)
        self.assertIn(f"#{alice['id']}", output)
        self.assertNotIn(f"#{other['id']} ·", output)
        self.assertNotIn("first", output)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn(f"admin:purchase:{alice['id']}", buttons)
        self.assertIn("admin:user:1001", buttons)

    async def test_sales_search_paginates_and_denies_non_admin(self):
        self.shop.store.ensure_user(1, "shop_admin", "Shop")
        self.shop.store.ensure_user(1001, "buyer_1001", "Người mua")
        self.shop.store.credit_test_balance(1001, 100_000, 1, 0,
                                            "search-credit-many")
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(product_id,
                                       [f"secret-{index}" for index in range(9)])
        for _ in range(9):
            self.shop.store.purchase(1001, product_id)
        self.assertEqual(self.shop.store.count_purchases_by_buyer("@BUYER"), 9)
        self.assertEqual(len(self.shop.store.list_all_purchases(
            8, 0, buyer_query="@BUYER")), 8)
        self.assertEqual(len(self.shop.store.list_all_purchases(
            8, 8, buyer_query="@BUYER")), 1)

        context = SimpleNamespace(user_data={"admin_sales_search_query": "@BUYER"})
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                answer=AsyncMock(), data="admin:salessearch:0", message=self.message,
            ),
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.callbacks(update, context)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:salessearch:1", buttons)
        update.callback_query.data = "admin:salessearch:1"
        await self.shop.callbacks(update, context)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:salessearch:0", buttons)
        self.assertNotIn("admin:salessearch:2", buttons)

        update.effective_user.id = 1001
        update.callback_query.data = "admin:salessearch"
        self.message.reply_text.reset_mock()
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])

    async def test_non_admin_cannot_open_admin_callback(self):
        query = SimpleNamespace(answer=AsyncMock(), data="admin:users:0", message=self.message)
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))

        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])
        self.assertNotIn("NGƯỜI DÙNG", self.message.reply_text.await_args.args[0])

    async def test_admin_status_shows_fresh_sources_and_flags_stale_checks(self):
        self.shop.config = replace(self.shop.config, sepay_api_token="test-api-token")
        self.shop.server_thread = SimpleNamespace(is_alive=lambda: True)
        product_id = self.shop.store.add_product("Low stock", 10_000)
        self.shop.store.add_stock(product_id, "only account")
        self.shop.store.claim_low_stock_alert(product_id)
        self.shop.store.record_bank_event(
            "status-unmatched", "0123456789", 20_000, "wrong code", "in", None,
            "0123456789", via_webhook=True,
        )
        self.shop.store.mark_reconciled("0123456789", 123)
        backup_dir = Path(self.temp.name) / "backups"
        backup = backup_and_verify(
            self.shop.config.database_path, backup_dir,
            self.shop.config.inventory_encryption_key,
        )
        update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        with patch("bot.backup_directory", return_value=backup_dir):
            await self.shop.status(update, None)
            text = self.message.reply_text.await_args.args[0]
            self.assertIn("Webhook nội bộ: đang chạy", text)
            self.assertIn("Webhook SePay xử lý gần nhất:", text)
            self.assertNotIn("chưa ghi nhận", text)
            self.assertIn("Giao dịch chưa khớp: 1", text)
            self.assertIn(f"#{product_id} Low stock: còn 1", text)
            self.assertIn("Bản sao lưu đã kiểm tra gần nhất:", text)
            self.assertNotIn("quá 10 phút", text)
            self.assertNotIn("quá 36 giờ", text)
            buttons = [button.callback_data for row in
                       self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                       for button in row]
            self.assertIn("admin:status", buttons)

            old = datetime(2020, 1, 1, tzinfo=timezone.utc)
            os.utime(backup, (old.timestamp(), old.timestamp()))
            with closing(sqlite3.connect(self.shop.store.path)) as conn:
                conn.execute(
                    "UPDATE reconciliation_state SET last_success_at=? WHERE account_number=?",
                    ("2020-01-01T00:00:00Z", "0123456789"),
                )
                conn.commit()
            update.callback_query = SimpleNamespace(
                answer=AsyncMock(), data="admin:status", message=self.message,
            )
            await self.shop.callbacks(update, SimpleNamespace(user_data={}))
            text = self.message.reply_text.await_args.args[0]
            self.assertIn("quá 10 phút, cần kiểm tra", text)
            self.assertIn("quá 36 giờ, cần kiểm tra", text)

    async def test_status_is_admin_only_and_missing_data_is_not_called_healthy(self):
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.shop.status(update, None)
        self.assertIn("chỉ dành cho quản trị viên", self.message.reply_text.await_args.args[0])
        update.effective_user.id = 1
        with patch("bot.backup_directory", return_value=Path(self.temp.name) / "none"):
            await self.shop.status(update, None)
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("chưa ghi nhận từ khi cập nhật", text)
        self.assertIn("chưa cấu hình API Token", text)
        self.assertIn("chưa thấy bản sao", text)

    async def test_admin_matches_unmatched_transfer_and_customer_is_notified_once(self):
        self.shop.store.ensure_user(1001, "alice", "Alice")
        topup = self.shop.store.create_topup(1001, 50_000)
        self.shop.store.record_bank_event(
            "sepay:manual-1", "0123456789", 49_000, "Sai ma nap", "in", None,
            "0123456789",
        )
        event_id = self.shop.store.list_unmatched_events()[0]["id"]
        message = SimpleNamespace(text="", reply_text=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data="admin:unmatched:0", message=message)
        update = SimpleNamespace(
            callback_query=query, message=message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={})
        telegram_bot = SimpleNamespace(send_message=AsyncMock())
        self.shop.application = SimpleNamespace(bot=telegram_bot)

        await self.shop.callbacks(update, context)
        buttons = [button.callback_data for row in
                   message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn(f"admin:match:{event_id}", buttons)

        query.data = f"admin:match:{event_id}"
        await self.shop.callbacks(update, context)
        self.assertEqual(context.user_data["awaiting_manual_credit"], event_id)
        message.text = topup["code"].lower()
        await self.shop.text_message(update, context)
        preview = message.reply_text.await_args.args[0]
        self.assertIn("49.000đ", preview)
        self.assertIn("@alice (ID 1001)", preview)
        self.assertIn(topup["code"], preview)
        self.assertEqual(self.shop.store.get_balance(1001), 0)
        buttons = [button.callback_data for row in
                   message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        confirm = next(button for button in buttons if button.startswith("admin:credit:"))

        query.data = confirm
        update.effective_user.id = 1001
        await self.shop.callbacks(update, SimpleNamespace(user_data={}))
        self.assertIn("Chỉ quản trị viên", message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_balance(1001), 0)

        update.effective_user.id = 1
        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_balance(1001), 49_000)
        self.assertEqual(self.shop.store.list_topups(1001)[0]["paid_amount"], 49_000)
        self.assertEqual(self.shop.store.list_unmatched_events(), [])
        audit = self.shop.store.list_manual_credits()[0]
        self.assertEqual((audit["bank_event_id"], audit["admin_id"], audit["user_id"]),
                         (event_id, 1, 1001))
        telegram_bot.send_message.assert_awaited_once()
        self.assertEqual(telegram_bot.send_message.await_args.args[0], 1001)

        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_balance(1001), 49_000)
        self.assertEqual(len(self.shop.store.list_manual_credits()), 1)
        telegram_bot.send_message.assert_awaited_once()
        await self.shop.send_admin_manual_credits(message)
        self.assertIn("sepay:manual-1", message.reply_text.await_args.args[0])

    async def test_manual_credit_requires_live_preview_and_can_be_cancelled(self):
        topup = self.shop.store.create_topup(1001, 50_000)
        self.shop.store.record_bank_event(
            "sepay:manual-2", "0123456789", 49_000, "Sai ma nap", "in", None,
            "0123456789",
        )
        event_id = self.shop.store.list_unmatched_events()[0]["id"]
        message = SimpleNamespace(text=topup["code"], reply_text=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data=f"admin:match:{event_id}", message=message)
        update = SimpleNamespace(
            callback_query=query, message=message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={})

        await self.shop.callbacks(update, context)
        await self.shop.text_message(update, context)
        pending = context.user_data["pending_manual_credit"]
        confirm = f"admin:credit:{event_id}:{pending[0]}"
        query.data = f"admin:credit:{event_id}:wrong"
        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_balance(1001), 0)
        self.assertNotIn("pending_manual_credit", context.user_data)

        query.data = f"admin:match:{event_id}"
        await self.shop.callbacks(update, context)
        await self.shop.text_message(update, context)
        pending = context.user_data["pending_manual_credit"]
        context.user_data["pending_manual_credit"] = (*pending[:4], 0)
        query.data = f"admin:credit:{event_id}:{pending[0]}"
        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_balance(1001), 0)

        query.data = f"admin:match:{event_id}"
        await self.shop.callbacks(update, context)
        await self.shop.text_message(update, context)
        query.data = "admin:matchcancel"
        await self.shop.callbacks(update, context)
        self.assertNotIn("pending_manual_credit", context.user_data)
        query.data = confirm
        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_balance(1001), 0)
        self.assertEqual(self.shop.store.list_manual_credits(), [])

    async def test_admin_can_navigate_from_products_to_one_stock_item(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        stock_id = self.shop.store.add_stock(product_id, "private-login:password")
        query = SimpleNamespace(answer=AsyncMock(), data="", message=self.message)
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={})

        for data, expected in (
            ("admin:home", "QUẢN TRỊ SHOP"),
            ("admin:products:0", "SẢN PHẨM"),
            (f"admin:product:{product_id}", "Kho: 1 chưa bán"),
            (f"admin:inventory:{product_id}:0", "private-login:password"),
        ):
            self.message.reply_text.reset_mock()
            query.data = data
            await self.shop.callbacks(update, context)
            self.assertIn(expected, self.message.reply_text.await_args.args[0])
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertNotIn(f"admin:stock:{stock_id}", buttons)

    async def test_admin_inventory_shows_secrets_with_page_navigation(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        ids = [self.shop.store.add_stock(product_id, f"login-{index}:secret")
               for index in range(1, 10)]

        await self.shop.send_admin_inventory(self.message, product_id, 0)
        first = self.message.reply_text.await_args
        self.assertIn(f"Kho #{ids[0]}\nlogin-1:secret", first.args[0])
        self.assertIn("login-8:secret", first.args[0])
        self.assertNotIn("login-9:secret", first.args[0])
        self.assertTrue(first.kwargs["protect_content"])
        first_buttons = [button.callback_data for row in first.kwargs["reply_markup"].inline_keyboard
                         for button in row]
        self.assertIn(f"admin:inventory:{product_id}:1", first_buttons)
        self.assertFalse(any(data.startswith("admin:stock:") for data in first_buttons))

        await self.shop.send_admin_inventory(self.message, product_id, 1)
        second = self.message.reply_text.await_args
        self.assertIn(f"Kho #{ids[-1]}\nlogin-9:secret", second.args[0])
        self.assertNotIn("login-1:secret", second.args[0])
        second_buttons = [button.callback_data for row in second.kwargs["reply_markup"].inline_keyboard
                          for button in row]
        self.assertIn(f"admin:inventory:{product_id}:0", second_buttons)

    async def test_admin_inventory_splits_long_secret_without_truncation(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        secret = "🔑" * 2500
        self.shop.store.add_stock(product_id, secret)

        await self.shop.send_admin_inventory(self.message, product_id)

        messages = self.message.reply_text.await_args_list
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(call.kwargs["protect_content"] for call in messages))
        self.assertTrue(all(telegram_text_units(call.args[0]) <= INVENTORY_TEXT_MAX_UNITS
                            for call in messages))
        self.assertEqual("".join(call.args[0].split("\n", 1)[1]
                                 if call.args[0].startswith("Kho #") else
                                 call.args[0].split("\n\nKho #", 1)[1].split("\n", 1)[1]
                                 for call in messages), secret)

    async def test_admin_product_buttons_create_once_and_reject_customer(self):
        message = SimpleNamespace(text="", reply_text=AsyncMock(),
                                  reply_document=AsyncMock(), delete=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data="admin:catalog:new", message=message)
        update = SimpleNamespace(
            callback_query=query, message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        await self.shop.callbacks(update, context)
        self.assertNotIn("admin_product_flow", context.user_data)
        self.assertEqual(self.shop.store.list_products(), [])

        update.effective_user.id = 1
        await self.shop.callbacks(update, context)
        self.assertEqual(context.user_data["admin_product_flow"]["step"], "name")
        message.text = "Tài khoản mới"
        await self.shop.text_message(update, context)
        message.text = "0"
        await self.shop.text_message(update, context)
        self.assertEqual(context.user_data["admin_product_flow"]["step"], "price")
        message.text = "30k"
        await self.shop.text_message(update, context)
        query.data = next(button.callback_data for row in
                          message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                          for button in row if button.text == "Bỏ qua mô tả")
        await self.shop.callbacks(update, context)
        query.data = next(button.callback_data for row in
                          message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                          for button in row if button.text == "Tạo sản phẩm")
        await self.shop.callbacks(update, context)
        created = self.shop.store.list_products()
        self.assertEqual(len(created), 1)
        self.assertEqual((created[0]["name"], created[0]["price"]), ("Tài khoản mới", 30_000))
        await self.shop.callbacks(update, context)
        self.assertEqual(len(self.shop.store.list_products()), 1)

    async def test_admin_product_buttons_edit_archive_and_restore(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.add_stock(product_id, "stock-secret")
        message = SimpleNamespace(text="", reply_text=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data="", message=message)
        update = SimpleNamespace(
            callback_query=query, message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={})
        query.data = f"admin:catalog:edit:{product_id}:price"
        await self.shop.callbacks(update, context)
        message.text = "8k"
        await self.shop.text_message(update, context)
        self.assertEqual(self.shop.store.get_product(product_id)["price"], 8_000)

        query.data = f"admin:catalog:visibility:{product_id}"
        await self.shop.callbacks(update, context)
        self.assertFalse(self.shop.store.get_product(product_id)["active"])
        await self.shop.callbacks(update, context)
        self.assertTrue(self.shop.store.get_product(product_id)["active"])

        query.data = f"admin:catalog:archive:{product_id}"
        await self.shop.callbacks(update, context)
        self.assertFalse(self.shop.store.get_product(product_id)["deleted"])
        query.data = next(button.callback_data for row in
                          message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                          for button in row if button.text == "Xác nhận")
        await self.shop.callbacks(update, context)
        self.assertTrue(self.shop.store.get_product(product_id)["deleted"])
        query.data = f"admin:catalog:restore:{product_id}"
        await self.shop.callbacks(update, context)
        restored = self.shop.store.get_product(product_id)
        self.assertFalse(restored["deleted"])
        self.assertFalse(restored["active"])
        self.assertEqual(restored["available"], 1)

    async def test_admin_product_buttons_import_stock_and_confirm_sale(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.ensure_user(1001)
        self.shop.store.set_product_following(1001, product_id, True)
        message = SimpleNamespace(text="", reply_text=AsyncMock(), delete=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data=f"admin:catalog:stock:{product_id}",
                                message=message)
        update = SimpleNamespace(
            callback_query=query, message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        await self.shop.callbacks(update, context)
        self.assertEqual(context.user_data["awaiting_bulk_stock"], product_id)
        message.text = "login:secret"
        await self.shop.text_message(update, context)
        message.delete.assert_awaited_once()
        self.assertEqual(self.shop.store.get_product(product_id)["available"], 1)
        self.assertNotIn("login:secret", context.bot.send_message.await_args.args[1])

        query.data = f"admin:catalog:sale:{product_id}"
        await self.shop.callbacks(update, context)
        message.text = "8k"
        await self.shop.text_message(update, context)
        message.text = "1"
        await self.shop.text_message(update, context)
        self.assertIsNone(self.shop.store.get_product(product_id)["sale_id"])
        query.data = next(button.callback_data for row in
                          message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                          for button in row if button.text == "Bắt đầu sale")
        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_product(product_id)["sale_price"], 8_000)
        self.assertEqual(len(self.shop.store.list_due_sale_announcements()), 1)

    async def test_bulk_stock_message_is_deleted_and_saves_one_account_per_line(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        message = SimpleNamespace(
            text=f"/bulkstock {product_id}", reply_text=AsyncMock(), delete=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(user_data={}, bot=bot)

        await self.shop.bulk_stock(update, context)
        self.assertEqual(context.user_data["awaiting_bulk_stock"], product_id)
        message.text = "login-one:secret\nlogin-two:secret"
        await self.shop.text_message(update, context)

        message.delete.assert_awaited_once()
        self.assertEqual(self.shop.store.stock_counts(product_id)["available"], 2)
        self.assertNotIn("awaiting_bulk_stock", context.user_data)
        response = bot.send_message.await_args.args[1]
        self.assertIn("Đã nhập 2 tài khoản", response)
        self.assertNotIn("login-one", response)

    async def test_customer_warranty_and_admin_reply_flow(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.add_stock(product_id, "private-login:password")
        topup = self.shop.store.create_topup(1001, 20_000)
        self.shop.store.record_bank_event(
            "warranty-bank", "0123456789", 20_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        self.shop.store.set_user_language(1001, "en")
        order = self.shop.store.purchase(1001, product_id)
        message = SimpleNamespace(text="", reply_text=AsyncMock(),
                                  reply_document=AsyncMock(), delete=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data=f"order:{order['id']}", message=message)
        update = SimpleNamespace(
            callback_query=query, message=message, effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(user_data={}, bot=bot)

        await self.shop.callbacks(update, context)
        buttons = [
            button.callback_data
            for row in message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn(f"warranty:start:{order['id']}", buttons)
        query.data = f"warranty:start:{order['id']}"
        await self.shop.callbacks(update, context)
        self.assertEqual(context.user_data["awaiting_warranty"], order["id"])
        self.assertIn("Describe the issue", message.reply_text.await_args.args[0])

        message.text = "Cannot log in"
        await self.shop.text_message(update, context)
        message.delete.assert_awaited_once()
        self.assertNotIn("awaiting_warranty", context.user_data)
        ticket = self.shop.store.list_warranties()[0]
        self.assertEqual(ticket["order_id"], order["id"])
        self.assertTrue(any(call.args[0] == 1 for call in bot.send_message.await_args_list))
        self.assertTrue(any(
            call.args[0] == 1001 and "Warranty request" in call.args[1]
            for call in bot.send_message.await_args_list
        ))

        message.text = f"/resolvewarranty {ticket['id']} | Please try the replacement"
        update.effective_user.id = 1
        await self.shop.admin_resolve_warranty(update, context)
        self.assertEqual(self.shop.store.get_warranty(ticket["id"])["status"], "resolved")
        self.assertEqual(
            self.shop.store.get_latest_warranty(1001, order["id"])["reply"],
            "Please try the replacement",
        )
        self.assertTrue(any(
            call.args[0] == 1001 and "The shop replied" in call.args[1]
            and "Please try the replacement" in call.args[1]
            for call in bot.send_message.await_args_list
        ))

    async def test_admin_marks_warranty_handled_privately_without_customer_notice(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.add_stock(product_id, "private-login:password")
        topup = self.shop.store.create_topup(1001, 10_000)
        self.shop.store.record_bank_event(
            "private-warranty-bank", "0123456789", 10_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        order = self.shop.store.purchase(1001, product_id)
        ticket = self.shop.store.create_warranty(1001, order["id"], "Không đăng nhập được")
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data="", message=message)
        update = SimpleNamespace(
            callback_query=query, message=message, effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(user_data={}, bot=bot)

        query.data = f"admin:warranty:{ticket['id']}"
        await self.shop.callbacks(update, context)
        buttons = [button.callback_data for row in message.reply_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:warrantyprivate:{ticket['id']}", buttons)
        query.data = f"admin:warrantyprivate:{ticket['id']}"
        await self.shop.callbacks(update, context)
        self.assertIn("không gửi tin cho khách", message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_warranty(ticket["id"])["status"], "open")

        update.effective_user.id = 1002
        query.data = f"admin:warrantyprivateconfirm:{ticket['id']}"
        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_warranty(ticket["id"])["status"], "open")
        update.effective_user.id = 1
        await self.shop.callbacks(update, context)

        resolved = self.shop.store.get_warranty(ticket["id"])
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolved_privately"], 1)
        self.assertEqual(resolved["privately_resolved_by"], 1)
        self.assertIsNone(resolved["reply"])
        self.assertEqual(self.shop.store.count_open_warranties(), 0)
        self.assertEqual(self.shop.store.list_warranties(), [])
        self.assertEqual(self.shop.store.list_warranties(None)[0]
                         ["resolved_privately"], 1)
        self.assertIn("đã xử lý riêng", message.reply_text.await_args.args[0])
        bot.send_message.assert_not_awaited()

        reopened = Store(self.shop.config.database_path,
                         self.shop.config.inventory_encryption_key)
        self.assertEqual(reopened.get_warranty(ticket["id"])["resolved_privately"], 1)
        await self.shop.callbacks(update, context)
        self.assertEqual(self.shop.store.get_warranty(ticket["id"])["status"], "resolved")
        bot.send_message.assert_not_awaited()

        await self.shop.send_admin_all_warranties(message)
        self.assertIn("đã xử lý riêng", message.reply_text.await_args.args[0])
        update.effective_user.id = 1001
        query.data = f"warranty:status:{order['id']}"
        await self.shop.callbacks(update, context)
        self.assertIn("đã xử lý", message.reply_text.await_args.args[0])
        self.assertNotIn("đã phản hồi", message.reply_text.await_args.args[0])
        self.assertNotIn("xử lý riêng", message.reply_text.await_args.args[0])
        bot.send_message.assert_not_awaited()
        self.shop.store.create_warranty(1001, order["id"], "Lỗi khác")

    async def test_admin_report_shows_selected_day_and_rejects_bad_date(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.admin_report(update, SimpleNamespace(args=["2026-09-26"]))
        self.assertIn("BÁO CÁO 2026-09-26", message.reply_text.await_args.args[0])
        self.assertIn("Số đơn: 0", message.reply_text.await_args.args[0])
        await self.shop.admin_report(update, SimpleNamespace(args=["2026-02-30"]))
        self.assertIn("Ngày không hợp lệ", message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.daily_report(date(2026, 9, 26))["topups"], 0)

    async def test_admin_can_export_by_button_or_command_and_cancel_prompt(self):
        message = SimpleNamespace(text="", reply_text=AsyncMock(), reply_document=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data="admin:export", message=message)
        update = SimpleNamespace(
            message=message, effective_message=message, callback_query=query,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={"awaiting_manual_credit": 99}, args=[])

        await self.shop.callbacks(update, context)
        self.assertTrue(context.user_data["awaiting_export_range"])
        self.assertNotIn("awaiting_manual_credit", context.user_data)
        self.assertIn("YYYY-MM-DD", message.reply_text.await_args.args[0])
        message.text = "2026-02-30 2026-03-01"
        await self.shop.text_message(update, context)
        self.assertTrue(context.user_data["awaiting_export_range"])
        message.reply_document.assert_not_awaited()

        message.text = "2026-09-01 2026-09-30"
        await self.shop.text_message(update, context)
        message.reply_document.assert_awaited_once()
        self.assertNotIn("awaiting_export_range", context.user_data)
        self.assertIsInstance(message.reply_document.await_args.kwargs["document"], InputFile)
        self.assertIn("Nạp thực nhận: 0đ", message.reply_document.await_args.kwargs["caption"])

        query.data = "admin:export"
        await self.shop.callbacks(update, context)
        await self.shop.cancel(update, context)
        self.assertNotIn("awaiting_export_range", context.user_data)

        message.reply_document.reset_mock()
        context.user_data["awaiting_manual_credit"] = 99
        context.args = ["2026-09-01", "2026-09-30"]
        await self.shop.admin_export(update, context)
        message.reply_document.assert_awaited_once()
        self.assertNotIn("awaiting_manual_credit", context.user_data)

    async def test_admin_can_export_users_and_sold_products_from_their_pages(self):
        self.shop.store.ensure_user(1, "shop_admin", "Shop")
        self.shop.store.ensure_user(1001, "buyer1001", "Buyer")
        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                answer=AsyncMock(), data="admin:exportusers", message=self.message,
            ),
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.send_admin_users(self.message)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:exportusers", buttons)
        await self.shop.callbacks(update, context)
        users_file = self.message.reply_document.await_args.kwargs["document"]
        self.assertIsInstance(users_file, InputFile)
        self.assertTrue(users_file.filename.endswith(".csv"))
        self.assertIn(b"buyer1001", users_file.input_file_content)
        self.assertNotIn(b"shop_admin", users_file.input_file_content)

        await self.shop.send_admin_sales(self.message)
        buttons = [button.callback_data for row in
                   self.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn("admin:exportsold", buttons)
        update.callback_query.data = "admin:exportsold"
        await self.shop.callbacks(update, context)
        sold_file = self.message.reply_document.await_args.kwargs["document"]
        self.assertTrue(sold_file.filename.endswith(".csv"))
        self.assertIn(b"purchase_id", sold_file.input_file_content)

        update.effective_user.id = 1001
        self.message.reply_document.reset_mock()
        for callback in ("admin:exportusers", "admin:exportsold"):
            update.callback_query.data = callback
            await self.shop.callbacks(update, context)
            self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])
        self.message.reply_document.assert_not_awaited()

    async def test_customer_cannot_request_report_archive(self):
        message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock(), data="admin:export", message=message)
        update = SimpleNamespace(
            message=message, effective_message=message, callback_query=query,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={}, args=["2026-09-01", "2026-09-30"])
        await self.shop.admin_export(update, context)
        self.assertIn("chỉ dành cho quản trị viên", message.reply_text.await_args.args[0])
        await self.shop.callbacks(update, context)
        self.assertIn("Chỉ quản trị viên", message.reply_text.await_args.args[0])
        self.assertNotIn("awaiting_export_range", context.user_data)
        message.reply_document.assert_not_awaited()

    async def test_reconciliation_command_requires_admin_and_api_token(self):
        update = SimpleNamespace(
            message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        await self.shop.reconcile(update, None)
        self.assertIn("chỉ dành cho quản trị viên", self.message.reply_text.await_args.args[0])
        update.effective_user.id = 1
        await self.shop.reconcile(update, None)
        self.assertIn("SEPAY_API_TOKEN", self.message.reply_text.await_args.args[0])

    async def test_purchase_notifies_admin_when_stock_reaches_two(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.bulk_add_stock(
            product_id, ["login-one:password", "login-two:password", "login-three:password"],
        )
        self.shop.store.ensure_user(1001, "alice", "Alice")
        topup = self.shop.store.create_topup(1001, 20_000)
        self.shop.store.record_bank_event(
            "low-stock-bank", "0123456789", 20_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        query = SimpleNamespace(answer=AsyncMock(), data="confirm:token", message=self.message)
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(
            user_data={"pending_purchase": ("token", product_id, 10_000)}, bot=bot,
        )

        await self.shop.callbacks(update, context)

        self.assertIn("Mua thành công", self.message.reply_text.await_args.args[0])
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args[0], 1)
        self.assertIn("còn 2 tài khoản", bot.send_message.await_args.args[1])

        await self.shop.send_pending_purchase_notifications(bot)
        self.assertEqual(bot.send_message.await_count, 2)
        alert = bot.send_message.await_args.args[1]
        self.assertIn("@alice (ID 1001)", alert)
        self.assertIn(f"Sản phẩm: #{product_id} Account", alert)
        self.assertIn("Thanh toán: 10.000đ", alert)
        self.assertNotIn("password", alert)
        self.assertTrue(bot.send_message.await_args.kwargs["protect_content"])
        await self.shop.send_pending_purchase_notifications(bot)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_failed_purchase_alert_retries_after_bot_restart(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.add_stock(product_id, "private-login:password")
        topup = self.shop.store.create_topup(1001, 20_000)
        self.shop.store.record_bank_event(
            "alert-bank", "0123456789", 20_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        self.shop.store.purchase(1001, product_id, notify_admin_ids=frozenset({1}))
        failed_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        await self.shop.send_pending_purchase_notifications(failed_bot)
        with closing(sqlite3.connect(self.shop.config.database_path)) as conn:
            status, attempts = conn.execute(
                "SELECT status, attempts FROM purchase_notification_deliveries"
            ).fetchone()
            self.assertEqual((status, attempts), ("pending", 1))
            conn.execute(
                "UPDATE purchase_notification_deliveries "
                "SET next_attempt_at='2000-01-01T00:00:00Z'"
            )
            conn.commit()

        restarted = ShopBot(self.shop.config, Store(
            self.shop.config.database_path,
            self.shop.config.inventory_encryption_key,
        ))
        recovered_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_pending_purchase_notifications(recovered_bot)
        recovered_bot.send_message.assert_awaited_once()
        self.assertNotIn("private-login:password", recovered_bot.send_message.await_args.args[1])
        self.assertEqual(restarted.store.list_due_purchase_notifications(), [])

    async def test_purchase_alert_is_not_sent_to_removed_admin(self):
        product_id = self.shop.store.add_product("Account", 10_000)
        self.shop.store.add_stock(product_id, "private-login:password")
        topup = self.shop.store.create_topup(1001, 20_000)
        self.shop.store.record_bank_event(
            "removed-admin-bank", "0123456789", 20_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        self.shop.store.purchase(1001, product_id, notify_admin_ids=frozenset({1}))
        self.shop.config = replace(self.shop.config, admin_ids=frozenset({2}))
        bot = SimpleNamespace(send_message=AsyncMock())
        await self.shop.send_pending_purchase_notifications(bot)
        bot.send_message.assert_not_awaited()
        self.assertEqual(self.shop.store.list_due_purchase_notifications(), [])

    async def test_customer_applies_coupon_before_confirming_purchase(self):
        product_id = self.shop.store.add_product("Account", 50_000)
        self.shop.store.add_stock(product_id, "login:secret")
        self.shop.store.create_coupon("sale10", "percent", 10, 10)
        topup = self.shop.store.create_topup(1001, 100_000)
        self.shop.store.record_bank_event(
            "coupon-bank", "0123456789", 100_000, topup["code"],
            "in", topup["code"], "0123456789",
        )
        query = SimpleNamespace(answer=AsyncMock(), data=f"buy:{product_id}", message=self.message)
        update = SimpleNamespace(
            callback_query=query, message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))

        await self.shop.callbacks(update, context)
        query.data = f"coupon:product:{product_id}"
        await self.shop.callbacks(update, context)
        self.message.text = "sale10"
        await self.shop.text_message(update, context)
        self.assertIn("Thanh toán: 45.000đ", self.message.reply_text.await_args.args[0])
        self.assertNotIn("awaiting_coupon", context.user_data)

        query.data = f"confirm:{context.user_data['pending_purchase'][0]}"
        await self.shop.callbacks(update, context)
        self.assertIn("login:secret", self.message.reply_text.await_args.args[0])
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])
        self.assertEqual(self.shop.store.get_balance(1001), 55_000)

    async def test_admin_creates_product_coupon_and_customer_uses_it_only_there(self):
        first = self.shop.store.add_product("Account A", 50_000)
        second = self.shop.store.add_product("Account B", 40_000)
        self.shop.store.add_stock(first, "a-1")
        self.shop.store.add_stock(second, "b-1")
        admin_message = SimpleNamespace(
            text=f"/addcoupon ONLYA | 10% | 2 | {first}", reply_text=AsyncMock(),
        )
        admin_update = SimpleNamespace(
            message=admin_message, effective_message=admin_message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )

        await self.shop.add_coupon(admin_update, None)

        self.assertIn(f"sản phẩm #{first}", admin_message.reply_text.await_args.args[0])
        admin_message.text = f"/addcoupon ONLYB | 5000 | 2 | 2099-12-31 | {second}"
        await self.shop.add_coupon(admin_update, None)
        coupons = {coupon["code"]: coupon for coupon in self.shop.store.list_coupons()}
        self.assertEqual(coupons["ONLYA"]["product_id"], first)
        self.assertEqual((coupons["ONLYB"]["product_id"], coupons["ONLYB"]["expires_on"]),
                         (second, "2099-12-31"))
        await self.shop.send_admin_coupons(self.message)
        self.assertIn(f"SP #{first} Account A", self.message.reply_text.await_args.args[0])

        customer_message = SimpleNamespace(text="ONLYA", reply_text=AsyncMock())
        customer_update = SimpleNamespace(
            message=customer_message, effective_message=customer_message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={"awaiting_coupon": ("product", second)})
        await self.shop.text_message(customer_update, context)
        self.assertIn("không áp dụng cho sản phẩm/gói này",
                      customer_message.reply_text.await_args.args[0])
        self.assertIn("awaiting_coupon", context.user_data)

        context.user_data["awaiting_coupon"] = ("product", first)
        await self.shop.text_message(customer_update, context)
        self.assertIn("Thanh toán: 45.000đ", customer_message.reply_text.await_args.args[0])

    async def test_admin_creates_bundle_and_customer_receives_each_account(self):
        first = self.shop.store.add_product("Account A", 40_000)
        second = self.shop.store.add_product("Account B", 30_000)
        self.shop.store.add_stock(first, "login-a:secret")
        self.shop.store.add_stock(second, "login-b:secret")
        admin_message = SimpleNamespace(
            text=f"/addbundle Combo | 55k | {first}x1,{second}x1 | Hai loại tài khoản",
            reply_text=AsyncMock(),
        )
        admin_update = SimpleNamespace(
            message=admin_message, effective_message=admin_message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.add_bundle(admin_update, None)
        self.assertIn("Đã tạo gói #1", admin_message.reply_text.await_args.args[0])
        topup = self.shop.store.create_topup(1001, 100_000)
        self.shop.store.record_bank_event(
            "bundle-bank", "0123456789", 100_000, topup["code"],
            "in", topup["code"], "0123456789",
        )
        query = SimpleNamespace(answer=AsyncMock(), data="buybundle:1", message=self.message)
        update = SimpleNamespace(
            callback_query=query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        await self.shop.callbacks(update, context)
        self.assertIn("Thanh toán: 55.000đ", self.message.reply_text.await_args.args[0])
        query.data = f"confirmbundle:{context.user_data['pending_bundle'][0]}"
        await self.shop.callbacks(update, context)

        messages = self.message.reply_text.await_args_list
        self.assertTrue(any("login-a:secret" in call.args[0] for call in messages))
        self.assertTrue(any("login-b:secret" in call.args[0] for call in messages))
        self.assertTrue(all(
            call.kwargs.get("protect_content") for call in messages
            if "login-" in call.args[0]
        ))
        deliveries = [call for call in messages if "Thông tin tài khoản:" in call.args[0]]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(
            deliveries[0].kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text,
            "login-a:secret\nlogin-b:secret",
        )
        self.assertTrue(all(
            call.kwargs["entities"][0].type == "pre" for call in messages
            if "Thông tin tài khoản:" in call.args[0]
        ))
        self.message.reply_document.assert_awaited_once()
        text_file = self.message.reply_document.await_args.kwargs[
            "document"].input_file_content.decode("utf-8-sig")
        self.assertIn("login-a:secret", text_file)
        self.assertIn("login-b:secret", text_file)
        self.assertEqual(self.shop.store.get_balance(1001), 45_000)
        self.assertEqual(len(self.shop.store.list_orders(1001)), 2)
        self.assertEqual(self.shop.store.get_user_summary(1001)["order_count"], 1)
        await self.shop.send_pending_purchase_notifications(context.bot)
        alerts = [call.args[1] for call in context.bot.send_message.await_args_list
                  if "CÓ ĐƠN MỚI" in call.args[1]]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Gói: #1 Combo", alerts[0])
        self.assertIn("Account A", alerts[0])
        self.assertIn("Account B", alerts[0])
        self.assertIn("Thanh toán: 55.000đ", alerts[0])
        self.assertNotIn("login-a:secret", alerts[0])
        purchase_id = min(item["id"] for item in self.shop.store.list_orders(1001))
        self.assertIn(f"Đơn: #{purchase_id}", alerts[0])
        self.assertIn(f"admin:purchase:{purchase_id}", [
            button.callback_data
            for call in context.bot.send_message.await_args_list
            if "CÓ ĐƠN MỚI" in call.args[1]
            for row in call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ])
