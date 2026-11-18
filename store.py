from contextlib import contextmanager, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata

from cryptography.fernet import Fernet


class ShopError(Exception):
    pass


class NotFound(ShopError):
    pass


class OutOfStock(ShopError):
    pass


class InsufficientBalance(ShopError):
    pass


class EmailRequired(ShopError):
    pass


class PriceChanged(ShopError):
    pass


class DuplicateStock(ShopError):
    pass


MAX_FILE_STOCK_BYTES = 1_000_000


def parse_json_file(content: str):
    """Accept one UTF-8 JSON object or array as a sellable file."""
    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    try:
        parsed = json.loads(content, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("File must contain valid JSON") from exc
    if not isinstance(parsed, (dict, list)):
        raise ValueError("JSON root must be an object or array")
    try:
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("JSON contains an invalid Unicode character") from exc
    return parsed


class TopupLimitReached(ShopError):
    pass


class WarrantyClosed(ShopError):
    pass


class WarrantyAlreadyOpen(ShopError):
    pass


class CouponUnavailable(ShopError):
    pass


class SaleActive(ShopError):
    pass


class SaleCouponConflict(CouponUnavailable):
    pass


class ManualCreditUnavailable(ShopError):
    pass


class BalanceChanged(ShopError):
    pass


class ExportTooLarge(ShopError):
    pass


LOW_STOCK_THRESHOLD = 2
BROADCAST_MAX_CHARS = 3500
SHOP_TIMEZONE = timezone(timedelta(hours=7))
COUPON_CODE_PATTERN = re.compile(r"[A-Z0-9_-]{4,24}")
_UNSET = object()
TOPUP_CODE_PATTERN = re.compile(r"(?:NAP|MUA)[0-9A-F]{12}")
EMAIL_LOCAL_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
EMAIL_DOMAIN_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")
MAX_CUSTOMER_EMAIL_TEXT_LENGTH = 2000
MAX_CUSTOMER_EMAILS_PER_PURCHASE = 20
MAX_CART_UNITS = 10
MAX_ADMIN_TEST_CREDIT = 10_000_000
DIRECT_CHECKOUT_MINUTES = 30
SPAM_WARNING_WINDOW = timedelta(hours=1)
SPAM_WARNING_COOLDOWN = timedelta(seconds=30)
SPAM_BAN_THRESHOLD = 3


def strip_pasted_table_borders(secret: str) -> str:
    """Remove decorative Markdown table borders from pasted account lines."""
    if not secret.startswith("| "):
        return secret
    cleaned = secret[2:]
    if cleaned.endswith("|") and cleaned.count("|") >= 2:
        cleaned = cleaned[:-1]
    return cleaned


def normalize_customer_email(value: str | None) -> str:
    if value is None or value == "":
        raise EmailRequired("Email is required")
    if not isinstance(value, str):
        raise ValueError("Invalid email")
    if not value.strip():
        raise EmailRequired("Email is required")
    if len(value) > MAX_CUSTOMER_EMAIL_TEXT_LENGTH:
        raise ValueError("Invalid email")
    normalized = []
    for email in value.split(","):
        if len(normalized) >= MAX_CUSTOMER_EMAILS_PER_PURCHASE:
            raise ValueError("Too many emails")
        email = email.strip()
        if email.count("@") != 1:
            raise ValueError("Invalid email")
        local, domain = email.split("@")
        if (not 1 <= len(local) <= 64 or not EMAIL_LOCAL_PATTERN.fullmatch(local)
                or local.startswith(".") or local.endswith(".") or ".." in local):
            raise ValueError("Invalid email")
        try:
            domain = domain.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError) as exc:
            raise ValueError("Invalid email") from exc
        labels = domain.split(".")
        if (len(labels) < 2 or len(domain) > 253 or len(local) + 1 + len(domain) > 254
                or any(len(label) > 63 or not EMAIL_DOMAIN_LABEL_PATTERN.fullmatch(label)
                       for label in labels)):
            raise ValueError("Invalid email")
        normalized.append(f"{local}@{domain}")
    result = ", ".join(normalized)
    if len(result) > MAX_CUSTOMER_EMAIL_TEXT_LENGTH:
        raise ValueError("Invalid email")
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    stock_notifications_enabled INTEGER NOT NULL DEFAULT 1
        CHECK (stock_notifications_enabled IN (0, 1)),
    language TEXT NOT NULL DEFAULT 'vi' CHECK (language IN ('vi', 'en')),
    username TEXT,
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS admin_audit_created_idx
    ON admin_audit_log(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS admin_audit_admin_idx
    ON admin_audit_log(admin_id, created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS spam_warnings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spam_warnings_time_idx ON spam_warnings(created_at);
CREATE INDEX IF NOT EXISTS spam_warnings_user_time_idx
    ON spam_warnings(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS spam_bans (
    user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    banned_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'spam' CHECK (reason IN ('spam', 'admin'))
);
CREATE INDEX IF NOT EXISTS spam_bans_time_idx ON spam_bans(banned_at DESC, user_id DESC);
CREATE TABLE IF NOT EXISTS funnel_tracking_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT OR IGNORE INTO funnel_tracking_state(id) VALUES (1);
CREATE TABLE IF NOT EXISTS funnel_events (
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('visit', 'catalog_view')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (user_id, event_date, event_type)
);
CREATE INDEX IF NOT EXISTS funnel_events_report_idx
    ON funnel_events(event_type, event_date, user_id);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    display_id INTEGER CHECK (display_id > 0),
    sort_order INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL CHECK (price > 0),
    previous_price INTEGER CHECK (previous_price IS NULL OR previous_price > price),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    low_stock_alerted INTEGER NOT NULL DEFAULT 0 CHECK (low_stock_alerted IN (0, 1)),
    requires_email INTEGER NOT NULL DEFAULT 0 CHECK (requires_email IN (0, 1)),
    delivery_type TEXT NOT NULL DEFAULT 'account' CHECK (delivery_type IN ('account', 'file')),
    file_only_delivery INTEGER NOT NULL DEFAULT 0 CHECK (file_only_delivery IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS product_view_tracking_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT OR IGNORE INTO product_view_tracking_state(id) VALUES (1);
CREATE TABLE IF NOT EXISTS product_view_days (
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    view_date TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 1 CHECK (views > 0),
    first_view_at TEXT NOT NULL,
    last_view_at TEXT NOT NULL,
    PRIMARY KEY (user_id, product_id, view_date)
);
CREATE INDEX IF NOT EXISTS product_view_days_date_idx
    ON product_view_days(view_date, product_id, user_id);
CREATE INDEX IF NOT EXISTS product_view_days_product_idx
    ON product_view_days(product_id, view_date, user_id);
CREATE TABLE IF NOT EXISTS product_follows (
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (user_id, product_id)
);
CREATE INDEX IF NOT EXISTS product_follows_product_idx
    ON product_follows(product_id, user_id);
CREATE TABLE IF NOT EXISTS product_sales (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    original_price INTEGER NOT NULL CHECK (original_price > 0),
    sale_price INTEGER NOT NULL CHECK (sale_price > 0 AND sale_price < original_price),
    max_units INTEGER NOT NULL CHECK (max_units > 0),
    sold_units INTEGER NOT NULL DEFAULT 0 CHECK (sold_units >= 0 AND sold_units <= max_units),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS product_sale_one_active_idx
    ON product_sales(product_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS sale_announcement_deliveries (
    sale_id INTEGER NOT NULL REFERENCES product_sales(id),
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT,
    PRIMARY KEY (sale_id, user_id)
);
CREATE INDEX IF NOT EXISTS sale_announcement_pending_idx
    ON sale_announcement_deliveries(next_attempt_at, sale_id, user_id)
    WHERE status='pending';
CREATE TABLE IF NOT EXISTS stock_announcement_batches (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    added_count INTEGER NOT NULL DEFAULT 0 CHECK (added_count >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS stock_announcement_deliveries (
    batch_id INTEGER NOT NULL REFERENCES stock_announcement_batches(id),
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT,
    PRIMARY KEY (batch_id, user_id)
);
CREATE INDEX IF NOT EXISTS stock_announcement_pending_idx
    ON stock_announcement_deliveries(next_attempt_at, batch_id, user_id)
    WHERE status='pending';
CREATE TABLE IF NOT EXISTS broadcast_batches (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'admin' CHECK (kind IN ('admin', 'busy', 'online')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS shop_presence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('busy', 'online')),
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS broadcast_deliveries (
    batch_id INTEGER NOT NULL REFERENCES broadcast_batches(id),
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT,
    PRIMARY KEY (batch_id, user_id)
);
CREATE INDEX IF NOT EXISTS broadcast_pending_idx
    ON broadcast_deliveries(next_attempt_at, batch_id, user_id)
    WHERE status='pending';
CREATE TABLE IF NOT EXISTS stock (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    secret_ciphertext TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    sold_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS stock_available_idx ON stock(product_id, sold_at);
CREATE INDEX IF NOT EXISTS stock_secret_hash_idx ON stock(secret_hash);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    stock_id INTEGER NOT NULL UNIQUE REFERENCES stock(id),
    price INTEGER NOT NULL CHECK (price > 0),
    product_name TEXT NOT NULL,
    sale_id INTEGER REFERENCES product_sales(id),
    customer_email_ciphertext TEXT,
    file_only_delivery INTEGER NOT NULL DEFAULT 0 CHECK (file_only_delivery IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS orders_user_idx ON orders(user_id, id DESC);
CREATE INDEX IF NOT EXISTS orders_created_idx ON orders(created_at, id);
CREATE TABLE IF NOT EXISTS admin_purchase_contacts (
    purchase_id INTEGER PRIMARY KEY REFERENCES orders(id),
    sent_count INTEGER NOT NULL CHECK (sent_count > 0),
    last_sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_order_completions (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id),
    admin_id INTEGER NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    notification_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (notification_status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS email_order_completion_pending_idx
    ON email_order_completions(next_attempt_at, order_id)
    WHERE notification_status='pending';
CREATE TABLE IF NOT EXISTS email_reminder_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    delay_minutes INTEGER NOT NULL DEFAULT 30
        CHECK (delay_minutes = 0 OR delay_minutes BETWEEN 5 AND 1440)
);
INSERT OR IGNORE INTO email_reminder_config(id, delay_minutes) VALUES (1, 30);
CREATE TABLE IF NOT EXISTS email_reminder_state (
    admin_id INTEGER PRIMARY KEY,
    last_sent_at TEXT,
    last_max_order_id INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS warranty_requests (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    note TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    reply TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS warranty_private_resolutions (
    ticket_id INTEGER PRIMARY KEY REFERENCES warranty_requests(id),
    admin_id INTEGER NOT NULL,
    resolved_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS warranty_one_open_idx
    ON warranty_requests(order_id) WHERE status='open';
CREATE INDEX IF NOT EXISTS warranty_status_idx ON warranty_requests(status, id DESC);
CREATE INDEX IF NOT EXISTS warranty_user_idx ON warranty_requests(user_id, id DESC);
CREATE TABLE IF NOT EXISTS warranty_reminder_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    delay_minutes INTEGER NOT NULL DEFAULT 60
        CHECK (delay_minutes = 0 OR delay_minutes BETWEEN 5 AND 1440)
);
INSERT OR IGNORE INTO warranty_reminder_config(id, delay_minutes) VALUES (1, 60);
CREATE TABLE IF NOT EXISTS warranty_reminder_state (
    admin_id INTEGER PRIMARY KEY,
    last_sent_at TEXT,
    last_max_ticket_id INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS topups (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    code TEXT NOT NULL UNIQUE,
    requested_amount INTEGER NOT NULL CHECK (requested_amount > 0),
    paid_amount INTEGER NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'paid')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS topups_user_idx ON topups(user_id, id DESC);
CREATE INDEX IF NOT EXISTS topups_user_created_idx ON topups(user_id, created_at);
CREATE TABLE IF NOT EXISTS shopping_carts (
    user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
    coupon_code TEXT
);
CREATE TABLE IF NOT EXISTS cart_items (
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 10),
    email_ciphertext TEXT,
    PRIMARY KEY (user_id, product_id)
);
CREATE TABLE IF NOT EXISTS direct_checkouts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    topup_id INTEGER NOT NULL UNIQUE REFERENCES topups(id),
    expected_amount INTEGER NOT NULL CHECK (expected_amount > 0),
    coupon_code TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'fulfilled', 'failed', 'expired')),
    failure_reason TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    fulfilled_at TEXT
);
CREATE INDEX IF NOT EXISTS direct_checkouts_user_idx ON direct_checkouts(user_id, id DESC);
CREATE TABLE IF NOT EXISTS direct_checkout_items (
    checkout_id INTEGER NOT NULL REFERENCES direct_checkouts(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 10),
    email_ciphertext TEXT,
    PRIMARY KEY (checkout_id, product_id)
);
CREATE TABLE IF NOT EXISTS direct_checkout_orders (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id),
    checkout_id INTEGER NOT NULL REFERENCES direct_checkouts(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS direct_checkout_orders_pending_idx
    ON direct_checkout_orders(next_attempt_at, order_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS direct_checkout_delivery_state (
    checkout_id INTEGER PRIMARY KEY REFERENCES direct_checkouts(id),
    message_sent_at TEXT,
    file_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS bank_events (
    id INTEGER PRIMARY KEY,
    provider_txn_id TEXT NOT NULL UNIQUE,
    bank_account TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    content TEXT NOT NULL,
    transfer_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('credited', 'unmatched', 'ignored')),
    topup_id INTEGER REFERENCES topups(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS bank_events_status_idx ON bank_events(status, id DESC);
CREATE TABLE IF NOT EXISTS manual_credit_resolutions (
    bank_event_id INTEGER PRIMARY KEY REFERENCES bank_events(id),
    topup_id INTEGER NOT NULL REFERENCES topups(id),
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    admin_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS manual_credit_created_idx
    ON manual_credit_resolutions(created_at, bank_event_id);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    amount INTEGER NOT NULL CHECK (amount != 0),
    kind TEXT NOT NULL CHECK (kind IN ('topup', 'purchase')),
    ref_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(kind, ref_id)
);
CREATE INDEX IF NOT EXISTS ledger_kind_created_idx ON ledger(kind, created_at, id);
CREATE TABLE IF NOT EXISTS admin_test_credits (
    id INTEGER PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    admin_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 10000000),
    balance_before INTEGER NOT NULL CHECK (balance_before >= 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS admin_test_credits_user_idx
    ON admin_test_credits(user_id, id DESC);
CREATE TABLE IF NOT EXISTS coupons (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('percent', 'fixed')),
    value INTEGER NOT NULL CHECK (value > 0),
    max_uses INTEGER NOT NULL CHECK (max_uses > 0),
    expires_on TEXT,
    product_id INTEGER REFERENCES products(id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS coupon_redemptions (
    code TEXT NOT NULL REFERENCES coupons(code),
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (code, user_id)
);
CREATE TABLE IF NOT EXISTS bundles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL CHECK (price > 0),
    previous_price INTEGER CHECK (previous_price IS NULL OR previous_price > price),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS bundle_items (
    bundle_id INTEGER NOT NULL REFERENCES bundles(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (bundle_id, product_id)
);
CREATE TABLE IF NOT EXISTS bundle_orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    bundle_id INTEGER NOT NULL REFERENCES bundles(id),
    bundle_name TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price > 0),
    coupon_code TEXT REFERENCES coupons(code),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS purchase_notification_deliveries (
    kind TEXT NOT NULL CHECK (kind IN ('product', 'bundle')),
    ref_id INTEGER NOT NULL CHECK (ref_id > 0),
    admin_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT,
    PRIMARY KEY (kind, ref_id, admin_id)
);
CREATE INDEX IF NOT EXISTS purchase_notification_pending_idx
    ON purchase_notification_deliveries(next_attempt_at, kind, ref_id, admin_id)
    WHERE status='pending';
CREATE TABLE IF NOT EXISTS new_customer_notification_deliveries (
    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
    admin_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT,
    PRIMARY KEY (user_id, admin_id)
);
CREATE INDEX IF NOT EXISTS new_customer_notification_pending_idx
    ON new_customer_notification_deliveries(next_attempt_at, user_id, admin_id)
    WHERE status='pending';
CREATE TABLE IF NOT EXISTS reconciliation_state (
    account_number TEXT PRIMARY KEY,
    last_txn_id INTEGER NOT NULL DEFAULT 0 CHECK (last_txn_id >= 0),
    last_success_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS webhook_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_success_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class Store:
    def __init__(self, path: Path | str, encryption_key: str):
        self.path = Path(path)
        self.cipher = Fernet(encryption_key.encode("ascii"))
        self.hash_key = hashlib.sha256(b"stock-dedupe:" + encryption_key.encode("ascii")).digest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._allow_duplicate_stock(conn)
            user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
            spam_ban_columns = {row["name"] for row in conn.execute("PRAGMA table_info(spam_bans)")}
            product_columns = {row["name"] for row in conn.execute("PRAGMA table_info(products)")}
            bundle_columns = {row["name"] for row in conn.execute("PRAGMA table_info(bundles)")}
            order_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
            broadcast_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(broadcast_batches)")
            }
            announcement_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(stock_announcement_batches)")
            }
            coupon_columns = {row["name"] for row in conn.execute("PRAGMA table_info(coupons)")}
            if ({"username", "display_name", "stock_notifications_enabled", "language"} - user_columns or
                    {"reason"} - spam_ban_columns or
                    {"deleted", "low_stock_alerted", "requires_email",
                     "display_id", "sort_order", "previous_price", "delivery_type",
                     "file_only_delivery"} - product_columns or
                    {"previous_price"} - bundle_columns or
                    {"product_name", "bundle_order_id", "sale_id", "customer_email_ciphertext",
                     "file_only_delivery"} - order_columns or
                    {"kind"} - broadcast_columns or
                    {"added_count"} - announcement_columns or
                    {"product_id"} - coupon_columns):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if "username" not in user_columns:
                        conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
                    if "display_name" not in user_columns:
                        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
                    if "stock_notifications_enabled" not in user_columns:
                        conn.execute(
                            "ALTER TABLE users ADD COLUMN stock_notifications_enabled "
                            "INTEGER NOT NULL DEFAULT 1 "
                            "CHECK (stock_notifications_enabled IN (0, 1))"
                        )
                    if "language" not in user_columns:
                        conn.execute(
                            "ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'vi' "
                            "CHECK (language IN ('vi', 'en'))"
                        )
                    if "reason" not in spam_ban_columns:
                        conn.execute(
                            "ALTER TABLE spam_bans ADD COLUMN reason TEXT NOT NULL "
                            "DEFAULT 'spam' CHECK (reason IN ('spam', 'admin'))"
                        )
                    if "deleted" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0 "
                            "CHECK (deleted IN (0, 1))"
                        )
                    if "low_stock_alerted" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN low_stock_alerted INTEGER NOT NULL "
                            "DEFAULT 0 CHECK (low_stock_alerted IN (0, 1))"
                        )
                    if "requires_email" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN requires_email INTEGER NOT NULL "
                            "DEFAULT 0 CHECK (requires_email IN (0, 1))"
                        )
                    if "display_id" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN display_id INTEGER "
                            "CHECK (display_id > 0)"
                        )
                        conn.execute("UPDATE products SET display_id=id")
                    if "sort_order" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
                        )
                        conn.execute("UPDATE products SET sort_order=id")
                    if "previous_price" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN previous_price INTEGER "
                            "CHECK (previous_price IS NULL OR previous_price > price)"
                        )
                    if "delivery_type" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN delivery_type TEXT NOT NULL "
                            "DEFAULT 'account' CHECK (delivery_type IN ('account', 'file'))"
                        )
                    if "file_only_delivery" not in product_columns:
                        conn.execute(
                            "ALTER TABLE products ADD COLUMN file_only_delivery INTEGER NOT NULL "
                            "DEFAULT 0 CHECK (file_only_delivery IN (0, 1))"
                        )
                        conn.execute(
                            "UPDATE products SET file_only_delivery=1 WHERE delivery_type='file'"
                        )
                    if "previous_price" not in bundle_columns:
                        conn.execute(
                            "ALTER TABLE bundles ADD COLUMN previous_price INTEGER "
                            "CHECK (previous_price IS NULL OR previous_price > price)"
                        )
                    if "product_name" not in order_columns:
                        conn.execute("ALTER TABLE orders ADD COLUMN product_name TEXT")
                        conn.execute(
                            """UPDATE orders SET product_name=(
                                SELECT name FROM products WHERE products.id=orders.product_id
                            )"""
                        )
                    if "bundle_order_id" not in order_columns:
                        conn.execute(
                            "ALTER TABLE orders ADD COLUMN bundle_order_id INTEGER "
                            "REFERENCES bundle_orders(id)"
                        )
                    if "sale_id" not in order_columns:
                        conn.execute(
                            "ALTER TABLE orders ADD COLUMN sale_id INTEGER "
                            "REFERENCES product_sales(id)"
                        )
                    if "customer_email_ciphertext" not in order_columns:
                        conn.execute("ALTER TABLE orders ADD COLUMN customer_email_ciphertext TEXT")
                    if "file_only_delivery" not in order_columns:
                        conn.execute(
                            "ALTER TABLE orders ADD COLUMN file_only_delivery INTEGER NOT NULL "
                            "DEFAULT 0 CHECK (file_only_delivery IN (0, 1))"
                        )
                        conn.execute(
                            """UPDATE orders SET file_only_delivery=1 WHERE product_id IN (
                                SELECT id FROM products WHERE delivery_type='file'
                            )"""
                        )
                    if "kind" not in broadcast_columns:
                        conn.execute(
                            "ALTER TABLE broadcast_batches ADD COLUMN kind TEXT NOT NULL "
                            "DEFAULT 'admin' CHECK (kind IN ('admin', 'busy', 'online'))"
                        )
                    if "added_count" not in announcement_columns:
                        conn.execute(
                            "ALTER TABLE stock_announcement_batches ADD COLUMN added_count "
                            "INTEGER NOT NULL DEFAULT 0 CHECK (added_count >= 0)"
                        )
                    if "product_id" not in coupon_columns:
                        conn.execute(
                            "ALTER TABLE coupons ADD COLUMN product_id INTEGER "
                            "REFERENCES products(id)"
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            conn.execute(
                "CREATE INDEX IF NOT EXISTS orders_bundle_idx ON orders(bundle_order_id)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS coupons_product_idx ON coupons(product_id)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS products_display_id_unique "
                "ON products(display_id)"
            )
            conn.execute(
                """UPDATE stock_announcement_deliveries AS d SET status='skipped'
                   WHERE d.status='pending' AND NOT EXISTS (
                       SELECT 1 FROM stock_announcement_batches b
                       JOIN product_follows f ON f.product_id=b.product_id
                                            AND f.user_id=d.user_id
                       WHERE b.id=d.batch_id
                   )"""
            )
            conn.execute(
                """UPDATE sale_announcement_deliveries AS d SET status='skipped'
                   WHERE d.status='pending' AND NOT EXISTS (
                       SELECT 1 FROM product_sales sale
                       JOIN product_follows f ON f.product_id=sale.product_id
                                            AND f.user_id=d.user_id
                       WHERE sale.id=d.sale_id
                   )"""
            )

    @staticmethod
    def _allow_duplicate_stock(conn: sqlite3.Connection) -> None:
        old_unique_index = False
        for index in conn.execute("PRAGMA index_list(stock)"):
            if not index["unique"]:
                continue
            quoted_name = index["name"].replace('"', '""')
            columns = [row["name"] for row in conn.execute(
                f'PRAGMA index_info("{quoted_name}")'
            )]
            if "secret_hash" in columns:
                old_unique_index = True
                break
        if not old_unique_index:
            return

        # SQLite cannot drop a UNIQUE constraint in place. Rebuild the table while
        # preserving stock IDs so existing orders still reference the same items.
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE stock_allow_duplicates_migration (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id),
                secret_ciphertext TEXT NOT NULL,
                secret_hash TEXT NOT NULL,
                sold_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )""")
            conn.execute("""INSERT INTO stock_allow_duplicates_migration
                (id, product_id, secret_ciphertext, secret_hash, sold_at, created_at)
                SELECT id, product_id, secret_ciphertext, secret_hash, sold_at, created_at
                FROM stock""")
            conn.execute("DROP TABLE stock")
            conn.execute("ALTER TABLE stock_allow_duplicates_migration RENAME TO stock")
            conn.execute("CREATE INDEX stock_available_idx ON stock(product_id, sold_at)")
            conn.execute("CREATE INDEX stock_secret_hash_idx ON stock(secret_hash)")
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Stock migration failed foreign key check")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write(self):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def log_admin_action(self, admin_id: int, action: str,
                         target_type: str | None = None,
                         target_id: int | str | None = None,
                         details: str = "") -> int:
        """Record a non-sensitive administrative action for later review."""
        if type(admin_id) is not int or admin_id <= 0:
            raise ValueError("Invalid admin ID")
        action = " ".join(str(action).split())
        if not action or len(action) > 80:
            raise ValueError("Invalid audit action")
        target_type = None if target_type is None else " ".join(str(target_type).split())
        if target_type and len(target_type) > 40:
            raise ValueError("Invalid audit target type")
        target_id = None if target_id is None else str(target_id)[:80]
        details = str(details or "").replace("\r", " ").replace("\n", " ").strip()[:1000]
        with self._write() as conn:
            return conn.execute(
                """INSERT INTO admin_audit_log(admin_id, action, target_type, target_id, details)
                   VALUES (?, ?, ?, ?, ?)""",
                (admin_id, action, target_type, target_id, details),
            ).lastrowid

    def list_admin_audit(self, limit: int = 9, offset: int = 0) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid audit page")
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT a.id, a.admin_id, a.action, a.target_type, a.target_id,
                          a.details, a.created_at, u.username, u.display_name
                   FROM admin_audit_log a
                   LEFT JOIN users u ON u.telegram_id=a.admin_id
                   ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            )]

    def export_admin_audit_rows(self, limit: int = 50_000) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 50_000:
            raise ValueError("Invalid audit export limit")
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT a.id, a.admin_id, a.action, a.target_type, a.target_id,
                          a.details, a.created_at, u.username, u.display_name
                   FROM admin_audit_log a
                   LEFT JOIN users u ON u.telegram_id=a.admin_id
                   ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
                (limit,),
            )]

    def ensure_user(
        self, user_id: int, username: str | None = None, display_name: str | None = None,
        notify_admin_ids: frozenset[int] | tuple[int, ...] = (),
    ) -> bool:
        with self._write() as conn:
            created = conn.execute(
                "INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,)
            ).rowcount == 1
            if username is not None or display_name is not None:
                conn.execute(
                    "UPDATE users SET username=?, display_name=? WHERE telegram_id=?",
                    (username, display_name, user_id),
                )
            if created:
                for admin_id in notify_admin_ids:
                    if admin_id != user_id:
                        conn.execute(
                            """INSERT INTO new_customer_notification_deliveries(user_id, admin_id)
                               VALUES (?, ?)""",
                            (user_id, admin_id),
                        )
            return created

    def user_exists(self, user_id: int) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM users WHERE telegram_id=?", (user_id,)
            ).fetchone() is not None

    def get_user_language(self, user_id: int) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT language FROM users WHERE telegram_id=?", (user_id,)
            ).fetchone()
            return row["language"] if row else "vi"

    def set_user_language(self, user_id: int, language: str) -> None:
        if language not in ("vi", "en"):
            raise ValueError("Unsupported language")
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
            conn.execute(
                "UPDATE users SET language=? WHERE telegram_id=?", (language, user_id)
            )

    def record_funnel_event(self, user_id: int, event_type: str,
                            event_date: date | None = None) -> bool:
        if type(user_id) is not int or user_id <= 0 or event_type not in ("visit", "catalog_view"):
            raise ValueError("Invalid funnel event")
        day = event_date or datetime.now(SHOP_TIMEZONE).date()
        if type(day) is not date:
            raise ValueError("Invalid funnel date")
        with self._write() as conn:
            inserted = conn.execute(
                """INSERT OR IGNORE INTO funnel_events(user_id, event_date, event_type)
                   SELECT ?, ?, ? WHERE EXISTS (
                       SELECT 1 FROM users WHERE telegram_id=?
                   )""",
                (user_id, day.isoformat(), event_type, user_id),
            ).rowcount
            return inserted == 1

    def funnel_report(self, days: int = 7, excluded_ids: frozenset[int] = frozenset(),
                      now: datetime | None = None) -> dict:
        if type(days) is not int or days not in (7, 30):
            raise ValueError("Funnel report must cover 7 or 30 days")
        now = now or datetime.now(SHOP_TIMEZONE)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Funnel report time must include a timezone")
        local_now = now.astimezone(SHOP_TIMEZONE)
        first_day = local_now.date() - timedelta(days=days - 1)
        requested_start = datetime.combine(first_day, datetime.min.time(),
                                           tzinfo=SHOP_TIMEZONE).astimezone(timezone.utc)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT started_at FROM funnel_tracking_state WHERE id=1"
            ).fetchone()
            tracking_start = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            start = max(requested_start, tracking_start)
            end = now.astimezone(timezone.utc)
            start_at = start.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            end_at = end.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            excluded = sorted(excluded_ids)
            exclude_sql = (" AND e.user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                           if excluded else "")
            result = conn.execute(
                """WITH bounds AS (
                       SELECT ? AS first_day, ? AS last_day, ? AS start_at, ? AS end_at
                   ), visitors AS (
                       SELECT DISTINCT e.user_id FROM funnel_events e CROSS JOIN bounds b
                       WHERE e.event_type='visit'
                         AND e.event_date BETWEEN b.first_day AND b.last_day
                         AND e.created_at>=b.start_at AND e.created_at<=b.end_at"""
                + exclude_sql +
                """
                   ), viewers AS (
                       SELECT e.user_id, MIN(e.created_at) AS first_view_at
                       FROM funnel_events e
                       JOIN visitors v ON v.user_id=e.user_id CROSS JOIN bounds b
                       WHERE e.event_type='catalog_view'
                         AND e.event_date BETWEEN b.first_day AND b.last_day
                         AND e.created_at>=b.start_at AND e.created_at<=b.end_at
                       GROUP BY e.user_id
                   )
                   SELECT
                       (SELECT COUNT(*) FROM visitors) AS visitors,
                       (SELECT COUNT(*) FROM visitors v JOIN users u ON u.telegram_id=v.user_id
                        CROSS JOIN bounds b WHERE u.created_at>=b.start_at
                          AND u.created_at<=b.end_at) AS new_visitors,
                       (SELECT COUNT(*) FROM viewers) AS viewers,
                       (SELECT COUNT(DISTINCT t.user_id) FROM topups t
                        JOIN visitors v ON v.user_id=t.user_id CROSS JOIN bounds b
                        WHERE t.created_at>=b.start_at AND t.created_at<=b.end_at)
                        AS topup_requesters,
                       (SELECT COUNT(DISTINCT t.user_id) FROM topups t
                        JOIN viewers v ON v.user_id=t.user_id CROSS JOIN bounds b
                        WHERE t.created_at>=v.first_view_at
                          AND t.created_at>=b.start_at AND t.created_at<=b.end_at)
                        AS viewer_topup_requesters,
                       (SELECT COUNT(DISTINCT l.user_id) FROM ledger l
                        JOIN visitors v ON v.user_id=l.user_id CROSS JOIN bounds b
                        WHERE l.kind='topup' AND l.created_at>=b.start_at
                          AND l.created_at<=b.end_at) AS payers,
                       (SELECT COUNT(DISTINCT o.user_id) FROM orders o
                        JOIN visitors v ON v.user_id=o.user_id CROSS JOIN bounds b
                        WHERE o.created_at>=b.start_at AND o.created_at<=b.end_at)
                        AS buyers,
                       (SELECT COUNT(DISTINCT o.user_id) FROM orders o
                        JOIN viewers v ON v.user_id=o.user_id CROSS JOIN bounds b
                        WHERE o.created_at>=v.first_view_at
                          AND o.created_at>=b.start_at AND o.created_at<=b.end_at)
                        AS viewer_buyers""",
                (start.astimezone(SHOP_TIMEZONE).date().isoformat(),
                 local_now.date().isoformat(), start_at, end_at, *excluded),
            ).fetchone()
            return {"days": days, "start_at": start, "end_at": end, **dict(result)}

    def record_product_view(self, user_id: int, product_id: int,
                            viewed_at: datetime | None = None) -> bool:
        if (type(user_id) is not int or user_id <= 0 or
                type(product_id) is not int or product_id <= 0):
            raise ValueError("Invalid product view")
        viewed_at = viewed_at or datetime.now(timezone.utc)
        if viewed_at.tzinfo is None or viewed_at.utcoffset() is None:
            raise ValueError("Product view time must include a timezone")
        timestamp = viewed_at.astimezone(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        view_date = viewed_at.astimezone(SHOP_TIMEZONE).date().isoformat()
        with self._write() as conn:
            return conn.execute(
                """INSERT INTO product_view_days
                   (user_id, product_id, view_date, views, first_view_at, last_view_at)
                   SELECT ?, ?, ?, 1, ?, ?
                   WHERE EXISTS (SELECT 1 FROM users WHERE telegram_id=?)
                     AND EXISTS (SELECT 1 FROM products WHERE id=?)
                   ON CONFLICT(user_id, product_id, view_date) DO UPDATE SET
                   views=product_view_days.views+1,
                   first_view_at=MIN(product_view_days.first_view_at, excluded.first_view_at),
                   last_view_at=MAX(product_view_days.last_view_at, excluded.last_view_at)""",
                (user_id, product_id, view_date, timestamp, timestamp,
                 user_id, product_id),
            ).rowcount == 1

    @staticmethod
    def _product_view_window(days: int, now: datetime | None) -> tuple[str, str, datetime]:
        if type(days) is not int or days not in (7, 30):
            raise ValueError("Product view report must cover 7 or 30 days")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Product view report time must include a timezone")
        local_now = now.astimezone(SHOP_TIMEZONE)
        first_day = local_now.date() - timedelta(days=days - 1)
        return first_day.isoformat(), local_now.date().isoformat(), local_now

    def product_view_report(self, days: int = 7,
                            excluded_ids: frozenset[int] = frozenset(),
                            limit: int = 8, offset: int = 0,
                            now: datetime | None = None) -> dict:
        if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid product view page")
        first_day, last_day, local_now = self._product_view_window(days, now)
        excluded = sorted(excluded_ids)
        exclude_sql = (" AND v.user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                       if excluded else "")
        with closing(self._connect()) as conn:
            started = conn.execute(
                "SELECT started_at FROM product_view_tracking_state WHERE id=1"
            ).fetchone()["started_at"]
            rows = [dict(row) for row in conn.execute(
                """SELECT p.id, COALESCE(p.display_id, p.id) AS display_id,
                          p.name, p.deleted,
                          COALESCE(SUM(v.views), 0) AS view_count,
                          COUNT(DISTINCT v.user_id) AS viewer_count,
                          MAX(v.last_view_at) AS last_view_at
                   FROM products p LEFT JOIN product_view_days v
                     ON v.product_id=p.id AND v.view_date BETWEEN ? AND ?"""
                + exclude_sql +
                """ GROUP BY p.id
                   HAVING COALESCE(SUM(v.views), 0) > 0
                   ORDER BY view_count DESC, viewer_count DESC, display_id, p.id
                   LIMIT ? OFFSET ?""",
                (first_day, last_day, *excluded, limit + 1, offset),
            )]
        tracking_start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        requested_start = datetime.combine(date.fromisoformat(first_day),
                                           datetime.min.time(), tzinfo=SHOP_TIMEZONE)
        return {
            "days": days, "start_at": max(requested_start, tracking_start),
            "end_at": local_now, "products": rows[:limit],
            "has_more": len(rows) > limit,
        }

    def product_view_customers(self, product_id: int, days: int = 7,
                               excluded_ids: frozenset[int] = frozenset(),
                               limit: int = 8, offset: int = 0,
                               now: datetime | None = None) -> dict:
        if (type(product_id) is not int or product_id <= 0 or
                type(limit) is not int or limit < 1 or
                type(offset) is not int or offset < 0):
            raise ValueError("Invalid product viewer page")
        first_day, last_day, _ = self._product_view_window(days, now)
        excluded = sorted(excluded_ids)
        exclude_sql = (" AND v.user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                       if excluded else "")
        with closing(self._connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                """SELECT v.user_id, u.username, u.display_name,
                          SUM(v.views) AS view_count,
                          MAX(v.last_view_at) AS last_view_at
                   FROM product_view_days v
                   JOIN users u ON u.telegram_id=v.user_id
                   WHERE v.product_id=? AND v.view_date BETWEEN ? AND ?"""
                + exclude_sql +
                """ GROUP BY v.user_id
                   ORDER BY view_count DESC, last_view_at DESC, v.user_id
                   LIMIT ? OFFSET ?""",
                (product_id, first_day, last_day, *excluded, limit + 1, offset),
            )]
        return {"customers": rows[:limit], "has_more": len(rows) > limit}

    def list_user_product_views(self, user_id: int, limit: int = 10,
                                offset: int = 0) -> list[dict]:
        """Products a user opened since view tracking began, newest first."""
        if (type(user_id) is not int or user_id <= 0 or
                type(limit) is not int or limit < 1 or
                type(offset) is not int or offset < 0):
            raise ValueError("Invalid user product view page")
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT p.id, COALESCE(p.display_id, p.id) AS display_id,
                          p.name, p.deleted, SUM(v.views) AS view_count,
                          MAX(v.last_view_at) AS last_view_at
                   FROM product_view_days v JOIN products p ON p.id=v.product_id
                   WHERE v.user_id=? GROUP BY p.id
                   ORDER BY last_view_at DESC, p.id DESC LIMIT ? OFFSET ?""",
                (user_id, limit, offset),
            )]

    def get_balance(self, user_id: int) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT balance FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            return row["balance"] if row else 0

    def stock_notifications_enabled(self, user_id: int) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT stock_notifications_enabled FROM users WHERE telegram_id=?",
                (user_id,),
            ).fetchone()
            return bool(row["stock_notifications_enabled"]) if row else True

    def set_stock_notifications_enabled(self, user_id: int, enabled: bool) -> None:
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
            conn.execute(
                "UPDATE users SET stock_notifications_enabled=? WHERE telegram_id=?",
                (int(enabled), user_id),
            )
            if not enabled:
                conn.execute(
                    """UPDATE stock_announcement_deliveries SET status='skipped'
                       WHERE user_id=? AND status='pending'""",
                    (user_id,),
                )

    def is_following_product(self, user_id: int, product_id: int) -> bool:
        with closing(self._connect()) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM product_follows WHERE user_id=? AND product_id=?",
                (user_id, product_id),
            ).fetchone())

    def set_product_following(self, user_id: int, product_id: int, following: bool) -> bool:
        if (type(user_id) is not int or user_id <= 0 or
                type(product_id) is not int or product_id <= 0 or
                type(following) is not bool):
            raise ValueError("Invalid product follow")
        with self._write() as conn:
            product = conn.execute(
                "SELECT active, deleted FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if not product:
                raise NotFound("Product not found")
            if following:
                if not product["active"] or product["deleted"]:
                    raise NotFound("Product unavailable")
                conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO product_follows(user_id, product_id) VALUES (?, ?)",
                    (user_id, product_id),
                )
            else:
                conn.execute(
                    "DELETE FROM product_follows WHERE user_id=? AND product_id=?",
                    (user_id, product_id),
                )
                conn.execute(
                    """UPDATE stock_announcement_deliveries SET status='skipped'
                       WHERE user_id=? AND status='pending' AND batch_id IN (
                           SELECT id FROM stock_announcement_batches WHERE product_id=?
                       )""",
                    (user_id, product_id),
                )
                conn.execute(
                    """UPDATE sale_announcement_deliveries SET status='skipped'
                       WHERE user_id=? AND status='pending' AND sale_id IN (
                           SELECT id FROM product_sales WHERE product_id=?
                       )""",
                    (user_id, product_id),
                )
            return following

    def list_followed_products(self, user_id: int) -> list[dict]:
        with closing(self._connect()) as conn:
            products = [dict(row) for row in conn.execute(
                """SELECT p.id, p.display_id, p.name, p.active, p.deleted, p.price,
                          p.previous_price, sale.sale_price,
                          (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id
                           AND s.sold_at IS NULL) AS available
                   FROM product_follows f
                   JOIN products p ON p.id=f.product_id
                   LEFT JOIN product_sales sale ON sale.product_id=p.id AND sale.active=1
                   WHERE f.user_id=? ORDER BY COALESCE(p.display_id, p.id), p.id""",
                (user_id,),
            )]
            for product in products:
                product["display_id"] = product["display_id"] or product["id"]
            return products

    def product_followers(self, product_id: int,
                          excluded_ids: frozenset[int] = frozenset(),
                          limit: int = 20, offset: int = 0) -> dict:
        if (type(product_id) is not int or product_id <= 0 or
                type(limit) is not int or limit < 1 or
                type(offset) is not int or offset < 0):
            raise ValueError("Invalid product follower page")
        excluded = sorted(excluded_ids)
        where = "f.product_id=?"
        if excluded:
            where += " AND f.user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
        params = (product_id, *excluded)
        with closing(self._connect()) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM product_follows f WHERE " + where, params,
            ).fetchone()[0]
            followers = [dict(row) for row in conn.execute(
                """SELECT f.user_id, f.created_at, u.username, u.display_name,
                          u.stock_notifications_enabled,
                          (b.user_id IS NOT NULL) AS banned
                   FROM product_follows f
                   JOIN users u ON u.telegram_id=f.user_id
                   LEFT JOIN spam_bans b ON b.user_id=f.user_id
                   WHERE """ + where +
                " ORDER BY f.created_at DESC, f.user_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )]
        return {"total": total, "followers": followers,
                "has_more": offset + len(followers) < total}

    def get_reconciliation_state(self, account_number: str):
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT last_txn_id, last_success_at FROM reconciliation_state WHERE account_number=?",
                (account_number,),
            ).fetchone()
            return dict(row) if row else None

    def get_operational_status(self, account_number: str) -> dict:
        with closing(self._connect()) as conn:
            webhook = conn.execute(
                "SELECT last_success_at FROM webhook_state WHERE id=1"
            ).fetchone()
            reconciliation = conn.execute(
                "SELECT last_success_at FROM reconciliation_state WHERE account_number=?",
                (account_number,),
            ).fetchone()
            unmatched = conn.execute(
                "SELECT COUNT(*) FROM bank_events WHERE status='unmatched'"
            ).fetchone()[0]
            low_stock = [dict(row) for row in conn.execute(
                """SELECT p.id, p.name,
                          SUM(CASE WHEN s.sold_at IS NULL THEN 1 ELSE 0 END) AS available
                   FROM products p JOIN stock s ON s.product_id=p.id
                   WHERE p.active=1 AND p.deleted=0
                   GROUP BY p.id
                   HAVING available <= ?
                   ORDER BY available, p.id""",
                (LOW_STOCK_THRESHOLD,),
            )]
            return {
                "last_webhook_at": webhook["last_success_at"] if webhook else None,
                "last_reconciliation_at": reconciliation["last_success_at"] if reconciliation else None,
                "unmatched_count": unmatched,
                "low_stock": low_stock,
            }

    def mark_reconciled(self, account_number: str, last_txn_id: int):
        if not account_number or type(last_txn_id) is not int or last_txn_id < 0:
            raise ValueError("Invalid reconciliation state")
        with self._write() as conn:
            conn.execute(
                """INSERT INTO reconciliation_state(account_number, last_txn_id)
                   VALUES (?, ?)
                   ON CONFLICT(account_number) DO UPDATE SET
                   last_txn_id=MAX(reconciliation_state.last_txn_id, excluded.last_txn_id),
                   last_success_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                (account_number, last_txn_id),
            )

    def count_users(self, exclude_ids: frozenset[int] = frozenset()) -> int:
        excluded = tuple(sorted(exclude_ids))
        where = (" WHERE telegram_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                 if excluded else "")
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM users" + where, excluded).fetchone()[0]

    def list_users(self, limit: int = 9, offset: int = 0,
                   exclude_ids: frozenset[int] = frozenset()):
        excluded = tuple(sorted(exclude_ids))
        where = (" WHERE u.telegram_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                 if excluded else "")
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT u.telegram_id, u.balance, u.username, u.display_name, u.created_at,
                          (SELECT COUNT(*) FROM ledger l
                           WHERE l.user_id=u.telegram_id AND l.kind='purchase') AS order_count
                   FROM users u""" + where +
                " ORDER BY u.created_at DESC, u.telegram_id DESC LIMIT ? OFFSET ?",
                (*excluded, limit, offset),
            )]

    def search_users(self, query: str, limit: int = 9, offset: int = 0,
                     exclude_ids: frozenset[int] = frozenset()) -> dict:
        """Find customers by exact Telegram ID, username or part of their name."""
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid user search page")
        where, params = self._user_search_condition(query)
        excluded = sorted(exclude_ids)
        if excluded:
            where += " AND u.telegram_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            params.extend(excluded)
        with closing(self._connect()) as conn:
            conn.create_function("user_search_key", 1, self._user_search_key,
                                 deterministic=True)
            total = conn.execute("SELECT COUNT(*) FROM users u WHERE " + where,
                                 params).fetchone()[0]
            users = [dict(row) for row in conn.execute(
                """SELECT u.telegram_id, u.balance, u.username, u.display_name,
                          (SELECT COUNT(*) FROM ledger l
                           WHERE l.user_id=u.telegram_id AND l.kind='purchase') AS order_count
                   FROM users u WHERE """ + where +
                " ORDER BY u.created_at DESC, u.telegram_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )]
        return {"total": total, "users": users}

    def _user_search_condition(self, query: str) -> tuple[str, list]:
        """Build a parameterized condition for a joined users table aliased as u."""
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 80:
            raise ValueError("Invalid user search")
        term = " ".join(query.strip().split())
        is_username = term.startswith("@")
        if is_username:
            term = term[1:]
        if not term:
            raise ValueError("Invalid user search")
        if not is_username and term.isascii() and term.isdecimal():
            user_id = int(term)
            if user_id > 9_223_372_036_854_775_807:
                raise ValueError("Invalid Telegram ID")
            return "u.telegram_id=?", [user_id]
        folded = self._user_search_key(term)
        if not folded:
            raise ValueError("Invalid user search")
        username_condition = "instr(user_search_key(COALESCE(u.username, '')), ?)>0"
        if is_username:
            return username_condition, [folded]
        return (f"({username_condition} OR "
                "instr(user_search_key(COALESCE(u.display_name, '')), ?)>0)"), [folded, folded]

    @staticmethod
    def _user_search_key(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
        return "".join(char for char in normalized
                       if unicodedata.category(char) != "Mn")

    def find_users_by_username(self, username: str) -> list[dict]:
        username = username.strip().removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ValueError("Invalid Telegram username")
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT telegram_id, username, display_name, balance FROM users
                   WHERE username=? COLLATE NOCASE ORDER BY telegram_id LIMIT 3""",
                (username,),
            )]

    def credit_test_balance(self, user_id: int, amount: int, admin_id: int,
                            expected_balance: int, request_key: str) -> dict:
        if (any(type(value) is not int or value <= 0 for value in
                (user_id, amount, admin_id)) or amount > MAX_ADMIN_TEST_CREDIT or
                type(expected_balance) is not int or expected_balance < 0 or
                not isinstance(request_key, str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_key)):
            raise ValueError("Invalid test credit")
        with self._write() as conn:
            previous = conn.execute(
                """SELECT id, user_id, admin_id, amount, balance_before, balance_after
                   FROM admin_test_credits WHERE request_key=?""", (request_key,),
            ).fetchone()
            if previous:
                if (previous["user_id"], previous["admin_id"], previous["amount"],
                        previous["balance_before"]) != (user_id, admin_id, amount,
                                                      expected_balance):
                    raise ValueError("Test credit key was reused for a different request")
                return {**dict(previous), "already_applied": True}
            user = conn.execute(
                "SELECT balance FROM users WHERE telegram_id=?", (user_id,),
            ).fetchone()
            if not user:
                raise NotFound("User not found")
            if user["balance"] != expected_balance:
                raise BalanceChanged("Balance changed since preview")
            new_balance = expected_balance + amount
            credit_id = conn.execute(
                """INSERT INTO admin_test_credits
                   (request_key, user_id, admin_id, amount, balance_before, balance_after)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (request_key, user_id, admin_id, amount, expected_balance, new_balance),
            ).lastrowid
            conn.execute(
                "UPDATE users SET balance=? WHERE telegram_id=?",
                (new_balance, user_id),
            )
            return {"id": credit_id, "user_id": user_id, "admin_id": admin_id,
                    "amount": amount, "balance_before": expected_balance,
                    "balance_after": new_balance, "already_applied": False}

    def list_test_credits(self, user_id: int | None = None,
                          limit: int = 5) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid credit history limit")
        if user_id is not None and (type(user_id) is not int or user_id <= 0):
            raise ValueError("Invalid user ID")
        where = " WHERE c.user_id=?" if user_id is not None else ""
        params = (user_id, limit) if user_id is not None else (limit,)
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT c.id, c.user_id, c.admin_id, c.amount,
                          c.balance_before, c.balance_after, c.created_at,
                          u.username, u.display_name
                   FROM admin_test_credits c
                   JOIN users u ON u.telegram_id=c.user_id""" + where +
                " ORDER BY c.id DESC LIMIT ?", params,
            )]

    def record_spam_warning(self, user_id: int, now: datetime | None = None) -> dict:
        """Record one warning and permanently ban after three within one hour."""
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Invalid Telegram user ID")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Warning time must include a timezone")
        now = now.astimezone(timezone.utc)
        stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        cutoff = (now - SPAM_WARNING_WINDOW).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        cooldown = (now - SPAM_WARNING_COOLDOWN).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM spam_bans WHERE user_id=?", (user_id,)).fetchone():
                return {"recorded": False, "count": SPAM_BAN_THRESHOLD, "banned": True}
            conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
            conn.execute("DELETE FROM spam_warnings WHERE created_at<=?", (cutoff,))
            recent = conn.execute(
                "SELECT created_at FROM spam_warnings WHERE user_id=? "
                "ORDER BY created_at DESC LIMIT 1", (user_id,),
            ).fetchone()
            if recent and recent["created_at"] > cooldown:
                return {"recorded": False, "count": conn.execute(
                    "SELECT COUNT(*) FROM spam_warnings WHERE user_id=?", (user_id,),
                ).fetchone()[0], "banned": False}
            conn.execute(
                "INSERT INTO spam_warnings(user_id, created_at) VALUES (?, ?)",
                (user_id, stamp),
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM spam_warnings WHERE user_id=?", (user_id,),
            ).fetchone()[0]
            banned = count >= SPAM_BAN_THRESHOLD
            if banned:
                conn.execute(
                    "INSERT INTO spam_bans(user_id, banned_at, reason) VALUES (?, ?, 'spam')",
                    (user_id, stamp),
                )
                conn.execute(
                    "UPDATE broadcast_deliveries SET status='failed' "
                    "WHERE user_id=? AND status='pending'", (user_id,),
                )
                conn.execute(
                    "UPDATE sale_announcement_deliveries SET status='skipped' "
                    "WHERE user_id=? AND status='pending'", (user_id,),
                )
                conn.execute(
                    "UPDATE stock_announcement_deliveries SET status='skipped' "
                    "WHERE user_id=? AND status='pending'", (user_id,),
                )
            return {"recorded": True, "count": count, "banned": banned}

    def banned_user_ids(self) -> set[int]:
        with closing(self._connect()) as conn:
            return {row[0] for row in conn.execute("SELECT user_id FROM spam_bans")}

    def list_spam_bans(self, limit: int = 9, offset: int = 0) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT b.user_id, b.banned_at, b.reason, u.username, u.display_name
                   FROM spam_bans b JOIN users u ON u.telegram_id=b.user_id
                   ORDER BY b.banned_at DESC, b.user_id DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            )]

    def get_spam_ban(self, user_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT user_id, banned_at, reason FROM spam_bans WHERE user_id=?", (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def ban_user(self, user_id: int) -> bool:
        """Manually block a customer and stop pending customer notifications."""
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Invalid Telegram user ID")
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z",
        )
        with self._write() as conn:
            if not conn.execute(
                "SELECT 1 FROM users WHERE telegram_id=?", (user_id,),
            ).fetchone():
                return False
            if conn.execute(
                "SELECT 1 FROM spam_bans WHERE user_id=?", (user_id,),
            ).fetchone():
                return False
            conn.execute(
                "INSERT INTO spam_bans(user_id, banned_at, reason) VALUES (?, ?, 'admin')",
                (user_id, stamp),
            )
            conn.execute("DELETE FROM spam_warnings WHERE user_id=?", (user_id,))
            conn.execute(
                "UPDATE broadcast_deliveries SET status='failed' "
                "WHERE user_id=? AND status='pending'", (user_id,),
            )
            conn.execute(
                "UPDATE sale_announcement_deliveries SET status='skipped' "
                "WHERE user_id=? AND status='pending'", (user_id,),
            )
            conn.execute(
                "UPDATE stock_announcement_deliveries SET status='skipped' "
                "WHERE user_id=? AND status='pending'", (user_id,),
            )
            return True

    def unban_user(self, user_id: int) -> bool:
        with self._write() as conn:
            removed = conn.execute(
                "DELETE FROM spam_bans WHERE user_id=?", (user_id,),
            ).rowcount > 0
            if removed:
                conn.execute("DELETE FROM spam_warnings WHERE user_id=?", (user_id,))
            return removed

    def count_broadcast_recipients(self, excluded_ids: frozenset[int]) -> int:
        excluded = sorted(excluded_ids)
        sql = ("SELECT COUNT(*) FROM users u WHERE NOT EXISTS "
               "(SELECT 1 FROM spam_bans b WHERE b.user_id=u.telegram_id)")
        if excluded:
            sql += " AND u.telegram_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
        with closing(self._connect()) as conn:
            return conn.execute(sql, excluded).fetchone()[0]

    def create_broadcast(
        self, admin_id: int, content: str, excluded_ids: frozenset[int],
    ) -> dict:
        if not content.strip() or len(content.encode("utf-16-le")) // 2 > BROADCAST_MAX_CHARS:
            raise ValueError("Invalid broadcast content")
        excluded = sorted(excluded_ids)
        with self._write() as conn:
            batch_id = conn.execute(
                "INSERT INTO broadcast_batches(admin_id, content) VALUES (?, ?)",
                (admin_id, content),
            ).lastrowid
            sql = ("INSERT INTO broadcast_deliveries(batch_id, user_id) "
                   "SELECT ?, u.telegram_id FROM users u WHERE NOT EXISTS "
                   "(SELECT 1 FROM spam_bans b WHERE b.user_id=u.telegram_id)")
            if excluded:
                sql += " AND u.telegram_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            conn.execute(sql, (batch_id, *excluded))
            recipients = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries WHERE batch_id=?", (batch_id,),
            ).fetchone()[0]
            return {"id": batch_id, "recipients": recipients}

    def list_due_broadcast_deliveries(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT d.batch_id, d.user_id, d.attempts, b.content
                   FROM broadcast_deliveries d
                   JOIN broadcast_batches b ON b.id=d.batch_id
                   WHERE b.kind='admin' AND d.status='pending'
                     AND d.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY d.next_attempt_at, d.batch_id, d.user_id LIMIT ?""",
                (limit,),
            )]

    def get_presence_state(self) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT state FROM shop_presence WHERE id=1").fetchone()
            return row["state"] if row else None

    def queue_presence_broadcast(
        self, state: str, content: str, excluded_ids: frozenset[int],
    ) -> dict | None:
        if state not in ("busy", "online") or not content.strip():
            raise ValueError("Invalid shop presence")
        excluded = sorted(excluded_ids)
        with self._write() as conn:
            current = conn.execute("SELECT state FROM shop_presence WHERE id=1").fetchone()
            if current and current["state"] == state:
                return None
            conn.execute(
                """UPDATE broadcast_deliveries SET status='failed'
                   WHERE status='pending' AND batch_id IN (
                       SELECT id FROM broadcast_batches WHERE kind IN ('busy', 'online')
                   )"""
            )
            batch_id = conn.execute(
                "INSERT INTO broadcast_batches(admin_id, content, kind) VALUES (0, ?, ?)",
                (content, state),
            ).lastrowid
            sql = ("INSERT INTO broadcast_deliveries(batch_id, user_id) "
                   "SELECT ?, u.telegram_id FROM users u WHERE NOT EXISTS "
                   "(SELECT 1 FROM spam_bans b WHERE b.user_id=u.telegram_id)")
            if excluded:
                sql += " AND u.telegram_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            conn.execute(sql, (batch_id, *excluded))
            conn.execute(
                """INSERT INTO shop_presence(id, state) VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                   changed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                (state,),
            )
            recipients = conn.execute(
                "SELECT COUNT(*) FROM broadcast_deliveries WHERE batch_id=?", (batch_id,),
            ).fetchone()[0]
            return {"id": batch_id, "recipients": recipients}

    def list_due_presence_deliveries(self, limit: int = 5) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT d.batch_id, d.user_id, d.attempts, b.content, b.kind AS state
                   FROM broadcast_deliveries d
                   JOIN broadcast_batches b ON b.id=d.batch_id
                   WHERE b.kind IN ('busy', 'online') AND d.status='pending'
                     AND d.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY d.next_attempt_at, d.batch_id, d.user_id LIMIT ?""",
                (limit,),
            )]

    def finish_broadcast_delivery(self, batch_id: int, user_id: int, status: str) -> None:
        if status not in ("sent", "failed"):
            raise ValueError("Invalid broadcast status")
        with self._write() as conn:
            conn.execute(
                """UPDATE broadcast_deliveries SET status=?, attempts=attempts+1,
                          sent_at=CASE WHEN ?='sent' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                       ELSE NULL END
                   WHERE batch_id=? AND user_id=? AND status='pending'""",
                (status, status, batch_id, user_id),
            )

    def retry_broadcast_delivery(
        self, batch_id: int, user_id: int, delay_seconds: int,
    ) -> None:
        if delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._write() as conn:
            conn.execute(
                """UPDATE broadcast_deliveries
                   SET attempts=attempts+1, next_attempt_at=?
                   WHERE batch_id=? AND user_id=? AND status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 batch_id, user_id),
            )

    def get_broadcast_status(self, batch_id: int | None = None) -> dict | None:
        with closing(self._connect()) as conn:
            if batch_id is None:
                row = conn.execute(
                    "SELECT MAX(id) FROM broadcast_batches WHERE kind='admin'"
                ).fetchone()
                batch_id = row[0]
            if batch_id is None:
                return None
            row = conn.execute(
                """SELECT b.id, b.admin_id, b.created_at,
                          COUNT(d.user_id) AS recipients,
                          COALESCE(SUM(d.status='pending'), 0) AS pending,
                          COALESCE(SUM(d.status='sent'), 0) AS sent,
                          COALESCE(SUM(d.status='failed'), 0) AS failed
                   FROM broadcast_batches b
                   LEFT JOIN broadcast_deliveries d ON d.batch_id=b.id
                   WHERE b.id=? AND b.kind='admin' GROUP BY b.id""",
                (batch_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_user_summary(self, user_id: int):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT u.telegram_id, u.balance, u.username, u.display_name, u.created_at,
                          (SELECT COUNT(*) FROM ledger l
                           WHERE l.user_id=u.telegram_id AND l.kind='purchase') AS order_count,
                          (SELECT COALESCE(SUM(amount), 0) FROM ledger l
                           WHERE l.user_id=u.telegram_id AND l.kind='topup') AS total_received,
                          (SELECT COALESCE(SUM(-amount), 0) FROM ledger l
                           WHERE l.user_id=u.telegram_id AND l.kind='purchase') AS total_spent,
                          (SELECT COALESCE(SUM(amount), 0) FROM admin_test_credits c
                           WHERE c.user_id=u.telegram_id) AS total_test_credit
                   FROM users u WHERE u.telegram_id=?""", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_products(self, include_inactive: bool = False, include_deleted: bool = False):
        sql = """
            SELECT p.*, COUNT(s.id) AS available,
                   (SELECT COUNT(*) FROM stock sold
                    WHERE sold.product_id=p.id AND sold.sold_at IS NOT NULL) AS sold,
                   sale.id AS sale_id, sale.sale_price,
                   sale.max_units AS sale_limit,
                   sale.max_units - sale.sold_units AS sale_remaining
            FROM products p LEFT JOIN stock s ON s.product_id=p.id AND s.sold_at IS NULL
            LEFT JOIN product_sales sale ON sale.product_id=p.id AND sale.active=1
        """
        conditions = []
        if not include_inactive:
            conditions.append("p.active=1")
        if not include_deleted:
            conditions.append("p.deleted=0")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " GROUP BY p.id ORDER BY p.sort_order, p.id"
        with closing(self._connect()) as conn:
            products = [dict(row) for row in conn.execute(sql)]
            for product in products:
                product["display_id"] = product["display_id"] or product["id"]
            return products

    def get_product(self, product_id: int):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT p.*, COUNT(s.id) AS available,
                          (SELECT COUNT(*) FROM stock sold
                           WHERE sold.product_id=p.id AND sold.sold_at IS NOT NULL) AS sold,
                          sale.id AS sale_id, sale.sale_price,
                          sale.max_units AS sale_limit,
                          sale.max_units - sale.sold_units AS sale_remaining
                   FROM products p
                   LEFT JOIN stock s ON s.product_id=p.id AND s.sold_at IS NULL
                   LEFT JOIN product_sales sale ON sale.product_id=p.id AND sale.active=1
                   WHERE p.id=? GROUP BY p.id""", (product_id,)
            ).fetchone()
            if not row:
                return None
            product = dict(row)
            product["display_id"] = product["display_id"] or product["id"]
            return product

    def product_id_for_display_id(self, display_id: int) -> int:
        if type(display_id) is not int or display_id <= 0:
            raise ValueError("Invalid product display ID")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id FROM products WHERE display_id=?", (display_id,)
            ).fetchone()
            if row is None:
                raise NotFound("Product display ID not found")
            return row["id"]

    @staticmethod
    def _active_sale(conn, product_id: int):
        return conn.execute(
            """SELECT * FROM product_sales
               WHERE product_id=? AND active=1 AND sold_units<max_units""",
            (product_id,),
        ).fetchone()

    @staticmethod
    def _close_sale(conn, product_id: int) -> int | None:
        sale = conn.execute(
            "SELECT id FROM product_sales WHERE product_id=? AND active=1",
            (product_id,),
        ).fetchone()
        if sale is None:
            return None
        conn.execute(
            """UPDATE product_sales SET active=0,
                      ended_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?""",
            (sale["id"],),
        )
        conn.execute(
            """UPDATE sale_announcement_deliveries SET status='skipped'
               WHERE sale_id=? AND status='pending'""",
            (sale["id"],),
        )
        return sale["id"]

    def start_sale(
        self, product_id: int, sale_price: int, max_units: int,
        *, announce_except: frozenset[int] | None = None,
    ) -> dict:
        if (not isinstance(sale_price, int) or isinstance(sale_price, bool)
                or not isinstance(max_units, int) or isinstance(max_units, bool)
                or sale_price <= 0 or max_units <= 0):
            raise ValueError("Invalid sale")
        with self._write() as conn:
            product = conn.execute(
                "SELECT price FROM products WHERE id=? AND active=1 AND deleted=0",
                (product_id,),
            ).fetchone()
            if product is None:
                raise NotFound("Product not available")
            if self._active_sale(conn, product_id):
                raise SaleActive("Product already has an active sale")
            available = conn.execute(
                "SELECT COUNT(*) FROM stock WHERE product_id=? AND sold_at IS NULL",
                (product_id,),
            ).fetchone()[0]
            if available == 0:
                raise OutOfStock("Out of stock")
            if sale_price >= product["price"] or max_units > available:
                raise ValueError("Sale price or quantity is invalid")
            self._close_sale(conn, product_id)
            sale_id = conn.execute(
                """INSERT INTO product_sales(product_id, original_price, sale_price, max_units)
                   VALUES (?, ?, ?, ?)""",
                (product_id, product["price"], sale_price, max_units),
            ).lastrowid
            recipients = 0
            if announce_except is not None:
                excluded = sorted(announce_except)
                sql = ("INSERT INTO sale_announcement_deliveries(sale_id, user_id) "
                       "SELECT ?, f.user_id FROM product_follows f "
                       "WHERE f.product_id=? AND NOT EXISTS "
                       "(SELECT 1 FROM spam_bans b WHERE b.user_id=f.user_id)")
                if excluded:
                    sql += " AND f.user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                conn.execute(sql, (sale_id, product_id, *excluded))
                recipients = conn.execute(
                    "SELECT COUNT(*) FROM sale_announcement_deliveries WHERE sale_id=?",
                    (sale_id,),
                ).fetchone()[0]
            return {
                "id": sale_id, "product_id": product_id, "original_price": product["price"],
                "sale_price": sale_price, "max_units": max_units,
                "available": available, "recipients": recipients,
            }

    def end_sale(self, product_id: int) -> int:
        with self._write() as conn:
            sale_id = self._close_sale(conn, product_id)
            if sale_id is None:
                raise NotFound("No active sale")
            return sale_id

    def list_due_sale_announcements(self, limit: int = 1) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT d.sale_id, d.user_id, d.attempts
                   FROM sale_announcement_deliveries d WHERE d.status='pending'
                     AND d.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY d.next_attempt_at, d.sale_id, d.user_id LIMIT ?""",
                (limit,),
            )]

    def get_sale_announcement(self, sale_id: int, user_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT d.status, sale.product_id, sale.original_price, sale.sale_price,
                          sale.max_units,
                          sale.max_units - sale.sold_units AS sale_remaining,
                          sale.active AS sale_active,
                          p.name, p.active AS product_active, p.deleted,
                          EXISTS (SELECT 1 FROM product_follows f
                                  WHERE f.user_id=d.user_id
                                    AND f.product_id=sale.product_id) AS following,
                          (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id
                           AND s.sold_at IS NULL) AS available
                   FROM sale_announcement_deliveries d
                   JOIN product_sales sale ON sale.id=d.sale_id
                   JOIN products p ON p.id=sale.product_id
                   WHERE d.sale_id=? AND d.user_id=?""",
                (sale_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def finish_sale_announcement(self, sale_id: int, user_id: int, status: str) -> None:
        if status not in ("sent", "skipped"):
            raise ValueError("Invalid announcement status")
        with self._write() as conn:
            conn.execute(
                """UPDATE sale_announcement_deliveries SET status=?, attempts=attempts+1,
                          sent_at=CASE WHEN ?='sent' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                       ELSE NULL END
                   WHERE sale_id=? AND user_id=? AND status='pending'""",
                (status, status, sale_id, user_id),
            )

    def retry_sale_announcement(self, sale_id: int, user_id: int, delay_seconds: int) -> None:
        if delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._write() as conn:
            conn.execute(
                """UPDATE sale_announcement_deliveries
                   SET attempts=attempts+1, next_attempt_at=?
                   WHERE sale_id=? AND user_id=? AND status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 sale_id, user_id),
            )

    def create_coupon(
        self, code: str, kind: str, value: int, max_uses: int, expires_on: str | None = None,
        product_id: int | None = None,
    ) -> str:
        code = code.strip().upper()
        if (not COUPON_CODE_PATTERN.fullmatch(code) or kind not in ("percent", "fixed")
                or not isinstance(value, int) or value <= 0
                or (kind == "percent" and value >= 100)
                or not isinstance(max_uses, int) or not 1 <= max_uses <= 100_000
                or (product_id is not None and
                    (not isinstance(product_id, int) or product_id <= 0))):
            raise ValueError("Invalid coupon")
        if expires_on is not None:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expires_on):
                raise ValueError("Invalid expiry date")
            try:
                expires = date.fromisoformat(expires_on)
            except ValueError as exc:
                raise ValueError("Invalid expiry date") from exc
            if expires < datetime.now(SHOP_TIMEZONE).date():
                raise ValueError("Coupon already expired")
        with self._write() as conn:
            if product_id is not None and not conn.execute(
                "SELECT 1 FROM products WHERE id=? AND deleted=0", (product_id,)
            ).fetchone():
                raise NotFound("Product not found")
            try:
                conn.execute(
                    """INSERT INTO coupons(code, kind, value, max_uses, expires_on, product_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (code, kind, value, max_uses, expires_on, product_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Coupon code already exists") from exc
        return code

    def list_coupons(self):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT c.*, p.name AS product_name, p.display_id AS product_display_id,
                          COUNT(r.order_id) AS used FROM coupons c
                   LEFT JOIN products p ON p.id=c.product_id
                   LEFT JOIN coupon_redemptions r ON r.code=c.code
                   GROUP BY c.code ORDER BY c.created_at DESC, c.code"""
            )]

    def set_coupon_active(self, code: str, active: bool):
        with self._write() as conn:
            if not conn.execute(
                "UPDATE coupons SET active=? WHERE code=?",
                (int(active), code.strip().upper()),
            ).rowcount:
                raise NotFound("Coupon not found")

    def update_coupon(self, code: str, *, kind: str | None = None,
                      value: int | None = None, max_uses: int | None = None,
                      expires_on=_UNSET) -> None:
        code = code.strip().upper()
        with self._write() as conn:
            current = conn.execute(
                "SELECT kind, value, max_uses, expires_on FROM coupons WHERE code=?", (code,),
            ).fetchone()
            if current is None:
                raise NotFound("Coupon not found")
            next_kind = current["kind"] if kind is None else kind
            next_value = current["value"] if value is None else value
            next_uses = current["max_uses"] if max_uses is None else max_uses
            next_expiry = current["expires_on"] if expires_on is _UNSET else expires_on
            used = conn.execute(
                "SELECT COUNT(*) FROM coupon_redemptions WHERE code=?", (code,),
            ).fetchone()[0]
            if (next_kind not in ("percent", "fixed") or type(next_value) is not int
                    or next_value <= 0 or (next_kind == "percent" and next_value >= 100)
                    or type(next_uses) is not int or not max(1, used) <= next_uses <= 100_000):
                raise ValueError("Invalid coupon")
            if expires_on is not _UNSET and next_expiry is not None:
                if not isinstance(next_expiry, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}", next_expiry,
                ):
                    raise ValueError("Invalid expiry date")
                try:
                    expiry = date.fromisoformat(next_expiry)
                except ValueError as exc:
                    raise ValueError("Invalid expiry date") from exc
                if expiry < datetime.now(SHOP_TIMEZONE).date():
                    raise ValueError("Coupon already expired")
            conn.execute(
                "UPDATE coupons SET kind=?, value=?, max_uses=?, expires_on=? WHERE code=?",
                (next_kind, next_value, next_uses, next_expiry, code),
            )

    @staticmethod
    def _apply_coupon(
        conn, user_id: int, price: int, code: str | None, min_price: int = 1,
        target_product_id: int | None = None,
    ):
        if not code:
            return price, 0, None
        code = code.strip().upper()
        if not COUPON_CODE_PATTERN.fullmatch(code):
            raise CouponUnavailable("Invalid coupon code")
        coupon = conn.execute("SELECT * FROM coupons WHERE code=? AND active=1", (code,)).fetchone()
        if not coupon:
            raise CouponUnavailable("Coupon not available")
        if coupon["product_id"] is not None and coupon["product_id"] != target_product_id:
            raise CouponUnavailable("Coupon is not valid for this product")
        if coupon["expires_on"] and coupon["expires_on"] < datetime.now(SHOP_TIMEZONE).date().isoformat():
            raise CouponUnavailable("Coupon expired")
        if conn.execute(
            "SELECT 1 FROM coupon_redemptions WHERE code=? AND user_id=?", (code, user_id)
        ).fetchone():
            raise CouponUnavailable("Coupon already used by customer")
        uses = conn.execute(
            "SELECT COUNT(*) FROM coupon_redemptions WHERE code=?", (code,)
        ).fetchone()[0]
        if uses >= coupon["max_uses"]:
            raise CouponUnavailable("Coupon usage limit reached")
        discount = (
            price * coupon["value"] // 100 if coupon["kind"] == "percent"
            else coupon["value"]
        )
        if discount <= 0 or price - discount < min_price:
            raise CouponUnavailable("Coupon does not apply to this price")
        return price - discount, discount, code

    @staticmethod
    def _price_product_quantity(conn, user_id: int, product, sale,
                                coupon_code: str | None, quantity: int):
        if type(quantity) is not int or not 1 <= quantity <= MAX_CUSTOMER_EMAILS_PER_PURCHASE:
            raise ValueError("Invalid purchase quantity")
        if sale and coupon_code:
            raise SaleCouponConflict("Coupon cannot be combined with a sale")
        sale_units = min(quantity, sale["max_units"] - sale["sold_units"]) if sale else 0
        unit_prices = (([sale["sale_price"]] * sale_units if sale else []) +
                       [product["price"]] * (quantity - sale_units))
        price, discount, code = Store._apply_coupon(
            conn, user_id, sum(unit_prices), coupon_code, min_price=quantity,
            target_product_id=product["id"],
        )
        if discount:
            each, remainder = divmod(price, quantity)
            unit_prices = [each + (index < remainder) for index in range(quantity)]
        return price, discount, code, sale_units, unit_prices

    def quote_product(self, user_id: int, product_id: int,
                      coupon_code: str | None = None, *, quantity: int = 1):
        if type(quantity) is not int or not 1 <= quantity <= MAX_CUSTOMER_EMAILS_PER_PURCHASE:
            raise ValueError("Invalid purchase quantity")
        with closing(self._connect()) as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id=? AND active=1 AND deleted=0", (product_id,)
            ).fetchone()
            if not product:
                raise NotFound("Product not available")
            available = conn.execute(
                "SELECT COUNT(*) FROM stock WHERE product_id=? AND sold_at IS NULL", (product_id,)
            ).fetchone()[0]
            if available < quantity:
                raise OutOfStock("Out of stock")
            sale = self._active_sale(conn, product_id)
            sale_price = sale["sale_price"] if sale else None
            price, discount, code, sale_units, _ = self._price_product_quantity(
                conn, user_id, product, sale, coupon_code, quantity,
            )
            return {"name": product["name"], "base_price": product["price"] * quantity,
                    "price": price, "discount": discount, "coupon_code": code,
                    "requires_email": bool(product["requires_email"]),
                    "quantity": quantity, "unit_price": product["price"],
                    "sale_price": sale_price, "sale_units": sale_units,
                    "sale_remaining": sale["max_units"] - sale["sold_units"] if sale else 0}

    @staticmethod
    def _bundle_from_conn(conn, bundle_id: int):
        row = conn.execute("SELECT * FROM bundles WHERE id=?", (bundle_id,)).fetchone()
        if not row:
            return None
        bundle = dict(row)
        bundle["items"] = [dict(item) for item in conn.execute(
            """SELECT bi.product_id, p.display_id, bi.quantity, p.name, p.price,
                      p.active, p.deleted,
                      p.requires_email, p.delivery_type, p.file_only_delivery,
                      (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id
                       AND s.sold_at IS NULL) AS stock_available
               FROM bundle_items bi JOIN products p ON p.id=bi.product_id
               WHERE bi.bundle_id=? ORDER BY bi.product_id""", (bundle_id,)
        )]
        bundle["available"] = min(
            (item["stock_available"] // item["quantity"] if item["active"] and not item["deleted"] else 0
             for item in bundle["items"]), default=0,
        )
        bundle["regular_price"] = sum(item["price"] * item["quantity"] for item in bundle["items"])
        return bundle

    def get_bundle(self, bundle_id: int):
        with closing(self._connect()) as conn:
            return self._bundle_from_conn(conn, bundle_id)

    def list_bundles(self, include_inactive: bool = False, include_deleted: bool = False):
        conditions = []
        if not include_inactive:
            conditions.append("active=1")
        if not include_deleted:
            conditions.append("deleted=0")
        sql = "SELECT id FROM bundles"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id"
        with closing(self._connect()) as conn:
            return [self._bundle_from_conn(conn, row["id"]) for row in conn.execute(sql)]

    def create_bundle(self, name: str, price: int, items: dict[int, int], description: str = "") -> int:
        if (not 1 <= len(name) <= 100 or len(description) > 500 or not isinstance(price, int)
                or not isinstance(items, dict) or not items
                or any(not isinstance(pid, int) or pid <= 0 or not isinstance(qty, int)
                       or not 1 <= qty <= 10 for pid, qty in items.items())
                or not 2 <= sum(items.values()) <= 10 or price < sum(items.values())):
            raise ValueError("Invalid bundle")
        with self._write() as conn:
            regular_price = 0
            for product_id, quantity in items.items():
                product = conn.execute(
                    "SELECT price FROM products WHERE id=? AND active=1 AND deleted=0",
                    (product_id,),
                ).fetchone()
                if not product:
                    raise NotFound("Product not available")
                regular_price += product["price"] * quantity
            if price >= regular_price:
                raise ValueError("Bundle price must be lower than separate prices")
            bundle_id = conn.execute(
                "INSERT INTO bundles(name, description, price) VALUES (?, ?, ?)",
                (name, description, price),
            ).lastrowid
            conn.executemany(
                "INSERT INTO bundle_items(bundle_id, product_id, quantity) VALUES (?, ?, ?)",
                ((bundle_id, pid, qty) for pid, qty in items.items()),
            )
            return bundle_id

    def update_bundle(self, bundle_id: int, name: str | None = None,
                      price: int | None = None, description: str | None = None):
        if (name is not None and not 1 <= len(name) <= 100
                or description is not None and len(description) > 500
                or price is not None and (not isinstance(price, int) or price <= 0)):
            raise ValueError("Invalid bundle")
        with self._write() as conn:
            bundle = self._bundle_from_conn(conn, bundle_id)
            if not bundle or bundle["deleted"]:
                raise NotFound("Bundle not found")
            new_price = bundle["price"] if price is None else price
            if new_price < sum(item["quantity"] for item in bundle["items"]):
                raise ValueError("Bundle price too low")
            previous_price = (
                bundle["price"] if new_price < bundle["price"] else
                None if new_price > bundle["price"] else bundle["previous_price"]
            )
            conn.execute(
                "UPDATE bundles SET name=?, price=?, previous_price=?, description=? WHERE id=?",
                (bundle["name"] if name is None else name, new_price, previous_price,
                 bundle["description"] if description is None else description, bundle_id),
            )

    def set_bundle_active(self, bundle_id: int, active: bool):
        with self._write() as conn:
            if not conn.execute(
                "UPDATE bundles SET active=? WHERE id=? AND deleted=0",
                (int(active), bundle_id),
            ).rowcount:
                raise NotFound("Bundle not found")

    def set_bundle_deleted(self, bundle_id: int, deleted: bool):
        with self._write() as conn:
            if not conn.execute("SELECT 1 FROM bundles WHERE id=?", (bundle_id,)).fetchone():
                raise NotFound("Bundle not found")
            conn.execute(
                "UPDATE bundles SET deleted=?, active=0 WHERE id=?",
                (int(deleted), bundle_id),
            )

    def quote_bundle(self, user_id: int, bundle_id: int, coupon_code: str | None = None):
        with closing(self._connect()) as conn:
            bundle = self._bundle_from_conn(conn, bundle_id)
            if not bundle or not bundle["active"] or bundle["deleted"]:
                raise NotFound("Bundle not available")
            if not bundle["available"]:
                raise OutOfStock("Bundle out of stock")
            price, discount, code = self._apply_coupon(
                conn, user_id, bundle["price"], coupon_code,
                sum(item["quantity"] for item in bundle["items"]),
            )
            return {"name": bundle["name"], "base_price": bundle["price"],
                    "price": price, "discount": discount, "coupon_code": code,
                    "requires_email": any(item["requires_email"] for item in bundle["items"])}

    def add_product(self, name: str, price: int, description: str = "",
                    *, delivery_type: str = "account") -> int:
        if (not (1 <= len(name) <= 100 and 0 <= len(description) <= 500 and price > 0)
                or delivery_type not in ("account", "file")):
            raise ValueError("Invalid product")
        with self._write() as conn:
            product_id = conn.execute(
                """INSERT INTO products(name, description, price, delivery_type,
                                        file_only_delivery, sort_order)
                   VALUES (?, ?, ?, ?, ?,
                           (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM products))""",
                (name, description, price, delivery_type, int(delivery_type == "file")),
            ).lastrowid
            display_id = product_id
            while conn.execute(
                "SELECT 1 FROM products WHERE display_id=?", (display_id,)
            ).fetchone():
                display_id += 1
            conn.execute(
                "UPDATE products SET display_id=? WHERE id=?", (display_id, product_id)
            )
            return product_id

    def set_product_display_id(self, product_id: int, display_id: int) -> dict:
        if (type(display_id) is not int or not 1 <= display_id <= 999_999 or
                type(product_id) is not int or product_id <= 0):
            raise ValueError("Product display ID must be between 1 and 999999")
        with self._write() as conn:
            current = conn.execute(
                "SELECT display_id FROM products WHERE id=?", (product_id,)
            ).fetchone()
            if not current:
                raise NotFound("Product not found")
            old_display_id = current["display_id"]
            if old_display_id is None:
                old_display_id = product_id
                while conn.execute(
                    "SELECT 1 FROM products WHERE display_id=?", (old_display_id,)
                ).fetchone():
                    old_display_id += 1
            if old_display_id == display_id:
                if current["display_id"] is None:
                    conn.execute(
                        "UPDATE products SET display_id=? WHERE id=?",
                        (display_id, product_id),
                    )
                return {"display_id": display_id, "swapped_product_id": None}
            other = conn.execute(
                "SELECT id FROM products WHERE display_id=?", (display_id,)
            ).fetchone()
            if other:
                conn.execute("UPDATE products SET display_id=NULL WHERE id=?", (other["id"],))
            conn.execute(
                "UPDATE products SET display_id=? WHERE id=?", (display_id, product_id)
            )
            if other:
                conn.execute(
                    "UPDATE products SET display_id=? WHERE id=?",
                    (old_display_id, other["id"]),
                )
            return {"display_id": display_id,
                    "swapped_product_id": other["id"] if other else None}

    @staticmethod
    def _ordered_product_ids(conn) -> list[int]:
        return [row["id"] for row in conn.execute(
            "SELECT id FROM products WHERE deleted=0 ORDER BY sort_order, id"
        )]

    @staticmethod
    def _save_product_order(conn, ids: list[int]) -> None:
        for position, product_id in enumerate(ids, start=1):
            conn.execute(
                "UPDATE products SET sort_order=? WHERE id=?", (position, product_id)
            )

    def move_product(self, product_id: int, position: int) -> dict:
        if type(product_id) is not int or product_id <= 0 or type(position) is not int:
            raise ValueError("Invalid product position")
        with self._write() as conn:
            ids = self._ordered_product_ids(conn)
            if product_id not in ids:
                raise NotFound("Product not found or archived")
            if not 1 <= position <= len(ids):
                raise ValueError("Product position is out of range")
            ids.remove(product_id)
            ids.insert(position - 1, product_id)
            self._save_product_order(conn, ids)
            return {"position": position, "count": len(ids)}

    def shift_product(self, product_id: int, direction: int) -> dict:
        if (type(product_id) is not int or product_id <= 0 or
                type(direction) is not int or direction not in (-1, 1)):
            raise ValueError("Invalid product move")
        with self._write() as conn:
            ids = self._ordered_product_ids(conn)
            if product_id not in ids:
                raise NotFound("Product not found or archived")
            old_index = ids.index(product_id)
            new_index = max(0, min(len(ids) - 1, old_index + direction))
            ids.insert(new_index, ids.pop(old_index))
            self._save_product_order(conn, ids)
            return {"position": new_index + 1, "count": len(ids)}

    def list_due_stock_announcements(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT d.batch_id, b.product_id, b.added_count, d.user_id, d.attempts,
                          p.name, p.description, p.price, p.previous_price,
                          p.active, p.deleted,
                          (SELECT sale.sale_price FROM product_sales sale
                           WHERE sale.product_id=p.id AND sale.active=1) AS sale_price,
                          (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id
                           AND s.sold_at IS NULL) AS available
                   FROM stock_announcement_deliveries d
                   JOIN stock_announcement_batches b ON b.id=d.batch_id
                   JOIN products p ON p.id=b.product_id
                   JOIN users u ON u.telegram_id=d.user_id
                   WHERE d.status='pending' AND u.stock_notifications_enabled=1
                     AND d.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY d.next_attempt_at, d.batch_id, d.user_id LIMIT ?""",
                (limit,),
            )]

    def stock_announcement_sendable(self, batch_id: int, user_id: int) -> bool:
        with closing(self._connect()) as conn:
            return bool(conn.execute(
                """SELECT 1 FROM stock_announcement_deliveries d
                   JOIN users u ON u.telegram_id=d.user_id
                   JOIN stock_announcement_batches b ON b.id=d.batch_id
                   JOIN products p ON p.id=b.product_id
                   WHERE d.batch_id=? AND d.user_id=? AND d.status='pending'
                     AND u.stock_notifications_enabled=1
                     AND p.active=1 AND p.deleted=0
                     AND EXISTS (SELECT 1 FROM product_follows f
                                 WHERE f.user_id=d.user_id AND f.product_id=p.id)
                     AND EXISTS (SELECT 1 FROM stock s
                                 WHERE s.product_id=p.id AND s.sold_at IS NULL)""",
                (batch_id, user_id),
            ).fetchone())

    def finish_stock_announcement(self, batch_id: int, user_id: int, status: str) -> None:
        if status not in ("sent", "skipped"):
            raise ValueError("Invalid announcement status")
        with self._write() as conn:
            conn.execute(
                """UPDATE stock_announcement_deliveries SET status=?, attempts=attempts+1,
                          sent_at=CASE WHEN ?='sent' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                       ELSE NULL END
                   WHERE batch_id=? AND user_id=? AND status='pending'""",
                (status, status, batch_id, user_id),
            )

    def retry_stock_announcement(
        self, batch_id: int, user_id: int, delay_seconds: int,
    ) -> None:
        if delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
        with self._write() as conn:
            conn.execute(
                """UPDATE stock_announcement_deliveries
                   SET attempts=attempts+1, next_attempt_at=?
                   WHERE batch_id=? AND user_id=? AND status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 batch_id, user_id),
            )

    def update_product_details(
        self, product_id: int, name: str | None = None, description: str | None = None,
    ) -> dict:
        if name is not None and not 1 <= len(name) <= 100:
            raise ValueError("Invalid product name")
        if description is not None and len(description) > 500:
            raise ValueError("Invalid product description")
        with self._write() as conn:
            product = conn.execute(
                "SELECT name, description FROM products WHERE id=? AND deleted=0", (product_id,)
            ).fetchone()
            if not product:
                raise NotFound("Product not found")
            updated = {
                "name": product["name"] if name is None else name,
                "description": product["description"] if description is None else description,
            }
            conn.execute(
                "UPDATE products SET name=?, description=? WHERE id=?",
                (updated["name"], updated["description"], product_id),
            )
            return updated

    def set_price(self, product_id: int, price: int):
        if price <= 0:
            raise ValueError("Price must be positive")
        with self._write() as conn:
            if self._active_sale(conn, product_id):
                raise SaleActive("End the sale before changing the regular price")
            product = conn.execute(
                "SELECT price, previous_price FROM products WHERE id=? AND deleted=0",
                (product_id,),
            ).fetchone()
            if not product:
                raise NotFound("Product not found")
            previous_price = (
                product["price"] if price < product["price"] else
                None if price > product["price"] else product["previous_price"]
            )
            conn.execute(
                "UPDATE products SET price=?, previous_price=? WHERE id=?",
                (price, previous_price, product_id),
            )

    def set_requires_email(self, product_id: int, required: bool) -> None:
        if type(required) is not bool:
            raise ValueError("Email setting must be true or false")
        with self._write() as conn:
            if not conn.execute(
                "UPDATE products SET requires_email=? WHERE id=? AND deleted=0",
                (int(required), product_id),
            ).rowcount:
                raise NotFound("Product not found")

    def set_file_only_delivery(self, product_id: int, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("Delivery setting must be true or false")
        with self._write() as conn:
            product = conn.execute(
                "SELECT delivery_type FROM products WHERE id=? AND deleted=0",
                (product_id,),
            ).fetchone()
            if not product:
                raise NotFound("Product not found")
            if product["delivery_type"] == "file" and not enabled:
                raise ValueError("JSON products must be delivered only in .json")
            conn.execute(
                "UPDATE products SET file_only_delivery=? WHERE id=?",
                (int(enabled), product_id),
            )

    def set_active(self, product_id: int, active: bool):
        with self._write() as conn:
            if not conn.execute(
                "UPDATE products SET active=? WHERE id=? AND deleted=0", (int(active), product_id)
            ).rowcount:
                raise NotFound("Product not found")
            if not active:
                self._close_sale(conn, product_id)

    def set_deleted(self, product_id: int, deleted: bool):
        with self._write() as conn:
            if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                raise NotFound("Product not found")
            conn.execute(
                "UPDATE products SET deleted=?, active=0 WHERE id=?",
                (int(deleted), product_id),
            )
            if deleted:
                self._close_sale(conn, product_id)

    def add_stock(self, product_id: int, secret: str) -> int:
        return self.bulk_add_stock(product_id, [secret], allow_duplicates=False)["ids"][0]

    def add_file_stock(self, product_id: int, content: bytes, *,
                       announce_except: frozenset[int] | None = None) -> dict:
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_FILE_STOCK_BYTES:
            raise ValueError("JSON file must be 1-1000000 bytes")
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("JSON file must be UTF-8") from exc
        parse_json_file(decoded)
        return self.bulk_add_stock(product_id, [decoded],
                                   announce_except=announce_except,
                                   stock_type="file")

    def bulk_add_stock(
        self, product_id: int, secrets: list[str],
        *, announce_except: frozenset[int] | None = None,
        allow_duplicates: bool = True,
        stock_type: str = "account",
    ) -> dict:
        if stock_type not in ("account", "file"):
            raise ValueError("Invalid stock type")
        if not 1 <= len(secrets) <= 100:
            raise ValueError("Batch must contain 1-100 accounts")
        entries = []
        for secret in secrets:
            if not isinstance(secret, str):
                raise ValueError("Account data must have 1-3000 characters")
            if stock_type == "account":
                secret = strip_pasted_table_borders(secret)
                if not 1 <= len(secret) <= 3000:
                    raise ValueError("Account data must have 1-3000 characters")
            raw = secret.encode("utf-8")
            if stock_type == "file":
                if not 0 < len(raw) <= MAX_FILE_STOCK_BYTES:
                    raise ValueError("JSON file must be 1-1000000 bytes")
                parse_json_file(secret)
            entries.append((
                self.cipher.encrypt(raw).decode("ascii"),
                hmac.new(self.hash_key, raw, hashlib.sha256).hexdigest(),
            ))
        with self._write() as conn:
            product = conn.execute(
                "SELECT active, delivery_type FROM products WHERE id=? AND deleted=0", (product_id,)
            ).fetchone()
            if not product:
                raise NotFound("Product not found")
            if product["delivery_type"] != stock_type:
                raise ValueError("Stock type does not match product")
            ids = []
            duplicate_lines = []
            seen_hashes = set()
            for line_number, (ciphertext, secret_hash) in enumerate(entries, start=1):
                duplicate = (secret_hash in seen_hashes or conn.execute(
                    "SELECT 1 FROM stock WHERE secret_hash=? LIMIT 1", (secret_hash,)
                ).fetchone() is not None)
                if duplicate:
                    if not allow_duplicates:
                        raise DuplicateStock("Account already in inventory")
                    duplicate_lines.append(line_number)
                ids.append(conn.execute(
                    """INSERT INTO stock(product_id, secret_ciphertext, secret_hash)
                       VALUES (?, ?, ?)""",
                    (product_id, ciphertext, secret_hash),
                ).lastrowid)
                seen_hashes.add(secret_hash)
            available = conn.execute(
                "SELECT COUNT(*) FROM stock WHERE product_id=? AND sold_at IS NULL",
                (product_id,),
            ).fetchone()[0]
            if ids and available > LOW_STOCK_THRESHOLD:
                conn.execute(
                    "UPDATE products SET low_stock_alerted=0 WHERE id=?", (product_id,)
                )
            announcement_id = None
            recipients = 0
            if ids and announce_except is not None and product["active"]:
                announcement_id = conn.execute(
                    "INSERT INTO stock_announcement_batches(product_id, added_count) VALUES (?, ?)",
                    (product_id, len(ids)),
                ).lastrowid
                excluded = sorted(announce_except)
                sql = ("INSERT INTO stock_announcement_deliveries(batch_id, user_id) "
                       "SELECT ?, f.user_id FROM product_follows f "
                       "JOIN users u ON u.telegram_id=f.user_id "
                       "WHERE f.product_id=? AND u.stock_notifications_enabled=1 AND NOT EXISTS "
                       "(SELECT 1 FROM spam_bans b WHERE b.user_id=u.telegram_id)")
                if excluded:
                    sql += " AND f.user_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                conn.execute(sql, (announcement_id, product_id, *excluded))
                recipients = conn.execute(
                    "SELECT COUNT(*) FROM stock_announcement_deliveries WHERE batch_id=?",
                    (announcement_id,),
                ).fetchone()[0]
            return {"ids": ids, "duplicate_lines": duplicate_lines,
                    "available": available,
                    "announcement_id": announcement_id, "recipients": recipients}

    def stock_counts(self, product_id: int) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(sold_at IS NULL), 0) AS available,
                          COALESCE(SUM(sold_at IS NOT NULL), 0) AS sold
                   FROM stock WHERE product_id=?""", (product_id,)
            ).fetchone()
            return dict(row)

    def list_low_stock_alerts(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT p.id AS product_id, p.name,
                          (SELECT COUNT(*) FROM stock s
                           WHERE s.product_id=p.id AND s.sold_at IS NULL) AS available
                   FROM products p
                   WHERE p.active=1 AND p.deleted=0 AND p.low_stock_alerted=0
                     AND EXISTS (SELECT 1 FROM stock s WHERE s.product_id=p.id)
                     AND (SELECT COUNT(*) FROM stock s
                          WHERE s.product_id=p.id AND s.sold_at IS NULL) <= ?
                   ORDER BY p.id""", (LOW_STOCK_THRESHOLD,)
            )]

    def claim_low_stock_alert(self, product_id: int) -> bool:
        with self._write() as conn:
            return bool(conn.execute(
                """UPDATE products SET low_stock_alerted=1
                   WHERE id=? AND active=1 AND deleted=0 AND low_stock_alerted=0
                     AND EXISTS (SELECT 1 FROM stock s WHERE s.product_id=products.id)
                     AND (SELECT COUNT(*) FROM stock s
                          WHERE s.product_id=products.id AND s.sold_at IS NULL) <= ?""",
                (product_id, LOW_STOCK_THRESHOLD),
            ).rowcount)

    def reset_low_stock_alert(self, product_id: int) -> None:
        with self._write() as conn:
            conn.execute("UPDATE products SET low_stock_alerted=0 WHERE id=?", (product_id,))

    def list_stock_items(self, product_id: int, limit: int = 9, offset: int = 0,
                         *, include_secret: bool = False):
        with closing(self._connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                """SELECT id, created_at""" + (", secret_ciphertext" if include_secret else "") + """
                   FROM stock
                   WHERE product_id=? AND sold_at IS NULL ORDER BY id LIMIT ? OFFSET ?""",
                (product_id, limit, offset),
            )]
            if include_secret:
                for row in rows:
                    row["secret"] = self.cipher.decrypt(
                        row.pop("secret_ciphertext").encode("ascii")
                    ).decode("utf-8")
            return rows

    def get_stock_item(self, stock_id: int):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT s.id, s.product_id, s.secret_ciphertext, s.created_at, p.name
                   FROM stock s JOIN products p ON p.id=s.product_id
                   WHERE s.id=? AND s.sold_at IS NULL""", (stock_id,)
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["secret"] = self.cipher.decrypt(
                item.pop("secret_ciphertext").encode("ascii")
            ).decode("utf-8")
            return item

    def list_sold_stock_items(self, product_id: int, limit: int = 9,
                              offset: int = 0) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT s.id, s.sold_at, o.id AS order_id, o.user_id,
                          u.username, u.display_name
                   FROM stock s
                   LEFT JOIN orders o ON o.stock_id=s.id
                   LEFT JOIN users u ON u.telegram_id=o.user_id
                   WHERE s.product_id=? AND s.sold_at IS NOT NULL
                   ORDER BY s.sold_at DESC, s.id DESC LIMIT ? OFFSET ?""",
                (product_id, limit, offset),
            )]

    def get_sold_stock_item(self, stock_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT s.id, s.product_id, s.secret_ciphertext, s.sold_at,
                          p.display_id, p.name AS product_name,
                          o.id AS order_id, o.user_id, o.price, o.created_at,
                          o.customer_email_ciphertext, u.username, u.display_name
                   FROM stock s JOIN products p ON p.id=s.product_id
                   LEFT JOIN orders o ON o.stock_id=s.id
                   LEFT JOIN users u ON u.telegram_id=o.user_id
                   WHERE s.id=? AND s.sold_at IS NOT NULL""", (stock_id,),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["secret"] = self.cipher.decrypt(
                item.pop("secret_ciphertext").encode("ascii")
            ).decode("utf-8")
            encrypted_email = item.pop("customer_email_ciphertext")
            item["customer_email"] = (self.cipher.decrypt(
                encrypted_email.encode("ascii")
            ).decode("utf-8") if encrypted_email else None)
            return item

    @staticmethod
    def _queue_purchase_notifications(conn, kind: str, ref_id: int,
                                      admin_ids: frozenset[int]) -> None:
        for admin_id in sorted(admin_ids):
            conn.execute(
                """INSERT INTO purchase_notification_deliveries(kind, ref_id, admin_id)
                   VALUES (?, ?, ?)""",
                (kind, ref_id, admin_id),
            )

    def purchase(self, user_id: int, product_id: int, expected_price: int | None = None,
                 coupon_code: str | None = None, *,
                 customer_email: str | None = None,
                 notify_admin_ids: frozenset[int] = frozenset()) -> dict:
        with self._write() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id=? AND active=1 AND deleted=0", (product_id,)
            ).fetchone()
            if not product:
                raise NotFound("Product not available")
            email = (normalize_customer_email(customer_email)
                     if product["requires_email"] else None)
            emails = email.split(", ") if email else [None]
            quantity = len(emails)
            sale = self._active_sale(conn, product_id)
            price, discount, code, sale_units, unit_prices = self._price_product_quantity(
                conn, user_id, product, sale, coupon_code, quantity,
            )
            if expected_price is not None and price != expected_price:
                raise PriceChanged("Product price changed")
            user = conn.execute("SELECT balance FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if not user or user["balance"] < price:
                raise InsufficientBalance("Insufficient balance")
            items = conn.execute(
                """SELECT id, secret_ciphertext FROM stock
                   WHERE product_id=? AND sold_at IS NULL ORDER BY id LIMIT ?""",
                (product_id, quantity),
            ).fetchall()
            if len(items) != quantity:
                raise OutOfStock("Out of stock")
            orders = []
            for index, (item, unit_email, unit_price) in enumerate(
                zip(items, emails, unit_prices)
            ):
                secret = self.cipher.decrypt(
                    item["secret_ciphertext"].encode("ascii")
                ).decode("utf-8")
                email_ciphertext = (
                    self.cipher.encrypt(unit_email.encode("utf-8")).decode("ascii")
                    if unit_email else None
                )
                conn.execute(
                    "UPDATE stock SET sold_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                    (item["id"],),
                )
                order_id = conn.execute(
                    """INSERT INTO orders(user_id, product_id, stock_id, price, product_name,
                                          sale_id, customer_email_ciphertext, file_only_delivery)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, product_id, item["id"], unit_price, product["name"],
                     sale["id"] if index < sale_units else None, email_ciphertext,
                     product["file_only_delivery"]),
                ).lastrowid
                orders.append({"id": order_id, "name": product["name"],
                               "delivery_type": product["delivery_type"],
                               "file_only_delivery": product["file_only_delivery"],
                               "secret": secret, "price": unit_price,
                               "customer_email": unit_email})
            self._queue_purchase_notifications(
                conn, "product", orders[0]["id"], notify_admin_ids,
            )
            if sale_units:
                conn.execute(
                    "UPDATE product_sales SET sold_units=sold_units+? WHERE id=?",
                    (sale_units, sale["id"]),
                )
                if sale["sold_units"] + sale_units == sale["max_units"]:
                    self._close_sale(conn, product_id)
            conn.execute(
                "UPDATE users SET balance=balance-? WHERE telegram_id=?",
                (price, user_id),
            )
            conn.execute(
                "INSERT INTO ledger(user_id, amount, kind, ref_id) VALUES (?, ?, 'purchase', ?)",
                (user_id, -price, orders[0]["id"]),
            )
            if code:
                conn.execute(
                    "INSERT INTO coupon_redemptions(code, user_id, order_id) VALUES (?, ?, ?)",
                    (code, user_id, orders[0]["id"]),
                )
            available = conn.execute(
                "SELECT COUNT(*) FROM stock WHERE product_id=? AND sold_at IS NULL",
                (product_id,),
            ).fetchone()[0]
            low_stock_alert = available <= LOW_STOCK_THRESHOLD and not product["low_stock_alerted"]
            if low_stock_alert:
                conn.execute(
                    "UPDATE products SET low_stock_alerted=1 WHERE id=?", (product_id,)
                )
            balance = user["balance"] - price
            return {
                "id": orders[0]["id"], "name": product["name"],
                "secret": orders[0]["secret"], "orders": orders,
                "quantity": quantity, "price": price,
                "base_price": product["price"] * quantity,
                "discount": discount, "coupon_code": code, "balance": balance,
                "sale_id": sale["id"] if sale_units else None,
                "customer_email": email,
                "product_id": product_id, "available": available,
                "low_stock_alert": low_stock_alert,
            }

    def purchase_bundle(self, user_id: int, bundle_id: int,
                        expected_price: int | None = None,
                        coupon_code: str | None = None, *,
                        customer_email: str | None = None,
                        notify_admin_ids: frozenset[int] = frozenset()) -> dict:
        with self._write() as conn:
            bundle = self._bundle_from_conn(conn, bundle_id)
            if not bundle or not bundle["active"] or bundle["deleted"]:
                raise NotFound("Bundle not available")
            if not bundle["available"]:
                raise OutOfStock("Bundle out of stock")
            email = (normalize_customer_email(customer_email)
                     if any(item["requires_email"] for item in bundle["items"]) else None)
            email_ciphertext = (self.cipher.encrypt(email.encode("utf-8")).decode("ascii")
                                if email else None)
            quantity = sum(item["quantity"] for item in bundle["items"])
            price, discount, code = self._apply_coupon(
                conn, user_id, bundle["price"], coupon_code, quantity,
            )
            if expected_price is not None and price != expected_price:
                raise PriceChanged("Bundle price changed")
            user = conn.execute("SELECT balance FROM users WHERE telegram_id=?", (user_id,)).fetchone()
            if not user or user["balance"] < price:
                raise InsufficientBalance("Insufficient balance")
            selected = []
            for component in bundle["items"]:
                rows = conn.execute(
                    """SELECT id, secret_ciphertext FROM stock
                       WHERE product_id=? AND sold_at IS NULL ORDER BY id LIMIT ?""",
                    (component["product_id"], component["quantity"]),
                ).fetchall()
                if len(rows) != component["quantity"]:
                    raise OutOfStock("Bundle out of stock")
                selected.extend((component, row) for row in rows)
            bundle_order_id = conn.execute(
                """INSERT INTO bundle_orders(user_id, bundle_id, bundle_name, price, coupon_code)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, bundle_id, bundle["name"], price, code),
            ).lastrowid
            each_price, remainder = divmod(price, quantity)
            orders = []
            for index, (component, stock) in enumerate(selected):
                item_price = each_price + (1 if index < remainder else 0)
                secret = self.cipher.decrypt(stock["secret_ciphertext"].encode("ascii")).decode("utf-8")
                conn.execute(
                    "UPDATE stock SET sold_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                    (stock["id"],),
                )
                order_id = conn.execute(
                    """INSERT INTO orders(user_id, product_id, stock_id, price, product_name,
                                          bundle_order_id, customer_email_ciphertext,
                                          file_only_delivery)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, component["product_id"], stock["id"], item_price,
                     component["name"], bundle_order_id,
                     email_ciphertext if component["requires_email"] else None,
                     component["file_only_delivery"]),
                ).lastrowid
                orders.append({"id": order_id, "name": component["name"], "secret": secret,
                               "delivery_type": component["delivery_type"],
                               "file_only_delivery": component["file_only_delivery"],
                               "product_id": component["product_id"], "price": item_price,
                               "customer_email": email if component["requires_email"] else None})
            conn.execute(
                "UPDATE users SET balance=balance-? WHERE telegram_id=?", (price, user_id)
            )
            conn.execute(
                "INSERT INTO ledger(user_id, amount, kind, ref_id) VALUES (?, ?, 'purchase', ?)",
                (user_id, -price, orders[0]["id"]),
            )
            if code:
                conn.execute(
                    "INSERT INTO coupon_redemptions(code, user_id, order_id) VALUES (?, ?, ?)",
                    (code, user_id, orders[0]["id"]),
                )
            alerts = []
            for component in bundle["items"]:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM stock WHERE product_id=? AND sold_at IS NULL",
                    (component["product_id"],),
                ).fetchone()[0]
                if remaining <= LOW_STOCK_THRESHOLD:
                    changed = conn.execute(
                        """UPDATE products SET low_stock_alerted=1
                           WHERE id=? AND low_stock_alerted=0""",
                        (component["product_id"],),
                    ).rowcount
                    if changed:
                        alerts.append({"product_id": component["product_id"],
                                       "name": component["name"], "available": remaining})
            self._queue_purchase_notifications(conn, "bundle", bundle_order_id, notify_admin_ids)
            return {"id": bundle_order_id, "name": bundle["name"], "orders": orders,
                    "price": price, "base_price": bundle["price"], "discount": discount,
                    "coupon_code": code, "balance": user["balance"] - price,
                    "low_stock_alerts": alerts}

    def list_due_purchase_notifications(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT n.kind, n.ref_id, n.admin_id, n.attempts,
                          COALESCE(o.user_id, b.user_id) AS user_id,
                          u.username, u.display_name,
                          COALESCE(o.product_name, b.bundle_name) AS name,
                          COALESCE(o.price, b.price) AS price,
                          o.product_id, p.display_id AS product_display_id, b.bundle_id,
                          CASE WHEN n.kind='product' THEN
                              CASE WHEN o.customer_email_ciphertext IS NOT NULL THEN o.id END
                          ELSE (SELECT MIN(bo.id) FROM orders bo
                                WHERE bo.bundle_order_id=b.id
                                  AND bo.customer_email_ciphertext IS NOT NULL)
                          END AS contact_order_id
                   FROM purchase_notification_deliveries n
                   LEFT JOIN orders o ON n.kind='product' AND o.id=n.ref_id
                   LEFT JOIN products p ON p.id=o.product_id
                   LEFT JOIN bundle_orders b ON n.kind='bundle' AND b.id=n.ref_id
                   JOIN users u ON u.telegram_id=COALESCE(o.user_id, b.user_id)
                   WHERE n.status='pending'
                     AND n.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY n.next_attempt_at, n.kind, n.ref_id, n.admin_id LIMIT ?""",
                (limit,),
            )]

    def list_due_new_customer_notifications(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT n.user_id, n.admin_id, n.attempts,
                          u.username, u.display_name, u.created_at
                   FROM new_customer_notification_deliveries n
                   JOIN users u ON u.telegram_id=n.user_id
                   WHERE n.status='pending'
                     AND n.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY n.next_attempt_at, n.user_id, n.admin_id LIMIT ?""",
                (limit,),
            )]

    def finish_new_customer_notification(self, user_id: int, admin_id: int,
                                         status: str) -> None:
        if status not in ("sent", "skipped"):
            raise ValueError("Invalid new customer notification status")
        with self._write() as conn:
            conn.execute(
                """UPDATE new_customer_notification_deliveries
                   SET status=?, attempts=attempts+1,
                       sent_at=CASE WHEN ?='sent' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                    ELSE NULL END
                   WHERE user_id=? AND admin_id=? AND status='pending'""",
                (status, status, user_id, admin_id),
            )

    def retry_new_customer_notification(self, user_id: int, admin_id: int,
                                        delay_seconds: int) -> None:
        if delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._write() as conn:
            conn.execute(
                """UPDATE new_customer_notification_deliveries
                   SET attempts=attempts+1, next_attempt_at=?
                   WHERE user_id=? AND admin_id=? AND status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 user_id, admin_id),
            )

    def bundle_order_items(self, bundle_order_id: int) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT o.product_id, p.display_id, o.product_name,
                          COUNT(*) AS quantity
                   FROM orders o LEFT JOIN products p ON p.id=o.product_id
                   WHERE o.bundle_order_id=?
                   GROUP BY o.product_id, p.display_id, o.product_name
                   ORDER BY o.product_id""",
                (bundle_order_id,),
            )]

    def purchase_id_for_bundle_order(self, bundle_order_id: int) -> int | None:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT MIN(id) FROM orders WHERE bundle_order_id=?",
                (bundle_order_id,),
            ).fetchone()[0]

    def finish_purchase_notification(self, kind: str, ref_id: int, admin_id: int,
                                     status: str) -> None:
        if status not in ("sent", "skipped"):
            raise ValueError("Invalid purchase notification status")
        with self._write() as conn:
            conn.execute(
                """UPDATE purchase_notification_deliveries
                   SET status=?, attempts=attempts+1,
                       sent_at=CASE WHEN ?='sent' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                    ELSE NULL END
                   WHERE kind=? AND ref_id=? AND admin_id=? AND status='pending'""",
                (status, status, kind, ref_id, admin_id),
            )

    def retry_purchase_notification(self, kind: str, ref_id: int, admin_id: int,
                                    delay_seconds: int) -> None:
        if delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._write() as conn:
            conn.execute(
                """UPDATE purchase_notification_deliveries
                   SET attempts=attempts+1, next_attempt_at=?
                   WHERE kind=? AND ref_id=? AND admin_id=? AND status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 kind, ref_id, admin_id),
            )

    def list_orders(self, user_id: int, limit: int = 10):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT o.id, o.price, o.created_at, o.bundle_order_id, b.bundle_name,
                          COALESCE(o.product_name, p.name) AS name,
                          o.customer_email_ciphertext IS NOT NULL AS has_email,
                          ec.completed_at AS email_completed_at
                   FROM orders o
                   JOIN products p ON p.id=o.product_id
                   LEFT JOIN bundle_orders b ON b.id=o.bundle_order_id
                   LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                   WHERE o.user_id=? ORDER BY o.id DESC LIMIT ?""", (user_id, limit)
            )]

    def list_purchased_products(self, user_id: int, limit: int = 10,
                                offset: int = 0) -> list[dict]:
        """Aggregate a buyer's purchased account items by their sale-time product name."""
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT COALESCE(o.product_name, p.name) AS name,
                          COUNT(*) AS quantity, MAX(o.id) AS last_order_id
                   FROM orders o JOIN products p ON p.id=o.product_id
                   WHERE o.user_id=?
                   GROUP BY o.product_id, COALESCE(o.product_name, p.name)
                   ORDER BY last_order_id DESC LIMIT ? OFFSET ?""",
                (user_id, limit, offset),
            )]

    def list_purchases(self, user_id: int, limit: int = 10) -> list[dict]:
        """List payments as orders; the existing orders rows are their account items.

        Each purchase ledger row points at its first account item. Purchases write
        all their items and the ledger row in one SQLite transaction, so the next
        purchase's first item is the exclusive end of this purchase's range.
        This also groups purchases made before this view was introduced.
        """
        with closing(self._connect()) as conn:
            purchases = [dict(row) for row in conn.execute(
                """SELECT l.ref_id AS id, -l.amount AS price, l.created_at,
                          (SELECT MIN(next.ref_id) FROM ledger next
                           WHERE next.kind='purchase' AND next.ref_id>l.ref_id) AS next_id
                   FROM ledger l WHERE l.kind='purchase' AND l.user_id=?
                   ORDER BY l.ref_id DESC LIMIT ?""", (user_id, limit),
            )]
            for purchase in purchases:
                purchase["items"] = [dict(row) for row in conn.execute(
                    """SELECT o.id, o.price, COALESCE(o.product_name, p.name) AS name,
                              p.delivery_type, o.file_only_delivery,
                              o.customer_email_ciphertext IS NOT NULL AS has_email,
                              ec.completed_at AS email_completed_at
                       FROM orders o JOIN products p ON p.id=o.product_id
                       LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                       WHERE o.user_id=? AND o.id>=? AND (? IS NULL OR o.id<?)
                       ORDER BY o.id""",
                    (user_id, purchase["id"], purchase["next_id"], purchase["next_id"]),
                )]
                purchase.pop("next_id")
            return purchases

    def get_purchase(self, user_id: int, purchase_id: int) -> dict | None:
        """Return one customer's complete purchase, decrypting only their items."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT l.ref_id AS id, -l.amount AS price, l.created_at,
                          (SELECT MIN(next.ref_id) FROM ledger next
                           WHERE next.kind='purchase' AND next.ref_id>l.ref_id) AS next_id
                   FROM ledger l WHERE l.kind='purchase' AND l.user_id=?
                     AND l.ref_id=?""", (user_id, purchase_id),
            ).fetchone()
            if not row:
                return None
            purchase = dict(row)
            items = [dict(item) for item in conn.execute(
                """SELECT o.id, o.price, COALESCE(o.product_name, p.name) AS name,
                          p.delivery_type, o.file_only_delivery,
                          o.customer_email_ciphertext, s.secret_ciphertext,
                          ec.completed_at AS email_completed_at
                   FROM orders o JOIN products p ON p.id=o.product_id
                   JOIN stock s ON s.id=o.stock_id
                   LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                   WHERE o.user_id=? AND o.id>=? AND (? IS NULL OR o.id<?)
                   ORDER BY o.id""",
                (user_id, purchase_id, purchase["next_id"], purchase["next_id"]),
            )]
            if not items:
                return None
            for item in items:
                item["secret"] = self.cipher.decrypt(
                    item.pop("secret_ciphertext").encode("ascii")
                ).decode("utf-8")
                encrypted_email = item.pop("customer_email_ciphertext")
                item["customer_email"] = (self.cipher.decrypt(
                    encrypted_email.encode("ascii")
                ).decode("utf-8") if encrypted_email else None)
            purchase.pop("next_id")
            purchase["items"] = items
            return purchase

    def list_all_purchases(self, limit: int = 9, offset: int = 0,
                           buyer_query: str | None = None) -> list[dict]:
        """Admin order list, with one row per payment instead of per account."""
        where = "l.kind='purchase'"
        params = []
        if buyer_query is not None:
            buyer_where, params = self._user_search_condition(buyer_query)
            where += " AND " + buyer_where
        with closing(self._connect()) as conn:
            if buyer_query is not None:
                conn.create_function("user_search_key", 1, self._user_search_key,
                                     deterministic=True)
            purchases = [dict(row) for row in conn.execute(
                """SELECT l.ref_id AS id, l.user_id, -l.amount AS price, l.created_at,
                          u.username, u.display_name,
                          COALESCE(c.sent_count, 0) AS admin_contact_count,
                          c.last_sent_at AS admin_contact_last_sent_at,
                          (SELECT MIN(next.ref_id) FROM ledger next
                           WHERE next.kind='purchase' AND next.ref_id>l.ref_id) AS next_id
                   FROM ledger l JOIN users u ON u.telegram_id=l.user_id
                   LEFT JOIN admin_purchase_contacts c ON c.purchase_id=l.ref_id
                    WHERE """ + where + " ORDER BY l.ref_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )]
            for purchase in purchases:
                purchase["items"] = [dict(row) for row in conn.execute(
                    """SELECT o.id, o.price, COALESCE(o.product_name, p.name) AS name,
                              p.delivery_type, o.file_only_delivery,
                              o.customer_email_ciphertext IS NOT NULL AS has_email,
                              ec.completed_at AS email_completed_at
                       FROM orders o JOIN products p ON p.id=o.product_id
                       LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                       WHERE o.user_id=? AND o.id>=? AND (? IS NULL OR o.id<?)
                       ORDER BY o.id""",
                    (purchase["user_id"], purchase["id"],
                     purchase["next_id"], purchase["next_id"]),
                )]
                purchase.pop("next_id")
            return purchases

    def count_purchases_by_buyer(self, query: str) -> int:
        where, params = self._user_search_condition(query)
        with closing(self._connect()) as conn:
            conn.create_function("user_search_key", 1, self._user_search_key,
                                 deterministic=True)
            return conn.execute(
                """SELECT COUNT(*) FROM ledger l
                   JOIN users u ON u.telegram_id=l.user_id
                   WHERE l.kind='purchase' AND """ + where,
                params,
            ).fetchone()[0]

    def get_admin_purchase(self, purchase_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT l.ref_id AS id, l.user_id, -l.amount AS price, l.created_at,
                          u.username, u.display_name,
                          COALESCE(c.sent_count, 0) AS admin_contact_count,
                          c.last_sent_at AS admin_contact_last_sent_at,
                          (SELECT MIN(next.ref_id) FROM ledger next
                           WHERE next.kind='purchase' AND next.ref_id>l.ref_id) AS next_id
                   FROM ledger l JOIN users u ON u.telegram_id=l.user_id
                   LEFT JOIN admin_purchase_contacts c ON c.purchase_id=l.ref_id
                   WHERE l.kind='purchase' AND l.ref_id=?""", (purchase_id,),
            ).fetchone()
            if not row:
                return None
            purchase = dict(row)
            purchase["items"] = [dict(item) for item in conn.execute(
                """SELECT o.id, o.product_id, p.display_id, o.price,
                          p.delivery_type, o.file_only_delivery,
                          COALESCE(o.product_name, p.name) AS name,
                          o.customer_email_ciphertext IS NOT NULL AS has_email,
                          ec.completed_at AS email_completed_at
                   FROM orders o JOIN products p ON p.id=o.product_id
                   LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                   WHERE o.user_id=? AND o.id>=? AND (? IS NULL OR o.id<?)
                   ORDER BY o.id""",
                (purchase["user_id"], purchase_id,
                 purchase["next_id"], purchase["next_id"]),
            )]
            purchase.pop("next_id")
            return purchase if purchase["items"] else None

    def record_admin_purchase_contact(self, purchase_id: int) -> None:
        """Record a successfully delivered admin message or file for one purchase."""
        if type(purchase_id) is not int or purchase_id <= 0:
            raise ValueError("Invalid purchase ID")
        with self._write() as conn:
            purchase = conn.execute(
                "SELECT 1 FROM ledger WHERE kind='purchase' AND ref_id=?",
                (purchase_id,),
            ).fetchone()
            if not purchase:
                raise NotFound("Purchase not found")
            conn.execute(
                """INSERT INTO admin_purchase_contacts(purchase_id, sent_count, last_sent_at)
                   VALUES (?, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                   ON CONFLICT(purchase_id) DO UPDATE SET
                       sent_count=admin_purchase_contacts.sent_count + 1,
                       last_sent_at=excluded.last_sent_at""",
                (purchase_id,),
            )

    def list_all_orders(self, limit: int = 9, offset: int = 0):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT o.id, o.user_id, o.product_id, o.price, o.created_at,
                          o.bundle_order_id, b.bundle_name,
                          COALESCE(o.product_name, p.name) AS name, u.username, u.display_name,
                          o.customer_email_ciphertext IS NOT NULL AS has_email,
                          ec.completed_at AS email_completed_at
                   FROM orders o JOIN products p ON p.id=o.product_id
                   LEFT JOIN bundle_orders b ON b.id=o.bundle_order_id
                   LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                   JOIN users u ON u.telegram_id=o.user_id
                   ORDER BY o.id DESC LIMIT ? OFFSET ?""", (limit, offset)
            )]

    def get_order(self, user_id: int, order_id: int):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT o.id, o.price, o.created_at, o.bundle_order_id, b.bundle_name,
                          (SELECT MAX(l.ref_id) FROM ledger l
                           WHERE l.kind='purchase' AND l.ref_id<=o.id) AS purchase_id,
                          COALESCE(o.product_name, p.name) AS name, s.secret_ciphertext,
                          o.customer_email_ciphertext,
                          ec.completed_at AS email_completed_at FROM orders o
                   JOIN products p ON p.id=o.product_id JOIN stock s ON s.id=o.stock_id
                   LEFT JOIN bundle_orders b ON b.id=o.bundle_order_id
                   LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                   WHERE o.user_id=? AND o.id=?""", (user_id, order_id)
            ).fetchone()
            if not row:
                return None
            order = dict(row)
            order["secret"] = self.cipher.decrypt(
                order.pop("secret_ciphertext").encode("ascii")
            ).decode("utf-8")
            email_ciphertext = order.pop("customer_email_ciphertext")
            order["customer_email"] = (self.cipher.decrypt(
                email_ciphertext.encode("ascii")
            ).decode("utf-8") if email_ciphertext else None)
            return order

    def get_admin_order_contact(self, order_id: int):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT o.id, o.user_id, o.stock_id, o.price, o.created_at, o.bundle_order_id,
                          COALESCE(o.product_name, p.name) AS name,
                          o.customer_email_ciphertext, u.username, u.display_name,
                          ec.completed_at AS email_completed_at,
                          ec.admin_id AS email_completed_by,
                          ec.notification_status AS email_notification_status
                   FROM orders o JOIN products p ON p.id=o.product_id
                   JOIN users u ON u.telegram_id=o.user_id
                   LEFT JOIN email_order_completions ec ON ec.order_id=o.id
                   WHERE o.id=?""", (order_id,),
            ).fetchone()
            if not row:
                return None
            order = dict(row)
            email_ciphertext = order.pop("customer_email_ciphertext")
            order["customer_email"] = (self.cipher.decrypt(
                email_ciphertext.encode("ascii")
            ).decode("utf-8") if email_ciphertext else None)
            return order

    def complete_email_order(self, order_id: int, admin_id: int) -> dict:
        if type(order_id) is not int or order_id <= 0 or type(admin_id) is not int or admin_id <= 0:
            raise ValueError("Invalid order or admin ID")
        with self._write() as conn:
            order = conn.execute(
                """SELECT id, user_id FROM orders
                   WHERE id=? AND customer_email_ciphertext IS NOT NULL""",
                (order_id,),
            ).fetchone()
            if not order:
                raise NotFound("Email order not found")
            created = conn.execute(
                "INSERT OR IGNORE INTO email_order_completions(order_id, admin_id) VALUES (?, ?)",
                (order_id, admin_id),
            ).rowcount == 1
            completion = conn.execute(
                """SELECT completed_at, notification_status FROM email_order_completions
                   WHERE order_id=?""", (order_id,),
            ).fetchone()
            return {"order_id": order_id, "user_id": order["user_id"], "created": created,
                    "completed_at": completion["completed_at"],
                    "notification_status": completion["notification_status"]}

    def list_due_email_completion_notifications(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT ec.order_id, ec.attempts, o.user_id,
                          COALESCE(o.product_name, p.name) AS name
                   FROM email_order_completions ec
                   JOIN orders o ON o.id=ec.order_id
                   JOIN products p ON p.id=o.product_id
                   WHERE ec.notification_status='pending'
                     AND ec.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY ec.next_attempt_at, ec.order_id LIMIT ?""", (limit,),
            )]

    def finish_email_completion_notification(self, order_id: int, status: str) -> None:
        if status not in ("sent", "failed"):
            raise ValueError("Invalid email completion notification status")
        with self._write() as conn:
            conn.execute(
                """UPDATE email_order_completions
                   SET notification_status=?, attempts=attempts+1,
                       notified_at=CASE WHEN ?='sent' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                        ELSE NULL END
                   WHERE order_id=? AND notification_status='pending'""",
                (status, status, order_id),
            )

    def retry_email_completion_notification(self, order_id: int, delay_seconds: int) -> None:
        if delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._write() as conn:
            conn.execute(
                """UPDATE email_order_completions
                   SET attempts=attempts+1, next_attempt_at=?
                   WHERE order_id=? AND notification_status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 order_id),
            )

    def list_pending_email_orders(self, limit: int = 9, offset: int = 0) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT o.id, o.user_id, o.created_at,
                          COALESCE(o.product_name, p.name) AS name,
                          u.username, u.display_name
                   FROM orders o
                   JOIN products p ON p.id=o.product_id
                   JOIN users u ON u.telegram_id=o.user_id
                   WHERE o.customer_email_ciphertext IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM email_order_completions ec
                                     WHERE ec.order_id=o.id)
                   ORDER BY o.created_at, o.id LIMIT ? OFFSET ?""",
                (limit, offset),
            )]

    def count_pending_email_orders(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                """SELECT COUNT(*) FROM orders o
                   WHERE o.customer_email_ciphertext IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM email_order_completions ec
                                     WHERE ec.order_id=o.id)"""
            ).fetchone()[0]

    def get_email_reminder_minutes(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT delay_minutes FROM email_reminder_config WHERE id=1"
            ).fetchone()[0]

    def set_email_reminder_minutes(self, minutes: int) -> None:
        if type(minutes) is not int or (minutes != 0 and not 5 <= minutes <= 1440):
            raise ValueError("Email reminder must be off or 5-1440 minutes")
        with self._write() as conn:
            current = conn.execute(
                "SELECT delay_minutes FROM email_reminder_config WHERE id=1"
            ).fetchone()[0]
            if current != minutes:
                conn.execute(
                    "UPDATE email_reminder_config SET delay_minutes=? WHERE id=1",
                    (minutes,),
                )
                conn.execute("DELETE FROM email_reminder_state")

    def due_email_reminder(self, admin_id: int, now: datetime | None = None) -> dict | None:
        if type(admin_id) is not int or admin_id <= 0:
            raise ValueError("Invalid admin ID")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder time must include a timezone")
        now = now.astimezone(timezone.utc)
        with closing(self._connect()) as conn:
            minutes = conn.execute(
                "SELECT delay_minutes FROM email_reminder_config WHERE id=1"
            ).fetchone()[0]
            if not minutes:
                return None
            cutoff = (now - timedelta(minutes=minutes)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            summary = conn.execute(
                """SELECT COUNT(*) AS count, MAX(o.id) AS max_order_id,
                          MIN(o.created_at) AS oldest_at
                   FROM orders o
                   WHERE o.customer_email_ciphertext IS NOT NULL
                     AND o.created_at<=?
                     AND NOT EXISTS (SELECT 1 FROM email_order_completions ec
                                     WHERE ec.order_id=o.id)""",
                (cutoff,),
            ).fetchone()
            if not summary["count"]:
                return None
            state = conn.execute(
                "SELECT last_sent_at, last_max_order_id, attempts, next_attempt_at "
                "FROM email_reminder_state WHERE admin_id=?", (admin_id,),
            ).fetchone()
            attempts = state["attempts"] if state else 0
            if state:
                next_attempt = datetime.fromisoformat(
                    state["next_attempt_at"].replace("Z", "+00:00")
                )
                if attempts:
                    if now < next_attempt:
                        return None
                else:
                    last_sent = (datetime.fromisoformat(
                        state["last_sent_at"].replace("Z", "+00:00")
                    ) if state["last_sent_at"] else None)
                    new_order_due = (
                        summary["max_order_id"] > state["last_max_order_id"]
                        and (last_sent is None or now - last_sent >= timedelta(minutes=15))
                    )
                    if not new_order_due and now < next_attempt:
                        return None
            return {"count": summary["count"], "max_order_id": summary["max_order_id"],
                    "oldest_at": summary["oldest_at"], "minutes": minutes,
                    "attempts": attempts}

    def finish_email_reminder(self, admin_id: int, max_order_id: int,
                              now: datetime | None = None) -> None:
        if type(admin_id) is not int or admin_id <= 0 or type(max_order_id) is not int or max_order_id <= 0:
            raise ValueError("Invalid reminder")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder time must include a timezone")
        now = now.astimezone(timezone.utc)
        sent_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self._write() as conn:
            minutes = conn.execute(
                "SELECT delay_minutes FROM email_reminder_config WHERE id=1"
            ).fetchone()[0]
            next_at = (now + timedelta(minutes=max(60, minutes))).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            conn.execute(
                """INSERT INTO email_reminder_state(
                       admin_id, last_sent_at, last_max_order_id, attempts, next_attempt_at
                   ) VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(admin_id) DO UPDATE SET
                       last_sent_at=excluded.last_sent_at,
                       last_max_order_id=MAX(email_reminder_state.last_max_order_id,
                                             excluded.last_max_order_id),
                       attempts=0, next_attempt_at=excluded.next_attempt_at""",
                (admin_id, sent_at, max_order_id, next_at),
            )

    def retry_email_reminder(self, admin_id: int, delay_seconds: int,
                             now: datetime | None = None) -> None:
        if type(admin_id) is not int or admin_id <= 0 or type(delay_seconds) is not int or delay_seconds < 1:
            raise ValueError("Invalid reminder retry")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder time must include a timezone")
        next_at = (now.astimezone(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with self._write() as conn:
            conn.execute(
                """INSERT INTO email_reminder_state(admin_id, attempts, next_attempt_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(admin_id) DO UPDATE SET
                       attempts=email_reminder_state.attempts+1,
                       next_attempt_at=excluded.next_attempt_at""",
                (admin_id, next_at),
            )

    def create_warranty(self, user_id: int, order_id: int, note: str) -> dict:
        note = note.strip()
        if not 1 <= len(note) <= 1000:
            raise ValueError("Warranty note must have 1-1000 characters")
        with self._write() as conn:
            bounds = self._purchase_bounds_for_order(conn, user_id, order_id)
            if not bounds:
                raise NotFound("Order not found")
            purchase_id, next_id = bounds
            open_ticket = conn.execute(
                """SELECT id FROM warranty_requests
                   WHERE user_id=? AND status='open' AND order_id>=?
                     AND (? IS NULL OR order_id<?) LIMIT 1""",
                (user_id, purchase_id, next_id, next_id),
            ).fetchone()
            if open_ticket:
                raise WarrantyAlreadyOpen("Warranty request already open for this purchase")
            try:
                ticket_id = conn.execute(
                    "INSERT INTO warranty_requests(order_id, user_id, note) VALUES (?, ?, ?)",
                    (purchase_id, user_id,
                     self.cipher.encrypt(note.encode("utf-8")).decode("ascii")),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise WarrantyAlreadyOpen("Warranty request already open") from exc
            return {"id": ticket_id, "order_id": purchase_id,
                    "user_id": user_id, "note": note}

    @staticmethod
    def _purchase_bounds_for_order(conn, user_id: int,
                                   order_id: int) -> tuple[int, int | None] | None:
        """Resolve a legacy account item ID or purchase ID to its purchase range."""
        order = conn.execute(
            "SELECT id FROM orders WHERE id=? AND user_id=?", (order_id, user_id),
        ).fetchone()
        if not order:
            return None
        purchase = conn.execute(
            """SELECT l.ref_id AS id, l.user_id,
                      (SELECT MIN(next.ref_id) FROM ledger next
                       WHERE next.kind='purchase' AND next.ref_id>l.ref_id) AS next_id
               FROM ledger l WHERE l.kind='purchase' AND l.ref_id<=?
               ORDER BY l.ref_id DESC LIMIT 1""", (order_id,),
        ).fetchone()
        if (not purchase or purchase["user_id"] != user_id or
                (purchase["next_id"] is not None and order_id >= purchase["next_id"])):
            return None
        return purchase["id"], purchase["next_id"]

    def get_latest_warranty(self, user_id: int, order_id: int):
        with closing(self._connect()) as conn:
            bounds = self._purchase_bounds_for_order(conn, user_id, order_id)
            if not bounds:
                return None
            purchase_id, next_id = bounds
            row = conn.execute(
                """SELECT w.id, w.order_id, w.status, w.note, w.reply,
                          w.created_at, w.resolved_at,
                          (pr.ticket_id IS NOT NULL) AS resolved_privately
                   FROM warranty_requests w
                   LEFT JOIN warranty_private_resolutions pr ON pr.ticket_id=w.id
                   WHERE w.user_id=? AND w.order_id>=?
                     AND (? IS NULL OR w.order_id<?)
                   ORDER BY (w.status='open') DESC, w.id DESC LIMIT 1""",
                (user_id, purchase_id, next_id, next_id),
            ).fetchone()
            if not row:
                return None
            ticket = dict(row)
            ticket["purchase_id"] = purchase_id
            ticket["note"] = self.cipher.decrypt(ticket["note"].encode("ascii")).decode("utf-8")
            if ticket["reply"]:
                ticket["reply"] = self.cipher.decrypt(ticket["reply"].encode("ascii")).decode("utf-8")
            return ticket

    def list_warranties(self, status: str | None = "open", limit: int = 9, offset: int = 0):
        if status not in (None, "open", "resolved"):
            raise ValueError("Invalid warranty status")
        sql = """SELECT w.id, w.order_id, w.user_id, w.status,
                        (pr.ticket_id IS NOT NULL) AS resolved_privately,
                        (SELECT MAX(l.ref_id) FROM ledger l
                         WHERE l.kind='purchase' AND l.ref_id<=w.order_id) AS purchase_id,
                        w.created_at, w.resolved_at,
                        COALESCE(o.product_name, p.name) AS product_name,
                        u.username, u.display_name
                 FROM warranty_requests w JOIN orders o ON o.id=w.order_id
                 JOIN products p ON p.id=o.product_id
                 JOIN users u ON u.telegram_id=w.user_id
                 LEFT JOIN warranty_private_resolutions pr ON pr.ticket_id=w.id"""
        params = []
        if status:
            sql += " WHERE w.status=?"
            params.append(status)
        sql += (" ORDER BY w.created_at, w.id LIMIT ? OFFSET ?" if status == "open"
                else " ORDER BY w.id DESC LIMIT ? OFFSET ?")
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(sql, (*params, limit, offset))]

    def count_open_warranties(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM warranty_requests WHERE status='open'"
            ).fetchone()[0]

    def get_warranty_reminder_minutes(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT delay_minutes FROM warranty_reminder_config WHERE id=1"
            ).fetchone()[0]

    def set_warranty_reminder_minutes(self, minutes: int) -> None:
        if type(minutes) is not int or (minutes != 0 and not 5 <= minutes <= 1440):
            raise ValueError("Warranty reminder must be off or 5-1440 minutes")
        with self._write() as conn:
            current = conn.execute(
                "SELECT delay_minutes FROM warranty_reminder_config WHERE id=1"
            ).fetchone()[0]
            if current != minutes:
                conn.execute(
                    "UPDATE warranty_reminder_config SET delay_minutes=? WHERE id=1",
                    (minutes,),
                )
                conn.execute("DELETE FROM warranty_reminder_state")

    def due_warranty_reminder(self, admin_id: int, now: datetime | None = None) -> dict | None:
        if type(admin_id) is not int or admin_id <= 0:
            raise ValueError("Invalid admin ID")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder time must include a timezone")
        now = now.astimezone(timezone.utc)
        with closing(self._connect()) as conn:
            minutes = conn.execute(
                "SELECT delay_minutes FROM warranty_reminder_config WHERE id=1"
            ).fetchone()[0]
            if not minutes:
                return None
            cutoff = (now - timedelta(minutes=minutes)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            summary = conn.execute(
                """SELECT COUNT(*) AS count, MAX(id) AS max_ticket_id,
                          MIN(created_at) AS oldest_at
                   FROM warranty_requests
                   WHERE status='open' AND created_at<=?""",
                (cutoff,),
            ).fetchone()
            if not summary["count"]:
                return None
            state = conn.execute(
                "SELECT last_sent_at, last_max_ticket_id, attempts, next_attempt_at "
                "FROM warranty_reminder_state WHERE admin_id=?", (admin_id,),
            ).fetchone()
            attempts = state["attempts"] if state else 0
            if state:
                next_attempt = datetime.fromisoformat(
                    state["next_attempt_at"].replace("Z", "+00:00")
                )
                if attempts:
                    if now < next_attempt:
                        return None
                else:
                    last_sent = (datetime.fromisoformat(
                        state["last_sent_at"].replace("Z", "+00:00")
                    ) if state["last_sent_at"] else None)
                    new_ticket_due = (
                        summary["max_ticket_id"] > state["last_max_ticket_id"]
                        and (last_sent is None or now - last_sent >= timedelta(minutes=15))
                    )
                    if not new_ticket_due and now < next_attempt:
                        return None
            return {"count": summary["count"], "max_ticket_id": summary["max_ticket_id"],
                    "oldest_at": summary["oldest_at"], "minutes": minutes,
                    "attempts": attempts}

    def finish_warranty_reminder(self, admin_id: int, max_ticket_id: int,
                                 now: datetime | None = None) -> None:
        if (type(admin_id) is not int or admin_id <= 0 or
                type(max_ticket_id) is not int or max_ticket_id <= 0):
            raise ValueError("Invalid reminder")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder time must include a timezone")
        now = now.astimezone(timezone.utc)
        sent_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self._write() as conn:
            minutes = conn.execute(
                "SELECT delay_minutes FROM warranty_reminder_config WHERE id=1"
            ).fetchone()[0]
            next_at = (now + timedelta(minutes=max(60, minutes))).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            conn.execute(
                """INSERT INTO warranty_reminder_state(
                       admin_id, last_sent_at, last_max_ticket_id, attempts, next_attempt_at
                   ) VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(admin_id) DO UPDATE SET
                       last_sent_at=excluded.last_sent_at,
                       last_max_ticket_id=MAX(warranty_reminder_state.last_max_ticket_id,
                                              excluded.last_max_ticket_id),
                       attempts=0, next_attempt_at=excluded.next_attempt_at""",
                (admin_id, sent_at, max_ticket_id, next_at),
            )

    def retry_warranty_reminder(self, admin_id: int, delay_seconds: int,
                                now: datetime | None = None) -> None:
        if (type(admin_id) is not int or admin_id <= 0 or
                type(delay_seconds) is not int or delay_seconds < 1):
            raise ValueError("Invalid reminder retry")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Reminder time must include a timezone")
        next_at = (now.astimezone(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with self._write() as conn:
            conn.execute(
                """INSERT INTO warranty_reminder_state(admin_id, attempts, next_attempt_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(admin_id) DO UPDATE SET
                       attempts=warranty_reminder_state.attempts+1,
                       next_attempt_at=excluded.next_attempt_at""",
                (admin_id, next_at),
            )

    def get_warranty(self, ticket_id: int):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT w.id, w.order_id, w.user_id, w.note, w.status, w.reply,
                          (pr.ticket_id IS NOT NULL) AS resolved_privately,
                          pr.admin_id AS privately_resolved_by,
                          (SELECT MAX(l.ref_id) FROM ledger l
                           WHERE l.kind='purchase' AND l.ref_id<=w.order_id) AS purchase_id,
                          w.created_at, w.resolved_at,
                          COALESCE(o.product_name, p.name) AS product_name,
                          u.username, u.display_name
                   FROM warranty_requests w JOIN orders o ON o.id=w.order_id
                   JOIN products p ON p.id=o.product_id
                   JOIN users u ON u.telegram_id=w.user_id
                   LEFT JOIN warranty_private_resolutions pr ON pr.ticket_id=w.id
                   WHERE w.id=?""", (ticket_id,)
            ).fetchone()
            if not row:
                return None
            ticket = dict(row)
            ticket["note"] = self.cipher.decrypt(ticket["note"].encode("ascii")).decode("utf-8")
            if ticket["reply"]:
                ticket["reply"] = self.cipher.decrypt(ticket["reply"].encode("ascii")).decode("utf-8")
            return ticket

    def resolve_warranty(self, ticket_id: int, reply: str) -> dict:
        reply = reply.strip()
        if not 1 <= len(reply) <= 1000:
            raise ValueError("Warranty reply must have 1-1000 characters")
        with self._write() as conn:
            ticket = conn.execute(
                "SELECT id, order_id, user_id, status FROM warranty_requests WHERE id=?",
                (ticket_id,),
            ).fetchone()
            if not ticket:
                raise NotFound("Warranty request not found")
            if ticket["status"] != "open":
                raise WarrantyClosed("Warranty request already resolved")
            conn.execute(
                """UPDATE warranty_requests SET status='resolved', reply=?,
                   resolved_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?""",
                (self.cipher.encrypt(reply.encode("utf-8")).decode("ascii"), ticket_id),
            )
            bounds = self._purchase_bounds_for_order(
                conn, ticket["user_id"], ticket["order_id"],
            )
            return {"id": ticket_id, "order_id": bounds[0] if bounds else ticket["order_id"],
                    "user_id": ticket["user_id"], "reply": reply}

    def resolve_warranty_privately(self, ticket_id: int, admin_id: int) -> None:
        """Close an outside-bot warranty resolution without a customer reply."""
        if (type(ticket_id) is not int or ticket_id <= 0 or
                type(admin_id) is not int or admin_id <= 0):
            raise ValueError("Invalid warranty or admin ID")
        with self._write() as conn:
            ticket = conn.execute(
                "SELECT status FROM warranty_requests WHERE id=?", (ticket_id,),
            ).fetchone()
            if not ticket:
                raise NotFound("Warranty request not found")
            if ticket["status"] != "open":
                raise WarrantyClosed("Warranty request already resolved")
            conn.execute(
                """UPDATE warranty_requests SET status='resolved', reply=NULL,
                   resolved_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?""",
                (ticket_id,),
            )
            conn.execute(
                """INSERT INTO warranty_private_resolutions(ticket_id, admin_id, resolved_at)
                   SELECT id, ?, resolved_at FROM warranty_requests WHERE id=?""",
                (admin_id, ticket_id),
            )

    def daily_report(self, day: date) -> dict:
        start = datetime.combine(day, datetime.min.time(), SHOP_TIMEZONE)
        start_utc = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        end_utc = (start + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with closing(self._connect()) as conn:
            topup_total = conn.execute(
                """SELECT COALESCE(SUM(amount), 0) FROM ledger
                   WHERE kind='topup' AND created_at>=? AND created_at<?""",
                (start_utc, end_utc),
            ).fetchone()[0]
            sales = conn.execute(
                """SELECT COALESCE(SUM(o.price), 0) AS revenue,
                          COUNT(DISTINCT (
                              SELECT MAX(l.ref_id) FROM ledger l
                              WHERE l.kind='purchase' AND l.ref_id<=o.id
                          )) AS order_count
                   FROM orders o WHERE o.created_at>=? AND o.created_at<?""",
                (start_utc, end_utc),
            ).fetchone()
            return {"date": day.isoformat(), "topups": topup_total,
                    "revenue": sales["revenue"], "orders": sales["order_count"]}

    def export_report_rows(self, start_utc: str, end_utc: str,
                           max_rows: int = 50_000) -> dict[str, list[dict]]:
        if not start_utc or not end_utc or start_utc >= end_utc or max_rows <= 0:
            raise ValueError("Invalid export range")
        reports = {
            "topups": """SELECT l.created_at, l.id AS ledger_id, l.user_id,
                                u.username, u.display_name, l.amount,
                                e.id AS bank_event_id, e.provider_txn_id, t.code,
                                CASE WHEN m.bank_event_id IS NULL THEN 'automatic'
                                     ELSE 'manual' END AS credit_method
                         FROM ledger l
                         LEFT JOIN users u ON u.telegram_id=l.user_id
                         LEFT JOIN bank_events e ON e.id=l.ref_id
                         LEFT JOIN topups t ON t.id=e.topup_id
                         LEFT JOIN manual_credit_resolutions m ON m.bank_event_id=e.id
                         WHERE l.kind='topup' AND l.created_at>=? AND l.created_at<?
                         ORDER BY l.created_at, l.id LIMIT ?""",
            "orders": """SELECT o.created_at, o.id AS order_id,
                                (SELECT MAX(l.ref_id) FROM ledger l
                                 WHERE l.kind='purchase' AND l.ref_id<=o.id) AS purchase_id,
                                o.user_id,
                                u.username, u.display_name, o.product_id,
                                COALESCE(o.product_name, p.name) AS product_name,
                                o.price, o.bundle_order_id, b.bundle_name,
                                COALESCE(r.code, b.coupon_code) AS coupon_code
                         FROM orders o
                         JOIN users u ON u.telegram_id=o.user_id
                         JOIN products p ON p.id=o.product_id
                         LEFT JOIN bundle_orders b ON b.id=o.bundle_order_id
                         LEFT JOIN coupon_redemptions r ON r.order_id=o.id
                         WHERE o.created_at>=? AND o.created_at<?
                         ORDER BY o.created_at, o.id LIMIT ?""",
            "manual_credits": """SELECT m.created_at, m.bank_event_id,
                                        e.provider_txn_id, m.user_id,
                                        u.username, u.display_name, t.code,
                                        m.admin_id, m.amount
                                 FROM manual_credit_resolutions m
                                 JOIN bank_events e ON e.id=m.bank_event_id
                                 JOIN topups t ON t.id=m.topup_id
                                 JOIN users u ON u.telegram_id=m.user_id
                                 WHERE m.created_at>=? AND m.created_at<?
                                 ORDER BY m.created_at, m.bank_event_id LIMIT ?""",
        }
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            result = {}
            remaining = max_rows
            for name, sql in reports.items():
                rows = [dict(row) for row in conn.execute(
                    sql, (start_utc, end_utc, remaining + 1),
                )]
                if len(rows) > remaining:
                    raise ExportTooLarge("Report contains too many rows")
                result[name] = rows
                remaining -= len(rows)
            return result

    def export_users_rows(self, exclude_ids: frozenset[int] = frozenset(),
                          max_rows: int = 50_000) -> list[dict]:
        """All known customers with wallet and purchase totals; never stock secrets."""
        if type(max_rows) is not int or max_rows <= 0:
            raise ValueError("Invalid export row limit")
        excluded = tuple(sorted(exclude_ids))
        where = ("WHERE u.telegram_id NOT IN (" + ",".join("?" for _ in excluded) + ")"
                 if excluded else "")
        with closing(self._connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                """WITH ledger_totals AS (
                       SELECT user_id,
                              SUM(CASE WHEN kind='topup' THEN amount ELSE 0 END) AS received,
                              SUM(CASE WHEN kind='purchase' THEN -amount ELSE 0 END) AS spent,
                              SUM(CASE WHEN kind='purchase' THEN 1 ELSE 0 END) AS orders
                       FROM ledger GROUP BY user_id
                   ), test_totals AS (
                       SELECT user_id, SUM(amount) AS test_credit
                       FROM admin_test_credits GROUP BY user_id
                   )
                   SELECT u.telegram_id, u.username, u.display_name, u.created_at,
                          u.balance, COALESCE(l.received, 0) AS received,
                          COALESCE(t.test_credit, 0) AS test_credit,
                          COALESCE(l.spent, 0) AS spent,
                          COALESCE(l.orders, 0) AS orders
                   FROM users u
                   LEFT JOIN ledger_totals l ON l.user_id=u.telegram_id
                   LEFT JOIN test_totals t ON t.user_id=u.telegram_id
                   """ + where +
                " ORDER BY u.created_at, u.telegram_id LIMIT ?",
                (*excluded, max_rows + 1),
            )]
        if len(rows) > max_rows:
            raise ExportTooLarge("Customer export contains too many rows")
        return rows

    def export_sold_product_rows(self, max_rows: int = 50_000) -> list[dict]:
        """One row per purchase and product, with historical name and summed price."""
        if type(max_rows) is not int or max_rows <= 0:
            raise ValueError("Invalid export row limit")
        with closing(self._connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                """WITH sold_items AS (
                       SELECT o.created_at, o.user_id, o.product_id,
                              COALESCE(o.product_name, p.name) AS product_name,
                              p.display_id AS current_display_id, o.price,
                              (SELECT MAX(l.ref_id) FROM ledger l
                               WHERE l.kind='purchase' AND l.ref_id<=o.id) AS purchase_id
                       FROM orders o JOIN products p ON p.id=o.product_id
                   )
                   SELECT MIN(s.created_at) AS created_at, s.purchase_id, s.user_id,
                          u.username, u.display_name, s.product_id,
                          s.current_display_id, s.product_name,
                          COUNT(*) AS quantity, SUM(s.price) AS total_price,
                          CASE WHEN MIN(s.price)=MAX(s.price) THEN MIN(s.price)
                               ELSE NULL END AS unit_price
                   FROM sold_items s JOIN users u ON u.telegram_id=s.user_id
                   WHERE s.purchase_id IS NOT NULL
                   GROUP BY s.purchase_id, s.user_id, s.product_id, s.product_name
                   ORDER BY s.purchase_id DESC, s.product_id LIMIT ?""",
                (max_rows + 1,),
            )]
        if len(rows) > max_rows:
            raise ExportTooLarge("Sold product export contains too many rows")
        return rows

    @staticmethod
    def _cart_rows(conn, user_id: int, checkout_id: int | None = None) -> list[dict]:
        if checkout_id is None:
            rows = conn.execute(
                """SELECT product_id, quantity, email_ciphertext FROM cart_items
                   WHERE user_id=? ORDER BY product_id""", (user_id,),
            )
        else:
            rows = conn.execute(
                """SELECT product_id, quantity, email_ciphertext FROM direct_checkout_items
                   WHERE checkout_id=? ORDER BY product_id""", (checkout_id,),
            )
        return [dict(row) for row in rows]

    def _quote_cart_conn(self, conn, user_id: int, rows: list[dict],
                         coupon_code: str | None, require_emails: bool = False) -> dict:
        if not rows or sum(row["quantity"] for row in rows) > MAX_CART_UNITS:
            raise ValueError("Cart is empty or too large")
        lines = []
        missing_email = []
        for row in rows:
            product = conn.execute(
                "SELECT * FROM products WHERE id=? AND active=1 AND deleted=0",
                (row["product_id"],),
            ).fetchone()
            if not product:
                raise NotFound("Cart product is unavailable")
            quantity = row["quantity"]
            available = conn.execute(
                "SELECT COUNT(*) FROM stock WHERE product_id=? AND sold_at IS NULL",
                (product["id"],),
            ).fetchone()[0]
            if quantity > available:
                raise OutOfStock("Cart product is out of stock")
            sale = self._active_sale(conn, product["id"])
            sale_units = min(quantity, sale["max_units"] - sale["sold_units"]) if sale else 0
            unit_prices = ([sale["sale_price"]] * sale_units if sale else []) + [
                product["price"]
            ] * (quantity - sale_units)
            emails = [None] * quantity
            if product["requires_email"]:
                if row["email_ciphertext"]:
                    raw_email = self.cipher.decrypt(
                        row["email_ciphertext"].encode("ascii")
                    ).decode("utf-8")
                    normalized = normalize_customer_email(raw_email)
                    emails = normalized.split(", ")
                    if len(emails) != quantity:
                        raise EmailRequired("Email quantity changed")
                else:
                    missing_email.append(product["id"])
            lines.append({
                "product_id": product["id"], "display_id": product["display_id"] or product["id"],
                "name": product["name"], "delivery_type": product["delivery_type"],
                "file_only_delivery": product["file_only_delivery"],
                "quantity": quantity,
                "unit_prices": unit_prices, "sale_id": sale["id"] if sale else None,
                "sale_units": sale_units, "emails": emails,
                "requires_email": bool(product["requires_email"]),
                "email_ready": not product["requires_email"] or bool(row["email_ciphertext"]),
            })
        if missing_email and require_emails:
            raise EmailRequired("Cart email required")
        subtotal = sum(sum(line["unit_prices"]) for line in lines)
        discount = 0
        code = coupon_code.strip().upper() if coupon_code else None
        coupon_target_product_id = None
        if code:
            if any(line["sale_units"] for line in lines):
                raise SaleCouponConflict("Coupon cannot be combined with a sale")
            coupon = conn.execute("SELECT product_id FROM coupons WHERE code=?", (code,)).fetchone()
            if coupon and coupon["product_id"] is not None:
                coupon_target_product_id = coupon["product_id"]
                selected = next((line for line in lines
                                 if line["product_id"] == coupon["product_id"]), None)
                if selected is None:
                    raise CouponUnavailable("Coupon product is not in cart")
                line_total = sum(selected["unit_prices"])
                reduced, discount, code = self._apply_coupon(
                    conn, user_id, line_total, code, min_price=selected["quantity"],
                    target_product_id=selected["product_id"],
                )
                each, remainder = divmod(reduced, selected["quantity"])
                selected["unit_prices"] = [each + (index < remainder)
                                           for index in range(selected["quantity"])]
            else:
                reduced, discount, code = self._apply_coupon(
                    conn, user_id, subtotal, code,
                    min_price=sum(line["quantity"] for line in lines),
                )
                quantity = sum(line["quantity"] for line in lines)
                each, remainder = divmod(reduced, quantity)
                index = 0
                for line in lines:
                    line["unit_prices"] = []
                    for _ in range(line["quantity"]):
                        line["unit_prices"].append(each + (index < remainder))
                        index += 1
        return {
            "items": lines, "quantity": sum(line["quantity"] for line in lines),
            "subtotal": subtotal, "price": subtotal - discount,
            "discount": discount, "coupon_code": code,
            "coupon_target_product_id": coupon_target_product_id,
            "missing_email": missing_email,
        }

    def cart_quote(self, user_id: int) -> dict:
        with closing(self._connect()) as conn:
            rows = self._cart_rows(conn, user_id)
            setting = conn.execute(
                "SELECT coupon_code FROM shopping_carts WHERE user_id=?", (user_id,),
            ).fetchone()
            return self._quote_cart_conn(
                conn, user_id, rows, setting["coupon_code"] if setting else None,
            )

    def _single_product_row(self, product_id: int, quantity: int,
                            customer_email: str | None) -> dict:
        if (type(product_id) is not int or product_id <= 0
                or type(quantity) is not int or not 1 <= quantity <= MAX_CART_UNITS):
            raise ValueError("Invalid purchase quantity")
        encrypted_email = None
        if customer_email is not None:
            normalized = normalize_customer_email(customer_email)
            if len(normalized.split(", ")) != quantity:
                raise EmailRequired("Email count must match quantity")
            encrypted_email = self.cipher.encrypt(normalized.encode("utf-8")).decode("ascii")
        return {"product_id": product_id, "quantity": quantity,
                "email_ciphertext": encrypted_email}

    def quote_product_quantity(self, user_id: int, product_id: int, quantity: int,
                               coupon_code: str | None = None,
                               customer_email: str | None = None) -> dict:
        row = self._single_product_row(product_id, quantity, customer_email)
        with closing(self._connect()) as conn:
            return self._quote_cart_conn(conn, user_id, [row], coupon_code)

    def purchase_product_quantity(
        self, user_id: int, product_id: int, quantity: int, expected_price: int,
        coupon_code: str | None = None, customer_email: str | None = None,
        notify_admin_ids: frozenset[int] = frozenset(),
    ) -> dict:
        row = self._single_product_row(product_id, quantity, customer_email)
        with self._write() as conn:
            quote = self._quote_cart_conn(
                conn, user_id, [row], coupon_code, require_emails=True,
            )
            if quote["price"] != expected_price:
                raise PriceChanged("Product price changed")
            return self._purchase_cart_conn(conn, user_id, quote, notify_admin_ids)

    def cart_items_view(self, user_id: int) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT ci.product_id, ci.quantity,
                          ci.email_ciphertext IS NOT NULL AS email_ready,
                          p.display_id, p.name, p.price, p.active, p.deleted,
                          p.requires_email,
                          (SELECT COUNT(*) FROM stock s
                           WHERE s.product_id=ci.product_id AND s.sold_at IS NULL) AS available
                   FROM cart_items ci JOIN products p ON p.id=ci.product_id
                   WHERE ci.user_id=? ORDER BY p.display_id, p.id""", (user_id,),
            )]

    def cart_add(self, user_id: int, product_id: int) -> None:
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
            product = conn.execute(
                "SELECT 1 FROM products WHERE id=? AND active=1 AND deleted=0", (product_id,),
            ).fetchone()
            if not product:
                raise NotFound("Product unavailable")
            current = conn.execute(
                "SELECT quantity FROM cart_items WHERE user_id=? AND product_id=?",
                (user_id, product_id),
            ).fetchone()
            next_quantity = (current["quantity"] if current else 0) + 1
            total = conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM cart_items WHERE user_id=?", (user_id,),
            ).fetchone()[0]
            if total >= MAX_CART_UNITS:
                raise ValueError("Cart is full")
            available = conn.execute(
                "SELECT COUNT(*) FROM stock WHERE product_id=? AND sold_at IS NULL", (product_id,),
            ).fetchone()[0]
            if available < next_quantity:
                raise OutOfStock("Not enough stock")
            conn.execute(
                """INSERT INTO cart_items(user_id, product_id, quantity) VALUES (?, ?, 1)
                   ON CONFLICT(user_id, product_id) DO UPDATE SET
                   quantity=quantity+1, email_ciphertext=NULL""",
                (user_id, product_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO shopping_carts(user_id) VALUES (?)", (user_id,),
            )
            conn.execute("UPDATE shopping_carts SET coupon_code=NULL WHERE user_id=?", (user_id,))

    def cart_decrement(self, user_id: int, product_id: int) -> None:
        with self._write() as conn:
            row = conn.execute(
                "SELECT quantity FROM cart_items WHERE user_id=? AND product_id=?",
                (user_id, product_id),
            ).fetchone()
            if not row:
                raise NotFound("Cart item not found")
            if row["quantity"] == 1:
                conn.execute(
                    "DELETE FROM cart_items WHERE user_id=? AND product_id=?",
                    (user_id, product_id),
                )
            else:
                conn.execute(
                    """UPDATE cart_items SET quantity=quantity-1, email_ciphertext=NULL
                       WHERE user_id=? AND product_id=?""", (user_id, product_id),
                )
            conn.execute("UPDATE shopping_carts SET coupon_code=NULL WHERE user_id=?", (user_id,))

    def cart_remove(self, user_id: int, product_id: int) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM cart_items WHERE user_id=? AND product_id=?", (user_id, product_id),
            )
            conn.execute("UPDATE shopping_carts SET coupon_code=NULL WHERE user_id=?", (user_id,))

    def cart_set_email(self, user_id: int, product_id: int, value: str) -> None:
        normalized = normalize_customer_email(value)
        with self._write() as conn:
            item = conn.execute(
                """SELECT ci.quantity, p.requires_email FROM cart_items ci
                   JOIN products p ON p.id=ci.product_id
                   WHERE ci.user_id=? AND ci.product_id=?""", (user_id, product_id),
            ).fetchone()
            if not item or not item["requires_email"]:
                raise NotFound("Email is not needed for this cart item")
            if len(normalized.split(", ")) != item["quantity"]:
                raise ValueError("Email count must match quantity")
            ciphertext = self.cipher.encrypt(normalized.encode("utf-8")).decode("ascii")
            conn.execute(
                """UPDATE cart_items SET email_ciphertext=?
                   WHERE user_id=? AND product_id=?""",
                (ciphertext, user_id, product_id),
            )

    def cart_set_coupon(self, user_id: int, code: str | None) -> dict:
        normalized = code.strip().upper() if code else None
        with self._write() as conn:
            if not self._cart_rows(conn, user_id):
                raise ValueError("Cart is empty")
            conn.execute(
                "INSERT OR IGNORE INTO shopping_carts(user_id) VALUES (?)", (user_id,),
            )
            conn.execute(
                "UPDATE shopping_carts SET coupon_code=? WHERE user_id=?",
                (normalized, user_id),
            )
            return self._quote_cart_conn(conn, user_id, self._cart_rows(conn, user_id), normalized)

    def cart_clear_coupon(self, user_id: int) -> None:
        with self._write() as conn:
            conn.execute("UPDATE shopping_carts SET coupon_code=NULL WHERE user_id=?", (user_id,))

    def _purchase_cart_conn(self, conn, user_id: int, quote: dict,
                            notify_admin_ids: frozenset[int],
                            checkout_id: int | None = None) -> dict:
        user = conn.execute("SELECT balance FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        if not user or user["balance"] < quote["price"]:
            raise InsufficientBalance("Insufficient balance")
        selected = []
        for line in quote["items"]:
            stocks = conn.execute(
                """SELECT id, secret_ciphertext FROM stock
                   WHERE product_id=? AND sold_at IS NULL ORDER BY id LIMIT ?""",
                (line["product_id"], line["quantity"]),
            ).fetchall()
            if len(stocks) != line["quantity"]:
                raise OutOfStock("Cart stock changed")
            for index, stock in enumerate(stocks):
                secret = self.cipher.decrypt(
                    stock["secret_ciphertext"].encode("ascii")
                ).decode("utf-8")
                email = line["emails"][index]
                email_ciphertext = (self.cipher.encrypt(email.encode("utf-8")).decode("ascii")
                                    if email else None)
                selected.append((line, index, stock["id"], secret, email, email_ciphertext))
        orders = []
        for line, index, stock_id, secret, email, email_ciphertext in selected:
            conn.execute(
                "UPDATE stock SET sold_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                (stock_id,),
            )
            order_id = conn.execute(
                """INSERT INTO orders(user_id, product_id, stock_id, price, product_name,
                                      sale_id, customer_email_ciphertext, file_only_delivery)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, line["product_id"], stock_id, line["unit_prices"][index],
                 line["name"], line["sale_id"] if index < line["sale_units"] else None,
                 email_ciphertext, line["file_only_delivery"]),
            ).lastrowid
            orders.append({"id": order_id, "product_id": line["product_id"],
                           "name": line["name"], "delivery_type": line["delivery_type"],
                           "file_only_delivery": line["file_only_delivery"],
                           "secret": secret,
                           "price": line["unit_prices"][index], "customer_email": email})
            if checkout_id is not None:
                conn.execute(
                    "INSERT INTO direct_checkout_orders(checkout_id, order_id) VALUES (?, ?)",
                    (checkout_id, order_id),
                )
        self._queue_purchase_notifications(
            conn, "product", orders[0]["id"], notify_admin_ids,
        )
        for line in quote["items"]:
            if line["sale_units"]:
                conn.execute(
                    "UPDATE product_sales SET sold_units=sold_units+? WHERE id=?",
                    (line["sale_units"], line["sale_id"]),
                )
                sale = conn.execute(
                    "SELECT sold_units, max_units FROM product_sales WHERE id=?", (line["sale_id"],),
                ).fetchone()
                if sale["sold_units"] == sale["max_units"]:
                    self._close_sale(conn, line["product_id"])
        conn.execute("UPDATE users SET balance=balance-? WHERE telegram_id=?",
                     (quote["price"], user_id))
        conn.execute(
            "INSERT INTO ledger(user_id, amount, kind, ref_id) VALUES (?, ?, 'purchase', ?)",
            (user_id, -quote["price"], orders[0]["id"]),
        )
        if quote["coupon_code"]:
            coupon_order = next(
                (order for order in orders
                 if order["product_id"] == quote["coupon_target_product_id"]),
                orders[0],
            )
            conn.execute(
                "INSERT INTO coupon_redemptions(code, user_id, order_id) VALUES (?, ?, ?)",
                (quote["coupon_code"], user_id, coupon_order["id"]),
            )
        return {"orders": orders, "price": quote["price"],
                "balance": user["balance"] - quote["price"],
                "coupon_code": quote["coupon_code"]}

    def purchase_cart(self, user_id: int, expected_price: int,
                      notify_admin_ids: frozenset[int] = frozenset()) -> dict:
        with self._write() as conn:
            rows = self._cart_rows(conn, user_id)
            setting = conn.execute(
                "SELECT coupon_code FROM shopping_carts WHERE user_id=?", (user_id,),
            ).fetchone()
            quote = self._quote_cart_conn(
                conn, user_id, rows, setting["coupon_code"] if setting else None,
                require_emails=True,
            )
            if quote["price"] != expected_price:
                raise PriceChanged("Cart price changed")
            purchase = self._purchase_cart_conn(conn, user_id, quote, notify_admin_ids)
            conn.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM shopping_carts WHERE user_id=?", (user_id,))
            return purchase

    def create_direct_checkout(self, user_id: int, max_amount: int) -> dict:
        with self._write() as conn:
            rows = self._cart_rows(conn, user_id)
            setting = conn.execute(
                "SELECT coupon_code FROM shopping_carts WHERE user_id=?", (user_id,),
            ).fetchone()
            quote = self._quote_cart_conn(
                conn, user_id, rows, setting["coupon_code"] if setting else None,
                require_emails=True,
            )
            checkout = self._create_direct_checkout_conn(
                conn, user_id, rows, quote, max_amount,
            )
            conn.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM shopping_carts WHERE user_id=?", (user_id,))
            return checkout

    def create_direct_checkout_product(
        self, user_id: int, product_id: int, quantity: int, expected_price: int,
        max_amount: int, coupon_code: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        row = self._single_product_row(product_id, quantity, customer_email)
        with self._write() as conn:
            quote = self._quote_cart_conn(
                conn, user_id, [row], coupon_code, require_emails=True,
            )
            if quote["price"] != expected_price:
                raise PriceChanged("Product price changed")
            return self._create_direct_checkout_conn(
                conn, user_id, [row], quote, max_amount,
            )

    def _create_direct_checkout_conn(self, conn, user_id: int, rows: list[dict],
                                     quote: dict, max_amount: int) -> dict:
        if quote["price"] > max_amount:
            raise ValueError("Checkout amount is above the bank payment limit")
        recent = conn.execute(
            """SELECT COUNT(*) FROM direct_checkouts WHERE user_id=?
               AND created_at>=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour')""",
            (user_id,),
        ).fetchone()[0]
        if recent >= 12:
            raise TopupLimitReached("Too many direct checkouts")
        topup_id = None
        code = None
        for _ in range(3):
            code = "MUA" + secrets.token_hex(6).upper()
            try:
                topup_id = conn.execute(
                    "INSERT INTO topups(user_id, code, requested_amount) VALUES (?, ?, ?)",
                    (user_id, code, quote["price"]),
                ).lastrowid
                break
            except sqlite3.IntegrityError:
                continue
        if topup_id is None:
            raise ShopError("Could not generate checkout code")
        checkout_id = conn.execute(
            """INSERT INTO direct_checkouts(user_id, topup_id, expected_amount,
                                            coupon_code, expires_at)
               VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+30 minutes'))""",
            (user_id, topup_id, quote["price"], quote["coupon_code"]),
        ).lastrowid
        conn.executemany(
            """INSERT INTO direct_checkout_items
               (checkout_id, product_id, quantity, email_ciphertext) VALUES (?, ?, ?, ?)""",
            ((checkout_id, row["product_id"], row["quantity"], row["email_ciphertext"])
             for row in rows),
        )
        return {"id": checkout_id, "topup_id": topup_id, "code": code,
                "amount": quote["price"], "items": quote["items"],
                "expires_minutes": DIRECT_CHECKOUT_MINUTES}

    def get_direct_checkout(self, user_id: int, checkout_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT c.*, t.code, t.paid_amount FROM direct_checkouts c
                   JOIN topups t ON t.id=c.topup_id WHERE c.id=? AND c.user_id=?""",
                (checkout_id, user_id),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["items"] = [dict(item) for item in conn.execute(
                """SELECT p.display_id, p.name, i.quantity FROM direct_checkout_items i
                   JOIN products p ON p.id=i.product_id WHERE i.checkout_id=?
                   ORDER BY p.display_id, p.id""", (checkout_id,),
            )]
            if result["status"] == "open" and result["expires_at"] < datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ"):
                result["status"] = "expired"
            return result

    def recent_direct_checkouts(self, user_id: int, limit: int = 3) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT c.id, c.expected_amount,
                          CASE WHEN c.status='open'
                                 AND c.expires_at<strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                               THEN 'expired' ELSE c.status END AS status,
                          c.expires_at,
                          t.code, t.paid_amount FROM direct_checkouts c
                   JOIN topups t ON t.id=c.topup_id
                   WHERE c.user_id=? ORDER BY c.id DESC LIMIT ?""", (user_id, limit),
            )]

    def list_due_direct_checkout_orders(self, limit: int = 20) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                """SELECT d.checkout_id, d.order_id, d.attempts, o.user_id,
                          o.product_name, o.customer_email_ciphertext, s.secret_ciphertext
                   FROM direct_checkout_orders d
                   JOIN orders o ON o.id=d.order_id JOIN stock s ON s.id=o.stock_id
                   WHERE d.status='pending'
                     AND d.next_attempt_at<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY d.order_id LIMIT ?""", (limit,),
            )]
            for row in rows:
                row["secret"] = self.cipher.decrypt(
                    row.pop("secret_ciphertext").encode("ascii")
                ).decode("utf-8")
                encrypted_email = row.pop("customer_email_ciphertext")
                row["customer_email"] = (self.cipher.decrypt(
                    encrypted_email.encode("ascii")
                ).decode("utf-8") if encrypted_email else None)
            return rows

    def list_due_direct_checkout_batches(self, limit: int = 20) -> list[dict]:
        """Return complete pending purchases, grouped by bank checkout."""
        if type(limit) is not int or limit < 1:
            raise ValueError("Invalid delivery batch limit")
        with closing(self._connect()) as conn:
            checkouts = [dict(row) for row in conn.execute(
                """SELECT d.checkout_id, MIN(d.order_id) AS first_order_id,
                          state.message_sent_at, state.file_sent_at
                   FROM direct_checkout_orders d
                   LEFT JOIN direct_checkout_delivery_state state
                     ON state.checkout_id=d.checkout_id
                   WHERE d.status='pending'
                   GROUP BY d.checkout_id
                   HAVING MAX(d.next_attempt_at)<=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   ORDER BY first_order_id LIMIT ?""", (limit,),
            )]
            for batch in checkouts:
                rows = [dict(row) for row in conn.execute(
                    """SELECT d.order_id AS id, d.attempts, o.user_id,
                              o.product_name AS name, p.delivery_type,
                              o.file_only_delivery,
                              o.customer_email_ciphertext,
                              s.secret_ciphertext
                       FROM direct_checkout_orders d
                       JOIN orders o ON o.id=d.order_id
                       JOIN products p ON p.id=o.product_id
                       JOIN stock s ON s.id=o.stock_id
                       WHERE d.checkout_id=? AND d.status='pending'
                       ORDER BY d.order_id""", (batch["checkout_id"],),
                )]
                for row in rows:
                    row["secret"] = self.cipher.decrypt(
                        row.pop("secret_ciphertext").encode("ascii")
                    ).decode("utf-8")
                    encrypted_email = row.pop("customer_email_ciphertext")
                    row["customer_email"] = (self.cipher.decrypt(
                        encrypted_email.encode("ascii")
                    ).decode("utf-8") if encrypted_email else None)
                batch["orders"] = rows
                batch["user_id"] = rows[0]["user_id"]
            return checkouts

    def mark_direct_checkout_delivery_step(self, checkout_id: int, step: str) -> None:
        if step not in ("message", "file"):
            raise ValueError("Invalid delivery step")
        column = "message_sent_at" if step == "message" else "file_sent_at"
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO direct_checkout_delivery_state(checkout_id) VALUES (?)",
                (checkout_id,),
            )
            conn.execute(
                f"UPDATE direct_checkout_delivery_state SET {column}="
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE checkout_id=?",
                (checkout_id,),
            )

    def finish_direct_checkout_batch(self, checkout_id: int) -> None:
        with self._write() as conn:
            state = conn.execute(
                """SELECT message_sent_at, file_sent_at
                   FROM direct_checkout_delivery_state WHERE checkout_id=?""",
                (checkout_id,),
            ).fetchone()
            if not state or not state["message_sent_at"] or not state["file_sent_at"]:
                raise ValueError("Both delivery steps must finish first")
            conn.execute(
                """UPDATE direct_checkout_orders SET status='sent',
                          sent_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE checkout_id=? AND status='pending'""", (checkout_id,),
            )

    def retry_direct_checkout_batch(self, checkout_id: int, delay_seconds: int) -> None:
        if type(delay_seconds) is not int or delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._write() as conn:
            conn.execute(
                """UPDATE direct_checkout_orders SET attempts=attempts+1,
                          next_attempt_at=? WHERE checkout_id=? AND status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 checkout_id),
            )

    def finish_direct_checkout_order(self, order_id: int) -> None:
        with self._write() as conn:
            conn.execute(
                """UPDATE direct_checkout_orders SET status='sent',
                          sent_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE order_id=? AND status='pending'""", (order_id,),
            )

    def retry_direct_checkout_order(self, order_id: int, delay_seconds: int) -> None:
        if delay_seconds < 1:
            raise ValueError("Invalid retry delay")
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._write() as conn:
            conn.execute(
                """UPDATE direct_checkout_orders SET attempts=attempts+1,
                          next_attempt_at=? WHERE order_id=? AND status='pending'""",
                (next_attempt.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 order_id),
            )

    def create_topup(self, user_id: int, requested_amount: int) -> dict:
        if requested_amount <= 0:
            raise ValueError("Top-up amount must be positive")
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
            recent = conn.execute(
                """SELECT COUNT(*) FROM topups WHERE user_id=? AND status='open'
                   AND created_at>=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 hour')""",
                (user_id,),
            ).fetchone()[0]
            if recent >= 12:
                raise TopupLimitReached("Too many top-up requests in one hour")
            for _ in range(3):
                code = "NAP" + secrets.token_hex(6).upper()
                try:
                    topup_id = conn.execute(
                        "INSERT INTO topups(user_id, code, requested_amount) VALUES (?, ?, ?)",
                        (user_id, code, requested_amount),
                    ).lastrowid
                    return {"id": topup_id, "code": code, "requested_amount": requested_amount}
                except sqlite3.IntegrityError:
                    pass
            raise ShopError("Could not generate a unique transfer code")

    def list_topups(self, user_id: int, limit: int = 10):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT id, code, requested_amount, paid_amount, status, created_at
                   FROM topups WHERE user_id=? ORDER BY id DESC LIMIT ?""", (user_id, limit)
            )]

    def list_all_topups(self, limit: int = 9, offset: int = 0):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT t.id, t.user_id, t.code, t.requested_amount, t.paid_amount,
                          t.status, t.created_at, u.username, u.display_name
                   FROM topups t JOIN users u ON u.telegram_id=t.user_id
                   ORDER BY t.id DESC LIMIT ? OFFSET ?""", (limit, offset)
            )]

    def record_bank_event(
        self, transaction_id: str, bank_account: str, amount: int,
        content: str, transfer_type: str, code: str | None,
        expected_account: str, *, via_webhook: bool = False,
        notify_admin_ids: frozenset[int] = frozenset(),
    ) -> dict:
        if not transaction_id or amount <= 0 or amount > 1_000_000_000_000:
            raise ValueError("Invalid bank transaction")
        with self._write() as conn:
            if via_webhook:
                conn.execute(
                    """INSERT INTO webhook_state(id) VALUES (1)
                       ON CONFLICT(id) DO UPDATE SET
                       last_success_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"""
                )
            existing = conn.execute(
                "SELECT status FROM bank_events WHERE provider_txn_id=?", (transaction_id,)
            ).fetchone()
            if existing:
                return {
                    "status": "duplicate", "original_status": existing["status"],
                    "transaction_id": transaction_id,
                }

            topup = None
            status = "ignored" if bank_account != expected_account or transfer_type != "in" else "unmatched"
            if status == "unmatched" and code:
                topup = conn.execute("SELECT id, user_id FROM topups WHERE code=?", (code,)).fetchone()
                if topup:
                    status = "credited"

            event_id = conn.execute(
                """INSERT INTO bank_events
                   (provider_txn_id, bank_account, amount, content, transfer_type, status, topup_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (transaction_id, bank_account, amount, content[:500], transfer_type,
                 status, topup["id"] if topup else None),
            ).lastrowid
            if status == "credited":
                conn.execute(
                    "UPDATE topups SET status='paid', paid_amount=paid_amount+? WHERE id=?",
                    (amount, topup["id"]),
                )
                conn.execute(
                    "UPDATE users SET balance=balance+? WHERE telegram_id=?",
                    (amount, topup["user_id"]),
                )
                conn.execute(
                    "INSERT INTO ledger(user_id, amount, kind, ref_id) VALUES (?, ?, 'topup', ?)",
                    (topup["user_id"], amount, event_id),
                )
                result = {
                    "status": status, "user_id": topup["user_id"], "amount": amount,
                    "code": code, "transaction_id": transaction_id,
                }
                checkout = conn.execute(
                    "SELECT * FROM direct_checkouts WHERE topup_id=?", (topup["id"],)
                ).fetchone()
                if checkout:
                    result["direct_checkout_id"] = checkout["id"]
                    paid_amount = conn.execute(
                        "SELECT paid_amount FROM topups WHERE id=?", (topup["id"],)
                    ).fetchone()[0]
                    result["expected_amount"] = checkout["expected_amount"]
                    result["paid_amount"] = paid_amount
                    if checkout["status"] == "open":
                        now = conn.execute(
                            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                        ).fetchone()[0]
                        if now > checkout["expires_at"]:
                            conn.execute(
                                "UPDATE direct_checkouts SET status='expired' WHERE id=?",
                                (checkout["id"],),
                            )
                            result["direct_status"] = "expired"
                        elif paid_amount < checkout["expected_amount"]:
                            result["direct_status"] = "partial"
                        else:
                            try:
                                rows = self._cart_rows(
                                    conn, topup["user_id"], checkout["id"],
                                )
                                quote = self._quote_cart_conn(
                                    conn, topup["user_id"], rows,
                                    checkout["coupon_code"], require_emails=True,
                                )
                                if quote["price"] != checkout["expected_amount"]:
                                    raise PriceChanged("Checkout price changed")
                                purchase = self._purchase_cart_conn(
                                    conn, topup["user_id"], quote,
                                    notify_admin_ids, checkout["id"],
                                )
                            except (NotFound, OutOfStock, PriceChanged, CouponUnavailable,
                                    EmailRequired, InsufficientBalance, ValueError) as exc:
                                reason = type(exc).__name__
                                conn.execute(
                                    """UPDATE direct_checkouts SET status='failed',
                                              failure_reason=? WHERE id=?""",
                                    (reason, checkout["id"]),
                                )
                                result["direct_status"] = "failed"
                                result["failure_reason"] = reason
                            else:
                                conn.execute(
                                    """UPDATE direct_checkouts SET status='fulfilled',
                                              fulfilled_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                                       WHERE id=?""", (checkout["id"],),
                                )
                                result["direct_status"] = "fulfilled"
                                result["order_count"] = len(purchase["orders"])
                    else:
                        result["direct_status"] = "extra"
                result["balance"] = conn.execute(
                    "SELECT balance FROM users WHERE telegram_id=?", (topup["user_id"],)
                ).fetchone()["balance"]
                return result
            return {
                "status": status, "amount": amount, "code": code,
                "transaction_id": transaction_id,
            }

    def list_unmatched_events(self, limit: int = 10, offset: int = 0):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT id, provider_txn_id, amount, content, created_at FROM bank_events
                   WHERE status='unmatched' ORDER BY id DESC LIMIT ? OFFSET ?""", (limit, offset)
            )]

    def get_unmatched_event(self, event_id: int):
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT id, provider_txn_id, bank_account, amount, content,
                          transfer_type, created_at FROM bank_events
                   WHERE id=? AND status='unmatched'""", (event_id,),
            ).fetchone()
            return dict(row) if row else None

    def preview_manual_credit(self, event_id: int, code: str, expected_account: str) -> dict:
        code = code.strip().upper()
        if not TOPUP_CODE_PATTERN.fullmatch(code):
            raise ValueError("Invalid top-up code")
        with closing(self._connect()) as conn:
            event = conn.execute(
                "SELECT * FROM bank_events WHERE id=?", (event_id,)
            ).fetchone()
            if not event or event["status"] != "unmatched":
                raise ManualCreditUnavailable("Transaction is no longer unmatched")
            if event["bank_account"] != expected_account or event["transfer_type"] != "in":
                raise ManualCreditUnavailable("Transaction is not a valid incoming transfer")
            topup = conn.execute(
                """SELECT t.id, t.code, t.user_id, t.requested_amount, t.paid_amount,
                          u.balance, u.username, u.display_name
                   FROM topups t JOIN users u ON u.telegram_id=t.user_id
                   WHERE t.code=?""", (code,),
            ).fetchone()
            if not topup:
                raise NotFound("Top-up code not found")
            return {
                "event_id": event["id"], "transaction_id": event["provider_txn_id"],
                "amount": event["amount"], "content": event["content"],
                "created_at": event["created_at"], "bank_account": event["bank_account"],
                "topup_id": topup["id"], "code": topup["code"],
                "requested_amount": topup["requested_amount"],
                "paid_amount": topup["paid_amount"], "user_id": topup["user_id"],
                "balance": topup["balance"], "username": topup["username"],
                "display_name": topup["display_name"],
            }

    def resolve_manual_credit(
        self, event_id: int, topup_id: int, expected_amount: int,
        expected_account: str, admin_id: int,
    ) -> dict:
        if any(type(value) is not int or value <= 0 for value in
               (event_id, topup_id, expected_amount, admin_id)):
            raise ValueError("Invalid manual credit confirmation")
        with self._write() as conn:
            event = conn.execute(
                "SELECT * FROM bank_events WHERE id=?", (event_id,)
            ).fetchone()
            if (not event or event["status"] != "unmatched"
                    or event["bank_account"] != expected_account
                    or event["transfer_type"] != "in"
                    or event["amount"] != expected_amount):
                raise ManualCreditUnavailable("Transaction changed or is not eligible")
            topup = conn.execute(
                "SELECT id, code, user_id FROM topups WHERE id=?", (topup_id,)
            ).fetchone()
            if not topup:
                raise ManualCreditUnavailable("Top-up no longer exists")
            conn.execute(
                "UPDATE bank_events SET status='credited', topup_id=? WHERE id=?",
                (topup_id, event_id),
            )
            conn.execute(
                "UPDATE topups SET status='paid', paid_amount=paid_amount+? WHERE id=?",
                (expected_amount, topup_id),
            )
            conn.execute(
                "UPDATE users SET balance=balance+? WHERE telegram_id=?",
                (expected_amount, topup["user_id"]),
            )
            conn.execute(
                "INSERT INTO ledger(user_id, amount, kind, ref_id) VALUES (?, ?, 'topup', ?)",
                (topup["user_id"], expected_amount, event_id),
            )
            conn.execute(
                """INSERT INTO manual_credit_resolutions
                   (bank_event_id, topup_id, user_id, admin_id, amount)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, topup_id, topup["user_id"], admin_id, expected_amount),
            )
            checkout = conn.execute(
                "SELECT id, status FROM direct_checkouts WHERE topup_id=?", (topup_id,),
            ).fetchone()
            if checkout and checkout["status"] == "open":
                conn.execute(
                    """UPDATE direct_checkouts SET status='failed',
                              failure_reason='ManualCredit' WHERE id=?""",
                    (checkout["id"],),
                )
            balance = conn.execute(
                "SELECT balance FROM users WHERE telegram_id=?", (topup["user_id"],)
            ).fetchone()["balance"]
            result = {
                "status": "credited", "user_id": topup["user_id"],
                "amount": expected_amount, "balance": balance, "code": topup["code"],
                "transaction_id": event["provider_txn_id"],
            }
            if checkout:
                result.update({
                    "direct_checkout_id": checkout["id"],
                    "direct_status": "failed" if checkout["status"] == "open" else "extra",
                    "failure_reason": "ManualCredit" if checkout["status"] == "open" else None,
                })
            return result

    def list_manual_credits(self, limit: int = 10, offset: int = 0) -> list[dict]:
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT m.bank_event_id, e.provider_txn_id, m.topup_id, t.code,
                          m.user_id, u.username, u.display_name, m.admin_id,
                          m.amount, m.created_at
                   FROM manual_credit_resolutions m
                   JOIN bank_events e ON e.id=m.bank_event_id
                   JOIN topups t ON t.id=m.topup_id
                   JOIN users u ON u.telegram_id=m.user_id
                   ORDER BY m.created_at DESC, m.bank_event_id DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            )]

    def list_bank_events(self, limit: int = 9, offset: int = 0):
        with closing(self._connect()) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT e.id, e.provider_txn_id, e.amount, e.content, e.status,
                          e.created_at, t.user_id, t.code, u.username, u.display_name
                   FROM bank_events e LEFT JOIN topups t ON t.id=e.topup_id
                   LEFT JOIN users u ON u.telegram_id=t.user_id
                   ORDER BY e.id DESC LIMIT ? OFFSET ?""", (limit, offset)
            )]
