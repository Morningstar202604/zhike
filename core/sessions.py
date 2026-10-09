"""渠道会话映射：QQ 群/私聊等渠道按 chat 维度复用会话，空闲超过 TTL 自动新建。"""
import os
import uuid
from datetime import datetime, timezone

from core import store


def idle_ttl() -> int:
    return int(os.environ.get("SESSION_IDLE_TTL", "1800"))


def resolve_session(channel: str, visitor_id: str) -> str:
    row = store.get_channel_session(channel)
    if row:
        sess = store.get_session(row["session_id"])
        if sess and sess["status"] == "ongoing":
            try:
                updated = datetime.fromisoformat(row["updated_at"])
                age = datetime.now(timezone.utc) - updated
                if age.total_seconds() <= idle_ttl():
                    return sess["id"]
            except ValueError:
                pass
    sid = uuid.uuid4().hex
    store.create_session(sid, visitor_id)
    if store.claim_channel_session(channel, sid):
        return sid
    # 并发输家：同渠道已被认领 → 复用赢家的会话，删掉自己刚建的空会话防孤儿
    winner = store.get_channel_session(channel)
    winner_sid = winner["session_id"] if winner else ""
    wsess = store.get_session(winner_sid) if winner_sid else None
    if wsess and wsess["status"] == "ongoing":
        store.discard_session(sid)
        return winner_sid
    # 极端窗口（赢家映射指向异常会话）：覆盖映射，用自己的新会话保证可用性
    store.set_channel_session(channel, sid)
    return sid
