from contextlib import closing
from datetime import date
from io import StringIO, BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
import csv
import sqlite3
import unittest

from cryptography.fernet import Fernet

from export_reports import (build_export_archive, build_sold_products_csv,
                            build_users_csv, parse_date_range)
from store import ExportTooLarge, Store


ACCOUNT = "0123456789"


def read_csv(archive: ZipFile, name: str) -> list[dict]:
    return list(csv.DictReader(StringIO(archive.read(name).decode("utf-8-sig"))))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "shop.sqlite3", Fernet.generate_key().decode())
        self.store.ensure_user(1001, "=HYPERLINK(fake)", "Customer")

    def test_export_uses_actual_credits_and_bundle_items_without_exposing_stock(self):
        topup = self.store.create_topup(1001, 50_000)
        self.store.record_bank_event(
            "bank-auto", ACCOUNT, 30_000, topup["code"], "in", topup["code"], ACCOUNT,
        )
        self.store.record_bank_event(
            "bank-manual", ACCOUNT, 40_000, "wrong code", "in", None, ACCOUNT,
        )
        event_id = self.store.list_unmatched_events()[0]["id"]
        self.store.resolve_manual_credit(event_id, topup["id"], 40_000, ACCOUNT, 1)
        self.store.record_bank_event(
            "bank-unmatched", ACCOUNT, 5_000, "wrong code", "in", None, ACCOUNT,
        )
        first = self.store.add_product("=DANGEROUS", 10_000)
        second = self.store.add_product("Other product", 20_000)
        self.store.bulk_add_stock(first, ["secret-password-1", "secret-password-2"])
        self.store.add_stock(second, "secret-password-3")
        self.store.purchase(1001, first)
        bundle_id = self.store.create_bundle("Bundle", 25_000, {first: 1, second: 1})
        self.store.purchase_bundle(1001, bundle_id)
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute(
                """UPDATE ledger SET created_at='2026-09-25T16:59:59.000Z'
                   WHERE kind='topup' AND ref_id=(
                       SELECT id FROM bank_events WHERE provider_txn_id='bank-auto')"""
            )
            conn.execute(
                """UPDATE ledger SET created_at='2026-09-25T17:00:00.000Z'
                   WHERE kind='topup' AND ref_id=?""", (event_id,),
            )
            conn.execute(
                "UPDATE manual_credit_resolutions SET created_at='2026-09-25T17:00:00.000Z'"
            )
            conn.execute("UPDATE orders SET created_at='2026-09-25T17:00:00.000Z'")
            conn.commit()

        report = build_export_archive(self.store, date(2026, 9, 26), date(2026, 9, 26))
        self.assertEqual((report["topup_total"], report["revenue"], report["orders"],
                          report["items"], report["manual_credits"]),
                         (40_000, 35_000, 2, 3, 1))
        self.assertEqual(self.store.daily_report(date(2026, 9, 26))["topups"],
                         report["topup_total"])
        self.assertEqual(self.store.daily_report(date(2026, 9, 26))["revenue"],
                         report["revenue"])
        with ZipFile(BytesIO(report["content"])) as archive:
            self.assertEqual(set(archive.namelist()), {
                "topups.csv", "orders.csv", "manual_credits.csv", "README.txt",
            })
            topups = read_csv(archive, "topups.csv")
            orders = read_csv(archive, "orders.csv")
            manual = read_csv(archive, "manual_credits.csv")
            self.assertEqual(len(topups), 1)
            self.assertEqual(topups[0]["bank_transaction_id"], "bank-manual")
            self.assertEqual(topups[0]["amount_vnd"], "40000")
            self.assertEqual(topups[0]["credit_method"], "manual")
            self.assertEqual(topups[0]["credited_at_gmt7"], "2026-09-26 00:00:00")
            self.assertTrue(topups[0]["username"].startswith("'="))
            self.assertEqual(len(orders), 3)
            self.assertTrue(any(row["product_name"].startswith("'=") for row in orders))
            self.assertEqual(len({row["bundle_order_id"] for row in orders
                                  if row["bundle_order_id"]}), 1)
            self.assertEqual(manual[0]["admin_id"], "1")
            self.assertIn("KHONG cong lai", archive.read("README.txt").decode())
            self.assertNotIn(b"secret-password", report["content"])
        sold = build_sold_products_csv(self.store)
        self.assertEqual((sold["orders"], sold["products"], sold["units"]),
                         (2, 3, 3))

    def test_date_range_and_size_limits(self):
        for text in ("2026-02-30 2026-03-01", "2026-09-30 2026-09-01",
                     "2026-01-01 2026-04-01", "2026-09-01", "2026-9-1 2026-9-2"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_date_range(text)
        self.assertEqual(parse_date_range("2026-09-01 2026-09-30"),
                         (date(2026, 9, 1), date(2026, 9, 30)))
        topup = self.store.create_topup(1001, 50_000)
        for transaction_id in ("bank-1", "bank-2"):
            self.store.record_bank_event(
                transaction_id, ACCOUNT, 20_000, topup["code"], "in", topup["code"], ACCOUNT,
            )
        with self.assertRaises(ExportTooLarge):
            self.store.export_report_rows("2020-01-01", "2030-01-01", max_rows=1)

    def test_empty_period_still_has_csv_headers(self):
        report = build_export_archive(self.store, date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual((report["topups"], report["orders"], report["manual_credits"]),
                         (0, 0, 0))
        with ZipFile(BytesIO(report["content"])) as archive:
            self.assertEqual(read_csv(archive, "topups.csv"), [])
            self.assertEqual(read_csv(archive, "orders.csv"), [])
            self.assertEqual(read_csv(archive, "manual_credits.csv"), [])
        self.assertIn(b"purchase_id", build_sold_products_csv(self.store)["content"])

    def test_customer_and_sold_product_files_group_by_purchase_without_secrets(self):
        self.store.ensure_user(1, "shop_admin", "Shop")
        self.store.ensure_user(1002, "guest1002", "Guest")
        topup = self.store.create_topup(1001, 50_000)
        self.store.record_bank_event(
            "bank-export", ACCOUNT, 50_000, topup["code"], "in",
            topup["code"], ACCOUNT,
        )
        product_id = self.store.add_product("=Account", 10_000)
        self.store.bulk_add_stock(product_id,
                                  ["secret-password-1", "secret-password-2",
                                   "secret-password-3"])
        self.store.set_requires_email(product_id, True)
        first = self.store.purchase(
            1001, product_id, customer_email="one@example.com,two@example.com",
        )
        second = self.store.purchase(
            1001, product_id, customer_email="three@example.com",
        )

        users_file = build_users_csv(self.store, frozenset({1}))
        users = list(csv.DictReader(StringIO(users_file["content"].decode("utf-8-sig"))))
        self.assertEqual(users_file["users"], 2)
        self.assertEqual({row["telegram_id"] for row in users}, {"1001", "1002"})
        buyer = next(row for row in users if row["telegram_id"] == "1001")
        self.assertTrue(buyer["username"].startswith("'="))
        self.assertEqual((buyer["balance_vnd"], buyer["received_vnd"],
                          buyer["spent_vnd"], buyer["orders"]),
                         ("20000", "50000", "30000", "2"))

        sold_file = build_sold_products_csv(self.store)
        sold = list(csv.DictReader(StringIO(sold_file["content"].decode("utf-8-sig"))))
        self.assertEqual((sold_file["orders"], sold_file["products"],
                          sold_file["units"]), (2, 2, 3))
        self.assertEqual({row["purchase_id"] for row in sold},
                         {str(first["id"]), str(second["id"])})
        grouped = next(row for row in sold if row["purchase_id"] == str(first["id"]))
        self.assertEqual((grouped["quantity"], grouped["unit_price_vnd"],
                          grouped["total_vnd"]), ("2", "10000", "20000"))
        self.assertTrue(grouped["product_name"].startswith("'="))
        self.assertNotIn(b"secret-password", users_file["content"] + sold_file["content"])
        self.assertNotIn(b"one@example.com", sold_file["content"])
        with self.assertRaises(ExportTooLarge):
            self.store.export_users_rows(frozenset({1}), max_rows=1)
        with self.assertRaises(ExportTooLarge):
            self.store.export_sold_product_rows(max_rows=1)
