from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock
import sqlite3

from cryptography.fernet import Fernet
from telegram.error import Forbidden, TelegramError

from bot import ShopBot
from config import Config
from store import EmailRequired, InsufficientBalance, NotFound, OutOfStock, PriceChanged, Store


def funded_shop(path: Path, key: str) -> Store:
    shop = Store(path, key)
    shop.ensure_user(1001)
    topup = shop.create_topup(1001, 100_000)
    shop.record_bank_event(
        "email-test-payment", "0123456789", 100_000, topup["code"],
        "in", topup["code"], "0123456789",
    )
    return shop


class EmailStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.shop = funded_shop(self.path, self.key)

    def tearDown(self):
        self.temp.cleanup()

    def test_required_email_is_checked_before_debit_and_encrypted_in_order(self):
        product_id = self.shop.add_product("Account", 20_000)
        self.shop.bulk_add_stock(product_id, ["first", "second"])
        self.shop.set_requires_email(product_id, True)
        self.assertTrue(self.shop.quote_product(1001, product_id)["requires_email"])
        with self.assertRaises(EmailRequired):
            self.shop.purchase(1001, product_id)
        with self.assertRaises(ValueError):
            self.shop.purchase(1001, product_id, customer_email="wrong address")
        self.assertEqual(self.shop.get_balance(1001), 100_000)
        self.assertEqual(self.shop.stock_counts(product_id)["available"], 2)
        self.assertEqual(self.shop.list_orders(1001), [])

        order = self.shop.purchase(1001, product_id,
                                   customer_email=" Buyer+tag@Example.COM ")
        self.assertEqual(order["customer_email"], "Buyer+tag@example.com")
        with closing(sqlite3.connect(self.path)) as conn:
            ciphertext = conn.execute(
                "SELECT customer_email_ciphertext FROM orders WHERE id=?", (order["id"],)
            ).fetchone()[0]
        self.assertNotIn("Buyer+tag@example.com", ciphertext)
        self.assertEqual(self.shop.get_order(1001, order["id"])["customer_email"],
                         "Buyer+tag@example.com")
        self.assertIsNone(self.shop.get_order(1002, order["id"]))
        self.assertEqual(self.shop.get_admin_order_contact(order["id"])["customer_email"],
                         "Buyer+tag@example.com")

        self.shop.set_requires_email(product_id, False)
        later = self.shop.purchase(1001, product_id)
        self.assertIsNone(self.shop.get_order(1001, later["id"])["customer_email"])
        self.assertEqual(self.shop.get_order(1001, order["id"])["customer_email"],
                         "Buyer+tag@example.com")

    def test_comma_separated_emails_buy_one_product_per_email(self):
        product_id = self.shop.add_product("Account", 20_000)
        self.shop.bulk_add_stock(product_id, ["first-secret", "second-secret"])
        self.shop.set_requires_email(product_id, True)
        for invalid in (
            "first@example.com, invalid",
            "first@example.com,",
            "first@example.com,,second@example.com",
            "a" * 2001,
        ):
            with self.subTest(invalid=invalid[:40]), self.assertRaises(ValueError):
                self.shop.purchase(1001, product_id, customer_email=invalid)
        self.assertEqual(self.shop.get_balance(1001), 100_000)
        self.assertEqual(self.shop.stock_counts(product_id)["available"], 2)

        expected = "Buyer+tag@example.com, helper@example.org"
        quote = self.shop.quote_product(1001, product_id, quantity=2)
        self.assertEqual(quote["price"], 40_000)
        order = self.shop.purchase(
            1001, product_id,
            expected_price=quote["price"],
            customer_email=" Buyer+tag@Example.COM , helper@Example.ORG ",
        )
        self.assertEqual(order["customer_email"], expected)
        self.assertEqual(order["quantity"], 2)
        self.assertEqual(order["price"], 40_000)
        self.assertEqual(self.shop.get_balance(1001), 60_000)
        self.assertEqual(self.shop.stock_counts(product_id)["available"], 0)
        self.assertEqual([item["customer_email"] for item in order["orders"]],
                         ["Buyer+tag@example.com", "helper@example.org"])
        self.assertEqual([item["secret"] for item in order["orders"]],
                         ["first-secret", "second-secret"])
        for item in order["orders"]:
            self.assertEqual(self.shop.get_order(1001, item["id"])["customer_email"],
                             item["customer_email"])
            self.assertEqual(self.shop.get_admin_order_contact(item["id"])["customer_email"],
                             item["customer_email"])
        with closing(sqlite3.connect(self.path)) as conn:
            ciphertexts = [row[0] for row in conn.execute(
                "SELECT customer_email_ciphertext FROM orders ORDER BY id"
            )]
        self.assertEqual(len(ciphertexts), 2)
        self.assertTrue(all("@" not in ciphertext for ciphertext in ciphertexts))
        self.assertIsNone(self.shop.get_order(1002, order["id"]))

    def test_multiple_emails_need_enough_stock_and_balance_before_any_debit(self):
        product_id = self.shop.add_product("Account", 20_000)
        self.shop.add_stock(product_id, "first-secret")
        self.shop.set_requires_email(product_id, True)
        emails = "first@example.com, second@example.com"
        with self.assertRaises(OutOfStock):
            self.shop.quote_product(1001, product_id, quantity=2)
        with self.assertRaises(OutOfStock):
            self.shop.purchase(1001, product_id, customer_email=emails)
        self.assertEqual(self.shop.stock_counts(product_id)["available"], 1)

        expensive_id = self.shop.add_product("Expensive", 60_000)
        self.shop.bulk_add_stock(expensive_id, ["second-secret", "third-secret"])
        self.shop.set_requires_email(expensive_id, True)
        with self.assertRaises(InsufficientBalance):
            self.shop.purchase(1001, expensive_id, customer_email=emails)
        self.assertEqual(self.shop.get_balance(1001), 100_000)
        self.assertEqual(self.shop.stock_counts(expensive_id)["available"], 2)
        self.assertEqual(self.shop.list_orders(1001), [])

    def test_multi_email_coupon_is_applied_once_to_total(self):
        product_id = self.shop.add_product("Account", 20_000)
        self.shop.bulk_add_stock(product_id, ["first", "second"])
        self.shop.set_requires_email(product_id, True)
        self.shop.create_coupon("SAVE5", "fixed", 5_000, 1)
        quoted = self.shop.quote_product(1001, product_id, "SAVE5", quantity=2)
        self.assertEqual((quoted["price"], quoted["discount"]), (35_000, 5_000))
        sale = self.shop.purchase(
            1001, product_id, expected_price=35_000, coupon_code="SAVE5",
            customer_email="first@example.com, second@example.com",
        )
        self.assertEqual(sale["price"], 35_000)
        self.assertEqual([item["price"] for item in sale["orders"]], [17_500, 17_500])
        self.assertEqual(self.shop.get_balance(1001), 65_000)
        self.assertEqual(self.shop.list_coupons()[0]["used"], 1)

    def test_multi_email_sale_prices_each_slot_and_rejects_changed_quote(self):
        product_id = self.shop.add_product("Account", 20_000)
        self.shop.bulk_add_stock(product_id, ["first", "second"])
        self.shop.set_requires_email(product_id, True)
        self.shop.start_sale(product_id, 15_000, 1)
        quoted = self.shop.quote_product(1001, product_id, quantity=2)
        self.assertEqual((quoted["price"], quoted["sale_units"]), (35_000, 1))
        self.assertEqual(quoted["sale_remaining"], 1)
        self.shop.end_sale(product_id)
        with self.assertRaises(PriceChanged):
            self.shop.purchase(
                1001, product_id, expected_price=35_000,
                customer_email="first@example.com, second@example.com",
            )
        self.assertEqual(self.shop.get_balance(1001), 100_000)
        self.assertEqual(self.shop.stock_counts(product_id)["available"], 2)

        self.shop.start_sale(product_id, 15_000, 1)
        sale = self.shop.purchase(
            1001, product_id, expected_price=35_000,
            customer_email="first@example.com, second@example.com",
        )
        self.assertEqual([item["price"] for item in sale["orders"]], [15_000, 20_000])
        self.assertEqual(self.shop.get_balance(1001), 65_000)
        self.assertIsNone(self.shop.get_product(product_id)["sale_id"])
        with closing(sqlite3.connect(self.path)) as conn:
            sale_ids = [row[0] for row in conn.execute(
                "SELECT sale_id FROM orders ORDER BY id"
            )]
        self.assertIsNotNone(sale_ids[0])
        self.assertIsNone(sale_ids[1])

    def test_bundle_requires_email_only_for_its_selected_product(self):
        first = self.shop.add_product("Needs email", 40_000)
        second = self.shop.add_product("No email", 30_000)
        self.shop.add_stock(first, "first account")
        self.shop.add_stock(second, "second account")
        self.shop.set_requires_email(first, True)
        bundle_id = self.shop.create_bundle("Combo", 55_000, {first: 1, second: 1})
        self.assertTrue(self.shop.quote_bundle(1001, bundle_id)["requires_email"])
        with self.assertRaises(EmailRequired):
            self.shop.purchase_bundle(1001, bundle_id)
        self.assertEqual(self.shop.get_balance(1001), 100_000)
        sale = self.shop.purchase_bundle(
            1001, bundle_id, customer_email="buyer@example.com, helper@example.org",
        )
        contacts = {order["product_id"]: self.shop.get_order(1001, order["id"])[
            "customer_email"] for order in sale["orders"]}
        self.assertEqual(contacts, {
            first: "buyer@example.com, helper@example.org", second: None,
        })

    def test_email_completion_is_per_order_persistent_and_only_queued_once(self):
        product_id = self.shop.add_product("Email product", 20_000)
        self.shop.bulk_add_stock(product_id, ["first-secret", "second-secret"])
        self.shop.set_requires_email(product_id, True)
        sale = self.shop.purchase(
            1001, product_id,
            customer_email="first@example.com, second@example.com",
        )
        first_id, second_id = [item["id"] for item in sale["orders"]]
        self.assertEqual(self.shop.list_due_email_completion_notifications(), [])
        first = self.shop.complete_email_order(first_id, 1)
        self.assertTrue(first["created"])
        self.assertEqual(first["notification_status"], "pending")
        self.assertFalse(self.shop.complete_email_order(first_id, 2)["created"])
        self.assertEqual(self.shop.get_admin_order_contact(first_id)["email_completed_by"], 1)
        self.assertIsNone(self.shop.get_order(1001, second_id)["email_completed_at"])
        self.assertEqual(
            [item["order_id"] for item in self.shop.list_due_email_completion_notifications()],
            [first_id],
        )

        restarted = Store(self.path, self.key)
        self.assertIsNotNone(restarted.get_order(1001, first_id)["email_completed_at"])
        self.assertIsNone(restarted.get_order(1001, second_id)["email_completed_at"])
        restarted.finish_email_completion_notification(first_id, "sent")
        self.assertEqual(restarted.list_due_email_completion_notifications(), [])
        self.assertFalse(restarted.complete_email_order(first_id, 1)["created"])
        self.assertEqual(restarted.get_admin_order_contact(first_id)[
            "email_notification_status"], "sent")

        plain_product = self.shop.add_product("No email", 5_000)
        self.shop.add_stock(plain_product, "plain-secret")
        plain = self.shop.purchase(1001, plain_product)
        with self.assertRaises(NotFound):
            self.shop.complete_email_order(plain["id"], 1)
        with self.assertRaises(NotFound):
            self.shop.complete_email_order(999999, 1)


