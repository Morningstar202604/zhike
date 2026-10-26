"""用户与群档案：把「这个人是谁 / 这个群什么调性」沉淀成可检索的结构。

为什么需要（对标 AstrBot 的会话档案 / Chatwoot 的联系人 / Koishi 的用户组）：
- 每轮对话都从零推断对端是谁，成本高且记不住；
- 群与人的语气、黑话、禁忌差异极大，没有档案只能给出"通用客服腔"。

设计：
- `profiles` 表：key（qq:123 / qq_group:456 / web:v-xxx）唯一，字段都可增量更新。
- `lexicon` 表：群/全局黑话词条（词、释义、示例、出现次数），供检索与注入。
- 二者只做「事实沉淀」，不做权限判断。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from core import store

MAX_ALIASES = 8
MAX_TAGS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables() -> None:
    """幂等建表（测试库只跑 store._ensure_schema，不跑迁移）。"""
    with store.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                profile_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                aliases TEXT DEFAULT '[]',
                tags TEXT DEFAULT '[]',
                note TEXT DEFAULT '',
                stats TEXT DEFAULT '{}',
                first_seen TEXT,
                last_seen TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lexicon (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                term TEXT NOT NULL,
                meaning TEXT DEFAULT '',
                example TEXT DEFAULT '',
                hits INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(scope, term)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lexicon_scope ON lexicon(scope, hits DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_profiles_kind ON profiles(kind, last_seen DESC)")


def _load(raw, default):
    try:
        v = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _row_to_profile(row: sqlite3.Row) -> dict:
    return {
        "key": row["profile_key"],
        "kind": row["kind"],
        "display_name": row["display_name"] or "",
        "aliases": _load(row["aliases"], []),
        "tags": _load(row["tags"], []),
        "note": row["note"] or "",
        "stats": _load(row["stats"], {}),
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
    }


def get_profile(key: str) -> dict | None:
    ensure_tables()
    with store.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM profiles WHERE profile_key = ?", (key,)).fetchone()
    return _row_to_profile(row) if row else None


def upsert_profile(key: str, kind: str, *, display_name: str = "",
                   alias: str = "", tags: list[str] | None = None,
                   note: str = "", stats_delta: dict | None = None) -> dict:
    """创建或增量更新档案；stats 计数累加，别名字/标签追加，其余覆盖（空值不覆盖）。"""
    if not key:
        raise ValueError("profile key 不能为空")
    ensure_tables()
    now = _now()
    with store.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM profiles WHERE profile_key = ?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO profiles (profile_key, kind, display_name, aliases, tags, note, stats,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, kind, display_name,
                 json.dumps([alias] if alias else [], ensure_ascii=False),
                 json.dumps(tags or [], ensure_ascii=False), note,
                 json.dumps(dict(stats_delta or {}), ensure_ascii=False), now, now),
            )
        else:
            cur = _row_to_profile(row)
            aliases = list(cur["aliases"])
            if alias and alias not in aliases:
                aliases.append(alias)
            merged_tags = list(dict.fromkeys([*cur["tags"], *(tags or [])]))[:MAX_TAGS]
            stats = dict(cur["stats"])
            for k, v in (stats_delta or {}).items():
                stats[k] = int(stats.get(k) or 0) + int(v)
            conn.execute(
                "UPDATE profiles SET display_name=?, aliases=?, tags=?, note=?, stats=?, last_seen=?"
                " WHERE profile_key=?",
                (display_name or cur["display_name"], json.dumps(aliases[-MAX_ALIASES:], ensure_ascii=False),
                 json.dumps(merged_tags, ensure_ascii=False), note or cur["note"],
                 json.dumps(stats, ensure_ascii=False), now, key),
            )
        row = conn.execute("SELECT * FROM profiles WHERE profile_key = ?", (key,)).fetchone()
    return _row_to_profile(row)


def list_profiles(kind: str = "", limit: int = 100) -> list[dict]:
    ensure_tables()
    sql = "SELECT * FROM profiles"
    args: list = []
    if kind:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY last_seen DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with store.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_profile(r) for r in rows]


def add_lexicon(scope: str, term: str, meaning: str = "", example: str = "") -> dict | None:
    """记一条黑话；同 scope 重复出现只累加 hits 并补空字段。"""
    term = (term or "").strip()
    if not term or len(term) > 40:
        return None
    ensure_tables()
    now = _now()
    with store.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM lexicon WHERE scope=? AND term=?", (scope, term)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO lexicon (scope, term, meaning, example, hits, created_at, updated_at)"
                " VALUES (?,?,?,?,1,?,?)",
                (scope, term, meaning[:300], example[:300], now, now),
            )
        else:
            conn.execute(
                "UPDATE lexicon SET hits=hits+1, meaning=?, example=?, updated_at=? WHERE id=?",
                (meaning or row["meaning"], example or row["example"], now, row["id"]),
            )
        row = conn.execute("SELECT * FROM lexicon WHERE scope=? AND term=?", (scope, term)).fetchone()
    return dict(row) if row else None


def list_lexicon(scope: str = "", limit: int = 100) -> list[dict]:
    ensure_tables()
    sql = "SELECT * FROM lexicon WHERE scope = 'global'"
    args: list = []
    if scope:
        sql = "SELECT * FROM lexicon WHERE scope = ? OR scope = 'global'"
        args.append(scope)
    sql += " ORDER BY hits DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with store.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


# ---- 黑话自动识别（保守：只认显式定义/询问句式，不猜） ----

_LEX_ASK = re.compile(
    r"(?:什么叫|啥叫|什么是)\s*[「『\"']?([^\s，。？！?]{1,20})[」』\"']?"
    r"|[「『\"']([^\s，。？！?]{1,20})[」』\"']?\s*(?:是什么意思|是啥意思|啥意思)")
_LEX_DEF = re.compile(
    r"^[「『\"']?([^\s，。？！?：:]{1,20})[」』\"']?\s*(?:就是|指的是|意思是|=)\s*(.{2,80})$")


def learn_from_message(text: str, scope: str) -> list[dict]:
    """从一条消息里抽黑话词条。返回新增/累加的条目。"""
    text = (text or "").strip()
    if not text or len(text) > 200:
        return []
    found: list[dict] = []
    m = _LEX_ASK.search(text)
    if m:
        term = (m.group(1) or m.group(2) or "").strip()
        if term:
            rec = add_lexicon(scope, term, meaning="（待补充释义）", example=text[:120])
            if rec:
                found.append(rec)
    m2 = _LEX_DEF.match(text)
    if m2:
        term, meaning = m2.group(1).strip(), m2.group(2).strip()
        if 1 <= len(term) <= 20:
            rec = add_lexicon(scope, term, meaning=meaning[:200], example=text[:120])
            if rec:
                found.append(rec)
    return found


def lexicon_block(scope: str, limit: int = 12) -> str:
    """给模型看的黑话速查（只取有释义的高频词）。"""
    items = [x for x in list_lexicon(scope, limit * 3)
             if x.get("meaning") and x["meaning"] != "（待补充释义）"]
    if not items:
        return ""
    lines = [f"- {x['term']}：{x['meaning']}" for x in items[:limit]]
    return "【本群黑话】回答时可以用，但别硬塞：\n" + "\n".join(lines)


def profile_block(key: str, group_key: str = "") -> str:
    """给模型看的档案摘要（谁在说话、这个群什么调性）。"""
    parts: list[str] = []
    p = get_profile(key)
    if p:
        bits = []
        if p["display_name"]:
            bits.append(f"称呼={p['display_name']}")
        if p["aliases"]:
            bits.append("别名=" + "/".join(p["aliases"][:4]))
        if p["tags"]:
            bits.append("标签=" + "/".join(p["tags"][:6]))
        if p["note"]:
            bits.append("备注=" + p["note"][:80])
        if p["stats"]:
            bits.append("互动=" + ", ".join(f"{k}:{v}" for k, v in list(p["stats"].items())[:4]))
        if bits:
            parts.append("【当前用户档案】" + "；".join(bits))
    if group_key:
        g = get_profile(group_key)
        if g:
            gb = []
            if g["display_name"]:
                gb.append(f"群名={g['display_name']}")
            if g["tags"]:
                gb.append("调性=" + "/".join(g["tags"][:6]))
            if g["note"]:
                gb.append("备注=" + g["note"][:80])
            if gb:
                parts.append("【当前群档案】" + "；".join(gb))
    return "\n".join(parts)
