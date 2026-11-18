"""Recover missed SePay webhooks using the v1 API's matching numeric transaction IDs."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import os
import re
import subprocess

from payments import process_sepay_payload
from store import SHOP_TIMEZONE, Store


SEPAY_TRANSACTIONS_URL = "https://my.sepay.vn/userapi/transactions/list"
PAGE_LIMIT = 5000
MAX_RESPONSE_BYTES = 8_000_000


class ReconciliationError(Exception):
    pass


def _fetch_with_windows_curl(url: str, api_token: str) -> tuple[int, bytes]:
    if "\r" in api_token or "\n" in api_token:
        raise ReconciliationError("SePay API Token không hợp lệ.")
    # Schannel (curl.exe) works with SePay on this Windows host; the Python TLS
    # handshake times out. Pass the token through stdin, never process arguments.
    command = [
        "curl.exe", "--ipv4", "--silent", "--show-error", "--max-time", "15",
        "--connect-timeout", "8", "--header", "@-", "--write-out", "\n%{http_code}",
        "--url", url,
    ]
    try:
        result = subprocess.run(
            command,
            input=("Authorization: Bearer " + api_token.strip()
                   + "\nAccept: application/json\n").encode("utf-8"),
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReconciliationError("Không kết nối được SePay API.") from exc
    if result.returncode:
        raise ReconciliationError("Không kết nối được SePay API.")
    try:
        raw, code = result.stdout.rsplit(b"\n", 1)
        status = int(code)
    except (ValueError, TypeError) as exc:
        raise ReconciliationError("SePay API trả phản hồi HTTP không hợp lệ.") from exc
    return status, raw


def _fetch_response(url: str, api_token: str) -> tuple[int, bytes]:
    if os.name == "nt":
        return _fetch_with_windows_curl(url, api_token)
    request = Request(
        url,
        headers={"Authorization": "Bearer " + api_token.strip(),
                 "Accept": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        return response.status, response.read(MAX_RESPONSE_BYTES + 1)


def fetch_transactions(api_token: str, account_number: str, last_txn_id: int) -> list:
    if not api_token or not account_number:
        raise ReconciliationError("Thiếu SePay API Token hoặc số tài khoản ngân hàng.")
    params = {"account_number": account_number, "limit": PAGE_LIMIT}
    if last_txn_id:
        # Replay a small overlap for transactions SePay may publish out of order.
        params["since_id"] = max(1, last_txn_id - 100)
    else:
        params["transaction_date_min"] = (
            datetime.now(SHOP_TIMEZONE).date() - timedelta(days=30)
        ).isoformat()
    url = SEPAY_TRANSACTIONS_URL + "?" + urlencode(params)
    try:
        status, raw = _fetch_response(url, api_token)
    except HTTPError as exc:
        status, raw = exc.code, b""
    except (URLError, TimeoutError, OSError) as exc:
        raise ReconciliationError("Không kết nối được SePay API.") from exc
    if status in (401, 403):
        raise ReconciliationError("SePay API từ chối Token; kiểm tra API Access.")
    if status == 429:
        raise ReconciliationError("SePay API đang giới hạn lượt gọi; sẽ thử lại sau.")
    if status != 200:
        raise ReconciliationError(f"SePay API trả HTTP {status}.")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReconciliationError("Phản hồi SePay API quá lớn.")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReconciliationError("SePay API trả dữ liệu không phải JSON hợp lệ.") from exc
    if (not isinstance(body, dict) or body.get("status") != 200
            or not isinstance(body.get("messages"), dict)
            or body["messages"].get("success") is not True
            or not isinstance(body.get("transactions"), list)):
        raise ReconciliationError("SePay API trả cấu trúc giao dịch không hợp lệ.")
    return body["transactions"]


def _parse_vnd(value) -> int:
    if type(value) is int:
        amount = value
    elif (isinstance(value, str) and len(value) <= 24
          and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value)):
        try:
            decimal = Decimal(value)
        except InvalidOperation as exc:
            raise ReconciliationError("Số tiền SePay không hợp lệ.") from exc
        if decimal != decimal.to_integral_value():
            raise ReconciliationError("Số tiền SePay không phải VND nguyên.")
        amount = int(decimal)
    else:
        raise ReconciliationError("Số tiền SePay không hợp lệ.")
    if not 0 <= amount <= 1_000_000_000_000:
        raise ReconciliationError("Số tiền SePay ngoài phạm vi hợp lệ.")
    return amount


def normalize_transaction(transaction: dict, expected_account: str) -> dict:
    if not isinstance(transaction, dict):
        raise ReconciliationError("Bản ghi SePay không hợp lệ.")
    raw_id = transaction.get("id")
    if type(raw_id) is int:
        transaction_id = raw_id
    elif (isinstance(raw_id, str) and len(raw_id) <= 19
          and re.fullmatch(r"[1-9][0-9]*", raw_id)):
        transaction_id = int(raw_id)
    else:
        raise ReconciliationError("ID giao dịch SePay không phải số của API v1.")
    if not 0 < transaction_id < 2**63:
        raise ReconciliationError("ID giao dịch SePay ngoài phạm vi hợp lệ.")
    account = transaction.get("account_number")
    if account != expected_account:
        raise ReconciliationError("SePay API trả giao dịch của tài khoản ngân hàng khác.")
    amount_in = _parse_vnd(transaction.get("amount_in"))
    amount_out = _parse_vnd(transaction.get("amount_out"))
    if (amount_in > 0) == (amount_out > 0):
        raise ReconciliationError("Giao dịch SePay không có đúng một chiều tiền.")
    content = transaction.get("transaction_content")
    code = transaction.get("code")
    if not isinstance(content, str) or len(content) > 5000:
        raise ReconciliationError("Nội dung giao dịch SePay không hợp lệ.")
    if not isinstance(code, (str, type(None))):
        raise ReconciliationError("Mã giao dịch SePay không hợp lệ.")
    return {
        "id": transaction_id,
        "accountNumber": account,
        "transferAmount": amount_in or amount_out,
        "transferType": "in" if amount_in else "out",
        "content": content,
        "code": code,
    }


def reconcile_once(store: Store, api_token: str, expected_account: str,
                   fetcher=fetch_transactions,
                   notify_admin_ids: frozenset[int] = frozenset()) -> dict:
    state = store.get_reconciliation_state(expected_account)
    last_txn_id = state["last_txn_id"] if state else 0
    transactions = fetcher(api_token, expected_account, last_txn_id)
    if not isinstance(transactions, list):
        raise ReconciliationError("SePay API trả danh sách giao dịch không hợp lệ.")
    if len(transactions) >= PAGE_LIMIT:
        # The v1 API has no documented page number or ascending sort. A full page
        # may omit older transactions, so never advance the cursor in that case.
        raise ReconciliationError("SePay trả đủ 5000 giao dịch; cần xử lý trước khi tiếp tục đối soát.")
    normalized = [normalize_transaction(tx, expected_account) for tx in transactions]
    ids = [tx["id"] for tx in normalized]
    if len(set(ids)) != len(ids):
        raise ReconciliationError("SePay API trả ID giao dịch trùng trong cùng một lần lấy.")
    counts = {"credited": 0, "duplicate": 0, "unmatched": 0, "ignored": 0}
    credited = []
    for payload in sorted(normalized, key=lambda tx: tx["id"]):
        result = process_sepay_payload(
            store, payload, expected_account, notify_admin_ids=notify_admin_ids,
        )
        counts[result["status"]] += 1
        if result["status"] == "credited":
            credited.append(result)
    cursor = max(ids, default=last_txn_id)
    store.mark_reconciled(expected_account, cursor)
    return {**counts, "credited_events": credited, "cursor": cursor,
            "fetched": len(transactions)}
