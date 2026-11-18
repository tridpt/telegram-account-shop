"""Notify customers about sustained bot outages and recoveries."""

import http.client
import json
import logging
import time
from typing import Callable

from store import Store


log = logging.getLogger(__name__)
OUTAGE_SECONDS = 60
SEND_INTERVAL = 0.2
SEND_BATCH_SIZE = 20
STATUS_TEXT = {
    "busy": "⏸ Chủ shop có việc bận, bot tạm thời không hoạt động. Vui lòng quay lại sau.",
    "online": "✅ Bot đã hoạt động. Bạn có thể tiếp tục xem sản phẩm và mua hàng.",
}
STATUS_TEXT_EN = {
    "busy": "⏸ The shop owner is busy. The bot is temporarily unavailable. Please come back later.",
    "online": "✅ The bot is back online. You can browse products and place orders again.",
}


def send_status_message(token: str, user_id: int, content: str) -> tuple[str, int]:
    connection = None
    try:
        connection = http.client.HTTPSConnection("api.telegram.org", timeout=8)
        body = json.dumps({"chat_id": user_id, "text": content}).encode("utf-8")
        connection.request(
            "POST", f"/bot{token}/sendMessage", body,
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read()
        if response.status == 200:
            return "sent", 0
        if response.status in (400, 403):
            return "failed", 0
        if response.status == 429:
            try:
                seconds = int(json.loads(payload)["parameters"]["retry_after"])
            except (ValueError, KeyError, TypeError):
                seconds = 30
            return "rate_limited", max(1, min(seconds + 1, 3600))
        log.warning("Customer status send to %s got HTTP %s", user_id, response.status)
    except (OSError, http.client.HTTPException):
        log.warning("Customer status send to %s failed", user_id)
    finally:
        if connection is not None:
            connection.close()
    return "retry", 0


class CustomerStatusNotifier:
    def __init__(
        self, store: Store, token: str, admin_ids: frozenset[int],
        probe: Callable[[], bool],
        sender: Callable[[str, int, str], tuple[str, int]] = send_status_message,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.token = token
        self.admin_ids = admin_ids
        self.probe = probe
        self.sender = sender
        self.clock = clock
        self.sleep = sleep
        self.state = store.get_presence_state()
        self.down_since: float | None = None
        self.pause_until = 0.0
        self.last_sent: dict[int, float] = {}

    def observe(self, healthy: bool) -> dict | None:
        now = self.clock()
        if healthy:
            self.down_since = None
            desired = "online"
        else:
            if self.down_since is None:
                self.down_since = now
            if now - self.down_since < OUTAGE_SECONDS:
                return None
            desired = "busy"
        if self.state == desired:
            return None
        batch = self.store.queue_presence_broadcast(
            desired, STATUS_TEXT[desired], self.admin_ids,
        )
        self.state = desired
        return {"state": desired, **batch} if batch else None

    def send_due(self) -> None:
        if self.clock() < self.pause_until:
            return
        for item in self.store.list_due_presence_deliveries(SEND_BATCH_SIZE):
            # A recovery can happen while a large batch is being sent.
            if item["state"] != self.state or self.probe() != (self.state == "online"):
                return
            user_id, batch_id = item["user_id"], item["batch_id"]
            self.sleep(max(0, 1.1 - (self.clock() - self.last_sent.get(user_id, float("-inf")))))
            content = (STATUS_TEXT_EN[item["state"]]
                       if self.store.get_user_language(user_id) == "en" else item["content"])
            result, retry_after = self.sender(self.token, user_id, content)
            self.last_sent[user_id] = self.clock()
            if result == "sent":
                self.store.finish_broadcast_delivery(batch_id, user_id, "sent")
            elif result == "failed":
                self.store.finish_broadcast_delivery(batch_id, user_id, "failed")
            elif result == "rate_limited":
                self.store.retry_broadcast_delivery(batch_id, user_id, retry_after)
                self.pause_until = self.clock() + retry_after
                return
            else:
                delay = min(3600, 30 * 2 ** min(item["attempts"], 7))
                self.store.retry_broadcast_delivery(batch_id, user_id, delay)
            self.sleep(SEND_INTERVAL)
