import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from html import escape
from io import BytesIO
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlencode

from telegram import (
    BotCommand, BotCommandScopeAllPrivateChats, CopyTextButton, InlineKeyboardButton,
    InlineKeyboardMarkup, InputFile, LinkPreviewOptions, MessageEntity, Update,
)
from telegram.constants import ChatType, InlineKeyboardButtonLimit
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.helpers import create_deep_linked_url
from telegram.ext import (
    Application, ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler,
    CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters,
)

from config import Config, backup_directory, load_config
from export_reports import (MAX_EXPORT_DAYS, build_export_archive,
                            build_admin_audit_csv, build_sold_products_csv,
                            build_users_csv,
                            parse_date_range)
from rate_limit import UpdateLimiter
from reconciliation import ReconciliationError, reconcile_once
from store import (
    BROADCAST_MAX_CHARS, COUPON_CODE_PATTERN, MAX_ADMIN_TEST_CREDIT,
    MAX_FILE_STOCK_BYTES,
    BalanceChanged, CouponUnavailable, DuplicateStock, ExportTooLarge,
    EmailRequired, InsufficientBalance, ManualCreditUnavailable,
    NotFound, OutOfStock, PriceChanged, SaleActive, SaleCouponConflict,
    SHOP_TIMEZONE, Store, TopupLimitReached, WarrantyAlreadyOpen, WarrantyClosed,
    normalize_customer_email, parse_json_file,
)
from webhook import start_webhook_server
from vietqr import vietqr_png


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
AMOUNT_PATTERN = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:[.,][0-9]{3})+)")
PAGE_SIZE = 8
PRODUCT_VIEW_PAGE_SIZE = 20
FOLLOWER_PAGE_SIZE = 20
INVENTORY_TEXT_MAX_UNITS = 3900
SHOP_CATALOG_MAX_PRODUCTS = 60
TOPUP_PRESETS = (10_000, 20_000, 50_000, 100_000, 200_000, 500_000, 1_000_000)
PURCHASE_QUANTITY_PRESETS = (1, 2, 3, 5, 10)
MAX_PURCHASE_QUANTITY = 10
ORDER_CHAT_MAX_UNITS = 2000
ORDER_CHAT_FILE_CAPTION_MAX_UNITS = 900
ORDER_CHAT_INPUT_SECONDS = 600
WEBHOOK_ALERT_INTERVAL = 3600
INVENTORY_CHECK_INTERVAL = 600
ANNOUNCEMENT_BATCH_SIZE = 20
ANNOUNCEMENT_SEND_INTERVAL = 0.2
ANNOUNCEMENT_POLL_INTERVAL = 5
BROADCAST_PREFIX = "📣 THÔNG BÁO TỪ SHOP\n\n"
BROADCAST_PREFIX_EN = "📣 SHOP ANNOUNCEMENT\n\n"
STATUS_RECONCILIATION_MAX_AGE = timedelta(minutes=10)
STATUS_BACKUP_MAX_AGE = timedelta(hours=36)
WEBHOOK_ALERT_TEXT = {
    "rejected": "⚠️ Webhook SePay bị từ chối (HTTP 4xx). Kiểm tra Secret Key, thời gian máy và lịch sử webhook lỗi trong SePay.",
    "processing": "🚨 Webhook SePay xử lý lỗi (HTTP 5xx). Kiểm tra log bot và lịch sử webhook lỗi trong SePay; đối soát sẽ thử bù giao dịch.",
    "overload": "⚠️ Webhook SePay quá tải (HTTP 503). Kiểm tra lưu lượng/ngrok và lịch sử webhook lỗi trong SePay.",
    "notification": "⚠️ Webhook đã xử lý giao dịch nhưng không lên lịch được thông báo. Kiểm tra giao dịch và log bot.",
}


def vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


def price_html(price: int, previous_price: int | None = None) -> str:
    if previous_price is not None and previous_price > price:
        return f"<s>{vnd(previous_price)}</s> {vnd(price)}"
    return vnd(price)


def telegram_text_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def split_telegram_text(value: str, max_units: int) -> list[str]:
    parts = []
    start = 0
    units = 0
    for index, char in enumerate(value):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > max_units:
            parts.append(value[start:index])
            start = index
            units = 0
        units += size
    parts.append(value[start:])
    return parts


def inventory_messages(header: str, items: list[dict]) -> list[str]:
    messages = []
    current = header
    for item in items:
        label = f"Kho #{item['id']}"
        budget = INVENTORY_TEXT_MAX_UNITS - telegram_text_units(header + label) - 50
        fragments = split_telegram_text(item["secret"], budget)
        for index, fragment in enumerate(fragments, 1):
            title = label if len(fragments) == 1 else f"{label} (phần {index}/{len(fragments)})"
            entry = f"{title}\n{fragment}"
            candidate = f"{current}\n\n{entry}"
            if telegram_text_units(candidate) > INVENTORY_TEXT_MAX_UNITS:
                messages.append(current)
                current = entry
            else:
                current = candidate
    messages.append(current)
    return messages


def tr(language: str, vietnamese: str, english: str) -> str:
    return english if language == "en" else vietnamese


def account_pre_entities(content: str, language: str,
                         secrets: list[str]) -> list[MessageEntity]:
    """Mark account values as Telegram code blocks using UTF-16 offsets."""
    marker = tr(language, "Thông tin tài khoản:\n", "Account details:\n")
    entities = []
    cursor = 0
    for secret in secrets:
        while True:
            marker_at = content.find(marker, cursor)
            if marker_at < 0:
                log.error("Account code block marker missing; sending plain account text")
                return []
            start = marker_at + len(marker)
            if content.startswith(secret, start):
                break
            cursor = start
        entities.append(MessageEntity(
            type=MessageEntity.PRE,
            offset=telegram_text_units(content[:start]),
            length=telegram_text_units(secret),
        ))
        cursor = start + len(secret)
    return entities


def file_only_delivery(order: dict) -> bool:
    return bool(order.get("file_only_delivery")) or order.get("delivery_type") == "file"


def order_delivery_extension(orders: list[dict]) -> str:
    file_items = [order for order in orders if order.get("delivery_type") == "file"]
    if not file_items:
        return ".txt"
    try:
        for order in file_items:
            parse_json_file(order["secret"])
    except ValueError:
        # Files imported before JSON support remain downloadable in their old format.
        return ".txt"
    return ".json"


def account_delivery_payload(orders: list[dict], language: str,
                             introduction: str,
                             warranty_ticket: dict | None = None) -> tuple[str, list[MessageEntity],
                                                         InlineKeyboardMarkup]:
    """Keep one purchase in one message, with a file fallback for long accounts."""
    if not orders:
        raise ValueError("A delivery needs at least one order")
    marker = tr(language, "Thông tin tài khoản:\n", "Account details:\n")
    purchase_id = orders[0]["id"]
    file_delivery = any(order.get("delivery_type") == "file" for order in orders)
    hide_details = any(file_only_delivery(order) for order in orders)
    extension = order_delivery_extension(orders)
    item_name = (tr(language, "sản phẩm", "item(s)") if file_delivery else
                 tr(language, "tài khoản", "account(s)"))
    heading = f"📦 {tr(language, 'Đơn hàng', 'Order')} #{purchase_id} · {len(orders)} {item_name}"
    sections = []
    for index, order in enumerate(orders, 1):
        email_status = (tr(language, "đã xử lý xong", "processed")
                        if order.get("email_completed_at") else
                        tr(language, "đang chờ xử lý", "pending"))
        email = (tr(language, f"Email xử lý đơn: {order['customer_email']}\n"
                    f"Trạng thái email: {email_status}\n",
                    f"Order email: {order['customer_email']}\n"
                    f"Email status: {email_status}\n") if order["customer_email"] else "")
        if hide_details:
            sections.append(f"{index}. {order['name']} · #{order['id']}")
        else:
            sections.append(tr(language,
                               f"{index}. {order['name']} · tài khoản #{order['id']}\n",
                               f"{index}. {order['name']} · account #{order['id']}\n")
                            + email + marker + order["secret"])
    footer = tr(language, "Cảm ơn bạn đã mua hàng! Xem lại bằng /orders.",
                "Thank you for your purchase! View it again with /orders.")
    content = introduction + "\n\n" + heading + "\n\n" + "\n\n".join(sections) + "\n\n" + footer
    if hide_details:
        content += tr(language, f"\n\nNội dung chỉ có trong file {extension} gửi bên dưới.",
                      f"\n\nThe contents are only in the {extension} file below.")
        entities = []
    elif telegram_text_units(content) <= 3900:
        entities = account_pre_entities(content, language,
                                        [order["secret"] for order in orders])
    else:
        content = introduction + "\n\n" + heading + "\n" + tr(
            language, "Thông tin tài khoản đầy đủ nằm trong file .txt bên dưới.\n",
            "All account details are in the .txt file below.\n",
        ) + footer
        entities = []
    buttons = []
    all_secrets = "\n".join(order["secret"] for order in orders) if not hide_details else ""
    if not hide_details and telegram_text_units(all_secrets) <= InlineKeyboardButtonLimit.MAX_COPY_TEXT:
        buttons.append([InlineKeyboardButton(
            tr(language, "📋 Sao chép đơn hàng", "📋 Copy order"),
            copy_text=CopyTextButton(all_secrets),
        )])
    elif not hide_details:
        buttons.append([InlineKeyboardButton(
            tr(language,
               "📋 Mở để sao chép đơn" if telegram_text_units(all_secrets) <= 3900
               else "📄 Tải lại file đơn",
               "📋 Open order to copy" if telegram_text_units(all_secrets) <= 3900
               else "📄 Get order file"),
            callback_data=f"copyorder:{purchase_id}",
        )])
    buttons.append([InlineKeyboardButton(
        tr(language, f"📄 Tải file {extension}", f"📄 Download {extension} file"),
        callback_data=f"orderfile:{purchase_id}",
    )])
    if warranty_ticket and warranty_ticket["status"] == "open":
        warranty_label = tr(language, "🛡 Xem bảo hành đang chờ",
                            "🛡 View pending warranty")
        warranty_action = "status"
    elif warranty_ticket:
        warranty_label = (tr(language, "🛡 Xem bảo hành", "🛡 View warranty")
                          if warranty_ticket.get("resolved_privately") else
                          tr(language, "🛡 Xem phản hồi bảo hành",
                             "🛡 View warranty reply"))
        warranty_action = "status"
    else:
        warranty_label = tr(language, "🛡 Yêu cầu bảo hành đơn",
                            "🛡 Request order warranty")
        warranty_action = "start"
    buttons.append([InlineKeyboardButton(
        warranty_label, callback_data=f"warranty:{warranty_action}:{purchase_id}",
    )])
    return content, entities, InlineKeyboardMarkup(buttons)


def account_delivery_file(orders: list[dict], language: str) -> bytes:
    """Build a UTF-8 text attachment in memory; never write credentials to disk."""
    if not orders:
        raise ValueError("A delivery needs at least one order")
    file_delivery = any(order.get("delivery_type") == "file" for order in orders)
    lines = [tr(language,
                f"ĐƠN HÀNG #{orders[0]['id']} — {len(orders)} {'SẢN PHẨM' if file_delivery else 'TÀI KHOẢN'}",
                f"ORDER #{orders[0]['id']} — {len(orders)} {'ITEMS' if file_delivery else 'ACCOUNTS'}")]
    for index, order in enumerate(orders, 1):
        lines.extend((
            "",
            f"{'=' * 32}",
            tr(language,
               f"{index}. {order['name']} · {'file' if order.get('delivery_type') == 'file' else 'tài khoản'} #{order['id']}",
               f"{index}. {order['name']} · {'file' if order.get('delivery_type') == 'file' else 'account'} #{order['id']}"),
        ))
        if order["customer_email"]:
            lines.append(tr(language,
                f"Email xử lý đơn: {order['customer_email']}",
                f"Order email: {order['customer_email']}"))
            lines.append(tr(language,
                "Trạng thái email: đã xử lý xong" if order.get("email_completed_at")
                else "Trạng thái email: đang chờ xử lý",
                "Email status: processed" if order.get("email_completed_at")
                else "Email status: pending"))
        lines.append(tr(language, "Nội dung file:" if order.get("delivery_type") == "file"
                        else "Thông tin tài khoản:",
                        "File contents:" if order.get("delivery_type") == "file"
                        else "Account details:"))
        lines.append(order["secret"])
    return ("\r\n".join(lines) + "\r\n").encode("utf-8-sig")


def order_delivery_file(orders: list[dict], language: str) -> tuple[bytes, str]:
    """Return the private attachment and its extension for one complete purchase."""
    extension = order_delivery_extension(orders)
    if extension == ".txt":
        return account_delivery_file(orders, language), extension
    if len(orders) == 1:
        return orders[0]["secret"].encode("utf-8"), extension
    items = []
    for order in orders:
        is_file = order.get("delivery_type") == "file"
        item = {
            "id": order["id"], "product": order["name"],
            "type": "file" if is_file else "account",
            "content": parse_json_file(order["secret"]) if is_file else order["secret"],
        }
        if order.get("customer_email"):
            item["customer_email"] = order["customer_email"]
        items.append(item)
    document = {"order_id": orders[0]["id"], "items": items}
    return (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
            .encode("utf-8")), extension


def sale_percent(original_price: int, sale_price: int) -> str:
    percent = (original_price - sale_price) * 100 / original_price
    return f"{percent:.1f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def latest_backup_at(directory: Path) -> datetime | None:
    timestamps = (
        path.stat().st_mtime for path in directory.glob("shop-*.sqlite3")
        if path.is_file()
    )
    latest = max(timestamps, default=None)
    return datetime.fromtimestamp(latest, timezone.utc) if latest is not None else None


def status_time(moment: datetime | None) -> str:
    return moment.astimezone(SHOP_TIMEZONE).strftime("%d/%m/%Y %H:%M") if moment else "chưa có"


def parse_stored_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def user_label(user_id: int, username: str | None, display_name: str | None) -> str:
    name = f"@{username}" if username else " ".join((display_name or "").split())[:40]
    return f"{name} (ID {user_id})" if name else f"ID {user_id}"


def purchased_product_label(product: dict, max_name_length: int = 48) -> str:
    name = " ".join(product["name"].split())
    if len(name) > max_name_length:
        name = name[:max_name_length - 1] + "…"
    return f"{name} ×{product['quantity']}"


def viewed_product_label(product: dict, max_name_length: int = 42,
                         show_time: bool = False) -> str:
    name = " ".join(product["name"].split())
    if len(name) > max_name_length:
        name = name[:max_name_length - 1] + "…"
    archived = " · đã lưu trữ" if product["deleted"] else ""
    label = f"#{product['display_id']} {name} · {product['view_count']} lượt mở{archived}"
    if show_time:
        label += f" · gần nhất {status_time(parse_stored_time(product['last_view_at']))}"
    return label


def parse_amount(value: str) -> int:
    value = value.strip()
    if re.fullmatch(r"[0-9]+[kK]", value):
        return int(value[:-1]) * 1000
    if not AMOUNT_PATTERN.fullmatch(value):
        raise ValueError("Số tiền không hợp lệ")
    return int(value.replace(".", "").replace(",", ""))


def parse_discount(value: str) -> tuple[str, int]:
    value = value.strip()
    if value.endswith("%"):
        amount = int(value[:-1])
        if not 1 <= amount <= 99:
            raise ValueError("Invalid percentage")
        return "percent", amount
    amount = parse_amount(value)
    if amount <= 0:
        raise ValueError("Invalid discount")
    return "fixed", amount


class AmbiguousProductReference(ValueError):
    def __init__(self, display_id: int):
        super().__init__(
            f"Số {display_id} có thể là số hiển thị của sản phẩm này hoặc mã quản lý "
            f"của sản phẩm khác. Hãy nhập #{display_id} trong lệnh để chọn đúng "
            "sản phẩm đang thấy trong /products. Chưa có gì được thay đổi."
        )


def menu_keyboard(language: str = "vi"):
    rows = [
        [InlineKeyboardButton(tr(language, "🛍 Xem sản phẩm", "🛍 Products"),
                              callback_data="shop:0", style="success")],
        [InlineKeyboardButton(tr(language, "🏦 Đơn chuyển khoản", "🏦 Bank orders"),
                              callback_data="payments")],
        [InlineKeyboardButton(tr(language, "🎁 Gói ưu đãi", "🎁 Bundles"), callback_data="bundles:0")],
        [InlineKeyboardButton(tr(language, "💳 Nạp tiền", "💳 Add funds"), callback_data="topup"),
         InlineKeyboardButton(tr(language, "💰 Số dư", "💰 Balance"), callback_data="balance")],
        [InlineKeyboardButton(tr(language, "📦 Đơn hàng", "📦 Orders"), callback_data="orders"),
         InlineKeyboardButton(tr(language, "🧾 Lịch sử nạp", "🧾 Deposits"), callback_data="deposits")],
        [InlineKeyboardButton(tr(language, "❤️ Sản phẩm theo dõi", "❤️ Following"),
                              callback_data="following:0"),
         InlineKeyboardButton(tr(language, "🔔 Thông báo kho", "🔔 Stock alerts"),
                              callback_data="stocknotify")],
        [InlineKeyboardButton("🌐 Ngôn ngữ / Language", callback_data="language")],
        [InlineKeyboardButton(tr(language, "ℹ️ Hướng dẫn", "ℹ️ Help"), callback_data="help")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Người dùng", callback_data="admin:users:0"),
         InlineKeyboardButton("🧾 Đơn đã bán", callback_data="admin:sales:0")],
        [InlineKeyboardButton("📜 Nhật ký quản trị", callback_data="admin:audit:0")],
        [InlineKeyboardButton("🧪 Cộng tiền thử nghiệm", callback_data="admin:balancecredit:home")],
        [InlineKeyboardButton("📧 Đơn email chờ", callback_data="admin:pendingemails:0"),
         InlineKeyboardButton("⏰ Nhắc đơn email", callback_data="admin:emailremind")],
        [InlineKeyboardButton("🚫 Người dùng bị cấm", callback_data="admin:bans:0")],
        [InlineKeyboardButton("💰 Yêu cầu nạp", callback_data="admin:topups:0"),
         InlineKeyboardButton("🏦 Giao dịch ngân hàng", callback_data="admin:transactions:0")],
        [InlineKeyboardButton("⚠️ Chưa khớp", callback_data="admin:unmatched:0"),
         InlineKeyboardButton("🧾 Đã xử lý tay", callback_data="admin:manualcredits:0")],
        [InlineKeyboardButton("🩺 Trạng thái shop", callback_data="admin:status"),
         InlineKeyboardButton("🔄 Đối soát SePay", callback_data="admin:reconcile")],
        [InlineKeyboardButton("📦 Sản phẩm", callback_data="admin:products:0"),
         InlineKeyboardButton("🗂 Đã lưu trữ", callback_data="admin:archived:0")],
        [InlineKeyboardButton("🎁 Gói sản phẩm", callback_data="admin:bundles:0"),
         InlineKeyboardButton("🏷 Mã giảm giá", callback_data="admin:coupons:0")],
        [InlineKeyboardButton("🛡 Bảo hành", callback_data="admin:warranties:0"),
         InlineKeyboardButton("⏰ Nhắc bảo hành", callback_data="admin:warrantyremind")],
        [InlineKeyboardButton("📊 Báo cáo 7 ngày", callback_data="admin:report")],
        [InlineKeyboardButton("📈 Hành trình khách", callback_data="admin:funnel:7")],
        [InlineKeyboardButton("👀 Sản phẩm khách xem", callback_data="admin:views:7:0")],
        [InlineKeyboardButton("📥 Xuất báo cáo CSV", callback_data="admin:export")],
        [InlineKeyboardButton("📣 Gửi thông báo", callback_data="admin:broadcast")],
    ])


def admin_footer(prefix: str, page: int, has_more: bool):
    rows = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀ Trước", callback_data=f"{prefix}:{page - 1}"))
    if has_more:
        navigation.append(InlineKeyboardButton("Sau ▶", callback_data=f"{prefix}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")])
    return rows


class ShopBot:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.server = None
        self.server_thread = None
        self.loop = None
        self.application: Application | None = None
        self.limiter = UpdateLimiter()
        self.banned_user_ids = store.banned_user_ids()
        self.reconciliation_lock = asyncio.Lock()
        self.reconciliation_task = None
        self.inventory_alert_task = None
        self.stock_announcement_task = None
        self.purchase_notification_task = None
        self.direct_delivery_task = None
        self.direct_delivery_lock = asyncio.Lock()
        self.new_customer_notification_task = None
        self.email_completion_task = None
        self.email_reminder_task = None
        self.warranty_reminder_task = None
        self.last_reconciliation_alert = 0.0
        self.webhook_alert_lock = threading.Lock()
        self.last_webhook_alerts = {}
        self.last_announcement_send = {}
        self.announcement_pause_until = 0.0
        self.funnel_cache_date: date | None = None
        self.funnel_cache: set[tuple[str, int]] = set()
        self.bot_username: str | None = None

    def admin_product_id(self, reference: str) -> int:
        match = re.fullmatch(r"(#?)([0-9]+)", reference.strip())
        if not match:
            raise ValueError("Invalid product number")
        display_id = int(match.group(2))
        product_id = self.store.product_id_for_display_id(display_id)
        if not match.group(1) and product_id != display_id and self.store.get_product(display_id):
            raise AmbiguousProductReference(display_id)
        return product_id

    def is_private(self, update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)

    def language_for_message(self, message) -> str:
        chat_id = self.message_user_id(message)
        return self.store.get_user_language(chat_id) if isinstance(chat_id, int) else "vi"

    @staticmethod
    def message_user_id(message) -> int | None:
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            chat_id = getattr(getattr(message, "chat", None), "id", None)
        return chat_id if isinstance(chat_id, int) else None

    def record_funnel_activity(self, user_id: int | None, event_type: str) -> None:
        if user_id is None or user_id in self.config.admin_ids:
            return
        today = datetime.now(SHOP_TIMEZONE).date()
        if self.funnel_cache_date != today:
            self.funnel_cache_date = today
            self.funnel_cache.clear()
        key = (event_type, user_id)
        if key not in self.funnel_cache and self.store.user_exists(user_id):
            self.store.record_funnel_event(user_id, event_type, today)
            self.funnel_cache.add(key)

    def audit_admin_action(self, admin_id: int, action: str,
                           target_type: str | None = None,
                           target_id: int | str | None = None,
                           details: str = "") -> None:
        """Best-effort audit logging; an audit failure must not break shop actions."""
        try:
            self.store.log_admin_action(admin_id, action, target_type, target_id, details)
        except Exception:
            log.exception("Could not record admin audit action")

    async def admin_allowed(self, update: Update) -> bool:
        if not self.is_private(update):
            return False
        if update.effective_user.id not in self.config.admin_ids:
            language = self.store.get_user_language(update.effective_user.id)
            await update.effective_message.reply_text(tr(
                language, "Lệnh này chỉ dành cho quản trị viên.",
                "This command is for shop admins only.",
            ))
            return False
        raw = getattr(getattr(update, "effective_message", None), "text", None)
        if isinstance(raw, str) and raw.startswith("/"):
            command = raw.split(maxsplit=1)[0].split("@", 1)[0]
            self.audit_admin_action(update.effective_user.id, "command", "command", command)
        return True

    async def spam_guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_private(update):
            raise ApplicationHandlerStop
        if not update.effective_user:
            return
        user_id = update.effective_user.id
        is_admin = user_id in self.config.admin_ids
        if not is_admin and user_id in self.banned_user_ids:
            raise ApplicationHandlerStop
        allowed, warn = self.limiter.check_update(
            user_id, is_admin=is_admin,
        )
        if allowed:
            if not self.store.user_exists(user_id):
                self.store.ensure_user(
                    user_id, getattr(update.effective_user, "username", None),
                    getattr(update.effective_user, "full_name", None),
                    notify_admin_ids=(self.config.admin_ids
                                      if user_id not in self.config.admin_ids else ()),
                )
            self.record_funnel_activity(user_id, "visit")
            return
        if warn:
            try:
                result = self.store.record_spam_warning(user_id)
            except Exception:
                log.exception("Could not record spam warning for %s", user_id)
                raise ApplicationHandlerStop
            if result["banned"]:
                self.banned_user_ids.add(user_id)
            if not result["recorded"]:
                raise ApplicationHandlerStop
            language = self.store.get_user_language(user_id)
            if result["banned"]:
                notice = tr(
                    language,
                    "Bạn đã nhận 3 cảnh cáo spam trong 1 giờ và bị cấm dùng bot. "
                    "Liên hệ chủ shop nếu cần hỗ trợ.",
                    "You received 3 spam warnings within an hour and have been banned. "
                    "Contact the shop owner for help.",
                )
            else:
                notice = tr(
                    language,
                    f"Cảnh cáo spam {result['count']}/3. Bạn thao tác quá nhanh. "
                    "Đủ 3 cảnh cáo trong 1 giờ sẽ bị cấm dùng bot.",
                    f"Spam warning {result['count']}/3. You are going too fast. "
                    "Three warnings within an hour will ban you from the bot.",
                )
            try:
                if update.callback_query:
                    await update.callback_query.answer(notice, show_alert=True)
                elif update.effective_message:
                    await update.effective_message.reply_text(notice)
            except TelegramError:
                pass
        raise ApplicationHandlerStop

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_private(update):
            return
        self.store.ensure_user(
            update.effective_user.id, update.effective_user.username,
            update.effective_user.full_name,
        )
        context.user_data.pop("awaiting_topup", None)
        context.user_data.pop("awaiting_warranty", None)
        context.user_data.pop("awaiting_bulk_stock", None)
        context.user_data.pop("awaiting_file_stock", None)
        context.user_data.pop("awaiting_coupon", None)
        context.user_data.pop("awaiting_order_email", None)
        context.user_data.pop("awaiting_export_range", None)
        context.user_data.pop("awaiting_manual_credit", None)
        context.user_data.pop("pending_manual_credit", None)
        self.clear_admin_test_credit_state(context)
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("pending_broadcast", None)
        context.user_data.pop("awaiting_admin_user_search", None)
        context.user_data.pop("admin_user_search_query", None)
        context.user_data.pop("awaiting_admin_sales_search", None)
        context.user_data.pop("admin_sales_search_query", None)
        context.user_data.pop("awaiting_display_id", None)
        context.user_data.pop("admin_product_flow", None)
        context.user_data.pop("admin_offer_flow", None)
        context.user_data.pop("product_checkout", None)
        context.user_data.pop("awaiting_buy_quantity", None)
        context.user_data.pop("awaiting_buy_email", None)
        context.user_data.pop("awaiting_buy_coupon", None)
        args = getattr(context, "args", ()) or ()
        payload = args[0] if len(args) == 1 else ""
        shared = re.fullmatch(r"([pb])_([1-9][0-9]{0,9})", payload)
        if shared:
            item_id = int(shared.group(2))
            if shared.group(1) == "p":
                await self.send_product_detail(update.message, update.effective_user.id, item_id)
            else:
                await self.send_bundle_detail(update.message, update.effective_user.id, item_id)
            return
        await self.send_menu(update.message, update.effective_user.id)

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            self.store.ensure_user(
                update.effective_user.id, update.effective_user.username,
                update.effective_user.full_name,
            )
            context.user_data.pop("awaiting_topup", None)
            context.user_data.pop("awaiting_warranty", None)
            context.user_data.pop("awaiting_bulk_stock", None)
            context.user_data.pop("awaiting_file_stock", None)
            context.user_data.pop("awaiting_coupon", None)
            context.user_data.pop("awaiting_order_email", None)
            context.user_data.pop("awaiting_export_range", None)
            context.user_data.pop("awaiting_manual_credit", None)
            context.user_data.pop("pending_manual_credit", None)
            self.clear_admin_test_credit_state(context)
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("pending_broadcast", None)
            context.user_data.pop("awaiting_admin_user_search", None)
            context.user_data.pop("admin_user_search_query", None)
            context.user_data.pop("awaiting_admin_sales_search", None)
            context.user_data.pop("admin_sales_search_query", None)
            context.user_data.pop("awaiting_admin_order_message", None)
            context.user_data.pop("awaiting_customer_order_reply", None)
            context.user_data.pop("awaiting_display_id", None)
            context.user_data.pop("admin_product_flow", None)
            context.user_data.pop("admin_offer_flow", None)
            context.user_data.pop("product_checkout", None)
            context.user_data.pop("awaiting_buy_quantity", None)
            context.user_data.pop("awaiting_buy_email", None)
            context.user_data.pop("awaiting_buy_coupon", None)
            await self.send_menu(update.message, update.effective_user.id)

    async def send_menu(self, message, user_id: int):
        language = self.store.get_user_language(user_id)
        balance = vnd(self.store.get_balance(user_id))
        await message.reply_text(
            tr(language,
               "🛍 CỬA HÀNG TÀI KHOẢN\n\n"
               "Chọn sản phẩm và số lượng để đặt mua.\n"
               "Thanh toán bằng số dư hoặc chuyển khoản ngân hàng.\n"
               "Đơn hàng sẽ được giao sau khi thanh toán thành công.\n\n"
               f"Số dư của bạn: {balance}",
               "🛍 ACCOUNT SHOP\n\n"
               "Choose a product and quantity to place an order.\n"
               "Pay with your balance or by bank transfer.\n"
               "Your order will be delivered after successful payment.\n\n"
               f"Your balance: {balance}"),
            reply_markup=menu_keyboard(language),
        )

    async def language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_language_options(update.message, update.effective_user.id)

    async def send_language_options(self, message, user_id: int):
        language = self.store.get_user_language(user_id)
        await message.reply_text(
            tr(language, "Chọn ngôn ngữ cho bot:", "Choose your bot language:"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🇻🇳 Tiếng Việt", callback_data="language:vi")],
                [InlineKeyboardButton("🇬🇧 English", callback_data="language:en")],
                [InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                      callback_data="menu")],
            ]),
        )

    async def stock_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            self.store.ensure_user(update.effective_user.id)
            await self.send_stock_notification_settings(update.message, update.effective_user.id)

    async def send_stock_notification_settings(self, message, user_id: int):
        language = self.store.get_user_language(user_id)
        enabled = self.store.stock_notifications_enabled(user_id)
        await message.reply_text(
            tr(language,
               "🔔 Thông báo kho đang " + ("bật." if enabled else "tắt.") + "\n"
               "Chỉ áp dụng cho tin có hàng mới của sản phẩm bạn theo dõi. "
               "Tin giảm giá vẫn được gửi khi bạn theo dõi sản phẩm.",
               "🔔 Stock alerts are " + ("on." if enabled else "off.") + "\n"
               "This only controls new stock alerts for products you follow. "
               "Sale alerts still arrive for followed products."),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    tr(language, "❌ Tắt thông báo kho", "❌ Turn off stock alerts")
                    if enabled else tr(language, "🔔 Bật thông báo kho", "🔔 Turn on stock alerts"),
                    callback_data="stocknotify:off" if enabled else "stocknotify:on",
                )],
                [InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                      callback_data="menu")],
            ]),
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_private(update):
            return
        await self.send_help(update.message)

    async def send_help(self, message):
        language = self.language_for_message(message)
        support = (tr(language, "\nHỗ trợ: ", "\nSupport: ") + self.config.support_contact
                   if self.config.support_contact else "")
        await message.reply_text(
            tr(language,
               "Chọn mục bằng các nút bên dưới hoặc gõ /menu để mở lại menu.\n"
               "Lệnh: /shop, /payments, /following, /bundles, /nap, /balance, /deposits, /orders, /stocknotify, /language, /id.\n"
               "Mở sản phẩm, chọn số lượng rồi chọn Chuyển khoản để nhận QR "
               "hoặc thanh toán bằng ví. Bot tự giao khi SePay xác nhận chuyển khoản.\n"
               "Trong /nap, chọn mức tiền có sẵn hoặc Số khác để tự nhập.\n"
               "Khi xác nhận mua, bấm Nhập mã giảm giá nếu bạn có mã.\n"
               "Mở đơn trong /orders để gửi yêu cầu bảo hành.\n"
               "Mở sản phẩm và bấm Theo dõi để nhận tin có hàng mới hoặc giảm giá.\n"
               "Dùng /stocknotify để bật hoặc tắt riêng tin có hàng mới.\n"
               "Số dư tự cộng khi ngân hàng ghi nhận giao dịch với đúng mã chuyển khoản.",
               "Use the buttons below or /menu to reopen the menu.\n"
               "Commands: /shop, /payments, /following, /bundles, /nap, /balance, /deposits, /orders, /stocknotify, /language, /id.\n"
               "Open a product, choose a quantity, then pay by bank QR or with your balance. "
               "The bot delivers after SePay confirms a bank transfer.\n"
               "In /nap, choose an amount or Other amount to enter your own.\n"
               "At checkout, use Apply coupon if you have a code.\n"
               "Open an order in /orders to request warranty support.\n"
               "Follow a product to receive its new stock and sale alerts.\n"
               "Use /stocknotify to turn new stock alerts on or off.\n"
               "Funds are added automatically when your bank transfer includes the correct code.")
            + support,
            reply_markup=menu_keyboard(language),
        )

    async def user_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            language = self.store.get_user_language(update.effective_user.id)
            await update.message.reply_text(
                tr(language, f"Telegram ID của bạn: {update.effective_user.id}",
                   f"Your Telegram ID: {update.effective_user.id}")
            )

    async def shop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_shop(update.message, 0)

    async def cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_bank_orders(update.message, update.effective_user.id)

    async def payments(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_bank_orders(update.message, update.effective_user.id)

    async def following(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_following(update.message, update.effective_user.id, 0)

    async def send_following(self, message, user_id: int, page: int = 0):
        language = self.store.get_user_language(user_id)
        products = self.store.list_followed_products(user_id)
        if not products:
            await message.reply_text(
                tr(language,
                   "Bạn chưa theo dõi sản phẩm nào. Mở một sản phẩm và bấm "
                   "🔔 Báo khi có hàng/giảm giá.",
                   "You are not following any products yet. Open a product and tap "
                   "🔔 Stock & sale alerts."),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(tr(language, "🛍 Xem sản phẩm", "🛍 Products"),
                                         callback_data="shop:0"),
                ]]),
            )
            return
        max_page = (len(products) - 1) // PAGE_SIZE
        page = max(0, min(page, max_page))
        visible = products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = [tr(language, f"❤️ ĐANG THEO DÕI · {len(products)} sản phẩm",
                    f"❤️ FOLLOWING · {len(products)} products")]
        rows = []
        for product in visible:
            if product["deleted"] or not product["active"]:
                status = tr(language, "tạm không bán", "unavailable")
            elif product["available"]:
                shown_price = price_html(
                    product["sale_price"] or product["price"],
                    product["price"] if product["sale_price"] else product["previous_price"],
                )
                status = tr(language, f"còn {product['available']} · {shown_price}",
                            f"{product['available']} left · {shown_price}")
            else:
                status = tr(language, "tạm hết hàng", "out of stock")
            lines.append(f"#{product['display_id']} {escape(product['name'])} · {status}")
            buttons = []
            if product["active"] and not product["deleted"]:
                buttons.append(InlineKeyboardButton(
                    tr(language, "Xem", "View"), callback_data=f"product:{product['id']}",
                ))
            buttons.append(InlineKeyboardButton(
                tr(language, "🔕 Bỏ theo dõi", "🔕 Unfollow"),
                callback_data=f"unfollow:{product['id']}",
            ))
            rows.append(buttons)
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                tr(language, "◀ Trước", "◀ Previous"), callback_data=f"following:{page - 1}",
            ))
        if page < max_page:
            navigation.append(InlineKeyboardButton(
                tr(language, "Sau ▶", "Next ▶"), callback_data=f"following:{page + 1}",
            ))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(
            tr(language, "🛍 Xem sản phẩm", "🛍 Products"), callback_data="shop:0",
        )])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                                 parse_mode="HTML")

    async def bundles(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_bundles(update.message, 0)

    async def send_shop(self, message, page: int):
        language = self.language_for_message(message)
        products = sorted(self.store.list_products(), key=lambda item: item["display_id"])
        if not products:
            await message.reply_text(
                tr(language, "Hiện chưa có sản phẩm nào.", "No products are available yet."),
                reply_markup=menu_keyboard(language),
            )
            return
        self.record_funnel_activity(self.message_user_id(message), "catalog_view")
        header = tr(language,
                    "🛍 SẢN PHẨM\nChạm vào sản phẩm để xem chi tiết.",
                    "🛍 PRODUCTS\nTap a product to see details.")
        pages = [products[index:index + SHOP_CATALOG_MAX_PRODUCTS]
                 for index in range(0, len(products), SHOP_CATALOG_MAX_PRODUCTS)]
        page = max(0, min(page, len(pages) - 1))
        visible = pages[page]
        rows = []
        for product in visible:
            name = " ".join(product["name"].split())
            if len(name) > 28:
                name = name[:27] + "…"
            shown_price = product["sale_price"] if product["sale_id"] else product["price"]
            stock = (f"📦 {product['available']}" if product["available"] else
                     tr(language, "📦 Hết hàng", "📦 Sold out"))
            rows.append([InlineKeyboardButton(
                f"#{product['display_id']} {name} · {vnd(shown_price)} · {stock}",
                callback_data=f"product:{product['id']}",
                style="success" if product["available"] else "danger",
            )])
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(tr(language, "◀ Trước", "◀ Previous"),
                                                   callback_data=f"shop:{page - 1}"))
        if page < len(pages) - 1:
            navigation.append(InlineKeyboardButton(tr(language, "Sau ▶", "Next ▶"),
                                                   callback_data=f"shop:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                          callback_data="menu")])
        await message.reply_text(header, reply_markup=InlineKeyboardMarkup(rows))

    async def send_bundles(self, message, page: int):
        language = self.language_for_message(message)
        bundles = self.store.list_bundles()
        if not bundles:
            await message.reply_text(
                tr(language, "Hiện chưa có gói ưu đãi nào.", "No bundles are available yet."),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(tr(language, "Xem sản phẩm", "View products"),
                                         callback_data="shop:0")
                ]]),
            )
            return
        self.record_funnel_activity(self.message_user_id(message), "catalog_view")
        max_page = (len(bundles) - 1) // PAGE_SIZE
        page = max(0, min(page, max_page))
        visible = bundles[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = [tr(language, "🎁 GÓI ƯU ĐÃI", "🎁 BUNDLES"),
                 tr(language, "Mỗi gói tự giao đủ tài khoản sau khi mua.\n",
                    "All accounts in a bundle are delivered automatically after purchase.\n")]
        rows = []
        for bundle in visible:
            status = (tr(language, f"Còn {bundle['available']} gói",
                         f"{bundle['available']} bundles left") if bundle["available"] else
                      tr(language, "Tạm hết hàng", "Out of stock"))
            lines.append(
                f"#{bundle['id']} {escape(bundle['name'])} · "
                f"{price_html(bundle['price'], bundle['previous_price'])} · {status}"
            )
            rows.append([InlineKeyboardButton(
                tr(language, f"Gói #{bundle['id']} {bundle['name'][:30]}",
                   f"Bundle #{bundle['id']} {bundle['name'][:30]}"),
                callback_data=f"bundle:{bundle['id']}",
                style="success" if bundle["available"] else "danger",
            )])
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(tr(language, "◀ Trước", "◀ Previous"),
                                                   callback_data=f"bundles:{page - 1}"))
        if page < max_page:
            navigation.append(InlineKeyboardButton(tr(language, "Sau ▶", "Next ▶"),
                                                   callback_data=f"bundles:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(tr(language, "🛍 Sản phẩm lẻ", "🛍 Products"),
                                          callback_data="shop:0"),
                     InlineKeyboardButton(tr(language, "🏠 Menu", "🏠 Menu"),
                                          callback_data="menu")])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                                 parse_mode="HTML")

    def share_item_button(self, language: str, kind: str, item: dict):
        if not self.bot_username:
            return None
        payload = f"{kind}_{item['id']}"
        deep_link = create_deep_linked_url(self.bot_username, payload)
        label = tr(language, "📤 Chia sẻ", "📤 Share")
        intro = tr(language, "Xem tại shop:", "See this at the shop:")
        share_url = "https://t.me/share/url?" + urlencode({
            "url": deep_link,
            "text": f"{intro} {item['name'][:100]}",
        })
        return InlineKeyboardButton(label, url=share_url)

    def build_product_post(self, product: dict) -> tuple[str, str] | None:
        if not self.bot_username:
            return None
        deep_link = create_deep_linked_url(self.bot_username, f"p_{product['id']}")
        plain_lines = []
        html_lines = []

        def add_line(plain: str, html: str | None = None) -> None:
            plain_lines.append(plain)
            html_lines.append(escape(plain) if html is None else html)

        add_line(f"🛍 {product['name']}")
        description = product["description"].strip()
        if description:
            add_line("")
            add_line(description)
        add_line("")
        if product["sale_id"] and product["sale_remaining"] > 0:
            add_line(f"💰 Giá gốc: {vnd(product['price'])}",
                     f"💰 Giá gốc: <s>{vnd(product['price'])}</s>")
            add_line(f"🔥 Giá sale: {vnd(product['sale_price'])} "
                     f"(-{sale_percent(product['price'], product['sale_price'])})")
        elif product["previous_price"] and product["previous_price"] > product["price"]:
            add_line(f"💰 Giá cũ: {vnd(product['previous_price'])}",
                     f"💰 Giá cũ: <s>{vnd(product['previous_price'])}</s>")
            add_line(f"🔥 Giá mới: {vnd(product['price'])}")
        else:
            add_line(f"💰 Giá: {vnd(product['price'])}")
        add_line("📦 Còn hàng")
        if product["requires_email"]:
            add_line("📧 Cần nhập email khi mua.")
        add_line("")
        add_line("Giá và tình trạng hàng được cập nhật khi mở bot.")
        body = "\n".join(plain_lines)
        html_body = "\n".join(html_lines)
        post = f"{html_body}\n\n🛒 Mua ngay: {escape(deep_link)}"
        share_url = "https://t.me/share/url?" + urlencode({
            "url": deep_link, "text": body,
        })
        return post, share_url

    async def send_bundle_detail(self, message, user_id: int, bundle_id: int):
        language = self.store.get_user_language(user_id)
        bundle = self.store.get_bundle(bundle_id)
        if not bundle or not bundle["active"] or bundle["deleted"]:
            await message.reply_text(
                tr(language, "Gói không còn bán.", "This bundle is no longer available."),
                reply_markup=menu_keyboard(language),
            )
            return
        self.record_funnel_activity(user_id, "catalog_view")
        components = "\n".join(
            f"• {item['quantity']} × {escape(item['name'])}" for item in bundle["items"]
        )
        saving = bundle["regular_price"] - bundle["price"]
        rows = [[InlineKeyboardButton(tr(language, "🛒 Mua gói", "🛒 Buy bundle"),
                                      callback_data=f"buybundle:{bundle_id}")]] if bundle["available"] else []
        if bundle["available"]:
            share_button = self.share_item_button(language, "b", bundle)
            if share_button:
                rows.append([share_button])
        rows.append([InlineKeyboardButton(tr(language, "⬅️ Các gói khác", "⬅️ Other bundles"),
                                         callback_data="bundles:0"),
                     InlineKeyboardButton("🏠 Menu", callback_data="menu")])
        await message.reply_text(
            f"🎁 {escape(bundle['name'])}\n{escape(bundle['description'])}\n\n"
            + tr(language, f"Gồm:\n{components}\n\n", f"Includes:\n{components}\n\n")
            + (tr(language, "📧 Cần email khi mua gói này.\n",
                  "📧 An email is required for this bundle.\n")
               if any(item["requires_email"] for item in bundle["items"]) else "")
            + tr(language,
                 f"Giá gói: {price_html(bundle['price'], bundle['previous_price'])}",
                 f"Bundle price: {price_html(bundle['price'], bundle['previous_price'])}")
            + (tr(language, f" · tiết kiệm {vnd(saving)}", f" · save {vnd(saving)}")
               if saving > 0 else "")
            + (tr(language, f"\nCòn {bundle['available']} gói",
                  f"\n{bundle['available']} bundles left") if bundle["available"] else
               tr(language, "\nTạm hết hàng", "\nOut of stock")),
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="HTML",
        )

    async def send_product_detail(self, message, user_id: int, product_id: int,
                                  *, count_view: bool = True):
        language = self.store.get_user_language(user_id)
        product = self.store.get_product(product_id)
        if not product or not product["active"] or product["deleted"]:
            await message.reply_text(
                tr(language, "Sản phẩm không còn bán.",
                   "This product is no longer available."),
                reply_markup=menu_keyboard(language),
            )
            return
        self.record_funnel_activity(user_id, "catalog_view")
        rows = ([[InlineKeyboardButton(
            tr(language, "🛒 Mua ngay", "🛒 Buy now"),
            callback_data=f"buyqty:{product_id}",
            style="success",
        )]] if product["available"] else [])
        following = self.store.is_following_product(user_id, product_id)
        rows.append([InlineKeyboardButton(
            tr(language, "🔕 Bỏ theo dõi", "🔕 Unfollow") if following else
            tr(language, "🔔 Báo khi có hàng/giảm giá", "🔔 Stock & sale alerts"),
            callback_data=f"unfollow:{product_id}" if following else f"follow:{product_id}",
        )])
        if product["available"]:
            share_button = self.share_item_button(language, "p", product)
            if share_button:
                rows.append([share_button])
        rows.append([InlineKeyboardButton(tr(language, "⬅️ Xem sản phẩm khác", "⬅️ Other products"),
                                         callback_data="shop:0"),
                     InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                          callback_data="menu")])
        status = (tr(language,
                     f"Còn {product['available']} {'file' if product['delivery_type'] == 'file' else 'tài khoản'}",
                     f"{product['available']} {'files' if product['delivery_type'] == 'file' else 'accounts'} left")
                  if product["available"] else
                  tr(language, "Tạm hết hàng · chưa thể mua",
                     "Out of stock · not available for purchase"))
        sale = (
            tr(language,
               f"Giá gốc: <s>{vnd(product['price'])}</s>\n"
               f"🔥 Giá sale: {vnd(product['sale_price'])} "
               f"(-{sale_percent(product['price'], product['sale_price'])})\n"
               f"Còn {product['sale_remaining']} suất sale",
               f"Regular price: <s>{vnd(product['price'])}</s>\n"
               f"🔥 Sale price: {vnd(product['sale_price'])} "
               f"(-{sale_percent(product['price'], product['sale_price'])})\n"
               f"{product['sale_remaining']} sale units left")
            if product["sale_id"] else
            tr(language,
               f"Giá: {price_html(product['price'], product['previous_price'])}",
               f"Price: {price_html(product['price'], product['previous_price'])}")
        )
        await message.reply_text(
            f"🛍 #{product['display_id']} {escape(product['name'])}\n\n"
            f"{escape(product['description'])}\n\n"
            + (tr(language, "📧 Cần email khi mua.\n",
                  "📧 An email is required to buy this item.\n")
               if product["requires_email"] else "")
            + tr(language,
                 f"{sale}\nTrạng thái: {status}",
                 f"{sale}\nStatus: {status}")
            + (tr(language, f"\nĐã bán: {product['sold']}",
                  f"\nSold: {product['sold']}") if product["sold"] > 0 else "")
            + (tr(language, "\n🔔 Đang theo dõi sản phẩm này.",
                  "\n🔔 You follow this product.") if following else ""),
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="HTML",
        )
        if count_view and user_id not in self.config.admin_ids:
            try:
                self.store.record_product_view(user_id, product_id)
            except Exception:
                log.exception("Could not record product view")

    async def send_product_quantity_options(self, message, user_id: int,
                                            product_id: int, context):
        language = self.store.get_user_language(user_id)
        product = self.store.get_product(product_id)
        if not product or not product["active"] or product["deleted"] or not product["available"]:
            await message.reply_text(tr(language,
                "Sản phẩm đã hết hàng hoặc ngừng bán.",
                "This product is out of stock or unavailable."))
            return
        for key in ("product_checkout", "awaiting_buy_quantity", "awaiting_buy_email",
                    "awaiting_buy_coupon", "pending_purchase"):
            context.user_data.pop(key, None)
        limit = min(MAX_PURCHASE_QUANTITY, product["available"])
        buttons = [InlineKeyboardButton(str(quantity),
                    callback_data=f"buyqty:{product_id}:{quantity}")
                   for quantity in PURCHASE_QUANTITY_PRESETS if quantity <= limit]
        buttons.append(InlineKeyboardButton(tr(language, "✏️ Số khác", "✏️ Other quantity"),
                    callback_data=f"buyqty:{product_id}:other"))
        rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
        rows.append([InlineKeyboardButton(tr(language, "⬅️ Sản phẩm", "⬅️ Product"),
                                          callback_data=f"product:{product_id}")])
        await message.reply_text(tr(language,
            f"Chọn số lượng {product['name']} (1–{limit}):",
            f"Choose the quantity of {product['name']} (1–{limit}):"),
            reply_markup=InlineKeyboardMarkup(rows))

    async def choose_product_quantity(self, message, user_id: int, product_id: int,
                                      quantity: int, context):
        language = self.store.get_user_language(user_id)
        try:
            quote = self.store.quote_product_quantity(user_id, product_id, quantity)
        except (NotFound, OutOfStock, ValueError):
            await message.reply_text(tr(language,
                "Số lượng không hợp lệ hoặc kho không đủ. Hãy chọn lại ở sản phẩm.",
                "Invalid quantity or insufficient stock. Choose again from the product."))
            return
        context.user_data["product_checkout"] = {
            "product_id": product_id, "quantity": quantity,
            "customer_email": None, "coupon_code": None,
        }
        context.user_data.pop("awaiting_buy_quantity", None)
        context.user_data.pop("awaiting_buy_email", None)
        context.user_data.pop("awaiting_buy_coupon", None)
        if quote["missing_email"]:
            context.user_data["awaiting_buy_email"] = True
            await message.reply_text(tr(language,
                f"📧 Gửi đúng {quantity} email cho đơn này, cách nhau bằng dấu phẩy "
                "(ví dụ a@example.com, b@example.com). Gửi /cancel để hủy.",
                f"📧 Send exactly {quantity} email address(es), separated by commas "
                "(for example a@example.com, b@example.com). Send /cancel to stop."))
            return
        await self.send_product_checkout(message, user_id, context)

    async def send_product_checkout(self, message, user_id: int, context,
                                    *, send_direct: bool = False):
        language = self.store.get_user_language(user_id)
        state = context.user_data.get("product_checkout")
        if not state:
            await message.reply_text(tr(language, "Hãy chọn sản phẩm để mua.",
                                        "Choose a product to buy."))
            return
        quote = self.store.quote_product_quantity(
            user_id, state["product_id"], state["quantity"],
            state["coupon_code"], state["customer_email"],
        )
        if quote["missing_email"]:
            context.user_data["awaiting_buy_email"] = True
            await message.reply_text(tr(language, "Hãy gửi email cho đơn này.",
                                        "Send the email address(es) for this order."))
            return
        token = secrets.token_hex(6)
        state["token"] = token
        line = quote["items"][0]
        subtotal = quote["subtotal"]
        discount_line = (tr(language,
            f"\nMã {quote['coupon_code']}: -{vnd(quote['discount'])}",
            f"\nCoupon {quote['coupon_code']}: -{vnd(quote['discount'])}")
            if quote["discount"] else "")
        email_line = (tr(language,
            f"\nEmail xử lý: {state['customer_email']}",
            f"\nOrder email: {state['customer_email']}")
            if state["customer_email"] else "")
        rows = [
            [InlineKeyboardButton(tr(language, "🏦 Chuyển khoản ngân hàng",
                                     "🏦 Pay by bank transfer"),
                                  callback_data=f"buyflow:bank:{token}")],
            [InlineKeyboardButton(tr(language, "💰 Thanh toán bằng ví",
                                     "💰 Pay with balance"),
                                  callback_data=f"buyflow:wallet:{token}")],
        ]
        if not line["sale_units"]:
            rows.append([InlineKeyboardButton(tr(language, "🏷 Nhập mã giảm giá",
                                                  "🏷 Apply coupon"),
                                              callback_data=f"buyflow:coupon:{token}")])
        if state["coupon_code"]:
            rows.append([InlineKeyboardButton(tr(language, "Bỏ mã giảm giá", "Remove coupon"),
                                              callback_data=f"buyflow:clear:{token}")])
        if state["customer_email"]:
            rows.append([InlineKeyboardButton(tr(language, "✏️ Sửa email", "✏️ Change email"),
                                              callback_data=f"buyflow:email:{token}")])
        rows.append([InlineKeyboardButton(tr(language, "⬅️ Đổi số lượng", "⬅️ Change quantity"),
                                          callback_data=f"buyflow:back:{token}")])
        confirmation = tr(language,
            f"🛍 {line['name']}\nSố lượng: {state['quantity']}\n"
            f"Tạm tính: {vnd(subtotal)}{discount_line}{email_line}\n"
            f"Cần thanh toán: {vnd(quote['price'])}\n"
            f"Số dư ví: {vnd(self.store.get_balance(user_id))}\n\n"
            "Chọn cách thanh toán bên dưới. Bấm Thanh toán bằng ví sẽ trừ tiền ngay.",
            f"🛍 {line['name']}\nQuantity: {state['quantity']}\n"
            f"Subtotal: {vnd(subtotal)}{discount_line}{email_line}\n"
            f"Total due: {vnd(quote['price'])}\n"
            f"Balance: {vnd(self.store.get_balance(user_id))}\n\n"
            "Choose a payment method below. Pay with balance charges immediately.")
        options = {"reply_markup": InlineKeyboardMarkup(rows),
                   "protect_content": bool(state["customer_email"])}
        if send_direct:
            await context.bot.send_message(user_id, confirmation, **options)
        else:
            await message.reply_text(confirmation, **options)

    async def product_checkout_callback(self, message, data: str, context, user_id: int):
        language = self.store.get_user_language(user_id)
        parts = data.split(":")
        if len(parts) != 3:
            return
        _, action, token = parts
        state = context.user_data.get("product_checkout")
        if not state or state.get("token") != token:
            await message.reply_text(tr(language,
                "Bước thanh toán đã hết hiệu lực. Hãy mở lại sản phẩm và chọn số lượng.",
                "This checkout expired. Open the product and choose a quantity again."))
            return
        if action == "back":
            await self.send_product_quantity_options(message, user_id, state["product_id"], context)
            return
        if action == "email":
            state["token"] = None
            context.user_data["awaiting_buy_email"] = True
            await message.reply_text(tr(language,
                f"Gửi đúng {state['quantity']} email mới, cách nhau bằng dấu phẩy; /cancel để hủy.",
                f"Send exactly {state['quantity']} new email address(es), comma separated; /cancel to stop."))
            return
        if action == "coupon":
            state["token"] = None
            context.user_data["awaiting_buy_coupon"] = True
            await message.reply_text(tr(language,
                "Gửi mã giảm giá hoặc /cancel để hủy.",
                "Send your coupon code or /cancel to stop."))
            return
        if action == "clear":
            state["coupon_code"] = None
            await self.send_product_checkout(message, user_id, context)
            return
        if action not in ("bank", "wallet"):
            return
        try:
            quote = self.store.quote_product_quantity(
                user_id, state["product_id"], state["quantity"],
                state["coupon_code"], state["customer_email"],
            )
            if quote["missing_email"]:
                raise EmailRequired("Missing email")
        except (NotFound, OutOfStock, CouponUnavailable, SaleCouponConflict,
                EmailRequired, ValueError):
            context.user_data.pop("product_checkout", None)
            await message.reply_text(tr(language,
                "Sản phẩm, kho hoặc mã giảm giá đã thay đổi. Hãy chọn mua lại.",
                "The product, stock, or coupon changed. Start the purchase again."))
            return
        if action == "bank" and not self.limiter.allow_topup(user_id):
            await message.reply_text(tr(language,
                "Bạn tạo mã chuyển khoản quá nhanh. Hãy thử lại sau khoảng 15 giây.",
                "You are creating bank codes too quickly. Try again in about 15 seconds."))
            return
        context.user_data.pop("product_checkout", None)
        if action == "bank":
            try:
                checkout = self.store.create_direct_checkout_product(
                    user_id, state["product_id"], state["quantity"], quote["price"],
                    self.config.max_topup, state["coupon_code"], state["customer_email"],
                )
            except TopupLimitReached:
                await message.reply_text(tr(language,
                    "Bạn đã tạo quá nhiều đơn chuyển khoản trong 1 giờ. Hãy thử lại sau.",
                    "Too many bank orders in one hour. Try again later."))
                return
            except (NotFound, OutOfStock, CouponUnavailable, SaleCouponConflict,
                    EmailRequired, PriceChanged, ValueError):
                await message.reply_text(tr(language,
                    "Giá, kho hoặc mã giảm giá đã thay đổi; chưa tạo đơn. Hãy chọn mua lại.",
                    "The price, stock, or coupon changed; no order was created. Start again."))
                return
            await self.send_direct_checkout(message, user_id, checkout["id"])
            return
        try:
            purchase = self.store.purchase_product_quantity(
                user_id, state["product_id"], state["quantity"], quote["price"],
                state["coupon_code"], state["customer_email"], self.config.admin_ids,
            )
        except InsufficientBalance:
            context.user_data["product_checkout"] = state
            await message.reply_text(tr(language,
                "Số dư chưa đủ. Bạn có thể chọn chuyển khoản ngân hàng bên dưới.",
                "Insufficient balance. You can choose bank transfer below."))
            await self.send_product_checkout(message, user_id, context)
            return
        except (NotFound, OutOfStock, CouponUnavailable, SaleCouponConflict,
                EmailRequired, PriceChanged, ValueError):
            await message.reply_text(tr(language,
                "Giá, kho hoặc mã giảm giá đã thay đổi; chưa trừ tiền. Hãy chọn mua lại.",
                "The price, stock, or coupon changed; no money was deducted. Start again."))
            return
        await self.send_purchased_accounts(message, user_id, purchase)

    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_balance(update.message, update.effective_user.id)

    async def send_balance(self, message, user_id: int):
        language = self.store.get_user_language(user_id)
        balance = vnd(self.store.get_balance(user_id))
        await message.reply_text(
            tr(language, f"Số dư của bạn: {balance}", f"Your balance: {balance}"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(tr(language, "Nạp tiền", "Add funds"), callback_data="topup"),
                 InlineKeyboardButton(tr(language, "Mua tài khoản", "Buy account"),
                                      callback_data="shop:0")],
                [InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                      callback_data="menu")],
            ]),
        )

    async def send_bank_orders(self, message, user_id: int):
        language = self.store.get_user_language(user_id)
        recent = self.store.recent_direct_checkouts(user_id, 10)
        if not recent:
            await message.reply_text(tr(language,
                "Bạn chưa có đơn chuyển khoản trực tiếp.",
                "You have no direct bank orders yet."),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(tr(language, "🛍 Xem sản phẩm", "🛍 Products"),
                                         callback_data="shop:0")]]))
            return
        rows = []
        for checkout in recent:
            state = (tr(language, "đã giao", "delivered")
                     if checkout["status"] == "fulfilled" else
                     tr(language, "chờ chuyển khoản", "awaiting payment")
                     if checkout["status"] == "open" else
                     tr(language, "đã hết hạn/không giao", "expired/not delivered"))
            rows.append([InlineKeyboardButton(
                tr(language,
                   f"Đơn #{checkout['id']} · {vnd(checkout['expected_amount'])} · {state}",
                   f"Order #{checkout['id']} · {vnd(checkout['expected_amount'])} · {state}"),
                callback_data=f"cart:checkout:{checkout['id']}")])
        rows.append([InlineKeyboardButton(tr(language, "🛍 Xem sản phẩm", "🛍 Products"),
                                          callback_data="shop:0")])
        await message.reply_text(tr(language, "🏦 Đơn chuyển khoản gần đây:",
                                    "🏦 Recent bank orders:"),
                                 reply_markup=InlineKeyboardMarkup(rows))

    async def send_direct_checkout(self, message, user_id: int, checkout_id: int):
        language = self.store.get_user_language(user_id)
        checkout = self.store.get_direct_checkout(user_id, checkout_id)
        if not checkout:
            await message.reply_text(tr(language, "Không tìm thấy đơn chuyển khoản.",
                                        "Bank order not found."))
            return
        items = ", ".join(
            f"#{item['display_id']} {item['name']} × {item['quantity']}"
            for item in checkout["items"]
        )
        if checkout["status"] != "open":
            state = {
                "fulfilled": tr(language, "đã thanh toán và giao hàng", "paid and delivered"),
                "failed": tr(language, "không giao được; tiền ở ví", "not fulfilled; funds in balance"),
                "expired": tr(language, "đã hết hạn; tiền đến sau vào ví",
                              "expired; late funds go to balance"),
            }[checkout["status"]]
            await message.reply_text(
                tr(language,
                   f"Đơn #{checkout_id}: {state}\n{items}\n"
                   f"Đã nhận: {vnd(checkout['paid_amount'])}. Xem /orders hoặc /balance.",
                   f"Order #{checkout_id}: {state}\n{items}\n"
                   f"Received: {vnd(checkout['paid_amount'])}. See /orders or /balance."),
            )
            return
        transfer_account = (self.config.bank_transfer_account_number
                            or self.config.bank_account_number)
        expires = status_time(parse_stored_time(checkout["expires_at"]))
        remaining = max(0, checkout["expected_amount"] - checkout["paid_amount"])
        instructions = tr(language,
            f"🛒 THANH TOÁN ĐƠN #{checkout_id}\n{items}\n\n"
            f"Ngân hàng: {self.config.bank_name}\n"
            f"Số tài khoản: {transfer_account}\n"
            f"Chủ tài khoản: {self.config.bank_account_holder}\n"
            f"Chuyển khoản: {vnd(remaining)}\n"
            f"Nội dung: {checkout['code']}\n"
            f"Hạn thanh toán: {expires} (GMT+7)\n\n"
            "Giữ nguyên nội dung và chuyển đúng số tiền. Bot tự giao hàng sau khi "
            "SePay xác nhận. Nếu đơn hết hạn, giá/kho thay đổi hoặc tiền chưa đủ, "
            "tiền đã nhận vẫn nằm trong số dư của bạn.",
            f"🛒 PAY FOR ORDER #{checkout_id}\n{items}\n\n"
            f"Bank: {self.config.bank_name}\n"
            f"Account: {transfer_account}\n"
            f"Account holder: {self.config.bank_account_holder}\n"
            f"Transfer: {vnd(remaining)}\n"
            f"Note: {checkout['code']}\n"
            f"Deadline: {expires} (GMT+7)\n\n"
            "Keep the note and transfer the exact amount. The bot delivers after SePay "
            "confirms payment. If the order expires, price/stock changes, or payment "
            "is short, received funds remain in your balance.")
        rows = InlineKeyboardMarkup([[
            InlineKeyboardButton(tr(language, "🏦 Đơn chuyển khoản", "🏦 Bank orders"),
                                 callback_data="payments"),
            InlineKeyboardButton(tr(language, "📦 Đơn hàng", "📦 Orders"), callback_data="orders"),
        ]])
        if self.config.bank_bin:
            try:
                image = await asyncio.to_thread(
                    vietqr_png, self.config.bank_bin, transfer_account,
                    remaining, checkout["code"],
                )
                caption = instructions
                if telegram_text_units(caption) > 1000:
                    caption = tr(language,
                        f"🛒 Đơn #{checkout_id} · {vnd(remaining)}\n"
                        f"Nội dung: {checkout['code']}\nHạn: {expires} (GMT+7)",
                        f"🛒 Order #{checkout_id} · {vnd(remaining)}\n"
                        f"Note: {checkout['code']}\nDeadline: {expires} (GMT+7)")
                await message.reply_photo(
                    photo=InputFile(BytesIO(image), filename="vietqr.png"),
                    caption=caption, reply_markup=rows,
                )
                if caption != instructions:
                    await message.reply_text(instructions)
                return
            except (OSError, ValueError, TelegramError) as exc:
                log.warning("Could not send direct checkout VietQR: %s", type(exc).__name__)
        await message.reply_text(instructions, reply_markup=rows)

    async def send_purchased_accounts(self, message, user_id: int, purchase: dict):
        language = self.store.get_user_language(user_id)
        kind = (tr(language, "sản phẩm", "item(s)") if any(
            order.get("delivery_type") == "file" for order in purchase["orders"]
        ) else tr(language, "tài khoản", "account(s)"))
        introduction = tr(language,
            f"Đã thanh toán {vnd(purchase['price'])} cho {len(purchase['orders'])} {kind}. "
            f"Số dư còn lại: {vnd(purchase['balance'])}.",
            f"Paid {vnd(purchase['price'])} for {len(purchase['orders'])} {kind}. "
            f"Remaining balance: {vnd(purchase['balance'])}.")
        await self.send_order_delivery_reply(
            message, language, purchase["orders"], introduction,
        )

    async def send_order_delivery_reply(self, message, language: str,
                                        orders: list[dict], introduction: str,
                                        warranty_ticket: dict | None = None) -> None:
        content, entities, markup = account_delivery_payload(
            orders, language, introduction, warranty_ticket,
        )
        await message.reply_text(content, entities=entities, reply_markup=markup,
                                 protect_content=True)
        first_id = orders[0]["id"]
        document_bytes, extension = order_delivery_file(orders, language)
        filename = f"don-hang-{first_id}{extension}"
        has_file_product = any(order.get("delivery_type") == "file" for order in orders)
        try:
            await message.reply_document(
                document=InputFile(BytesIO(document_bytes),
                                   filename=filename),
                caption=tr(language,
                           f"📄 File sản phẩm đã mua ({extension})" if has_file_product
                           else "📄 File tài khoản đã mua",
                           f"📄 Purchased product file ({extension})" if has_file_product
                           else "📄 Purchased accounts text file"),
            )
            log.info("Sent %s order file for purchase %s", extension, first_id)
        except TelegramError as exc:
            log.warning("Could not send %s order file for purchase %s: %s", extension,
                        first_id, type(exc).__name__)
            await message.reply_text(tr(language,
                f"Chưa gửi được file {extension}. Mở /orders rồi bấm “📄 Tải file {extension}” để thử lại.",
                f"The {extension} file could not be sent. Open /orders and tap “📄 Download {extension} file” to retry."))

    async def cart_callback(self, message, data: str, context, user_id: int):
        language = self.store.get_user_language(user_id)
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "checkout" and len(parts) == 3:
            try:
                checkout_id = int(parts[2])
            except ValueError:
                return
            await self.send_direct_checkout(message, user_id, checkout_id)
            return
        if action == "add" and len(parts) == 3:
            try:
                product_id = int(parts[2])
            except ValueError:
                return
            await self.send_product_quantity_options(message, user_id, product_id, context)
            return
        if action == "view":
            await self.send_bank_orders(message, user_id)
            return
        await message.reply_text(tr(language,
            "Giao diện giỏ hàng đã đổi. Hãy mở sản phẩm và chọn số lượng để mua.",
            "The cart interface has changed. Open a product and choose a quantity."),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(tr(language, "🛍 Xem sản phẩm", "🛍 Products"),
                                     callback_data="shop:0")]]))

    async def topup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_private(update):
            return
        context.user_data.pop("awaiting_topup", None)
        context.user_data.pop("awaiting_warranty", None)
        context.user_data.pop("awaiting_bulk_stock", None)
        context.user_data.pop("awaiting_coupon", None)
        context.user_data.pop("awaiting_order_email", None)
        if context.args:
            try:
                amount = parse_amount(context.args[0])
                await self.create_topup(update.message, update.effective_user.id, amount)
            except ValueError:
                language = self.store.get_user_language(update.effective_user.id)
                await update.message.reply_text(
                    tr(language,
                       f"Nhập số tiền từ {vnd(self.config.min_topup)} đến "
                       f"{vnd(self.config.max_topup)}, ví dụ /nap 50000.",
                       f"Enter an amount from {vnd(self.config.min_topup)} to "
                       f"{vnd(self.config.max_topup)}, for example /nap 50000.")
                )
            return
        await self.send_topup_options(update.message)

    async def send_topup_options(self, message):
        language = self.language_for_message(message)
        buttons = [
            InlineKeyboardButton(vnd(amount), callback_data=f"topup:amount:{amount}")
            for amount in TOPUP_PRESETS
            if self.config.min_topup <= amount <= self.config.max_topup
        ]
        buttons.append(InlineKeyboardButton(tr(language, "✏️ Số khác", "✏️ Other amount"),
                                            callback_data="topup:other"))
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                          callback_data="menu")])
        await message.reply_text(
            tr(language,
               f"Chọn số tiền muốn nạp ({vnd(self.config.min_topup)}–"
               f"{vnd(self.config.max_topup)}). Chọn Số khác để tự nhập.",
               f"Choose an amount to add ({vnd(self.config.min_topup)}–"
               f"{vnd(self.config.max_topup)}). Select Other amount to enter your own."),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            context.user_data.pop("awaiting_topup", None)
            context.user_data.pop("awaiting_warranty", None)
            context.user_data.pop("awaiting_bulk_stock", None)
            context.user_data.pop("awaiting_file_stock", None)
            context.user_data.pop("awaiting_coupon", None)
            context.user_data.pop("awaiting_order_email", None)
            context.user_data.pop("awaiting_export_range", None)
            context.user_data.pop("pending_purchase", None)
            context.user_data.pop("pending_bundle", None)
            context.user_data.pop("awaiting_manual_credit", None)
            context.user_data.pop("pending_manual_credit", None)
            self.clear_admin_test_credit_state(context)
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("pending_broadcast", None)
            context.user_data.pop("awaiting_admin_user_search", None)
            context.user_data.pop("admin_user_search_query", None)
            context.user_data.pop("awaiting_admin_sales_search", None)
            context.user_data.pop("admin_sales_search_query", None)
            context.user_data.pop("awaiting_admin_order_message", None)
            context.user_data.pop("awaiting_customer_order_reply", None)
            context.user_data.pop("awaiting_display_id", None)
            context.user_data.pop("admin_product_flow", None)
            context.user_data.pop("admin_offer_flow", None)
            context.user_data.pop("product_checkout", None)
            context.user_data.pop("awaiting_buy_quantity", None)
            context.user_data.pop("awaiting_buy_email", None)
            context.user_data.pop("awaiting_buy_coupon", None)
            language = self.store.get_user_language(update.effective_user.id)
            await update.message.reply_text(
                tr(language, "Đã hủy thao tác.", "Action cancelled."),
                reply_markup=menu_keyboard(language),
            )

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_private(update):
            return
        language = self.store.get_user_language(update.effective_user.id)
        if "awaiting_file_stock" in context.user_data and update.effective_user.id in self.config.admin_ids:
            await update.message.reply_text("Hãy gửi file .json dưới dạng tài liệu, hoặc /cancel để dừng nhập.")
            return
        if "awaiting_admin_order_message" in context.user_data:
            await self.send_admin_order_message(update, context)
            return
        if "awaiting_customer_order_reply" in context.user_data:
            await self.send_customer_order_reply(update, context)
            return
        if "awaiting_buy_quantity" in context.user_data:
            product_id = context.user_data["awaiting_buy_quantity"]
            raw = update.message.text.strip()
            if not raw.isascii() or not raw.isdecimal():
                await update.message.reply_text(tr(language,
                    "Nhập số lượng nguyên từ 1 đến 10 hoặc /cancel.",
                    "Enter a whole quantity from 1 to 10 or /cancel."))
                return
            await self.choose_product_quantity(
                update.message, update.effective_user.id, product_id, int(raw), context,
            )
            return
        if context.user_data.get("awaiting_buy_email"):
            state = context.user_data.get("product_checkout")
            if not state:
                context.user_data.pop("awaiting_buy_email", None)
                return
            deleted = await self.delete_private_input(update.message)
            warning = (tr(language, " Hãy tự xóa tin nhắn email vừa gửi.",
                          " Please delete the email message you sent.") if not deleted else "")
            try:
                normalized = normalize_customer_email(update.message.text)
                if len(normalized.split(", ")) != state["quantity"]:
                    raise EmailRequired("Email count must match quantity")
                self.store.quote_product_quantity(
                    update.effective_user.id, state["product_id"], state["quantity"],
                    state["coupon_code"], normalized,
                )
            except (EmailRequired, ValueError):
                await context.bot.send_message(update.effective_user.id,
                    tr(language,
                       f"Email chưa hợp lệ. Gửi đúng {state['quantity']} email, cách nhau bằng dấu phẩy; /cancel để hủy.",
                       f"Invalid email. Send exactly {state['quantity']} email address(es), comma separated; /cancel to stop.") + warning)
                return
            except (NotFound, OutOfStock, CouponUnavailable, SaleCouponConflict):
                context.user_data.pop("product_checkout", None)
                context.user_data.pop("awaiting_buy_email", None)
                await context.bot.send_message(update.effective_user.id,
                    tr(language, "Sản phẩm hoặc mã giảm giá đã thay đổi. Hãy chọn mua lại.",
                       "The product or coupon changed. Start the purchase again.") + warning)
                return
            state["customer_email"] = normalized
            context.user_data.pop("awaiting_buy_email", None)
            await self.send_product_checkout(update.message, update.effective_user.id, context,
                                             send_direct=True)
            if warning:
                await context.bot.send_message(update.effective_user.id, warning.strip())
            return
        if context.user_data.get("awaiting_buy_coupon"):
            state = context.user_data.get("product_checkout")
            if not state:
                context.user_data.pop("awaiting_buy_coupon", None)
                return
            code = update.message.text.strip().upper()
            try:
                self.store.quote_product_quantity(
                    update.effective_user.id, state["product_id"], state["quantity"],
                    code, state["customer_email"],
                )
            except (CouponUnavailable, SaleCouponConflict, ValueError):
                await update.message.reply_text(tr(language,
                    "Mã không áp dụng, đã hết hạn/lượt hoặc không dùng cùng sale. Gửi mã khác hoặc /cancel.",
                    "This coupon is invalid, expired, used up, or conflicts with a sale. Try another or /cancel."))
                return
            except (NotFound, OutOfStock):
                context.user_data.pop("product_checkout", None)
                context.user_data.pop("awaiting_buy_coupon", None)
                await update.message.reply_text(tr(language,
                    "Sản phẩm đã hết hàng hoặc ngừng bán. Hãy chọn mua lại.",
                    "This product is out of stock or unavailable. Start again."))
                return
            state["coupon_code"] = code
            context.user_data.pop("awaiting_buy_coupon", None)
            await self.send_product_checkout(update.message, update.effective_user.id, context)
            return
        if "admin_offer_flow" in context.user_data:
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("admin_offer_flow", None)
                return
            await self.admin_offer_text(update.message, context, update.message.text)
            return
        if "admin_product_flow" in context.user_data:
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("admin_product_flow", None)
                return
            await self.admin_catalog_text(update.message, context, update.message.text)
            return
        if context.user_data.get("awaiting_broadcast"):
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("awaiting_broadcast", None)
                return
            await self.prepare_broadcast(update.message, context, update.message.text)
            return
        if "awaiting_display_id" in context.user_data:
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("awaiting_display_id", None)
                return
            await self.change_product_display_id(
                update.message, context.user_data["awaiting_display_id"],
                update.message.text.strip(), context,
            )
            return
        if context.user_data.get("awaiting_export_range"):
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("awaiting_export_range", None)
                return
            await self.send_admin_export(update.message, context, update.message.text)
            return
        if "awaiting_manual_credit" in context.user_data:
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("awaiting_manual_credit", None)
                return
            await self.prepare_manual_credit(
                update.message, context, context.user_data["awaiting_manual_credit"],
            )
            return
        if context.user_data.get("awaiting_admin_user_search"):
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("awaiting_admin_user_search", None)
                context.user_data.pop("admin_user_search_query", None)
                return
            await self.receive_admin_user_search(update.message, context,
                                                 update.message.text)
            return
        if context.user_data.get("awaiting_admin_sales_search"):
            if update.effective_user.id not in self.config.admin_ids:
                context.user_data.pop("awaiting_admin_sales_search", None)
                context.user_data.pop("admin_sales_search_query", None)
                return
            await self.receive_admin_sales_search(update.message, context,
                                                  update.message.text)
            return
        if context.user_data.get("awaiting_admin_credit_target"):
            if update.effective_user.id not in self.config.admin_ids:
                self.clear_admin_test_credit_state(context)
                return
            await self.lookup_admin_test_credit_target(
                update.message, context, update.message.text,
            )
            return
        if "awaiting_admin_credit_amount" in context.user_data:
            if update.effective_user.id not in self.config.admin_ids:
                self.clear_admin_test_credit_state(context)
                return
            await self.receive_admin_test_credit_amount(
                update.message, context, update.message.text,
            )
            return
        if "awaiting_bulk_stock" in context.user_data:
            await self.save_bulk_stock(update, context, context.user_data["awaiting_bulk_stock"], update.message.text)
            return
        if "awaiting_warranty" in context.user_data:
            await self.save_warranty(update, context, context.user_data["awaiting_warranty"])
            return
        if "awaiting_order_email" in context.user_data:
            await self.save_order_email(update, context)
            return
        if "awaiting_coupon" in context.user_data:
            coupon_state = context.user_data["awaiting_coupon"]
            target, target_id = coupon_state[:2]
            customer_email = coupon_state[2] if len(coupon_state) > 2 else None
            code = update.message.text.strip().upper()
            try:
                await self.send_purchase_confirmation(
                    update.message, update.effective_user.id, context,
                    target, target_id, code, customer_email,
                )
            except SaleCouponConflict:
                await update.message.reply_text(
                    tr(language,
                       "Sản phẩm đang sale nên không dùng đồng thời mã giảm giá. "
                       "Bấm Mua ngay ở sản phẩm để xem giá sale.",
                       "Coupons cannot be combined with this product's sale price. "
                       "Tap Buy now on the product to see the sale price.")
                )
                context.user_data.pop("awaiting_coupon", None)
                return
            except CouponUnavailable:
                await update.message.reply_text(
                    tr(language,
                       "Mã không hợp lệ, không áp dụng cho sản phẩm/gói này, "
                       "đã hết lượt, đã hết hạn hoặc bạn đã dùng. "
                       "Gửi mã khác hoặc /cancel.",
                       "This coupon is invalid, unavailable for this item, expired, used up, "
                       "or already used by you. Send another code or /cancel.")
                )
                return
            except (NotFound, OutOfStock):
                await update.message.reply_text(tr(
                    language, "Sản phẩm hoặc gói đã hết hàng / ngừng bán.",
                    "This product or bundle is out of stock or unavailable.",
                ))
            context.user_data.pop("awaiting_coupon", None)
            return
        if not context.user_data.get("awaiting_topup"):
            await update.message.reply_text(
                tr(language, "Chọn chức năng bên dưới hoặc gửi /help.",
                   "Choose an option below or send /help."),
                reply_markup=menu_keyboard(language),
            )
            return
        try:
            amount = parse_amount(update.message.text)
            await self.create_topup(update.message, update.effective_user.id, amount)
        except ValueError:
            await update.message.reply_text(
                tr(language,
                   f"Số tiền không hợp lệ. Nhập từ {vnd(self.config.min_topup)} "
                   f"đến {vnd(self.config.max_topup)}, ví dụ 50000.",
                   f"Invalid amount. Enter {vnd(self.config.min_topup)} to "
                   f"{vnd(self.config.max_topup)}, for example 50000.")
            )
            return
        context.user_data.pop("awaiting_topup", None)

    async def save_order_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        language = self.store.get_user_language(user_id)
        target, target_id, coupon_code = context.user_data["awaiting_order_email"]
        deleted = await self.delete_private_input(update.message)
        warning = (tr(language,
                      " Hãy tự xóa tin nhắn email vừa gửi vì bot không xóa được.",
                      " Please delete your email message because the bot could not delete it.")
                   if not deleted else "")
        try:
            email = normalize_customer_email(update.message.text)
        except (EmailRequired, ValueError):
            await context.bot.send_message(
                user_id,
                tr(language,
                   "Email không hợp lệ. Gửi một hoặc nhiều email cách nhau bằng dấu phẩy "
                   "(ví dụ a@example.com, b@example.com; tối đa 20 email và 2000 ký tự), "
                   "hoặc /cancel để hủy.",
                   "Invalid email. Send one or more emails separated by commas "
                   "(for example a@example.com, b@example.com; up to 20 emails and 2000 characters), "
                   "or /cancel to stop.") + warning,
            )
            return
        context.user_data.pop("awaiting_order_email", None)
        try:
            await self.send_purchase_confirmation(
                update.message, user_id, context, target, target_id, coupon_code,
                email, send_direct=True,
            )
        except OutOfStock:
            await context.bot.send_message(
                user_id,
                tr(language,
                   "Kho không đủ cho số email vừa nhập. Hãy chọn mua lại với ít email hơn.",
                   "There is not enough stock for those emails. Start again with fewer emails."),
            )
        except (NotFound, CouponUnavailable, SaleCouponConflict):
            await context.bot.send_message(
                user_id,
                tr(language,
                   "Sản phẩm/gói hoặc mã giảm giá đã thay đổi. Hãy chọn mua lại.",
                   "The item or coupon changed. Please start the purchase again."),
            )
        if warning:
            await context.bot.send_message(user_id, warning.strip())

    async def send_purchase_confirmation(
        self, message, user_id: int, context, target: str, target_id: int,
        coupon_code: str | None = None,
        customer_email: str | None = None, *, send_direct: bool = False,
    ):
        language = self.store.get_user_language(user_id)
        quote = (
            self.store.quote_product(user_id, target_id, coupon_code)
            if target == "product" else
            self.store.quote_bundle(user_id, target_id, coupon_code)
        )
        if quote["requires_email"] and customer_email is None:
            context.user_data["awaiting_order_email"] = (target, target_id, quote["coupon_code"])
            context.user_data.pop("pending_purchase", None)
            context.user_data.pop("pending_bundle", None)
            await message.reply_text(
                tr(language,
                   f"📧 {quote['name']} cần email để xử lý đơn. Gửi một hoặc nhiều email "
                   "cách nhau bằng dấu phẩy (ví dụ a@example.com, b@example.com; "
                   "tối đa 20 email và 2000 ký tự). "
                   + ("Mỗi email tính một sản phẩm. " if target == "product" else "")
                   + "Bot sẽ hỏi xác nhận trước khi trừ tiền. "
                   "Gửi /cancel để hủy.",
                   f"📧 {quote['name']} requires email for this order. Send one or more emails "
                   "separated by commas (for example a@example.com, b@example.com; "
                   "up to 20 emails and 2000 characters). "
                   + ("Each email counts as one item. " if target == "product" else "")
                   + "The bot will ask for confirmation "
                   "before charging you. Send /cancel to stop."),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(tr(language, "Hủy", "Cancel"),
                                         callback_data="cancel_buy")
                ]]),
            )
            return
        customer_email = (normalize_customer_email(customer_email)
                          if quote["requires_email"] else None)
        if target == "product" and customer_email:
            quote = self.store.quote_product(
                user_id, target_id, coupon_code,
                quantity=len(customer_email.split(", ")),
            )
        context.user_data.pop("awaiting_order_email", None)
        token = secrets.token_hex(6)
        if target == "product":
            context.user_data["pending_purchase"] = (
                token, target_id, quote["price"], quote["coupon_code"], customer_email,
            )
            context.user_data.pop("pending_bundle", None)
            confirm_action = f"confirm:{token}"
        else:
            context.user_data["pending_bundle"] = (
                token, target_id, quote["price"], quote["coupon_code"], customer_email,
            )
            context.user_data.pop("pending_purchase", None)
            confirm_action = f"confirmbundle:{token}"
        discount_text = (
            tr(language,
               f"Giá gốc: {vnd(quote['base_price'])}\n"
               f"Mã {quote['coupon_code']}: -{vnd(quote['discount'])}\n",
               f"Regular price: {vnd(quote['base_price'])}\n"
               f"Coupon {quote['coupon_code']}: -{vnd(quote['discount'])}\n")
            if quote["coupon_code"] else ""
        )
        sale_text = (
            tr(language,
               f"Giá gốc: {vnd(quote['base_price'])}\n"
               f"🔥 Giá sale: {vnd(quote['sale_price'])} × {quote['sale_units']} "
               f"(-{sale_percent(quote['unit_price'], quote['sale_price'])})\n"
               + (f"Giá thường: {vnd(quote['unit_price'])} × "
                  f"{quote['quantity'] - quote['sale_units']}\n"
                  if quote['quantity'] > quote['sale_units'] else "")
               +
               f"Còn {quote['sale_remaining']} suất sale.\n",
               f"Regular price: {vnd(quote['base_price'])}\n"
               f"🔥 Sale price: {vnd(quote['sale_price'])} × {quote['sale_units']} "
               f"(-{sale_percent(quote['unit_price'], quote['sale_price'])})\n"
               + (f"Regular price: {vnd(quote['unit_price'])} × "
                  f"{quote['quantity'] - quote['sale_units']}\n"
                  if quote['quantity'] > quote['sale_units'] else "")
               +
               f"{quote['sale_remaining']} sale units left.\n")
            if target == "product" and quote["sale_price"] else ""
        )
        rows = [[InlineKeyboardButton(tr(language, "Xác nhận mua", "Confirm purchase"),
                                      callback_data=confirm_action)]]
        if customer_email:
            rows.append([InlineKeyboardButton(
                tr(language, "✏️ Sửa email", "✏️ Change email"),
                callback_data=f"email:edit:{token}",
            )])
        if sale_text:
            rows.append([InlineKeyboardButton(tr(language, "Hủy", "Cancel"),
                                              callback_data="cancel_buy")])
        else:
            rows.append([
                InlineKeyboardButton(tr(language, "🏷 Nhập mã giảm giá", "🏷 Apply coupon"),
                                     callback_data=f"coupon:{target}:{target_id}"),
                InlineKeyboardButton(tr(language, "Hủy", "Cancel"), callback_data="cancel_buy"),
            ])
        balance = vnd(self.store.get_balance(user_id))
        email_text = (tr(language, f"Email xử lý đơn: {customer_email}\n",
                         f"Order email: {customer_email}\n") if customer_email else "")
        quantity_text = (
            tr(language, f"Số lượng: {quote['quantity']} sản phẩm\n",
               f"Quantity: {quote['quantity']} items\n")
            if target == "product" and quote["quantity"] > 1 else ""
        )
        confirmation = tr(
            language,
            f"Xác nhận mua {quote['name']}?\n"
            f"{sale_text}{discount_text}{quantity_text}{email_text}"
            f"Thanh toán: {vnd(quote['price'])}\n"
            f"Số dư hiện tại: {balance}",
            f"Buy {quote['name']}?\n"
            f"{sale_text}{discount_text}{quantity_text}{email_text}"
            f"Total: {vnd(quote['price'])}\n"
            f"Current balance: {balance}",
        )
        if send_direct:
            await context.bot.send_message(
                user_id, confirmation, reply_markup=InlineKeyboardMarkup(rows),
                protect_content=bool(customer_email),
            )
        else:
            await message.reply_text(
                confirmation, reply_markup=InlineKeyboardMarkup(rows),
                protect_content=bool(customer_email),
            )

    async def create_topup(self, message, user_id: int, amount: int):
        language = self.store.get_user_language(user_id)
        if not self.config.min_topup <= amount <= self.config.max_topup:
            raise ValueError("Top-up outside configured range")
        if not self.limiter.allow_topup(user_id):
            await message.reply_text(tr(
                language, "Bạn yêu cầu nạp quá nhanh. Hãy thử lại sau khoảng 15 giây.",
                "You are requesting top-ups too quickly. Try again in about 15 seconds.",
            ))
            return
        try:
            topup = self.store.create_topup(user_id, amount)
        except TopupLimitReached:
            await message.reply_text(
                tr(language,
                   "Bạn có quá nhiều mã nạp chưa thanh toán trong 1 giờ. "
                   "Dùng /deposits để xem mã cũ hoặc thử lại sau.",
                   "You have too many unpaid top-up codes in the past hour. "
                   "Check /deposits or try again later.")
            )
            return
        transfer_account = (self.config.bank_transfer_account_number
                            or self.config.bank_account_number)
        instructions = (
            tr(language,
               f"Yêu cầu nạp #{topup['id']}\n\n"
               f"Ngân hàng: {self.config.bank_name}\n"
               f"Số tài khoản: {transfer_account}\n"
               f"Chủ tài khoản: {self.config.bank_account_holder}\n"
               f"Số tiền gợi ý: {vnd(amount)}\n"
               f"Nội dung chuyển khoản: {topup['code']}\n\n"
               "Giữ nguyên mã trong nội dung chuyển khoản. Bot tự cộng đúng số tiền "
               "ngân hàng báo đã nhận; bạn không cần gửi ảnh hay chờ duyệt.",
               f"Top-up request #{topup['id']}\n\n"
               f"Bank: {self.config.bank_name}\n"
               f"Account number: {transfer_account}\n"
               f"Account holder: {self.config.bank_account_holder}\n"
               f"Suggested amount: {vnd(amount)}\n"
               f"Transfer note: {topup['code']}\n\n"
               "Keep the transfer note exactly as shown. The bot credits the amount "
               "reported by the bank automatically; no receipt or manual approval is needed.")
        )
        rows = [[InlineKeyboardButton(tr(language, "Xem số dư", "View balance"),
                                      callback_data="balance"),
                 InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                      callback_data="menu")]]
        if self.config.bank_bin:
            try:
                image = await asyncio.to_thread(
                    vietqr_png, self.config.bank_bin, transfer_account, amount, topup["code"],
                )
                await message.reply_photo(
                    photo=InputFile(BytesIO(image), filename="vietqr.png"),
                    caption=instructions,
                    reply_markup=InlineKeyboardMarkup(rows),
                )
                return
            except (OSError, ValueError, TelegramError) as exc:
                log.warning("Could not create or send inline VietQR: %s", type(exc).__name__)
        await message.reply_text(instructions, reply_markup=InlineKeyboardMarkup(rows))

    async def orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_orders(update.message, update.effective_user.id)

    async def deposits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.is_private(update):
            await self.send_deposits(update.message, update.effective_user.id)

    async def send_deposits(self, message, user_id: int):
        language = self.store.get_user_language(user_id)
        topups = self.store.list_topups(user_id)
        if not topups:
            await message.reply_text(tr(language, "Bạn chưa tạo yêu cầu nạp nào.",
                                        "You have not made a top-up request yet."),
                                     reply_markup=menu_keyboard(language))
            return
        lines = [tr(language, "10 mã nạp gần nhất:", "Your 10 most recent top-up codes:")]
        for topup in topups:
            state = (tr(language, f"đã nhận {vnd(topup['paid_amount'])}",
                        f"received {vnd(topup['paid_amount'])}") if topup["paid_amount"]
                     else tr(language, "chưa nhận tiền", "payment pending"))
            lines.append(
                tr(language,
                   f"#{topup['id']} {topup['code']} — gợi ý {vnd(topup['requested_amount'])}, {state}",
                   f"#{topup['id']} {topup['code']} — suggested {vnd(topup['requested_amount'])}, {state}")
            )
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                  callback_data="menu")],
        ]))

    async def send_orders(self, message, user_id: int):
        language = self.store.get_user_language(user_id)
        purchases = self.store.list_purchases(user_id)
        if not purchases:
            await message.reply_text(tr(language, "Bạn chưa có đơn hàng nào.",
                                        "You have no orders yet."),
                                     reply_markup=menu_keyboard(language))
            return
        lines = [tr(language,
                    "10 đơn hàng gần nhất:", "Your 10 most recent orders:")]
        rows = []
        for purchase in purchases:
            counts = {}
            for item in purchase["items"]:
                counts[item["name"]] = counts.get(item["name"], 0) + 1
            names = [f"{name} ×{count}" if count > 1 else name
                     for name, count in counts.items()]
            preview = ", ".join(names[:2])
            if len(names) > 2:
                preview += tr(language, f" và {len(names) - 2} loại khác",
                              f" and {len(names) - 2} more")
            if len(preview) > 100:
                preview = preview[:97] + "..."
            count = len(purchase["items"])
            item_kind = ("sản phẩm" if any(item["delivery_type"] == "file"
                                       for item in purchase["items"]) else "tài khoản")
            item_kind_en = "item(s)" if item_kind == "sản phẩm" else "account(s)"
            email_total = sum(bool(item["has_email"]) for item in purchase["items"])
            email_done = sum(bool(item["email_completed_at"]) for item in purchase["items"])
            email_state = (
                tr(language, " · email đã xử lý", " · email processed")
                if email_total and email_done == email_total else
                tr(language, " · email chờ xử lý", " · email pending")
                if email_total and not email_done else
                tr(language, f" · email {email_done}/{email_total} đã xử lý",
                   f" · email {email_done}/{email_total} processed")
                if email_total else ""
            )
            lines.append(tr(language,
                f"#{purchase['id']} · {count} {item_kind} · {vnd(purchase['price'])}{email_state}",
                f"#{purchase['id']} · {count} {item_kind_en} · {vnd(purchase['price'])}{email_state}"))
            lines.append(f"  {preview}")
            rows.append([InlineKeyboardButton(
                tr(language, f"Xem đơn #{purchase['id']}",
                   f"View order #{purchase['id']}"),
                callback_data=f"purchase:{purchase['id']}"
            )])
        rows.append([InlineKeyboardButton(tr(language, "🏠 Menu chính", "🏠 Main menu"),
                                          callback_data="menu")])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_purchase_detail(self, message, user_id: int,
                                   purchase_id: int) -> None:
        language = self.store.get_user_language(user_id)
        purchase = self.store.get_purchase(user_id, purchase_id)
        if not purchase:
            await message.reply_text(tr(language,
                "Không tìm thấy đơn hàng của bạn.", "Your order was not found."))
            return
        ticket = self.store.get_latest_warranty(user_id, purchase_id)
        introduction = tr(language,
            f"Chi tiết đơn hàng · Tổng thanh toán {vnd(purchase['price'])}.",
            f"Order details · Total paid {vnd(purchase['price'])}.")
        await self.send_order_delivery_reply(
            message, language, purchase["items"], introduction, ticket,
        )

    async def document_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_private(update) or update.effective_user.id not in self.config.admin_ids:
            return
        product_id = context.user_data.get("awaiting_file_stock")
        if product_id is None:
            if "awaiting_admin_order_message" in context.user_data:
                await self.send_admin_order_message(update, context)
            return
        document = update.message.document
        if not (document.file_name or "").lower().endswith(".json"):
            await update.message.reply_text("Chỉ nhận file có đuôi .json. Hãy gửi lại hoặc /cancel.")
            return
        if document.file_size is not None and not 0 < document.file_size <= MAX_FILE_STOCK_BYTES:
            await update.message.reply_text("File phải có nội dung và không quá 1 MB.")
            return
        try:
            remote_file = await document.get_file()
            raw = bytes(await remote_file.download_as_bytearray())
        except TelegramError:
            await update.message.reply_text("Chưa tải được file từ Telegram. Hãy gửi lại.")
            return
        deleted = await self.delete_private_input(update.message)
        try:
            result = self.store.add_file_stock(product_id, raw)
        except ValueError:
            response = "File cần là .json UTF-8 hợp lệ, có nội dung và không quá 1 MB. Hãy gửi lại."
        except NotFound:
            context.user_data.pop("awaiting_file_stock", None)
            response = "Sản phẩm không còn trong kho. Mở lại mục Sản phẩm."
        else:
            product = self.store.get_product(product_id)
            response = (f"Đã nhập 1 file cho sản phẩm #{product['display_id']}. "
                        f"Kho còn {result['available']} file. Gửi tiếp file .json hoặc /cancel.")
            if result["duplicate_lines"]:
                response += " Nội dung file trùng file đã nhập; file này vẫn được thêm vào kho."
        if not deleted:
            response += " Hãy tự xóa tin nhắn chứa file vừa gửi trong chat admin."
        await context.bot.send_message(update.effective_user.id, response)

    async def delete_private_input(self, message) -> bool:
        try:
            await message.delete()
            return True
        except TelegramError:
            log.warning("Could not delete sensitive input message")
            return False

    async def save_warranty(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int):
        language = self.store.get_user_language(update.effective_user.id)
        deleted = await self.delete_private_input(update.message)
        try:
            ticket = self.store.create_warranty(update.effective_user.id, order_id, update.message.text)
        except ValueError:
            await context.bot.send_message(update.effective_user.id, tr(
                language, "Nội dung cần từ 1 đến 1000 ký tự. Hãy gửi lại.",
                "Your message must be 1–1000 characters. Please send it again.",
            ))
            return
        except NotFound:
            context.user_data.pop("awaiting_warranty", None)
            await context.bot.send_message(update.effective_user.id, tr(
                language, "Không tìm thấy đơn hàng của bạn.", "Your order was not found.",
            ))
            return
        except WarrantyAlreadyOpen:
            context.user_data.pop("awaiting_warranty", None)
            await context.bot.send_message(update.effective_user.id, tr(
                language, "Đơn này đã có yêu cầu bảo hành đang mở.",
                "This order already has an open warranty request.",
            ))
            return
        context.user_data.pop("awaiting_warranty", None)
        warning = (tr(language, " Hãy tự xóa tin nhắn vừa gửi vì bot không xóa được.",
                      " Please delete the message you just sent; the bot could not delete it.")
                   if not deleted else "")
        await context.bot.send_message(
            update.effective_user.id,
            tr(language,
               f"Đã gửi yêu cầu bảo hành #{ticket['id']} cho đơn #{ticket['order_id']}. "
               f"Shop sẽ phản hồi trong bot.{warning}",
               f"Warranty request #{ticket['id']} for order #{ticket['order_id']} was sent. "
               f"The shop will reply in this bot.{warning}"),
        )
        for admin_id in self.config.admin_ids:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"🛡 Yêu cầu bảo hành mới #{ticket['id']} · đơn #{ticket['order_id']} "
                    f"· khách ID {ticket['user_id']}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Xem yêu cầu", callback_data=f"admin:warranty:{ticket['id']}")
                    ]]),
                )
            except TelegramError:
                log.warning("Could not notify admin %s of warranty request", admin_id)

    async def save_bulk_stock(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, raw: str,
    ):
        deleted = await self.delete_private_input(update.message)
        accounts = [line.strip() for line in raw.splitlines() if line.strip()]
        saved = False
        try:
            result = self.store.bulk_add_stock(
                product_id, accounts, announce_except=self.config.admin_ids,
            )
        except ValueError:
            response = "Lô không hợp lệ: gửi 1-100 dòng, mỗi tài khoản tối đa 3000 ký tự. Hãy gửi lại."
        except NotFound:
            context.user_data.pop("awaiting_bulk_stock", None)
            response = "Không tìm thấy sản phẩm hoặc sản phẩm đã lưu trữ. Mở lại mục Sản phẩm."
        else:
            saved = True
            context.user_data.pop("awaiting_bulk_stock", None)
            display_id = self.store.get_product(product_id)["display_id"]
            response = (
                f"Đã nhập {len(result['ids'])} tài khoản cho sản phẩm #{display_id}. "
                f"Kho còn {result['available']} tài khoản."
            )
            if result["duplicate_lines"]:
                lines = ", ".join(map(str, result["duplicate_lines"]))
                response += (
                    f" Có {len(result['duplicate_lines'])} dòng trùng vẫn đã nhập "
                    f"(tính theo các dòng có nội dung): {lines}. "
                    "Các dòng này có thể giao cùng tài khoản cho nhiều khách."
                )
            if result["announcement_id"] is not None:
                response += f" Đã xếp thông báo cho {result['recipients']} khách theo dõi sản phẩm."
            else:
                response += " Sản phẩm đang ẩn nên chưa thông báo cho khách."
        if not deleted:
            response += " Hãy tự xóa tin nhắn vừa gửi vì bot không xóa được."
        markup = (InlineKeyboardMarkup([[
            InlineKeyboardButton("Xem sản phẩm", callback_data=f"admin:product:{product_id}"),
        ]]) if saved else self.admin_catalog_cancel_button())
        await context.bot.send_message(update.effective_user.id, response, reply_markup=markup)

    async def notify_low_stock(self, bot, order: dict):
        sent = False
        product = self.store.get_product(order["product_id"])
        display_id = product["display_id"]
        file_product = product["delivery_type"] == "file"
        for admin_id in self.config.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Sản phẩm #{display_id} {order['name']} sắp hết: "
                    f"còn {order['available']} {'file' if file_product else 'tài khoản'}. "
                    f"Bấm Xem sản phẩm rồi {'Nhập file .json' if file_product else 'Nhập tài khoản'}.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Xem sản phẩm", callback_data=f"admin:product:{order['product_id']}")
                    ]]),
                )
                sent = True
            except TelegramError:
                log.warning("Could not notify admin %s of low stock", admin_id)
        if not sent:
            self.store.reset_low_stock_alert(order["product_id"])

    async def check_inventory_alerts(self):
        alerts = await asyncio.to_thread(self.store.list_low_stock_alerts)
        for alert in alerts:
            if await asyncio.to_thread(self.store.claim_low_stock_alert, alert["product_id"]):
                await self.notify_low_stock(self.application.bot, alert)

    async def inventory_alert_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self.check_inventory_alerts()
            except Exception:
                log.exception("Could not check low inventory")
            await asyncio.sleep(INVENTORY_CHECK_INTERVAL)

    async def send_pending_stock_announcements(self, bot) -> int:
        pending = await asyncio.to_thread(
            self.store.list_due_stock_announcements, ANNOUNCEMENT_BATCH_SIZE,
        )
        for item in pending:
            batch_id, product_id, user_id = item["batch_id"], item["product_id"], item["user_id"]
            if not item["active"] or item["deleted"] or item["available"] <= 0:
                await asyncio.to_thread(
                    self.store.finish_stock_announcement, batch_id, user_id, "skipped",
                )
                continue
            language = self.store.get_user_language(user_id)
            added = (tr(language, f"➕ Thêm: {item['added_count']}\n",
                        f"➕ Added: {item['added_count']}\n")
                     if item["added_count"] else "")
            price = (
                tr(language,
                   f"🔥 Giá sale: {price_html(item['sale_price'], item['price'])}",
                   f"🔥 Sale price: {price_html(item['sale_price'], item['price'])}")
                if item["sale_price"] else
                tr(language,
                   f"💰 Giá: {price_html(item['price'], item['previous_price'])}",
                   f"💰 Price: {price_html(item['price'], item['previous_price'])}")
            )
            text = (
                f"<b>{tr(language, '🆕 HÀNG MỚI VỀ!', '🆕 NEW STOCK!')}\n\n"
                f"📦 {escape(item['name'])}\n"
                f"{added}"
                + tr(language, f"🗄️ Tồn kho hiện tại: {item['available']}\n",
                     f"🗄️ Available now: {item['available']}\n")
                + f"{price}</b>"
            )
            last_sent = self.last_announcement_send.get(user_id, float("-inf"))
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - last_sent)))
            if not await asyncio.to_thread(
                self.store.stock_announcement_sendable, batch_id, user_id,
            ):
                await asyncio.to_thread(
                    self.store.finish_stock_announcement, batch_id, user_id, "skipped",
                )
                continue
            try:
                await bot.send_message(
                    user_id, text,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(tr(language, "🛒 Mua ngay", "🛒 Buy now"),
                                             callback_data=f"buy:{product_id}"),
                    ], [
                        InlineKeyboardButton(tr(language, "🔕 Bỏ theo dõi sản phẩm", "🔕 Unfollow product"),
                                             callback_data=f"unfollow:{product_id}"),
                    ]]),
                )
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                delay = max(1, int(seconds) + 1)
                self.announcement_pause_until = time.monotonic() + delay
                await asyncio.to_thread(
                    self.store.retry_stock_announcement, batch_id, user_id, delay,
                )
                return delay
            except (Forbidden, BadRequest):
                await asyncio.to_thread(
                    self.store.finish_stock_announcement, batch_id, user_id, "skipped",
                )
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(item["attempts"], 7))
                await asyncio.to_thread(
                    self.store.retry_stock_announcement, batch_id, user_id, delay,
                )
                log.warning("Stock announcement to %s failed: %s", user_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_stock_announcement, batch_id, user_id, "sent",
                )
            finally:
                self.last_announcement_send[user_id] = time.monotonic()
            await asyncio.sleep(ANNOUNCEMENT_SEND_INTERVAL)
        return 0 if len(pending) == ANNOUNCEMENT_BATCH_SIZE else ANNOUNCEMENT_POLL_INTERVAL

    async def send_pending_sale_announcements(self, bot) -> int:
        pending = await asyncio.to_thread(self.store.list_due_sale_announcements, 1)
        for delivery in pending:
            sale_id, user_id = delivery["sale_id"], delivery["user_id"]
            last_sent = self.last_announcement_send.get(user_id, float("-inf"))
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - last_sent)))
            item = await asyncio.to_thread(self.store.get_sale_announcement, sale_id, user_id)
            if (not item or item["status"] != "pending" or not item["following"]
                    or not item["sale_active"]
                    or not item["product_active"] or item["deleted"]
                    or item["sale_remaining"] <= 0 or item["available"] <= 0):
                await asyncio.to_thread(
                    self.store.finish_sale_announcement, sale_id, user_id, "skipped",
                )
                continue
            language = self.store.get_user_language(user_id)
            limit_text = (
                tr(language, f"Chỉ áp dụng cho {item['max_units']} suất đầu tiên",
                   f"Only for the first {item['max_units']} units")
                if item["sale_remaining"] == item["max_units"] else
                tr(language, f"Còn {item['sale_remaining']}/{item['max_units']} suất giá sale",
                   f"{item['sale_remaining']}/{item['max_units']} sale units left")
            )
            text = (
                f"<b>{tr(language, '🔥 SALE SỐC!', '🔥 FLASH SALE!')}\n\n"
                f"📦 {escape(item['name'])}\n"
                + tr(language, f"💰 Giá gốc: <s>{vnd(item['original_price'])}</s>\n",
                     f"💰 Regular price: <s>{vnd(item['original_price'])}</s>\n")
                + tr(language, f"🔥 Giá sale: {vnd(item['sale_price'])} ",
                     f"🔥 Sale price: {vnd(item['sale_price'])} ")
                + f"(-{sale_percent(item['original_price'], item['sale_price'])})\n"
                + tr(language, f"🎯 {limit_text}, nhanh tay kẻo lỡ!\n",
                     f"🎯 {limit_text}. Get yours while available!\n")
                + tr(language, f"🗄️ Tồn kho: {item['available']}</b>",
                     f"🗄️ In stock: {item['available']}</b>")
            )
            try:
                await bot.send_message(
                    user_id, text, parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            tr(language, "🛒 Mua ngay", "🛒 Buy now"),
                            callback_data=f"buy:{item['product_id']}",
                        ),
                    ], [
                        InlineKeyboardButton(
                            tr(language, "🔕 Bỏ theo dõi sản phẩm", "🔕 Unfollow product"),
                            callback_data=f"unfollow:{item['product_id']}",
                        ),
                    ]]),
                )
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                delay = max(1, int(seconds) + 1)
                self.announcement_pause_until = time.monotonic() + delay
                await asyncio.to_thread(
                    self.store.retry_sale_announcement, sale_id, user_id, delay,
                )
                return delay
            except (Forbidden, BadRequest):
                await asyncio.to_thread(
                    self.store.finish_sale_announcement, sale_id, user_id, "skipped",
                )
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(delivery["attempts"], 7))
                await asyncio.to_thread(
                    self.store.retry_sale_announcement, sale_id, user_id, delay,
                )
                log.warning("Sale announcement to %s failed: %s", user_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_sale_announcement, sale_id, user_id, "sent",
                )
            finally:
                self.last_announcement_send[user_id] = time.monotonic()
            await asyncio.sleep(ANNOUNCEMENT_SEND_INTERVAL)
        return 0 if pending else ANNOUNCEMENT_POLL_INTERVAL

    async def send_pending_broadcasts(self, bot) -> int:
        pending = await asyncio.to_thread(
            self.store.list_due_broadcast_deliveries, ANNOUNCEMENT_BATCH_SIZE,
        )
        for item in pending:
            batch_id, user_id = item["batch_id"], item["user_id"]
            if user_id in self.banned_user_ids:
                await asyncio.to_thread(
                    self.store.finish_broadcast_delivery, batch_id, user_id, "failed",
                )
                continue
            last_sent = self.last_announcement_send.get(user_id, float("-inf"))
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - last_sent)))
            if user_id in self.banned_user_ids:
                await asyncio.to_thread(
                    self.store.finish_broadcast_delivery, batch_id, user_id, "failed",
                )
                continue
            try:
                language = self.store.get_user_language(user_id)
                await bot.send_message(
                    user_id,
                    (BROADCAST_PREFIX_EN if language == "en" else BROADCAST_PREFIX)
                    + item["content"],
                )
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                delay = max(1, int(seconds) + 1)
                self.announcement_pause_until = time.monotonic() + delay
                await asyncio.to_thread(
                    self.store.retry_broadcast_delivery, batch_id, user_id, delay,
                )
                return delay
            except (Forbidden, BadRequest):
                await asyncio.to_thread(
                    self.store.finish_broadcast_delivery, batch_id, user_id, "failed",
                )
            except TelegramError as exc:
                if item["attempts"] >= 4:
                    await asyncio.to_thread(
                        self.store.finish_broadcast_delivery, batch_id, user_id, "failed",
                    )
                else:
                    delay = min(3600, 30 * 2 ** item["attempts"])
                    await asyncio.to_thread(
                        self.store.retry_broadcast_delivery, batch_id, user_id, delay,
                    )
                log.warning("Broadcast to %s failed: %s", user_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_broadcast_delivery, batch_id, user_id, "sent",
                )
            finally:
                self.last_announcement_send[user_id] = time.monotonic()
            await asyncio.sleep(ANNOUNCEMENT_SEND_INTERVAL)
        return 0 if len(pending) == ANNOUNCEMENT_BATCH_SIZE else ANNOUNCEMENT_POLL_INTERVAL

    async def stock_announcement_loop(self):
        await asyncio.sleep(3)
        while True:
            pause = self.announcement_pause_until - time.monotonic()
            if pause > 0:
                await asyncio.sleep(pause)
                continue
            try:
                stock_delay = await self.send_pending_stock_announcements(self.application.bot)
                pause = self.announcement_pause_until - time.monotonic()
                if pause > 0:
                    delay = pause
                else:
                    sale_delay = await self.send_pending_sale_announcements(self.application.bot)
                    pause = self.announcement_pause_until - time.monotonic()
                    if pause > 0:
                        delay = pause
                    else:
                        broadcast_delay = await self.send_pending_broadcasts(self.application.bot)
                        delay = min(stock_delay, sale_delay, broadcast_delay)
            except Exception:
                log.exception("Could not send announcements")
                delay = ANNOUNCEMENT_POLL_INTERVAL
            await asyncio.sleep(delay)

    async def send_pending_purchase_notifications(self, bot) -> int:
        pending = await asyncio.to_thread(self.store.list_due_purchase_notifications, 20)
        for item in pending:
            kind, ref_id, admin_id = item["kind"], item["ref_id"], item["admin_id"]
            if admin_id not in self.config.admin_ids:
                await asyncio.to_thread(
                    self.store.finish_purchase_notification, kind, ref_id, admin_id, "skipped",
                )
                continue
            buyer = user_label(item["user_id"], item["username"], item["display_name"])
            purchase = None
            if kind == "product":
                purchase_id = ref_id
                purchase = await asyncio.to_thread(self.store.get_admin_purchase, ref_id)
                if purchase:
                    counts = {}
                    for account in purchase["items"]:
                        key = (account["display_id"] or account["product_id"], account["name"])
                        counts[key] = counts.get(key, 0) + 1
                    components = ", ".join(
                        f"#{display_id} {name}" + (f" ×{count}" if count > 1 else "")
                        for (display_id, name), count in counts.items()
                    )
                    item_kind = ("sản phẩm" if any(account["delivery_type"] == "file"
                                                  for account in purchase["items"])
                                 else "tài khoản")
                    details = f"Đơn: #{ref_id} · {len(purchase['items'])} {item_kind}\nSản phẩm: {components}"
                    price = purchase["price"]
                    contact_order_id = next((account["id"] for account in purchase["items"]
                                             if account["has_email"]), None)
                else:
                    # A notification queued by an older version may reference an item
                    # after the first one. Keep it deliverable during an upgrade.
                    details = (
                        f"Sản phẩm: #{item['product_display_id'] or item['product_id']} "
                        f"{item['name']}\nĐơn: #{ref_id}"
                    )
                    price = item["price"]
                    contact_order_id = item["contact_order_id"]
            else:
                items = await asyncio.to_thread(self.store.bundle_order_items, ref_id)
                purchase_id = await asyncio.to_thread(
                    self.store.purchase_id_for_bundle_order, ref_id,
                )
                components = ", ".join(
                    f"{row['quantity']} × #{row['display_id'] or row['product_id']} "
                    f"{row['product_name']}"
                    for row in items
                )
                details = (
                    f"Gói: #{item['bundle_id']} {item['name']}\n"
                    f"Gồm: {components}\nĐơn: #{purchase_id or ref_id}"
                )
                price = item["price"]
                contact_order_id = item["contact_order_id"]
            message = (
                f"🛒 CÓ ĐƠN MỚI\nKhách: {buyer}\n"
                f"{details}\nThanh toán: {vnd(price)}"
            )
            button_rows = [[
                InlineKeyboardButton("👤 Xem khách", callback_data=f"admin:user:{item['user_id']}"),
                InlineKeyboardButton("🧾 Xem đơn", callback_data=(
                    f"admin:purchase:{purchase_id}" if purchase_id and
                    (kind == "bundle" or purchase)
                    else "admin:sales:0")),
            ]]
            if purchase_id and (kind == "bundle" or purchase):
                button_rows.append([InlineKeyboardButton(
                    "💬 Nhắn khách", callback_data=f"admin:message:{purchase_id}",
                )])
            if contact_order_id:
                button_rows.insert(0, [InlineKeyboardButton(
                    "📧 Xem email đơn", callback_data=f"admin:order:{contact_order_id}",
                )])
            buttons = InlineKeyboardMarkup(button_rows)
            try:
                await bot.send_message(
                    admin_id, message, reply_markup=buttons, protect_content=True,
                )
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                await asyncio.to_thread(
                    self.store.retry_purchase_notification,
                    kind, ref_id, admin_id, max(1, int(seconds) + 1),
                )
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(item["attempts"], 7))
                await asyncio.to_thread(
                    self.store.retry_purchase_notification, kind, ref_id, admin_id, delay,
                )
                log.warning("Purchase notification to admin %s failed: %s",
                            admin_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_purchase_notification, kind, ref_id, admin_id, "sent",
                )
            await asyncio.sleep(0.2)
        return len(pending)

    async def send_pending_new_customer_notifications(self, bot) -> int:
        pending = await asyncio.to_thread(
            self.store.list_due_new_customer_notifications, 20,
        )
        for item in pending:
            user_id, admin_id = item["user_id"], item["admin_id"]
            if admin_id not in self.config.admin_ids:
                await asyncio.to_thread(
                    self.store.finish_new_customer_notification,
                    user_id, admin_id, "skipped",
                )
                continue
            customer = user_label(user_id, item["username"], item["display_name"])
            joined_at = status_time(parse_stored_time(item["created_at"]))
            message = (
                f"👤 KHÁCH MỚI TRUY CẬP BOT\n"
                f"Khách: {customer}\n"
                f"Tương tác đầu tiên: {joined_at}"
            )
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton("👤 Xem khách", callback_data=f"admin:user:{user_id}"),
            ]])
            try:
                await bot.send_message(
                    admin_id, message, reply_markup=buttons, protect_content=True,
                )
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                await asyncio.to_thread(
                    self.store.retry_new_customer_notification,
                    user_id, admin_id, max(1, int(seconds) + 1),
                )
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(item["attempts"], 7))
                await asyncio.to_thread(
                    self.store.retry_new_customer_notification,
                    user_id, admin_id, delay,
                )
                log.warning("New customer notification to admin %s failed: %s",
                            admin_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_new_customer_notification,
                    user_id, admin_id, "sent",
                )
            await asyncio.sleep(0.2)
        return len(pending)

    async def send_pending_email_completion_notifications(self, bot) -> int:
        pending = await asyncio.to_thread(
            self.store.list_due_email_completion_notifications, 20,
        )
        for item in pending:
            order_id, user_id = item["order_id"], item["user_id"]
            language = self.store.get_user_language(user_id)
            notice = tr(
                language,
                f"✅ Đơn #{order_id} — {item['name']} đã được shop xử lý xong "
                "cho email bạn cung cấp. Mở đơn để xem lại hoặc gửi yêu cầu bảo hành nếu cần.",
                f"✅ Order #{order_id} — {item['name']} has been processed "
                "for the email you provided. Open the order or request warranty support if needed.",
            )
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton(tr(language, "📦 Xem đơn", "📦 View order"),
                                     callback_data=f"order:{order_id}"),
            ]])
            try:
                await bot.send_message(user_id, notice, reply_markup=buttons)
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                await asyncio.to_thread(
                    self.store.retry_email_completion_notification,
                    order_id, max(1, int(seconds) + 1),
                )
            except (Forbidden, BadRequest):
                await asyncio.to_thread(
                    self.store.finish_email_completion_notification, order_id, "failed",
                )
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(item["attempts"], 7))
                await asyncio.to_thread(
                    self.store.retry_email_completion_notification, order_id, delay,
                )
                log.warning("Email order completion notice for %s failed: %s",
                            order_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_email_completion_notification, order_id, "sent",
                )
            await asyncio.sleep(0.2)
        return len(pending)

    async def email_completion_notification_loop(self):
        await asyncio.sleep(2)
        while True:
            try:
                count = await self.send_pending_email_completion_notifications(
                    self.application.bot,
                )
            except Exception:
                log.exception("Could not send email order completion notifications")
                count = 0
            await asyncio.sleep(0 if count == 20 else 3)

    async def send_due_email_reminders(self, bot) -> int:
        sent = 0
        for admin_id in sorted(self.config.admin_ids):
            reminder = await asyncio.to_thread(self.store.due_email_reminder, admin_id)
            if not reminder:
                continue
            oldest = status_time(parse_stored_time(reminder["oldest_at"]))
            text = (
                f"⏰ CÓ {reminder['count']} ĐƠN EMAIL CHỜ QUÁ {reminder['minutes']} PHÚT\n"
                f"Đơn cũ nhất từ: {oldest}\n"
                "Mở danh sách để xử lý; bot sẽ nhắc lại nếu vẫn còn đơn chờ."
            )
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton("📧 Xem đơn email chờ",
                                     callback_data="admin:pendingemails:0"),
            ]])
            try:
                await bot.send_message(admin_id, text, reply_markup=buttons,
                                       protect_content=True)
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                await asyncio.to_thread(
                    self.store.retry_email_reminder, admin_id, max(1, int(seconds) + 1),
                )
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(reminder["attempts"], 7))
                await asyncio.to_thread(self.store.retry_email_reminder, admin_id, delay)
                log.warning("Email order reminder to admin %s failed: %s",
                            admin_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_email_reminder, admin_id, reminder["max_order_id"],
                )
                sent += 1
        return sent

    async def email_reminder_loop(self):
        await asyncio.sleep(4)
        while True:
            try:
                await self.send_due_email_reminders(self.application.bot)
            except Exception:
                log.exception("Could not check overdue email orders")
            await asyncio.sleep(60)

    async def send_due_warranty_reminders(self, bot) -> int:
        sent = 0
        for admin_id in sorted(self.config.admin_ids):
            reminder = await asyncio.to_thread(self.store.due_warranty_reminder, admin_id)
            if not reminder:
                continue
            oldest = status_time(parse_stored_time(reminder["oldest_at"]))
            text = (
                f"⏰ CÓ {reminder['count']} YÊU CẦU BẢO HÀNH CHỜ QUÁ "
                f"{reminder['minutes']} PHÚT\n"
                f"Yêu cầu cũ nhất từ: {oldest}\n"
                "Mở danh sách để xử lý; bot sẽ nhắc lại nếu vẫn còn yêu cầu chờ."
            )
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛡 Xem bảo hành đang mở",
                                     callback_data="admin:warranties:0"),
            ]])
            try:
                await bot.send_message(admin_id, text, reply_markup=buttons,
                                       protect_content=True)
            except RetryAfter as exc:
                seconds = (exc.retry_after.total_seconds()
                           if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                await asyncio.to_thread(
                    self.store.retry_warranty_reminder, admin_id, max(1, int(seconds) + 1),
                )
            except TelegramError as exc:
                delay = min(3600, 30 * 2 ** min(reminder["attempts"], 7))
                await asyncio.to_thread(self.store.retry_warranty_reminder, admin_id, delay)
                log.warning("Warranty reminder to admin %s failed: %s",
                            admin_id, type(exc).__name__)
            else:
                await asyncio.to_thread(
                    self.store.finish_warranty_reminder, admin_id, reminder["max_ticket_id"],
                )
                sent += 1
        return sent

    async def warranty_reminder_loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                await self.send_due_warranty_reminders(self.application.bot)
            except Exception:
                log.exception("Could not check overdue warranty requests")
            await asyncio.sleep(60)

    async def new_customer_notification_loop(self):
        await asyncio.sleep(2)
        while True:
            try:
                count = await self.send_pending_new_customer_notifications(self.application.bot)
            except Exception:
                log.exception("Could not send new customer notifications")
                count = 0
            await asyncio.sleep(0 if count == 20 else 3)

    async def purchase_notification_loop(self):
        await asyncio.sleep(2)
        while True:
            try:
                count = await self.send_pending_purchase_notifications(self.application.bot)
            except Exception:
                log.exception("Could not send purchase notifications")
                count = 0
            await asyncio.sleep(0 if count == 20 else 3)

    async def send_pending_direct_checkout_orders(self, bot) -> int:
        async with self.direct_delivery_lock:
            pending = await asyncio.to_thread(self.store.list_due_direct_checkout_batches, 20)
            for batch in pending:
                checkout_id = batch["checkout_id"]
                orders = batch["orders"]
                user_id = batch["user_id"]
                language = self.store.get_user_language(user_id)
                has_file_product = any(order.get("delivery_type") == "file" for order in orders)
                introduction = tr(language,
                    f"✅ Đã thanh toán đơn chuyển khoản #{checkout_id} "
                    f"· {len(orders)} {'sản phẩm' if has_file_product else 'tài khoản'}",
                    f"✅ Bank order #{checkout_id} paid · {len(orders)} "
                    f"{'item(s)' if has_file_product else 'account(s)'}")
                content, entities, markup = account_delivery_payload(
                    orders, language, introduction,
                )
                document_bytes, extension = order_delivery_file(orders, language)
                try:
                    if not batch["message_sent_at"]:
                        await bot.send_message(user_id, content, protect_content=True,
                                               entities=entities, reply_markup=markup)
                        await asyncio.to_thread(
                            self.store.mark_direct_checkout_delivery_step,
                            checkout_id, "message",
                        )
                    if not batch["file_sent_at"]:
                        await bot.send_document(
                            user_id,
                            document=InputFile(
                                BytesIO(document_bytes),
                                filename=f"don-chuyen-khoan-{checkout_id}{extension}",
                            ),
                            caption=tr(language,
                                       f"📄 File sản phẩm đã mua ({extension})" if has_file_product
                                       else "📄 File tài khoản đã mua",
                                       f"📄 Purchased product file ({extension})" if has_file_product
                                       else "📄 Purchased accounts text file"),
                        )
                        log.info("Sent %s direct checkout file for checkout %s",
                                 extension, checkout_id)
                        await asyncio.to_thread(
                            self.store.mark_direct_checkout_delivery_step,
                            checkout_id, "file",
                        )
                    await asyncio.to_thread(
                        self.store.finish_direct_checkout_batch, checkout_id,
                    )
                except RetryAfter as exc:
                    seconds = (exc.retry_after.total_seconds()
                               if isinstance(exc.retry_after, timedelta) else float(exc.retry_after))
                    await asyncio.to_thread(
                        self.store.retry_direct_checkout_batch,
                        checkout_id, max(1, int(seconds) + 1),
                    )
                except TelegramError as exc:
                    delay = min(3600, 30 * 2 ** min(max(order["attempts"] for order in orders), 7))
                    await asyncio.to_thread(
                        self.store.retry_direct_checkout_batch, checkout_id, delay,
                    )
                    log.warning("Direct checkout delivery to %s failed: %s",
                                user_id, type(exc).__name__)
            return len(pending)

    async def direct_delivery_loop(self):
        await asyncio.sleep(2)
        while True:
            try:
                count = await self.send_pending_direct_checkout_orders(self.application.bot)
            except Exception:
                log.exception("Could not deliver direct checkout orders")
                count = 0
            await asyncio.sleep(0 if count == 20 else 3)

    async def _notify_operational_admin(self, text: str):
        for admin_id in self.config.admin_ids:
            try:
                await self.application.bot.send_message(admin_id, text)
            except TelegramError:
                log.warning("Could not notify admin %s of webhook error", admin_id)

    def schedule_webhook_alert(self, category: str):
        text = WEBHOOK_ALERT_TEXT.get(category)
        if not text or self.loop is None or self.loop.is_closed():
            return
        now = time.monotonic()
        with self.webhook_alert_lock:
            if now - self.last_webhook_alerts.get(category, float("-inf")) < WEBHOOK_ALERT_INTERVAL:
                return
            self.last_webhook_alerts[category] = now
        notification = self._notify_operational_admin(text)
        try:
            future = asyncio.run_coroutine_threadsafe(notification, self.loop)
        except RuntimeError:
            notification.close()
            log.warning("Could not schedule webhook alert: event loop is unavailable")
            return
        future.add_done_callback(self._operational_notification_done)

    @staticmethod
    def _operational_notification_done(future):
        try:
            future.result()
        except Exception:
            log.exception("Operational notification failed")

    async def callbacks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not self.is_private(update):
            await query.answer()
            return
        await query.answer()
        data = query.data or ""
        user_id = update.effective_user.id
        language = self.store.get_user_language(user_id)
        if data != "topup":
            context.user_data.pop("awaiting_topup", None)
        context.user_data.pop("awaiting_warranty", None)
        context.user_data.pop("awaiting_bulk_stock", None)
        context.user_data.pop("awaiting_file_stock", None)
        context.user_data.pop("awaiting_coupon", None)
        context.user_data.pop("awaiting_order_email", None)
        context.user_data.pop("awaiting_export_range", None)
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("awaiting_admin_order_message", None)
        context.user_data.pop("awaiting_customer_order_reply", None)
        context.user_data.pop("awaiting_admin_user_search", None)
        if not data.startswith("admin:usersearch:"):
            context.user_data.pop("admin_user_search_query", None)
        context.user_data.pop("awaiting_admin_sales_search", None)
        if not data.startswith("admin:salessearch:"):
            context.user_data.pop("admin_sales_search_query", None)
        context.user_data.pop("awaiting_display_id", None)
        if not data.startswith(("buyqty:", "buyflow:")):
            context.user_data.pop("product_checkout", None)
            context.user_data.pop("awaiting_buy_quantity", None)
            context.user_data.pop("awaiting_buy_email", None)
            context.user_data.pop("awaiting_buy_coupon", None)
        if not data.startswith("admin:catalog:"):
            context.user_data.pop("admin_product_flow", None)
        if not data.startswith("admin:offer:"):
            context.user_data.pop("admin_offer_flow", None)
        if not data.startswith("admin:broadcast:"):
            context.user_data.pop("pending_broadcast", None)
        if not data.startswith(("admin:match:", "admin:credit:")):
            context.user_data.pop("awaiting_manual_credit", None)
            context.user_data.pop("pending_manual_credit", None)
        if not data.startswith("admin:balancecredit:"):
            self.clear_admin_test_credit_state(context)
        if data.startswith("admin:"):
            if user_id not in self.config.admin_ids:
                await query.message.reply_text(tr(
                    language, "Chỉ quản trị viên được xem mục này.",
                    "Only shop admins can view this section.",
                ))
            else:
                callback_kind = (data.split(":", 2)[1] if ":" in data else "")
                if callback_kind in {
                    "ban", "banconfirm", "unban", "balancecredit", "catalog", "offer",
                    "email", "displayid", "moveproduct", "postproduct", "credit", "match",
                    "warrantyprivate", "warrantyprivateconfirm", "broadcast", "emaildone",
                    "warranty", "reconcile", "exportaudit",
                }:
                    self.audit_admin_action(user_id, "button", callback_kind, None, data)
                await self.admin_callback(query.message, data, context, user_id)
            return
        if data.startswith("buyerreply:"):
            parts = data.split(":")
            try:
                if len(parts) != 3:
                    raise ValueError
                purchase_id, admin_id = int(parts[1]), int(parts[2])
            except ValueError:
                return
            if admin_id not in self.config.admin_ids or not self.store.get_purchase(user_id, purchase_id):
                await query.message.reply_text(tr(language,
                    "Không tìm thấy đơn hàng hoặc cuộc trò chuyện này không còn hiệu lực.",
                    "The order was not found or this conversation is no longer available."))
                return
            context.user_data["awaiting_customer_order_reply"] = (
                purchase_id, admin_id, time.monotonic() + ORDER_CHAT_INPUT_SECONDS,
            )
            await query.message.reply_text(tr(language,
                f"Gửi lời nhắn cho chủ shop về đơn #{purchase_id} (tối đa 2000 ký tự). "
                "Không gửi mật khẩu hoặc mã OTP. Gửi /cancel để hủy.",
                f"Send your reply about order #{purchase_id} (up to 2000 characters). "
                "Do not send passwords or OTP codes. Send /cancel to stop."))
            return
        if data.startswith("cart:"):
            await self.cart_callback(query.message, data, context, user_id)
            return
        if data.startswith("buyqty:"):
            parts = data.split(":")
            if len(parts) not in (2, 3):
                return
            try:
                product_id = int(parts[1])
            except ValueError:
                return
            if len(parts) == 2:
                await self.send_product_quantity_options(query.message, user_id,
                                                         product_id, context)
            elif parts[2] == "other":
                product = self.store.get_product(product_id)
                if not product or not product["active"] or product["deleted"] or not product["available"]:
                    await query.message.reply_text(tr(language,
                        "Sản phẩm đã hết hàng hoặc ngừng bán.",
                        "This product is out of stock or unavailable."))
                    return
                context.user_data["awaiting_buy_quantity"] = product_id
                context.user_data.pop("product_checkout", None)
                limit = min(MAX_PURCHASE_QUANTITY, product["available"])
                await query.message.reply_text(tr(language,
                    f"Gửi số lượng từ 1 đến {limit}; /cancel để hủy.",
                    f"Send a quantity from 1 to {limit}; /cancel to stop."))
            elif parts[2] in {str(quantity) for quantity in PURCHASE_QUANTITY_PRESETS}:
                await self.choose_product_quantity(query.message, user_id,
                                                   product_id, int(parts[2]), context)
            return
        if data.startswith("buyflow:"):
            await self.product_checkout_callback(query.message, data, context, user_id)
            return
        if data == "menu":
            await self.send_menu(query.message, user_id)
        elif data == "language":
            await self.send_language_options(query.message, user_id)
        elif data in ("language:vi", "language:en"):
            selected = data.split(":", 1)[1]
            self.store.set_user_language(user_id, selected)
            await query.message.reply_text(
                "Đã chọn Tiếng Việt." if selected == "vi" else "Language set to English."
            )
            await self.send_menu(query.message, user_id)
        elif data == "stocknotify":
            self.store.ensure_user(user_id)
            await self.send_stock_notification_settings(query.message, user_id)
        elif data in ("stocknotify:on", "stocknotify:off"):
            self.store.set_stock_notifications_enabled(user_id, data == "stocknotify:on")
            await self.send_stock_notification_settings(query.message, user_id)
        elif data.startswith("following:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                return
            await self.send_following(query.message, user_id, page)
        elif data.startswith(("follow:", "unfollow:")):
            action, raw_id = data.split(":", 1)
            try:
                product_id = int(raw_id)
                self.store.set_product_following(user_id, product_id, action == "follow")
            except ValueError:
                return
            except NotFound:
                await query.message.reply_text(tr(
                    language, "Sản phẩm không còn bán hoặc không tìm thấy.",
                    "This product is unavailable or was not found.",
                ))
                return
            if action == "follow":
                stock_enabled = self.store.stock_notifications_enabled(user_id)
                note = (tr(language,
                           "Đã theo dõi sản phẩm. Bot sẽ báo khi có hàng mới hoặc giảm giá.",
                           "Product followed. You will get its new stock and sale alerts.")
                        if stock_enabled else tr(language,
                           "Đã theo dõi sản phẩm. Tin giảm giá vẫn bật; thông báo kho đang tắt. "
                           "Bật thông báo kho để nhận tin có hàng mới.",
                           "Product followed. Sale alerts are on, but stock alerts are off. "
                           "Turn on stock alerts to get restock notices."))
                buttons = (InlineKeyboardMarkup([[
                    InlineKeyboardButton(tr(language, "🔔 Bật thông báo kho", "🔔 Turn on stock alerts"),
                                         callback_data="stocknotify:on"),
                ]]) if not stock_enabled else None)
                await query.message.reply_text(note, reply_markup=buttons)
            else:
                await query.message.reply_text(tr(
                    language, "Đã bỏ theo dõi. Bot sẽ không gửi thêm tin kho hoặc sale "
                    "của sản phẩm này.",
                    "Unfollowed. You will no longer receive stock or sale alerts "
                    "for this product.",
                ))
            product = self.store.get_product(product_id)
            if product and product["active"] and not product["deleted"]:
                await self.send_product_detail(query.message, user_id, product_id,
                                               count_view=False)
            else:
                await self.send_following(query.message, user_id, 0)
        elif data == "help":
            await self.send_help(query.message)
        elif data == "balance":
            await self.send_balance(query.message, user_id)
        elif data == "topup":
            await self.send_topup_options(query.message)
        elif data == "topup:other":
            context.user_data["awaiting_topup"] = True
            await query.message.reply_text(
                tr(language,
                   f"Gửi số tiền muốn nạp từ {vnd(self.config.min_topup)} "
                   f"đến {vnd(self.config.max_topup)}, ví dụ 75000. Gửi /cancel để hủy.",
                   f"Send an amount from {vnd(self.config.min_topup)} to "
                   f"{vnd(self.config.max_topup)}, for example 75000. Send /cancel to stop.")
            )
        elif data.startswith("topup:amount:"):
            amount_text = data.removeprefix("topup:amount:")
            if amount_text not in {str(preset) for preset in TOPUP_PRESETS}:
                await self.send_topup_options(query.message)
                return
            amount = int(amount_text)
            if (amount not in TOPUP_PRESETS
                    or not self.config.min_topup <= amount <= self.config.max_topup):
                await self.send_topup_options(query.message)
                return
            await self.create_topup(query.message, user_id, amount)
        elif data == "orders":
            await self.send_orders(query.message, user_id)
        elif data == "deposits":
            await self.send_deposits(query.message, user_id)
        elif data == "payments":
            await self.send_bank_orders(query.message, user_id)
        elif data.startswith("shop:"):
            try:
                await self.send_shop(query.message, int(data.split(":", 1)[1]))
            except ValueError:
                return
        elif data.startswith("bundles:"):
            try:
                await self.send_bundles(query.message, int(data.split(":", 1)[1]))
            except ValueError:
                return
        elif data.startswith("bundle:"):
            try:
                bundle_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            await self.send_bundle_detail(query.message, user_id, bundle_id)
        elif data.startswith("product:"):
            try:
                product_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            await self.send_product_detail(query.message, user_id, product_id)
        elif data.startswith("buy:"):
            try:
                product_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            await self.send_product_quantity_options(query.message, user_id,
                                                     product_id, context)
        elif data.startswith("buybundle:"):
            try:
                bundle_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            try:
                await self.send_purchase_confirmation(
                    query.message, user_id, context, "bundle", bundle_id,
                )
            except (NotFound, OutOfStock):
                await query.message.reply_text(tr(language,
                    "Gói đã hết hoặc không còn bán.",
                    "This bundle is out of stock or unavailable."))
        elif data.startswith("coupon:"):
            try:
                _, target, target_id = data.split(":", 2)
                target_id = int(target_id)
            except ValueError:
                return
            if target not in ("product", "bundle"):
                return
            if target == "product":
                product = self.store.get_product(target_id)
                if product and product["sale_id"]:
                    await query.message.reply_text(
                        tr(language,
                           "Sản phẩm đang sale nên không dùng đồng thời mã giảm giá.",
                           "Coupons cannot be combined with this product's sale price.")
                    )
                    return
            pending = context.user_data.get(
                "pending_purchase" if target == "product" else "pending_bundle"
            )
            customer_email = (pending[4] if pending and len(pending) > 4
                              and pending[1] == target_id else None)
            context.user_data.pop("pending_purchase", None)
            context.user_data.pop("pending_bundle", None)
            context.user_data["awaiting_coupon"] = (target, target_id, customer_email)
            await query.message.reply_text(
                tr(language,
                   "Gửi mã giảm giá của bạn (mỗi mã dùng một lần cho mỗi người). Gửi /cancel để hủy.",
                   "Send your coupon code (one use per person). Send /cancel to stop.")
            )
        elif data.startswith("email:edit:"):
            token = data.split(":", 2)[2]
            pending = context.user_data.get("pending_purchase")
            target = "product"
            if not pending or pending[0] != token:
                pending = context.user_data.get("pending_bundle")
                target = "bundle"
            if not pending or pending[0] != token or len(pending) < 5 or not pending[4]:
                await query.message.reply_text(tr(
                    language, "Bước sửa email đã hết hiệu lực. Hãy chọn mua lại.",
                    "This email change has expired. Start the purchase again.",
                ))
                return
            context.user_data.pop("pending_purchase", None)
            context.user_data.pop("pending_bundle", None)
            context.user_data["awaiting_order_email"] = (target, pending[1], pending[3])
            await query.message.reply_text(tr(
                language,
                "Gửi email mới cho đơn này (có thể nhập nhiều email cách nhau bằng dấu phẩy) "
                "hoặc /cancel để hủy.",
                "Send new email for this order (you can enter multiple emails separated "
                "by commas) or /cancel to stop.",
            ))
        elif data == "cancel_buy":
            context.user_data.pop("pending_purchase", None)
            context.user_data.pop("pending_bundle", None)
            context.user_data.pop("awaiting_order_email", None)
            await query.message.reply_text(
                tr(language, "Đã hủy mua hàng.", "Purchase cancelled."),
                reply_markup=menu_keyboard(language),
            )
        elif data.startswith("confirm:"):
            pending = context.user_data.pop("pending_purchase", None)
            if not pending or pending[0] != data.split(":", 1)[1]:
                await query.message.reply_text(tr(language,
                    "Xác nhận này đã hết hiệu lực. Hãy chọn mua lại.",
                    "This confirmation has expired. Please start the purchase again."))
                return
            try:
                order = self.store.purchase(
                    user_id, pending[1], expected_price=pending[2],
                    coupon_code=pending[3] if len(pending) > 3 else None,
                    customer_email=pending[4] if len(pending) > 4 else None,
                    notify_admin_ids=self.config.admin_ids,
                )
            except (EmailRequired, ValueError):
                context.user_data["awaiting_order_email"] = (
                    "product", pending[1], pending[3] if len(pending) > 3 else None,
                )
                await query.message.reply_text(tr(
                    language,
                    "Sản phẩm này cần email hợp lệ trước khi mua. Gửi một hoặc nhiều email "
                    "cách nhau bằng dấu phẩy, hoặc /cancel.",
                    "This product requires valid email before purchase. Send one or more "
                    "emails separated by commas, or /cancel to stop.",
                ))
                return
            except InsufficientBalance:
                await query.message.reply_text(tr(language, "Số dư chưa đủ. Dùng /nap để nạp tiền.",
                                                  "Insufficient balance. Use /nap to add funds."))
                return
            except (NotFound, OutOfStock):
                await query.message.reply_text(tr(
                    language,
                    "Sản phẩm không còn bán hoặc kho không đủ cho số email đã nhập. "
                    "Chưa trừ tiền; hãy chọn mua lại.",
                    "The product is unavailable or stock is insufficient for those emails. "
                    "No money was deducted; please start again.",
                ))
                return
            except PriceChanged:
                await query.message.reply_text(tr(language,
                    "Giá sản phẩm đã đổi. Hãy xem lại giá mới trước khi mua.",
                    "The product price changed. Check the new price before buying."))
                return
            except SaleCouponConflict:
                await query.message.reply_text(
                    tr(language,
                       "Sản phẩm đang sale nên không dùng đồng thời mã giảm giá. Hãy chọn mua lại.",
                       "Coupons cannot be combined with the sale price. Start the purchase again.")
                )
                return
            except CouponUnavailable:
                await query.message.reply_text(tr(language,
                    "Mã giảm giá không còn dùng được. Hãy chọn mua lại.",
                    "This coupon is no longer available. Start the purchase again."))
                return
            file_product = any(item.get("delivery_type") == "file" for item in order["orders"])
            introduction = tr(
                language,
                f"Mua thành công {order['quantity']} {'file' if file_product else 'tài khoản'} {order['name']}! "
                f"Thanh toán {vnd(order['price'])}. "
                f"Số dư còn lại: {vnd(order['balance'])}.",
                f"Purchase successful! {order['quantity']} {'file(s)' if file_product else 'account(s)'} of {order['name']} for "
                f"{vnd(order['price'])}! Remaining balance: {vnd(order['balance'])}.",
            )
            await self.send_order_delivery_reply(
                query.message, language, order["orders"], introduction,
            )
            if order["low_stock_alert"]:
                await self.notify_low_stock(context.bot, order)
        elif data.startswith("confirmbundle:"):
            pending = context.user_data.pop("pending_bundle", None)
            if not pending or pending[0] != data.split(":", 1)[1]:
                await query.message.reply_text(tr(language,
                    "Xác nhận này đã hết hiệu lực. Hãy chọn mua lại.",
                    "This confirmation has expired. Please start the purchase again."))
                return
            try:
                sale = self.store.purchase_bundle(
                    user_id, pending[1], expected_price=pending[2],
                    coupon_code=pending[3],
                    customer_email=pending[4] if len(pending) > 4 else None,
                    notify_admin_ids=self.config.admin_ids,
                )
            except (EmailRequired, ValueError):
                context.user_data["awaiting_order_email"] = (
                    "bundle", pending[1], pending[3] if len(pending) > 3 else None,
                )
                await query.message.reply_text(tr(
                    language,
                    "Gói này cần email hợp lệ trước khi mua. Gửi một hoặc nhiều email "
                    "cách nhau bằng dấu phẩy, hoặc /cancel.",
                    "This bundle requires valid email before purchase. Send one or more "
                    "emails separated by commas, or /cancel to stop.",
                ))
                return
            except InsufficientBalance:
                await query.message.reply_text(tr(language, "Số dư chưa đủ. Dùng /nap để nạp tiền.",
                                                  "Insufficient balance. Use /nap to add funds."))
                return
            except (NotFound, OutOfStock):
                await query.message.reply_text(tr(language, "Gói vừa hết hàng hoặc ngừng bán.",
                                                  "This bundle just sold out or became unavailable."))
                return
            except PriceChanged:
                await query.message.reply_text(tr(language,
                    "Giá gói đã đổi. Hãy xem lại giá mới trước khi mua.",
                    "The bundle price changed. Check the new price before buying."))
                return
            except CouponUnavailable:
                await query.message.reply_text(tr(language,
                    "Mã giảm giá không còn dùng được. Hãy chọn mua lại.",
                    "This coupon is no longer available. Start the purchase again."))
                return
            introduction = tr(
                language,
                f"Mua thành công gói {sale['name']}! Thanh toán {vnd(sale['price'])}. "
                f"Số dư còn lại: {vnd(sale['balance'])}.",
                f"Bundle {sale['name']} purchased for {vnd(sale['price'])}! "
                f"Remaining balance: {vnd(sale['balance'])}.",
            )
            await self.send_order_delivery_reply(
                query.message, language, sale["orders"], introduction,
            )
            for alert in sale["low_stock_alerts"]:
                await self.notify_low_stock(context.bot, alert)
        elif data.startswith("purchase:"):
            try:
                purchase_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            await self.send_purchase_detail(query.message, user_id, purchase_id)
        elif data.startswith("order:"):
            # Old buttons still contain an account item ID. Open its full order.
            try:
                order_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            order = self.store.get_order(user_id, order_id)
            if not order:
                await query.message.reply_text(tr(language,
                    "Không tìm thấy đơn hàng của bạn.", "Your order was not found."))
                return
            await self.send_purchase_detail(
                query.message, user_id, order["purchase_id"] or order_id,
            )
        elif data.startswith("copyorder:"):
            try:
                purchase_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            purchase = self.store.get_purchase(user_id, purchase_id)
            if not purchase:
                await query.message.reply_text(tr(language,
                    "Không tìm thấy đơn hàng của bạn.", "Your order was not found."))
                return
            if any(file_only_delivery(item) for item in purchase["items"]):
                extension = order_delivery_extension(purchase["items"])
                await query.message.reply_text(tr(language,
                    f"Đơn này chỉ giao nội dung trong file {extension}. Bấm “📄 Tải file {extension}” ở đơn hàng.",
                    f"This order is delivered only in {extension}. Tap “📄 Download {extension} file”."))
                return
            account_text = "\n".join(item["secret"] for item in purchase["items"])
            units = telegram_text_units(account_text)
            if units <= 3900:
                await query.message.reply_text(
                    account_text,
                    entities=[MessageEntity(type=MessageEntity.PRE, offset=0, length=units)],
                    protect_content=True,
                )
            else:
                await query.message.reply_document(
                    document=InputFile(
                        BytesIO(account_delivery_file(purchase["items"], language)),
                        filename=f"don-hang-{purchase_id}.txt",
                    ),
                    caption=tr(language, "📄 File tài khoản của đơn",
                               "📄 Order accounts text file"),
                )
        elif data.startswith("orderfile:"):
            try:
                purchase_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            purchase = self.store.get_purchase(user_id, purchase_id)
            if not purchase:
                await query.message.reply_text(tr(language,
                    "Không tìm thấy đơn hàng của bạn.", "Your order was not found."))
                return
            try:
                has_file_product = any(item.get("delivery_type") == "file"
                                       for item in purchase["items"])
                document_bytes, extension = order_delivery_file(purchase["items"], language)
                await query.message.reply_document(
                    document=InputFile(
                        BytesIO(document_bytes),
                        filename=f"don-hang-{purchase_id}{extension}",
                    ),
                    caption=tr(language,
                               f"📄 File sản phẩm của đơn hàng ({extension})" if has_file_product
                               else "📄 File tài khoản của toàn bộ đơn hàng",
                               f"📄 {extension} file for the complete order"),
                )
                log.info("Resent %s order file for purchase %s", extension, purchase_id)
            except TelegramError as exc:
                log.warning("Could not resend %s order file for purchase %s: %s",
                            extension, purchase_id, type(exc).__name__)
                await query.message.reply_text(tr(language,
                    f"Chưa gửi được file {extension}. Vui lòng thử lại sau.",
                    f"The {extension} file could not be sent. Please try again later."))
        elif data.startswith("warranty:start:") or data.startswith("warranty:status:"):
            try:
                action, order_id = data.split(":", 2)[1:]
                order_id = int(order_id)
            except ValueError:
                return
            order = self.store.get_order(user_id, order_id)
            if not order:
                await query.message.reply_text(tr(language, "Không tìm thấy đơn hàng của bạn.",
                                                  "Your order was not found."))
                return
            purchase_id = order["purchase_id"] or order_id
            ticket = self.store.get_latest_warranty(user_id, purchase_id)
            if action == "status":
                if not ticket:
                    await query.message.reply_text(tr(language,
                        "Đơn này chưa có yêu cầu bảo hành.",
                        "This order has no warranty request yet."))
                    return
                if ticket["status"] == "open":
                    state = tr(language, "đang chờ xử lý", "pending")
                elif ticket["resolved_privately"]:
                    state = tr(language, "đã xử lý", "resolved")
                else:
                    state = tr(language, "đã phản hồi", "answered")
                content = tr(language,
                    f"🛡 Bảo hành #{ticket['id']} · đơn #{purchase_id} · {state}\n"
                    f"Nội dung: {ticket['note']}",
                    f"🛡 Warranty #{ticket['id']} · order #{purchase_id} · {state}\n"
                    f"Request: {ticket['note']}")
                if ticket["reply"]:
                    content += tr(language, f"\n\nPhản hồi: {ticket['reply']}",
                                  f"\n\nReply: {ticket['reply']}")
                rows = [[InlineKeyboardButton(
                    tr(language, "⬅️ Xem đơn", "⬅️ View order"),
                    callback_data=f"purchase:{purchase_id}",
                )]]
                if ticket["status"] != "open":
                    rows.insert(0, [InlineKeyboardButton(
                        tr(language, "🛡 Gửi yêu cầu bảo hành mới",
                           "🛡 Send a new warranty request"),
                        callback_data=f"warranty:start:{purchase_id}",
                    )])
                await query.message.reply_text(content, protect_content=True,
                    reply_markup=InlineKeyboardMarkup(rows))
                return
            if ticket and ticket["status"] == "open":
                await query.message.reply_text(
                    tr(language,
                       f"Yêu cầu bảo hành #{ticket['id']} cho đơn #{purchase_id} đang chờ xử lý.",
                       f"Warranty request #{ticket['id']} for order #{purchase_id} is pending.")
                )
                return
            context.user_data["awaiting_warranty"] = purchase_id
            await query.message.reply_text(
                tr(language,
                   f"Gửi nội dung cần bảo hành cho toàn bộ đơn #{purchase_id} "
                   "(tối đa 1000 ký tự). "
                   "Không gửi mật khẩu khác hoặc mã OTP. Gửi /cancel để hủy.",
                   f"Describe the issue with the whole order #{purchase_id} "
                   "(up to 1000 characters). "
                   "Do not send another password or an OTP. Send /cancel to stop.")
            )

    async def admin_callback(self, message, data: str, context, admin_id: int):
        parts = data.split(":")
        if parts == ["admin", "usersearch"]:
            await self.begin_admin_user_search(message, context)
            return
        if parts == ["admin", "salessearch"]:
            await self.begin_admin_sales_search(message, context)
            return
        if parts == ["admin", "exportusers"]:
            await self.send_admin_users_export(message)
            return
        if parts == ["admin", "exportsold"]:
            await self.send_admin_sold_products_export(message)
            return
        if parts == ["admin", "exportaudit"]:
            await self.send_admin_audit_export(message)
            return
        if len(parts) == 3 and parts[:2] == ["admin", "message"]:
            try:
                purchase_id = int(parts[2])
            except ValueError:
                return
            purchase = self.store.get_admin_purchase(purchase_id)
            if not purchase:
                await message.reply_text("Không tìm thấy đơn hàng đã bán.")
                return
            context.user_data["awaiting_admin_order_message"] = (
                purchase_id, time.monotonic() + ORDER_CHAT_INPUT_SECONDS,
            )
            buyer = user_label(purchase["user_id"], purchase["username"],
                               purchase["display_name"])
            await message.reply_text(
                f"💬 Nhắn cho {buyer} về đơn #{purchase_id}.\n"
                "Gửi tin nhắn (tối đa 2000 ký tự) hoặc file dưới dạng tài liệu "
                "(.txt, .json, ...). Có thể thêm chú thích cho file; /cancel để hủy."
            )
            return
        if len(parts) >= 3 and parts[1] == "balancecredit":
            await self.admin_test_credit_callback(message, parts, context, admin_id)
            return
        if len(parts) >= 3 and parts[1] == "offer":
            await self.admin_offer_callback(message, data, context)
            return
        if len(parts) >= 3 and parts[1] == "catalog":
            await self.admin_catalog_callback(message, data, context)
            return
        if parts == ["admin", "home"]:
            await self.send_admin_menu(message)
            return
        if parts == ["admin", "report"]:
            await self.send_admin_report(message)
            return
        if len(parts) == 3 and parts[:2] == ["admin", "funnel"]:
            if parts[2] in ("7", "30"):
                await self.send_admin_funnel(message, int(parts[2]))
            return
        if len(parts) == 4 and parts[:2] == ["admin", "views"]:
            if parts[2] in ("7", "30"):
                try:
                    await self.send_admin_product_views(message, int(parts[2]),
                                                        max(0, int(parts[3])))
                except ValueError:
                    pass
            return
        if len(parts) == 5 and parts[:2] == ["admin", "viewproduct"]:
            if parts[3] in ("7", "30"):
                try:
                    await self.send_admin_product_viewers(
                        message, int(parts[2]), int(parts[3]), max(0, int(parts[4])),
                    )
                except ValueError:
                    pass
            return
        if len(parts) == 4 and parts[:2] == ["admin", "followers"]:
            try:
                await self.send_admin_product_followers(
                    message, int(parts[2]), max(0, int(parts[3])),
                )
            except ValueError:
                pass
            return
        if parts == ["admin", "export"]:
            await self.begin_admin_export(message, context)
            return
        if parts == ["admin", "status"]:
            await self.send_admin_status(message)
            return
        if parts == ["admin", "emailremind"]:
            await self.send_email_reminder_settings(message)
            return
        if parts == ["admin", "warrantyremind"]:
            await self.send_warranty_reminder_settings(message)
            return
        if parts == ["admin", "reconcile"]:
            await self.run_reconciliation(message)
            return
        if parts == ["admin", "broadcast"]:
            await self.begin_broadcast(message, context)
            return
        if parts == ["admin", "broadcast", "cancel"]:
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("pending_broadcast", None)
            await message.reply_text("Đã hủy thông báo. Chưa gửi cho khách nào.")
            return
        if len(parts) == 4 and parts[:3] == ["admin", "broadcast", "send"]:
            await self.confirm_broadcast(message, context, admin_id, parts[3])
            return
        if parts == ["admin", "matchcancel"]:
            context.user_data.pop("awaiting_manual_credit", None)
            context.user_data.pop("pending_manual_credit", None)
            await message.reply_text("Đã hủy xử lý giao dịch. Tiền chưa được cộng.")
            return
        if len(parts) == 4 and parts[1] == "credit":
            await self.confirm_manual_credit(message, context, admin_id, parts[2], parts[3])
            return
        if len(parts) == 4 and parts[1] == "email" and parts[3] in ("on", "off"):
            try:
                product_id = int(parts[2])
                self.store.set_requires_email(product_id, parts[3] == "on")
            except ValueError:
                return
            except NotFound:
                await message.reply_text("Không tìm thấy sản phẩm hoặc đã lưu trữ.")
                return
            await self.send_admin_product(message, product_id)
            return
        if len(parts) == 3 and parts[1] == "displayid":
            try:
                product_id = int(parts[2])
            except ValueError:
                return
            product = self.store.get_product(product_id)
            if not product:
                await message.reply_text("Không tìm thấy sản phẩm.")
                return
            context.user_data["awaiting_display_id"] = product_id
            await message.reply_text(
                f"Gửi số mới cho sản phẩm {product['name']} "
                f"(đang hiện #{product['display_id']}). "
                "Số từ 1 đến 999999. Nếu số đã thuộc sản phẩm khác, bot sẽ hoán đổi. "
                "Bấm Hủy nếu không muốn đổi.",
                reply_markup=self.admin_catalog_cancel_button(),
            )
            return
        if len(parts) == 4 and parts[1] == "moveproduct":
            try:
                product_id = int(parts[2])
                direction = {"up": -1, "down": 1}[parts[3]]
                result = self.store.shift_product(product_id, direction)
            except (ValueError, KeyError):
                return
            except NotFound:
                await message.reply_text("Không tìm thấy sản phẩm đang quản lý.")
                return
            await message.reply_text(
                f"Đã chuyển sản phẩm #{self.store.get_product(product_id)['display_id']} "
                f"đến vị trí {result['position']}/{result['count']} trong danh sách quản trị. "
                "Danh sách khách được xếp theo số hiển thị."
            )
            await self.send_admin_product(message, product_id)
            return
        if len(parts) == 3 and parts[1] == "postproduct":
            try:
                product_id = int(parts[2])
            except ValueError:
                return
            await self.send_admin_product_post(message, product_id)
            return
        if len(parts) == 3 and parts[1] == "ban":
            try:
                user_id = int(parts[2])
            except ValueError:
                return
            await self.confirm_ban_customer(message, user_id)
            return
        if len(parts) == 3 and parts[1] == "banconfirm":
            try:
                user_id = int(parts[2])
            except ValueError:
                return
            await self.ban_customer(message, user_id, admin_id)
            return
        if len(parts) == 3:
            paged = {
                "users": self.send_admin_users,
                "bans": self.send_admin_bans,
                "sales": self.send_admin_sales,
                "pendingemails": self.send_admin_pending_emails,
                "topups": self.send_admin_topups,
                "transactions": self.send_admin_transactions,
                "products": self.send_admin_products,
                "archived": self.send_admin_archived,
                "warranties": self.send_admin_warranties,
                "warrantiesall": self.send_admin_all_warranties,
                "bundles": self.send_admin_bundles,
                "coupons": self.send_admin_coupons,
                "unmatched": self.send_admin_unmatched,
                "manualcredits": self.send_admin_manual_credits,
                "audit": self.send_admin_audit,
            }
            try:
                number = int(parts[2])
            except ValueError:
                return
            if parts[1] in paged:
                await paged[parts[1]](message, max(0, number))
            elif parts[1] == "usersearch":
                await self.send_admin_user_search_results(message, context, max(0, number))
            elif parts[1] == "salessearch":
                await self.send_admin_sales_search_results(message, context, max(0, number))
            elif parts[1] == "emailremind":
                await self.change_email_reminder(message, number)
            elif parts[1] == "warrantyremind":
                await self.change_warranty_reminder(message, number)
            elif parts[1] == "emaildone":
                await self.mark_email_order_done(message, number, admin_id)
            elif parts[1] == "unban":
                await self.unban_customer(message, number, admin_id)
            elif parts[1] == "user":
                await self.send_admin_user(message, number)
            elif parts[1] == "product":
                await self.send_admin_product(message, number)
            elif parts[1] == "order":
                await self.send_admin_order_contact(message, number)
            elif parts[1] == "purchase":
                await self.send_admin_purchase(message, number)
            elif parts[1] == "stock":
                await self.send_admin_stock_item(message, number)
            elif parts[1] == "warranty":
                await self.send_admin_warranty(message, number)
            elif parts[1] == "warrantyprivate":
                await self.confirm_private_warranty(message, number)
            elif parts[1] == "warrantyprivateconfirm":
                await self.mark_private_warranty_done(message, number, admin_id)
            elif parts[1] == "bundle":
                await self.send_admin_bundle(message, number)
            elif parts[1] == "match":
                await self.send_admin_match_detail(message, context, number)
        elif len(parts) == 4 and parts[1] == "userviews":
            try:
                await self.send_admin_user_views(
                    message, int(parts[2]), max(0, int(parts[3])),
                )
            except ValueError:
                return
        elif len(parts) == 4 and parts[1] == "userproducts":
            try:
                await self.send_admin_user_products(
                    message, int(parts[2]), max(0, int(parts[3])),
                )
            except ValueError:
                return
        elif len(parts) == 4 and parts[1] == "inventory":
            try:
                await self.send_admin_inventory(message, int(parts[2]), max(0, int(parts[3])))
            except ValueError:
                return
        elif len(parts) == 4 and parts[1] == "sold":
            try:
                await self.send_admin_sold_stock(message, int(parts[2]), max(0, int(parts[3])))
            except ValueError:
                return
        elif len(parts) == 4 and parts[1] == "solditem":
            try:
                await self.send_admin_sold_stock_item(message, int(parts[2]),
                                                      max(0, int(parts[3])))
            except ValueError:
                return

    async def send_admin_menu(self, message):
        await message.reply_text(
            "🛠 QUẢN TRỊ SHOP\nChọn mục để xem người dùng, đơn, tiền nạp và kho.\n\n"
            "📦 Sản phẩm: bấm vào mục bên dưới để thêm, sửa giá, nhập kho, "
            "xem tài khoản đã bán, tạo sale, ẩn hoặc lưu trữ bằng nút.\n"
            "Đơn email: /orderemail ID; xử lý xong: /emaildone ID\n"
            "Đơn email chờ: /pendingemails; thời gian nhắc: /emailremind phút\n"
            "🎁 Gói sản phẩm: bấm để tạo, sửa, ẩn hoặc khôi phục gói.\n"
            "🏷 Mã giảm giá: bấm để tạo, sửa mức giảm, số lượt, hạn dùng hoặc bật/tắt.\n"
            "Bảo hành: /warranties (mở yêu cầu để đánh dấu xử lý riêng), "
            "/resolvewarranty ID | Phản hồi (gửi cho khách); "
            "nhắc sau: /warrantyremind phút\n"
            "Báo cáo: /report hoặc /report YYYY-MM-DD\n"
            "Hành trình khách: /funnel 7 hoặc /funnel 30\n"
            "👀 Sản phẩm khách xem: bấm mục bên dưới để xem lượt mở và khách quan tâm.\n"
            "Xuất CSV: /export YYYY-MM-DD YYYY-MM-DD\n"
            "Giao dịch chưa khớp: /unmatched; đã xử lý tay: /manualcredits\n"
            "Trạng thái vận hành: /status\n"
            "Nhật ký quản trị: /audit\n"
            "Người dùng bị cấm: /banned; bỏ cấm: /unban Telegram_ID\n"
            "Thông báo cho khách: /broadcast; xem tiến độ: /broadcaststatus\n"
            "Đối soát ngay: /reconcile",
            reply_markup=admin_keyboard(),
        )

    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        parts = update.message.text.split(maxsplit=1)
        content = parts[1] if len(parts) == 2 else None
        await self.begin_broadcast(update.message, context, content)

    async def begin_broadcast(self, message, context, content: str | None = None):
        for key in (
            "awaiting_topup", "awaiting_warranty", "awaiting_bulk_stock",
            "awaiting_coupon", "awaiting_order_email", "awaiting_export_range",
            "awaiting_manual_credit",
            "pending_manual_credit", "pending_purchase", "pending_bundle",
            "pending_broadcast",
            "awaiting_admin_credit_target", "awaiting_admin_credit_amount",
            "pending_admin_test_credit",
        ):
            context.user_data.pop(key, None)
        context.user_data["awaiting_broadcast"] = True
        if content is None:
            await message.reply_text(
                f"Gửi nội dung thông báo cho khách (tối đa {BROADCAST_MAX_CHARS} ký tự). "
                "Bot sẽ cho xem trước và hỏi xác nhận. Gửi /cancel để hủy."
            )
        else:
            await self.prepare_broadcast(message, context, content)

    async def prepare_broadcast(self, message, context, content: str):
        content = content.strip()
        if not content or len(content.encode("utf-16-le")) // 2 > BROADCAST_MAX_CHARS:
            await message.reply_text(
                f"Nội dung phải có từ 1 đến {BROADCAST_MAX_CHARS} ký tự. "
                "Gửi lại hoặc /cancel để hủy."
            )
            return
        recipients = await asyncio.to_thread(
            self.store.count_broadcast_recipients, self.config.admin_ids,
        )
        if not recipients:
            context.user_data.pop("awaiting_broadcast", None)
            await message.reply_text("Chưa có khách nào từng dùng bot để nhận thông báo.")
            return
        context.user_data.pop("awaiting_broadcast", None)
        nonce = secrets.token_hex(8)
        context.user_data["pending_broadcast"] = (
            nonce, content, time.monotonic() + 300,
        )
        await message.reply_text(
            f"XEM TRƯỚC THÔNG BÁO ({recipients} khách)\n\n"
            f"{BROADCAST_PREFIX}{content}\n\n"
            "Chỉ bấm Gửi khi nội dung đã đúng. Nút hết hạn sau 5 phút.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Gửi cho tất cả", callback_data=f"admin:broadcast:send:{nonce}"),
                InlineKeyboardButton("Hủy", callback_data="admin:broadcast:cancel"),
            ]]),
        )

    async def confirm_broadcast(self, message, context, admin_id: int, nonce: str):
        pending = context.user_data.pop("pending_broadcast", None)
        if not pending or pending[0] != nonce or time.monotonic() > pending[2]:
            await message.reply_text("Xác nhận đã hết hạn hoặc không hợp lệ. Dùng /broadcast để tạo lại.")
            return
        try:
            batch = await asyncio.to_thread(
                self.store.create_broadcast, admin_id, pending[1], self.config.admin_ids,
            )
        except Exception:
            log.exception("Could not queue broadcast")
            await message.reply_text("Chưa lưu được thông báo. Dùng /broadcast để thử lại.")
            return
        self.audit_admin_action(
            admin_id, "broadcast", "broadcast", batch["id"],
            f"recipients={batch['recipients']}; chars={len(pending[1])}",
        )
        await message.reply_text(
            f"Đã xếp thông báo #{batch['id']} cho {batch['recipients']} khách. "
            f"Bot đang gửi lần lượt. Dùng /broadcaststatus {batch['id']} để xem tiến độ."
        )

    async def broadcast_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        args = context.args or []
        if len(args) > 1 or (args and (not args[0].isascii() or not args[0].isdigit())):
            await update.message.reply_text("Dùng: /broadcaststatus hoặc /broadcaststatus ID")
            return
        batch_id = int(args[0]) if args else None
        status = await asyncio.to_thread(self.store.get_broadcast_status, batch_id)
        if not status:
            await update.message.reply_text("Chưa tìm thấy đợt thông báo này.")
            return
        state = "đã hoàn tất" if not status["pending"] else "đang gửi"
        await update.message.reply_text(
            f"Thông báo #{status['id']} · {state}\n"
            f"Tổng khách: {status['recipients']}\n"
            f"Đã gửi: {status['sent']}\n"
            f"Chưa gửi: {status['pending']}\n"
            f"Không gửi được: {status['failed']}"
        )

    async def send_admin_status(self, message):
        snapshot = await asyncio.to_thread(
            self.store.get_operational_status, self.config.bank_account_number,
        )
        try:
            backup_at = await asyncio.to_thread(latest_backup_at, backup_directory())
            backup_error = False
        except OSError as exc:
            log.warning("Could not inspect backup directory: %s", type(exc).__name__)
            backup_at = None
            backup_error = True

        now = datetime.now(timezone.utc)
        webhook_at = parse_stored_time(snapshot["last_webhook_at"])
        reconciliation_at = parse_stored_time(snapshot["last_reconciliation_at"])
        listener = "đang chạy" if self.server_thread and self.server_thread.is_alive() else "cần kiểm tra"
        webhook_text = status_time(webhook_at) if webhook_at else "chưa ghi nhận từ khi cập nhật"
        if not self.config.sepay_api_token:
            reconciliation_text = "chưa cấu hình API Token"
        elif not reconciliation_at:
            reconciliation_text = "chưa có lần thành công"
        else:
            reconciliation_text = status_time(reconciliation_at)
            if now - reconciliation_at > STATUS_RECONCILIATION_MAX_AGE:
                reconciliation_text += " (quá 10 phút, cần kiểm tra)"

        if backup_error:
            backup_text = "không đọc được thư mục sao lưu"
        elif not backup_at:
            backup_text = "chưa thấy bản sao trong thư mục cấu hình"
        else:
            backup_text = status_time(backup_at)
            if now - backup_at > STATUS_BACKUP_MAX_AGE:
                backup_text += " (quá 36 giờ, cần kiểm tra)"

        low_stock = snapshot["low_stock"]
        lines = [
            "🩺 TRẠNG THÁI SHOP (GMT+7)",
            f"Webhook nội bộ: {listener}",
            f"Webhook SePay xử lý gần nhất: {webhook_text}",
            f"Đối soát thành công gần nhất: {reconciliation_text}",
            f"Bản sao lưu đã kiểm tra gần nhất: {backup_text}",
            f"Giao dịch chưa khớp: {snapshot['unmatched_count']}",
            f"Sản phẩm sắp hết (còn 0-2 tài khoản): {len(low_stock)}",
        ]
        for product in low_stock[:5]:
            name = " ".join(product["name"].split())[:50]
            lines.append(f"#{product['id']} {name}: còn {product['available']}")
        if len(low_stock) > 5:
            lines.append(f"... và {len(low_stock) - 5} sản phẩm khác")
        lines.append("Webhook có thể chưa có mốc nếu chưa phát sinh giao dịch; ngrok công khai do cửa sổ giám sát kiểm tra.")
        await message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Làm mới", callback_data="admin:status")],
                [InlineKeyboardButton("⚠️ Chưa khớp", callback_data="admin:unmatched:0"),
                 InlineKeyboardButton("📦 Sản phẩm", callback_data="admin:products:0")],
                [InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")],
            ]),
        )

    async def send_admin_users(self, message, page: int = 0):
        total_customers = self.store.count_users(self.config.admin_ids)
        users = self.store.list_users(
            PAGE_SIZE + 1, page * PAGE_SIZE, self.config.admin_ids,
        )
        visible = users[:PAGE_SIZE]
        lines = ["👥 NGƯỜI DÙNG", f"Tổng khách từng truy cập: {total_customers}"]
        rows = [[InlineKeyboardButton(
            "🔎 Tìm người dùng", callback_data="admin:usersearch",
        )], [InlineKeyboardButton(
            "📥 Xuất file người dùng", callback_data="admin:exportusers",
        )]]
        for user in visible:
            name = user_label(user["telegram_id"], user["username"], user["display_name"])
            lines.append(
                f"{name} · Số dư {vnd(user['balance'])} "
                f"· {user['order_count']} đơn"
            )
            bought = self.store.list_purchased_products(user["telegram_id"], 3)
            if bought:
                preview = ", ".join(purchased_product_label(product, 32)
                                    for product in bought[:2])
                lines.append(f"  Đã mua: {preview}{', …' if len(bought) > 2 else ''}")
            viewed = self.store.list_user_product_views(user["telegram_id"], 3)
            if viewed:
                preview = ", ".join(
                    f"#{product['display_id']} {' '.join(product['name'].split())[:28]}"
                    for product in viewed[:2]
                )
                lines.append(f"  Đã xem: {preview}{', …' if len(viewed) > 2 else ''}")
            rows.append([InlineKeyboardButton(
                f"Xem ID {user['telegram_id']}", callback_data=f"admin:user:{user['telegram_id']}"
            )])
        if not visible:
            lines.append("Chưa có người dùng ở trang này.")
        rows.extend(admin_footer("admin:users", page, len(users) > PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_audit(self, message, page: int = 0):
        entries = self.store.list_admin_audit(PAGE_SIZE + 1, page * PAGE_SIZE)
        visible = entries[:PAGE_SIZE]
        lines = [
            "📜 NHẬT KÝ QUẢN TRỊ",
            "Lưu thao tác của admin, không lưu mật khẩu, mã OTP hoặc nội dung tài khoản.",
        ]
        for entry in visible:
            actor = user_label(entry["admin_id"], entry.get("username"), entry.get("display_name"))
            stamp = status_time(parse_stored_time(entry["created_at"]))
            target = ""
            if entry.get("target_type") or entry.get("target_id"):
                target = f" · {entry.get('target_type') or 'đối tượng'}"
                if entry.get("target_id") is not None:
                    target += f" {entry['target_id']}"
            details = " ".join((entry.get("details") or "").split())
            if len(details) > 100:
                details = details[:97] + "…"
            line = f"#{entry['id']} · {stamp} · {actor} · {entry['action']}{target}"
            if details:
                line += f"\n  {details}"
            lines.append(line)
        if not visible:
            lines.append("Chưa có thao tác quản trị nào được ghi nhận.")
        rows = [[InlineKeyboardButton(
            "📥 Xuất nhật ký CSV", callback_data="admin:exportaudit",
        )]]
        rows.extend(admin_footer("admin:audit", page, len(entries) > PAGE_SIZE))
        rows.append([InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_audit_export(self, message):
        try:
            export = await asyncio.to_thread(build_admin_audit_csv, self.store)
        except ExportTooLarge:
            await message.reply_text("Nhật ký quá lớn để xuất một file. Hãy xuất sớm hơn hoặc xóa bản sao cũ.")
            return
        await message.reply_document(
            document=InputFile(BytesIO(export["content"]), filename=export["filename"]),
            caption=f"📜 Nhật ký quản trị · {export['entries']} dòng",
            protect_content=True,
        )

    async def begin_admin_user_search(self, message, context):
        context.user_data.pop("admin_user_search_query", None)
        context.user_data["awaiting_admin_user_search"] = True
        await message.reply_text(
            "🔎 TÌM NGƯỜI DÙNG\n"
            "Gửi Telegram ID, @username hoặc một phần tên khách. "
            "Gửi /cancel để hủy.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Danh sách người dùng",
                                     callback_data="admin:users:0"),
            ]]),
        )

    async def receive_admin_user_search(self, message, context, raw: str):
        query = " ".join(raw.strip().split())
        context.user_data["admin_user_search_query"] = query
        await self.send_admin_user_search_results(message, context, 0)

    async def send_admin_user_search_results(self, message, context, page: int = 0):
        query = context.user_data.get("admin_user_search_query")
        if not query:
            await self.begin_admin_user_search(message, context)
            return
        try:
            result = self.store.search_users(
                query, PAGE_SIZE, page * PAGE_SIZE, self.config.admin_ids,
            )
        except ValueError:
            context.user_data.pop("admin_user_search_query", None)
            context.user_data["awaiting_admin_user_search"] = True
            await message.reply_text(
                "Từ khóa không hợp lệ. Nhập Telegram ID, @username hoặc một phần tên "
                "(tối đa 80 ký tự); /cancel để hủy."
            )
            return
        context.user_data.pop("awaiting_admin_user_search", None)
        lines = ["🔎 KẾT QUẢ TÌM NGƯỜI DÙNG",
                 f"Từ khóa: {query}", f"Tìm thấy: {result['total']} khách"]
        rows = []
        for user in result["users"]:
            label = user_label(user["telegram_id"], user["username"],
                               user["display_name"])
            lines.append(f"{label} · Số dư {vnd(user['balance'])} "
                         f"· {user['order_count']} đơn")
            rows.append([InlineKeyboardButton(
                f"Xem ID {user['telegram_id']}",
                callback_data=f"admin:user:{user['telegram_id']}",
            )])
        if not result["users"]:
            lines.append("Không có khách phù hợp ở trang này.")
        rows.extend(admin_footer("admin:usersearch", page,
                                 (page + 1) * PAGE_SIZE < result["total"]))
        rows.insert(-1, [InlineKeyboardButton("🔎 Tìm lại",
                                            callback_data="admin:usersearch")])
        rows.insert(-1, [InlineKeyboardButton("⬅️ Danh sách người dùng",
                                            callback_data="admin:users:0")])
        await message.reply_text("\n".join(lines),
                                 reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_bans(self, message, page: int = 0):
        bans = self.store.list_spam_bans(PAGE_SIZE + 1, page * PAGE_SIZE)
        visible = bans[:PAGE_SIZE]
        lines = ["🚫 KHÁCH BỊ CẤM"]
        rows = []
        for ban in visible:
            user_id = ban["user_id"]
            name = user_label(user_id, ban["username"], ban["display_name"])
            reason = "spam" if ban.get("reason") == "spam" else "quản trị viên"
            lines.append(
                f"{name} · lý do: {reason} · từ "
                f"{status_time(parse_stored_time(ban['banned_at']))}"
            )
            rows.append([
                InlineKeyboardButton(f"👤 Xem ID {user_id}", callback_data=f"admin:user:{user_id}"),
                InlineKeyboardButton("Bỏ cấm", callback_data=f"admin:unban:{user_id}"),
            ])
        if not visible:
            lines.append("Không có khách nào bị cấm.")
        rows.extend(admin_footer("admin:bans", page, len(bans) > PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def unban_customer(self, message, user_id: int, admin_id: int | None = None):
        if user_id <= 0:
            await message.reply_text("Telegram ID không hợp lệ.")
            return
        if not self.store.unban_user(user_id):
            await message.reply_text(f"ID {user_id} hiện không bị cấm.")
            return
        self.banned_user_ids.discard(user_id)
        self.limiter.reset_user(user_id)
        if admin_id is not None:
            self.audit_admin_action(admin_id, "unban_user", "user", user_id)
        await message.reply_text(f"Đã bỏ cấm ID {user_id}. Khách có thể dùng bot lại.")

    async def confirm_ban_customer(self, message, user_id: int):
        if user_id <= 0:
            await message.reply_text("Telegram ID không hợp lệ.")
            return
        if user_id in self.config.admin_ids:
            await message.reply_text("Không thể ban tài khoản quản trị viên.")
            return
        user = self.store.get_user_summary(user_id)
        if not user:
            await message.reply_text("Không tìm thấy người dùng.")
            return
        if self.store.get_spam_ban(user_id):
            await message.reply_text("Người dùng này đã bị ban.")
            return
        label = user_label(user_id, user["username"], user["display_name"])
        await message.reply_text(
            f"⚠️ Bạn có chắc muốn ban {label}?\n"
            "Khách sẽ không thể sử dụng bot và không nhận thông báo quảng bá. "
            "Giao dịch đang chờ thanh toán vẫn được xử lý nếu ngân hàng ghi nhận tiền.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Xác nhận ban",
                                      callback_data=f"admin:banconfirm:{user_id}"),
                 InlineKeyboardButton("Hủy", callback_data=f"admin:user:{user_id}")],
            ]),
        )

    async def ban_customer(self, message, user_id: int, admin_id: int | None = None):
        if user_id <= 0:
            await message.reply_text("Telegram ID không hợp lệ.")
            return
        if user_id in self.config.admin_ids:
            await message.reply_text("Không thể ban tài khoản quản trị viên.")
            return
        if not self.store.ban_user(user_id):
            if self.store.get_user_summary(user_id) is None:
                await message.reply_text("Không tìm thấy người dùng.")
            else:
                await message.reply_text("Người dùng này đã bị ban.")
            return
        self.banned_user_ids.add(user_id)
        self.limiter.reset_user(user_id)
        if admin_id is not None:
            self.audit_admin_action(admin_id, "ban_user", "user", user_id)
        await message.reply_text(f"Đã ban ID {user_id}. Khách không thể dùng bot nữa.")
        await self.send_admin_user(message, user_id)

    async def send_admin_user(self, message, user_id: int):
        user = self.store.get_user_summary(user_id)
        if not user:
            await message.reply_text("Không tìm thấy người dùng.")
            return
        lines = [
            f"👤 {user_label(user_id, user['username'], user['display_name'])}",
            f"Số dư: {vnd(user['balance'])}",
            f"Tổng nạp đã nhận: {vnd(user['total_received'])}",
            f"Tiền thử nghiệm admin đã cộng: {vnd(user['total_test_credit'])}",
            f"Tổng đã mua: {vnd(user['total_spent'])}",
            f"Số đơn: {user['order_count']}",
            "\nSản phẩm đã xem (lượt mở chi tiết):",
        ]
        ban = self.store.get_spam_ban(user_id)
        if ban:
            reason = "do spam" if ban.get("reason") == "spam" else "bởi quản trị viên"
            lines.insert(1, f"🚫 Bị cấm {reason} từ "
                         f"{status_time(parse_stored_time(ban['banned_at']))}")
        viewed = self.store.list_user_product_views(user_id, 4)
        lines.extend(viewed_product_label(product, show_time=True) for product in viewed[:3])
        if len(viewed) > 3:
            lines.append("… còn sản phẩm khác; bấm Xem tất cả bên dưới.")
        if not viewed:
            lines.append("Chưa ghi nhận lượt mở chi tiết sản phẩm.")
        lines.append("\nSản phẩm đã mua:")
        products = self.store.list_purchased_products(user_id, 6)
        lines.extend(purchased_product_label(product) for product in products[:5])
        if len(products) > 5:
            lines.append("… còn sản phẩm khác; bấm Xem tất cả bên dưới.")
        if not products:
            lines.append("Chưa mua hàng.")
        lines.append("\nĐơn gần đây:")
        purchases = self.store.list_purchases(user_id, 5)
        for purchase in purchases:
            counts = {}
            for item in purchase["items"]:
                counts[item["name"]] = counts.get(item["name"], 0) + 1
            names = [purchased_product_label({"name": name, "quantity": count}, 36)
                     for name, count in counts.items()]
            preview = ", ".join(names[:2])
            if len(names) > 2:
                preview += f" và {len(names) - 2} loại khác"
            lines.append(f"#{purchase['id']} · {preview} · {vnd(purchase['price'])}")
        if not purchases:
            lines.append("Chưa có đơn hàng.")
        lines.append("\nYêu cầu nạp gần đây:")
        topups = self.store.list_topups(user_id, 5)
        lines.extend(
            f"#{topup['id']} {topup['code']} · đã nhận {vnd(topup['paid_amount'])}"
            for topup in topups
        )
        if not topups:
            lines.append("Chưa tạo yêu cầu nạp.")
        test_credits = self.store.list_test_credits(user_id, 3)
        if test_credits:
            lines.append("\nTiền thử nghiệm gần đây:")
            lines.extend(f"#{credit['id']} +{vnd(credit['amount'])} "
                         f"· admin ID {credit['admin_id']}" for credit in test_credits)
        rows = []
        if ban:
            rows.append([InlineKeyboardButton("Bỏ cấm khách này", callback_data=f"admin:unban:{user_id}")])
        elif user_id not in self.config.admin_ids:
            rows.append([InlineKeyboardButton("🚫 Ban người dùng",
                                              callback_data=f"admin:ban:{user_id}")])
        if viewed:
            rows.append([InlineKeyboardButton(
                "👀 Xem tất cả sản phẩm đã xem",
                callback_data=f"admin:userviews:{user_id}:0",
            )])
        if products:
            rows.append([InlineKeyboardButton(
                "📦 Xem tất cả sản phẩm đã mua",
                callback_data=f"admin:userproducts:{user_id}:0",
            )])
        for purchase in purchases:
            rows.append([InlineKeyboardButton(
                f"🧾 Xem đơn #{purchase['id']}",
                callback_data=f"admin:purchase:{purchase['id']}",
            )])
        rows.append([InlineKeyboardButton(
            "🧪 Cộng tiền thử nghiệm", callback_data=f"admin:balancecredit:user:{user_id}",
        )])
        rows.append(
            [InlineKeyboardButton("⬅️ Người dùng", callback_data="admin:users:0"),
             InlineKeyboardButton("🛠 Quản trị", callback_data="admin:home")]
        )
        await message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def send_admin_user_views(self, message, user_id: int,
                                    page: int = 0) -> None:
        user = self.store.get_user_summary(user_id)
        if not user:
            await message.reply_text("Không tìm thấy người dùng.")
            return
        viewed = self.store.list_user_product_views(
            user_id, PAGE_SIZE + 1, page * PAGE_SIZE,
        )
        visible = viewed[:PAGE_SIZE]
        lines = [
            "👀 SẢN PHẨM KHÁCH ĐÃ XEM",
            user_label(user_id, user["username"], user["display_name"]),
            "Chỉ tính lần mở chi tiết từ khi bật thống kê.",
            "",
        ]
        lines.extend(viewed_product_label(product, 58, True) for product in visible)
        if not visible:
            lines.append("Không có lượt xem ở trang này.")
        rows = []
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "◀ Trước", callback_data=f"admin:userviews:{user_id}:{page - 1}",
            ))
        if len(viewed) > PAGE_SIZE:
            navigation.append(InlineKeyboardButton(
                "Sau ▶", callback_data=f"admin:userviews:{user_id}:{page + 1}",
            ))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(
            "⬅️ Người dùng", callback_data=f"admin:user:{user_id}",
        )])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_user_products(self, message, user_id: int,
                                       page: int = 0) -> None:
        user = self.store.get_user_summary(user_id)
        if not user:
            await message.reply_text("Không tìm thấy người dùng.")
            return
        products = self.store.list_purchased_products(
            user_id, PAGE_SIZE + 1, page * PAGE_SIZE,
        )
        visible = products[:PAGE_SIZE]
        lines = [
            "📦 SẢN PHẨM KHÁCH ĐÃ MUA",
            user_label(user_id, user["username"], user["display_name"]),
            f"Số đơn: {user['order_count']}",
            "",
        ]
        lines.extend(purchased_product_label(product, 72) for product in visible)
        if not visible:
            lines.append("Không có sản phẩm nào ở trang này.")
        rows = []
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "◀ Trước", callback_data=f"admin:userproducts:{user_id}:{page - 1}",
            ))
        if len(products) > PAGE_SIZE:
            navigation.append(InlineKeyboardButton(
                "Sau ▶", callback_data=f"admin:userproducts:{user_id}:{page + 1}",
            ))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(
            "⬅️ Người dùng", callback_data=f"admin:user:{user_id}",
        )])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    @staticmethod
    def clear_admin_test_credit_state(context) -> None:
        for key in ("awaiting_admin_credit_target", "awaiting_admin_credit_amount",
                    "pending_admin_test_credit"):
            context.user_data.pop(key, None)

    async def send_admin_test_credit_menu(self, message, admin_id: int) -> None:
        own = self.store.get_user_summary(admin_id)
        lines = [
            "🧪 CỘNG TIỀN THỬ NGHIỆM",
            "Số tiền này dùng để thử mua hàng, không tính là tiền khách chuyển khoản.",
            f"Giới hạn mỗi lần: {vnd(MAX_ADMIN_TEST_CREDIT)}.",
            "\nLịch sử cộng gần đây:",
        ]
        credits = self.store.list_test_credits(limit=5)
        for credit in credits:
            label = user_label(credit["user_id"], credit["username"],
                               credit["display_name"])
            lines.append(f"#{credit['id']} +{vnd(credit['amount'])} → {label}")
        if not credits:
            lines.append("Chưa có lần cộng nào.")
        rows = []
        if own:
            rows.append([InlineKeyboardButton(
                "👤 Ví admin của tôi", callback_data="admin:balancecredit:me",
            )])
        rows.extend([
            [InlineKeyboardButton("🔎 Tìm khách bằng @username / ID",
                                  callback_data="admin:balancecredit:lookup")],
            [InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")],
        ])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_test_credit_amounts(self, message, user_id: int) -> None:
        user = self.store.get_user_summary(user_id)
        if not user:
            await message.reply_text("Không tìm thấy người dùng đã dùng bot.")
            return
        rows = [[InlineKeyboardButton(
            f"+{vnd(amount)}", callback_data=f"admin:balancecredit:amount:{user_id}:{amount}",
        )] for amount in (50_000, 100_000, 500_000, 1_000_000)]
        rows.extend([
            [InlineKeyboardButton("✏️ Số khác", callback_data=f"admin:balancecredit:other:{user_id}")],
            [InlineKeyboardButton("⬅️ Tiền thử nghiệm", callback_data="admin:balancecredit:home")],
        ])
        await message.reply_text(
            f"🧪 CỘNG TIỀN CHO {user_label(user_id, user['username'], user['display_name'])}\n"
            f"Số dư hiện tại: {vnd(user['balance'])}\n"
            "Chọn số tiền thử nghiệm cần cộng. Bot sẽ hỏi xác nhận trước khi cộng.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def lookup_admin_test_credit_target(self, message, context, raw: str) -> None:
        target = raw.strip()
        try:
            if re.fullmatch(r"[1-9][0-9]{0,19}", target):
                matches = [self.store.get_user_summary(int(target))]
                matches = [item for item in matches if item]
            else:
                matches = self.store.find_users_by_username(target)
        except ValueError:
            matches = []
        if len(matches) != 1:
            reason = ("Có nhiều tài khoản trùng username đã lưu; hãy gửi Telegram ID."
                      if len(matches) > 1 else
                      "Không tìm thấy tài khoản này trong bot. Gửi đúng @username hoặc Telegram ID.")
            await message.reply_text(reason + " Gửi /cancel để hủy.")
            return
        context.user_data.pop("awaiting_admin_credit_target", None)
        await self.send_admin_test_credit_amounts(message, matches[0]["telegram_id"])

    async def receive_admin_test_credit_amount(self, message, context, raw: str) -> None:
        try:
            amount = parse_amount(raw)
        except ValueError:
            amount = 0
        if not 1 <= amount <= MAX_ADMIN_TEST_CREDIT:
            await message.reply_text(
                f"Nhập số tiền nguyên từ 1đ đến {vnd(MAX_ADMIN_TEST_CREDIT)}; "
                "ví dụ 500000 hoặc 500k. Gửi /cancel để hủy."
            )
            return
        user_id = context.user_data.pop("awaiting_admin_credit_amount")
        await self.prepare_admin_test_credit(message, context, user_id, amount)

    async def prepare_admin_test_credit(self, message, context, user_id: int,
                                        amount: int) -> None:
        user = self.store.get_user_summary(user_id)
        if not user or not 1 <= amount <= MAX_ADMIN_TEST_CREDIT:
            await message.reply_text("Người dùng hoặc số tiền không hợp lệ.")
            return
        nonce = secrets.token_hex(16)
        context.user_data["pending_admin_test_credit"] = (
            nonce, user_id, amount, user["balance"], time.monotonic() + 300,
        )
        await message.reply_text(
            "🧪 XÁC NHẬN CỘNG TIỀN THỬ NGHIỆM\n"
            f"Tài khoản: {user_label(user_id, user['username'], user['display_name'])}\n"
            f"Cộng: {vnd(amount)}\n"
            f"Số dư: {vnd(user['balance'])} → {vnd(user['balance'] + amount)}\n"
            "Khoản này được ghi riêng, không tính là tiền nạp qua ngân hàng. "
            "Xác nhận có hiệu lực 5 phút.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Xác nhận cộng", callback_data=f"admin:balancecredit:confirm:{nonce}")],
                [InlineKeyboardButton("Hủy", callback_data="admin:balancecredit:cancel")],
            ]),
        )

    async def confirm_admin_test_credit(self, message, context, admin_id: int,
                                        nonce: str) -> None:
        pending = context.user_data.pop("pending_admin_test_credit", None)
        if (not pending or pending[0] != nonce or
                time.monotonic() > pending[4]):
            await message.reply_text("Xác nhận đã hết hiệu lực. Tiền chưa được cộng.")
            return
        _, user_id, amount, balance, _ = pending
        try:
            result = self.store.credit_test_balance(
                user_id, amount, admin_id, balance, nonce,
            )
        except BalanceChanged:
            await message.reply_text(
                "Số dư đã đổi từ lúc xem trước. Tiền chưa được cộng; hãy chọn lại số tiền."
            )
            await self.send_admin_test_credit_amounts(message, user_id)
            return
        except (NotFound, ValueError):
            await message.reply_text("Không cộng được tiền. Kiểm tra lại tài khoản và thử lại.")
            return
        await message.reply_text(
            f"✅ Đã cộng {vnd(amount)} tiền thử nghiệm cho ID {user_id}. "
            f"Số dư mới: {vnd(result['balance_after'])}. Mã ghi nhận #{result['id']}."
        )
        self.audit_admin_action(
            admin_id, "credit_test_balance", "user", user_id,
            f"amount={amount}; credit_id={result['id']}",
        )
        await self.send_admin_user(message, user_id)

    async def admin_test_credit_callback(self, message, parts: list[str], context,
                                         admin_id: int) -> None:
        if parts == ["admin", "balancecredit", "home"]:
            self.clear_admin_test_credit_state(context)
            await self.send_admin_test_credit_menu(message, admin_id)
        elif parts == ["admin", "balancecredit", "lookup"]:
            self.clear_admin_test_credit_state(context)
            context.user_data["awaiting_admin_credit_target"] = True
            await message.reply_text(
                "Gửi @username hoặc Telegram ID của người cần cộng tiền thử nghiệm. "
                "Tài khoản phải từng dùng bot. Gửi /cancel để hủy.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Hủy", callback_data="admin:balancecredit:cancel"),
                ]]),
            )
        elif parts == ["admin", "balancecredit", "me"]:
            self.clear_admin_test_credit_state(context)
            await self.send_admin_test_credit_amounts(message, admin_id)
        elif len(parts) == 4 and parts[2] == "user":
            try:
                user_id = int(parts[3])
            except ValueError:
                return
            self.clear_admin_test_credit_state(context)
            await self.send_admin_test_credit_amounts(message, user_id)
        elif len(parts) == 4 and parts[2] == "other":
            try:
                user_id = int(parts[3])
            except ValueError:
                return
            if not self.store.get_user_summary(user_id):
                await message.reply_text("Không tìm thấy người dùng.")
                return
            self.clear_admin_test_credit_state(context)
            context.user_data["awaiting_admin_credit_amount"] = user_id
            await message.reply_text(
                f"Gửi số tiền cần cộng (1đ–{vnd(MAX_ADMIN_TEST_CREDIT)}), "
                "ví dụ 500000 hoặc 500k. Gửi /cancel để hủy.",
            )
        elif len(parts) == 5 and parts[2] == "amount":
            try:
                user_id, amount = int(parts[3]), int(parts[4])
            except ValueError:
                return
            self.clear_admin_test_credit_state(context)
            await self.prepare_admin_test_credit(message, context, user_id, amount)
        elif len(parts) == 4 and parts[2] == "confirm":
            await self.confirm_admin_test_credit(message, context, admin_id, parts[3])
        elif parts == ["admin", "balancecredit", "cancel"]:
            self.clear_admin_test_credit_state(context)
            await message.reply_text("Đã hủy. Tiền chưa được cộng.")
            await self.send_admin_test_credit_menu(message, admin_id)

    async def begin_admin_sales_search(self, message, context):
        context.user_data.pop("admin_sales_search_query", None)
        context.user_data["awaiting_admin_sales_search"] = True
        await message.reply_text(
            "🔎 TÌM KHÁCH TRONG ĐƠN ĐÃ BÁN\n"
            "Gửi Telegram ID, @username hoặc một phần tên khách. "
            "Gửi /cancel để hủy.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Đơn đã bán", callback_data="admin:sales:0"),
            ]]),
        )

    async def receive_admin_sales_search(self, message, context, raw: str):
        context.user_data["admin_sales_search_query"] = " ".join(raw.strip().split())
        await self.send_admin_sales_search_results(message, context, 0)

    async def send_admin_sales_search_results(self, message, context, page: int = 0):
        query = context.user_data.get("admin_sales_search_query")
        if not query:
            await self.begin_admin_sales_search(message, context)
            return
        try:
            await self.send_admin_sales(message, page, search_query=query)
        except ValueError:
            context.user_data.pop("admin_sales_search_query", None)
            context.user_data["awaiting_admin_sales_search"] = True
            await message.reply_text(
                "Từ khóa không hợp lệ. Nhập Telegram ID, @username hoặc một phần tên "
                "(tối đa 80 ký tự); /cancel để hủy."
            )
            return
        context.user_data.pop("awaiting_admin_sales_search", None)

    async def send_admin_sales(self, message, page: int = 0,
                               search_query: str | None = None):
        total = (self.store.count_purchases_by_buyer(search_query)
                 if search_query is not None else None)
        purchases = self.store.list_all_purchases(
            PAGE_SIZE + 1, page * PAGE_SIZE, buyer_query=search_query,
        )
        visible = purchases[:PAGE_SIZE]
        lines = (["🔎 ĐƠN ĐÃ BÁN THEO KHÁCH", f"Từ khóa: {search_query}",
                  f"Tìm thấy: {total} đơn"] if search_query is not None else
                 ["🧾 ĐƠN ĐÃ BÁN"])
        rows = [[InlineKeyboardButton(
            "🔎 Tìm lại" if search_query is not None else "🔎 Tìm khách mua",
            callback_data="admin:salessearch",
        )]]
        if search_query is None:
            rows.append([InlineKeyboardButton(
                "📥 Xuất file sản phẩm đã bán", callback_data="admin:exportsold",
            )])
        for purchase in visible:
            buyer = user_label(purchase["user_id"], purchase["username"],
                               purchase["display_name"])
            counts = {}
            for item in purchase["items"]:
                counts[item["name"]] = counts.get(item["name"], 0) + 1
            names = [f"{name} ×{count}" if count > 1 else name
                     for name, count in counts.items()]
            preview = ", ".join(names[:2])
            if len(names) > 2:
                preview += f" và {len(names) - 2} loại khác"
            if len(preview) > 90:
                preview = preview[:87] + "..."
            pending_email = sum(bool(item["has_email"] and not item["email_completed_at"])
                                for item in purchase["items"])
            email_state = f" · 📧 {pending_email} email chờ xử lý" if pending_email else ""
            contact_state = " · 💬 Đã nhắn khách" if purchase["admin_contact_count"] else ""
            item_kind = ("sản phẩm" if any(item["delivery_type"] == "file"
                                          for item in purchase["items"]) else "tài khoản")
            lines.append(
                f"#{purchase['id']} · {buyer} · {len(purchase['items'])} {item_kind} "
                f"· {vnd(purchase['price'])}{email_state}{contact_state} "
                f"· {purchase['created_at'][:16]} UTC\n"
                f"   {preview}"
            )
            rows.append([
                InlineKeyboardButton(
                    f"{'📧 ' if pending_email else ''}"
                    f"{'💬 ' if purchase['admin_contact_count'] else ''}"
                    f"Đơn #{purchase['id']}",
                    callback_data=f"admin:purchase:{purchase['id']}",
                ),
                InlineKeyboardButton(
                    f"Người mua {purchase['user_id']}",
                    callback_data=f"admin:user:{purchase['user_id']}",
                ),
            ])
        if not visible:
            lines.append("Không có đơn của khách phù hợp." if search_query is not None
                         else "Chưa có đơn hàng ở trang này.")
        rows.extend(admin_footer(
            "admin:salessearch" if search_query is not None else "admin:sales",
            page, len(purchases) > PAGE_SIZE,
        ))
        if search_query is not None:
            rows.insert(-1, [InlineKeyboardButton(
                "⬅️ Tất cả đơn đã bán", callback_data="admin:sales:0",
            )])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_purchase(self, message, purchase_id: int):
        purchase = self.store.get_admin_purchase(purchase_id)
        if not purchase:
            await message.reply_text("Không tìm thấy đơn hàng.")
            return
        buyer = user_label(purchase["user_id"], purchase["username"],
                           purchase["display_name"])
        item_kind = ("sản phẩm" if any(item["delivery_type"] == "file"
                                      for item in purchase["items"]) else "tài khoản")
        lines = [
            f"🧾 ĐƠN #{purchase_id} · {len(purchase['items'])} {item_kind}",
            f"Khách: {buyer}",
            f"Tổng thanh toán: {vnd(purchase['price'])}",
            f"Thời gian: {purchase['created_at'][:16]} UTC",
            (f"💬 Đã nhắn khách: {purchase['admin_contact_count']} lần · gần nhất "
             f"{purchase['admin_contact_last_sent_at'][:16]} UTC"
             if purchase["admin_contact_count"] else
             "Liên hệ khách: chưa ghi nhận tin nhắn từ shop"),
            "",
        ]
        rows = []
        for index, item in enumerate(purchase["items"], 1):
            email_state = (" · email đã xử lý" if item["email_completed_at"] else
                           " · email chờ xử lý" if item["has_email"] else "")
            lines.append(f"{index}. {item['name']} · "
                         f"{'file' if item['delivery_type'] == 'file' else 'tài khoản'} #{item['id']} "
                         f"· {vnd(item['price'])}{email_state}")
            rows.append([InlineKeyboardButton(
                f"Xem {'file' if item['delivery_type'] == 'file' else 'tài khoản'} {index} (#{item['id']})",
                callback_data=f"admin:order:{item['id']}",
            )])
        rows.append([InlineKeyboardButton(
            "💬 Nhắn khách", callback_data=f"admin:message:{purchase_id}",
        )])
        rows.append([InlineKeyboardButton(
            "⬅️ Đơn đã bán", callback_data="admin:sales:0",
        )])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_order_message(self, update: Update,
                                       context: ContextTypes.DEFAULT_TYPE) -> None:
        admin_id = update.effective_user.id
        state = context.user_data.get("awaiting_admin_order_message")
        if admin_id not in self.config.admin_ids or not state or time.monotonic() > state[1]:
            context.user_data.pop("awaiting_admin_order_message", None)
            await update.message.reply_text("Lượt nhắn đã hết hạn. Mở đơn đã bán và bấm Nhắn khách lại.")
            return
        purchase_id = state[0]
        purchase = self.store.get_admin_purchase(purchase_id)
        if not purchase:
            context.user_data.pop("awaiting_admin_order_message", None)
            await update.message.reply_text("Không tìm thấy đơn hàng đã bán.")
            return
        document = getattr(update.message, "document", None)
        if document is None:
            content = (update.message.text or "").strip()
            if not content or telegram_text_units(content) > ORDER_CHAT_MAX_UNITS:
                await update.message.reply_text(
                    "Tin nhắn cần có nội dung và tối đa 2000 ký tự. Gửi lại hoặc /cancel."
                )
                return
        else:
            content = (getattr(update.message, "caption", None) or "").strip()
            if telegram_text_units(content) > ORDER_CHAT_FILE_CAPTION_MAX_UNITS:
                await update.message.reply_text(
                    "Chú thích file tối đa 900 ký tự. Hãy gửi lại file hoặc /cancel."
                )
                return
        buyer_language = self.store.get_user_language(purchase["user_id"])
        if document is None:
            text = tr(buyer_language,
                f"💬 TIN NHẮN TỪ SHOP · ĐƠN #{purchase_id}\n\n{content}",
                f"💬 MESSAGE FROM SHOP · ORDER #{purchase_id}\n\n{content}")
        else:
            text = tr(buyer_language,
                f"📎 FILE TỪ SHOP · ĐƠN #{purchase_id}",
                f"📎 FILE FROM SHOP · ORDER #{purchase_id}")
            if content:
                text += f"\n\n{content}"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                tr(buyer_language, "↩️ Trả lời chủ shop", "↩️ Reply to shop"),
                callback_data=f"buyerreply:{purchase_id}:{admin_id}",
            ),
        ]])
        try:
            if document is None:
                await context.bot.send_message(purchase["user_id"], text,
                                               reply_markup=markup)
            else:
                await context.bot.send_document(purchase["user_id"],
                                                document=document.file_id,
                                                caption=text, reply_markup=markup)
        except TelegramError as exc:
            log.warning("Could not send order message for purchase %s: %s",
                        purchase_id, type(exc).__name__)
            await update.message.reply_text(
                "Chưa gửi được tin hoặc file cho khách (có thể khách đã chặn bot). "
                "Gửi lại để thử hoặc /cancel để hủy."
            )
            return
        context.user_data.pop("awaiting_admin_order_message", None)
        try:
            self.store.record_admin_purchase_contact(purchase_id)
        except (sqlite3.Error, NotFound):
            log.exception("Could not record admin contact for purchase %s", purchase_id)
            await update.message.reply_text(
                f"Đã gửi cho khách của đơn #{purchase_id}, nhưng chưa lưu được dấu đã nhắn. "
                "Đừng gửi lại ngay; hãy kiểm tra đơn trước."
            )
            return
        await update.message.reply_text(f"Đã nhắn cho khách của đơn #{purchase_id}.")

    async def send_customer_order_reply(self, update: Update,
                                        context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        language = self.store.get_user_language(user_id)
        state = context.user_data.get("awaiting_customer_order_reply")
        if not state or time.monotonic() > state[2]:
            context.user_data.pop("awaiting_customer_order_reply", None)
            await update.message.reply_text(tr(language,
                "Lượt trả lời đã hết hạn. Bấm Trả lời chủ shop trong tin nhắn để thử lại.",
                "Your reply session expired. Tap Reply to shop in the message to try again."))
            return
        purchase_id, admin_id = state[:2]
        if admin_id not in self.config.admin_ids or not self.store.get_purchase(user_id, purchase_id):
            context.user_data.pop("awaiting_customer_order_reply", None)
            await update.message.reply_text(tr(language,
                "Không tìm thấy đơn hàng hoặc cuộc trò chuyện này không còn hiệu lực.",
                "The order was not found or this conversation is no longer available."))
            return
        content = update.message.text.strip()
        if not content or telegram_text_units(content) > ORDER_CHAT_MAX_UNITS:
            await update.message.reply_text(tr(language,
                "Tin nhắn cần có nội dung và tối đa 2000 ký tự. Gửi lại hoặc /cancel.",
                "Enter a message up to 2000 characters. Try again or send /cancel."))
            return
        purchase = self.store.get_admin_purchase(purchase_id)
        if not purchase:
            context.user_data.pop("awaiting_customer_order_reply", None)
            await update.message.reply_text(tr(language,
                "Không tìm thấy đơn hàng của bạn.", "Your order was not found."))
            return
        buyer = user_label(user_id, purchase["username"], purchase["display_name"])
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Trả lời khách",
                                 callback_data=f"admin:message:{purchase_id}"),
        ]])
        try:
            await context.bot.send_message(
                admin_id, f"💬 KHÁCH TRẢ LỜI · ĐƠN #{purchase_id}\nKhách: {buyer}\n\n{content}",
                reply_markup=markup,
            )
        except TelegramError as exc:
            log.warning("Could not forward order reply for purchase %s: %s",
                        purchase_id, type(exc).__name__)
            await update.message.reply_text(tr(language,
                "Chưa gửi được tin. Hãy thử lại hoặc /cancel để hủy.",
                "Your message could not be sent. Try again or send /cancel."))
            return
        context.user_data.pop("awaiting_customer_order_reply", None)
        await update.message.reply_text(tr(language,
            f"Đã gửi lời nhắn cho chủ shop về đơn #{purchase_id}.",
            f"Your reply about order #{purchase_id} was sent to the shop."))

    async def send_admin_pending_emails(self, message, page: int = 0):
        pending = self.store.list_pending_email_orders(PAGE_SIZE + 1, page * PAGE_SIZE)
        visible = pending[:PAGE_SIZE]
        count = self.store.count_pending_email_orders()
        minutes = self.store.get_email_reminder_minutes()
        lines = [f"📧 ĐƠN EMAIL CHỜ XỬ LÝ · {count} đơn"]
        if minutes:
            lines.append(f"Nhắc admin sau {minutes} phút chờ.")
        else:
            lines.append("Đang tắt lời nhắc tự động.")
        rows = []
        now = datetime.now(timezone.utc)
        for order in visible:
            buyer = user_label(order["user_id"], order["username"], order["display_name"])
            created = parse_stored_time(order["created_at"])
            age = max(0, int((now - created).total_seconds() // 60))
            lines.append(
                f"#{order['id']} {order['name']} · {buyer}\n"
                f"   chờ {age} phút · từ {status_time(created)}"
            )
            rows.append([InlineKeyboardButton(
                f"Xử lý đơn #{order['id']}", callback_data=f"admin:order:{order['id']}",
            )])
        if not visible:
            lines.append("Không có đơn email nào đang chờ ở trang này.")
        rows.append([InlineKeyboardButton(
            "⏰ Cài thời gian nhắc", callback_data="admin:emailremind",
        )])
        rows.extend(admin_footer("admin:pendingemails", page, len(pending) > PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_email_reminder_settings(self, message):
        minutes = self.store.get_email_reminder_minutes()
        pending = self.store.count_pending_email_orders()
        state = (f"Nhắc khi đơn chờ quá {minutes} phút" if minutes
                 else "Đang tắt lời nhắc tự động")
        rows = [[InlineKeyboardButton(
            f"{value} phút" if value else "Tắt nhắc",
            callback_data=f"admin:emailremind:{value}",
        ) for value in values] for values in ((15, 30), (60, 120), (0,))]
        rows.append([InlineKeyboardButton("📧 Xem đơn đang chờ",
                                          callback_data="admin:pendingemails:0")])
        rows.append([InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")])
        await message.reply_text(
            f"⏰ NHẮC ĐƠN EMAIL\n{state}.\nHiện có {pending} đơn email chờ xử lý.\n"
            "Chọn mức có sẵn hoặc gõ /emailremind SỐ_PHÚT (5–1440). "
            "Gõ /emailremind 0 để tắt. Khi còn đơn quá hạn, bot nhắc lại "
            "ít nhất mỗi 60 phút; đơn mới quá hạn có thể được báo sớm hơn.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def change_email_reminder(self, message, minutes: int):
        try:
            self.store.set_email_reminder_minutes(minutes)
        except ValueError:
            await message.reply_text("Chọn 0 để tắt, hoặc số phút từ 5 đến 1440.")
            return
        await self.send_email_reminder_settings(message)

    async def send_admin_order_contact(self, message, order_id: int):
        order = self.store.get_admin_order_contact(order_id)
        if not order:
            await message.reply_text("Không tìm thấy đơn hàng.")
            return
        buyer = user_label(order["user_id"], order["username"], order["display_name"])
        bundle = f" · gói #{order['bundle_order_id']}" if order["bundle_order_id"] else ""
        if order["customer_email"]:
            email_status = (
                f"Đã xử lý lúc {status_time(parse_stored_time(order['email_completed_at']))} "
                f"bởi admin {order['email_completed_by']}"
                if order["email_completed_at"] else "Chờ admin xử lý"
            )
            notice_status = {
                "pending": "đang chờ gửi",
                "sent": "đã gửi",
                "failed": "không gửi được (khách có thể đã chặn bot)",
            }.get(order["email_notification_status"], "chưa có")
        else:
            email_status = "Không áp dụng"
            notice_status = "không áp dụng"
        rows = []
        if order["customer_email"] and not order["email_completed_at"]:
            rows.append([InlineKeyboardButton(
                "✅ Đã xử lý xong · Báo khách", callback_data=f"admin:emaildone:{order_id}",
            )])
        rows.append([InlineKeyboardButton(
            "🔐 Xem tài khoản đã giao",
            callback_data=f"admin:solditem:{order['stock_id']}:0",
        )])
        rows.append([
            InlineKeyboardButton("⬅️ Đơn đã bán", callback_data="admin:sales:0"),
            InlineKeyboardButton("👤 Xem khách", callback_data=f"admin:user:{order['user_id']}"),
        ])
        if order["customer_email"] and not order["email_completed_at"]:
            rows.append([InlineKeyboardButton("📧 Danh sách chờ",
                                              callback_data="admin:pendingemails:0")])
        await message.reply_text(
            f"🧾 ĐƠN #{order_id}{bundle}\n"
            f"Khách: {buyer}\n"
            f"Sản phẩm: {order['name']}\n"
            f"Thanh toán: {vnd(order['price'])}\n"
            f"Email xử lý đơn: {order['customer_email'] or '(không yêu cầu / đơn cũ)'}\n"
            f"Xử lý email: {email_status}\n"
            f"Thông báo khách: {notice_status}",
            reply_markup=InlineKeyboardMarkup(rows),
            protect_content=True,
        )

    async def mark_email_order_done(self, message, order_id: int, admin_id: int):
        try:
            result = self.store.complete_email_order(order_id, admin_id)
        except (NotFound, ValueError):
            await message.reply_text("Không tìm thấy đơn yêu cầu email này.")
            return
        if result["created"]:
            self.audit_admin_action(admin_id, "complete_email_order", "order", order_id)
            await message.reply_text(
                f"Đã đánh dấu đơn #{order_id} xử lý xong. Bot sẽ thông báo cho khách trong Telegram."
            )
        else:
            await message.reply_text(
                f"Đơn #{order_id} đã được đánh dấu xử lý trước đó; không gửi thêm thông báo."
            )
        await self.send_admin_order_contact(message, order_id)

    async def send_admin_topups(self, message, page: int = 0):
        topups = self.store.list_all_topups(PAGE_SIZE + 1, page * PAGE_SIZE)
        visible = topups[:PAGE_SIZE]
        lines = ["💰 YÊU CẦU NẠP"]
        rows = []
        for topup in visible:
            status = "đã nhận" if topup["paid_amount"] else "chờ tiền"
            owner = user_label(topup["user_id"], topup["username"], topup["display_name"])
            lines.append(
                f"#{topup['id']} · {owner} · {topup['code']}\n"
                f"Gợi ý {vnd(topup['requested_amount'])} · {status} {vnd(topup['paid_amount'])} "
                f"· {topup['created_at'][:16]} UTC"
            )
            rows.append([InlineKeyboardButton(
                f"Người nạp {topup['user_id']}", callback_data=f"admin:user:{topup['user_id']}"
            )])
        if not visible:
            lines.append("Chưa có yêu cầu nạp ở trang này.")
        rows.extend(admin_footer("admin:topups", page, len(topups) > PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_transactions(self, message, page: int = 0):
        events = self.store.list_bank_events(PAGE_SIZE + 1, page * PAGE_SIZE)
        visible = events[:PAGE_SIZE]
        labels = {"credited": "đã cộng", "unmatched": "chưa khớp", "ignored": "đã bỏ qua"}
        lines = ["🏦 GIAO DỊCH NGÂN HÀNG"]
        for event in visible:
            content = " ".join(event["content"].split())[:80]
            owner = (
                f" · {user_label(event['user_id'], event['username'], event['display_name'])}"
                if event["user_id"] else ""
            )
            lines.append(
                f"#{event['id']} {event['provider_txn_id'][:32]} · {vnd(event['amount'])}"
                f" · {labels.get(event['status'], event['status'])}{owner}"
                f" · {event['created_at'][:16]} UTC\n{content}"
            )
        if not visible:
            lines.append("Chưa có giao dịch ở trang này.")
        rows = admin_footer("admin:transactions", page, len(events) > PAGE_SIZE)
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_warranties(self, message, page: int = 0):
        await self._send_admin_warranties(message, page, all_items=False)

    async def send_admin_all_warranties(self, message, page: int = 0):
        await self._send_admin_warranties(message, page, all_items=True)

    async def _send_admin_warranties(self, message, page: int, all_items: bool):
        tickets = self.store.list_warranties(
            None if all_items else "open", PAGE_SIZE + 1, page * PAGE_SIZE,
        )
        visible = tickets[:PAGE_SIZE]
        count = self.store.count_open_warranties()
        minutes = self.store.get_warranty_reminder_minutes()
        lines = (["🛡 TẤT CẢ YÊU CẦU BẢO HÀNH"] if all_items else
                 [f"🛡 BẢO HÀNH ĐANG MỞ · {count} yêu cầu",
                  f"Nhắc admin sau {minutes} phút chờ." if minutes
                  else "Đang tắt lời nhắc tự động."])
        rows = []
        now = datetime.now(timezone.utc)
        for ticket in visible:
            buyer = user_label(ticket["user_id"], ticket["username"], ticket["display_name"])
            state = ("đang mở" if ticket["status"] == "open" else
                     "đã xử lý riêng" if ticket["resolved_privately"] else "đã phản hồi")
            age = max(0, int((now - parse_stored_time(ticket["created_at"])).total_seconds() // 60))
            purchase_id = ticket["purchase_id"] or ticket["order_id"]
            purchase = self.store.get_admin_purchase(purchase_id)
            count = len(purchase["items"]) if purchase else 1
            lines.append(
                f"#{ticket['id']} · đơn #{purchase_id} · {count} tài khoản\n"
                f"{buyer} · {state}" + (f" · chờ {age} phút" if state == "đang mở" else "")
            )
            rows.append([InlineKeyboardButton(
                f"Xem yêu cầu #{ticket['id']}", callback_data=f"admin:warranty:{ticket['id']}"
            )])
        if not visible:
            lines.append("Chưa có yêu cầu ở trang này.")
        prefix = "admin:warrantiesall" if all_items else "admin:warranties"
        rows.append([InlineKeyboardButton(
            "Chỉ xem đang mở" if all_items else "Xem tất cả",
            callback_data="admin:warranties:0" if all_items else "admin:warrantiesall:0",
        )])
        rows.append([InlineKeyboardButton(
            "⏰ Cài thời gian nhắc bảo hành", callback_data="admin:warrantyremind",
        )])
        rows.extend(admin_footer(prefix, page, len(tickets) > PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_warranty_reminder_settings(self, message):
        minutes = self.store.get_warranty_reminder_minutes()
        pending = self.store.count_open_warranties()
        state = (f"Nhắc khi yêu cầu chờ quá {minutes} phút" if minutes
                 else "Đang tắt lời nhắc tự động")
        rows = [[InlineKeyboardButton(
            f"{value} phút" if value else "Tắt nhắc",
            callback_data=f"admin:warrantyremind:{value}",
        ) for value in values] for values in ((15, 30), (60, 120), (0,))]
        rows.append([InlineKeyboardButton("🛡 Xem yêu cầu đang mở",
                                          callback_data="admin:warranties:0")])
        rows.append([InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")])
        await message.reply_text(
            f"⏰ NHẮC BẢO HÀNH\n{state}.\nHiện có {pending} yêu cầu bảo hành đang mở.\n"
            "Chọn mức có sẵn hoặc gõ /warrantyremind SỐ_PHÚT (5–1440). "
            "Gõ /warrantyremind 0 để tắt. Khi còn yêu cầu quá hạn, bot nhắc lại "
            "ít nhất mỗi 60 phút; yêu cầu mới quá hạn có thể được báo sớm hơn.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def change_warranty_reminder(self, message, minutes: int):
        try:
            self.store.set_warranty_reminder_minutes(minutes)
        except ValueError:
            await message.reply_text("Chọn 0 để tắt, hoặc số phút từ 5 đến 1440.")
            return
        await self.send_warranty_reminder_settings(message)

    async def send_admin_warranty(self, message, ticket_id: int):
        ticket = self.store.get_warranty(ticket_id)
        if not ticket:
            await message.reply_text("Không tìm thấy yêu cầu bảo hành.")
            return
        buyer = user_label(ticket["user_id"], ticket["username"], ticket["display_name"])
        status = ("đang mở" if ticket["status"] == "open" else
                  "đã xử lý riêng" if ticket["resolved_privately"] else "đã phản hồi")
        purchase_id = ticket["purchase_id"] or ticket["order_id"]
        purchase = self.store.get_admin_purchase(purchase_id)
        products = (", ".join(f"{item['name']}" for item in purchase["items"])
                    if purchase else ticket["product_name"])
        if len(products) > 300:
            products = products[:297] + "..."
        if ticket["resolved_privately"]:
            reply = ("\n\nĐã tự xử lý riêng với khách; bot không gửi thông báo. "
                     f"Admin ID {ticket['privately_resolved_by']} đánh dấu lúc "
                     f"{ticket['resolved_at'][:16]} UTC.")
        elif ticket["reply"]:
            reply = f"\n\nPhản hồi: {ticket['reply']}"
        else:
            reply = f"\n\nTrả lời: /resolvewarranty {ticket_id} | Nội dung phản hồi"
        rows = []
        if ticket["status"] == "open":
            rows.append([InlineKeyboardButton(
                "✅ Đã xử lý riêng với khách",
                callback_data=f"admin:warrantyprivate:{ticket_id}",
            )])
        rows.append([InlineKeyboardButton(
            "⬅️ Yêu cầu đang mở", callback_data="admin:warranties:0",
        )])
        await message.reply_text(
            f"🛡 BẢO HÀNH #{ticket_id} · {status}\n"
            f"Đơn #{purchase_id} · {len(purchase['items']) if purchase else 1} tài khoản\n"
            f"Sản phẩm: {products}\n"
            f"Khách: {buyer}\n\n"
            f"Nội dung: {ticket['note']}{reply}",
            reply_markup=InlineKeyboardMarkup(rows),
            protect_content=True,
        )

    async def confirm_private_warranty(self, message, ticket_id: int):
        ticket = self.store.get_warranty(ticket_id)
        if not ticket:
            await message.reply_text("Không tìm thấy yêu cầu bảo hành.")
            return
        if ticket["status"] != "open":
            await message.reply_text("Yêu cầu bảo hành này đã được xử lý.")
            await self.send_admin_warranty(message, ticket_id)
            return
        await message.reply_text(
            f"Xác nhận bảo hành #{ticket_id} đã được bạn xử lý riêng với khách?\n"
            "Bot sẽ đóng yêu cầu, dừng nhắc admin và không gửi tin cho khách.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Xác nhận đã xử lý riêng",
                                      callback_data=f"admin:warrantyprivateconfirm:{ticket_id}")],
                [InlineKeyboardButton("⬅️ Quay lại yêu cầu",
                                      callback_data=f"admin:warranty:{ticket_id}")],
            ]),
        )

    async def mark_private_warranty_done(self, message, ticket_id: int, admin_id: int):
        try:
            self.store.resolve_warranty_privately(ticket_id, admin_id)
        except ValueError:
            await message.reply_text("ID yêu cầu bảo hành không hợp lệ.")
            return
        except NotFound:
            await message.reply_text("Không tìm thấy yêu cầu bảo hành.")
            return
        except WarrantyClosed:
            await message.reply_text("Yêu cầu bảo hành này đã được xử lý.")
            await self.send_admin_warranty(message, ticket_id)
            return
        await message.reply_text(
            f"Đã đánh dấu bảo hành #{ticket_id} là xử lý riêng. Bot không gửi tin cho khách."
        )
        self.audit_admin_action(admin_id, "resolve_warranty_private", "warranty", ticket_id)
        await self.send_admin_warranty(message, ticket_id)

    async def send_admin_report(self, message, day: date | None = None):
        if day is not None:
            report = self.store.daily_report(day)
            lines = [
                f"📊 BÁO CÁO {report['date']} (GMT+7)",
                f"Tiền nạp thực nhận: {vnd(report['topups'])}",
                f"Doanh thu: {vnd(report['revenue'])}",
                f"Số đơn: {report['orders']}",
            ]
        else:
            today = datetime.now(SHOP_TIMEZONE).date()
            reports = [self.store.daily_report(today - timedelta(days=offset)) for offset in range(6, -1, -1)]
            lines = ["📊 BÁO CÁO 7 NGÀY (GMT+7)"]
            for report in reports:
                lines.append(
                    f"{report['date']}: nạp {vnd(report['topups'])} · "
                    f"bán {vnd(report['revenue'])} · {report['orders']} đơn"
                )
            lines.extend([
                "\nTổng 7 ngày:",
                f"Tiền nạp thực nhận: {vnd(sum(report['topups'] for report in reports))}",
                f"Doanh thu: {vnd(sum(report['revenue'] for report in reports))}",
                f"Số đơn: {sum(report['orders'] for report in reports)}",
            ])
        await message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")
            ]]),
        )

    async def send_admin_funnel(self, message, days: int = 7):
        report = self.store.funnel_report(days, self.config.admin_ids)

        def share(part: int, whole: int) -> str:
            return f"{part * 100 / whole:.1f}".replace(".", ",") + "%" if whole else "0%"

        visitors = report["visitors"]
        viewers = report["viewers"]
        lines = [
            f"📈 HÀNH TRÌNH KHÁCH · {days} NGÀY",
            f"Từ {status_time(report['start_at'])} đến {status_time(report['end_at'])} (GMT+7)",
            f"Khách tương tác: {visitors} · khách mới: {report['new_visitors']}",
            f"Xem sản phẩm/gói: {viewers} ({share(viewers, visitors)} khách tương tác)",
            f"Tạo mã nạp: {report['topup_requesters']} ({share(report['topup_requesters'], visitors)})",
            f"Nạp thành công: {report['payers']} ({share(report['payers'], visitors)})",
            f"Mua hàng: {report['buyers']} ({share(report['buyers'], visitors)})",
            "",
            f"Xem → tạo mã nạp: {report['viewer_topup_requesters']}/{viewers} "
            f"({share(report['viewer_topup_requesters'], viewers)})",
            f"Xem → mua: {report['viewer_buyers']}/{viewers} "
            f"({share(report['viewer_buyers'], viewers)})",
            f"Tương tác → mua: {report['buyers']}/{visitors} "
            f"({share(report['buyers'], visitors)})",
            "",
            "Mỗi khách được tính một lần ở mỗi mục. Lượt vào/xem chỉ được ghi từ khi bật tính năng.",
        ]
        await message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("7 ngày", callback_data="admin:funnel:7"),
                 InlineKeyboardButton("30 ngày", callback_data="admin:funnel:30")],
                [InlineKeyboardButton("⬅️ Quản trị", callback_data="admin:home")],
            ]),
        )

    async def send_admin_product_views(self, message, days: int = 7, page: int = 0):
        report = self.store.product_view_report(
            days, self.config.admin_ids, PRODUCT_VIEW_PAGE_SIZE,
            page * PRODUCT_VIEW_PAGE_SIZE,
        )
        lines = [
            f"👀 SẢN PHẨM KHÁCH XEM · {days} NGÀY",
            f"Từ {status_time(report['start_at'])} đến {status_time(report['end_at'])} (GMT+7)",
            "Chỉ tính các lần khách mở trang chi tiết từ khi bật thống kê.",
        ]
        rows = []
        for product in report["products"]:
            archived = " · đã lưu trữ" if product["deleted"] else ""
            lines.append(
                f"#{product['display_id']} {product['name']} · "
                f"{product['view_count']} lượt mở · {product['viewer_count']} khách{archived}"
            )
            rows.append([InlineKeyboardButton(
                f"Xem khách · #{product['display_id']} {product['name'][:28]}",
                callback_data=f"admin:viewproduct:{product['id']}:{days}:0",
            )])
        if not report["products"]:
            lines.append("Chưa có khách xem sản phẩm trong khoảng này.")
        rows.append([
            InlineKeyboardButton("7 ngày", callback_data="admin:views:7:0"),
            InlineKeyboardButton("30 ngày", callback_data="admin:views:30:0"),
        ])
        rows.extend(admin_footer(f"admin:views:{days}", page, report["has_more"]))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_product_viewers(self, message, product_id: int,
                                         days: int = 7, page: int = 0):
        product = self.store.get_product(product_id)
        if not product:
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        report = self.store.product_view_customers(
            product_id, days, self.config.admin_ids, PAGE_SIZE, page * PAGE_SIZE,
        )
        lines = [
            f"👀 KHÁCH XEM #{product['display_id']} {product['name']} · {days} NGÀY",
            "Xếp theo số lần mở trang chi tiết.",
        ]
        rows = []
        for customer in report["customers"]:
            name = user_label(customer["user_id"], customer["username"],
                              customer["display_name"])
            lines.append(
                f"{name} · {customer['view_count']} lượt · "
                f"gần nhất {status_time(parse_stored_time(customer['last_view_at']))}"
            )
            rows.append([InlineKeyboardButton(
                f"👤 Xem khách ID {customer['user_id']}",
                callback_data=f"admin:user:{customer['user_id']}",
            )])
        if not report["customers"]:
            lines.append("Chưa có khách nào mở sản phẩm trong khoảng này.")
        rows.extend(admin_footer(
            f"admin:viewproduct:{product_id}:{days}", page, report["has_more"],
        ))
        rows.insert(-1, [InlineKeyboardButton(
            "⬅️ Thống kê sản phẩm", callback_data=f"admin:views:{days}:0",
        )])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                                 protect_content=True)

    @staticmethod
    def _enter_admin_export(context):
        for key in ("awaiting_topup", "awaiting_warranty", "awaiting_bulk_stock",
                    "awaiting_coupon", "awaiting_order_email", "awaiting_manual_credit",
                    "pending_manual_credit"):
            context.user_data.pop(key, None)
        context.user_data["awaiting_export_range"] = True

    async def begin_admin_export(self, message, context):
        self._enter_admin_export(context)
        today = datetime.now(SHOP_TIMEZONE).date()
        await message.reply_text(
            f"Gửi ngày bắt đầu và kết thúc: YYYY-MM-DD YYYY-MM-DD. "
            f"Ví dụ: {today.replace(day=1)} {today}. Tối đa {MAX_EXPORT_DAYS} ngày. "
            "Gửi /cancel để hủy."
        )

    async def send_admin_export(self, message, context, value: str):
        try:
            start, end = parse_date_range(value)
        except ValueError as exc:
            await message.reply_text(f"{exc} Gửi lại khoảng ngày hoặc /cancel.")
            return
        context.user_data.pop("awaiting_export_range", None)
        try:
            report = await asyncio.to_thread(build_export_archive, self.store, start, end)
        except ExportTooLarge:
            await message.reply_text("Báo cáo quá lớn. Hãy xuất khoảng ngày ngắn hơn.")
            return
        except Exception:
            log.exception("Could not build admin report archive")
            await message.reply_text("Không xuất được báo cáo. Kiểm tra log bot rồi thử lại.")
            return
        await message.reply_document(
            document=InputFile(BytesIO(report["content"]), filename=report["filename"]),
            caption=(
                f"Báo cáo {start} đến {end} (GMT+7)\n"
                f"Nạp thực nhận: {vnd(report['topup_total'])} ({report['topups']} giao dịch)\n"
                f"Doanh thu: {vnd(report['revenue'])} ({report['orders']} đơn)\n"
                f"Xử lý tay: {report['manual_credits']} giao dịch, đã tính trong tiền nạp."
            ),
        )

    async def send_admin_users_export(self, message):
        try:
            export = await asyncio.to_thread(build_users_csv, self.store,
                                             self.config.admin_ids)
        except ExportTooLarge:
            await message.reply_text("Danh sách người dùng quá lớn để xuất một file CSV.")
            return
        except Exception:
            log.exception("Could not export customers")
            await message.reply_text("Không xuất được file người dùng. Kiểm tra log bot rồi thử lại.")
            return
        await message.reply_document(
            document=InputFile(BytesIO(export["content"]), filename=export["filename"]),
            caption=f"Danh sách {export['users']} khách từng truy cập bot (GMT+7).",
        )

    async def send_admin_sold_products_export(self, message):
        try:
            export = await asyncio.to_thread(build_sold_products_csv, self.store)
        except ExportTooLarge:
            await message.reply_text("Dữ liệu bán hàng quá lớn để xuất một file CSV.")
            return
        except Exception:
            log.exception("Could not export sold products")
            await message.reply_text("Không xuất được file sản phẩm đã bán. Kiểm tra log bot rồi thử lại.")
            return
        await message.reply_document(
            document=InputFile(BytesIO(export["content"]), filename=export["filename"]),
            caption=(f"Đã bán: {export['orders']} đơn · {export['products']} dòng sản phẩm "
                     f"· {export['units']} sản phẩm. Không gồm thông tin tài khoản."),
        )

    async def send_admin_bundles(self, message, page: int = 0):
        bundles = list(reversed(self.store.list_bundles(
            include_inactive=True, include_deleted=True,
        )))
        visible = bundles[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = ["🎁 GÓI SẢN PHẨM", "Chọn gói để quản lý hoặc tạo gói mới."]
        rows = [[InlineKeyboardButton("➕ Tạo gói", callback_data="admin:offer:bundle:new")]]
        for bundle in visible:
            status = "đã lưu trữ" if bundle["deleted"] else "đang bán" if bundle["active"] else "đã ẩn"
            bundle_price = (
                f"{vnd(bundle['previous_price'])} → {vnd(bundle['price'])}"
                if bundle["previous_price"] else vnd(bundle["price"])
            )
            lines.append(
                f"#{bundle['id']} {bundle['name']} · {bundle_price} "
                f"· còn {bundle['available']} · {status}"
            )
            rows.append([InlineKeyboardButton(
                f"Xem gói #{bundle['id']}", callback_data=f"admin:bundle:{bundle['id']}",
            )])
        if not visible:
            lines.append("Chưa có gói ở trang này.")
        rows.extend(admin_footer("admin:bundles", page, len(bundles) > (page + 1) * PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_bundle(self, message, bundle_id: int):
        bundle = self.store.get_bundle(bundle_id)
        if not bundle:
            await message.reply_text("Không tìm thấy gói.")
            return
        components = "\n".join(
            f"#{item['display_id']} {item['name']} × {item['quantity']} "
            f"(kho {item['stock_available']})" for item in bundle["items"]
        )
        status = "đã lưu trữ" if bundle["deleted"] else "đang bán" if bundle["active"] else "đã ẩn"
        rows = []
        if bundle["deleted"]:
            rows.append([InlineKeyboardButton(
                "♻️ Khôi phục gói", callback_data=f"admin:offer:bundle:restore:{bundle_id}",
            )])
        else:
            rows.extend([
                [InlineKeyboardButton("✏️ Sửa tên", callback_data=f"admin:offer:bundle:edit:{bundle_id}:name"),
                 InlineKeyboardButton("📝 Sửa mô tả", callback_data=f"admin:offer:bundle:edit:{bundle_id}:description")],
                [InlineKeyboardButton("💰 Sửa giá", callback_data=f"admin:offer:bundle:edit:{bundle_id}:price")],
                [InlineKeyboardButton("👁 Hiện bán" if not bundle["active"] else "🙈 Ẩn gói",
                                      callback_data=f"admin:offer:bundle:toggle:{bundle_id}"),
                 InlineKeyboardButton("🗑 Xóa khỏi shop", callback_data=f"admin:offer:bundle:archive:{bundle_id}")],
            ])
        rows.append([InlineKeyboardButton("⬅️ Các gói", callback_data="admin:bundles:0")])
        await message.reply_text(
            f"🎁 GÓI #{bundle_id}: {bundle['name']}\n"
            f"{bundle['description'] or '(không có mô tả)'}\n\n{components}\n\n"
            f"Giá lẻ hiện tại: {vnd(bundle['regular_price'])}\n"
            f"Giá gói: "
            + (f"{vnd(bundle['previous_price'])} → {vnd(bundle['price'])}"
               if bundle["previous_price"] else vnd(bundle["price"]))
            + f" · còn {bundle['available']} · {status}\n\n"
            "Chọn nút bên dưới để quản lý. Thành phần gói được giữ nguyên khi sửa.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def send_admin_coupons(self, message, page: int = 0):
        coupons = self.store.list_coupons()
        visible = coupons[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = ["🏷 MÃ GIẢM GIÁ", "Chọn mã để sửa hoặc bật/tắt."]
        rows = [[InlineKeyboardButton("➕ Tạo mã giảm giá", callback_data="admin:offer:coupon:new")]]
        for coupon in visible:
            value = f"{coupon['value']}%" if coupon["kind"] == "percent" else vnd(coupon["value"])
            status = "đang bật" if coupon["active"] else "đã tắt"
            scope = (f"SP #{coupon['product_display_id']} {coupon['product_name']}"
                     if coupon["product_id"] else "toàn shop")
            lines.append(
                f"{coupon['code']} · giảm {value} · {coupon['used']}/{coupon['max_uses']} lượt "
                f"· {scope} · hết hạn {coupon['expires_on'] or 'không đặt'} · {status}"
            )
            rows.append([InlineKeyboardButton(
                f"Xem {coupon['code']}", callback_data=f"admin:offer:coupon:view:{coupon['code']}",
            )])
        if not visible:
            lines.append("Chưa có mã nào.")
        rows.extend(admin_footer("admin:coupons", page, len(coupons) > (page + 1) * PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_coupon(self, message, code: str):
        coupon = next((item for item in self.store.list_coupons()
                       if item["code"] == code.upper()), None)
        if not coupon:
            await message.reply_text("Không tìm thấy mã giảm giá.")
            return
        value = f"{coupon['value']}%" if coupon["kind"] == "percent" else vnd(coupon["value"])
        scope = (f"Sản phẩm #{coupon['product_display_id']} {coupon['product_name']}"
                 if coupon["product_id"] else "Toàn shop")
        await message.reply_text(
            f"🏷 MÃ {coupon['code']}\nGiảm: {value}\nPhạm vi: {scope}\n"
            f"Đã dùng: {coupon['used']}/{coupon['max_uses']} lượt\n"
            f"Hết hạn: {coupon['expires_on'] or 'không đặt'}\n"
            f"Trạng thái: {'đang bật' if coupon['active'] else 'đã tắt'}\n\n"
            "Mỗi khách chỉ dùng mã một lần. Mã và phạm vi không đổi sau khi tạo.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Sửa mức giảm",
                                      callback_data=f"admin:offer:coupon:edit:{code}:discount"),
                 InlineKeyboardButton("🔢 Sửa số lượt",
                                      callback_data=f"admin:offer:coupon:edit:{code}:uses")],
                [InlineKeyboardButton("📅 Sửa ngày hết hạn",
                                      callback_data=f"admin:offer:coupon:edit:{code}:expiry"),
                 InlineKeyboardButton("🔔 Bật mã" if not coupon["active"] else "🔕 Tắt mã",
                                      callback_data=f"admin:offer:coupon:toggle:{code}")],
                [InlineKeyboardButton("⬅️ Các mã", callback_data="admin:coupons:0")],
            ]),
        )

    @staticmethod
    def offer_cancel_markup(token: str):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Hủy", callback_data=f"admin:offer:cancel:{token}"),
        ]])

    async def send_offer_coupon_scope(self, message, flow: dict, page: int = 0):
        products = sorted(self.store.list_products(), key=lambda item: item["display_id"])
        page = max(0, min(page, (len(products) - 1) // PAGE_SIZE if products else 0))
        visible = products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        token = flow["token"]
        rows = [[InlineKeyboardButton("Toàn shop", callback_data=f"admin:offer:c:all:{token}")]]
        rows.extend([[InlineKeyboardButton(
            f"#{item['display_id']} {item['name'][:30]}",
            callback_data=f"admin:offer:c:pick:{item['id']}:{token}",
        )] for item in visible])
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("◀ Trước", callback_data=f"admin:offer:c:page:{page-1}:{token}"))
        if (page + 1) * PAGE_SIZE < len(products):
            navigation.append(InlineKeyboardButton("Sau ▶", callback_data=f"admin:offer:c:page:{page+1}:{token}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton("Hủy", callback_data=f"admin:offer:cancel:{token}")])
        await message.reply_text("Mã áp dụng cho toàn shop hay một sản phẩm?", reply_markup=InlineKeyboardMarkup(rows))

    async def send_offer_coupon_expiry(self, message, flow: dict):
        token = flow["token"]
        await message.reply_text(
            "Chọn thời hạn mã giảm giá:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Không đặt hạn", callback_data=f"admin:offer:c:none:{token}")],
                [InlineKeyboardButton("Nhập ngày hết hạn", callback_data=f"admin:offer:c:date:{token}")],
                [InlineKeyboardButton("Hủy", callback_data=f"admin:offer:cancel:{token}")],
            ]),
        )

    async def send_offer_bundle_picker(self, message, flow: dict, page: int = 0):
        products = sorted(self.store.list_products(), key=lambda item: item["display_id"])
        page = max(0, min(page, (len(products) - 1) // PAGE_SIZE if products else 0))
        flow["picker_page"] = page
        visible = products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        token = flow["token"]
        rows = []
        for item in visible:
            quantity = flow["items"].get(item["id"], 0)
            row = [InlineKeyboardButton(
                f"➕ #{item['display_id']} {item['name'][:20]} ({quantity})",
                callback_data=f"admin:offer:b:add:{item['id']}:{token}",
            )]
            if quantity:
                row.append(InlineKeyboardButton(
                    "➖", callback_data=f"admin:offer:b:remove:{item['id']}:{token}",
                ))
            rows.append(row)
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("◀ Trước", callback_data=f"admin:offer:b:page:{page-1}:{token}"))
        if (page + 1) * PAGE_SIZE < len(products):
            navigation.append(InlineKeyboardButton("Sau ▶", callback_data=f"admin:offer:b:page:{page+1}:{token}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton("✅ Chọn xong", callback_data=f"admin:offer:b:next:{token}")])
        rows.append([InlineKeyboardButton("Hủy", callback_data=f"admin:offer:cancel:{token}")])
        count = sum(flow["items"].values())
        await message.reply_text(
            f"Chọn thành phần cho gói {flow['name']}. Bấm ➕/➖ để đổi số lượng. "
            f"Cần 2–10 tài khoản; đã chọn {count}.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def send_offer_preview(self, message, flow: dict):
        token = flow["token"]
        if flow["kind"] == "coupon_new":
            discount = f"{flow['value']}%" if flow["discount_kind"] == "percent" else vnd(flow["value"])
            product = self.store.get_product(flow["product_id"]) if flow["product_id"] else None
            if flow["product_id"] and not product:
                await message.reply_text("Sản phẩm đã thay đổi. Hãy tạo lại mã giảm giá.")
                return
            scope = f"#{product['display_id']} {product['name']}" if product else "Toàn shop"
            content = (f"Xác nhận tạo mã {flow['code']}\nGiảm: {discount}\n"
                       f"Lượt tối đa: {flow['max_uses']}\nPhạm vi: {scope}\n"
                       f"Hết hạn: {flow['expires_on'] or 'không đặt'}")
            label = "Tạo mã"
        elif flow["kind"] == "bundle_new":
            products = {item["id"]: item for item in self.store.list_products()}
            details = []
            for product_id, quantity in flow["items"].items():
                item = products.get(product_id)
                if not item:
                    await message.reply_text("Thành phần gói đã thay đổi. Chọn lại sản phẩm.")
                    return
                details.append(f"#{item['display_id']} {item['name']} × {quantity}")
            content = (f"Xác nhận tạo gói {flow['name']}\n" + "\n".join(details)
                       + f"\nGiá gói: {vnd(flow['price'])}\n"
                       f"Mô tả: {flow['description'] or '(trống)'}")
            label = "Tạo gói"
        else:
            bundle = self.store.get_bundle(flow["bundle_id"])
            if not bundle:
                await message.reply_text("Không tìm thấy gói.")
                return
            content = (f"Xác nhận xóa gói #{bundle['id']} {bundle['name']} khỏi shop? "
                       "Đơn cũ vẫn được giữ và có thể khôi phục gói sau.")
            label = "Xác nhận xóa"
        await message.reply_text(
            content,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=f"admin:offer:confirm:{token}")],
                [InlineKeyboardButton("Hủy", callback_data=f"admin:offer:cancel:{token}")],
            ]),
        )

    async def admin_offer_text(self, message, context, raw: str):
        flow = context.user_data["admin_offer_flow"]
        kind, step, value = flow["kind"], flow["step"], raw.strip()
        if kind == "coupon_new":
            if step == "code":
                code = value.upper()
                if not COUPON_CODE_PATTERN.fullmatch(code):
                    await message.reply_text("Mã cần 4–24 ký tự A–Z, 0–9, _ hoặc -. Gửi lại mã.")
                    return
                flow["code"], flow["step"] = code, "discount"
                await message.reply_text("Gửi mức giảm, ví dụ 10% hoặc 5000.", reply_markup=self.offer_cancel_markup(flow["token"]))
                return
            if step == "discount":
                try:
                    discount_kind, amount = parse_discount(value)
                except ValueError:
                    await message.reply_text("Mức giảm không hợp lệ. Gửi 1–99% hoặc số tiền VND dương.")
                    return
                flow["discount_kind"], flow["value"], flow["step"] = discount_kind, amount, "uses"
                await message.reply_text("Gửi số lượt dùng tối đa (1–100000).", reply_markup=self.offer_cancel_markup(flow["token"]))
                return
            if step == "uses":
                try:
                    uses = int(value)
                    if not 1 <= uses <= 100_000:
                        raise ValueError
                except ValueError:
                    await message.reply_text("Số lượt phải là số nguyên từ 1 đến 100000.")
                    return
                flow["max_uses"], flow["step"] = uses, "scope"
                await self.send_offer_coupon_scope(message, flow)
                return
            if step == "expiry_input":
                try:
                    expiry = date.fromisoformat(value)
                    if expiry < datetime.now(SHOP_TIMEZONE).date():
                        raise ValueError
                except ValueError:
                    await message.reply_text("Ngày không hợp lệ hoặc đã qua. Gửi YYYY-MM-DD.")
                    return
                flow["expires_on"], flow["step"] = expiry.isoformat(), "confirm"
                await self.send_offer_preview(message, flow)
                return
        if kind == "coupon_edit":
            code = flow["code"]
            try:
                if step == "discount":
                    discount_kind, amount = parse_discount(value)
                    self.store.update_coupon(code, kind=discount_kind, value=amount)
                elif step == "uses":
                    self.store.update_coupon(code, max_uses=int(value))
                elif step == "expiry":
                    expires_on = None if value == "-" else date.fromisoformat(value).isoformat()
                    self.store.update_coupon(code, expires_on=expires_on)
                else:
                    return
            except (ValueError, NotFound):
                await message.reply_text(
                    "Giá trị không hợp lệ. Mức giảm 1–99% hoặc số VND dương; "
                    "số lượt 1–100000 và không thấp hơn số đã dùng; "
                    "ngày dạng YYYY-MM-DD hoặc - để bỏ hạn. Hãy gửi lại."
                )
                return
            context.user_data.pop("admin_offer_flow", None)
            await message.reply_text("Đã cập nhật mã giảm giá.")
            await self.send_admin_coupon(message, code)
            return
        if kind == "bundle_new":
            if step == "name":
                name = " ".join(value.split())
                if not 1 <= len(name) <= 100:
                    await message.reply_text("Tên gói cần 1–100 ký tự. Gửi lại tên.")
                    return
                flow["name"], flow["items"], flow["step"] = name, {}, "items"
                await self.send_offer_bundle_picker(message, flow)
                return
            if step == "price":
                products = {item["id"]: item for item in self.store.list_products()}
                regular = sum(products[pid]["price"] * qty for pid, qty in flow["items"].items()
                              if pid in products)
                try:
                    price = parse_amount(value)
                    if not sum(flow["items"].values()) <= price < regular:
                        raise ValueError
                except ValueError:
                    await message.reply_text(
                        f"Giá gói phải thấp hơn giá lẻ {vnd(regular)} và ít nhất "
                        "1đ cho mỗi tài khoản. Gửi lại giá."
                    )
                    return
                flow["price"], flow["step"] = price, "description"
                await message.reply_text(
                    "Gửi mô tả gói (tối đa 500 ký tự) hoặc bấm Bỏ qua.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Bỏ qua mô tả", callback_data=f"admin:offer:b:skip:{flow['token']}")],
                        [InlineKeyboardButton("Hủy", callback_data=f"admin:offer:cancel:{flow['token']}")],
                    ]),
                )
                return
            if step == "description":
                if len(value) > 500:
                    await message.reply_text("Mô tả tối đa 500 ký tự. Gửi lại mô tả.")
                    return
                flow["description"], flow["step"] = value, "confirm"
                await self.send_offer_preview(message, flow)
                return
        if kind == "bundle_edit":
            bundle_id = flow["bundle_id"]
            try:
                if step == "name":
                    self.store.update_bundle(bundle_id, name=" ".join(value.split()))
                elif step == "price":
                    bundle = self.store.get_bundle(bundle_id)
                    price = parse_amount(value)
                    if not bundle or not sum(item["quantity"] for item in bundle["items"]) <= price < bundle["regular_price"]:
                        raise ValueError("Invalid bundle price")
                    self.store.update_bundle(bundle_id, price=price)
                elif step == "description":
                    self.store.update_bundle(bundle_id, description="" if value == "-" else value)
                else:
                    return
            except (ValueError, NotFound):
                await message.reply_text("Giá trị không hợp lệ. Tên 1–100 ký tự; mô tả tối đa 500 ký tự; giá dương. Gửi lại.")
                return
            context.user_data.pop("admin_offer_flow", None)
            await message.reply_text("Đã cập nhật gói.")
            await self.send_admin_bundle(message, bundle_id)
            return
        if step == "confirm":
            await self.send_offer_preview(message, flow)
        elif step in ("scope", "expiry", "items"):
            await message.reply_text("Hãy bấm nút chọn ở tin nhắn phía trên hoặc bấm Hủy.")

    async def admin_offer_callback(self, message, data: str, context):
        parts = data.split(":")
        action = parts[2] if len(parts) > 2 else ""
        if action in ("coupon", "bundle"):
            context.user_data.pop("admin_offer_flow", None)
        if action == "cancel":
            flow = context.user_data.get("admin_offer_flow")
            if len(parts) != 4 or not flow or flow.get("token") != parts[3]:
                await message.reply_text("Thao tác đã hết hiệu lực. Mở lại mục quản trị.")
                return
            context.user_data.pop("admin_offer_flow", None)
            await message.reply_text("Đã hủy thao tác.")
            await self.send_admin_menu(message)
            return
        if action == "confirm" and len(parts) == 4:
            flow = context.user_data.get("admin_offer_flow")
            if not flow or flow.get("token") != parts[3] or flow["step"] != "confirm":
                await message.reply_text("Thao tác đã hết hiệu lực. Mở lại mục quản trị.")
                return
            try:
                if flow["kind"] == "coupon_new":
                    code = self.store.create_coupon(
                        flow["code"], flow["discount_kind"], flow["value"],
                        flow["max_uses"], flow["expires_on"], flow["product_id"],
                    )
                    destination = ("coupon", code)
                elif flow["kind"] == "bundle_new":
                    bundle_id = self.store.create_bundle(
                        flow["name"], flow["price"], flow["items"], flow["description"],
                    )
                    destination = ("bundle", bundle_id)
                elif flow["kind"] == "bundle_archive":
                    bundle_id = flow["bundle_id"]
                    self.store.set_bundle_deleted(bundle_id, True)
                    destination = ("bundle", bundle_id)
                else:
                    return
            except (ValueError, NotFound):
                context.user_data.pop("admin_offer_flow", None)
                await message.reply_text("Dữ liệu đã thay đổi hoặc không hợp lệ. Hãy kiểm tra và tạo lại.")
                return
            context.user_data.pop("admin_offer_flow", None)
            await message.reply_text("Đã lưu thay đổi.")
            if destination[0] == "coupon":
                await self.send_admin_coupon(message, destination[1])
            else:
                await self.send_admin_bundle(message, destination[1])
            return
        if action == "coupon" and len(parts) >= 4:
            operation = parts[3]
            if operation == "new":
                context.user_data["admin_offer_flow"] = {
                    "kind": "coupon_new", "step": "code", "token": secrets.token_hex(4),
                }
                await message.reply_text(
                    "Gửi mã giảm giá mới (4–24 ký tự A–Z, 0–9, _ hoặc -).",
                    reply_markup=self.offer_cancel_markup(context.user_data["admin_offer_flow"]["token"]),
                )
                return
            if len(parts) < 5:
                return
            code = parts[4].upper()
            coupon = next((item for item in self.store.list_coupons()
                           if item["code"] == code), None)
            if not coupon:
                await message.reply_text("Không tìm thấy mã giảm giá.")
                return
            if operation == "view":
                await self.send_admin_coupon(message, code)
            elif operation == "toggle":
                self.store.set_coupon_active(code, not coupon["active"])
                await self.send_admin_coupon(message, code)
            elif operation == "edit" and len(parts) == 6 and parts[5] in ("discount", "uses", "expiry"):
                field = parts[5]
                context.user_data["admin_offer_flow"] = {
                    "kind": "coupon_edit", "step": field, "code": code,
                    "token": secrets.token_hex(4),
                }
                prompt = {
                    "discount": "Gửi mức giảm mới, ví dụ 10% hoặc 5000.",
                    "uses": "Gửi số lượt tối đa mới (không thấp hơn số đã dùng).",
                    "expiry": "Gửi ngày hết hạn YYYY-MM-DD hoặc dấu - để bỏ hạn.",
                }[field]
                await message.reply_text(prompt, reply_markup=self.offer_cancel_markup(context.user_data["admin_offer_flow"]["token"]))
            return
        if action == "bundle" and len(parts) >= 4:
            operation = parts[3]
            if operation == "new":
                context.user_data["admin_offer_flow"] = {
                    "kind": "bundle_new", "step": "name", "token": secrets.token_hex(4),
                }
                await message.reply_text(
                    "Gửi tên gói mới (1–100 ký tự).",
                    reply_markup=self.offer_cancel_markup(context.user_data["admin_offer_flow"]["token"]),
                )
                return
            if len(parts) < 5:
                return
            try:
                bundle_id = int(parts[4])
            except ValueError:
                return
            bundle = self.store.get_bundle(bundle_id)
            if not bundle:
                await message.reply_text("Không tìm thấy gói.")
                return
            if operation == "restore" and bundle["deleted"]:
                self.store.set_bundle_deleted(bundle_id, False)
                await self.send_admin_bundle(message, bundle_id)
                return
            if bundle["deleted"]:
                await message.reply_text("Gói đã lưu trữ. Hãy khôi phục trước khi sửa.")
                return
            if operation == "toggle":
                self.store.set_bundle_active(bundle_id, not bundle["active"])
                await self.send_admin_bundle(message, bundle_id)
            elif operation == "archive":
                flow = {"kind": "bundle_archive", "step": "confirm", "bundle_id": bundle_id,
                        "token": secrets.token_hex(4)}
                context.user_data["admin_offer_flow"] = flow
                await self.send_offer_preview(message, flow)
            elif operation == "edit" and len(parts) == 6 and parts[5] in ("name", "price", "description"):
                field = parts[5]
                context.user_data["admin_offer_flow"] = {
                    "kind": "bundle_edit", "step": field, "bundle_id": bundle_id,
                    "token": secrets.token_hex(4),
                }
                prompt = {
                    "name": "Gửi tên gói mới (1–100 ký tự).",
                    "price": "Gửi giá gói mới bằng VND. Giá phải thấp hơn tổng giá lẻ.",
                    "description": "Gửi mô tả mới (tối đa 500 ký tự), hoặc dấu - để xóa.",
                }[field]
                await message.reply_text(prompt, reply_markup=self.offer_cancel_markup(context.user_data["admin_offer_flow"]["token"]))
            return
        if action in ("c", "b") and len(parts) >= 5:
            flow = context.user_data.get("admin_offer_flow")
            if not flow or flow.get("token") != parts[-1]:
                await message.reply_text("Thao tác đã hết hiệu lực. Mở lại mục quản trị.")
                return
            operation = parts[3]
            if action == "c" and flow["kind"] == "coupon_new":
                if operation == "page" and flow["step"] == "scope" and len(parts) == 6:
                    await self.send_offer_coupon_scope(message, flow, int(parts[4]))
                elif operation in ("all", "pick") and flow["step"] == "scope":
                    if operation == "pick" and len(parts) == 6:
                        product_id = int(parts[4])
                        if not any(item["id"] == product_id for item in self.store.list_products()):
                            await message.reply_text("Sản phẩm không còn bán. Hãy chọn lại.")
                            return
                        flow["product_id"] = product_id
                    elif operation == "all" and len(parts) == 5:
                        flow["product_id"] = None
                    else:
                        return
                    flow["step"] = "expiry"
                    await self.send_offer_coupon_expiry(message, flow)
                elif operation in ("none", "date") and flow["step"] == "expiry":
                    if operation == "none":
                        flow["expires_on"], flow["step"] = None, "confirm"
                        await self.send_offer_preview(message, flow)
                    else:
                        flow["step"] = "expiry_input"
                        await message.reply_text(
                            "Gửi ngày hết hạn dạng YYYY-MM-DD.",
                            reply_markup=self.offer_cancel_markup(flow["token"]),
                        )
                return
            if action == "b" and flow["kind"] == "bundle_new":
                if operation == "skip" and flow["step"] == "description":
                    flow["description"], flow["step"] = "", "confirm"
                    await self.send_offer_preview(message, flow)
                    return
                if flow["step"] != "items":
                    return
                if operation == "page" and len(parts) == 6:
                    await self.send_offer_bundle_picker(message, flow, int(parts[4]))
                elif operation in ("add", "remove") and len(parts) == 6:
                    product_id = int(parts[4])
                    if not any(item["id"] == product_id for item in self.store.list_products()):
                        await message.reply_text("Sản phẩm không còn bán. Hãy chọn lại.")
                        return
                    count = flow["items"].get(product_id, 0)
                    if operation == "add":
                        if sum(flow["items"].values()) >= 10 or count >= 10:
                            await message.reply_text("Gói tối đa 10 tài khoản.")
                            return
                        flow["items"][product_id] = count + 1
                    elif count > 1:
                        flow["items"][product_id] = count - 1
                    else:
                        flow["items"].pop(product_id, None)
                    await self.send_offer_bundle_picker(message, flow, flow.get("picker_page", 0))
                elif operation == "next":
                    count = sum(flow["items"].values())
                    if not 2 <= count <= 10:
                        await message.reply_text("Chọn 2–10 tài khoản trong gói trước khi tiếp tục.")
                        return
                    flow["step"] = "price"
                    products = {item["id"]: item for item in self.store.list_products()}
                    regular = sum(products[pid]["price"] * qty for pid, qty in flow["items"].items()
                                  if pid in products)
                    await message.reply_text(
                        f"Giá lẻ cộng lại: {vnd(regular)}. Gửi giá gói thấp hơn mức này.",
                        reply_markup=self.offer_cancel_markup(flow["token"]),
                    )

    async def send_admin_products(self, message, page: int = 0):
        products = self.store.list_products(include_inactive=True)
        await self._send_admin_product_list(message, products, page, archived=False)

    async def send_admin_archived(self, message, page: int = 0):
        products = [
            product for product in self.store.list_products(
                include_inactive=True, include_deleted=True
            ) if product["deleted"]
        ]
        await self._send_admin_product_list(message, list(reversed(products)), page, archived=True)

    async def _send_admin_product_list(self, message, products, page: int, archived: bool):
        visible = products[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = ["🗂 SẢN PHẨM ĐÃ LƯU TRỮ" if archived else "📦 SẢN PHẨM",
                 "Chạm vào sản phẩm để quản lý."]
        rows = ([[InlineKeyboardButton("⬅️ Sản phẩm đang quản lý",
                                       callback_data="admin:products:0")]] if archived else [
            [InlineKeyboardButton("➕ Thêm sản phẩm", callback_data="admin:catalog:new")],
            [InlineKeyboardButton("📄 Thêm sản phẩm file .json", callback_data="admin:catalog:newfile")],
            [InlineKeyboardButton("🗂 Sản phẩm đã lưu trữ", callback_data="admin:archived:0")],
        ])
        for product in visible:
            status = "đã lưu trữ" if archived else "đang bán" if product["active"] else "đã ẩn"
            shown_price = (
                f"{vnd(product['price'])} → 🔥 {vnd(product['sale_price'])} "
                f"({product['sale_remaining']} suất)"
                if product["sale_id"] else
                f"{vnd(product['previous_price'])} → {vnd(product['price'])}"
                if product["previous_price"] else vnd(product["price"])
            )
            lines.append(
                f"#{product['display_id']} {product['name']} · {shown_price} "
                f"· còn {product['available']} · đã bán {product['sold']} · {status}"
            )
            rows.append([InlineKeyboardButton(
                f"Xem #{product['display_id']} {product['name'][:30]}",
                callback_data=f"admin:product:{product['id']}",
            )])
        if not visible:
            lines.append("Chưa có sản phẩm ở trang này.")
        prefix = "admin:archived" if archived else "admin:products"
        rows.extend(admin_footer(prefix, page, len(products) > (page + 1) * PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_product(self, message, product_id: int):
        product = self.store.get_product(product_id)
        if not product:
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        shown = f"#{product['display_id']}"
        counts = self.store.stock_counts(product_id)
        follower_count = self.store.product_followers(
            product_id, self.config.admin_ids, limit=1,
        )["total"]
        status = "đã lưu trữ" if product["deleted"] else "đang bán" if product["active"] else "đã ẩn"
        ordered = self.store.list_products(include_inactive=True)
        position = next((index for index, item in enumerate(ordered, start=1)
                         if item["id"] == product_id), None)
        sale = (
            f"🔥 Đang sale: {vnd(product['sale_price'])} · "
            f"còn {product['sale_remaining']}/{product['sale_limit']} suất\n"
            if product["sale_id"] else ""
        )
        rows = []
        file_product = product["delivery_type"] == "file"
        if product["deleted"]:
            rows.append([InlineKeyboardButton(
                "♻️ Khôi phục sản phẩm", callback_data=f"admin:catalog:restore:{product_id}",
            )])
            rows.append([InlineKeyboardButton(
                "🔐 Xem kho", callback_data=f"admin:inventory:{product_id}:0",
            )])
            rows.append([InlineKeyboardButton(
                "🧾 Xem file đã bán" if file_product else "🧾 Xem tài khoản đã bán",
                callback_data=f"admin:sold:{product_id}:0",
            )])
        else:
            price_row = ([] if product["sale_id"] else [InlineKeyboardButton(
                "💰 Sửa giá", callback_data=f"admin:catalog:edit:{product_id}:price",
            )])
            if product["sale_id"] or (product["active"] and product["available"]):
                price_row.append(InlineKeyboardButton(
                    "🔥 Kết thúc sale" if product["sale_id"] else "🔥 Tạo sale",
                    callback_data=f"admin:catalog:{'endsale' if product['sale_id'] else 'sale'}:{product_id}",
                ))
            rows.extend([
                [InlineKeyboardButton("✏️ Sửa tên", callback_data=f"admin:catalog:edit:{product_id}:name"),
                 InlineKeyboardButton("📝 Sửa mô tả", callback_data=f"admin:catalog:edit:{product_id}:description")],
                price_row,
                [InlineKeyboardButton("📄 Nhập file .json" if file_product else "➕ Nhập tài khoản",
                                      callback_data=f"admin:catalog:stock:{product_id}"),
                 InlineKeyboardButton("🔐 Xem kho", callback_data=f"admin:inventory:{product_id}:0")],
                [InlineKeyboardButton("🧾 Xem file đã bán" if file_product else "🧾 Xem tài khoản đã bán",
                                      callback_data=f"admin:sold:{product_id}:0")],
                [InlineKeyboardButton("👁 Hiện bán" if not product["active"] else "🙈 Ẩn sản phẩm",
                                      callback_data=f"admin:catalog:visibility:{product_id}"),
                 InlineKeyboardButton("📧 Tắt email" if product["requires_email"] else "📧 Yêu cầu email",
                                      callback_data=f"admin:email:{product_id}:"
                                                    f"{'off' if product['requires_email'] else 'on'}")],
                [InlineKeyboardButton("🔢 Đổi số hiển thị", callback_data=f"admin:displayid:{product_id}"),
                 InlineKeyboardButton("🗑 Xóa khỏi shop", callback_data=f"admin:catalog:archive:{product_id}")],
            ])
            if not file_product:
                rows.insert(4, [InlineKeyboardButton(
                    "💬 Hiện tài khoản + file .txt" if product["file_only_delivery"]
                    else "📄 Chỉ giao bằng file .txt",
                    callback_data=f"admin:catalog:delivery:{product_id}:"
                                  f"{'chat' if product['file_only_delivery'] else 'file'}",
                )])
        if position:
            move_buttons = []
            if position > 1:
                move_buttons.append(InlineKeyboardButton(
                    "⬆️ Lên", callback_data=f"admin:moveproduct:{product_id}:up",
                ))
            if position < len(ordered):
                move_buttons.append(InlineKeyboardButton(
                    "⬇️ Xuống", callback_data=f"admin:moveproduct:{product_id}:down",
                ))
            if move_buttons:
                rows.append(move_buttons)
        rows.append([InlineKeyboardButton(
            f"👥 Khách theo dõi ({follower_count})",
            callback_data=f"admin:followers:{product_id}:0",
        )])
        if product["active"] and not product["deleted"] and product["available"]:
            rows.append([InlineKeyboardButton(
                "📣 Tạo bài đăng", callback_data=f"admin:postproduct:{product_id}",
            )])
        rows.extend([
            [InlineKeyboardButton("⬅️ Sản phẩm", callback_data="admin:products:0"),
             InlineKeyboardButton("🛠 Quản trị", callback_data="admin:home")],
        ])
        position_text = (f" · vị trí quản trị {position}/{len(ordered)}" if position
                         else " · đã lưu trữ")
        await message.reply_text(
            f"📦 SẢN PHẨM #{product['display_id']}\n{product['name']}\n"
            f"Số hiển thị: {shown}{position_text}\n"
            f"Loại: {'File .json' if file_product else 'Tài khoản'}\n"
            f"Cách giao: {'Chỉ file .json' if file_product else 'Chỉ file .txt' if product['file_only_delivery'] else 'Tin nhắn + file .txt'}\n"
            f"Mô tả: {product['description'] or '(trống)'}\n"
            "Giá: "
            + (f"{vnd(product['previous_price'])} → {vnd(product['price'])}"
               if product["previous_price"] else vnd(product["price"]))
            + f" · {status}\n"
            f"Email khi mua: {'bắt buộc' if product['requires_email'] else 'không yêu cầu'}\n"
            f"{sale}Kho: {counts['available']} chưa bán, {counts['sold']} đã bán\n\n"
            f"Khách theo dõi: {follower_count}\n\n"
            "Chọn nút bên dưới để quản lý sản phẩm.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def send_admin_product_followers(self, message, product_id: int,
                                           page: int = 0):
        product = self.store.get_product(product_id)
        if not product:
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        report = self.store.product_followers(
            product_id, self.config.admin_ids, FOLLOWER_PAGE_SIZE,
            page * FOLLOWER_PAGE_SIZE,
        )
        if page and page * FOLLOWER_PAGE_SIZE >= report["total"]:
            page = max(0, (report["total"] - 1) // FOLLOWER_PAGE_SIZE)
            report = self.store.product_followers(
                product_id, self.config.admin_ids, FOLLOWER_PAGE_SIZE,
                page * FOLLOWER_PAGE_SIZE,
            )
        lines = [
            f"👥 KHÁCH THEO DÕI #{product['display_id']} {product['name']}",
            f"Tổng: {report['total']} khách",
        ]
        rows = []
        for follower in report["followers"]:
            user_id = follower["user_id"]
            label = user_label(user_id, follower["username"], follower["display_name"])
            status = " · 🔕 tắt tin kho" if not follower["stock_notifications_enabled"] else ""
            status += " · 🚫 bị chặn" if follower["banned"] else ""
            lines.append(
                f"{label} · từ {status_time(parse_stored_time(follower['created_at']))}{status}"
            )
            rows.append([InlineKeyboardButton(
                f"👤 Xem ID {user_id}", callback_data=f"admin:user:{user_id}",
            )])
        if not report["followers"]:
            lines.append("Chưa có khách theo dõi sản phẩm này.")
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(
                "◀ Trước", callback_data=f"admin:followers:{product_id}:{page - 1}",
            ))
        if report["has_more"]:
            navigation.append(InlineKeyboardButton(
                "Sau ▶", callback_data=f"admin:followers:{product_id}:{page + 1}",
            ))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton(
            "⬅️ Sản phẩm", callback_data=f"admin:product:{product_id}",
        )])
        await message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
            protect_content=True,
        )

    @staticmethod
    def admin_catalog_cancel_button():
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Hủy thao tác", callback_data="admin:catalog:cancel"),
        ]])

    async def admin_catalog_preview(self, message, flow: dict):
        token = flow["token"]
        if flow["kind"] == "create":
            file_product = flow.get("delivery_type") == "file"
            summary = (f"➕ XÁC NHẬN THÊM SẢN PHẨM\nTên: {flow['name']}\n"
                       f"Loại: {'File .json' if file_product else 'Tài khoản'}\n"
                       f"Giá: {vnd(flow['price'])}\n"
                       f"Mô tả: {flow['description'] or '(trống)'}\n\n"
                       "Sản phẩm mới chưa có hàng trong kho. Khách chưa thể mua đến khi bạn nhập kho.")
            label = "Tạo sản phẩm"
        elif flow["kind"] == "sale":
            product = self.store.get_product(flow["product_id"])
            if not product or product["deleted"] or not product["active"]:
                await message.reply_text("Sản phẩm không còn bán. Hãy mở lại danh sách sản phẩm.")
                return
            summary = (f"🔥 XÁC NHẬN SALE #{product['display_id']} {product['name']}\n"
                       f"Giá gốc: {vnd(product['price'])}\n"
                       f"Giá sale: {vnd(flow['price'])}\n"
                       f"Số suất: {flow['quantity']}\n\n"
                       "Khi xác nhận, bot sẽ xếp thông báo cho khách theo dõi sản phẩm.")
            label = "Bắt đầu sale"
        else:
            product = self.store.get_product(flow["product_id"])
            if not product:
                await message.reply_text("Không tìm thấy sản phẩm.")
                return
            action = "lưu trữ" if flow["kind"] == "archive" else (
                "ẩn và kết thúc sale" if flow["kind"] == "hide" else "kết thúc sale"
            )
            summary = f"Xác nhận {action} sản phẩm #{product['display_id']} {product['name']}?"
            if flow["kind"] == "archive":
                summary += "\nĐơn hàng và kho vẫn được giữ; bạn có thể khôi phục sau."
            label = "Xác nhận"
        await message.reply_text(
            summary,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(label, callback_data=f"admin:catalog:confirm:{token}")],
                [InlineKeyboardButton("Hủy", callback_data="admin:catalog:cancel")],
            ]),
        )

    async def admin_catalog_text(self, message, context, raw: str):
        flow = context.user_data["admin_product_flow"]
        kind, step = flow["kind"], flow["step"]
        value = raw.strip()
        if kind == "create":
            if step == "name":
                name = " ".join(value.split())
                if not 1 <= len(name) <= 100:
                    await message.reply_text("Tên cần từ 1 đến 100 ký tự. Gửi lại tên sản phẩm.")
                    return
                flow["name"], flow["step"] = name, "price"
                await message.reply_text(
                    "Gửi giá bán bằng VND, ví dụ 30000 hoặc 30k.",
                    reply_markup=self.admin_catalog_cancel_button(),
                )
                return
            if step == "price":
                try:
                    price = parse_amount(value)
                    if price <= 0:
                        raise ValueError("Price must be positive")
                except ValueError:
                    await message.reply_text("Giá không hợp lệ. Gửi số VND, ví dụ 30000 hoặc 30k.")
                    return
                flow["price"], flow["step"] = price, "description"
                await message.reply_text(
                    "Gửi mô tả (tối đa 500 ký tự) hoặc bấm Bỏ qua.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Bỏ qua mô tả",
                                              callback_data=f"admin:catalog:skipdesc:{flow['token']}")],
                        [InlineKeyboardButton("Hủy", callback_data="admin:catalog:cancel")],
                    ]),
                )
                return
            if step == "description":
                if len(value) > 500:
                    await message.reply_text("Mô tả tối đa 500 ký tự. Hãy gửi lại.")
                    return
                flow["description"], flow["step"] = value, "confirm"
                await self.admin_catalog_preview(message, flow)
                return
        if kind == "edit":
            product_id = flow["product_id"]
            try:
                if step == "name":
                    name = " ".join(value.split())
                    self.store.update_product_details(product_id, name=name)
                elif step == "description":
                    self.store.update_product_details(
                        product_id, description="" if value == "-" else value,
                    )
                elif step == "price":
                    self.store.set_price(product_id, parse_amount(value))
                else:
                    return
            except ValueError:
                await message.reply_text(
                    "Giá trị không hợp lệ. Tên 1–100 ký tự, mô tả tối đa 500 ký tự, "
                    "giá là số VND lớn hơn 0. Hãy gửi lại."
                )
                return
            except SaleActive:
                context.user_data.pop("admin_product_flow", None)
                await message.reply_text("Sản phẩm đang sale. Hãy kết thúc sale trước khi sửa giá.")
                await self.send_admin_product(message, product_id)
                return
            except NotFound:
                context.user_data.pop("admin_product_flow", None)
                await message.reply_text("Sản phẩm không còn để chỉnh sửa.")
                return
            context.user_data.pop("admin_product_flow", None)
            await message.reply_text("Đã cập nhật sản phẩm.")
            await self.send_admin_product(message, product_id)
            return
        if kind == "sale":
            product = self.store.get_product(flow["product_id"])
            if not product or product["deleted"] or not product["active"]:
                context.user_data.pop("admin_product_flow", None)
                await message.reply_text("Sản phẩm không còn bán.")
                return
            if step == "price":
                try:
                    price = parse_amount(value)
                    if not 0 < price < product["price"]:
                        raise ValueError("Invalid sale price")
                except ValueError:
                    await message.reply_text(
                        f"Giá sale phải thấp hơn {vnd(product['price'])}. Gửi lại giá sale."
                    )
                    return
                flow["price"], flow["step"] = price, "quantity"
                await message.reply_text(
                    f"Gửi số suất sale (từ 1 đến {product['available']}).",
                    reply_markup=self.admin_catalog_cancel_button(),
                )
                return
            if step == "quantity":
                try:
                    quantity = int(value)
                    if not 1 <= quantity <= product["available"]:
                        raise ValueError("Invalid sale quantity")
                except ValueError:
                    await message.reply_text(
                        f"Số suất phải là số nguyên từ 1 đến {product['available']}. Hãy gửi lại."
                    )
                    return
                flow["quantity"], flow["step"] = quantity, "confirm"
                await self.admin_catalog_preview(message, flow)
                return
        if step == "confirm":
            await self.admin_catalog_preview(message, flow)

    async def admin_catalog_callback(self, message, data: str, context):
        parts = data.split(":")
        action = parts[2] if len(parts) > 2 else ""
        if action == "cancel":
            context.user_data.pop("admin_product_flow", None)
            context.user_data.pop("awaiting_bulk_stock", None)
            context.user_data.pop("awaiting_file_stock", None)
            await message.reply_text("Đã hủy thao tác quản lý sản phẩm.")
            await self.send_admin_products(message)
            return
        if action in ("new", "newfile"):
            context.user_data["admin_product_flow"] = {
                "kind": "create", "step": "name", "token": secrets.token_hex(4),
                "delivery_type": "file" if action == "newfile" else "account",
            }
            await message.reply_text(
                ("📄 THÊM SẢN PHẨM FILE .JSON\n" if action == "newfile"
                 else "➕ THÊM SẢN PHẨM\n")
                + "Gửi tên sản phẩm (tối đa 100 ký tự).",
                reply_markup=self.admin_catalog_cancel_button(),
            )
            return
        if action in ("skipdesc", "confirm") and len(parts) == 4:
            flow = context.user_data.get("admin_product_flow")
            if not flow or flow.get("token") != parts[3]:
                await message.reply_text("Thao tác đã hết hiệu lực. Mở lại mục Sản phẩm.")
                return
            if action == "skipdesc":
                if flow["kind"] != "create" or flow["step"] != "description":
                    return
                flow["description"], flow["step"] = "", "confirm"
                await self.admin_catalog_preview(message, flow)
                return
            if flow["step"] != "confirm":
                return
            try:
                if flow["kind"] == "create":
                    product_id = self.store.add_product(
                        flow["name"], flow["price"], flow["description"],
                        delivery_type=flow.get("delivery_type", "account"),
                    )
                    notice = ("Đã tạo sản phẩm file. Bấm Nhập file .json để thêm hàng vào kho."
                              if flow.get("delivery_type") == "file" else
                              "Đã tạo sản phẩm. Bấm Nhập tài khoản để thêm hàng vào kho.")
                else:
                    product_id = flow["product_id"]
                    if flow["kind"] == "archive":
                        self.store.set_deleted(product_id, True)
                        notice = "Đã lưu trữ sản phẩm; đơn và kho vẫn được giữ."
                    elif flow["kind"] == "hide":
                        self.store.set_active(product_id, False)
                        notice = "Đã ẩn sản phẩm và kết thúc sale."
                    elif flow["kind"] == "endsale":
                        self.store.end_sale(product_id)
                        notice = "Đã kết thúc sale; giá thường được áp dụng lại."
                    elif flow["kind"] == "sale":
                        result = self.store.start_sale(
                            product_id, flow["price"], flow["quantity"],
                            announce_except=self.config.admin_ids,
                        )
                        notice = (f"Đã bắt đầu sale. Đã xếp thông báo cho "
                                  f"{result['recipients']} khách theo dõi sản phẩm.")
                    else:
                        return
            except (ValueError, NotFound, SaleActive, OutOfStock):
                context.user_data.pop("admin_product_flow", None)
                await message.reply_text(
                    "Sản phẩm hoặc kho đã thay đổi nên không thể hoàn tất. Hãy kiểm tra rồi thử lại."
                )
                return
            context.user_data.pop("admin_product_flow", None)
            await message.reply_text(notice)
            await self.send_admin_product(message, product_id)
            return
        if len(parts) not in (4, 5):
            return
        try:
            product_id = int(parts[3])
        except ValueError:
            return
        product = self.store.get_product(product_id)
        if not product:
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        if action == "restore" and product["deleted"]:
            context.user_data.pop("admin_product_flow", None)
            self.store.set_deleted(product_id, False)
            await message.reply_text("Đã khôi phục sản phẩm ở trạng thái ẩn.")
            await self.send_admin_product(message, product_id)
            return
        if action == "restore":
            await message.reply_text("Sản phẩm này chưa được lưu trữ.")
            return
        if product["deleted"]:
            await message.reply_text("Sản phẩm đã lưu trữ. Bấm Khôi phục trước khi chỉnh sửa.")
            return
        if action == "delivery" and len(parts) == 5 and parts[4] in ("file", "chat"):
            file_only = parts[4] == "file"
            try:
                self.store.set_file_only_delivery(product_id, file_only)
            except (ValueError, NotFound):
                await message.reply_text("Không đổi được cách giao của sản phẩm này.")
                return
            await message.reply_text(
                "Đã chọn chỉ giao nội dung trong file .txt. Khách vẫn nhận tin xác nhận đơn."
                if file_only else
                "Đã chọn hiện thông tin tài khoản trong tin nhắn và gửi kèm file .txt."
            )
            await self.send_admin_product(message, product_id)
            return
        if action == "edit" and len(parts) == 5 and parts[4] in ("name", "description", "price"):
            field = parts[4]
            if field == "price" and product["sale_id"]:
                await message.reply_text("Hãy kết thúc sale trước khi sửa giá thường.")
                return
            context.user_data["admin_product_flow"] = {
                "kind": "edit", "step": field, "product_id": product_id,
                "token": secrets.token_hex(4),
            }
            prompt = {
                "name": "Gửi tên mới (1–100 ký tự).",
                "description": "Gửi mô tả mới (tối đa 500 ký tự). Gửi dấu - để xóa mô tả.",
                "price": "Gửi giá mới bằng VND, ví dụ 30000 hoặc 30k.",
            }[field]
            await message.reply_text(
                f"✏️ SẢN PHẨM #{product['display_id']} {product['name']}\n{prompt}",
                reply_markup=self.admin_catalog_cancel_button(),
            )
            return
        if action == "stock":
            context.user_data.pop("admin_product_flow", None)
            if product["delivery_type"] == "file":
                context.user_data.pop("awaiting_bulk_stock", None)
                context.user_data["awaiting_file_stock"] = product_id
                await message.reply_text(
                    f"Gửi một file .json UTF-8 hợp lệ cho sản phẩm #{product['display_id']} "
                    "(tối đa 1 MB). Mỗi file là một đơn vị trong kho. "
                    "Có thể gửi tiếp nhiều file; /cancel để dừng nhập.",
                    reply_markup=self.admin_catalog_cancel_button(),
                )
                return
            context.user_data.pop("awaiting_file_stock", None)
            context.user_data["awaiting_bulk_stock"] = product_id
            await message.reply_text(
                f"Gửi tài khoản cho sản phẩm #{product['display_id']}, mỗi dòng một tài khoản "
                "(tối đa 100 dòng). Bot sẽ xóa tin nhắn chứa tài khoản sau khi nhận. "
                "Dòng trùng vẫn được nhập và báo sau.",
                reply_markup=self.admin_catalog_cancel_button(),
            )
            return
        if action == "visibility":
            context.user_data.pop("admin_product_flow", None)
            if product["active"] and product["sale_id"]:
                flow = {"kind": "hide", "step": "confirm", "product_id": product_id,
                        "token": secrets.token_hex(4)}
                context.user_data["admin_product_flow"] = flow
                await self.admin_catalog_preview(message, flow)
                return
            try:
                self.store.set_active(product_id, not product["active"])
            except NotFound:
                await message.reply_text("Sản phẩm không còn để chỉnh sửa.")
                return
            await message.reply_text("Đã hiện sản phẩm." if not product["active"] else "Đã ẩn sản phẩm.")
            await self.send_admin_product(message, product_id)
            return
        if action == "sale":
            if not product["active"] or not product["available"] or product["sale_id"]:
                await message.reply_text("Chỉ tạo sale khi sản phẩm đang hiện bán, còn kho và chưa có sale.")
                return
            context.user_data["admin_product_flow"] = {
                "kind": "sale", "step": "price", "product_id": product_id,
                "token": secrets.token_hex(4),
            }
            await message.reply_text(
                f"Gửi giá sale cho #{product['display_id']} (giá gốc {vnd(product['price'])}).",
                reply_markup=self.admin_catalog_cancel_button(),
            )
            return
        if action in ("archive", "endsale"):
            if action == "endsale" and not product["sale_id"]:
                await message.reply_text("Sản phẩm không còn sale đang chạy.")
                return
            flow = {"kind": action, "step": "confirm", "product_id": product_id,
                    "token": secrets.token_hex(4)}
            context.user_data["admin_product_flow"] = flow
            await self.admin_catalog_preview(message, flow)

    async def send_admin_product_post(self, message, product_id: int):
        product = self.store.get_product(product_id)
        if not product:
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        if product["deleted"] or not product["active"] or not product["available"]:
            await message.reply_text(
                "Chỉ tạo bài đăng cho sản phẩm đang bán và còn hàng. "
                "Hãy kiểm tra trạng thái và kho rồi thử lại."
            )
            return
        built = self.build_product_post(product)
        if not built:
            await message.reply_text("Chưa lấy được tên bot để tạo link mua. Hãy thử lại sau.")
            return
        post, share_url = built
        await message.reply_text(
            post,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Chia sẻ vào nhóm", url=share_url)],
                [InlineKeyboardButton("⬅️ Về sản phẩm",
                                      callback_data=f"admin:product:{product_id}")],
            ]),
            parse_mode="HTML",
        )

    async def send_admin_inventory(self, message, product_id: int, page: int = 0):
        product = self.store.get_product(product_id)
        if not product:
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        items = self.store.list_stock_items(
            product_id, PAGE_SIZE + 1, page * PAGE_SIZE,
            include_secret=product["delivery_type"] != "file",
        )
        visible = items[:PAGE_SIZE]
        counts = self.store.stock_counts(product_id)
        header = "\n".join([
            f"🔐 KHO SẢN PHẨM #{product['display_id']} · {product['name']}",
            f"Chưa bán: {counts['available']} · Đã bán: {counts['sold']}",
        ])
        if not visible:
            header += "\n\nKhông có hàng chưa bán ở trang này."
        rows = []
        if product["delivery_type"] == "file":
            lines = [header]
            for item in visible:
                lines.append(f"File kho #{item['id']}")
                rows.append([InlineKeyboardButton(
                    f"📄 Xem file kho #{item['id']}",
                    callback_data=f"admin:stock:{item['id']}",
                )])
            rows.extend(admin_footer(f"admin:inventory:{product_id}", page,
                                     len(items) > PAGE_SIZE))
            rows.insert(-1, [InlineKeyboardButton(
                "⬅️ Sản phẩm", callback_data=f"admin:product:{product_id}",
            )])
            await message.reply_text("\n".join(lines),
                                     reply_markup=InlineKeyboardMarkup(rows),
                                     protect_content=True)
            return
        rows.extend(admin_footer(f"admin:inventory:{product_id}", page, len(items) > PAGE_SIZE))
        rows.insert(-1, [InlineKeyboardButton("⬅️ Sản phẩm", callback_data=f"admin:product:{product_id}")])
        pages = inventory_messages(header, visible)
        for part in pages[:-1]:
            await message.reply_text(
                part, protect_content=True,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        await message.reply_text(
            pages[-1], reply_markup=InlineKeyboardMarkup(rows), protect_content=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def send_admin_sold_stock(self, message, product_id: int, page: int = 0):
        product = self.store.get_product(product_id)
        if not product:
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        items = self.store.list_sold_stock_items(
            product_id, PAGE_SIZE + 1, page * PAGE_SIZE,
        )
        counts = self.store.stock_counts(product_id)
        file_product = product["delivery_type"] == "file"
        lines = [
            f"🧾 {'FILE' if file_product else 'TÀI KHOẢN'} ĐÃ BÁN · #{product['display_id']} {product['name']}",
            f"Đã bán: {counts['sold']} · Chưa bán: {counts['available']}",
        ]
        rows = []
        for item in items[:PAGE_SIZE]:
            buyer = (user_label(item["user_id"], item["username"], item["display_name"])
                     if item["user_id"] else "không có thông tin đơn")
            order = f"đơn #{item['order_id']}" if item["order_id"] else "chưa có đơn"
            sold_time = status_time(parse_stored_time(item["sold_at"]))
            lines.append(f"Kho #{item['id']} · {order} · {buyer} · {sold_time}")
            rows.append([InlineKeyboardButton(
                f"{'📄 Xem file' if file_product else '🔐 Xem tài khoản'} kho #{item['id']}",
                callback_data=f"admin:solditem:{item['id']}:{page}",
            )])
        if not items[:PAGE_SIZE]:
            lines.append("Không có hàng đã bán ở trang này.")
        rows.extend(admin_footer(f"admin:sold:{product_id}", page, len(items) > PAGE_SIZE))
        rows.insert(-1, [
            InlineKeyboardButton("🔐 Xem chưa bán", callback_data=f"admin:inventory:{product_id}:0"),
            InlineKeyboardButton("⬅️ Sản phẩm", callback_data=f"admin:product:{product_id}"),
        ])
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
                                 protect_content=True)

    async def send_admin_sold_stock_item(self, message, stock_id: int, page: int = 0):
        item = self.store.get_sold_stock_item(stock_id)
        if not item:
            await message.reply_text("Không tìm thấy tài khoản đã bán này.")
            return
        if self.store.get_product(item["product_id"])["delivery_type"] == "file":
            extension = order_delivery_extension([item | {"delivery_type": "file"}])
            await message.reply_document(
                document=InputFile(BytesIO(item["secret"].encode("utf-8")),
                                   filename=f"file-da-ban-{stock_id}{extension}"),
                caption=f"📄 File đã bán · kho #{stock_id} · đơn #{item['order_id']}",
                protect_content=True,
            )
            return
        buyer = (user_label(item["user_id"], item["username"], item["display_name"])
                 if item["user_id"] else "không có thông tin đơn")
        header = "\n".join([
            f"🔐 TÀI KHOẢN ĐÃ BÁN · #{item['display_id']} {item['product_name']}",
            f"Kho #{stock_id} · Đơn #{item['order_id']}" if item["order_id"]
            else f"Kho #{stock_id} · Chưa có đơn",
            f"Khách: {buyer}",
            f"Đã bán: {status_time(parse_stored_time(item['sold_at']))}",
            f"Giá đơn: {vnd(item['price'])}" if item["price"] is not None else "Giá đơn: chưa có",
            f"Email xử lý: {item['customer_email']}" if item["customer_email"] else "",
            "\nThông tin tài khoản đã giao:",
        ])
        rows = []
        if item["order_id"]:
            rows.append([InlineKeyboardButton(
                f"🧾 Xem đơn #{item['order_id']}",
                callback_data=f"admin:order:{item['order_id']}",
            )])
        rows.append([InlineKeyboardButton(
            "⬅️ Danh sách đã bán",
            callback_data=f"admin:sold:{item['product_id']}:{page}",
        )])
        pages = inventory_messages(header, [{"id": stock_id, "secret": item["secret"]}])
        for part in pages[:-1]:
            await message.reply_text(
                part, protect_content=True,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        await message.reply_text(
            pages[-1], reply_markup=InlineKeyboardMarkup(rows), protect_content=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def send_admin_stock_item(self, message, stock_id: int):
        item = self.store.get_stock_item(stock_id)
        if not item:
            await message.reply_text("Không tìm thấy tài khoản chưa bán trong kho.")
            return
        if self.store.get_product(item["product_id"])["delivery_type"] == "file":
            extension = order_delivery_extension([item | {"delivery_type": "file"}])
            await message.reply_document(
                document=InputFile(BytesIO(item["secret"].encode("utf-8")),
                                   filename=f"file-kho-{stock_id}{extension}"),
                caption=f"📄 File chưa bán · kho #{stock_id}",
                protect_content=True,
            )
            return
        display_id = self.store.get_product(item["product_id"])["display_id"]
        await message.reply_text(
            f"🔐 KHO #{stock_id} · SẢN PHẨM #{display_id} {item['name']}\n\n"
            f"{item['secret']}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Về kho", callback_data=f"admin:inventory:{item['product_id']}:0")
            ]]),
            protect_content=True,
        )

    async def admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("awaiting_file_stock", None)
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("pending_broadcast", None)
            context.user_data.pop("awaiting_admin_user_search", None)
            context.user_data.pop("admin_user_search_query", None)
            context.user_data.pop("awaiting_admin_sales_search", None)
            context.user_data.pop("admin_sales_search_query", None)
            context.user_data.pop("admin_product_flow", None)
            context.user_data.pop("admin_offer_flow", None)
            await self.send_admin_menu(update.message)

    async def admin_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("awaiting_admin_user_search", None)
            context.user_data.pop("admin_user_search_query", None)
            context.user_data.pop("awaiting_admin_sales_search", None)
            context.user_data.pop("admin_sales_search_query", None)
            await self.send_admin_users(update.message)

    async def admin_audit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.send_admin_audit(update.message)

    async def admin_bans(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.send_admin_bans(update.message)

    async def admin_unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            if len(context.args) != 1:
                raise ValueError
            user_id = int(context.args[0])
        except (ValueError, TypeError):
            await update.message.reply_text("Dùng: /unban Telegram_ID")
            return
        await self.unban_customer(update.message, user_id, update.effective_user.id)

    async def admin_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            user_id = int(context.args[0])
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /user Telegram_ID")
            return
        await self.send_admin_user(update.message, user_id)

    async def admin_sales(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("awaiting_admin_sales_search", None)
            context.user_data.pop("admin_sales_search_query", None)
            await self.send_admin_sales(update.message)

    async def admin_pending_emails(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.send_admin_pending_emails(update.message)

    async def admin_email_remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if not context.args:
            await self.send_email_reminder_settings(update.message)
            return
        if len(context.args) != 1:
            await update.message.reply_text("Dùng: /emailremind SỐ_PHÚT (0 để tắt)")
            return
        try:
            minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Số phút phải là số nguyên.")
            return
        await self.change_email_reminder(update.message, minutes)

    async def admin_order_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if len(context.args) != 1:
            await update.message.reply_text("Dùng: /orderemail ID_đơn")
            return
        try:
            order_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID đơn phải là số.")
            return
        await self.send_admin_order_contact(update.message, order_id)

    async def admin_email_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if len(context.args) != 1:
            await update.message.reply_text("Dùng: /emaildone ID_đơn")
            return
        try:
            order_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID đơn phải là số.")
            return
        await self.mark_email_order_done(update.message, order_id, update.effective_user.id)

    async def admin_require_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if len(context.args) != 2 or context.args[1].lower() not in ("on", "off"):
            await update.message.reply_text("Dùng: /requireemail #ID_sản_phẩm on hoặc off")
            return
        try:
            product_id = self.admin_product_id(context.args[0])
            required = context.args[1].lower() == "on"
            self.store.set_requires_email(product_id, required)
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except ValueError:
            await update.message.reply_text("ID sản phẩm phải là số dương.")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm hoặc đã lưu trữ.")
            return
        await update.message.reply_text(
            f"Sản phẩm #{self.store.get_product(product_id)['display_id']}: "
            + ("đã yêu cầu khách nhập email khi mua." if required else
               "đã tắt yêu cầu email khi mua."),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Xem sản phẩm", callback_data=f"admin:product:{product_id}")
            ]]),
        )

    async def admin_topups(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.send_admin_topups(update.message)

    async def admin_transactions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.send_admin_transactions(update.message)

    async def admin_warranties(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            if context.args and context.args[0].lower() == "all":
                await self.send_admin_all_warranties(update.message)
            else:
                await self.send_admin_warranties(update.message)

    async def admin_warranty_remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if not context.args:
            await self.send_warranty_reminder_settings(update.message)
            return
        if len(context.args) != 1:
            await update.message.reply_text("Dùng: /warrantyremind SỐ_PHÚT (0 để tắt)")
            return
        try:
            minutes = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Số phút phải là số nguyên.")
            return
        await self.change_warranty_reminder(update.message, minutes)

    async def admin_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if not context.args:
            await self.send_admin_report(update.message)
            return
        if len(context.args) != 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", context.args[0]):
            await update.message.reply_text("Dùng: /report hoặc /report YYYY-MM-DD")
            return
        try:
            day = date.fromisoformat(context.args[0])
        except ValueError:
            await update.message.reply_text("Ngày không hợp lệ. Dùng YYYY-MM-DD.")
            return
        await self.send_admin_report(update.message, day)

    async def admin_funnel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if len(context.args) > 1 or (context.args and context.args[0] not in ("7", "30")):
            await update.message.reply_text("Dùng: /funnel 7 hoặc /funnel 30")
            return
        await self.send_admin_funnel(update.message, int(context.args[0]) if context.args else 7)

    async def admin_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if context.args:
            self._enter_admin_export(context)
            await self.send_admin_export(update.message, context, " ".join(context.args))
        else:
            await self.begin_admin_export(update.message, context)

    async def admin_resolve_warranty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        deleted = await self.delete_private_input(update.message)
        parts = update.message.text.partition(" ")[2].split("|", 1)
        try:
            if len(parts) != 2:
                raise ValueError("Missing reply")
            ticket_id = int(parts[0].strip())
            result = self.store.resolve_warranty(ticket_id, parts[1])
        except ValueError:
            response = "Dùng: /resolvewarranty ID | Nội dung phản hồi (1-1000 ký tự)."
        except NotFound:
            response = "Không tìm thấy yêu cầu bảo hành."
        except WarrantyClosed:
            response = "Yêu cầu này đã được phản hồi."
        else:
            self.audit_admin_action(
                update.effective_user.id, "resolve_warranty", "warranty", ticket_id,
            )
            try:
                language = self.store.get_user_language(result["user_id"])
                await context.bot.send_message(
                    result["user_id"],
                    tr(language,
                       f"🛡 Shop đã phản hồi bảo hành #{ticket_id} cho đơn #{result['order_id']}:\n"
                       f"{result['reply']}",
                       f"🛡 The shop replied to warranty #{ticket_id} for order #{result['order_id']}:\n"
                       f"{result['reply']}"),
                    protect_content=True,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(tr(language, "Xem đơn", "View order"),
                                             callback_data=f"order:{result['order_id']}")
                    ]]),
                )
                response = f"Đã phản hồi bảo hành #{ticket_id} và gửi cho khách."
            except TelegramError:
                log.warning("Could not notify user %s of warranty reply", result["user_id"])
                response = (
                    f"Đã lưu phản hồi bảo hành #{ticket_id} nhưng chưa gửi được tin nhắn cho khách. "
                    "Khách vẫn xem được phản hồi trong đơn hàng."
                )
        if not deleted:
            response += " Hãy tự xóa tin nhắn chứa phản hồi vì bot không xóa được."
        await context.bot.send_message(update.effective_user.id, response)

    async def admin_archived(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.send_admin_archived(update.message)

    async def admin_product_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            product_id = self.admin_product_id(context.args[0])
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /productinfo #ID")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        await self.send_admin_product(update.message, product_id)

    async def admin_post_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            product_id = self.admin_product_id(context.args[0])
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /postproduct #ID_sản_phẩm")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        await self.send_admin_product_post(update.message, product_id)

    async def admin_inventory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            product_id = self.admin_product_id(context.args[0])
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /inventory #ID_sản_phẩm")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        await self.send_admin_inventory(update.message, product_id)

    async def admin_stock_item(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            stock_id = int(context.args[0])
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /stockitem ID_kho")
            return
        await self.send_admin_stock_item(update.message, stock_id)

    async def add_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        parts = update.message.text.partition(" ")[2].split("|", 2)
        if len(parts) < 2:
            await update.message.reply_text("Dùng: /addproduct Tên | Giá | Mô tả")
            return
        try:
            name = parts[0].strip()
            price = parse_amount(parts[1])
            description = parts[2].strip() if len(parts) > 2 else ""
            product_id = self.store.add_product(name, price, description)
        except ValueError:
            await update.message.reply_text(
                "Tên, giá hoặc mô tả không hợp lệ. Giá nhập số VND, ví dụ 10000 hoặc 10k."
            )
            return
        display_id = self.store.get_product(product_id)["display_id"]
        await update.message.reply_text(
            f"Đã tạo sản phẩm #{display_id}: {name} — {vnd(price)} "
            f"\nChưa thông báo cho khách. Nhập kho bằng /bulkstock #{display_id}; "
            "bot sẽ báo khách sau khi nhập thành công.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Xem sản phẩm", callback_data=f"admin:product:{product_id}")
            ]]),
        )

    async def add_coupon(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        parts = [part.strip() for part in update.message.text.partition(" ")[2].split("|")]
        if not 3 <= len(parts) <= 5:
            await update.message.reply_text(
                "Dùng: /addcoupon CODE | 10% hoặc 10000 | Số lượt | #ID_sản_phẩm\n"
                "Có hạn: /addcoupon CODE | 10% | Số lượt | YYYY-MM-DD | #ID_sản_phẩm\n"
                "Bỏ ID để mã áp dụng toàn shop."
            )
            return
        try:
            if parts[1].endswith("%"):
                kind, value = "percent", int(parts[1][:-1])
            else:
                kind, value = "fixed", parse_amount(parts[1])
            max_uses = int(parts[2])
            expires_on = None
            product_id = None
            if len(parts) == 4:
                if parts[3].isdigit():
                    product_id = self.admin_product_id(parts[3])
                elif parts[3].startswith("#"):
                    product_id = self.admin_product_id(parts[3])
                elif parts[3] != "-":
                    expires_on = parts[3]
            elif len(parts) == 5:
                expires_on = parts[3] if parts[3] != "-" else None
                product_id = self.admin_product_id(parts[4])
            code = self.store.create_coupon(
                parts[0], kind, value, max_uses, expires_on, product_id,
            )
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy ID sản phẩm này. Dùng /products để xem ID.")
            return
        except ValueError:
            await update.message.reply_text(
                "Mã đã tồn tại hoặc dữ liệu không hợp lệ. Mã 4-24 ký tự A-Z, 0-9, _ hoặc -; "
                "giảm % từ 1-99; số lượt từ 1-100000; ngày dạng YYYY-MM-DD; "
                "ID sản phẩm phải là số dương."
            )
            return
        await update.message.reply_text(
            f"Đã tạo mã {code} cho "
            f"{'sản phẩm #' + str(self.store.get_product(product_id)['display_id']) if product_id else 'toàn shop'}. "
            f"Mỗi khách dùng tối đa một lần; tổng cộng tối đa {max_uses} lượt."
        )

    async def admin_coupons(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("admin_offer_flow", None)
            await self.send_admin_coupons(update.message)

    async def set_coupon_visibility(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        active = update.message.text.split()[0].split("@")[0].lower() == "/enablecoupon"
        try:
            code = context.args[0].strip().upper()
            self.store.set_coupon_active(code, active)
        except IndexError:
            await update.message.reply_text("Dùng: /disablecoupon CODE hoặc /enablecoupon CODE")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy mã giảm giá.")
            return
        await update.message.reply_text(f"Mã {code} đã {'bật' if active else 'tắt'}.")

    async def add_bundle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        parts = [part.strip() for part in update.message.text.partition(" ")[2].split("|", 3)]
        if len(parts) < 3:
            await update.message.reply_text(
                "Dùng: /addbundle Tên gói | Giá gói | #IDxSL,#IDxSL | Mô tả\n"
                "Ví dụ: /addbundle Combo 2 loại | 80k | #1x1,#2x1 | Giao 2 tài khoản"
            )
            return
        try:
            items = {}
            for entry in parts[2].split(","):
                match = re.fullmatch(r"\s*(#?[0-9]+)\s*[xX*]\s*([0-9]+)\s*", entry)
                if not match:
                    raise ValueError("Invalid item")
                product_id = self.admin_product_id(match.group(1))
                quantity = int(match.group(2))
                if product_id in items:
                    raise ValueError("Duplicate product")
                items[product_id] = quantity
            price = parse_amount(parts[1])
            bundle_id = self.store.create_bundle(
                parts[0], price, items, parts[3] if len(parts) > 3 else "",
            )
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except ValueError:
            await update.message.reply_text(
                "Gói không hợp lệ: cần 2-10 tài khoản, IDxSL cách nhau bằng dấu phẩy; "
                "giá gói phải thấp hơn tổng giá lẻ và đủ ít nhất 1đ mỗi tài khoản."
            )
            return
        except NotFound:
            await update.message.reply_text("Có sản phẩm không tồn tại hoặc đang ẩn/lưu trữ.")
            return
        await update.message.reply_text(
            f"Đã tạo gói #{bundle_id}: {parts[0]} — {vnd(price)}. "
            "Gói tự dùng kho của các sản phẩm trong gói.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Xem gói", callback_data=f"admin:bundle:{bundle_id}")
            ]]),
        )

    async def admin_bundles(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("admin_offer_flow", None)
            await self.send_admin_bundles(update.message)

    async def edit_bundle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        parts = [part.strip() for part in update.message.text.partition(" ")[2].split("|", 3)]
        if len(parts) < 3:
            await update.message.reply_text(
                "Dùng: /editbundle ID | Tên mới hoặc - | Giá mới hoặc - | Mô tả mới hoặc -"
            )
            return
        try:
            bundle_id = int(parts[0])
            name = None if parts[1] == "-" else parts[1]
            price = None if parts[2] == "-" else parse_amount(parts[2])
            description = None if len(parts) < 4 or parts[3] == "-" else parts[3]
            self.store.update_bundle(bundle_id, name, price, description)
        except ValueError:
            await update.message.reply_text("ID, tên, giá hoặc mô tả không hợp lệ.")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy gói.")
            return
        await update.message.reply_text(f"Đã cập nhật gói #{bundle_id}.")

    async def set_bundle_visibility(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        active = update.message.text.split()[0].split("@")[0].lower() == "/showbundle"
        try:
            bundle_id = int(context.args[0])
            self.store.set_bundle_active(bundle_id, active)
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /hidebundle ID hoặc /showbundle ID")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy gói.")
            return
        await update.message.reply_text(f"Gói #{bundle_id} đã {'hiện' if active else 'ẩn'}.")

    async def set_bundle_deleted(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        deleted = update.message.text.split()[0].split("@")[0].lower() == "/deletebundle"
        try:
            bundle_id = int(context.args[0])
            self.store.set_bundle_deleted(bundle_id, deleted)
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /deletebundle ID hoặc /restorebundle ID")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy gói.")
            return
        await update.message.reply_text(
            f"Gói #{bundle_id} đã {'lưu trữ' if deleted else 'khôi phục ở trạng thái ẩn'}."
        )

    async def edit_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        parts = update.message.text.partition(" ")[2].split("|", 2)
        if len(parts) < 2:
            await update.message.reply_text(
                "Dùng: /editproduct ID | Tên mới | Mô tả mới\n"
                "Nhập - thay tên để giữ tên cũ; bỏ phần mô tả để giữ mô tả cũ."
            )
            return
        try:
            product_id = self.admin_product_id(parts[0].strip())
            name = parts[1].strip()
            description = parts[2].strip() if len(parts) > 2 else None
            product = self.store.update_product_details(
                product_id, None if name == "-" else name, description,
            )
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except ValueError:
            await update.message.reply_text("ID, tên hoặc mô tả không hợp lệ.")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        await update.message.reply_text(
            f"Đã cập nhật sản phẩm #{self.store.get_product(product_id)['display_id']}: "
            f"{product['name']}\n"
            f"Mô tả: {product['description'] or '(trống)'}"
        )

    async def add_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Dùng: /stock #ID Dữ liệu tài khoản")
            return
        deleted = await self.delete_private_input(update.message)
        try:
            product_id = self.admin_product_id(parts[1])
            item_id = self.store.add_stock(product_id, parts[2].strip())
        except AmbiguousProductReference as exc:
            response = str(exc)
        except ValueError:
            response = "ID hoặc dữ liệu tài khoản không hợp lệ."
        except NotFound:
            response = "Không tìm thấy sản phẩm."
        except DuplicateStock:
            response = "Tài khoản này đã có trong kho."
        else:
            response = f"Đã thêm tài khoản vào kho, mã kho #{item_id}."
        if not deleted:
            response += " Hãy tự xóa tin nhắn chứa tài khoản vì bot không xóa được."
        await context.bot.send_message(
            chat_id=update.effective_user.id, text=response,
        )

    async def bulk_stock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        context.user_data.pop("admin_product_flow", None)
        context.user_data.pop("awaiting_bulk_stock", None)
        context.user_data.pop("awaiting_file_stock", None)
        parts = update.message.text.split(maxsplit=2)
        try:
            product_id = self.admin_product_id(parts[1])
        except AmbiguousProductReference as exc:
            if len(parts) > 2:
                await self.delete_private_input(update.message)
            await context.bot.send_message(update.effective_user.id, str(exc))
            return
        except NotFound:
            if len(parts) > 2:
                await self.delete_private_input(update.message)
            await context.bot.send_message(update.effective_user.id, "Không tìm thấy sản phẩm.")
            return
        except (IndexError, ValueError):
            if len(parts) > 2:
                await self.delete_private_input(update.message)
            await context.bot.send_message(update.effective_user.id, "Dùng: /bulkstock #ID_sản_phẩm")
            return
        product = self.store.get_product(product_id)
        if not product or product["deleted"]:
            if len(parts) > 2:
                await self.delete_private_input(update.message)
            await context.bot.send_message(update.effective_user.id, "Không tìm thấy sản phẩm.")
            return
        if product["delivery_type"] == "file":
            if len(parts) > 2:
                await self.delete_private_input(update.message)
            await context.bot.send_message(
                update.effective_user.id,
                "Sản phẩm này giao bằng file. Mở /admin > Sản phẩm > chọn sản phẩm > Nhập file .json.",
            )
            return
        context.user_data.pop("awaiting_topup", None)
        context.user_data.pop("awaiting_warranty", None)
        context.user_data["awaiting_bulk_stock"] = product_id
        if len(parts) > 2:
            await self.save_bulk_stock(update, context, product_id, parts[2])
        else:
            await update.message.reply_text(
                f"Gửi tài khoản cho sản phẩm #{product['display_id']}, mỗi dòng một tài khoản "
                "(tối đa 100 dòng). Dòng trùng vẫn được nhập và báo sau. "
                "Bot sẽ xóa tin nhắn nhập kho. Gửi /cancel để hủy."
            )

    async def admin_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("admin_product_flow", None)
            await self.send_admin_products(update.message)

    async def change_product_display_id(self, message, product_id: int,
                                        value: str, context=None):
        try:
            display_id = int(value)
            result = self.store.set_product_display_id(product_id, display_id)
        except ValueError:
            await message.reply_text("Số hiển thị phải là số nguyên từ 1 đến 999999.")
            return
        except NotFound:
            if context:
                context.user_data.pop("awaiting_display_id", None)
            await message.reply_text("Không tìm thấy sản phẩm.")
            return
        if context:
            context.user_data.pop("awaiting_display_id", None)
        product = self.store.get_product(product_id)
        swapped = (self.store.get_product(result["swapped_product_id"])
                   if result["swapped_product_id"] else None)
        swap = (f" Sản phẩm {swapped['name']} chuyển sang "
                f"#{swapped['display_id']}." if swapped else "")
        await message.reply_text(
            f"Sản phẩm {product['name']} giờ hiển thị là "
            f"#{result['display_id']}.{swap} Khách sẽ thấy số mới và thứ tự "
            "tăng dần khi mở lại danh sách. Bấm các nút bên dưới để tiếp tục quản lý."
        )
        await self.send_admin_product(message, product_id)

    async def admin_set_product_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if len(context.args) != 2:
            await update.message.reply_text("Dùng: /setproductid #SỐ_HIỆN_TẠI SỐ_MỚI")
            return
        try:
            product_id = self.admin_product_id(context.args[0])
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except ValueError:
            await update.message.reply_text("Số sản phẩm phải là số nguyên, ví dụ #1.")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        context.user_data.pop("awaiting_display_id", None)
        await self.change_product_display_id(update.message, product_id, context.args[1])

    async def admin_move_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if len(context.args) != 2:
            await update.message.reply_text("Dùng: /moveproduct #SỐ_SẢN_PHẨM VỊ_TRÍ")
            return
        try:
            product_id = self.admin_product_id(context.args[0])
            position = int(context.args[1])
            result = self.store.move_product(product_id, position)
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except ValueError:
            await update.message.reply_text(
                "Số sản phẩm và vị trí phải là số nguyên; vị trí tính từ 1 "
                "trong danh sách /products."
            )
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm đang quản lý.")
            return
        await update.message.reply_text(
            f"Đã chuyển sản phẩm #{self.store.get_product(product_id)['display_id']} "
            f"đến vị trí {result['position']}/{result['count']} trong danh sách quản trị. "
            "Danh sách khách được xếp theo số hiển thị."
        )
        await self.send_admin_products(update.message)

    async def delete_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            product_id = self.admin_product_id(context.args[0])
            self.store.set_deleted(product_id, True)
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /deleteproduct #ID")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        await update.message.reply_text(
            f"Đã lưu trữ sản phẩm #{self.store.get_product(product_id)['display_id']}. "
            "Sản phẩm không còn bán; đơn hàng và kho vẫn được giữ. "
            f"Dùng /restoreproduct #{self.store.get_product(product_id)['display_id']} để khôi phục."
        )

    async def restore_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            product_id = self.admin_product_id(context.args[0])
            product = self.store.get_product(product_id)
            if not product or not product["deleted"]:
                raise NotFound("Product not archived")
            self.store.set_deleted(product_id, False)
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /restoreproduct #ID")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm đã lưu trữ.")
            return
        await update.message.reply_text(
            f"Đã khôi phục sản phẩm #{product['display_id']} ở trạng thái ẩn. "
            f"Dùng /show #{product['display_id']} khi sẵn sàng bán."
        )

    async def start_sale(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        if len(context.args) != 3:
            await update.message.reply_text(
                "Dùng: /sale #ID Giá_sale Số_suất. Ví dụ: /sale #2 19000 40"
            )
            return
        try:
            product_id = self.admin_product_id(context.args[0])
            sale_price = parse_amount(context.args[1])
            max_units = int(context.args[2])
            sale = self.store.start_sale(
                product_id, sale_price, max_units,
                announce_except=self.config.admin_ids,
            )
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except SaleActive:
            await update.message.reply_text(
                "Sản phẩm đang có sale. Dùng /endsale #ID trước khi tạo sale mới."
            )
            return
        except NotFound:
            await update.message.reply_text("Sản phẩm không còn bán hoặc không tìm thấy.")
            return
        except OutOfStock:
            await update.message.reply_text("Sản phẩm hết kho. Hãy nhập tài khoản trước khi tạo sale.")
            return
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Sale không hợp lệ: giá sale phải thấp hơn giá gốc và số suất "
                "không vượt quá tồn kho. Ví dụ: /sale #2 19000 40"
            )
            return
        await update.message.reply_text(
            f"Đã bắt đầu sale sản phẩm #{self.store.get_product(product_id)['display_id']}: "
            f"{vnd(sale['original_price'])} → {vnd(sale['sale_price'])} "
            f"(-{sale_percent(sale['original_price'], sale['sale_price'])}), "
            f"{max_units} suất, kho hiện có {sale['available']}. "
                f"Đã xếp thông báo cho {sale['recipients']} khách theo dõi sản phẩm. "
            f"Dùng /endsale #{self.store.get_product(product_id)['display_id']} để kết thúc."
        )

    async def end_sale(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            product_id = self.admin_product_id(context.args[0])
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /endsale #ID_sản_phẩm")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        try:
            self.store.end_sale(product_id)
        except NotFound:
            await update.message.reply_text("Sản phẩm không có sale đang chạy.")
            return
        await update.message.reply_text(
            f"Đã kết thúc sale sản phẩm #{self.store.get_product(product_id)['display_id']}. "
            "Giá thường đã được áp dụng lại; "
            "thông báo chưa gửi sẽ được hủy."
        )

    async def set_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        try:
            product_id = self.admin_product_id(context.args[0])
            price = parse_amount(context.args[1])
            self.store.set_price(product_id, price)
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /setprice #ID Giá")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        except SaleActive:
            await update.message.reply_text("Sản phẩm đang sale. Dùng /endsale #ID trước khi đổi giá gốc.")
            return
        product = self.store.get_product(product_id)
        await update.message.reply_text(
            f"Đã đổi giá sản phẩm #{product['display_id']} {product['name']} thành {vnd(price)}."
        )

    async def set_visibility(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.admin_allowed(update):
            return
        active = update.message.text.split()[0].split("@")[0].lower() == "/show"
        try:
            product_id = self.admin_product_id(context.args[0])
            self.store.set_active(product_id, active)
        except AmbiguousProductReference as exc:
            await update.message.reply_text(str(exc))
            return
        except (IndexError, ValueError):
            await update.message.reply_text("Dùng: /hide #ID hoặc /show #ID")
            return
        except NotFound:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return
        await update.message.reply_text(
            f"Sản phẩm #{self.store.get_product(product_id)['display_id']} "
            f"{'đã hiện' if active else 'đã ẩn'}."
        )

    async def send_admin_unmatched(self, message, page: int = 0):
        events = self.store.list_unmatched_events(PAGE_SIZE + 1, page * PAGE_SIZE)
        visible = events[:PAGE_SIZE]
        lines = ["⚠️ GIAO DỊCH CHƯA KHỚP"]
        rows = []
        for event in visible:
            content = " ".join(event["content"].split())[:100]
            lines.append(
                f"#{event['id']} · {event['provider_txn_id']} · {vnd(event['amount'])} "
                f"· {event['created_at'][:16]} UTC\n{content}"
            )
            rows.append([InlineKeyboardButton(
                f"Xử lý #{event['id']}", callback_data=f"admin:match:{event['id']}"
            )])
        if not visible:
            lines.append("Không có giao dịch chưa khớp ở trang này.")
        rows.extend(admin_footer("admin:unmatched", page, len(events) > PAGE_SIZE))
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def send_admin_match_detail(self, message, context, event_id: int):
        event = self.store.get_unmatched_event(event_id)
        if not event:
            await message.reply_text("Giao dịch không còn ở trạng thái chưa khớp. Mở /unmatched để xem lại.")
            return
        context.user_data.pop("pending_manual_credit", None)
        context.user_data["awaiting_manual_credit"] = event_id
        await message.reply_text(
            f"⚠️ GIAO DỊCH #{event_id}\n"
            f"Mã ngân hàng: {event['provider_txn_id']}\n"
            f"Tiền thực nhận: {vnd(event['amount'])}\n"
            f"Tài khoản nhận: {event['bank_account']}\n"
            f"Nội dung: {event['content'][:500]}\n\n"
            "Hãy kiểm tra với khách rồi gửi đúng mã nạp NAP... do bot cấp. "
            "Bot sẽ hiện thông tin khách và yêu cầu xác nhận trước khi cộng tiền. Gửi /cancel để hủy."
        )

    async def prepare_manual_credit(self, message, context, event_id: int):
        try:
            preview = self.store.preview_manual_credit(
                event_id, message.text, self.config.bank_account_number,
            )
        except ValueError:
            await message.reply_text("Mã nạp cần có dạng NAP và 12 ký tự hex. Gửi lại mã hoặc /cancel.")
            return
        except NotFound:
            await message.reply_text("Không tìm thấy mã nạp này. Kiểm tra với khách rồi gửi lại hoặc /cancel.")
            return
        except ManualCreditUnavailable:
            context.user_data.pop("awaiting_manual_credit", None)
            await message.reply_text("Giao dịch đã đổi trạng thái hoặc không phải tiền vào đúng tài khoản.")
            return
        context.user_data.pop("awaiting_manual_credit", None)
        nonce = secrets.token_hex(8)
        context.user_data["pending_manual_credit"] = (
            nonce, preview["event_id"], preview["topup_id"],
            preview["amount"], time.monotonic() + 300,
        )
        customer = user_label(
            preview["user_id"], preview["username"], preview["display_name"],
        )
        await message.reply_text(
            f"XÁC NHẬN CỘNG TIỀN\n"
            f"Giao dịch: #{preview['event_id']} · {preview['transaction_id']}\n"
            f"Tiền thực nhận sẽ cộng: {vnd(preview['amount'])}\n"
            f"Nội dung chuyển khoản: {preview['content'][:300]}\n"
            f"Khách nhận tiền: {customer}\n"
            f"Mã nạp: {preview['code']} (yêu cầu {vnd(preview['requested_amount'])}, "
            f"đã nhận {vnd(preview['paid_amount'])})\n"
            f"Số dư hiện tại: {vnd(preview['balance'])}\n\n"
            "Chỉ bấm xác nhận sau khi đối chiếu giao dịch và đúng khách. "
            "Nút hết hạn sau 5 phút.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Xác nhận cộng tiền",
                    callback_data=f"admin:credit:{preview['event_id']}:{nonce}",
                ),
                InlineKeyboardButton("Hủy", callback_data="admin:matchcancel"),
            ]]),
        )

    async def confirm_manual_credit(self, message, context, admin_id: int,
                                    event_text: str, nonce: str):
        pending = context.user_data.get("pending_manual_credit")
        if (not pending or not event_text.isascii() or not event_text.isdigit()
                or pending[0] != nonce or pending[1] != int(event_text)
                or time.monotonic() > pending[4]):
            context.user_data.pop("pending_manual_credit", None)
            await message.reply_text("Xác nhận đã hết hạn hoặc không hợp lệ. Mở /unmatched để làm lại.")
            return
        context.user_data.pop("pending_manual_credit", None)
        try:
            result = self.store.resolve_manual_credit(
                pending[1], pending[2], pending[3], self.config.bank_account_number,
                admin_id,
            )
        except ManualCreditUnavailable:
            await message.reply_text("Giao dịch đã được xử lý hoặc thay đổi. Không cộng tiền lần nữa.")
            return
        except Exception:
            log.exception("Manual top-up credit failed")
            await message.reply_text("Chưa cộng tiền vì có lỗi lưu dữ liệu. Kiểm tra /unmatched và thử lại.")
            return
        try:
            await self.notify_payment(result)
        except Exception:
            log.exception("Could not notify customer of manual credit")
        self.audit_admin_action(
            admin_id, "resolve_manual_credit", "bank_event", pending[1],
            f"user_id={result['user_id']}; amount={result['amount']}",
        )
        await message.reply_text(
            f"Đã cộng {vnd(result['amount'])} cho khách ID {result['user_id']} "
            f"từ giao dịch {result['transaction_id']}. "
            f"Số dư mới: {vnd(result['balance'])}. Xem /manualcredits để kiểm tra lịch sử."
        )

    async def send_admin_manual_credits(self, message, page: int = 0):
        credits = self.store.list_manual_credits(PAGE_SIZE + 1, page * PAGE_SIZE)
        visible = credits[:PAGE_SIZE]
        lines = ["🧾 GIAO DỊCH ĐÃ XỬ LÝ TAY"]
        for credit in visible:
            customer = user_label(
                credit["user_id"], credit["username"], credit["display_name"],
            )
            lines.append(
                f"#{credit['bank_event_id']} · {credit['provider_txn_id']} "
                f"· {vnd(credit['amount'])}\n"
                f"{customer} · {credit['code']} · admin {credit['admin_id']} "
                f"· {credit['created_at'][:16]} UTC"
            )
        if not visible:
            lines.append("Chưa có giao dịch xử lý tay ở trang này.")
        rows = admin_footer("admin:manualcredits", page, len(credits) > PAGE_SIZE)
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def unmatched(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("awaiting_manual_credit", None)
            context.user_data.pop("pending_manual_credit", None)
            await self.send_admin_unmatched(update.message)

    async def manual_credits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            context.user_data.pop("awaiting_manual_credit", None)
            context.user_data.pop("pending_manual_credit", None)
            await self.send_admin_manual_credits(update.message)

    async def reconcile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.run_reconciliation(update.message)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self.admin_allowed(update):
            await self.send_admin_status(update.message)

    async def _notify_reconciliation_admin(self, text: str):
        for admin_id in self.config.admin_ids:
            try:
                await self.application.bot.send_message(admin_id, text)
            except TelegramError:
                log.warning("Could not notify admin %s about reconciliation", admin_id)

    async def _reconciliation_failed(self, reason: str, message=None):
        log.warning("SePay reconciliation failed: %s", reason)
        text = f"Đối soát SePay lỗi: {reason} Bot sẽ thử lại; có thể dùng /reconcile."
        if message:
            await message.reply_text(text)
        elif time.monotonic() - self.last_reconciliation_alert >= 3600:
            self.last_reconciliation_alert = time.monotonic()
            await self._notify_reconciliation_admin(text)

    async def run_reconciliation(self, message=None):
        if not self.config.sepay_api_token:
            if message:
                await message.reply_text(
                    "Chưa cấu hình SEPAY_API_TOKEN trong .env. Tạo Token ở SePay "
                    "→ Cấu hình Công ty → API Access, rồi khởi động lại bot."
                )
            return
        if self.reconciliation_lock.locked():
            if message:
                await message.reply_text("Đối soát đang chạy. Vui lòng chờ hoàn tất.")
            return
        async with self.reconciliation_lock:
            try:
                report = await asyncio.to_thread(
                    reconcile_once, self.store, self.config.sepay_api_token,
                    self.config.bank_account_number,
                    notify_admin_ids=self.config.admin_ids,
                )
            except ReconciliationError as exc:
                await self._reconciliation_failed(str(exc), message)
                return
            except Exception:
                log.exception("Unexpected SePay reconciliation failure")
                await self._reconciliation_failed("Lỗi nội bộ khi xử lý giao dịch.", message)
                return
        for event in report["credited_events"]:
            await self.notify_payment(event)
        if report["unmatched"] and not message:
            await self._notify_reconciliation_admin(
                f"Đối soát thấy {report['unmatched']} giao dịch chưa khớp mã nạp. Xem /unmatched."
            )
        if message:
            state = self.store.get_reconciliation_state(self.config.bank_account_number)
            await message.reply_text(
                f"Đối soát SePay xong: lấy {report['fetched']} giao dịch; "
                f"mới cộng {report['credited']}; đã xử lý trước {report['duplicate']}; "
                f"chưa khớp {report['unmatched']}.\n"
                f"ID cuối: {report['cursor']}; hoàn tất lúc {state['last_success_at']} UTC."
            )

    async def reconciliation_loop(self):
        await asyncio.sleep(10)
        while True:
            await self.run_reconciliation()
            await asyncio.sleep(120)

    async def post_init(self, application: Application):
        self.application = application
        self.loop = asyncio.get_running_loop()
        self.bot_username = getattr(application.bot, "username", None)
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("menu", "Mở menu chính"),
                    BotCommand("shop", "Xem sản phẩm"),
                    BotCommand("payments", "Đơn chuyển khoản"),
                    BotCommand("following", "Sản phẩm đang theo dõi"),
                    BotCommand("bundles", "Xem gói ưu đãi"),
                    BotCommand("nap", "Nạp tiền qua ngân hàng"),
                    BotCommand("balance", "Xem số dư"),
                    BotCommand("orders", "Xem đơn hàng"),
                    BotCommand("deposits", "Xem lịch sử nạp"),
                    BotCommand("stocknotify", "Bật/tắt thông báo kho"),
                    BotCommand("language", "Chọn Tiếng Việt / English"),
                    BotCommand("help", "Hướng dẫn sử dụng"),
                ],
                scope=BotCommandScopeAllPrivateChats(),
            )
        except TelegramError:
            log.warning("Could not update Telegram command menu")
        self.server, self.server_thread = start_webhook_server(
            self.config, self.store, self.schedule_payment_notification,
            self.schedule_webhook_alert,
        )
        log.info("SePay webhook listening on %s:%s", self.config.webhook_host, self.config.webhook_port)
        self.inventory_alert_task = asyncio.create_task(self.inventory_alert_loop())
        self.stock_announcement_task = asyncio.create_task(self.stock_announcement_loop())
        self.purchase_notification_task = asyncio.create_task(self.purchase_notification_loop())
        self.direct_delivery_task = asyncio.create_task(self.direct_delivery_loop())
        self.new_customer_notification_task = asyncio.create_task(
            self.new_customer_notification_loop()
        )
        self.email_completion_task = asyncio.create_task(
            self.email_completion_notification_loop()
        )
        self.email_reminder_task = asyncio.create_task(self.email_reminder_loop())
        self.warranty_reminder_task = asyncio.create_task(self.warranty_reminder_loop())
        if self.config.sepay_api_token:
            self.reconciliation_task = asyncio.create_task(self.reconciliation_loop())
            log.info("SePay reconciliation scheduled every 120 seconds")
        else:
            log.info("SePay reconciliation waiting for SEPAY_API_TOKEN")

    async def post_shutdown(self, application: Application):
        if self.direct_delivery_task:
            self.direct_delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.direct_delivery_task
        if self.warranty_reminder_task:
            self.warranty_reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.warranty_reminder_task
        if self.email_reminder_task:
            self.email_reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.email_reminder_task
        if self.email_completion_task:
            self.email_completion_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.email_completion_task
        if self.new_customer_notification_task:
            self.new_customer_notification_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.new_customer_notification_task
        if self.purchase_notification_task:
            self.purchase_notification_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.purchase_notification_task
        if self.stock_announcement_task:
            self.stock_announcement_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stock_announcement_task
        if self.inventory_alert_task:
            self.inventory_alert_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.inventory_alert_task
        if self.reconciliation_task:
            self.reconciliation_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reconciliation_task
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=3)

    def schedule_payment_notification(self, result: dict):
        future = asyncio.run_coroutine_threadsafe(self.notify_payment(result), self.loop)
        future.add_done_callback(self._notification_done)

    @staticmethod
    def _notification_done(future):
        try:
            future.result()
        except Exception:
            log.exception("Payment notification failed")

    async def notify_payment(self, result: dict):
        if result["status"] == "credited":
            if result.get("direct_checkout_id"):
                user_id = result["user_id"]
                language = self.store.get_user_language(user_id)
                checkout_id = result["direct_checkout_id"]
                direct_status = result["direct_status"]
                if direct_status == "fulfilled":
                    await self.send_pending_direct_checkout_orders(self.application.bot)
                    return
                if direct_status == "partial":
                    remaining = max(0, result["expected_amount"] - result["paid_amount"])
                    content = tr(language,
                        f"Đơn #{checkout_id} đã nhận {vnd(result['paid_amount'])}. "
                        f"Cần chuyển thêm {vnd(remaining)} với cùng nội dung {result['code']} "
                        "trước hạn để bot giao hàng. Tiền đã nhận đang ở số dư của bạn.",
                        f"Order #{checkout_id} received {vnd(result['paid_amount'])}. "
                        f"Transfer {vnd(remaining)} more with the same note {result['code']} "
                        "before the deadline. Received funds are in your balance.")
                elif direct_status in ("expired", "failed"):
                    reason = (tr(language, "đơn đã hết hạn", "the order expired")
                              if direct_status == "expired" else
                              tr(language, "giao dịch được đối soát thủ công",
                                 "the transfer was matched manually")
                              if result.get("failure_reason") == "ManualCredit" else
                              tr(language, "giá, kho hoặc mã giảm giá đã thay đổi",
                                 "price, stock, or coupon changed"))
                    content = tr(language,
                        f"Đơn chuyển khoản #{checkout_id} chưa thể giao vì {reason}. "
                        f"Toàn bộ tiền đã nhận được giữ trong số dư: {vnd(result['balance'])}. "
                        "Bạn có thể chọn mua lại bằng ví hoặc liên hệ chủ shop.",
                        f"Bank order #{checkout_id} could not be delivered because {reason}. "
                        f"Received funds are in your balance: {vnd(result['balance'])}. "
                        "You can buy again with your balance or contact the shop.")
                else:
                    content = tr(language,
                        f"Đơn chuyển khoản #{checkout_id} đã xử lý trước đó. "
                        f"Khoản chuyển thêm {vnd(result['amount'])} được cộng vào ví; "
                        f"số dư hiện tại: {vnd(result['balance'])}.",
                        f"Bank order #{checkout_id} was already processed. "
                        f"The extra {vnd(result['amount'])} was added to your balance; "
                        f"current balance: {vnd(result['balance'])}.")
                try:
                    await self.application.bot.send_message(
                        user_id, content,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(tr(language, "Xem đơn", "View order"),
                                                 callback_data=f"cart:checkout:{checkout_id}"),
                            InlineKeyboardButton(tr(language, "🏦 Đơn chuyển khoản", "🏦 Bank orders"),
                                                 callback_data="payments"),
                        ]]),
                    )
                except TelegramError:
                    log.warning("Could not notify user %s of direct checkout", user_id)
                return
            try:
                language = self.store.get_user_language(result["user_id"])
                await self.application.bot.send_message(
                    result["user_id"],
                    tr(language,
                       f"Nạp tiền thành công: +{vnd(result['amount'])}\n"
                       f"Mã: {result['code']}\nSố dư mới: {vnd(result['balance'])}",
                       f"Top-up successful: +{vnd(result['amount'])}\n"
                       f"Code: {result['code']}\nNew balance: {vnd(result['balance'])}"),
                    reply_markup=menu_keyboard(language),
                )
            except TelegramError:
                log.warning("Could not notify user %s of top-up", result["user_id"])
        elif result["status"] == "unmatched":
            for admin_id in self.config.admin_ids:
                try:
                    await self.application.bot.send_message(
                        admin_id,
                        f"Giao dịch {result['transaction_id']} ({vnd(result['amount'])}) "
                        "chưa khớp mã nạp. Xem /unmatched.",
                    )
                except TelegramError:
                    log.warning("Could not notify admin %s of unmatched transfer", admin_id)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        log.error("Telegram update failed: %s", type(context.error).__name__)
        if isinstance(update, Update) and update.effective_message and self.is_private(update):
            try:
                language = self.store.get_user_language(update.effective_user.id)
                await update.effective_message.reply_text(tr(
                    language, "Có lỗi xảy ra. Vui lòng thử lại sau.",
                    "Something went wrong. Please try again later.",
                ))
            except TelegramError:
                pass


def main():
    config = load_config()
    store = Store(config.database_path, config.inventory_encryption_key)
    shop = ShopBot(config, store)
    app = ApplicationBuilder().token(config.bot_token).post_init(shop.post_init).post_shutdown(shop.post_shutdown).build()
    app.add_handler(TypeHandler(Update, shop.spam_guard), group=-1)
    for command, handler in [
        ("start", shop.start), ("menu", shop.menu), ("help", shop.help),
        ("language", shop.language), ("lang", shop.language), ("id", shop.user_id),
        ("shop", shop.shop), ("payments", shop.payments),
        ("cart", shop.cart), ("bundles", shop.bundles),
        ("following", shop.following),
        ("balance", shop.balance), ("nap", shop.topup),
        ("stocknotify", shop.stock_notify),
        ("cancel", shop.cancel), ("orders", shop.orders),
        ("deposits", shop.deposits), ("admin", shop.admin),
        ("broadcast", shop.broadcast), ("broadcaststatus", shop.broadcast_status),
         ("users", shop.admin_users), ("audit", shop.admin_audit), ("user", shop.admin_user),
        ("banned", shop.admin_bans), ("unban", shop.admin_unban),
        ("sales", shop.admin_sales), ("topups", shop.admin_topups),
        ("pendingemails", shop.admin_pending_emails),
        ("emailremind", shop.admin_email_remind),
        ("orderemail", shop.admin_order_email), ("requireemail", shop.admin_require_email),
        ("emaildone", shop.admin_email_done),
        ("transactions", shop.admin_transactions),
        ("warranties", shop.admin_warranties),
        ("warrantyremind", shop.admin_warranty_remind),
        ("resolvewarranty", shop.admin_resolve_warranty), ("report", shop.admin_report),
        ("funnel", shop.admin_funnel),
        ("export", shop.admin_export),
        ("addproduct", shop.add_product), ("editproduct", shop.edit_product),
        ("setproductid", shop.admin_set_product_id),
        ("moveproduct", shop.admin_move_product),
        ("addcoupon", shop.add_coupon), ("coupons", shop.admin_coupons),
        ("disablecoupon", shop.set_coupon_visibility),
        ("enablecoupon", shop.set_coupon_visibility),
        ("addbundle", shop.add_bundle), ("editbundle", shop.edit_bundle),
        ("adminbundles", shop.admin_bundles),
        ("hidebundle", shop.set_bundle_visibility),
        ("showbundle", shop.set_bundle_visibility),
        ("deletebundle", shop.set_bundle_deleted),
        ("restorebundle", shop.set_bundle_deleted),
        ("stock", shop.add_stock), ("bulkstock", shop.bulk_stock),
        ("products", shop.admin_products), ("setprice", shop.set_price),
        ("sale", shop.start_sale), ("endsale", shop.end_sale),
        ("productinfo", shop.admin_product_info), ("inventory", shop.admin_inventory),
        ("postproduct", shop.admin_post_product),
        ("stockitem", shop.admin_stock_item), ("archived", shop.admin_archived),
        ("deleteproduct", shop.delete_product), ("restoreproduct", shop.restore_product),
        ("hide", shop.set_visibility), ("show", shop.set_visibility),
        ("unmatched", shop.unmatched),
        ("manualcredits", shop.manual_credits),
        ("status", shop.status),
        ("reconcile", shop.reconcile),
    ]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(shop.callbacks))
    app.add_handler(MessageHandler(filters.Document.ALL, shop.document_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, shop.text_message))
    app.add_error_handler(shop.on_error)
    app.run_polling(stop_signals=None, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
