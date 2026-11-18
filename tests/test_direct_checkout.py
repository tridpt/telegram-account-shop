from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cryptography.fernet import Fernet

from payments import process_sepay_payload
from store import EmailRequired, InsufficientBalance, PriceChanged, Store


ACCOUNT = "0123456789"


def payment(transaction_id: int, code: str, amount: int) -> dict:
    return {
        "id": transaction_id, "accountNumber": ACCOUNT,
        "transferAmount": amount, "transferType": "in",
        "content": code, "code": None,
    }


class DirectCheckoutTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(
            Path(self.temp.name) / "shop.sqlite3", Fernet.generate_key().decode("ascii"),
        )
        self.user_id = 1001
        self.store.ensure_user(self.user_id)

    def tearDown(self):
        self.temp.cleanup()

    def test_single_product_checkout_rechecks_price_and_preserves_pending_cart(self):
        product = self.store.add_product("Account", 10_000)
        self.store.bulk_add_stock(product, ["one", "two", "three"])
        self.store.cart_add(self.user_id, product)
        quote = self.store.quote_product_quantity(self.user_id, product, 2)
        self.store.set_price(product, 12_000)
        with self.assertRaises(PriceChanged):
            self.store.create_direct_checkout_product(
                self.user_id, product, 2, quote["price"], 100_000,
            )
        self.assertEqual(self.store.recent_direct_checkouts(self.user_id), [])
        self.assertEqual(self.store.cart_items_view(self.user_id)[0]["quantity"], 1)
        checkout = self.store.create_direct_checkout_product(
            self.user_id, product, 2, 24_000, 100_000,
        )
        self.assertEqual(checkout["amount"], 24_000)
        self.assertEqual(self.store.cart_items_view(self.user_id)[0]["quantity"], 1)
        result = process_sepay_payload(
            self.store, payment(654321, checkout["code"], 24_000), ACCOUNT,
        )
        self.assertEqual(result["direct_status"], "fulfilled")
        self.assertEqual(len(self.store.list_orders(self.user_id)), 2)

    def test_two_products_coupon_and_email_are_paid_and_delivered_once(self):
        first = self.store.add_product("First", 20_000)
        second = self.store.add_product("Second", 30_000)
        self.store.add_stock(first, "first-login")
        self.store.add_stock(second, "second-login")
        self.store.set_requires_email(second, True)
        self.store.create_coupon("WELCOME", "percent", 10, 10)
        self.store.cart_add(self.user_id, first)
        self.store.cart_add(self.user_id, second)
        with self.assertRaises(EmailRequired):
            self.store.create_direct_checkout(self.user_id, 100_000)
        self.store.cart_set_email(self.user_id, second, "buyer@example.com")
        self.store.cart_set_coupon(self.user_id, "WELCOME")
        checkout = self.store.create_direct_checkout(self.user_id, 100_000)
        self.assertEqual(checkout["amount"], 45_000)
        self.assertEqual(self.store.cart_items_view(self.user_id), [])

        result = process_sepay_payload(
            self.store, payment(12345, checkout["code"], 45_000), ACCOUNT,
            notify_admin_ids=frozenset({1}),
        )
        self.assertEqual(result["direct_status"], "fulfilled")
        self.assertEqual(result["order_count"], 2)
        self.assertEqual(self.store.get_balance(self.user_id), 0)
        orders = self.store.list_orders(self.user_id)
        self.assertEqual(len(orders), 2)
        self.assertEqual(sum(item["price"] for item in orders), 45_000)
        self.assertEqual(self.store.list_coupons()[0]["used"], 1)
        self.assertEqual(len(self.store.list_due_direct_checkout_orders()), 2)
        self.assertEqual(len(self.store.list_due_purchase_notifications()), 1)
        self.store.finish_direct_checkout_order(orders[0]["id"])
        self.assertEqual(len(self.store.list_due_direct_checkout_orders()), 1)
        self.assertEqual(process_sepay_payload(
            self.store, payment(12345, checkout["code"], 45_000), ACCOUNT,
        )["status"], "duplicate")
        self.assertEqual(len(self.store.list_orders(self.user_id)), 2)

    def test_partial_payment_then_completion_and_extra_payment(self):
        product_id = self.store.add_product("Account", 10_000)
        self.store.add_stock(product_id, "login")
        self.store.cart_add(self.user_id, product_id)
        checkout = self.store.create_direct_checkout(self.user_id, 100_000)
        first = process_sepay_payload(
            self.store, payment(1, checkout["code"], 4_000), ACCOUNT,
        )
        self.assertEqual(first["direct_status"], "partial")
        self.assertEqual(self.store.get_balance(self.user_id), 4_000)
        self.assertEqual(self.store.list_orders(self.user_id), [])
        second = process_sepay_payload(
            self.store, payment(2, checkout["code"], 6_000), ACCOUNT,
        )
        self.assertEqual(second["direct_status"], "fulfilled")
        self.assertEqual(len(self.store.list_orders(self.user_id)), 1)
        self.assertEqual(self.store.get_balance(self.user_id), 0)
        extra = process_sepay_payload(
            self.store, payment(3, checkout["code"], 2_000), ACCOUNT,
        )
        self.assertEqual(extra["direct_status"], "extra")
        self.assertEqual(self.store.get_balance(self.user_id), 2_000)
        self.assertEqual(len(self.store.list_orders(self.user_id)), 1)

    def test_product_coupon_is_recorded_on_the_discounted_cart_item(self):
        first = self.store.add_product("First", 20_000)
        second = self.store.add_product("Second", 30_000)
        self.store.add_stock(first, "first")
        self.store.add_stock(second, "second")
        self.store.create_coupon("SECOND5", "fixed", 5_000, 10, product_id=second)
        self.store.cart_add(self.user_id, first)
        self.store.cart_add(self.user_id, second)
        self.store.cart_set_coupon(self.user_id, "SECOND5")
        checkout = self.store.create_direct_checkout(self.user_id, 100_000)
        result = process_sepay_payload(
            self.store, payment(8, checkout["code"], 45_000), ACCOUNT,
        )
        self.assertEqual(result["direct_status"], "fulfilled")
        with closing(self.store._connect()) as conn:
            redemption = conn.execute(
                """SELECT o.product_id FROM coupon_redemptions r
                   JOIN orders o ON o.id=r.order_id WHERE r.code='SECOND5'"""
            ).fetchone()
        self.assertEqual(redemption["product_id"], second)
        self.assertEqual(self.store.get_balance(self.user_id), 0)

    def test_price_change_or_expiry_keeps_bank_money_in_balance(self):
        product_id = self.store.add_product("Account", 10_000)
        self.store.bulk_add_stock(product_id, ["one", "two"])
        self.store.cart_add(self.user_id, product_id)
        changed = self.store.create_direct_checkout(self.user_id, 100_000)
        self.store.set_price(product_id, 12_000)
        result = process_sepay_payload(
            self.store, payment(4, changed["code"], 10_000), ACCOUNT,
        )
        self.assertEqual(result["direct_status"], "failed")
        self.assertEqual(result["failure_reason"], "PriceChanged")
        self.assertEqual(self.store.get_balance(self.user_id), 10_000)
        self.assertEqual(self.store.list_orders(self.user_id), [])

        self.store.cart_add(self.user_id, product_id)
        expired = self.store.create_direct_checkout(self.user_id, 100_000)
        with self.store._write() as conn:
            conn.execute(
                "UPDATE direct_checkouts SET expires_at='2000-01-01T00:00:00.000Z' WHERE id=?",
                (expired["id"],),
            )
        late = process_sepay_payload(
            self.store, payment(5, expired["code"], 12_000), ACCOUNT,
        )
        self.assertEqual(late["direct_status"], "expired")
        self.assertEqual(self.store.get_balance(self.user_id), 22_000)
        self.assertEqual(self.store.list_orders(self.user_id), [])

    def test_wallet_cart_purchase_is_atomic(self):
        first = self.store.add_product("First", 15_000)
        second = self.store.add_product("Second", 20_000)
        self.store.add_stock(first, "one")
        self.store.add_stock(second, "two")
        self.store.cart_add(self.user_id, first)
        self.store.cart_add(self.user_id, second)
        with self.assertRaises(InsufficientBalance):
            self.store.purchase_cart(self.user_id, 35_000)
        self.assertEqual(len(self.store.cart_items_view(self.user_id)), 2)
        topup = self.store.create_topup(self.user_id, 35_000)
        process_sepay_payload(self.store, payment(6, topup["code"], 35_000), ACCOUNT)
        with self.assertRaises(PriceChanged):
            self.store.purchase_cart(self.user_id, 34_000)
        purchased = self.store.purchase_cart(self.user_id, 35_000)
        self.assertEqual(len(purchased["orders"]), 2)
        self.assertEqual(self.store.get_balance(self.user_id), 0)
        self.assertEqual(self.store.cart_items_view(self.user_id), [])

    def test_bad_second_stock_record_does_not_partially_deliver(self):
        first = self.store.add_product("First", 10_000)
        second = self.store.add_product("Second", 10_000)
        self.store.add_stock(first, "good")
        bad_stock_id = self.store.add_stock(second, "bad")
        self.store.cart_add(self.user_id, first)
        self.store.cart_add(self.user_id, second)
        checkout = self.store.create_direct_checkout(self.user_id, 100_000)
        with self.store._write() as conn:
            conn.execute(
                "UPDATE stock SET secret_ciphertext=? WHERE id=?",
                (self.store.cipher.encrypt(b"\xff").decode("ascii"), bad_stock_id),
            )
        result = process_sepay_payload(
            self.store, payment(7, checkout["code"], 20_000), ACCOUNT,
        )
        self.assertEqual(result["direct_status"], "failed")
        self.assertEqual(self.store.get_balance(self.user_id), 20_000)
        self.assertEqual(self.store.list_orders(self.user_id), [])
        self.assertEqual(self.store.get_product(first)["available"], 1)

    def test_manual_match_of_direct_payment_credits_wallet_and_closes_checkout(self):
        product = self.store.add_product("Account", 10_000)
        self.store.add_stock(product, "login")
        self.store.cart_add(self.user_id, product)
        checkout = self.store.create_direct_checkout(self.user_id, 100_000)
        result = process_sepay_payload(
            self.store, payment(9, "khong co ma", 10_000), ACCOUNT,
        )
        self.assertEqual(result["status"], "unmatched")
        event_id = self.store.list_unmatched_events()[0]["id"]
        self.assertEqual(self.store.preview_manual_credit(
            event_id, checkout["code"], ACCOUNT,
        )["topup_id"], checkout["topup_id"])
        credited = self.store.resolve_manual_credit(
            event_id, checkout["topup_id"], 10_000, ACCOUNT, admin_id=1,
        )
        self.assertEqual(credited["direct_status"], "failed")
        self.assertEqual(self.store.get_balance(self.user_id), 10_000)
        self.assertEqual(self.store.get_direct_checkout(self.user_id, checkout["id"])["status"],
                         "failed")
        self.assertEqual(self.store.list_orders(self.user_id), [])
