from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
import json
import sqlite3
import unittest

from cryptography.fernet import Fernet

from payments import process_sepay_payload
from reconciliation import (
    PAGE_LIMIT, ReconciliationError, _fetch_with_windows_curl, fetch_transactions, normalize_transaction,
    reconcile_once,
)
from store import Store


ACCOUNT = "0123456789"


def api_transaction(transaction_id, content, amount="50000.00", account=ACCOUNT,
                    amount_out="0.00"):
    return {
        "id": str(transaction_id), "account_number": account,
        "amount_in": amount, "amount_out": amount_out,
        "transaction_content": content, "code": None,
    }


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(
            Path(self.temp.name) / "shop.sqlite3", Fernet.generate_key().decode("ascii"),
        )
        self.store.ensure_user(1001)
        self.topup = self.store.create_topup(1001, 50_000)

    def tearDown(self):
        self.temp.cleanup()

    def test_missed_webhook_is_credited_once_and_cursor_persists(self):
        transaction = api_transaction(501, "CK " + self.topup["code"], "49000.00")
        calls = []

        def fetcher(token, account, last_id):
            calls.append((account, last_id))
            return [transaction]

        first = reconcile_once(self.store, "test-token", ACCOUNT, fetcher)
        second = reconcile_once(self.store, "test-token", ACCOUNT, fetcher)

        self.assertEqual((first["credited"], second["duplicate"]), (1, 1))
        self.assertEqual(self.store.get_balance(1001), 49_000)
        self.assertEqual(self.store.list_topups(1001)[0]["paid_amount"], 49_000)
        self.assertEqual(self.store.get_reconciliation_state(ACCOUNT)["last_txn_id"], 501)
        self.assertEqual(calls, [(ACCOUNT, 0), (ACCOUNT, 501)])
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ledger WHERE kind='topup'").fetchone()[0], 1)

    def test_webhook_first_then_reconciliation_does_not_double_credit(self):
        transaction = api_transaction(601, self.topup["code"])
        webhook = {
            "id": 601, "accountNumber": ACCOUNT, "transferAmount": 50_000,
            "transferType": "in", "content": self.topup["code"], "code": None,
        }
        process_sepay_payload(self.store, webhook, ACCOUNT)
        result = reconcile_once(
            self.store, "test-token", ACCOUNT, lambda *_: [transaction],
        )
        self.assertEqual((result["credited"], result["duplicate"]), (0, 1))
        self.assertEqual(self.store.get_balance(1001), 50_000)

    def test_virtual_account_transfer_is_reconciled_through_main_account(self):
        virtual_account = "VQRQAMHLG6528"
        transaction = api_transaction(602, self.topup["code"])
        transaction["sub_account"] = virtual_account
        requested_accounts = []

        def fetcher(token, account, last_id):
            requested_accounts.append(account)
            return [transaction]

        result = reconcile_once(self.store, "test-token", ACCOUNT, fetcher)
        self.assertEqual(result["credited"], 1)
        self.assertEqual(requested_accounts, [ACCOUNT])
        self.assertEqual(self.store.get_balance(1001), 50_000)

    def test_malformed_page_and_full_page_do_not_advance_or_credit(self):
        good = api_transaction(701, self.topup["code"])
        bad = api_transaction("not-an-id", self.topup["code"])
        with self.assertRaises(ReconciliationError):
            reconcile_once(self.store, "test-token", ACCOUNT, lambda *_: [good, bad])
        with self.assertRaises(ReconciliationError):
            reconcile_once(self.store, "test-token", ACCOUNT, lambda *_: [good] * PAGE_LIMIT)
        self.assertIsNone(self.store.get_reconciliation_state(ACCOUNT))
        self.assertEqual(self.store.get_balance(1001), 0)

    def test_wrong_account_and_fractional_amount_are_rejected(self):
        with self.assertRaises(ReconciliationError):
            normalize_transaction(api_transaction(801, "x", account="99999"), ACCOUNT)
        with self.assertRaises(ReconciliationError):
            normalize_transaction(api_transaction(802, "x", amount="100.50"), ACCOUNT)
        with self.assertRaises(ReconciliationError):
            normalize_transaction(api_transaction("uuid-id", "x"), ACCOUNT)

    def test_outgoing_is_recorded_but_never_credited(self):
        outbound = api_transaction(901, self.topup["code"], amount="0.00", amount_out="50000.00")
        result = reconcile_once(self.store, "test-token", ACCOUNT, lambda *_: [outbound])
        self.assertEqual((result["ignored"], self.store.get_balance(1001)), (1, 0))

    def test_webhook_race_with_reconciliation_is_idempotent(self):
        transaction = api_transaction(1001, self.topup["code"])
        webhook = {
            "id": 1001, "accountNumber": ACCOUNT, "transferAmount": 50_000,
            "transferType": "in", "content": self.topup["code"], "code": None,
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(process_sepay_payload, self.store, webhook, ACCOUNT),
                pool.submit(reconcile_once, self.store, "test-token", ACCOUNT,
                            lambda *_: [transaction]),
            ]
            for future in futures:
                future.result()
        self.assertEqual(self.store.get_balance(1001), 50_000)

    def test_cursor_write_failure_retries_without_double_credit(self):
        transaction = api_transaction(1051, self.topup["code"])
        with patch.object(self.store, "mark_reconciled", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                reconcile_once(self.store, "test-token", ACCOUNT, lambda *_: [transaction])
        self.assertEqual(self.store.get_balance(1001), 50_000)
        self.assertIsNone(self.store.get_reconciliation_state(ACCOUNT))
        second = reconcile_once(self.store, "test-token", ACCOUNT, lambda *_: [transaction])
        self.assertEqual(second["duplicate"], 1)
        self.assertEqual(self.store.get_balance(1001), 50_000)

    def test_fetch_uses_documented_v1_endpoint_and_checks_response(self):
        response = json.dumps({
            "status": 200, "error": None, "messages": {"success": True},
            "transactions": [api_transaction(1101, "test")],
        }).encode("utf-8")
        with patch("reconciliation._fetch_response", return_value=(200, response)) as opener:
            transactions = fetch_transactions("test-token", ACCOUNT, 1234)
        self.assertEqual(len(transactions), 1)
        url, token = opener.call_args.args
        self.assertEqual(urlparse(url).netloc, "my.sepay.vn")
        self.assertEqual(parse_qs(urlparse(url).query)["since_id"], ["1134"])
        self.assertEqual(token, "test-token")

    def test_auth_error_does_not_include_token(self):
        error = HTTPError("https://my.sepay.vn/userapi/transactions/list", 401, "Unauthorized", {}, None)
        with patch("reconciliation._fetch_response", side_effect=error):
            with self.assertRaises(ReconciliationError) as caught:
                fetch_transactions("test-token", ACCOUNT, 0)
        self.assertNotIn("test-token", str(caught.exception))

    def test_windows_curl_receives_token_only_on_stdin(self):
        with patch("reconciliation.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout=b'{}\n200',
        )) as run:
            status, body = _fetch_with_windows_curl("https://my.sepay.vn/test", "test-token")
        self.assertEqual((status, body), (200, b'{}'))
        args, kwargs = run.call_args
        self.assertNotIn("test-token", repr(args))
        self.assertIn(b"Authorization: Bearer test-token", kwargs["input"])
        self.assertIn("--ipv4", args[0])
        self.assertEqual(args[0][args[0].index("--header") + 1], "@-")


if __name__ == "__main__":
    unittest.main()