class EmailBotTests(IsolatedAsyncioTestCase):
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
        self.shop = ShopBot(config, funded_shop(config.database_path, key))
        self.message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock(),
                                       delete=AsyncMock(),
                                       text="")
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        self.context = SimpleNamespace(user_data={}, bot=self.bot)
        self.query = SimpleNamespace(answer=AsyncMock(), data="", message=self.message)
        self.customer = SimpleNamespace(
            callback_query=self.query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_email_prompt_coupon_checkout_and_admin_access(self):
        product_id = self.shop.store.add_product("Email account", 20_000)
        self.shop.store.add_stock(product_id, "login:secret")
        self.shop.store.create_coupon("SAVE10", "percent", 10, 5)
        self.shop.store.set_requires_email(product_id, True)

        self.query.data = f"buy:{product_id}"
        await self.shop.callbacks(self.customer, self.context)
        self.assertIn("Chọn số lượng", self.message.reply_text.await_args.args[0])
        self.query.data = f"buyqty:{product_id}:1"
        await self.shop.callbacks(self.customer, self.context)
        self.assertIn("Gửi đúng 1 email", self.message.reply_text.await_args.args[0])
        self.assertIn("cách nhau bằng dấu phẩy", self.message.reply_text.await_args.args[0])
        self.assertIn("awaiting_buy_email", self.context.user_data)
        self.assertNotIn("pending_purchase", self.context.user_data)

        text_update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        self.message.text = "buyer@example.com, bad address"
        await self.shop.text_message(text_update, self.context)
        self.assertIn("awaiting_buy_email", self.context.user_data)
        self.assertIn("Email chưa hợp lệ", self.bot.send_message.await_args.args[1])

        self.message.text = "buyer@example.com"
        await self.shop.text_message(text_update, self.context)
        self.assertNotIn("awaiting_buy_email", self.context.user_data)
        self.assertIn("Email xử lý: buyer@example.com",
                      self.bot.send_message.await_args.args[1])
        self.assertTrue(self.bot.send_message.await_args.kwargs["protect_content"])
        self.assertEqual(self.message.delete.await_count, 2)

        old_token = self.context.user_data["product_checkout"]["token"]
        self.query.data = f"buyflow:email:{old_token}"
        await self.shop.callbacks(self.customer, self.context)
        self.assertIn("email mới", self.message.reply_text.await_args.args[0])
        self.message.text = "new@example.com"
        await self.shop.text_message(text_update, self.context)
        self.assertIn("Email xử lý: new@example.com",
                      self.bot.send_message.await_args.args[1])
        self.assertEqual(self.message.delete.await_count, 3)

        token = self.context.user_data["product_checkout"]["token"]
        self.query.data = f"buyflow:coupon:{token}"
        await self.shop.callbacks(self.customer, self.context)
        self.message.text = "SAVE10"
        await self.shop.text_message(text_update, self.context)
        self.assertIn("Email xử lý: new@example.com",
                      self.message.reply_text.await_args.args[0])
        token = self.context.user_data["product_checkout"]["token"]
        self.query.data = f"buyflow:wallet:{token}"
        await self.shop.callbacks(self.customer, self.context)
        self.assertTrue(any("Đã thanh toán" in call.args[0]
                            for call in self.message.reply_text.await_args_list))
        order_id = self.shop.store.list_orders(1001)[0]["id"]
        self.assertEqual(self.shop.store.get_order(1001, order_id)["customer_email"],
                         "new@example.com")
        self.assertEqual(self.shop.store.get_balance(1001), 82_000)

        await self.shop.send_pending_purchase_notifications(self.bot)
        notice = self.bot.send_message.await_args
        self.assertNotIn("new@example.com", notice.args[1])
        buttons = [button.callback_data for row in notice.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertIn(f"admin:order:{order_id}", buttons)

        admin = SimpleNamespace(
            callback_query=self.query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        self.query.data = f"admin:order:{order_id}"
        await self.shop.callbacks(admin, SimpleNamespace(user_data={}))
        self.assertIn("new@example.com",
                      self.message.reply_text.await_args.args[0])
        self.assertTrue(self.message.reply_text.await_args.kwargs["protect_content"])
        await self.shop.callbacks(self.customer, SimpleNamespace(user_data={}))
        self.assertIn("Chỉ quản trị viên", self.message.reply_text.await_args.args[0])

    async def test_two_emails_show_two_item_price_and_deliver_two_orders(self):
        product_id = self.shop.store.add_product("Canva slot", 5_000)
        self.shop.store.bulk_add_stock(product_id, ["slot-one", "slot-two"])
        self.shop.store.set_requires_email(product_id, True)
        self.query.data = f"buy:{product_id}"
        await self.shop.callbacks(self.customer, self.context)
        self.query.data = f"buyqty:{product_id}:2"
        await self.shop.callbacks(self.customer, self.context)

        text_update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        self.message.text = "first@example.com, second@example.com"
        await self.shop.text_message(text_update, self.context)
        confirmation = self.bot.send_message.await_args.args[1]
        self.assertIn("Số lượng: 2", confirmation)
        self.assertIn("Cần thanh toán: 10.000đ", confirmation)
        self.assertEqual(self.context.user_data["product_checkout"]["quantity"], 2)

        token = self.context.user_data["product_checkout"]["token"]
        self.query.data = f"buyflow:wallet:{token}"
        await self.shop.callbacks(self.customer, self.context)
        delivered = "\n".join(call.args[0] for call in self.message.reply_text.await_args_list)
        self.assertIn("Đã thanh toán 10.000đ cho 2 tài khoản", delivered)
        self.assertIn("first@example.com", delivered)
        self.assertIn("second@example.com", delivered)
        self.assertIn("slot-one", delivered)
        self.assertIn("slot-two", delivered)
        self.assertEqual(self.shop.store.get_balance(1001), 90_000)
        self.assertEqual(self.shop.store.stock_counts(product_id)["sold"], 2)
        orders = self.shop.store.list_orders(1001)
        self.assertEqual(len(orders), 2)
        self.assertEqual(sorted(order["price"] for order in orders), [5_000, 5_000])
        self.assertEqual(
            {self.shop.store.get_order(1001, item["id"])["customer_email"]
             for item in orders},
            {"first@example.com", "second@example.com"},
        )
        self.assertEqual(len(self.shop.store.list_due_purchase_notifications()), 1)

    async def test_two_emails_cannot_confirm_when_only_one_item_remains(self):
        product_id = self.shop.store.add_product("Canva slot", 5_000)
        self.shop.store.add_stock(product_id, "only-slot")
        self.shop.store.set_requires_email(product_id, True)
        self.query.data = f"buy:{product_id}"
        await self.shop.callbacks(self.customer, self.context)
        self.query.data = f"buyqty:{product_id}:other"
        await self.shop.callbacks(self.customer, self.context)
        text_update = SimpleNamespace(
            message=self.message, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001),
        )
        self.message.text = "2"
        await self.shop.text_message(text_update, self.context)
        self.assertIn("kho không đủ", self.message.reply_text.await_args.args[0])
        self.assertNotIn("product_checkout", self.context.user_data)
        self.assertEqual(self.shop.store.get_balance(1001), 100_000)
        self.assertEqual(self.shop.store.stock_counts(product_id)["available"], 1)

    async def test_admin_marks_email_order_done_and_customer_sees_one_notice(self):
        product_id = self.shop.store.add_product("Email account", 20_000)
        self.shop.store.add_stock(product_id, "login:secret")
        self.shop.store.set_requires_email(product_id, True)
        order = self.shop.store.purchase(
            1001, product_id, customer_email="buyer@example.com",
        )
        order_id = order["id"]
        admin = SimpleNamespace(
            callback_query=self.query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        self.query.data = f"admin:order:{order_id}"
        await self.shop.callbacks(admin, SimpleNamespace(user_data={}))
        buttons = [button.callback_data
                   for row in self.message.reply_text.await_args.kwargs[
                       "reply_markup"].inline_keyboard for button in row]
        self.assertIn(f"admin:emaildone:{order_id}", buttons)
        self.assertIn("Chờ admin xử lý", self.message.reply_text.await_args.args[0])

        self.query.data = f"order:{order_id}"
        await self.shop.callbacks(self.customer, SimpleNamespace(user_data={}))
        self.assertIn("Trạng thái email: đang chờ xử lý",
                      self.message.reply_text.await_args.args[0])
        self.query.data = f"admin:emaildone:{order_id}"
        await self.shop.callbacks(self.customer, SimpleNamespace(user_data={}))
        self.assertIsNone(self.shop.store.get_order(1001, order_id)["email_completed_at"])

        await self.shop.callbacks(admin, SimpleNamespace(user_data={}))
        self.assertIsNotNone(self.shop.store.get_order(1001, order_id)["email_completed_at"])
        self.assertIn("Xử lý email: Đã xử lý", self.message.reply_text.await_args.args[0])
        await self.shop.callbacks(admin, SimpleNamespace(user_data={}))
        self.assertEqual(len(self.shop.store.list_due_email_completion_notifications()), 1)

        await self.shop.send_pending_email_completion_notifications(self.bot)
        self.bot.send_message.assert_awaited_once()
        sent = self.bot.send_message.await_args
        self.assertEqual(sent.args[0], 1001)
        self.assertIn(f"Đơn #{order_id}", sent.args[1])
        self.assertNotIn("buyer@example.com", sent.args[1])
        self.assertNotIn("login:secret", sent.args[1])
        self.assertEqual(sent.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                         f"order:{order_id}")
        self.assertEqual(self.shop.store.get_admin_order_contact(order_id)[
            "email_notification_status"], "sent")
        await self.shop.send_pending_email_completion_notifications(self.bot)
        self.bot.send_message.assert_awaited_once()

        self.query.data = f"order:{order_id}"
        await self.shop.callbacks(self.customer, SimpleNamespace(user_data={}))
        self.assertIn("Trạng thái email: đã xử lý xong",
                      self.message.reply_text.await_args.args[0])
        await self.shop.send_orders(self.message, 1001)
        self.assertIn("email đã xử lý", self.message.reply_text.await_args.args[0])

    async def test_email_completion_notice_retries_after_restart_and_records_blocked_user(self):
        product_id = self.shop.store.add_product("Email account", 20_000)
        self.shop.store.bulk_add_stock(product_id, ["first", "second"])
        self.shop.store.set_requires_email(product_id, True)
        sale = self.shop.store.purchase(
            1001, product_id,
            customer_email="first@example.com, second@example.com",
        )
        first_id, second_id = [item["id"] for item in sale["orders"]]
        self.shop.store.complete_email_order(first_id, 1)
        self.shop.store.complete_email_order(second_id, 1)
        failed_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("offline")))
        await self.shop.send_pending_email_completion_notifications(failed_bot)
        with closing(sqlite3.connect(self.shop.config.database_path)) as conn:
            conn.execute(
                "UPDATE email_order_completions SET next_attempt_at='2000-01-01T00:00:00Z' "
                "WHERE order_id=?", (first_id,),
            )
            conn.commit()

        restarted = ShopBot(self.shop.config, Store(
            self.shop.config.database_path, self.shop.config.inventory_encryption_key,
        ))
        recovered_bot = SimpleNamespace(send_message=AsyncMock())
        await restarted.send_pending_email_completion_notifications(recovered_bot)
        self.assertEqual(recovered_bot.send_message.await_count, 1)
        self.assertEqual(restarted.store.get_admin_order_contact(first_id)[
            "email_notification_status"], "sent")

        with closing(sqlite3.connect(self.shop.config.database_path)) as conn:
            conn.execute(
                "UPDATE email_order_completions SET next_attempt_at='2000-01-01T00:00:00Z' "
                "WHERE order_id=?", (second_id,),
            )
            conn.commit()
        blocked_bot = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked")))
        await restarted.send_pending_email_completion_notifications(blocked_bot)
        self.assertEqual(restarted.store.get_admin_order_contact(second_id)[
            "email_notification_status"], "failed")

    async def test_admin_toggle_and_late_requirement_cannot_sell_without_email(self):
        product_id = self.shop.store.add_product("Account", 20_000)
        self.shop.store.add_stock(product_id, "login:secret")
        self.query.data = f"buy:{product_id}"
        await self.shop.callbacks(self.customer, self.context)
        self.query.data = f"buyqty:{product_id}:1"
        await self.shop.callbacks(self.customer, self.context)
        token = self.context.user_data["product_checkout"]["token"]

        admin = SimpleNamespace(
            callback_query=self.query, effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        self.query.data = f"admin:email:{product_id}:on"
        await self.shop.callbacks(admin, SimpleNamespace(user_data={}))
        self.assertTrue(self.shop.store.get_product(product_id)["requires_email"])
        self.assertIn("Email khi mua: bắt buộc", self.message.reply_text.await_args.args[0])

        self.query.data = f"buyflow:wallet:{token}"
        await self.shop.callbacks(self.customer, self.context)
        self.assertIn("đã thay đổi", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.shop.store.get_balance(1001), 100_000)
        self.assertEqual(self.shop.store.stock_counts(product_id)["available"], 1)
