"""安全组件：每身份滑动窗口限流 + 管理员令牌校验 + 时序安全比较。"""
import os
import secrets
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


def constant_time_equals(a: str | None, b: str | None) -> bool:
    """时序安全的字符串比较（防 token 侧信道）。空值一律 False。

    secrets.compare_digest 对含非 ASCII 的 str 会抛 TypeError，
    攻击者可用它把鉴权端点变成 500（已复现）。统一先编码成 bytes 再比。
    """
    if not a or not b:
        return False
    try:
        return secrets.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))
    except (UnicodeError, TypeError):
        return False


def is_channel_protected() -> bool:
    """CHANNEL_TOKEN 是否设置。未设置时 /v1/ingest 与心跳应直接拒绝，防公网裸奔刷 LLM 成本。"""
    return bool(os.environ.get("CHANNEL_TOKEN", "").strip())
