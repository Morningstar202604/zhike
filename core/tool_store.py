"""Persistent tool registry and its versioned in-process cache."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading

from core.store import _now, get_conn

_CACHE: dict = {"path": "", "version": -1, "rows": []}
_CACHE_LOCK = threading.Lock()


def _db_key() -> str:
    return os.path.abspath(os.environ.get("CS_AGENT_DB", "cs_agent.db"))


def _copy_rows(rows: list[dict]) -> list[dict]:
    return copy.deepcopy(rows)


def _tool_row(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "desc": row["desc"],
        "args_json": json.loads(row["args_json"] or "{}"),
        "enabled": bool(row["enabled"]),
        "builtin": bool(row["builtin"]),
    }


def list_tools() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, desc, args_json, enabled, builtin FROM tools ORDER BY id"
        ).fetchall()
    return [_tool_row(row) for row in rows]


def _bump_tools_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('tools_version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def tools_version() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'tools_version'").fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def active_tools() -> list[dict]:
    path = _db_key()
    version = tools_version()
    with _CACHE_LOCK:
        if _CACHE["path"] == path and _CACHE["version"] == version:
            return _copy_rows(_CACHE["rows"])
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, desc, args_json, enabled, builtin FROM tools "
                "WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        items = [_tool_row(row) for row in rows]
        _CACHE.update({"path": path, "version": version, "rows": items})
        return _copy_rows(items)


def invalidate_tools_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.update({"path": "", "version": -1, "rows": []})


def get_tool(tool_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (tool_id,)).fetchone()
    return _tool_row(row) if row else None


def upsert_tool(tool_id: str, name: str, desc: str, args_json: dict,
                enabled: bool = True, builtin: bool = False) -> dict:
    if not isinstance(args_json, dict):
        raise ValueError("args_json 必须是对象（参数名 → 类型）")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tools (id, name, desc, args_json, enabled, builtin, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, desc = excluded.desc, "
            "args_json = excluded.args_json, enabled = excluded.enabled, "
            "builtin = excluded.builtin, updated_at = excluded.updated_at",
            (tool_id, name, desc, json.dumps(args_json, ensure_ascii=False),
             1 if enabled else 0, 1 if builtin else 0, _now()),
        )
        _bump_tools_version(conn)
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (tool_id,)).fetchone()
    invalidate_tools_cache()
    return _tool_row(row) if row else None


def set_tool_enabled(tool_id: str, enabled: bool) -> dict | None:
    desired = 1 if enabled else 0
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tools SET enabled = ?, updated_at = ? "
            "WHERE id = ? AND enabled != ?",
            (desired, _now(), tool_id, desired),
        )
        if cur.rowcount:
            _bump_tools_version(conn)
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (tool_id,)).fetchone()
    if cur.rowcount:
        invalidate_tools_cache()
    return _tool_row(row) if row else None


def delete_tool(tool_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tools WHERE id = ?", (tool_id,))
        if cur.rowcount:
            _bump_tools_version(conn)
    if cur.rowcount:
        invalidate_tools_cache()
    return bool(cur.rowcount)
