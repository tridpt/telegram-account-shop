"""Create VietQR bank-transfer images without an external image service."""

from io import BytesIO
import re

import segno


def _tlv(tag: str, value: str) -> str:
    if not value.isascii() or len(value) > 99:
        raise ValueError("Invalid VietQR field")
    return f"{tag}{len(value):02d}{value}"


def vietqr_payload(bank_bin: str, account_number: str, amount: int, reference: str) -> str:
    if not re.fullmatch(r"[0-9]{6}", bank_bin, re.ASCII):
        raise ValueError("Invalid bank BIN")
    if not re.fullmatch(r"[A-Z0-9]{4,32}", account_number, re.ASCII):
        raise ValueError("Invalid bank account")
    if type(amount) is not int or amount <= 0 or amount > 1_000_000_000_000:
        raise ValueError("Invalid transfer amount")
    if not re.fullmatch(r"[A-Z0-9]{1,25}", reference, re.ASCII):
        raise ValueError("Invalid transfer reference")

    recipient = _tlv("00", bank_bin) + _tlv("01", account_number)
    merchant = (_tlv("00", "A000000727") + _tlv("01", recipient)
                + _tlv("02", "QRIBFTTA"))
    payload = ("000201010212" + _tlv("38", merchant) + _tlv("53", "704")
               + _tlv("54", str(amount)) + _tlv("58", "VN")
               + _tlv("62", _tlv("08", reference)) + "6304")
    # EMVCo CRC-16/CCITT-FALSE includes the 6304 tag before its four hex digits.
    crc = 0xFFFF
    for byte in payload.encode("ascii"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return payload + f"{crc:04X}"


def vietqr_png(bank_bin: str, account_number: str, amount: int, reference: str) -> bytes:
    payload = vietqr_payload(bank_bin, account_number, amount, reference)
    image = segno.make_qr(payload, error="m")
    output = BytesIO()
    image.save(output, kind="png", scale=8, border=4)
    return output.getvalue()
