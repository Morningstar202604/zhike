"""数据库迁移：版本化、幂等、可回滚保护。

为什么需要：原来靠 `CREATE TABLE IF NOT EXISTS` + 手写 ALTER 拼凑，
加表/改列时无法判断"这个库迁移到哪一步了"，也无法保证顺序执行。

设计：
- `meta.schema_version` 记录已应用到第几版
- MIGRATIONS 是有序列表，每项 (version, 描述, 执行函数)
- 启动时把高于当前版本的依次应用，全部在一个事务里
- 迁移失败回滚并拒绝启动（不允许半迁移状态对外提供服务）
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger("core")

# 迁移函数签名：(conn) -> None
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _v1_baseline(conn: sqlite3.Connection) -> None:
    """0001：基线。老库的所有表都由 store._ensure_schema 创建，这里只标记版本。"""
    # 基线本身不建表——建表逻辑仍在 store 里（幂等），迁移只负责版本推进。
    pass


def _v2_audit_index(conn: sqlite3.Connection) -> None:
    """0002：审计链按 actor 查询的索引（控制台按操作者筛选）。"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_chain_actor ON audit_chain(actor, id DESC)")


def _v3_action_duration_index(conn: sqlite3.Connection) -> None:
    """0003：动作耗时统计用的索引（性能面板按动作聚合）。"""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_log_action_time ON action_log(action, created_at DESC)"
    )


def _v4_rate_cleanup_index(conn: sqlite3.Connection) -> None:
    """0004：限流过期清理依赖 ts 索引，确保老库也有。"""
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_hits_ts ON rate_hits(ts)")
    except sqlite3.Error:
        # rate_hits 由限流器首次使用时创建；此时不存在就跳过，下次迁移会补
        pass


def _v5_outbox(conn: sqlite3.Connection) -> None:
    """0005：跨进程事件表。多 worker 或执行器需要通知时用它，代替内存队列。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_at TEXT,
            created_at TEXT NOT NULL,
            processed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_at)")


def _v6_audit_retention(conn: sqlite3.Connection) -> None:
    """0006：审计保留期支持。给 audit_chain 加 created 索引便于按时间清理。

    注意：audit_chain 本身永不删除（哈希链要完整），
    清理只针对 action_log / approval 等可归档表。
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_action_log_created_cleanup ON action_log(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_created ON approvals(created_at)")


def _v7_message_thought(conn: sqlite3.Connection) -> None:
    """0007：messages.thought 持久化模型推理过程，历史回放不丢折叠块。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "thought" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN thought TEXT")


MIGRATIONS: list[Migration] = [
    (1, "基线：标记已有结构", _v1_baseline),
    (2, "审计链 actor 索引", _v2_audit_index),
    (3, "动作日志动作+时间索引", _v3_action_duration_index),
    (4, "限流 ts 索引", _v4_rate_cleanup_index),
    (5, "跨进程事件 outbox 表", _v5_outbox),
    (6, "审计保留期索引", _v6_audit_retention),
    (7, "消息表 thought 列", _v7_message_thought),
]

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        return 0
    if not row:
        return 0
    try:
        return int(row["value"] if isinstance(row, sqlite3.Row) else row[0])
    except (TypeError, ValueError):
        return 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def migrate(conn: sqlite3.Connection) -> dict:
    """把库迁移到最新版本。返回 {from, to, applied, ok, error}。

    整个迁移在一个事务里：任一步失败则全部回滚，调用方应拒绝启动。
    """
    start = current_version(conn)
    if start >= LATEST_VERSION:
        return {"from": start, "to": start, "applied": [], "ok": True, "error": None}

    applied: list[int] = []
    # 复用调用方（get_conn）已开启的事务，不再自己 BEGIN——
    # sqlite3 在已有事务里再 BEGIN 会抛 "cannot start a transaction within a transaction"，
    # 而这个错误会让「首次启动」直接失败。
    own_tx = not conn.in_transaction
    try:
        if own_tx:
            conn.execute("BEGIN")
        for version, desc, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
            if version <= start:
                continue
            fn(conn)
            _set_version(conn, version)
            applied.append(version)
            logger.info("migration %04d applied: %s", version, desc)
        if own_tx:
            conn.commit()
        return {"from": start, "to": LATEST_VERSION, "applied": applied, "ok": True, "error": None}
    except Exception as e:
        # 显式回滚：不依赖「关闭连接时 SQLite 隐式回滚」这个副作用。
        # 连接池/长连接场景下没有这层保护，必须主动回滚。
        try:
            conn.rollback()
            rolled_back = True
        except sqlite3.Error:
            rolled_back = False
        logger.error("migration failed at version %s: %s", start, e)
        return {
            "from": start, "to": start, "applied": applied,
            "ok": False, "rolled_back": rolled_back,
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }
