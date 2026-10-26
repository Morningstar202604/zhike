"""OneBot v11 事件 → 核心摄取请求的纯函数映射（不依赖 nonebot，可独立单测）。"""
import re
from dataclasses import dataclass

MAX_REPLY_CHARS = 4500

# OneBot face id → 常见中文名（与 gateway/humanize.py 的 FACES 同源）
_FACE_NAMES: dict[str, str] = {
    "0": "惊讶", "1": "撇嘴", "3": "发呆", "4": "得意", "5": "流泪", "6": "害羞",
    "7": "闭嘴", "8": "睡", "9": "大哭", "10": "尴尬", "11": "发怒", "12": "调皮",
    "13": "呲牙", "14": "微笑", "15": "难过", "16": "酷", "19": "抓狂", "20": "偷笑",
    "21": "可爱", "22": "白眼", "23": "傲慢", "24": "饥饿", "25": "困", "26": "惊恐",
    "27": "流汗", "28": "憨笑", "29": "悠闲", "30": "奋斗", "31": "咒骂", "32": "疑问",
    "33": "嘘", "34": "晕", "35": "折磨", "36": "衰", "37": "骷髅", "38": "敲打",
    "39": "再见", "40": "擦汗", "41": "抠鼻", "42": "鼓掌", "43": "糗大了", "44": "坏笑",
    "46": "鄙视", "47": "委屈", "48": "快哭了", "49": "阴险", "50": "亲亲", "51": "吓",
    "52": "可怜", "56": "示爱", "57": "抱抱", "74": "太阳", "75": "月亮", "76": "赞",
    "77": "踩", "78": "握手", "79": "胜利", "80": "抱拳", "81": "勾引", "82": "拳头",
    "83": "差劲", "84": "爱你", "85": "NO", "86": "OK", "88": "转圈", "89": "猪头",
    "146": "吃瓜", "148": "问号", "171": "让我想想", "178": "滑稽",
}


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
        # CQ 码：把表情/语音等转成可读标记，其余结构化段直接去掉
        out = re.sub(r"\[CQ:face,[^\]]*id=([0-9]+)[^\]]*\]",
                     lambda m: f"[表情:{_FACE_NAMES.get(m.group(1), 'face' + m.group(1))}]", message)
        out = re.sub(r"\[CQ:mface,[^\]]*summary=([^,\]]+)[^\]]*\]",
                     lambda m: f"[表情:{m.group(1).strip('[]')}]", out)
        out = re.sub(r"\[CQ:record[^\]]*\]", "[语音]", out)
        out = re.sub(r"\[CQ:video[^\]]*\]", "[视频]", out)
        text = re.sub(r"\[CQ:[^\]]*\]", "", out)
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
        elif seg_type == "face":
            # 小黄脸：id → 名字（模型看得懂「[表情:呲牙]」，看不懂 [CQ:face,id=13]）
            fid = str(data.get("id") or "")
            name = _FACE_NAMES.get(fid) or f"face{fid}"
            parts.append(f"[表情:{name}]")
        elif seg_type == "mface":
            # 商城表情/超级表情：summary 就是它的名字（如「[动画表情]」或「猫猫头」）
            summary = str(data.get("summary") or "").strip("[]")
            parts.append(f"[表情:{summary or '动画表情'}]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "file":
            parts.append(f"[文件:{str(data.get('name') or '')[:40]}]")
        elif seg_type == "reply":
            # 引用段：把被引用消息 id 标记出来，便于上层知道这条是"回某句"
            parts.append(f"[引用:{data.get('id') or ''}]")
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
