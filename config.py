from dataclasses import dataclass
from pathlib import Path
import os
import re

from dotenv import load_dotenv
from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parent


def backup_directory() -> Path:
    path = Path(os.getenv(
        "SHOP_BACKUP_DIR", str(Path.home() / "Documents" / "TelegramAccountShopBackups"),
    ))
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    bank_name: str
    bank_bin: str
    bank_account_number: str
    bank_account_holder: str
    sepay_webhook_secret: str
    inventory_encryption_key: str
    database_path: Path
    webhook_host: str
    webhook_port: int
    min_topup: int
    max_topup: int
    support_contact: str
    sepay_api_token: str = ""
    bank_transfer_account_number: str = ""


def load_config() -> Config:
    load_dotenv(ROOT / ".env")

    def required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise ValueError(f"Missing {name} in .env")
        return value

    admin_ids = frozenset(
        int(part.strip()) for part in required("ADMIN_IDS").split(",") if part.strip()
    )
    if not admin_ids:
        raise ValueError("ADMIN_IDS must contain at least one Telegram user ID")

    bank_bin = os.getenv("BANK_BIN", "").strip()
    if bank_bin and (not bank_bin.isascii() or not bank_bin.isdigit() or len(bank_bin) != 6):
        raise ValueError("BANK_BIN must be a 6-digit VietQR bank BIN")
    bank_account_number = required("BANK_ACCOUNT_NUMBER")
    if not re.fullmatch(r"[0-9]{4,32}", bank_account_number, re.ASCII):
        raise ValueError("BANK_ACCOUNT_NUMBER must be the linked bank account's digits")
    bank_transfer_account_number = os.getenv("BANK_TRANSFER_ACCOUNT_NUMBER", "").strip().upper()
    if bank_transfer_account_number and not re.fullmatch(
        r"[A-Z0-9]{4,32}", bank_transfer_account_number, re.ASCII
    ):
        raise ValueError("BANK_TRANSFER_ACCOUNT_NUMBER must be 4-32 ASCII letters or digits")

    database_path = Path(os.getenv("DATABASE_PATH", "shop.sqlite3"))
    if not database_path.is_absolute():
        database_path = ROOT / database_path

    config = Config(
        bot_token=required("BOT_TOKEN"),
        admin_ids=admin_ids,
        bank_name=required("BANK_NAME"),
        bank_bin=bank_bin,
        bank_account_number=bank_account_number,
        bank_account_holder=required("BANK_ACCOUNT_HOLDER"),
        sepay_webhook_secret=required("SEPAY_WEBHOOK_SECRET"),
        inventory_encryption_key=required("INVENTORY_ENCRYPTION_KEY"),
        database_path=database_path,
        webhook_host=os.getenv("WEBHOOK_HOST", "127.0.0.1").strip(),
        webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
        min_topup=int(os.getenv("MIN_TOPUP", "10000")),
        max_topup=int(os.getenv("MAX_TOPUP", "100000000")),
        support_contact=os.getenv("SUPPORT_CONTACT", "").strip(),
        sepay_api_token=os.getenv("SEPAY_API_TOKEN", "").strip(),
        bank_transfer_account_number=bank_transfer_account_number,
    )
    if not (0 < config.min_topup <= config.max_topup <= 1_000_000_000_000):
        raise ValueError("Invalid MIN_TOPUP or MAX_TOPUP")
    if not (1 <= config.webhook_port <= 65535):
        raise ValueError("WEBHOOK_PORT must be between 1 and 65535")
    if config.sepay_webhook_secret.startswith("replace-") or len(config.sepay_webhook_secret) < 24:
        raise ValueError("Set a strong SEPAY_WEBHOOK_SECRET")
    try:
        Fernet(config.inventory_encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("INVENTORY_ENCRYPTION_KEY must be a Fernet key") from exc
    return config
