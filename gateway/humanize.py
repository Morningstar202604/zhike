"""拟人化发送与 QQ 表情包渲染（纯函数，供 bot.py 与单测使用）。

设计目标：
1. 防风控：拟打字延迟 + 群聊最小间隔 + 每小时上限——机器人不能"秒回"，更不能刷屏。
2. 表情包：让 LLM 用 [表情:名字] 标记表达情绪，网关翻译成 QQ 内置表情（OneBot face 码）；
   名字来自 QQ 经典小黄脸，用户在聊天里看到的就是 QQ 自带表情，不是外部图片。
3. 图片：[图片:URL] → OneBot image 段（仅当 LLM 真的掌握直链时才会出现）。
"""
from __future__ import annotations

import os
import random
import re
import time

# ---------------------------------------------------------------------------
# QQ 经典小黄脸（OneBot v11 face id）：只收高置信度、常用的表情
# ---------------------------------------------------------------------------
FACES: dict[str, int] = {
    "惊讶": 0, "撇嘴": 1, "发呆": 3, "得意": 4, "流泪": 5, "害羞": 6, "闭嘴": 7,
    "睡": 8, "大哭": 9, "尴尬": 10, "发怒": 11, "调皮": 12, "呲牙": 13, "微笑": 14,
    "难过": 15, "酷": 16, "偷笑": 20, "可爱": 21, "白眼": 22, "傲慢": 23, "困": 25,
    "惊恐": 26, "流汗": 27, "憨笑": 28, "悠闲": 29, "奋斗": 30, "咒骂": 31, "疑问": 32,
    "嘘": 33, "晕": 34, "折磨": 35, "衰": 36, "敲打": 38, "再见": 39, "擦汗": 40,
    "抠鼻": 41, "鼓掌": 42, "坏笑": 44, "鄙视": 46, "委屈": 47, "快哭了": 48,
    "阴险": 49, "亲亲": 50, "吓": 51, "可怜": 52, "赞": 76, "踩": 77, "握手": 78,
    "胜利": 79, "抱拳": 80, "勾引": 81, "拳头": 82, "差劲": 83, "爱你": 84,
    "NO": 85, "OK": 86, "吃瓜": 146, "问号": 148, "滑稽": 178,
}
_ALIASES: dict[str, str] = {
    "哈哈": "呲牙", "笑": "呲牙", "高兴": "呲牙", "笑哭": "憨笑", "捂脸": "憨笑",
    "生气": "发怒", "愤怒": "发怒", "疑惑": "疑问", "问": "疑问", "什么": "问号",
    "吃鲸": "惊讶", "震惊": "惊讶", "冷汗": "流汗", "汗": "流汗", "阴笑": "阴险",
    "点赞": "赞", "牛": "赞", "6": "赞", "六": "赞", "ok": "OK", "yes": "OK",
    "no": "NO", "不行": "NO", "鼓掌": "鼓掌", "斜眼": "滑稽", "滑稽": "滑稽",
    "哭": "大哭", "泪": "流泪", "困了": "困", "睡觉": "睡", "晚安": "睡",
}

_FACE_RE = re.compile(r"[\[（(【]\s*表情\s*[:：]\s*([^\]）)】]{1,12}?)\s*[\]）)】]")
_IMG_RE = re.compile(r"[\[（(【]\s*(?:图片|图)\s*[:：]\s*(https?://[^\]）)】\s]{4,})\s*[\]）)】]")
# 裸格式：LLM 天然爱写 [得意] [滑稽]——只认 FACES 里的精确名字，白名单外一律不动
_BARE_FACE_RE = re.compile(r"\[([^\[\]:]{1,4})\]")


def render_faces(text: str) -> str:
    """[表情:名] / (表情:名) / [名] → [CQ:face,id=N]；未知名字整个删除（绝不把标记原文发出去）。

    裸格式只做白名单精确匹配（名字必须原样出现在 FACES 里），避免误伤普通文本。
    """

    def sub(m: re.Match) -> str:
        name = m.group(1).strip()
        key = name if name in FACES else _ALIASES.get(name) or _ALIASES.get(name.lower())
        fid = FACES.get(key) if key else None
        return f"[CQ:face,id={fid}]" if fid is not None else ""

    def sub_bare(m: re.Match) -> str:
        fid = FACES.get(m.group(1).strip())
        return f"[CQ:face,id={fid}]" if fid is not None else m.group(0)

    out = _FACE_RE.sub(sub, text or "")
    return _BARE_FACE_RE.sub(sub_bare, out)


def render_images(text: str) -> str:
    """[图片:URL] → [CQ:image,file=URL]（NapCat 支持直链下发）。"""

    def sub(m: re.Match) -> str:
        return f"[CQ:image,file={m.group(1)}]"

    return _IMG_RE.sub(sub, text or "")


def render(text: str) -> str:
    """统一出口：先图后表情，网关发送前调用。"""
    return render_images(render_faces(text))


# ---------------------------------------------------------------------------
# 拟人化节奏：别秒回、别刷屏、别把群搞疯
# ---------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v >= 0 else default
    except ValueError:
        return default


TYPING_CPS = _env_float("GATEWAY_TYPING_CPS", 14.0)      # 拟打字速度：每秒约 14 字
DELAY_CAP = _env_float("GATEWAY_DELAY_CAP", 22.0)         # 单条延迟上限（秒）


def send_delay(text_len: int, is_group: bool, rng=random.random) -> float:
    """发送前拟人延迟 = 打字时间 + 抖动（群聊再多一层"犹豫"）。

    依据：真人不可能 0.2 秒回一大段话；自动化检测最敏感的就是规律性秒回。
    """
    base = 0.8 + max(0, text_len) / max(TYPING_CPS, 1.0)
    jitter = rng() * 1.6
    if is_group:
        base += rng() * 3.0
    return round(min(DELAY_CAP, base + jitter), 2)


class GroupThrottle:
    """群聊频控：随机化最小间隔 + 每小时上限。超过上限的回复**静默丢弃**——
    宁可少说一句，也不能被检测出自动化把号搞没。"""

    def __init__(self, min_gap: float = 10.0, hourly_cap: int = 18, now=time.monotonic):
        self.min_gap = max(0.0, min_gap)
        self.cap = max(0, hourly_cap)
        self._sent: dict[str, list[float]] = {}
        self._now = now

    def wait_seconds(self, chat_id: str) -> float:
        """距上次发送还需等多久（秒）；-1 表示本小时已达上限，应静默放弃。"""
        now = self._now()
        stamps = [t for t in self._sent.get(chat_id, []) if now - t < 3600]
        self._sent[chat_id] = stamps
        if self.cap and len(stamps) >= self.cap:
            return -1.0
        if not stamps:
            return 0.0
        elapsed = now - max(stamps)
        # 间隔本身带 ±20% 随机化：固定间隔同样是可以被检测的节律
        need = self.min_gap * (0.8 + random.random() * 0.4)
        return max(0.0, need - elapsed)

    def record(self, chat_id: str) -> None:
        self._sent.setdefault(chat_id, []).append(self._now())
