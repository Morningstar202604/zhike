"""工具调用的运行上下文：谁在问、在哪个渠道、哪个会话。

为什么需要它：
- 「记住我下周三出差」必须知道 visitor_id，否则记到匿名桶里就丢了；
- 「把这条发到群里」必须知道 group_id，否则只能瞎猜；
- 「读一下我刚发的那个文件」必须知道会话里的消息。

设计约束：
- 不可变。pipeline 每轮构造一次，工具只读，避免跨请求串味。
- 全部字段可空。缺信息时工具返回 `need_context` 而不是猜测。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ToolContext:
    visitor_id: str = ""
    session_id: str = ""
    channel_type: str = "webchat"
    channel_user_id: str = ""
    group_id: str | None = None
    group_name: str = ""
    message_text: str = ""
    # 当前消息附带的图片（base64，不含 data: 前缀）
    image_b64: str = ""
    # 会话内最近若干条消息（role/content），工具可据此回答「我刚说了什么」
    history: tuple[dict, ...] = ()
    # 触发者主体（格式同 actions.authz.parse_principal：qq:123 / admin / operator:x）。
    # 写类工具（改文件、发消息、生图）必须靠它做权限判定与审计——
    # 工具层原来完全绕过动作层的档位/审批，等于给提示注入留了一个后门。
    principal: str = ""

    @property
    def chat_key(self) -> str:
        return f"{self.channel_type}:{self.group_id or self.channel_user_id}"

    def with_history(self, history: list[dict]) -> "ToolContext":
        return replace(self, history=tuple(history or ()))

    def describe(self) -> str:
        """给模型看的场景摘要（不要泄露内部 id 之外的东西）。"""
        bits = [f"渠道={self.channel_type}"]
        if self.group_name:
            bits.append(f"群={self.group_name}")
        elif self.group_id:
            bits.append(f"群号={self.group_id}")
        if self.channel_user_id:
            bits.append(f"用户={self.channel_user_id}")
        return "，".join(bits)


EMPTY = ToolContext()
