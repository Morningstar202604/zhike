"""多模型并行择优（小模型集群）：同一问题两个模型各答一版，按规则挑更好的那版。

为什么值得多花一次请求（对标 LLM-as-judge / best-of-n 采样）：
- 单个 flash 模型的状态波动很大：同一句「你是谁」，有时自称 AI，有时写成客服通报；
- 两版并行只多花一次算力，延迟几乎不变（并行），但能稳定挑掉明显的坏答案；
- 用确定性规则打分而不是再叫一个裁判模型：省一次调用，且结果可复现、可单测。

评分只看「人设硬伤 / 客服腔 / 长度 / 信息量」四类，不判断谁的文采更好——
文采是主观的，硬伤是客观的。
"""
from __future__ import annotations

import re

from core import quality

# 长度分档：QQ 闲聊 1-5 句最好，太短没内容，太长像小作文
_IDEAL_MIN, _IDEAL_MAX = 8, 160


def score_answer(text: str, query: str = "", channel: str = "") -> float:
    """给一版答案打分；分越高越好。"""
    t = (text or "").strip()
    if not t:
        return -100.0
    s = 0.0
    for issue in quality.critique(t, query, channel=channel):
        sev = issue.get("severity")
        s -= {"high": 25.0, "mid": 4.0, "low": 1.0}.get(sev, 1.0)
    n = len(t)
    if n < _IDEAL_MIN:
        s -= 2.0
    elif _IDEAL_MIN <= n <= _IDEAL_MAX:
        s += 3.0
    elif n > 240:
        s -= 3.0
    # 有具体信息（数字/专名/标点层次）略加分，空话扣分
    if re.search(r"\d", t):
        s += 1.0
    if re.search(r"(我这就|稍等|正在为你|马上为你)", t):
        s -= 6.0
    # 表情标记与猫味：少量加分，不是硬指标
    if "喵" in t:
        s += 0.5
    return round(s, 3)


def choose(candidates: list[dict], query: str = "", channel: str = "") -> tuple[int, list[float]]:
    """从多版答案里挑一版，返回 (下标, 各版分数)。全空时返回 (-1, [])。"""
    if not candidates:
        return -1, []
    scores = [score_answer(str(c.get("answer") or ""), query, channel) for c in candidates]
    best = max(range(len(scores)), key=lambda i: scores[i])
    if scores[best] <= -100:
        return -1, scores
    return best, scores
