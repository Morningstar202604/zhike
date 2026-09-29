"""NoneBot2 网关入口：OneBot v11（NapCat / Lagrange / LLOneBot）→ 核心服务 /v1/ingest。

启动：python gateway/bot.py（先配置 ONEBOT_WS_URL 与 CORE_BASE_URL 环境变量）。
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import nonebot
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter, MessageEvent
from nonebot.rule import to_me

from gateway.mapping import ingest_payload, reply_segments

nonebot.init(
    driver="~fastapi+~websockets",
    onebot_ws_urls=[os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001")],
)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

CORE_BASE_URL = os.environ.get("CORE_BASE_URL", "http://127.0.0.1:8000")
matcher = on_message(rule=to_me(), priority=5, block=False)


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
                    )
            except Exception:  # aqg: top-level boundary
                pass
            await asyncio.sleep(30)

    asyncio.create_task(_beat())


@matcher.handle()
async def handle(event: MessageEvent):
    text = event.get_plaintext().strip()
    if not text:
        return
    payload = ingest_payload(
        type("E", (), {
            "channel_type": "qq_group" if event.message_type == "group" else "qq_private",
            "chat_id": str(getattr(event, "group_id", "") or event.user_id),
            "user_id": str(event.user_id),
            "text": text,
            "display_name": event.sender.nickname or "",
            "message_id": str(event.message_id),
        })(),
    )
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(f"{CORE_BASE_URL}/v1/ingest", json=payload)
        resp.raise_for_status()
        reply_text = resp.json()["reply"]["text"]
    for seg in reply_segments(reply_text):
        await matcher.send(seg)


if __name__ == "__main__":
    nonebot.run()
