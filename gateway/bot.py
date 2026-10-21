"""NoneBot2 网关入口：OneBot v11（NapCat / Lagrange / LLOneBot）→ 核心服务 /v1/ingest。

启动：python gateway/bot.py（先配置 ONEBOT_WS_URL 与 CORE_BASE_URL 环境变量）。
可选 CHANNEL_TOKEN：与核心服务一致时以 X-Channel-Token 头上报，防伪造消息注入。
"""
import asyncio
import base64
import logging
import os
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import nonebot
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter, MessageEvent
from nonebot.rule import to_me

from gateway.adapters import OneBot11Adapter, bus, registry
from gateway.adapters.bus import EV_ACTION_DONE, EV_MESSAGE_IN, EV_REPLY_OUT

logger = logging.getLogger("gateway")

# 适配器实例：整个网关生命周期复用（重连时 bind 新 bot）
adapter = OneBot11Adapter()

_init_kwargs: dict = {
    "driver": "~fastapi+~websockets",
    "onebot_ws_urls": [os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001")],
}
# 协议端若启用 access token（NapCat WebUI 可配），网关必须带上，否则握手被 403。
# 用 NoneBot 配置项 ONEBOT_ACCESS_TOKEN 传入，适配器会以 Authorization: Bearer 发送。
if os.environ.get("ONEBOT_ACCESS_TOKEN"):
    _init_kwargs["onebot_access_token"] = os.environ["ONEBOT_ACCESS_TOKEN"]

nonebot.init(**_init_kwargs)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

CORE_BASE_URL = os.environ.get("CORE_BASE_URL", "http://127.0.0.1:8000")
CHANNEL_TOKEN = os.environ.get("CHANNEL_TOKEN", "")
matcher = on_message(rule=to_me(), priority=5, block=False)


def _channel_headers() -> dict[str, str]:
    return {"X-Channel-Token": CHANNEL_TOKEN} if CHANNEL_TOKEN else {}


def _pick_bot():
    """取当前在线的 bot。多账号时取第一个；无 bot 返回 None。"""
    bots = list(nonebot.get_bots().values())
    return bots[0] if bots else None


def _register_action_endpoint() -> None:
    """向 NoneBot 的 FastAPI driver 挂内部动作端点。

    只监听内网/回环（compose 里 gateway 不发布端口），靠 X-Channel-Token 鉴权。
    网关只做协议转换：不做权限判断、不做业务逻辑，那些都在 core 侧完成。
    """
    import hmac

    from fastapi import Request
    from fastapi.responses import JSONResponse

    app = driver.server_app

    def _authorized(req: Request) -> bool:
        if not CHANNEL_TOKEN:
            # 未配置令牌视为未启用（与 core 的 503 语义一致）
            return False
        got = req.headers.get("x-channel-token", "")
        return hmac.compare_digest(got, CHANNEL_TOKEN)

    @app.post("/internal/action")
    async def _action(req: Request):
        if not _authorized(req):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

        action = (body or {}).get("action")
        params = (body or {}).get("params") or {}
        if not isinstance(action, str) or not action:
            return JSONResponse({"ok": False, "error": "missing action"}, status_code=400)
        if not isinstance(params, dict):
            return JSONResponse({"ok": False, "error": "params must be object"}, status_code=400)

        bot = _pick_bot()
        if bot is None:
            return JSONResponse(
                {"ok": False, "error": "协议端未连接（机器人未登录）"}, status_code=503
            )
        try:
            data = await bot.call_api(action, **params)
        except Exception as e:  # 协议端拒绝/超时：转成结构化错误，不抛 traceback
            logger.warning("action %s failed: %s: %s", action, type(e).__name__, str(e)[:150])
            return JSONResponse(
                {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}, status_code=502
            )
        return {"ok": True, "data": data}

    @app.get("/internal/status")
    async def _status(req: Request):
        if not _authorized(req):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        bot = _pick_bot()
        return {
            "connected": bot is not None,
            "self_id": str(bot.self_id) if bot is not None else "",
            "bots": len(nonebot.get_bots()),
        }

    logger.info("internal action endpoint registered at /internal/action")


_register_action_endpoint()


async def _fetch_image_b64(url: str) -> str:
    """拉取 QQ 图片转 base64（8MB 上限）；任何失败静默降级为纯文本，不阻塞回复主链路。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return base64.b64encode(resp.content[: 8 * 1024 * 1024]).decode()
    except Exception:  # aqg: top-level boundary
        return ""


_heartbeat_task: asyncio.Task | None = None
HEARTBEAT_INTERVAL_S = int(os.environ.get("HEARTBEAT_INTERVAL_S", "30"))


async def _heartbeat_loop():
    """定时上报心跳。连续失败会记日志（不刷屏），且该任务可被 shutdown 取消。"""
    consecutive_failures = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{CORE_BASE_URL}/v1/gateway/heartbeat",
                    json={
                        "gateway": "onebot_v11",
                        "status": "online",
                        "detail": {"adapter": "nonebot2+onebot_v11"},
                    },
                    headers=_channel_headers(),
                )
                resp.raise_for_status()
            if consecutive_failures:
                logger.info("heartbeat recovered after %d failures", consecutive_failures)
                consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 心跳失败不能拖垮网关，但也不能完全静默
            consecutive_failures += 1
            # 只在前几次与每 10 次时输出，避免核心长时间不可用时刷爆日志
            if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                logger.warning("heartbeat failed (%d): %s: %s",
                               consecutive_failures, type(e).__name__, str(e)[:120])
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            raise


@driver.on_startup
async def _start_heartbeat():
    global _heartbeat_task
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    # 保存引用可避免任务被 GC 回收；同时让 shutdown 能干净地停掉它
    logger.info("heartbeat started, interval=%ss", HEARTBEAT_INTERVAL_S)


@driver.on_shutdown
async def _stop_heartbeat():
    global _heartbeat_task
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
    _heartbeat_task = None


def _event_to_raw(event: MessageEvent) -> dict:
    """NoneBot 事件 → OneBot 原始 dict（适配器只吃原始 dict，便于脱离框架测试）。"""
    segs = [{"type": s.type, "data": dict(s.data)} for s in event.message]
    return {
        "post_type": "message",
        "self_id": str(event.self_id),
        "user_id": event.user_id,
        "message_type": event.message_type,
        "group_id": getattr(event, "group_id", None),
        "message": segs,
        "sender": {"nickname": (event.sender.nickname if event.sender else "") or ""},
        "message_id": event.message_id,
        # 适配器已剥离命中的 @ 段，群聊判定依赖事件级 to_me
        "to_me": bool(getattr(event, "to_me", False)),
    }


@driver.on_startup
async def _bind_adapter():
    """启动时把在线 bot 绑到适配器上，并把适配器登记进注册表。"""
    bot = _pick_bot()
    if bot is not None:
        adapter.bind(bot)
    try:
        registry.register(adapter)
    except ValueError:
        pass  # 已注册（重连场景）
    logger.info("adapter bound: %s", adapter.name)


@matcher.handle()
async def handle(event: MessageEvent):
    # 单一事实源：统一走适配器接口（内部仍是 mapping 纯函数），不在网关层维护第二套解析
    bot = _pick_bot()
    if bot is not None:
        adapter.bind(bot)

    msg = adapter.parse(_event_to_raw(event))
    if msg is None:
        return

    await bus.publish(EV_MESSAGE_IN, {
        "channel": msg.channel, "chat_id": msg.chat_id,
        "sender": msg.sender_id, "text": msg.text[:100],
    })

    payload = {
        "channel_type": msg.channel,
        "channel_user_id": msg.sender_id,
        "group_id": msg.chat_id if msg.is_group else None,
        "text": msg.text,
        "display_name": msg.display_name,
        "message_id": msg.message_id,
    }
    if msg.images:
        image_b64 = await _fetch_image_b64(msg.images[0])
        if image_b64:
            payload["image_b64"] = image_b64

    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                f"{CORE_BASE_URL}/v1/ingest", json=payload, headers=_channel_headers()
            )
            resp.raise_for_status()
            reply_text = resp.json()["reply"]["text"]
        await bus.publish(EV_ACTION_DONE, {
            "action": "ingest", "duration_ms": int((time.time() - started) * 1000), "ok": True,
        })
    except Exception as e:  # 核心不可达时给用户明确反馈，而不是沉默
        logger.warning("ingest failed: %s: %s", type(e).__name__, str(e)[:120])
        reply_text = "客服系统开小差了，请稍后再试～"
        await bus.publish(EV_ACTION_DONE, {
            "action": "ingest", "duration_ms": int((time.time() - started) * 1000),
            "ok": False, "error": f"{type(e).__name__}",
        })

    result = await adapter.send(msg, reply_text)
    if not result.ok:
        logger.warning("reply send failed: %s", result.error)
    await bus.publish(EV_REPLY_OUT, {
        "channel": msg.channel, "chat_id": msg.chat_id, "ok": result.ok,
    })


if __name__ == "__main__":
    nonebot.run()
