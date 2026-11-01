"""Configuration persistence primitives backed by the shared SQLite connection layer."""
from __future__ import annotations

import json
import sqlite3

from core.store import _now, get_conn


def get_config() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: json.loads(r["value"] or "{}") for r in rows}


def config_version() -> int:
    """配置版本号：每次 set_config 递增，供上游缓存判断是否失效。"""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'config_version'").fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _bump_config_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('config_version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def set_config(patch: dict) -> None:
    with get_conn() as conn:
        for group, value in (patch or {}).items():
            row = conn.execute("SELECT value FROM config WHERE key = ?", (group,)).fetchone()
            current = json.loads(row["value"]) if row and row["value"] else {}
            if isinstance(value, dict) and isinstance(current, dict):
                merged = {**current, **value}
            else:
                merged = value
            conn.execute(
                "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (group, json.dumps(merged, ensure_ascii=False), _now()),
            )
        _bump_config_version(conn)
