from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock
import sqlite3

from cryptography.fernet import Fernet

from bot import ShopBot
from config import Config
from store import SHOP_TIMEZONE, Store


class FunnelStoreTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.key = Fernet.generate_key().decode("ascii")
        self.store = Store(self.path, self.key)

    def tearDown(self):
        self.temp.cleanup()

    def test_unique_visitors_views_and_financial_stages(self):
        for user_id in (1, 1001, 1002, 1003, 1004):
            self.store.ensure_user(user_id)
        today = datetime.now(SHOP_TIMEZONE).date()
        for user_id in (1, 1001, 1002, 1003):
            self.assertTrue(self.store.record_funnel_event(user_id, "visit", today))
        self.assertFalse(self.store.record_funnel_event(1001, "visit", today))
        for user_id in (1001, 1002):
            self.assertTrue(self.store.record_funnel_event(user_id, "catalog_view", today))
        self.assertFalse(self.store.record_funnel_event(1001, "catalog_view", today))

        product_id = self.store.add_product("Account", 10_000)
        self.store.add_stock(product_id, "first-account")
        paid = self.store.create_topup(1001, 20_000)
        self.store.record_bank_event(
            "funnel-paid-1001", "0123456789", 20_000, paid["code"], "in",
            paid["code"], "0123456789",
        )
        self.store.purchase(1001, product_id)
        self.store.create_topup(1003, 10_000)
        outside_cohort = self.store.create_topup(1004, 10_000)
        self.store.record_bank_event(
            "funnel-paid-1004", "0123456789", 10_000, outside_cohort["code"], "in",
            outside_cohort["code"], "0123456789",
        )

        report = self.store.funnel_report(
            7, frozenset({1}), datetime.now(SHOP_TIMEZONE) + timedelta(seconds=10),
        )
        self.assertEqual(
            {key: report[key] for key in (
                "visitors", "new_visitors", "viewers", "topup_requesters",
                "viewer_topup_requesters", "payers", "buyers", "viewer_buyers",
            )},
            {"visitors": 3, "new_visitors": 3, "viewers": 2,
             "topup_requesters": 2, "viewer_topup_requesters": 1,
             "payers": 1, "buyers": 1, "viewer_buyers": 1},
        )
        self.assertFalse(Store(self.path, self.key).record_funnel_event(1001, "visit", today))

    def test_old_financial_data_is_not_counted_before_tracking_started(self):
        self.store.ensure_user(1001)
        product_id = self.store.add_product("Account", 10_000)
        self.store.add_stock(product_id, "old-account")
        topup = self.store.create_topup(1001, 20_000)
        self.store.record_bank_event(
            "old-funnel-payment", "0123456789", 20_000, topup["code"], "in",
            topup["code"], "0123456789",
        )
        self.store.purchase(1001, product_id)
        old_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with closing(sqlite3.connect(self.path)) as conn:
            for table in ("topups", "ledger", "orders"):
                conn.execute(f"UPDATE {table} SET created_at=?", (old_time,))
            conn.commit()
        self.store.record_funnel_event(1001, "visit")
        self.store.record_funnel_event(1001, "catalog_view")

        report = self.store.funnel_report(
            30, frozenset(), datetime.now(SHOP_TIMEZONE) + timedelta(seconds=10),
        )
        self.assertEqual((report["visitors"], report["viewers"]), (1, 1))
        self.assertEqual((report["topup_requesters"], report["payers"], report["buyers"]),
                         (0, 0, 0))
        with self.assertRaises(ValueError):
            self.store.funnel_report(365)

    def test_seven_and_thirty_day_windows_respect_tracking_start(self):
        now = datetime.now(SHOP_TIMEZONE)
        today = now.date()
        for user_id in (1001, 1002, 1003):
            self.store.ensure_user(user_id)
        for user_id, days_ago in ((1001, 15), (1001, 14), (1002, 0), (1003, 27)):
            event_day = today - timedelta(days=days_ago)
            for event_type in ("visit", "catalog_view"):
                self.store.record_funnel_event(user_id, event_type, event_day)

        def utc_stamp(value):
            return value.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")

        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(
                "UPDATE funnel_tracking_state SET started_at=? WHERE id=1",
                (utc_stamp(now - timedelta(days=25)),),
            )
            for days_ago in (15, 14, 27):
                conn.execute(
                    "UPDATE funnel_events SET created_at=? WHERE event_date=?",
                    (utc_stamp(now - timedelta(days=days_ago)),
                     (today - timedelta(days=days_ago)).isoformat()),
                )
            conn.commit()

        week = self.store.funnel_report(7, now=now + timedelta(seconds=10))
        month = self.store.funnel_report(30, now=now + timedelta(seconds=10))
        self.assertEqual((week["visitors"], week["viewers"]), (1, 1))
        self.assertEqual((month["visitors"], month["viewers"]), (2, 2))
        self.assertEqual(
            month["start_at"].isoformat(timespec="milliseconds"),
            utc_stamp(now - timedelta(days=25)).replace("Z", "+00:00"),
        )


class FunnelBotTests(IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_accepted_visit_and_catalog_view_appear_in_admin_report(self):
        customer_message = SimpleNamespace(chat_id=1001, reply_text=AsyncMock())
        customer_update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1001, username="buyer", full_name="Buyer"),
            effective_message=customer_message, callback_query=None,
        )
        await self.shop.spam_guard(customer_update, None)
        await self.shop.spam_guard(customer_update, None)
        product_id = self.shop.store.add_product("Account", 10_000)
        await self.shop.send_shop(customer_message, 0)
        await self.shop.send_shop(customer_message, 0)

        admin_message = SimpleNamespace(reply_text=AsyncMock())
        admin_update = SimpleNamespace(
            message=admin_message, effective_message=admin_message,
            effective_chat=SimpleNamespace(type="private"),
            effective_user=SimpleNamespace(id=1),
        )
        await self.shop.admin_funnel(admin_update, SimpleNamespace(args=[]))
        output = admin_message.reply_text.await_args.args[0]
        self.assertIn("Khách tương tác: 1", output)
        self.assertIn("Xem sản phẩm/gói: 1", output)
        self.assertIn("Mua hàng: 0", output)
        buttons = [button.callback_data for row in
                   admin_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for button in row]
        self.assertEqual(buttons, ["admin:funnel:7", "admin:funnel:30", "admin:home"])

        query = SimpleNamespace(answer=AsyncMock(), data=f"product:{product_id}",
                                message=customer_message)
        customer_update.callback_query = query
        await self.shop.callbacks(customer_update, SimpleNamespace(user_data={}))
        report = self.shop.store.funnel_report(7, self.shop.config.admin_ids)
        self.assertEqual((report["visitors"], report["viewers"]), (1, 1))

        query.data = "admin:funnel:30"
        admin_update.callback_query = query
        query.message = admin_message
        await self.shop.callbacks(admin_update, SimpleNamespace(user_data={}))
        self.assertIn("30 NGÀY", admin_message.reply_text.await_args.args[0])

        await self.shop.admin_funnel(customer_update, SimpleNamespace(args=[]))
        self.assertIn("chỉ dành cho quản trị viên",
                      customer_message.reply_text.await_args.args[0])
