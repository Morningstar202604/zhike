"""限流：滑动窗口，状态在 SQLite，多 worker 共享。

业界事实标准（FastAPI 多 worker 部署时进程内存不共享，必须落库或上 Redis）。
SQLite WAL 模式支持高并发读 + 单写，中小流量足够；超大流量再上 Redis。
"""
from __future__ import annotations

import os
import time

from core import store

# 每 N 次允许调用做一次过期清理，避免每个请求都跑 DELETE
_CLEAN_EVERY = int(os.environ.get("RATE_LIMIT_CLEAN_EVERY", "200"))


class SQLiteRateLimiter:
    """滑动窗口限流，状态在 SQLite，多 worker 共享。

    并发正确性靠 `BEGIN IMMEDIATE`：它立刻拿写锁，让"计数 → 判断 → 插入"
    成为不可分割的临界区。只用默认的延迟事务（DEFERRED）时，两个连接可以同时
    读到旧计数再各自插入，实测上限 5 会放行 8。
    """

    def __init__(self):
        self._calls = 0
        with store.get_conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rate_hits (key TEXT, ts REAL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_hits_key_ts ON rate_hits(key, ts)")
            # 过期清理按 ts 扫描，需要单独的 ts 索引，否则是全表扫描
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_hits_ts ON rate_hits(ts)")
        # 独立连接：限流要在自己的事务里拿写锁，不能和业务事务混在一起
        self._conn = None

    def _ensure_conn(self):
        if self._conn is None:
            import sqlite3
            path = os.environ.get("CS_AGENT_DB", "cs_agent.db")
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def allow(self, key: str, window_s: float | None = None, max_n: int | None = None) -> bool:
        window = float(os.environ.get("RATE_LIMIT_WINDOW", "60")) if window_s is None else window_s
        limit = int(os.environ.get("RATE_LIMIT_MAX", "20")) if max_n is None else max_n
        now = time.time()
        cutoff = now - window
        conn = self._ensure_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception:
            # 拿不到写锁（极端争用）：按最保守策略拒绝，避免超发
            return False
        try:
            self._calls += 1
            if self._calls % _CLEAN_EVERY == 0:
                conn.execute("DELETE FROM rate_hits WHERE ts < ?", (cutoff,))
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM rate_hits WHERE key = ? AND ts >= ?",
                (key, cutoff),
            ).fetchone()["c"]
            if count >= limit:
                conn.execute("COMMIT")
                return False
            conn.execute("INSERT INTO rate_hits(key, ts) VALUES (?, ?)", (key, now))
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
