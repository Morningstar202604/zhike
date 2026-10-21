"""示例：接入一个全新渠道需要写什么。

这个 Mock 适配器模拟一个「命令行/Webhook 渠道」，用来证明：
- 新渠道只需实现 5 个成员
- 不需要改 core 任何代码
- 能力可以按需声明（这里只支持文本，不支持群管理）
"""
from __future__ import annotations

import hmac
import os

from gateway.adapters.base import (
    CAP_SEND_TEXT,
    NormalizedMessage,
    SendResult,
)


class MockChannelAdapter:
    """最小可用适配器。生产接入企业微信时照这个骨架写即可。"""

    name = "mock"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    # ---- 1. 声明能力 ----
    def capabilities(self) -> set[str]:
        # 故意只声明文本：core 若试图发图片会得到明确错误，而不是运行时异常
        return {CAP_SEND_TEXT}

    # ---- 2. 收：平台事件 → 统一消息 ----
    def parse(self, raw: dict) -> NormalizedMessage | None:
        if raw.get("event") != "message":
            return None
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        return NormalizedMessage(
            channel=f"mock_{raw.get('kind', 'direct')}",
            chat_id=str(raw.get("chat_id") or "default"),
            sender_id=str(raw.get("from") or "anonymous"),
            text=text,
            display_name=str(raw.get("name") or ""),
            message_id=str(raw.get("id")) if raw.get("id") else None,
            raw=raw,
        )

    # ---- 3. 发 ----
    async def send(self, msg: NormalizedMessage, content: str) -> SendResult:
        # 真实实现里这里是 HTTP 调用/WS 推送
        self.sent.append((msg.chat_id, content))
        return SendResult(ok=True, message_id=f"mock-{len(self.sent)}")

    # ---- 4. 平台特有动作 ----
    def execute(self, action: str, params: dict) -> dict:
        if action == "echo":
            return {"echo": params}
        raise NotImplementedError(f"mock 渠道不支持动作：{action}")

    # ---- 5. 来源校验 ----
    def verify(self, headers: dict) -> bool:
        """共享密钥校验。未配置 MOCK_CHANNEL_TOKEN 时一律拒绝（默认安全）。

        这里刻意不用 `or True` 之类的示例写法：复制这个骨架的人会把漏洞一起复制走。
        """
        expected = os.environ.get("MOCK_CHANNEL_TOKEN", "")
        if not expected:
            return False
        got = (headers or {}).get("x-mock-token", "")
        return hmac.compare_digest(str(got).encode(), expected.encode())


def build() -> MockChannelAdapter:
    """插件入口：manifest 里的 entry 指向这里。"""
    return MockChannelAdapter()
