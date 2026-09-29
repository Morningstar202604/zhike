"""安全组件：每身份滑动窗口限流 + 管理员令牌校验。"""
import os
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, window_s: float | None = None, max_n: int | None = None) -> bool:
        window = float(os.environ.get("RATE_LIMIT_WINDOW", "60")) if window_s is None else window_s
        limit = int(os.environ.get("RATE_LIMIT_MAX", "20")) if max_n is None else max_n
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "")
