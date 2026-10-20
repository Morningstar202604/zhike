"""渠道适配器接口：接入新平台只需实现这一组方法，不改内核。

设计目标（对应计划 10.1）：
- 收：把平台原始事件解析成统一的 NormalizedMessage
- 发：把文本/内容发到指定目标
- 动作：执行平台特有操作（OneBot 的 call_api 等）
- 能力声明：不支持的动作提前告知，避免运行时才炸

适配器只做协议转换，不做业务判断、不做权限判定——那些都在 core。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class NormalizedMessage:
    """跨平台统一消息。所有适配器都必须产出这个结构。"""
    channel: str                  # qq_group / qq_private / wecom / webchat ...
    chat_id: str                  # 群号 / 用户 id / 会话 key
    sender_id: str                # 发送者标识
    text: str = ""
    images: list[str] = field(default_factory=list)   # base64 或 URL
    display_name: str = ""
    message_id: str | None = None
    reply_to: str | None = None
    raw: dict = field(default_factory=dict)           # 原始事件，便于排障

    @property
    def is_group(self) -> bool:
        return self.channel.endswith("_group") or self.channel.endswith("_chat")

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.images)

    @property
    def identity(self) -> str:
        """限流与记忆用的身份键。"""
        return f"{self.channel}:{self.sender_id}"

    @property
    def session_key(self) -> str:
        """会话维度：群聊按群聚合，私聊按人。"""
        return f"{self.channel}:{self.chat_id}"


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    raw: Any = None


# 标准能力名：适配器按需声明，core 据此判断能否调用
CAP_SEND_TEXT = "send.text"
CAP_SEND_IMAGE = "send.image"
CAP_SEND_FILE = "send.file"
CAP_DELETE_MSG = "msg.delete"
CAP_GROUP_ADMIN = "group.admin"
CAP_GROUP_MEMBER_LIST = "group.member_list"
CAP_MESSAGE_HISTORY = "msg.history"

ALL_CAPABILITIES = {
    CAP_SEND_TEXT, CAP_SEND_IMAGE, CAP_SEND_FILE, CAP_DELETE_MSG,
    CAP_GROUP_ADMIN, CAP_GROUP_MEMBER_LIST, CAP_MESSAGE_HISTORY,
}


@runtime_checkable
class ChannelAdapter(Protocol):
    """渠道适配器契约。

    实现者只需提供这 5 个成员；不需要继承任何基类（结构化类型）。
    """

    name: str

    def capabilities(self) -> set[str]:
        """声明本平台支持哪些标准能力。"""
        ...

    def parse(self, raw: dict) -> NormalizedMessage | None:
        """平台原始事件 → 统一消息。返回 None 表示忽略（如非消息事件、自己发的）。"""
        ...

    async def send(self, msg: NormalizedMessage, content: str) -> SendResult:
        """把回复发回来源。"""
        ...

    def execute(self, action: str, params: dict) -> dict:
        """执行平台特有动作（同步描述；异步实现请包装）。"""
        ...

    def verify(self, headers: dict) -> bool:
        """校验入站请求来源（签名/令牌）。网关内部调用可返回 True。"""
        ...


class UnsupportedCapability(Exception):
    """适配器不支持该能力。core 捕获后返回明确错误，而不是运行时崩。"""


def require_capability(adapter: ChannelAdapter, cap: str) -> None:
    caps = adapter.capabilities()
    if cap not in caps:
        raise UnsupportedCapability(
            f"渠道 {getattr(adapter, 'name', '?')} 不支持能力 {cap}"
        )


def adapter_info(adapter: ChannelAdapter) -> dict:
    """给控制台/注册表看的适配器描述。"""
    return {
        "name": getattr(adapter, "name", "unknown"),
        "capabilities": sorted(adapter.capabilities()),
        "supports": {c: c in adapter.capabilities() for c in sorted(ALL_CAPABILITIES)},
    }
