"""NoneBot2 网关入口：OneBot v11（NapCat / Lagrange / LLOneBot）→ 核心服务 /v1/ingest。

启动：python gateway/bot.py（先配置 ONEBOT_WS_URL 与 CORE_BASE_URL 环境变量）。
可选 CHANNEL_TOKEN：与核心服务一致时以 X-Channel-Token 头上报，防伪造消息注入。
"""
import asyncio
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import nonebot
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter, MessageEvent
from nonebot.rule import to_me

from gateway.mapping import from_onebot_v11, ingest_payload, reply_segments

nonebot.init(
    driver="~fastapi+~websockets",
    onebot_ws_urls=[os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001")],
)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

CORE_BASE_URL = os.environ.get("CORE_BASE_URL", "http://127.0.0.1:8000")
CHANNEL_TOKEN = os.environ.get("CHANNEL_TOKEN", "")
matcher = on_message(rule=to_me(), priority=5, block=False)


def _channel_headers() -> dict[str, str]:
    return {"X-Channel-Token": CHANNEL_TOKEN} if CHANNEL_TOKEN else {}


async def _fetch_image_b64(url: str) -> str:
    """拉取 QQ 图片转 base64（8MB 上限）；任何失败静默降级为纯文本，不阻塞回复主链路。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return base64.b64encode(resp.content[: 8 * 1024 * 1024]).decode()
    except Exception:  # aqg: top-level boundary
        return ""


@driver.on_startup
async def _start_heartbeat():
    async def _beat():
        while True:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"{CORE_BASE_URL}/v1/gateway/heartbeat",
                        json={
                            "gateway": "onebot_v11",
                            "status": "online",
                            "detail": {"adapter": "nonebot2+onebot_v11"},
                        },
                        headers=_channel_headers(),
                    )
            except Exception:  # aqg: top-level boundary
                pass
            await asyncio.sleep(30)

    asyncio.create_task(_beat())


@matcher.handle()
async def handle(event: MessageEvent):
    # 单一事实源：统一走 mapping 纯函数（与单测同源），不在网关层维护第二套消息解析
    segs = [{"type": s.type, "data": dict(s.data)} for s in event.message]
    ge = from_onebot_v11(
        {
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
    )
    if ge is None:
        return
    payload = ingest_payload(ge)
    if ge.image_url:
        image_b64 = await _fetch_image_b64(ge.image_url)
        if image_b64:
            payload["image_b64"] = image_b64
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                f"{CORE_BASE_URL}/v1/ingest", json=payload, headers=_channel_headers()
            )
            resp.raise_for_status()
            reply_text = resp.json()["reply"]["text"]
    except Exception:  # 核心不可达时给用户明确反馈，而不是沉默
        reply_text = "客服系统开小差了，请稍后再试～"
    for seg in reply_segments(reply_text):
        await matcher.send(seg)


if __name__ == "__main__":
    nonebot.run()
