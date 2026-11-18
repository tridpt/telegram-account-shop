from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from cryptography.fernet import Fernet

from store import (
    CouponUnavailable, NotFound, OutOfStock, PriceChanged, SaleActive, SaleCouponConflict,
    SHOP_TIMEZONE, Store,
)


class PromotionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(
            Path(self.temp.name) / "shop.sqlite3", Fernet.generate_key().decode("ascii"),
        )
        for user_id in (1001, 1002):
            self.store.ensure_user(user_id)
            topup = self.store.create_topup(user_id, 200_000)
            self.store.record_bank_event(
                f"promo-bank-{user_id}", "0123456789", 200_000, topup["code"],
                "in", topup["code"], "0123456789",
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_lower_manual_price_keeps_only_last_old_price_and_raise_clears_it(self):
        first = self.store.add_product("Account A", 50_000)
        second = self.store.add_product("Account B", 40_000)
        bundle_id = self.store.create_bundle("Combo", 75_000, {first: 1, second: 1})

        self.store.set_price(first, 45_000)
        self.assertEqual(self.store.get_product(first)["previous_price"], 50_000)
        self.store.set_price(first, 45_000)
        self.assertEqual(self.store.get_product(first)["previous_price"], 50_000)
        self.store.set_price(first, 40_000)
        self.assertEqual(self.store.get_product(first)["previous_price"], 45_000)
        self.store.set_price(first, 60_000)
        self.assertIsNone(self.store.get_product(first)["previous_price"])

        self.store.update_bundle(bundle_id, price=65_000)
        self.assertEqual(self.store.get_bundle(bundle_id)["previous_price"], 75_000)
        self.store.update_bundle(bundle_id, name="Combo mới")
        self.assertEqual(self.store.get_bundle(bundle_id)["previous_price"], 75_000)
        self.store.update_bundle(bundle_id, price=80_000)
        self.assertIsNone(self.store.get_bundle(bundle_id)["previous_price"])

    def test_coupon_discount_is_recorded_once_per_user_and_globally_limited(self):
        product_id = self.store.add_product("Account", 50_000)
        self.store.bulk_add_stock(product_id, ["login-a", "login-b", "login-c"])
        self.store.create_coupon("sale10", "percent", 10, 1)

        quote = self.store.quote_product(1001, product_id, "SaLe10")
        self.assertEqual((quote["price"], quote["discount"]), (45_000, 5_000))
        order = self.store.purchase(1001, product_id, quote["price"], "SALE10")
        self.assertEqual((order["price"], order["balance"]), (45_000, 155_000))
        self.assertEqual(self.store.get_order(1001, order["id"])["price"], 45_000)
        self.assertEqual(self.store.list_coupons()[0]["used"], 1)
        with self.assertRaises(CouponUnavailable):
            self.store.purchase(1001, product_id, 45_000, "SALE10")
        with self.assertRaises(CouponUnavailable):
            self.store.purchase(1002, product_id, 45_000, "SALE10")
        self.assertEqual(self.store.get_balance(1002), 200_000)
        self.assertEqual(self.store.get_product(product_id)["available"], 2)

    def test_coupon_expiry_deactivation_and_minimum_price(self):
        product_id = self.store.add_product("Account", 20_000)
        self.store.add_stock(product_id, "login-a")
        tomorrow = (datetime.now(SHOP_TIMEZONE) + timedelta(days=1)).date().isoformat()
        with self.assertRaises(ValueError):
            self.store.create_coupon("bad_date", "fixed", 5_000, 10, "20261231")
        self.store.create_coupon("fixed5", "fixed", 5_000, 10, tomorrow)
        self.assertEqual(self.store.quote_product(1001, product_id, "fixed5")["price"], 15_000)
        self.store.set_coupon_active("FIXED5", False)
        with self.assertRaises(CouponUnavailable):
            self.store.quote_product(1001, product_id, "fixed5")
        self.store.set_coupon_active("FIXED5", True)
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("UPDATE coupons SET expires_on='2000-01-01' WHERE code='FIXED5'")
            conn.commit()
        with self.assertRaises(CouponUnavailable):
            self.store.quote_product(1001, product_id, "fixed5")
        self.store.create_coupon("too_big", "fixed", 20_000, 10)
        with self.assertRaises(CouponUnavailable):
            self.store.quote_product(1001, product_id, "too_big")

    def test_product_coupon_only_discounts_its_product(self):
        first = self.store.add_product("Account A", 50_000)
        second = self.store.add_product("Account B", 40_000)
        self.store.bulk_add_stock(first, ["a-1", "a-2"])
        self.store.bulk_add_stock(second, ["b-1", "b-2"])
        bundle_id = self.store.create_bundle("Combo", 70_000, {first: 1, second: 1})
        self.store.create_coupon("ONLYA", "percent", 10, 2, product_id=first)

        coupon = self.store.list_coupons()[0]
        self.assertEqual((coupon["product_id"], coupon["product_name"]),
                         (first, "Account A"))
        self.assertEqual(self.store.quote_product(1001, first, "ONLYA")["price"], 45_000)
        with self.assertRaises(CouponUnavailable):
            self.store.quote_product(1001, second, "ONLYA")
        with self.assertRaises(CouponUnavailable):
            self.store.quote_bundle(1001, bundle_id, "ONLYA")
        with self.assertRaises(CouponUnavailable):
            self.store.purchase(1001, second, expected_price=36_000, coupon_code="ONLYA")
        with self.assertRaises(CouponUnavailable):
            self.store.purchase_bundle(1001, bundle_id, coupon_code="ONLYA")
        self.assertEqual(self.store.get_balance(1001), 200_000)
        self.assertEqual(self.store.get_product(second)["available"], 2)

        order = self.store.purchase(1001, first, expected_price=45_000, coupon_code="ONLYA")
        self.assertEqual((order["price"], order["coupon_code"]), (45_000, "ONLYA"))
        self.assertEqual(self.store.list_coupons()[0]["used"], 1)
        with self.assertRaises(CouponUnavailable):
            self.store.purchase(1001, first, expected_price=45_000, coupon_code="ONLYA")

    def test_product_coupon_requires_existing_product_and_legacy_codes_stay_global(self):
        with self.assertRaises(NotFound):
            self.store.create_coupon("MISSING", "fixed", 5_000, 2, product_id=999)
        self.assertEqual(self.store.list_coupons(), [])

        legacy_path = Path(self.temp.name) / "legacy-coupons.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript("""
                CREATE TABLE coupons (
                    code TEXT PRIMARY KEY, kind TEXT NOT NULL, value INTEGER NOT NULL,
                    max_uses INTEGER NOT NULL, expires_on TEXT,
                    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
                );
                INSERT INTO coupons VALUES ('OLD10', 'percent', 10, 5, NULL, 1, '2026-01-01');
            """)
        key = Fernet.generate_key().decode("ascii")
        migrated = Store(legacy_path, key)
        Store(legacy_path, key)
        product_id = migrated.add_product("Old product", 50_000)
        migrated.add_stock(product_id, "legacy-stock")
        self.assertEqual(migrated.list_coupons()[0]["product_id"], None)
        self.assertEqual(migrated.quote_product(1001, product_id, "OLD10")["price"], 45_000)

    def test_limited_sale_charges_sale_price_only_for_first_units(self):
        product_id = self.store.add_product("Account", 50_000)
        self.store.bulk_add_stock(product_id, ["a", "b", "c"])
        sale = self.store.start_sale(product_id, 45_000, 2)

        quote = self.store.quote_product(1001, product_id)
        self.assertEqual((quote["base_price"], quote["price"], quote["sale_remaining"]),
                         (50_000, 45_000, 2))
        first = self.store.purchase(1001, product_id, expected_price=45_000)
        second = self.store.purchase(1002, product_id, expected_price=45_000)
        self.assertEqual((first["price"], second["price"]), (45_000, 45_000))
        self.assertEqual((first["sale_id"], second["sale_id"]), (sale["id"], sale["id"]))
        self.assertIsNone(self.store.get_product(product_id)["sale_id"])

        with self.assertRaises(PriceChanged):
            self.store.purchase(1001, product_id, expected_price=45_000)
        self.assertEqual(self.store.get_product(product_id)["available"], 1)
        third = self.store.purchase(1001, product_id, expected_price=50_000)
        self.assertEqual((third["price"], third["sale_id"]), (50_000, None))
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT sold_units, active FROM product_sales WHERE id=?",
                             (sale["id"],)).fetchone(),
                (2, 0),
            )

    def test_concurrent_buyers_cannot_exceed_sale_limit(self):
        product_id = self.store.add_product("Account", 50_000)
        self.store.bulk_add_stock(product_id, ["a", "b"])
        self.store.start_sale(product_id, 45_000, 1)

        def buy(user_id):
            try:
                return self.store.purchase(user_id, product_id, expected_price=45_000)
            except PriceChanged:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, (1001, 1002)))
        self.assertEqual(sum(order is not None for order in results), 1)
        self.assertEqual(self.store.get_product(product_id)["available"], 1)
        self.assertEqual(
            sorted(self.store.get_balance(user_id) for user_id in (1001, 1002)),
            [155_000, 200_000],
        )

    def test_sale_cannot_stack_with_coupon_or_change_regular_price(self):
        product_id = self.store.add_product("Account", 50_000)
        self.store.bulk_add_stock(product_id, ["a", "b"])
        self.store.create_coupon("SAVE10", "percent", 10, 2)
        self.store.start_sale(product_id, 45_000, 1)
        with self.assertRaises(SaleCouponConflict):
            self.store.quote_product(1001, product_id, "SAVE10")
        with self.assertRaises(SaleCouponConflict):
            self.store.purchase(1001, product_id, 45_000, "SAVE10")
        with self.assertRaises(SaleActive):
            self.store.set_price(product_id, 60_000)
        self.store.end_sale(product_id)
        self.assertEqual(self.store.quote_product(1001, product_id, "SAVE10")["price"], 45_000)
        self.store.set_price(product_id, 60_000)
        self.assertEqual(self.store.get_product(product_id)["price"], 60_000)

    def test_sale_requires_stock_and_is_cancelled_when_product_hidden(self):
        product_id = self.store.add_product("Account", 50_000)
        with self.assertRaises(OutOfStock):
            self.store.start_sale(product_id, 45_000, 1)
        self.store.add_stock(product_id, "a")
        with self.assertRaises(ValueError):
            self.store.start_sale(product_id, 45_000, 2)
        self.store.start_sale(product_id, 45_000, 1)
        with self.assertRaises(SaleActive):
            self.store.start_sale(product_id, 40_000, 1)
        self.store.set_active(product_id, False)
        self.store.set_active(product_id, True)
        self.assertIsNone(self.store.get_product(product_id)["sale_id"])

    def test_bundle_sells_all_accounts_once_and_counts_one_sale(self):
        first = self.store.add_product("A", 40_000)
        second = self.store.add_product("B", 30_000)
        self.store.bulk_add_stock(first, ["a-1", "a-2", "a-3"])
        self.store.add_stock(second, "b-1")
        bundle_id = self.store.create_bundle("Combo", 95_000, {first: 2, second: 1})
        self.store.create_coupon("combo5", "fixed", 5_000, 10)
        quote = self.store.quote_bundle(1001, bundle_id, "COMBO5")
        self.assertEqual(quote["price"], 90_000)

        sale = self.store.purchase_bundle(1001, bundle_id, 90_000, "combo5")
        self.assertEqual(len(sale["orders"]), 3)
        self.assertEqual(sum(order["price"] for order in sale["orders"]), 90_000)
        self.assertEqual({order["secret"] for order in sale["orders"]}, {"a-1", "a-2", "b-1"})
        self.assertEqual(self.store.get_balance(1001), 110_000)
        self.assertEqual(self.store.get_user_summary(1001)["order_count"], 1)
        purchases = self.store.list_purchases(1001)
        self.assertEqual(len(purchases), 1)
        self.assertEqual(len(purchases[0]["items"]), 3)
        warranty = self.store.create_warranty(1001, sale["orders"][2]["id"], "Bundle issue")
        self.assertEqual(warranty["order_id"], sale["orders"][0]["id"])
        self.assertEqual(self.store.get_user_summary(1001)["total_spent"], 90_000)
        report = self.store.daily_report(datetime.now(SHOP_TIMEZONE).date())
        self.assertEqual((report["revenue"], report["orders"]), (90_000, 1))
        self.assertEqual(self.store.list_coupons()[0]["used"], 1)
        for order in sale["orders"]:
            self.assertEqual(self.store.get_order(1001, order["id"])["secret"], order["secret"])
        self.assertIsNone(self.store.get_order(1002, sale["orders"][0]["id"]))

    def test_bundle_shortage_and_price_change_leave_balance_and_stock_untouched(self):
        first = self.store.add_product("A", 40_000)
        second = self.store.add_product("B", 30_000)
        self.store.add_stock(first, "a-1")
        self.store.add_stock(second, "b-1")
        bundle_id = self.store.create_bundle("Combo", 55_000, {first: 1, second: 1})
        self.assertEqual(self.store.quote_bundle(1001, bundle_id)["price"], 55_000)
        self.store.update_bundle(bundle_id, price=50_000)
        with self.assertRaises(PriceChanged):
            self.store.purchase_bundle(1001, bundle_id, expected_price=55_000)
        self.store.purchase(1002, second)
        with self.assertRaises(OutOfStock):
            self.store.purchase_bundle(1001, bundle_id)
        self.assertEqual(self.store.get_balance(1001), 200_000)
        self.assertEqual(self.store.get_product(first)["available"], 1)

    def test_concurrent_redemption_does_not_exceed_global_limit(self):
        product_id = self.store.add_product("Account", 40_000)
        self.store.bulk_add_stock(product_id, ["a-1", "a-2"])
        self.store.create_coupon("onlyone", "fixed", 10_000, 1)

        def buy(user_id):
            try:
                self.store.purchase(user_id, product_id, 30_000, "ONLYONE")
                return "bought"
            except CouponUnavailable:
                return "unavailable"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, (1001, 1002)))
        self.assertEqual(sorted(results), ["bought", "unavailable"])
        self.assertEqual(self.store.list_coupons()[0]["used"], 1)
        self.assertEqual(self.store.get_product(product_id)["available"], 1)


if __name__ == "__main__":
    unittest.main()
