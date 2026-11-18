"""Small, bounded in-memory limits for Telegram updates and QR creation."""

from collections import OrderedDict
from dataclasses import dataclass
import time


@dataclass
class TokenBucket:
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float

    def refill(self, now: float) -> None:
        self.tokens = min(
            self.capacity,
            self.tokens + max(0.0, now - self.updated_at) * self.refill_per_second,
        )
        self.updated_at = now


@dataclass
class UserLimits:
    requests: TokenBucket
    warned_at: float = float("-inf")
    last_topup_at: float = float("-inf")


class UpdateLimiter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        now = clock()
        self.users: OrderedDict[int, UserLimits] = OrderedDict()
        self.global_requests = TokenBucket(40, 10, 40, now)
        self.global_topups = TokenBucket(3, 0.1, 3, now)

    def _user(self, user_id: int, now: float) -> UserLimits:
        user = self.users.get(user_id)
        if user is None:
            user = UserLimits(TokenBucket(10, 0.5, 10, now))
            self.users[user_id] = user
            if len(self.users) > 10_000:
                self.users.popitem(last=False)
        else:
            self.users.move_to_end(user_id)
        return user

    def check_update(self, user_id: int, *, is_admin: bool = False) -> tuple[bool, bool]:
        """Return (allowed, send_one_warning); excess updates need no bot API call."""
        if is_admin:
            return True, False
        now = self.clock()
        user = self._user(user_id, now)
        user.requests.refill(now)
        if user.requests.tokens < 1:
            warn = now - user.warned_at >= 30
            if warn:
                user.warned_at = now
            return False, warn
        self.global_requests.refill(now)
        if self.global_requests.tokens < 1:
            return False, False
        user.requests.tokens -= 1
        self.global_requests.tokens -= 1
        return True, False

    def allow_topup(self, user_id: int) -> bool:
        now = self.clock()
        user = self._user(user_id, now)
        if now - user.last_topup_at < 15:
            return False
        self.global_topups.refill(now)
        if self.global_topups.tokens < 1:
            return False
        self.global_topups.tokens -= 1
        user.last_topup_at = now
        return True

    def reset_user(self, user_id: int) -> None:
        """Give an unbanned customer a fresh request bucket."""
        self.users.pop(user_id, None)
