import re

from store import Store


TRANSFER_CODE = re.compile(r"(?:NAP|MUA)[0-9A-F]{12}", re.ASCII)


def extract_transfer_code(content: str, provider_code: str | None) -> str | None:
    candidates = set(TRANSFER_CODE.findall(content.upper()))
    if provider_code:
        candidates.update(TRANSFER_CODE.findall(provider_code.upper()))
    return next(iter(candidates)) if len(candidates) == 1 else None


def process_sepay_payload(store: Store, payload: dict, expected_account: str,
                          *, via_webhook: bool = False,
                          notify_admin_ids: frozenset[int] = frozenset()) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    transaction_id = payload.get("id")
    amount = payload.get("transferAmount")
    account = payload.get("accountNumber")
    transfer_type = payload.get("transferType")
    content = payload.get("content")
    provider_code = payload.get("code")
    if type(transaction_id) is not int or transaction_id <= 0:
        raise ValueError("Invalid SePay transaction ID")
    if type(amount) is not int or not 0 < amount <= 1_000_000_000_000:
        raise ValueError("Invalid transfer amount")
    if not isinstance(account, str) or not isinstance(transfer_type, str):
        raise ValueError("Invalid bank account or transfer type")
    if not isinstance(content, str) or not isinstance(provider_code, (str, type(None))):
        raise ValueError("Invalid transfer content")

    code = extract_transfer_code(content, provider_code)
    return store.record_bank_event(
        transaction_id=f"sepay:{transaction_id}",
        bank_account=account.strip(),
        amount=amount,
        content=content,
        transfer_type=transfer_type.strip().lower(),
        code=code,
        expected_account=expected_account,
        via_webhook=via_webhook,
        notify_admin_ids=notify_admin_ids,
    )
