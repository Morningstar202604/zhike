"""OneBot v11 适配器：把现有 mapping 纯函数包装成标准接口。

不新写逻辑，只做接口适配——mapping.py 的解析规则保持不变，
这样现有 14 个 mapping 单测继续有效。
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any

from gateway.adapters.base import (
    CAP_DELETE_MSG,
    CAP_GROUP_ADMIN,
    CAP_GROUP_MEMBER_LIST,
    CAP_MESSAGE_HISTORY,
    CAP_SEND_FILE,
    CAP_SEND_IMAGE,
    CAP_SEND_TEXT,
    NormalizedMessage,
    SendResult,
)
from gateway.mapping import GatewayEvent, from_onebot_v11, reply_segments


class OneBot11Adapter:
    """OneBot v11 适配器。

    bot 由外部注入（NoneBot 的 Bot 实例），便于单测时替换为假对象。
    """

    name = "onebot_v11"

    def __init__(self, bot=None, max_chars: int = 4500):
        self._bot = bot
        self._max_chars = max_chars

    def bind(self, bot) -> None:
        """绑定/更换底层 bot（网关启动或重连后调用）。"""
        self._bot = bot

    # ---------- 能力 ----------

    def capabilities(self) -> set[str]:
        caps = {CAP_SEND_TEXT, CAP_SEND_IMAGE, CAP_DELETE_MSG, CAP_GROUP_MEMBER_LIST, CAP_MESSAGE_HISTORY}
        if self._bot is not None:
            caps |= {CAP_SEND_FILE, CAP_GROUP_ADMIN}
        return caps

    # ---------- 收 ----------

    def parse(self, raw: dict) -> NormalizedMessage | None:
        """OneBot 事件 → 统一消息。复用 mapping.from_onebot_v11。"""
        ev: GatewayEvent | None = from_onebot_v11(raw)
        if ev is None:
            return None
        images = [ev.image_url] if ev.image_url else []
        return NormalizedMessage(
            channel=ev.channel_type,
            chat_id=ev.chat_id,
            sender_id=ev.user_id,
            text=ev.text,
            images=images,
            display_name=ev.display_name,
            message_id=ev.message_id,
            raw=raw,
        )

    # ---------- 发 ----------

    def _resolve_api(self, msg: NormalizedMessage) -> tuple[str, dict]:
        if msg.is_group:
            return "send_group_msg", {"group_id": self._coerce_id(msg.chat_id)}
        return "send_private_msg", {"user_id": self._coerce_id(msg.chat_id)}

    @staticmethod
    def _coerce_id(value: str):
        """OneBot 要求数字 id 时给 int，其他保持字符串。"""
        s = str(value)
        return int(s) if s.isdigit() else s

    async def send(self, msg: NormalizedMessage, content: str) -> SendResult:
        if self._bot is None:
            return SendResult(ok=False, error="协议端未连接")
        api, params = self._resolve_api(msg)
        last_id = None
        try:
            for seg in reply_segments(content or "", self._max_chars):
                if not seg:
                    continue
                data = await self._bot.call_api(api, message=seg, **params)
                if isinstance(data, dict) and data.get("message_id") is not None:
                    last_id = str(data["message_id"])
        except Exception as e:  # 发送失败要能上报，不能吞
            return SendResult(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
        return SendResult(ok=True, message_id=last_id)

    async def send_image(self, msg: NormalizedMessage, image_b64: str) -> SendResult:
        if self._bot is None:
            return SendResult(ok=False, error="协议端未连接")
        api, params = self._resolve_api(msg)
        try:
            data = await self._bot.call_api(
                api, message=f"[CQ:image,file=base64://{image_b64}]", **params
            )
            mid = data.get("message_id") if isinstance(data, dict) else None
            return SendResult(ok=True, message_id=str(mid) if mid else None)
        except Exception as e:
            return SendResult(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")

    # ---------- 动作 ----------

    def execute(self, action: str, params: dict) -> dict:
        """动作执行。同步签名，内部用已绑定 bot 的异步 call_api。

        注意：真正的动作调用在 core 侧走 gateway 的 /internal/action 端点，
        这里提供同步包装是为本地调用与单测方便。
        """
        if self._bot is None:
            raise RuntimeError("协议端未连接")
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._bot.call_api(action, **(params or {})))
        finally:
            loop.close()

    # ---------- 校验 ----------

    def verify(self, headers: dict) -> bool:
        """网关内部调用不做来源校验；协议端连接由 OneBot access_token 保证。"""
        return True


def image_from_bytes(data: bytes) -> str:
    """图片字节 → base64（供网关拉取 QQ 图片后转交）。"""
    return base64.b64encode(data).decode()
