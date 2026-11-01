"""Persistence for multi-step Agent runs and their recorded steps."""
from __future__ import annotations

import json

from core.store import _now, get_conn


def agent_run_create(run_id: str, session_id: str | None, principal: str, goal: str,
                     tier: int, budget: dict) -> dict:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_runs (id, session_id, principal, goal, status, tier, "
            "budget_json, usage_json, started_at) VALUES (?,?,?,?,'running',?,?,?,?)",
            (run_id, session_id, principal, goal[:2000], tier,
             json.dumps(budget, ensure_ascii=False),
             json.dumps({"steps": 0, "tool_calls": 0, "tokens": 0, "cost": 0.0}), _now()),
        )
    return agent_run_get(run_id) or {}


def agent_run_get(run_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    for key in ("budget_json", "usage_json", "result"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, ValueError):
                pass
    return data


def agent_run_update(run_id: str, **fields) -> None:
    allowed = {"status", "usage_json", "result", "error", "ended_at"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("usage_json", "result") and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    params.append(run_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?", params)


def agent_step_add(run_id: str, step_no: int, thought: str, action: str | None,
                   params: dict | None, observation: str, status: str,
                   duration_ms: int = 0) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO agent_steps (run_id, step_no, thought, action, params, observation, "
            "status, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, step_no, (thought or "")[:4000], action,
             json.dumps(params or {}, ensure_ascii=False) if params is not None else None,
             (observation or "")[:8000], status, duration_ms, _now()),
        )
        return cur.lastrowid or 0


def agent_steps_list(run_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_no, id", (run_id,)
        ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        if data.get("params"):
            try:
                data["params"] = json.loads(data["params"])
            except (TypeError, ValueError):
                pass
        result.append(data)
    return result


def agent_runs_list(limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT id, session_id, principal, goal, status, tier, started_at, ended_at FROM agent_runs"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
