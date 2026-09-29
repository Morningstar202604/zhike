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
    store.set_channel_session(channel, sid)
    return sid
