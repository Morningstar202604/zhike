"""渠道会话映射：QQ 群/私聊等渠道按 chat 维度复用会话，空闲超过 TTL 自动新建。

TTL 语义：从「最后一次活动」（读取或写入该渠道映射）起算的空闲时长，不是会话总寿命。
并发原语分工：
- claim_channel_session：首次创建时原子认领，主键冲突即失败（输家复用赢家）
- replace_channel_session：TTL 过期换新会话时覆盖（必须能盖掉旧行）
- touch_channel_session：复用成功后刷新最后活动时间，否则 TTL 永远不滚动
"""
import os
import uuid
from datetime import datetime, timezone

from core import store


def idle_ttl() -> int:
    return int(os.environ.get("SESSION_IDLE_TTL", "1800"))


def _is_fresh(row: dict | None) -> bool:
    """映射是否存在、会话是否仍 ongoing、空闲时长是否在 TTL 内。"""
    if not row:
        return False
    sess = store.get_session(row["session_id"])
    if not sess or sess["status"] != "ongoing":
        return False
    try:
        updated = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return False
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    return age <= idle_ttl()


def resolve_session(channel: str, visitor_id: str) -> str:
    row = store.get_channel_session(channel)
    if _is_fresh(row):
        # 命中复用：必须刷新最后活动时间，否则映射永远停留在建会话那一刻
        store.touch_channel_session(channel, row["session_id"])
        return row["session_id"]

    sid = uuid.uuid4().hex
    store.create_session(sid, visitor_id)

    if row is None:
        # 无映射：原子认领，保证并发只建一个
        if store.claim_channel_session(channel, sid):
            return sid
        winner = store.get_channel_session(channel)
        winner_sid = winner["session_id"] if winner else ""
        if _is_fresh(winner):
            store.discard_session(sid)
            return winner_sid
        # 赢家映射指向异常会话：覆盖，保证可用性
        store.replace_channel_session(channel, sid)
        return sid

    # 有映射但已过期/已关闭：覆盖换新会话（claim 在这里必然失败，不能复用）
    if store.replace_channel_session(channel, sid):
        return sid
    # 并发覆盖输家：别人先写了新映射，复用它并清理自己的空会话
    winner = store.get_channel_session(channel)
    winner_sid = winner["session_id"] if winner else ""
    if _is_fresh(winner):
        store.discard_session(sid)
        return winner_sid
    # 极端情况：赢家又坏了，兜底用自己（可能在下一轮被修复）
    return sid
