"""Build an admin-only archive of shop accounting CSV files."""

from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile
import csv
import re

from store import SHOP_TIMEZONE, ExportTooLarge, Store


MAX_EXPORT_DAYS = 90
MAX_EXPORT_BYTES = 15_000_000
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def validate_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError("Ngày bắt đầu phải trước hoặc bằng ngày kết thúc.")
    if end == date.max or (end - start).days >= MAX_EXPORT_DAYS:
        raise ValueError(f"Mỗi lần chỉ xuất tối đa {MAX_EXPORT_DAYS} ngày.")


def parse_date_range(value: str) -> tuple[date, date]:
    parts = value.split()
    if len(parts) != 2 or any(not DATE_PATTERN.fullmatch(part) for part in parts):
        raise ValueError("Gửi hai ngày dạng YYYY-MM-DD YYYY-MM-DD, ví dụ 2026-09-01 2026-09-30.")
    try:
        start, end = (date.fromisoformat(part) for part in parts)
    except ValueError as exc:
        raise ValueError("Ngày không hợp lệ. Kiểm tra lại tháng và ngày.") from exc
    validate_range(start, end)
    return start, end


def _local_time(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        SHOP_TIMEZONE,
    ).strftime("%Y-%m-%d %H:%M:%S")


def _safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, str):
        clean = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if clean.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + clean
        return clean
    return value


def _csv_bytes(headers: tuple[str, ...], rows) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(tuple(_safe_cell(value) for value in row) for row in rows)
    return output.getvalue().encode("utf-8-sig")


def build_users_csv(store: Store, admin_ids: frozenset[int]) -> dict:
    rows = store.export_users_rows(admin_ids)
    content = _csv_bytes(
        ("telegram_id", "username", "display_name", "first_seen_gmt7",
         "balance_vnd", "received_vnd", "test_credit_vnd", "spent_vnd", "orders"),
        ((row["telegram_id"], row["username"], row["display_name"],
          _local_time(row["created_at"]), row["balance"], row["received"],
          row["test_credit"], row["spent"], row["orders"]) for row in rows),
    )
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportTooLarge("Customer CSV is too large")
    stamp = datetime.now(SHOP_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return {"filename": f"shop-users-{stamp}.csv", "content": content,
            "users": len(rows)}


def build_sold_products_csv(store: Store) -> dict:
    rows = store.export_sold_product_rows()
    content = _csv_bytes(
        ("sold_at_gmt7", "purchase_id", "telegram_id", "username",
         "display_name", "product_id", "current_display_id", "product_name",
         "quantity", "unit_price_vnd", "total_vnd"),
        ((_local_time(row["created_at"]), row["purchase_id"], row["user_id"],
          row["username"], row["display_name"], row["product_id"],
          row["current_display_id"], row["product_name"], row["quantity"],
          row["unit_price"], row["total_price"]) for row in rows),
    )
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportTooLarge("Sold product CSV is too large")
    stamp = datetime.now(SHOP_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return {"filename": f"shop-sold-products-{stamp}.csv", "content": content,
            "products": len(rows),
            "units": sum(row["quantity"] for row in rows),
            "orders": len({row["purchase_id"] for row in rows})}


def build_admin_audit_csv(store: Store) -> dict:
    rows = store.export_admin_audit_rows()
    content = _csv_bytes(
        ("audit_id", "created_at_gmt7", "admin_id", "admin_username", "admin_name",
         "action", "target_type", "target_id", "details"),
        ((row["id"], _local_time(row["created_at"]), row["admin_id"],
          row["username"], row["display_name"], row["action"], row["target_type"],
          row["target_id"], row["details"]) for row in rows),
    )
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportTooLarge("Audit CSV is too large")
    stamp = datetime.now(SHOP_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return {"filename": f"shop-admin-audit-{stamp}.csv", "content": content,
            "entries": len(rows)}


def build_export_archive(store: Store, start: date, end: date) -> dict:
    validate_range(start, end)
    start_utc = datetime.combine(start, time.min, SHOP_TIMEZONE).astimezone(
        timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%S")
    end_utc = datetime.combine(end + timedelta(days=1), time.min, SHOP_TIMEZONE).astimezone(
        timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%S")
    data = store.export_report_rows(start_utc, end_utc)
    topups, orders, manual = data["topups"], data["orders"], data["manual_credits"]
    topup_total = sum(row["amount"] for row in topups)
    revenue = sum(row["price"] for row in orders)
    order_count = len({row["purchase_id"] for row in orders})

    files = {
        "topups.csv": _csv_bytes(
            ("credited_at_gmt7", "ledger_id", "telegram_id", "username", "display_name",
             "amount_vnd", "bank_event_id", "bank_transaction_id", "topup_code", "credit_method"),
            ((_local_time(row["created_at"]), row["ledger_id"], row["user_id"],
              row["username"], row["display_name"], row["amount"], row["bank_event_id"],
              row["provider_txn_id"], row["code"], row["credit_method"]) for row in topups),
        ),
        "orders.csv": _csv_bytes(
            ("sold_at_gmt7", "order_id", "purchase_id", "telegram_id", "username", "display_name",
             "product_id", "product_name", "amount_vnd", "bundle_order_id",
             "bundle_name", "coupon_code"),
            ((_local_time(row["created_at"]), row["order_id"], row["purchase_id"], row["user_id"],
              row["username"], row["display_name"], row["product_id"],
              row["product_name"], row["price"], row["bundle_order_id"],
              row["bundle_name"], row["coupon_code"]) for row in orders),
        ),
        "manual_credits.csv": _csv_bytes(
            ("handled_at_gmt7", "bank_event_id", "bank_transaction_id", "telegram_id",
             "username", "display_name", "topup_code", "admin_id", "amount_vnd"),
            ((_local_time(row["created_at"]), row["bank_event_id"],
              row["provider_txn_id"], row["user_id"], row["username"],
              row["display_name"], row["code"], row["admin_id"], row["amount"])
             for row in manual),
        ),
    }
    notes = (
        f"Bao cao shop: {start.isoformat()} den {end.isoformat()} (GMT+7, gom ca hai ngay).\n"
        f"Tien nap thuc nhan: {topup_total} VND ({len(topups)} giao dich).\n"
        f"Doanh thu: {revenue} VND ({order_count} don, {len(orders)} mat hang).\n"
        f"Xu ly tay: {len(manual)} giao dich. manual_credits.csv la lich su cua mot phan "
        "topups.csv; KHONG cong lai so tien nay vao tong tien nap.\n"
        "orders.csv co mot dong cho moi tai khoan da ban; cac dong cung purchase_id "
        "thuoc mot don hang. So tien trong CSV la VND nguyen.\n"
        "CSV dung UTF-8 BOM de mo trong Excel tren Windows. Khong co mat khau tai khoan trong bao cao.\n"
    )
    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        for name, content in files.items():
            zip_file.writestr(name, content)
        zip_file.writestr("README.txt", notes.encode("utf-8"))
    content = archive.getvalue()
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportTooLarge("Report archive is too large")
    return {
        "filename": f"shop-report-{start:%Y%m%d}-{end:%Y%m%d}.zip",
        "content": content,
        "topups": len(topups), "topup_total": topup_total,
        "orders": order_count, "items": len(orders), "revenue": revenue,
        "manual_credits": len(manual),
    }
