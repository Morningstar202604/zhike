"""限流：从内存 deque 换成 SQLite 表，多 worker 共享。

业界事实标准（FastAPI 多 worker 部署时进程内存不共享，必须落库或上 Redis）。
SQLite WAL 模式支持高并发读 + 单写，中小流量足够；超大流量再上 Redis。
"""
from __future__ import annotations

import time

from core import store


class SQLiteRateLimiter:
    """滑动窗口限流，状态在 SQLite，多 worker 共享。"""

    def __init__(self):
        # 建表（幂等）
        with store.get_conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rate_hits (key TEXT, ts REAL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_hits_key_ts ON rate_hits(key, ts)")

    def allow(self, key: str, window_s: float | None = None, max_n: int | None = None) -> bool:
        import os
        window = float(os.environ.get("RATE_LIMIT_WINDOW", "60")) if window_s is None else window_s
        limit = int(os.environ.get("RATE_LIMIT_MAX", "20")) if max_n is None else max_n
        now = time.time()
        cutoff = now - window
        with store.get_conn() as conn:
            # 清理过期（顺带做，每 100 次请求清一次避免频繁 DELETE）
            conn.execute("DELETE FROM rate_hits WHERE ts < ?", (cutoff,))
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM rate_hits WHERE key = ? AND ts >= ?",
                (key, cutoff),
            ).fetchone()["c"]
            if count >= limit:
                return False
            conn.execute("INSERT INTO rate_hits(key, ts) VALUES (?, ?)", (key, now))
            return True
