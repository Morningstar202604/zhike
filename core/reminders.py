"""提醒调度器：到点把提醒推回用户所在的渠道。

为什么不用 APScheduler：只需要一个「每 30 秒扫一次到期行」的循环，
引入调度库要额外进程/存储；而 SQLite 已经是唯一事实源，
多进程重复推送用 `status=pending` 的条件更新天然互斥（乐观锁）。

推送通道：
- QQ 群/私聊：走网关 /internal/action（send_group_msg / send_private_msg）
- 网页：写入该会话消息 + WebSocket 广播，用户回到页面就能看到

失败不丢：attempts + last_error 落库，超过 MAX_ATTEMPTS 标记 failed，
不会无限重试打爆网关。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from core import store

logger = logging.getLogger("core.reminders")

TICK_S = float(os.environ.get("REMINDER_TICK_SECONDS", "30"))
MAX_ATTEMPTS = int(os.environ.get("REMINDER_MAX_ATTEMPTS", "5"))


async def _deliver_qq(rec: dict, text: str) -> tuple[bool, str]:
    from core.actions import client as action_client

    gid = (rec.get("group_id") or "").strip()
    uid = (rec.get("channel_user_id") or "").strip()
    try:
        if gid:
            await action_client.call_action(
                "send_group_msg",
                {"group_id": int(gid) if gid.isdigit() else gid, "message": text},
                timeout_s=15,
            )
        elif uid:
            await action_client.call_action(
                "send_private_msg",
                {"user_id": int(uid) if uid.isdigit() else uid, "message": text},
                timeout_s=15,
            )
        else:
            return False, "没有可用的 QQ 目标"
    except Exception as e:  # 网关离线/超时都要落 last_error
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    return True, ""


async def _deliver_web(rec: dict, text: str) -> tuple[bool, str]:
    """网页渠道：落一条 agent 消息 + 尝试 WS 广播（无人接听不算失败）。"""
    sid = (rec.get("chat_key") or "").split(":", 1)[-1]
    session = store.get_session(sid) if sid else None
    if not session:
        return False, "会话已不存在"
    store.add_message(session["id"], "agent", text)
    try:
        from core.main import hub  # 延迟导入，避免循环依赖

        await hub.publish(session["id"], {"type": "message", "role": "agent", "content": text})
    except Exception:
        pass  # 广播失败不影响落库
    return True, ""


async def deliver(rec: dict) -> bool:
    when = ""
    try:
        when = datetime.fromisoformat(rec["due_at"]).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        when = rec["due_at"]
    text = f"⏰ 提醒：{rec['text']}" + (f"（你设的是 {when}）" if when else "")

    channel = (rec.get("channel_type") or "webchat").lower()
    try:
        if channel.startswith("qq"):
            ok, err = await _deliver_qq(rec, text)
        else:
            ok, err = await _deliver_web(rec, text)
    except Exception as e:  # 投递实现内部炸了也必须落 last_error，不能中断整轮扫描
        ok, err = False, f"{type(e).__name__}: {str(e)[:120]}"

    if ok:
        store.reminder_mark(int(rec["id"]), "sent")
        logger.info("reminder %s sent via %s", rec["id"], channel)
        return True
    attempts = int(rec.get("attempts") or 0) + 1
    store.reminder_mark(int(rec["id"]), "failed" if attempts >= MAX_ATTEMPTS else "pending", err)
    logger.warning("reminder %s delivery failed (%s/%s): %s",
                   rec["id"], attempts, MAX_ATTEMPTS, err)
    return False


async def tick() -> int:
    """扫一次到期提醒，返回成功投递数。"""
    now = datetime.now(timezone.utc).isoformat()
    rows = store.reminder_due(now)
    sent = 0
    for rec in rows:
        if await deliver(rec):
            sent += 1
    return sent


async def run_forever(stop: asyncio.Event | None = None) -> None:
    """常驻循环。由 app lifespan 启停。"""
    stop = stop or asyncio.Event()
    logger.info("reminder scheduler started (tick=%ss)", TICK_S)
    while not stop.is_set():
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 单次失败不能杀掉循环
            logger.warning("reminder tick failed: %s: %s", type(e).__name__, str(e)[:150])
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_S)
        except asyncio.TimeoutError:
            pass
    logger.info("reminder scheduler stopped")
