from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import unittest

from cryptography.fernet import Fernet

from config import load_config


class ConfigTests(unittest.TestCase):
    def test_bank_virtual_account_is_separate_from_sepay_main_account(self):
        with TemporaryDirectory() as temp:
            values = {
                "BOT_TOKEN": "test-token", "ADMIN_IDS": "1",
                "BANK_NAME": "MB Bank", "BANK_BIN": "970422",
                "BANK_ACCOUNT_NUMBER": "0326402876",
                "BANK_TRANSFER_ACCOUNT_NUMBER": "vqrqamhlg6528",
                "BANK_ACCOUNT_HOLDER": "TEST USER",
                "SEPAY_WEBHOOK_SECRET": "test-secret-long-enough-to-be-safe",
                "INVENTORY_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "DATABASE_PATH": str(Path(temp) / "shop.sqlite3"),
            }
            with patch.dict(os.environ, values, clear=True), patch("config.load_dotenv"):
                config = load_config()
                self.assertEqual(config.bank_account_number, "0326402876")
                self.assertEqual(config.bank_transfer_account_number, "VQRQAMHLG6528")
                with patch.dict(os.environ, {"BANK_ACCOUNT_NUMBER": "VQRQAMHLG6528"}):
                    with self.assertRaises(ValueError):
                        load_config()
                for invalid in ("VQRQ/OTHER", "VQRQ-OTHER", "ABC", "A" * 33):
                    with self.subTest(invalid=invalid), patch.dict(
                        os.environ, {"BANK_TRANSFER_ACCOUNT_NUMBER": invalid}
                    ), self.assertRaises(ValueError):
                        load_config()


if __name__ == "__main__":
    unittest.main()
