"""Send operational alerts even when the main Telegram bot is offline."""

import http.client
import json
import logging
import os
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


def send_admin_message(text: str, root: Path = ROOT) -> bool:
    values = dotenv_values(root / ".env")
    token = (os.environ.get("BOT_TOKEN", values.get("BOT_TOKEN")) or "").strip()
    raw_ids = (os.environ.get("ADMIN_IDS", values.get("ADMIN_IDS")) or "").strip()
    if not token or not raw_ids:
        log.warning("Admin alert unavailable: BOT_TOKEN or ADMIN_IDS missing")
        return False

    delivered = False
    for raw_id in raw_ids.split(","):
        try:
            admin_id = int(raw_id.strip())
        except ValueError:
            log.warning("Admin alert skipped an invalid ADMIN_IDS entry")
            continue
        connection = None
        try:
            connection = http.client.HTTPSConnection("api.telegram.org", timeout=8)
            body = json.dumps({"chat_id": admin_id, "text": text}).encode("utf-8")
            connection.request(
                "POST", f"/bot{token}/sendMessage", body,
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                delivered = True
            else:
                log.warning("Admin alert to %s got HTTP %s", admin_id, response.status)
        except Exception as exc:
            log.warning("Admin alert to %s failed: %s", admin_id, type(exc).__name__)
        finally:
            if connection is not None:
                connection.close()
    return delivered
