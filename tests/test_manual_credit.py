from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from cryptography.fernet import Fernet

from payments import process_sepay_payload
from store import ManualCreditUnavailable, NotFound, Store


ACCOUNT = "0123456789"


class ManualCreditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "shop.sqlite3", Fernet.generate_key().decode())
        self.store.ensure_user(1001, "alice", "Alice")
        self.topup = self.store.create_topup(1001, 50_000)
        self.store.record_bank_event(
            "sepay:9001", ACCOUNT, 49_000, "CHUYEN KHOAN SAI MA", "in", None, ACCOUNT,
        )
        self.event = self.store.list_unmatched_events()[0]

    def test_preview_and_confirm_credit_actual_amount_once_with_audit(self):
        preview = self.store.preview_manual_credit(
            self.event["id"], self.topup["code"].lower(), ACCOUNT,
        )
        self.assertEqual((preview["user_id"], preview["amount"]), (1001, 49_000))
        result = self.store.resolve_manual_credit(
            self.event["id"], preview["topup_id"], preview["amount"], ACCOUNT, 1,
        )
        self.assertEqual((result["amount"], result["balance"]), (49_000, 49_000))
        self.assertEqual(self.store.list_topups(1001)[0]["paid_amount"], 49_000)
        self.assertEqual(self.store.get_balance(1001), 49_000)
        self.assertEqual(self.store.list_unmatched_events(), [])
        audit = self.store.list_manual_credits()[0]
        self.assertEqual((audit["user_id"], audit["admin_id"], audit["amount"]),
                         (1001, 1, 49_000))
        with self.assertRaises(ManualCreditUnavailable):
            self.store.resolve_manual_credit(
                self.event["id"], preview["topup_id"], preview["amount"], ACCOUNT, 1,
            )
        duplicate = process_sepay_payload(self.store, {
            "id": 9001, "accountNumber": ACCOUNT, "transferAmount": 49_000,
            "transferType": "in", "content": self.topup["code"], "code": None,
        }, ACCOUNT)
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(self.store.get_balance(1001), 49_000)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE kind='topup' AND ref_id=?",
                (self.event["id"],),
            ).fetchone()[0], 1)

    def test_unknown_code_and_non_incoming_transactions_cannot_be_credited(self):
        with self.assertRaises(NotFound):
            self.store.preview_manual_credit(self.event["id"], "NAP000000000000", ACCOUNT)
        wrong = self.store.record_bank_event(
            "sepay:9002", "999999999", 50_000, "wrong account", "in", None, ACCOUNT,
        )
        self.assertEqual(wrong["status"], "ignored")
        with closing(sqlite3.connect(self.store.path)) as conn:
            wrong_id = conn.execute(
                "SELECT id FROM bank_events WHERE provider_txn_id='sepay:9002'"
            ).fetchone()[0]
        with self.assertRaises(ManualCreditUnavailable):
            self.store.resolve_manual_credit(wrong_id, self.topup["id"], 50_000, ACCOUNT, 1)
        with self.assertRaises(ManualCreditUnavailable):
            self.store.resolve_manual_credit(
                self.event["id"], self.topup["id"], 48_000, ACCOUNT, 1,
            )
        self.assertEqual(self.store.get_balance(1001), 0)

    def test_two_admins_racing_credit_only_once(self):
        def resolve(admin_id):
            try:
                return self.store.resolve_manual_credit(
                    self.event["id"], self.topup["id"], 49_000, ACCOUNT, admin_id,
                )["status"]
            except ManualCreditUnavailable:
                return "already_done"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(resolve, [1, 2]))
        self.assertEqual(sorted(outcomes), ["already_done", "credited"])
        self.assertEqual(self.store.get_balance(1001), 49_000)
        self.assertEqual(len(self.store.list_manual_credits()), 1)

    def test_audit_write_failure_rolls_back_credit(self):
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("""CREATE TRIGGER fail_manual_audit
                            BEFORE INSERT ON manual_credit_resolutions
                            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.resolve_manual_credit(
                self.event["id"], self.topup["id"], 49_000, ACCOUNT, 1,
            )
        self.assertEqual(self.store.get_balance(1001), 0)
        self.assertEqual(len(self.store.list_unmatched_events()), 1)
        self.assertEqual(self.store.list_manual_credits(), [])


if __name__ == "__main__":
    unittest.main()
