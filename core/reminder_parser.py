"""提醒时间解析：纯函数，无 DB / HTTP / 全局状态依赖。

从 core/tools.py 抽出（原文件 1896 行，解析逻辑与工具执行混在一起，
无法在不导入整张工具注册表的情况下单测时间语义）。
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM_RE = re.compile(r"[零〇一二两三四五六七八九十]{1,3}")


def cn_num_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """返回中文数字归一化文本及其字符到原文的区间映射。"""
    source = text or ""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _CN_NUM_RE.finditer(source):
        unchanged = source[cursor:match.start()]
        out.append(unchanged)
        spans.extend((i, i + 1) for i in range(cursor, match.start()))
        raw = match.group(0)
        if raw in _CN_DIGITS:
            replacement = str(_CN_DIGITS[raw])
        elif "十" in raw:
            left, _, right = raw.partition("十")
            tens = _CN_DIGITS.get(left, 1) if left else 1
            ones = _CN_DIGITS.get(right, 0) if right else 0
            replacement = str(tens * 10 + ones)
        else:
            replacement = raw
        out.append(replacement)
        spans.extend((match.start(), match.end()) for _ in replacement)
        cursor = match.end()
    out.append(source[cursor:])
    spans.extend((i, i + 1) for i in range(cursor, len(source)))
    return "".join(out), spans


def cn_num(text: str) -> str:
    """把「八」「二十三」「十」这类中文数字换成阿拉伯数字。"""
    return cn_num_with_spans(text)[0]


def original_span(spans: list[tuple[int, int]], start: int, end: int) -> tuple[int, int]:
    """把归一化文本的半开区间映射回原文。"""
    if start >= end or start < 0 or end > len(spans):
        return start, end
    return spans[start][0], spans[end - 1][1]


def parse_due(text: str, now: datetime | None = None) -> datetime | None:
    """把「10 分钟后 / 明天 9 点 / 3 天后」解析成绝对时间。

    只支持明确的时间表达；解析不出来返回 None（由工具向用户追问），
    绝不默认「1 小时后」这种猜测——提醒错了比不提醒更糟。
    """
    # 用户说的「8 点」是本地时间：必须用本地时区构造，否则 UTC+8 会整体偏 8 小时
    now = now or datetime.now().astimezone()
    t = (text or "").strip()
    if not t:
        return None
    # ISO 时间直接用
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    t_num = cn_num(t)
    m = re.search(r"(\d+)\s*(分钟|小时|天|周|秒)后", t_num)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"秒": timedelta(seconds=n), "分钟": timedelta(minutes=n),
                 "小时": timedelta(hours=n), "天": timedelta(days=n), "周": timedelta(weeks=n)}[unit]
        return now + delta

    # 裸钟点（「8点」「早上9点」「下午3点半」）：周期提醒剥掉「每天」后剩的就是这种
    m = re.search(r"^(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[:：点]\s*(半|\d{1,2})?$", t_num.strip())
    if m:
        period, hh, mm = m.groups()
        hour = int(hh)
        minute = 30 if mm == "半" else int(mm or 0)
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if base <= now:
            base += timedelta(days=1)
        return base

    m = re.search(r"(明天|后天|今天|今晚)\s*(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[:：点]\s*(半|\d{1,2})?", t_num)
    if m:
        day_word, period, hh, mm = m.groups()
        days = {"今天": 0, "今晚": 0, "明天": 1, "后天": 2}[day_word]
        hour = int(hh)
        minute = 30 if mm == "半" else int(mm or 0)
        # 中文时段 → 24 小时制（"晚上 9 点"是 21 点，不是 9 点）
        if (period in ("下午", "晚上") or day_word == "今晚") and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        base = (now + timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        return base if base > now else base + timedelta(days=1)
    return None


def parse_clock(text: str) -> tuple[int, int] | None:
    """从文本里抽钟点（「上午9点」「下午3点半」「8:30」），返回 (时, 分)。"""
    t_num = cn_num(text or "")
    m = re.search(r"(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[:：点]\s*(半|\d{1,2})?", t_num)
    if not m:
        return None
    period, hh, mm = m.groups()
    hour = int(hh)
    minute = 30 if mm == "半" else int(mm or 0)
    if period in ("下午", "晚上") and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def next_occurrence(rule: str, hour: int, minute: int, now: datetime) -> datetime | None:
    """按周期规则算下一次触发时刻（含钟点），停机后也只补最近一次。"""
    kind, _, arg = (rule or "").partition(":")
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def at(day_offset: int) -> datetime:
        return (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)

    if kind == "daily":
        return base if base > now else at(1)

    if kind == "weekday":
        # 旧实现把 `base <= now` 写在循环里当条件：今天钟点未过时它恒为 False，
        # 7 次迭代全在试「今天」，周末发起就永远找不到工作日 → 提醒建不上。
        # 正确语义：i=0 试今天，之后逐天往后找，共查 8 天必覆盖下个工作日。
        for i in range(0, 8):
            cand = base if i == 0 else at(i)
            if cand > now and cand.weekday() < 5:
                return cand
        return None

    if kind == "weekly":
        try:
            target = int(arg) % 7
        except ValueError:
            return None
        for i in range(0, 8):
            cand = at(i)
            if cand > now and cand.weekday() == target:
                return cand
        return None

    if kind == "monthly":
        try:
            day = int(arg)
        except ValueError:
            return None
        for i in range(0, 14):
            y, m = now.year, now.month + i
            y += (m - 1) // 12
            m = (m - 1) % 12 + 1
            d = min(day, calendar.monthrange(y, m)[1])
            cand = now.replace(year=y, month=m, day=d,
                               hour=hour, minute=minute, second=0, microsecond=0)
            if cand > now:
                return cand
        return None

    return None


def parse_repeat(text: str) -> tuple[str, str]:
    """从「每天/每周三/每月15号」解析重复规则，返回 (rule, 去掉规则后的文本)。"""
    t = text or ""
    t_num, spans = cn_num_with_spans(t)

    def remove(match: re.Match) -> str:
        start, end = original_span(spans, match.start(), match.end())
        return (t[:start] + t[end:]).strip(" ，,。")

    m = re.search(r"每(?:周|星期)([一二三四五六日天]|[1-7])", t_num)
    if m:
        day_token = m.group(1)
        if day_token.isdigit():
            day = (int(day_token) - 1) % 7
        else:
            idx = "一二三四五六日".find(day_token)
            day = 0 if day_token == "天" else (idx if idx >= 0 else 0)
        return f"weekly:{day}", remove(m)
    m = re.search(r"每月\s*(\d{1,2})\s*[号日]", t_num)
    if m:
        return f"monthly:{int(m.group(1))}", remove(m)
    m = re.search(r"每?个?工作日", t_num)
    if m:
        return "weekday", remove(m)
    m = re.search(r"每天|每日|天天", t_num)
    if m:
        return "daily", remove(m)
    return "", t
