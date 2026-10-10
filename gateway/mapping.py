"""OneBot v11 事件 → 核心摄取请求的纯函数映射（不依赖 nonebot，可独立单测）。"""
import re
from dataclasses import dataclass

MAX_REPLY_CHARS = 4500


@dataclass
class GatewayEvent:
    channel_type: str
    chat_id: str
    user_id: str
    text: str
    display_name: str = ""
    message_id: str | None = None
    image_url: str = ""

    @property
    def should_reply(self) -> bool:
        return bool(self.text)


def _extract_text(message, self_id: str) -> tuple[str, bool, str]:
    """提取纯文本 / 是否 @机器人 / 首个图片 URL（CQ 码字符串与分段两种格式统一处理）。"""
    if isinstance(message, str):
        at_self = f"[CQ:at,qq={self_id}]" in message
        image_url = ""
        m = re.search(r"\[CQ:image,[^\]]*url=([^,\]]+)\]", message)
        if m:
            image_url = m.group(1).replace("&amp;", "&")
        text = re.sub(r"\[CQ:[^\]]*\]", "", message)
        return text, at_self, image_url
    parts: list[str] = []
    at_self = False
    image_url = ""
    for seg in message or []:
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(data.get("text", ""))
        elif seg_type == "at" and str(data.get("qq", "")) == str(self_id):
            at_self = True
        elif seg_type == "image":
            parts.append("[图片]")
            if not image_url:
                image_url = str(data.get("url") or "")
    return "".join(parts), at_self, image_url


def from_onebot_v11(event: dict) -> GatewayEvent | None:
    if event.get("post_type") != "message":
        return None
    self_id = str(event.get("self_id", ""))
    user_id = str(event.get("user_id", ""))
    if not user_id or user_id == self_id:
        return None
    message_type = event.get("message_type")
    if message_type not in ("group", "private"):
        return None
    text, at_self, image_url = _extract_text(event.get("message"), self_id)
    # 适配器已把命中的 @ 段从 message 中剥离，群聊判定需结合事件级 to_me（matcher 已按 to_me 过滤）
    at_me = at_self or bool(event.get("to_me"))
    if message_type == "group" and not at_me:
        return None
    text = text.strip()
    if not text:
        return None
    chat_id = str(event.get("group_id", "")) if message_type == "group" else user_id
    return GatewayEvent(
        channel_type=f"qq_{message_type}",
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        display_name=str(event.get("sender", {}).get("nickname", "") or ""),
        message_id=str(event.get("message_id", "")) or None,
        image_url=image_url,
    )


def should_reply(event: GatewayEvent) -> bool:
    return bool(event.text)


def ingest_payload(event: GatewayEvent) -> dict:
    return {
        "channel_type": event.channel_type,
        "channel_user_id": event.user_id,
        "group_id": event.chat_id if event.channel_type == "qq_group" else None,
        "text": event.text,
        "display_name": event.display_name,
        "message_id": event.message_id,
    }


def reply_segments(text: str, limit: int = MAX_REPLY_CHARS) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    return [text[i:i + limit] for i in range(0, len(text), limit)]
