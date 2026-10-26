"""工具注册表：内置实现 + DB 数据驱动开关。

**真实性原则**：工具要么真的做成了，要么明确返回 `connected: false` / `found: false`
并说明「未接入」。绝不编造订单号、物流状态、网页内容或文件内容——
管线看到 `found/connected` 为 False 会标记升级并给出「暂时查不了」，绝不伪造数据。

**同步 vs 异步**：历史上工具全是同步函数（`_make_llm_reply` 直接 `tools[name](**args)`）。
新增的网络/QQ/文件类工具天然是异步的，所以这里定义了两种注册方式：
- `register_builtin(name, fn, async_=False)`：同步函数保持原调用路径零改动；
- 异步工具统一走 `invoke(name, args, ctx)`——调用方（pipeline）本来就是 async，
  用 `run_sync` 兜住同步工具即可，不需要给每个旧工具写一份 async 包装。

**上下文**：`ToolContext` 携带 visitor/session/群/图片/history。
没有上下文时工具返回 `need_context`，不瞎猜（比如「发到群里」必须知道群号）。
"""
from __future__ import annotations

import inspect
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from core import redact, retrieval, store
from core.tool_context import EMPTY, ToolContext


# ============================================================
# 结果契约
# ============================================================

def ok_result(tool: str, note: str, **extra: Any) -> dict:
    return {"tool": tool, "connected": True, "found": True, "note": note, **extra}


def miss_result(tool: str, note: str, **extra: Any) -> dict:
    return {"tool": tool, "connected": True, "found": False, "note": note, **extra}


def empty_result(tool: str, note: str, **extra: Any) -> dict:
    """正常但为空的结果（「没有待办」「还没记过你」）。

    必须与 miss_result 分开：这类答案是事实本身，不是知识库缺资料，
    管线不能因此开升级工单——否则用户每问一次空列表就多一张票。
    """
    return {"tool": tool, "connected": True, "found": False, "status": "empty",
            "note": note, **extra}


def off_result(tool: str, note: str, **extra: Any) -> dict:
    return {"tool": tool, "connected": False, "found": False, "note": note, **extra}


def need_ctx(tool: str, what: str) -> dict:
    return {"tool": tool, "connected": True, "found": False, "status": "need_context",
            "note": f"缺少必要信息：{what}。请向用户确认后再试。"}


# ============================================================
# 业务工具：订单 / 退款 / 政策（保持原行为，未配置即明说）
# ============================================================

def _order_endpoint() -> tuple[str, str]:
    return ((os.environ.get("ORDER_QUERY_URL") or "").strip(),
            (os.environ.get("ORDER_QUERY_TOKEN") or "").strip())


def _refund_endpoint() -> tuple[str, str]:
    return ((os.environ.get("REFUND_API_URL") or "").strip(),
            (os.environ.get("REFUND_API_TOKEN") or "").strip())


def _not_connected(tool: str, hint: str) -> dict:
    return off_result(tool, f"{hint}暂时查不了。")


def tool_query_order(order_id: str) -> dict:
    url_tpl, token = _order_endpoint()
    if not url_tpl:
        return _not_connected("query_order", "订单查询暂未接入自助渠道，")
    url = url_tpl.replace("{order_id}", order_id)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"tool": "query_order", "connected": True, "found": False,
                "error": f"{type(e).__name__}", "note": "订单系统暂时不可用。"}
    status = str(data.get("status") or "")
    tracking = str(data.get("tracking_no") or "")
    carrier = str(data.get("carrier") or "")
    note = f"订单 {order_id} 当前状态：{status}。"
    if carrier or tracking:
        note += f" 物流：{carrier} {tracking}。"
    return {"tool": "query_order", "connected": True, "found": True, "order_id": order_id,
            "status": status, "carrier": carrier, "tracking_no": tracking,
            "eta": str(data.get("eta") or ""), "note": note}


def tool_initiate_refund(order_id: str, reason: str = "") -> dict:
    url, token = _refund_endpoint()
    if not url:
        return _not_connected("initiate_refund", "退款申请暂未接入自助渠道，")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json={"order_id": order_id, "reason": reason or "未说明"},
                               headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"tool": "initiate_refund", "connected": True, "found": False,
                "error": f"{type(e).__name__}", "note": "退款系统暂时不可用。"}
    refund_id = str(data.get("refund_id") or "")
    status = str(data.get("status") or "submitted")
    return {"tool": "initiate_refund", "connected": True, "found": True, "order_id": order_id,
            "refund_id": refund_id, "status": status,
            "note": f"退款申请已提交（单号 {refund_id or '待生成'}），当前状态：{status}。"}


def tool_lookup_policy(topic: str) -> dict:
    hits = retrieval.retrieve(topic, top_k=2)
    entries = [{"id": h["id"], "title": h["title"], "answer": h["answer"]} for h in hits]
    if entries:
        note = "；".join(f"【{e['title']}】{e['answer']}（依据 {e['id']}）" for e in entries)
        return ok_result("lookup_policy", note, topic=topic, entries=entries)
    return miss_result("lookup_policy", f"资料里没有与「{topic}」相关的内容。", topic=topic, entries=[])


# ============================================================
# 上下文工具：读当前对话 / 记忆
# ============================================================

def tool_current_context(ctx: ToolContext, **_kw) -> dict:
    """让模型看到「我刚才说了什么、我现在在哪个群」。"""
    if not ctx.session_id and not ctx.history:
        return need_ctx("current_context", "当前会话")
    tail = list(ctx.history)[-8:]
    lines = []
    for m in tail:
        role = {"visitor": "用户", "agent": "我", "operator": "真人"}.get(m.get("role"), str(m.get("role")))
        content = str(m.get("content") or "").replace("\n", " ")[:120]
        if content:
            lines.append(f"{role}：{content}")
    note = "；".join(lines) if lines else "当前会话还没有历史消息。"
    return ok_result("current_context", note, scene=ctx.describe(),
                     message_count=len(ctx.history))


def tool_recall_memory(ctx: ToolContext, key: str = "", **_kw) -> dict:
    """读长期记忆。key 为空时返回全部键值。"""
    if not ctx.visitor_id:
        return need_ctx("recall_memory", "访客身份")
    mem = store.get_memory(ctx.visitor_id) or {}
    if not mem:
        return empty_result("recall_memory", "我这儿还没有关于你的记录。")
    if key:
        val = mem.get(key)
        if val is None:
            return empty_result("recall_memory", f"没有「{key}」这条记录。", keys=list(mem)[:20])
        return ok_result("recall_memory", f"{key}：{val}", key=key, value=val)
    brief = "；".join(f"{k}：{str(v)[:80]}" for k, v in list(mem.items())[:12])
    return ok_result("recall_memory", brief, keys=list(mem))


def tool_remember(ctx: ToolContext, key: str, value: str, **_kw) -> dict:
    """写长期记忆（秘书要记住「我下周三出差」这类事）。"""
    if not ctx.visitor_id:
        return need_ctx("remember", "访客身份")
    key = (key or "").strip()[:40]
    value = (value or "").strip()[:500]
    if not key or not value:
        return need_ctx("remember", "要记住的键与内容")
    store.set_memory(ctx.visitor_id, key, value)
    return ok_result("remember", f"记住了：{key} = {value}", key=key)


# ============================================================
# 提醒 / 待办
# ============================================================

def _parse_due(text: str, now: datetime | None = None) -> datetime | None:
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

    m = re.search(r"(\d+)\s*(分钟|小时|天|周|秒)后", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"秒": timedelta(seconds=n), "分钟": timedelta(minutes=n),
                 "小时": timedelta(hours=n), "天": timedelta(days=n), "周": timedelta(weeks=n)}[unit]
        return now + delta

    # 裸钟点（「8点」「早上9点」「下午3点半」）：周期提醒剥掉「每天」后剩的就是这种
    m = re.search(r"^(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[:：点]\s*(半|\d{1,2})?$", t.strip())
    if m:
        period, hh, mm = m.groups()
        hour = int(hh)
        minute = 30 if mm == "半" else int(mm or 0)
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if base <= now:
            base += timedelta(days=1)
        return base

    m = re.search(r"(明天|后天|今天|今晚)\s*(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[:：点]\s*(\d{1,2})?", t)
    if m:
        day_word, period, hh, mm = m.groups()
        days = {"今天": 0, "今晚": 0, "明天": 1, "后天": 2}[day_word]
        hour = int(hh)
        # 中文时段 → 24 小时制（"晚上 9 点"是 21 点，不是 9 点）
        if (period in ("下午", "晚上") or day_word == "今晚") and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        base = (now + timedelta(days=days)).replace(
            hour=hour, minute=int(mm or 0), second=0, microsecond=0)
        return base if base > now else base + timedelta(days=1)
    return None


def _parse_clock(text: str) -> tuple[int, int] | None:
    """从文本里抽钟点（「上午9点」「下午3点半」「8:30」），返回 (时, 分)。"""
    m = re.search(r"(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[:：点]\s*(半|\d{1,2})?", text or "")
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


def _next_occurrence(rule: str, hour: int, minute: int, now: datetime) -> datetime | None:
    """按周期规则算下一次触发时刻（含钟点），停机后也只补最近一次。"""
    import calendar as _cal

    kind, _, arg = (rule or "").partition(":")
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def at(day_offset: int) -> datetime:
        d = (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        return d

    if kind == "daily":
        return base if base > now else at(1)

    if kind == "weekday":
        for i in range(1, 8):
            cand = at(i) if base <= now else base
            if i == 1 and base > now:
                cand = base
            if cand.weekday() < 5:
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
            d = min(day, _cal.monthrange(y, m)[1])
            cand = now.replace(year=y, month=m, day=d,
                               hour=hour, minute=minute, second=0, microsecond=0)
            if cand > now:
                return cand
        return None

    return None


_REPEAT_PATTERNS = [
    (r"每天|每日|天天", "daily"),
    (r"每个?工作日", "weekday"),
    (r"每周([一二三四五六日天])", None),      # weekly:N
    (r"每月\s*(\d{1,2})\s*[号日]", None),   # monthly:D
]


def _parse_repeat(text: str) -> tuple[str, str]:
    """从「每天/每周三/每月15号」解析重复规则，返回 (rule, 去掉规则后的文本)。"""
    t = text or ""
    m = re.search(r"每(?:周|星期)([一二三四五六日天])", t)
    if m:
        idx = "一二三四五六日".find(m.group(1))
        day = 0 if m.group(1) == "天" else (idx if idx >= 0 else 0)
        return f"weekly:{day}", (t[:m.start()] + t[m.end():]).strip(" ，,。")
    m = re.search(r"每月\s*(\d{1,2})\s*[号日]", t)
    if m:
        return f"monthly:{int(m.group(1))}", (t[:m.start()] + t[m.end():]).strip(" ，,。")
    if re.search(r"每?个?工作日", t):
        return "weekday", re.sub(r"每?个?工作日", "", t).strip(" ，,。")
    if re.search(r"每天|每日|天天", t):
        return "daily", re.sub(r"每天|每日|天天", "", t).strip(" ，,。")
    return "", t


def tool_remind(ctx: ToolContext, text: str, when: str, **_kw) -> dict:
    """加一条提醒。when 支持「10 分钟后 / 明天 9 点 / ISO」。"""
    if not ctx.visitor_id:
        return need_ctx("remind", "访客身份")
    raw_text = (text or "").strip()
    if not raw_text:
        return need_ctx("remind", "要提醒的内容")
    # 周期提醒：「每天 8 点提醒我喝水」——规则从 text/when 两处都可能出现
    rule, cleaned = _parse_repeat(raw_text)
    when_clean = when or ""
    if not rule:
        rule, when_clean = _parse_repeat(when_clean)
    text = (cleaned or raw_text)[:200]
    if rule:
        # 周期提醒：钟点缺省 9:00。必须按规则算「下一次」，不能拿今天的钟点顶替
        # （"每月15号"若今天是 26 号，直接取 10:00 会立刻误触发）。
        hm = _parse_clock(when_clean) or _parse_clock(raw_text) or (9, 0)
        due = _next_occurrence(rule, hm[0], hm[1], datetime.now().astimezone())
    else:
        due = _parse_due(when_clean)
    if due is None:
        return need_ctx("remind", f"提醒时间（「{when}」没看懂，请说清几点或多久后）")
    rec = store.reminder_add(
        visitor_id=ctx.visitor_id, chat_key=ctx.chat_key, text=text,
        due_at=due.isoformat(), channel_type=ctx.channel_type,
        group_id=ctx.group_id, channel_user_id=ctx.channel_user_id,
        repeat_rule=rule,
    )
    local = due.astimezone().strftime("%m-%d %H:%M")
    suffix = {"daily": "，每天这个点", "weekday": "，工作日每天",
              "weekly": "，每周同一天", "monthly": "，每月同一天"}.get(
        rule.split(":")[0], "")
    return ok_result("remind", f"好，{local} 提醒你：{text}{suffix}", reminder_id=rec.get("id"),
                     due_at=due.isoformat(), repeat_rule=rule)


def tool_list_reminders(ctx: ToolContext, **_kw) -> dict:
    if not ctx.visitor_id:
        return need_ctx("list_reminders", "访客身份")
    rows = store.reminder_list(ctx.visitor_id)
    if not rows:
        return empty_result("list_reminders", "你现在没有待提醒的事。")
    lines = []
    for r in rows[:10]:
        try:
            local = datetime.fromisoformat(r["due_at"]).astimezone().strftime("%m-%d %H:%M")
        except ValueError:
            local = r["due_at"]
        lines.append(f"#{r['id']} {local} {r['text']}")
    return ok_result("list_reminders", "；".join(lines), count=len(rows))


def tool_cancel_reminder(ctx: ToolContext, reminder_id: str, **_kw) -> dict:
    if not ctx.visitor_id:
        return need_ctx("cancel_reminder", "访客身份")
    try:
        rid = int(str(reminder_id).lstrip("#"))
    except ValueError:
        return need_ctx("cancel_reminder", "提醒编号")
    rec = store.reminder_get(rid)
    if not rec or rec["visitor_id"] != ctx.visitor_id:
        return empty_result("cancel_reminder", f"找不到 #{rid} 这条提醒。")
    if not store.reminder_cancel(rid):
        return empty_result("cancel_reminder", f"#{rid} 已经发过或已取消。")
    return ok_result("cancel_reminder", f"已取消 #{rid}。")


# ============================================================
# 网页：读页面 / 搜网
# ============================================================

async def tool_read_webpage(url: str, max_chars: str = "2000", **_kw) -> dict:
    """抓取一个网页并转成纯文本。

    SSRF 防护在 core.url_guard：只允许公网 http/https，且校验每一跳重定向。
    """
    from core import url_guard

    try:
        safe_url = url_guard.check_url(url)
    except url_guard.UnsafeUrl as e:
        return miss_result("read_webpage", f"这个链接不能打开：{e}", url=url)

    limit = 6000
    try:
        limit = max(200, min(int(str(max_chars)), 6000))
    except (TypeError, ValueError):
        pass

    try:
        from core.http_pool import get_client
        client = get_client(timeout=15)
        resp = await client.get(safe_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Zhike/1.0)",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        resp.raise_for_status()
        raw = resp.text
    except httpx.HTTPError as e:
        return miss_result("read_webpage", f"打不开这个页面（{type(e).__name__}）。", url=safe_url)

    text = _html_to_text(raw)[:limit]
    if not text.strip():
        return miss_result("read_webpage", "页面打开了，但没有可读文字（可能是纯图或需要登录）。",
                           url=safe_url)
    return ok_result("read_webpage", text, url=safe_url, chars=len(text))


def _html_to_text(raw: str) -> str:
    import html as _html

    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def tool_web_search(query: str, top_k: str = "4", **_kw) -> dict:
    from core import websearch

    if not websearch.available():
        return off_result("web_search", "联网搜索还没配置，先用我已有的资料回答。")
    try:
        n = max(1, min(int(str(top_k)), 8))
    except (TypeError, ValueError):
        n = 4
    try:
        result = await websearch.search(query, top_k=n)
    except Exception as e:
        return miss_result("web_search", f"搜索失败了（{type(e).__name__}）。", query=query)
    if not result.get("ok"):
        return miss_result("web_search", f"没搜到结果：{result.get('error') or '空结果'}。", query=query)
    items = result.get("results") or []
    if not items:
        return miss_result("web_search", f"没搜到「{query}」的相关内容。", query=query)
    lines = []
    for i, r in enumerate(items, 1):
        title = redact.mask_text(str(r.get("title") or ""))[:80]
        snippet = redact.mask_text(str(r.get("content") or r.get("snippet") or ""))[:200]
        link = str(r.get("url") or "")
        lines.append(f"{i}. {title} — {snippet}（{link}）")
    return ok_result("web_search", "\n".join(lines), query=query, count=len(items))


# ============================================================
# 文件：列表 / 读 / 找（走执行器策略，越权即拒）
# ============================================================

async def tool_list_files(path: str, depth: str = "1", **_kw) -> dict:
    from core import executor_client

    try:
        d = int(str(depth))
    except (TypeError, ValueError):
        d = 1
    try:
        data = await executor_client.invoke("fs.list", {"path": path, "depth": max(1, min(d, 3))})
    except executor_client.ExecutorOffline:
        return off_result("list_files", "文件工具没启用（执行器未运行），我没法读本机文件。")
    except executor_client.ExecutorDenied as e:
        return miss_result("list_files", f"没有权限访问这个路径：{e}", path=path)
    except executor_client.ExecutorError as e:
        return miss_result("list_files", f"列目录失败：{e}", path=path)

    entries = data.get("entries") or data.get("items") or []
    if not entries:
        return miss_result("list_files", "这个目录是空的或不存在。", path=path)
    lines = []
    for e in entries[:60]:
        name = e.get("name") or e.get("path") or "?"
        kind = "目录" if e.get("is_dir") or e.get("type") == "dir" else "文件"
        size = e.get("size")
        lines.append(f"{kind} {name}" + (f" ({size}B)" if size else ""))
    return ok_result("list_files", "\n".join(lines), path=path, count=len(entries))


async def tool_read_file(path: str, max_chars: str = "4000", **_kw) -> dict:
    from core import executor_client

    try:
        limit = max(200, min(int(str(max_chars)), 20000))
    except (TypeError, ValueError):
        limit = 4000
    try:
        data = await executor_client.invoke("fs.read", {"path": path})
    except executor_client.ExecutorOffline:
        return off_result("read_file", "文件工具没启用（执行器未运行），我没法读本机文件。")
    except executor_client.ExecutorDenied as e:
        return miss_result("read_file", f"没有权限读这个文件：{e}", path=path)
    except executor_client.ExecutorError as e:
        return miss_result("read_file", f"读取失败：{e}", path=path)

    content = str(data.get("content") or "")
    truncated = len(content) > limit
    body = redact.mask_text(content[:limit])
    note = f"（{path}，{len(content)} 字符{'，已截断' if truncated else ''}）\n{body}"
    return ok_result("read_file", note, path=path, truncated=truncated)


async def tool_write_file(path: str, content: str, append: str = "", **_kw) -> dict:
    """写文件。走执行器策略：allow_roots 之外一律拒绝。

    append=true 时追加而不是覆盖——秘书场景里「把这条记到笔记里」
    远比「覆盖整个文件」常见，默认覆盖是危险的。
    """
    from core import executor_client

    body = content or ""
    if not body:
        return need_ctx("write_file", "要写入的内容")
    do_append = str(append).lower() in ("1", "true", "yes", "on", "追加")
    if do_append:
        # 执行器只有整文件写；追加 = 读旧内容 + 拼接。
        # 「读失败」必须区分「文件不存在」与「超限/执行器故障」：
        # 之前一律当作新建，向 1MB 以上的文件追加会把原文件静默截断成新内容。
        try:
            old = await executor_client.invoke("fs.read", {"path": path})
            prev = str(old.get("content") or "")
            if prev and not prev.endswith("\n"):
                prev += "\n"
            body = prev + body
        except executor_client.ExecutorDenied as e:
            return miss_result("write_file", f"没有权限读这个路径：{e}", path=path)
        except executor_client.ExecutorOffline:
            return off_result("write_file", "文件工具没启用（执行器未运行）。")
        except executor_client.ExecutorError as e:
            # 只有「路径不存在 / 不是文件」可以按新建处理；超限、超时等
            # 其它故障必须失败返回，否则会把原文件静默截断成新内容。
            msg = str(e)
            if not ("不存在" in msg or "不是文件" in msg):
                return miss_result("write_file", f"追加前读取失败：{e}", path=path)
    try:
        data = await executor_client.invoke(
            "fs.write", {"path": path, "content": body})
    except executor_client.ExecutorOffline:
        return off_result("write_file", "文件工具没启用（执行器未运行）。")
    except executor_client.ExecutorDenied as e:
        return miss_result("write_file", f"没有权限写这个路径：{e}", path=path)
    except executor_client.ExecutorError as e:
        return miss_result("write_file", f"写入失败：{e}", path=path)
    size = data.get("bytes") or data.get("size") or len(body.encode("utf-8"))
    verb = "追加" if do_append else "写入"
    return ok_result("write_file", f"已{verb} {path}（{size} 字节）。", path=path, append=do_append)


# ============================================================
# 图片 / 表情包
# ============================================================

async def tool_describe_image(ctx: ToolContext, source: str = "", question: str = "", **_kw) -> dict:
    """看图。source 为空表示「用户刚发的那张」。"""
    import base64

    from core import config, llm

    cfg = config.get_config()
    vision_model = ((cfg.get("multimodal", {}) or {}).get("vision_model", "") or "").strip()         or str((cfg.get("llm") or {}).get("model") or "")
    if not vision_model:
        return off_result("describe_image", "还没配视觉模型，我看不了图。")
    if not (cfg.get("llm") or {}).get("has_key"):
        return off_result("describe_image", "还没有配模型 Key，看不了图。")

    raw = (source or "").strip()
    b64 = ""
    if raw.startswith("http://") or raw.startswith("https://"):
        from core import url_guard
        try:
            safe = url_guard.check_url(raw)
        except url_guard.UnsafeUrl as e:
            return miss_result("describe_image", f"这个图片地址不能访问：{e}")
        try:
            from core.http_pool import get_client
            resp = await get_client(timeout=15).get(safe)
            resp.raise_for_status()
            if len(resp.content) > 8_000_000:
                return miss_result("describe_image", "图片太大了（超过 8MB）。")
            b64 = base64.b64encode(resp.content).decode()
        except httpx.HTTPError as e:
            return miss_result("describe_image", f"图片下载失败（{type(e).__name__}）。")
    elif raw.startswith("data:"):
        b64 = raw.split(",", 1)[-1]
    else:
        b64 = ctx.image_b64 or raw

    if not b64:
        return need_ctx("describe_image", "图片（用户这条消息里没有图片）")

    q = (question or "用两三句话描述这张图的关键信息；如果里面有文字，把文字读出来。").strip()
    messages = [
        {"role": "system", "content": "你是看图助手。只描述你真实看到的内容，看不清就说看不清，不要编造。"},
        {"role": "user", "content": [
            {"type": "text", "text": q},
            {"type": "image_url", "image_url": {"url": llm._data_url(b64)}},
        ]},
    ]
    payload = {"model": vision_model, "messages": messages,
               "max_tokens": 512, "temperature": 0.2}
    try:
        text = await llm._post_chat((cfg["llm"]["base_url"]).rstrip("/"),
                                    cfg["llm"]["api_key"], payload)
    except Exception as e:
        return miss_result("describe_image", f"看图失败了（{type(e).__name__}）。")
    if not text:
        return miss_result("describe_image", "图看不太清，说不出内容。")
    return ok_result("describe_image", text, model=vision_model)


def tool_search_emote(ctx: ToolContext, keyword: str = "", **_kw) -> dict:
    """搜机器人自己收藏的表情包（网关侧 EmoteLib 的索引文件）。"""
    import pathlib

    index_path = pathlib.Path(os.environ.get("GATEWAY_EMOTE_DIR", "C:/Users/YOU/napcat/emotes")) / "index.json"
    if not index_path.is_file():
        return empty_result("search_emote", "还没有收藏表情包。")
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_result("search_emote", "表情包索引读不出来。")
    items = data.get("items") or data.get("emotes") or []
    if not items:
        return empty_result("search_emote", "表情库是空的。")
    kw = (keyword or "").strip()
    hits = [i for i in items if kw and kw in str(i.get("summary") or i.get("name") or "")]
    if not hits:
        hits = items[:10]
    lines = [f"{i.get('summary') or i.get('name') or '?'}（{i.get('kind') or 'face'}）" for i in hits[:10]]
    note = (f"匹配「{kw}」的有：" if kw and hits and kw in str(hits[0]) else "库里有这些：") + "；".join(lines)
    return ok_result("search_emote", note, count=len(hits))


# ============================================================
# QQ 动作（走网关 /internal/action，权限由 actions 层判定）
# ============================================================

async def tool_qq_group_history(ctx: ToolContext, group_id: str = "", count: str = "10", **_kw) -> dict:
    """读群聊记录。默认读当前群。"""
    from core import executor_client  # noqa: F401  (保持导入一致性)
    from core.actions import client as action_client

    gid = (group_id or ctx.group_id or "").strip()
    if not gid:
        return need_ctx("qq_group_history", "群号（当前不是群聊）")
    try:
        n = max(1, min(int(str(count)), 50))
    except (TypeError, ValueError):
        n = 10
    try:
        data = await action_client.call_action(
            "get_group_msg_history", {"group_id": int(gid) if str(gid).isdigit() else gid, "count": n})
    except Exception as e:
        return miss_result("qq_group_history", f"读群记录失败：{type(e).__name__}")
    msgs = (data.get("data") or {}).get("messages") or []
    if not msgs:
        return empty_result("qq_group_history", "这个群最近没有消息。", group_id=gid)
    lines = []
    for m in msgs[-n:]:
        sender = (m.get("sender") or {}).get("nickname") or (m.get("sender") or {}).get("user_id") or "?"
        text = ""
        for seg in (m.get("message") or []):
            if seg.get("type") == "text":
                text += str((seg.get("data") or {}).get("text") or "")
            elif seg.get("type") == "image":
                text += "[图片]"
            elif seg.get("type") == "face":
                text += "[表情]"
        lines.append(f"{sender}：{text[:100]}")
    return ok_result("qq_group_history", "\n".join(lines), group_id=gid, count=len(lines))


async def tool_qq_send(ctx: ToolContext, text: str, target: str = "", **_kw) -> dict:
    """主动发 QQ 消息。target 可为群号/QQ 号；留空=回当前会话。"""
    from core.actions import client as action_client

    body = (text or "").strip()
    if not body:
        return need_ctx("qq_send", "要发送的内容")
    tgt = (target or "").strip()
    if tgt.isdigit():
        gid = tgt
        action, params = "send_group_msg", {"group_id": int(gid), "message": body}
    elif ctx.group_id:
        action = "send_group_msg"
        params = {"group_id": int(ctx.group_id) if ctx.group_id.isdigit() else ctx.group_id,
                  "message": body}
    elif ctx.channel_user_id:
        action = "send_private_msg"
        params = {"user_id": int(ctx.channel_user_id) if ctx.channel_user_id.isdigit()
                  else ctx.channel_user_id, "message": body}
    else:
        return need_ctx("qq_send", "发给谁（当前会话不是 QQ）")
    try:
        await action_client.call_action(action, params, timeout_s=15)
    except Exception as e:
        return miss_result("qq_send", f"发送失败：{type(e).__name__}")
    return ok_result("qq_send", f"已发送（{action}）。", action=action)


# ============================================================
# 时间 / 计算：模型没有时钟也没有可靠算术，这两件必须给工具
# ============================================================

def tool_now(ctx: ToolContext, **_kw) -> dict:
    """当前时间。模型自己不知道今天几号，猜日期是常见幻觉来源。"""
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now().astimezone()
    week = "一二三四五六日"[now.weekday()]
    return ok_result(
        "now",
        f"现在是 {now.strftime('%Y-%m-%d %H:%M:%S')}（星期{week}，{now.tzname() or '本地时区'}），"
        f"UTC 时间 {_dt.now(_tz.utc).strftime('%Y-%m-%d %H:%M')}。",
        iso=now.isoformat(), date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M:%S"),
        weekday=week, tz=now.tzname() or "", epoch=int(now.timestamp()),
    )


def tool_calc(expression: str, **_kw) -> dict:
    """只算数学表达式，不执行代码。

    用 ast 白名单求值（+ - * / // % ** 括号/常数），绝不 eval：
    eval 会让「算一下 1+1」变成任意代码执行入口。
    """
    import ast

    expr = (expression or "").strip()
    if not expr:
        return need_ctx("calc", "要计算的表达式")
    if len(expr) > 200:
        return miss_result("calc", "表达式太长（上限 200 字符）。")
    try:
        tree = ast.parse(expr.replace("×", "*").replace("÷", "/").replace("^", "**"), mode="eval")
    except SyntaxError:
        return miss_result("calc", f"看不懂这个算式：{expr}")

    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub,
               ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd)
    exponent_ok = True
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return miss_result("calc", "只支持加减乘除、整除、取余、乘方和括号。")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            return miss_result("calc", "只支持数字。")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            # 防「9**9**9」把进程算死：指数必须是可静态判断的小数字
            right = node.right
            if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)):
                exponent_ok = exponent_ok and abs(right.value) <= 1000
            elif isinstance(right, ast.UnaryOp) and isinstance(right.operand, ast.Constant):
                val = getattr(right.operand, "value", 0) or 0
                exponent_ok = exponent_ok and isinstance(val, (int, float)) and abs(val) <= 1000
            else:
                exponent_ok = False
    if not exponent_ok:
        return miss_result("calc", "指数太大或不是常数，拒绝计算。")
    try:
        value = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})
    except (ZeroDivisionError, OverflowError, ValueError) as e:
        return miss_result("calc", f"算不出来：{type(e).__name__}")
    if isinstance(value, float):
        out = round(value, 10)
        if out == int(out) and abs(out) < 1e15:
            out = int(out)
    else:
        out = value
    return ok_result("calc", f"{expr} = {out}", expression=expr, value=out)


# ============================================================
# 发图 / 生图：QQ 侧真发出去，不是把链接丢给用户
# ============================================================

async def _send_cq_image(ctx: ToolContext, cq_file: str) -> dict:
    """把一段 [CQ:image,...] 发给当前会话。url/file/base64 三种来源由调用方拼好。"""
    from core.actions import client as action_client

    if ctx.group_id:
        action, target = "send_group_msg", int(ctx.group_id) if str(ctx.group_id).isdigit() else ctx.group_id
        params = {"group_id": target, "message": cq_file}
    elif ctx.channel_user_id:
        action, target = "send_private_msg", int(ctx.channel_user_id) if str(ctx.channel_user_id).isdigit() else ctx.channel_user_id
        params = {"user_id": target, "message": cq_file}
    else:
        return need_ctx("send_image", "当前会话不是 QQ，发不了图")
    try:
        await action_client.call_action(action, params, timeout_s=20)
    except Exception as e:
        return miss_result("send_image", f"发图失败：{type(e).__name__}")
    return ok_result("send_image", "图发出去了。", action=action)


async def tool_send_image(ctx: ToolContext, source: str = "", **_kw) -> dict:
    """发一张图：支持 http 链接、data URL、或执行器白名单内的本地路径。"""
    raw = (source or "").strip()
    if not raw:
        return need_ctx("send_image", "图片地址或路径")
    # 先确认有 QQ 会话可发，再去读文件/下载：否则白跑一趟还可能读本地文件
    if not ctx.group_id and not ctx.channel_user_id:
        return need_ctx("send_image", "当前会话不是 QQ，发不了图")
    if raw.startswith(("http://", "https://")):
        from core import url_guard

        try:
            safe = url_guard.check_url(raw)
        except url_guard.UnsafeUrl as e:
            return miss_result("send_image", f"这个图片地址不能发：{e}")
        return await _send_cq_image(ctx, f"[CQ:image,file={safe}]")
    if raw.startswith("data:"):
        b64 = raw.split(",", 1)[-1]
        return await _send_cq_image(ctx, f"[CQ:image,file=base64://{b64}]")

    # 本地路径：必须过执行器策略，不能直接读机器上的任意文件
    from core import executor_client

    try:
        data = await executor_client.invoke("fs.read_b64", {"path": raw})
    except executor_client.ExecutorOffline:
        return off_result("send_image", "文件工具没启用（执行器未运行），发不了本地图。")
    except executor_client.ExecutorDenied as e:
        return miss_result("send_image", f"没有权限读这个文件：{e}")
    except executor_client.ExecutorError as e:
        return miss_result("send_image", f"读图失败：{e}")
    b64 = str(data.get("b64") or "")
    if not b64:
        return miss_result("send_image", "这张图读出来是空的。")
    return await _send_cq_image(ctx, f"[CQ:image,file=base64://{b64}]")


async def tool_generate_image(ctx: ToolContext, prompt: str, size: str = "1024x1024", **_kw) -> dict:
    """文生图：调同站点的 /images/generations，再把图真的发到会话里。"""
    from core import config, http_pool

    text = (prompt or "").strip()
    if not text:
        return need_ctx("generate_image", "画面描述")
    cfg = config.get_config()
    mm = cfg.get("multimodal") or {}
    # 生图端点与对话端点通常不是同一个（UniSound 实测 /images/generations 404）。
    # 支持独立配置 IMAGE_API_BASE_URL / IMAGE_API_KEY；没配就如实说画不了，
    # 不要拿对话端点的 key 去撞一个不存在的路径再报「画图失败」。
    image_base = str(mm.get("image_base_url") or os.environ.get("IMAGE_API_BASE_URL") or "").rstrip("/")
    image_key = str(mm.get("image_api_key") or os.environ.get("IMAGE_API_KEY") or "")
    llm_cfg = cfg.get("llm") or {}
    if not image_base:
        llm_cfg = llm_cfg if llm_cfg.get("has_key") else {}
        image_base = str(llm_cfg.get("base_url") or "").rstrip("/")
        image_key = str(llm_cfg.get("api_key") or "")
    if not image_base or not image_key:
        return off_result("generate_image", "生图服务还没接（需要 IMAGE_API_BASE_URL + IMAGE_API_KEY）。")
    model = mm.get("image_model") or "gpt-image-2"
    try:
        resp = await http_pool.get_client(timeout=120).post(
            f"{image_base}/images/generations",
            headers={"Authorization": f"Bearer {image_key}"},
            json={"model": model, "prompt": text[:900], "n": 1, "size": size or "1024x1024"},
        )
        resp.raise_for_status()
        item = (resp.json().get("data") or [{}])[0]
    except Exception as e:  # 生成失败不能拖垮对话
        return miss_result("generate_image", f"画图失败：{type(e).__name__}")
    url = str(item.get("url") or "")
    b64 = str(item.get("b64_json") or "")
    if not url and not b64:
        return miss_result("generate_image", "画图服务没返回图片。")
    sent = await _send_cq_image(
        ctx, f"[CQ:image,file=base64://{b64}]" if b64 else f"[CQ:image,file={url}]")
    if sent.get("found"):
        return ok_result("generate_image", "图画好了，已经发出去。", prompt=text, url=url)
    # 非 QQ 渠道（网页）：把链接交给前端渲染，照样算成功
    if url:
        return ok_result("generate_image", f"图画好了：{url}", prompt=text, url=url)
    return sent


# ============================================================
# 文档解析：把 PDF/DOCX/XLSX 变成可读文字
# ============================================================

async def tool_parse_document(ctx: ToolContext, path: str = "", url: str = "", **_kw) -> dict:
    """解析 PDF/DOCX/XLSX/MD/TXT 并返回正文摘要。"""
    from core import docparse, executor_client

    src = (path or url or "").strip()
    if not src:
        return need_ctx("parse_document", "文档路径或链接")
    data = b""
    name = src
    if src.startswith(("http://", "https://")):
        from core import url_guard

        try:
            safe = url_guard.check_url(src)
        except url_guard.UnsafeUrl as e:
            return miss_result("parse_document", f"这个链接不能下载：{e}")
        try:
            from core.http_pool import get_client

            resp = await get_client(timeout=30).get(safe)
            resp.raise_for_status()
            data = resp.content
        except httpx.HTTPError as e:
            return miss_result("parse_document", f"下载失败：{type(e).__name__}")
        name = safe.split("?")[0].rsplit("/", 1)[-1] or "download"
    else:
        try:
            raw = await executor_client.invoke("fs.read_b64", {"path": src})
        except executor_client.ExecutorOffline:
            return off_result("parse_document", "文件工具没启用（执行器未运行），读不了本机文档。")
        except executor_client.ExecutorDenied as e:
            return miss_result("parse_document", f"没有权限读这个文件：{e}")
        except executor_client.ExecutorError as e:
            return miss_result("parse_document", f"读取失败：{e}")
        import base64 as _b64

        try:
            data = _b64.b64decode(str(raw.get("b64") or ""))
        except ValueError:
            return miss_result("parse_document", "文件内容不是合法 base64。")

    if not data:
        return miss_result("parse_document", "文档是空的。")
    try:
        text = docparse.extract_text(name, data)
    except RuntimeError as e:
        return miss_result("parse_document", str(e)[:200])
    except Exception as e:  # 解析器自身异常也要可读
        return miss_result("parse_document", f"解析失败：{type(e).__name__}")
    body = (text or "").strip()
    if not body:
        return empty_result("parse_document", "文档里没有可读文字（可能是扫描件）。")
    return ok_result("parse_document", body[:6000], filename=name,
                     chars=len(body), truncated=len(body) > 6000)


# ============================================================
# 通用 QQ 查询：群成员 / 群列表 / 好友列表
# ============================================================

async def _qq_query(action: str, params: dict, tool: str) -> dict:
    from core.actions import client as action_client

    try:
        data = await action_client.call_action(action, params, timeout_s=15)
    except Exception as e:
        return miss_result(tool, f"查询失败：{type(e).__name__}")
    return ok_result(tool, json.dumps(data.get("data"), ensure_ascii=False)[:3000], raw=data.get("data"))


async def tool_qq_group_members(ctx: ToolContext, group_id: str = "", keyword: str = "", **_kw) -> dict:
    gid = (group_id or ctx.group_id or "").strip()
    if not gid:
        return need_ctx("qq_group_members", "群号（当前不是群聊）")
    res = await _qq_query("get_group_member_list",
                          {"group_id": int(gid) if gid.isdigit() else gid}, "qq_group_members")
    if not res.get("found"):
        return res
    members = res.get("raw") or []
    kw = (keyword or "").strip()
    rows = []
    for m in members if isinstance(members, list) else []:
        nick = str(m.get("card") or m.get("nickname") or "")
        if kw and kw not in nick:
            continue
        rows.append(f"{nick}（{m.get('user_id')}）")
    if not rows:
        return empty_result("qq_group_members", f"群 {gid} 里没找到匹配「{kw}」的成员。")
    return ok_result("qq_group_members", "；".join(rows[:40]), count=len(rows))


async def tool_qq_groups(ctx: ToolContext, **_kw) -> dict:
    return await _qq_query("get_group_list", {}, "qq_groups")


async def tool_qq_friends(ctx: ToolContext, **_kw) -> dict:
    return await _qq_query("get_friend_list", {}, "qq_friends")


# ============================================================
# 注册表
# ============================================================

# 同步实现（保持既有调用链）
_SYNC_IMPLS: dict[str, Callable[..., dict]] = {
    "query_order": tool_query_order,
    "initiate_refund": tool_initiate_refund,
    "lookup_policy": tool_lookup_policy,
    "current_context": tool_current_context,
    "recall_memory": tool_recall_memory,
    "remember": tool_remember,
    "remind": tool_remind,
    "list_reminders": tool_list_reminders,
    "cancel_reminder": tool_cancel_reminder,
    "search_emote": tool_search_emote,
    "now": tool_now,
    "calc": tool_calc,
}

# 异步实现
_ASYNC_IMPLS: dict[str, Callable[..., Any]] = {
    "read_webpage": tool_read_webpage,
    "web_search": tool_web_search,
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "describe_image": tool_describe_image,
    "qq_group_history": tool_qq_group_history,
    "qq_send": tool_qq_send,
    "send_image": tool_send_image,
    "generate_image": tool_generate_image,
    "parse_document": tool_parse_document,
    "qq_group_members": tool_qq_group_members,
    "qq_groups": tool_qq_groups,
    "qq_friends": tool_qq_friends,
}

# 元数据：名称/描述/参数/是否写操作（写操作在群聊里默认不自动执行）
TOOL_META: dict[str, dict] = {
    "current_context": {"name": "读当前对话", "desc": "查看这次对话最近说了什么、当前在哪个群",
                        "args": {}},
    "recall_memory": {"name": "回忆关于你的事", "desc": "读取长期记忆里存的信息",
                      "args": {"key": "string"}},
    "remember": {"name": "记住一件事", "desc": "把用户的偏好或事实写入长期记忆",
                 "args": {"key": "string", "value": "string"}},
    "remind": {"name": "设提醒", "desc": "在指定时间提醒用户（10分钟后 / 明天9点）",
               "args": {"text": "string", "when": "string"}},
    "list_reminders": {"name": "查看待提醒", "desc": "列出还没触发的提醒", "args": {}},
    "cancel_reminder": {"name": "取消提醒", "desc": "按编号取消一条提醒",
                        "args": {"reminder_id": "string"}},
    "read_webpage": {"name": "读网页", "desc": "打开一个链接并读正文",
                     "args": {"url": "string", "max_chars": "string"}},
    "web_search": {"name": "联网搜", "desc": "搜索实时信息",
                   "args": {"query": "string", "top_k": "string"}},
    "list_files": {"name": "列目录", "desc": "列出某个目录下的文件（受策略白名单限制）",
                   "args": {"path": "string", "depth": "string"}},
    "read_file": {"name": "读文件", "desc": "读取一个文本文件（受策略白名单限制）",
                  "args": {"path": "string", "max_chars": "string"}},
    "describe_image": {"name": "看图", "desc": "识别图片内容，或读用户刚发的图里的文字",
                       "args": {"source": "string", "question": "string"}},
    "search_emote": {"name": "找表情包", "desc": "在收藏的表情包里按关键词找",
                     "args": {"keyword": "string"}},
    "qq_group_history": {"name": "读群记录", "desc": "读取群聊最近的聊天记录",
                         "args": {"group_id": "string", "count": "string"}},
    "qq_send": {"name": "发QQ消息", "desc": "主动往群里或私聊发一条消息",
                "args": {"text": "string", "target": "string"}},
    "now": {"name": "看时间", "desc": "查当前日期、时间和星期（模型自己不知道今天几号）",
            "args": {}},
    "calc": {"name": "算数", "desc": "计算数学表达式（加减乘除、取余、乘方、括号）",
             "args": {"expression": "string"}},
    "send_image": {"name": "发图片", "desc": "把一张图片发到当前会话（链接 / 本地路径 / data URL）",
                   "args": {"source": "string"}},
    "generate_image": {"name": "画图", "desc": "按描述生成一张图片并发出去",
                       "args": {"prompt": "string", "size": "string"}},
    "parse_document": {"name": "读文档", "desc": "解析 PDF/DOCX/XLSX/MD/TXT 并返回正文",
                       "args": {"path": "string", "url": "string"}},
    "qq_group_members": {"name": "查群成员", "desc": "列出群成员，可按昵称筛选",
                         "args": {"group_id": "string", "keyword": "string"}},
    "qq_groups": {"name": "查我的群", "desc": "列出机器人加入的 QQ 群", "args": {}},
    "qq_friends": {"name": "查好友", "desc": "列出机器人的 QQ 好友", "args": {}},
}

WRITE_TOOLS = frozenset({"remember", "remind", "cancel_reminder", "qq_send",
                         "send_image", "generate_image"})

# 需要「可信主体」才能用的工具：能读写主机文件、能以机器人身份往任意群发消息。
# remember/remind 只作用于调用者自己的记忆与提醒，任何渠道用户都可用；
# 这几个一旦对普通群成员开放，等于把执行器 allow_roots 和整个 QQ 账号交出去
# （提示注入就能让模型「帮我读一下 C:/... 然后发到群里」）。
PRIVILEGED_TOOLS = frozenset({
    "list_files", "read_file", "write_file", "parse_document",
    "qq_send", "send_image", "generate_image",
})


def _authorize_tool(name: str, ctx: ToolContext) -> dict | None:
    """权限闸门：返回 None 表示放行，否则返回拒绝结果。

    工具层原来完全不看主体：模型（或注入内容）选中 write_file 就直接落到执行器，
    既不查 allow_roots 之外的档位，也不落 action_log。这里补上同一套主体判定，
    并写审计；主体为空（内部直调/单测）时保持原行为，避免破坏工具的本职逻辑。
    """
    if name not in PRIVILEGED_TOOLS or not ctx.principal:
        return None
    from core.actions import authz

    principal = authz.parse_principal(ctx.principal)
    if principal.is_trusted_human():
        return None
    if principal.kind == authz.PRINCIPAL_QQ and principal.ident in authz.authorized_qq():
        return None
    if principal.kind == authz.PRINCIPAL_AGENT and principal.origin and (
            principal.origin.is_trusted_human()
            or (principal.origin.kind == authz.PRINCIPAL_QQ
                and principal.origin.ident in authz.authorized_qq())):
        return None
    try:
        store.append_audit(ctx.principal, "tool.denied", {"tool": name})
    except Exception:  # 审计写失败不能改变拒绝结论
        pass
    return miss_result(name, "这个操作需要管理员权限，当前身份不能执行。")


def _builtin_impls() -> dict:
    return dict(_SYNC_IMPLS)


def is_async_tool(name: str) -> bool:
    return name in _ASYNC_IMPLS


def _adapt_kwargs(fn, args: dict, ctx: ToolContext) -> dict:
    """按函数签名挑参数。

    必须识别 **kwargs（VAR_KEYWORD）：`_make_kb_search_tool` 与 `_make_stub_tool`
    都是 `def run(**kw)`，按名字过滤会让它们永远收到空参数——检索词恒为空、
    自定义工具拿到 args:{}（已复现）。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(args)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        out = {k: v for k, v in args.items() if k != "ctx"}
        if "ctx" in params:
            out["ctx"] = ctx
        return out
    kwargs = {k: v for k, v in args.items() if k in params}
    if "ctx" in params:
        kwargs["ctx"] = ctx
    return kwargs


async def invoke_with(registry_map: dict | None, name: str, args: dict | None,
                      ctx: ToolContext = EMPTY) -> dict:
    """按调用方给定的工具表执行，兼容 monkeypatch / 自定义工具。

    `registry_map` 来自 build_context 的 ctx["tools"]（已按配置过滤启用项）；
    当它包含该工具时以它为准——否则测试替身与用户自定义实现都会被内置表盖掉。
    """
    args = dict(args or {})
    denied = _authorize_tool(name, ctx)
    if denied is not None:
        return denied
    fn = (registry_map or {}).get(name)
    if fn is not None and not is_async_tool(name):
        try:
            res = fn(**_adapt_kwargs(fn, args, ctx))
            if inspect.isawaitable(res):
                res = await res
            return res
        except TypeError as e:
            return miss_result(name, f"参数不对：{e}")
    return await invoke(name, args, ctx)


async def invoke(name: str, args: dict | None, ctx: ToolContext = EMPTY) -> dict:
    """统一调用入口：同步/异步工具都能用。"""
    args = dict(args or {})
    denied = _authorize_tool(name, ctx)
    if denied is not None:
        return denied
    if not _config_ready(name):
        return off_result(name, "这个能力还没接入（缺配置）。用已有资料回答，并说明查不到实时数据。")
    if name in _ASYNC_IMPLS:
        fn = _ASYNC_IMPLS[name]
        try:
            res = fn(ctx=ctx, **args)
            if inspect.isawaitable(res):
                res = await res
            return res
        except TypeError as e:
            return miss_result(name, f"参数不对：{e}")
    if name in _SYNC_IMPLS:
        fn = _SYNC_IMPLS[name]
        try:
            return fn(**_adapt_kwargs(fn, args, ctx))
        except TypeError as e:
            return miss_result(name, f"参数不对：{e}")
    return miss_result(name, f"工具「{name}」没有实现。")


def _make_kb_search_tool(t: dict):
    args_schema = t.get("args_json", {})
    search_arg = next(iter(args_schema.keys()), "topic")

    def run(**kw):
        return tool_lookup_policy(str(kw.get(search_arg, "")))
    return run


def _make_stub_tool(t: dict):
    """自定义工具未接入实现时的占位执行器：明说没接，不假装成功。"""
    def run(**kw):
        return {
            "tool": t["id"], "connected": False, "found": False, "status": "not_implemented",
            "note": f"工具「{t.get('name') or t['id']}」尚未接入实际后端。",
            "args": kw,
        }
    return run


def active_tools(cfg: dict) -> dict:
    tools_cfg = cfg.get("tools", {})
    out: dict = {}
    for t in store.active_tools():
        tid = t["id"]
        if not tools_cfg.get(tid, True):
            continue
        if tid in _SYNC_IMPLS:
            out[tid] = _SYNC_IMPLS[tid]
        elif tid in _ASYNC_IMPLS:
            out[tid] = _ASYNC_IMPLS[tid]
        elif t.get("kind") == "kb_search" or "kb_search" in json.dumps(t.get("args_json", {})):
            out[tid] = _make_kb_search_tool(t)
        else:
            out[tid] = _make_stub_tool(t)
    return out


# 依赖外部配置才有意义的工具：没配置时不该出现在模型的可选清单里。
# 否则模型会挑一个注定返回「未配置」的工具，用户看到的是一句推脱，
# 而真正该做的是用已有资料回答（实测 u2-flash 联网题稳定踩这个坑）。
_REQUIRES_CONFIG = {
    "web_search": ("SEARCH_PROVIDER", "TAVILY_API_KEY", "SERPER_API_KEY", "SEARXNG_URL"),
    "query_order": ("ORDER_QUERY_URL",),
    "initiate_refund": ("REFUND_API_URL",),
    "list_files": ("EXECUTOR_URL",),
    "read_file": ("EXECUTOR_URL",),
    "write_file": ("EXECUTOR_URL",),
    "parse_document": ("EXECUTOR_URL",),
}


def _config_ready(tid: str) -> bool:
    from core import websearch

    if tid == "web_search":
        return websearch.available()
    if tid in ("query_order", "initiate_refund"):
        return bool(os.environ.get(_REQUIRES_CONFIG[tid][0], "").strip())
    if tid in _REQUIRES_CONFIG:
        # 执行器相关：URL 有默认值，真正可用与否由运行时 503 决定，
        # 这里只在显式配置了才暴露，避免默认环境下模型选了必然失败的工具。
        return bool(os.environ.get("EXECUTOR_URL", "").strip())
    return True


def tool_schema_block(cfg: dict) -> list[dict]:
    out = []
    enabled_cfg = cfg.get("tools", {})
    for t in store.active_tools():
        tid = t["id"]
        if not enabled_cfg.get(tid, True):
            continue
        if not _config_ready(tid):
            continue
        args = tool_args(tid) or list(t.get("args_json", {}).keys())
        out.append({"name": tid, "args": args, "desc": t.get("desc") or t.get("name") or tid})
    return out


def tool_args(name: str) -> list[str]:
    if name in TOOL_META:
        return list(TOOL_META[name]["args"])
    builtin = {"query_order": ["order_id"], "initiate_refund": ["order_id", "reason"],
               "lookup_policy": ["topic"]}
    if name in builtin:
        return builtin[name]
    t = store.get_tool(name)
    return list(t.get("args_json", {}).keys()) if t else []


def detect_tool(query: str, tools: dict | None = None) -> tuple[str | None, dict | None]:
    """规则路由：只处理高置信度的确定性问法。

    其余交给模型自己决定（LLM 模式有 tool_args，规则模式不猜）。
    提醒类在这里拦一道，是因为「10 分钟后提醒我」语义极其明确、
    而规则模式（无 Key 部署）也必须能设提醒。
    """
    enabled = tools if tools is not None else active_tools({"tools": {}})
    q = query or ""

    # 提醒意图：先把时间片段剥出来，剩下的就是提醒内容。
    # 用「搜索 + 删除」而不是一个复杂正则：两种语序（时间在前/在后）
    # 都能正确切分，也不会因非贪婪量词把 "10分钟后" 咬成 "1"。
    if "提醒" in q and "remind" in enabled:
        # 时间片段要按「最长可识别形态」匹配：先试完整的「每天8点 / 每周三下午3点 /
        # 每月31号 / 明天早上9点」，再退到裸「明天」。反过来会把 "明天早上9点"
        # 切成 when=明天 + text=早上9点，时间就丢了。
        tm = (
            # 周期规则 + 钟点。「工作日」单独出现太容易误伤（"这个工作日"），
            # 因此 weekday 必须带钟点才算；其余规则钟点可选（「每月31号」本身够定位）。
            re.search(r"((?:每天|每日|天天|每(?:周|星期)[一二三四五六日天]|每月\s*\d{1,2}\s*[号日])"
                      r"(?:\s*(?:早上|上午|中午|下午|晚上)?\s*\d{1,2}\s*[:：点]\s*(?:半|\d{1,2})?)?)", q)
            or re.search(r"((?:每个|每)?工作日\s*(?:早上|上午|中午|下午|晚上)?\s*\d{1,2}\s*[:：点]\s*(?:半|\d{1,2})?)", q)
            or re.search(r"(\d+\s*(?:分钟|小时|天|周|秒)后)", q)
            or re.search(r"((?:明天|后天|今天|今晚)\s*(?:早上|上午|中午|下午|晚上)?\s*\d{0,2}\s*[点:：]\s*\d{0,2})", q)
            or re.search(r"((?:早上|上午|中午|下午|晚上)\s*\d{1,2}\s*[:：点]\s*(?:半|\d{1,2})?)", q)
            or re.search(r"((?<![\d:])\d{1,2}\s*[:：点]\s*(?:半|\d{1,2})?)", q)
        )
        if tm:
            when = tm.group(1).strip()
            text = (q[:tm.start()] + " " + q[tm.end():]).replace("提醒我", " ").replace("提醒", " ")
            text = text.strip(" ，,。.；;：: 请帮我设个")
            if text:
                return "remind", {"text": text, "when": when}
    if ("待办" in q or "提醒" in q) and ("列" in q or "有什" in q or "查看" in q) and "list_reminders" in enabled:
        return "list_reminders", {}

    order_match = re.search(r"(?:订单|order|单号)\s*[:：]?\s*([A-Za-z0-9]{6,12})", q)
    if order_match and ("退款" in q or "退货" in q) and "initiate_refund" in enabled:
        return "initiate_refund", {"order_id": order_match.group(1), "reason": "规则路由"}
    if order_match and ("发货" in q or "物流" in q or "快递" in q or "查询" in q
                        or "什么时候" in q or "状态" in q) and "query_order" in enabled:
        return "query_order", {"order_id": order_match.group(1)}
    if ("政策" in q or "规定" in q or "条款" in q) and "lookup_policy" in enabled:
        return "lookup_policy", {"topic": q}

    if ("群里" in q or "群聊" in q or "大家" in q) and any(
            w in q for w in ("聊什么", "聊啥", "在聊", "说了什么", "说了啥", "最近")
    ) and "qq_group_history" in enabled:
        return "qq_group_history", {}

    # 现在几点/今天几号：模型没有时钟，必须交给工具（否则它会编日期）
    if "now" in enabled and re.search(
            r"(现在|今天|当前).{0,4}(几点|什么时间|日期|几号|星期几|周几)"
            r"|(几点|现在时间|当前时间)", q):
        return "now", {}

    # 纯算式：只认「算一下/等于几」或整句就是算式，避免把正文里的数字当计算
    if "calc" in enabled:
        m = re.search(r"(?:算一下|算算|计算)\s*[:：]?\s*([-+*/%().\d\s^×÷]+)", q) or             re.search(r"([-+*/%().\d\s^×÷]+?)\s*(?:等于多少|等于几|=)", q)
        expr = m.group(1).strip() if m else ""
        if not expr and re.fullmatch(r"[\d\s+\-*/%().^×÷]+", q) and re.search(r"[\d]", q):
            expr = q.strip()
        if expr and re.search(r"[-+*/%^×÷]", expr):
            return "calc", {"expression": expr}

    url = re.search(r"https?://[^\s，。；）)]+", q)
    if url and ("看" in q or "读" in q or "打开" in q or "总结" in q or "内容" in q) and "read_webpage" in enabled:
        return "read_webpage", {"url": url.group(0)}

    return None, None


# ============================================================
# QQ 全量接管：把 OneBot v11 的动作暴露成模型可调用的工具
# ============================================================
# 设计（对标 AstrBot / NoneBot 插件生态的「动作可编排」）：
# - 模型只拿到**动作名 + 参数名**，权限与审批仍由 core.actions 统一裁决；
# - 危险动作（L2）永远走审批，不会因为"变成工具"就绕过；
# - 这一层不做业务判断，只把工具参数原样转成动作参数。

def _qq_tool(action: str, tool_name: str, desc: str, args: dict[str, str]):
    """生成一个薄封装工具：调用 → actions_service.execute（含鉴权/审计/审批）。"""

    async def run(ctx: ToolContext, **kw) -> dict:
        from core.actions import service as actions_service

        params = {k: v for k, v in kw.items() if v not in (None, "")}
        # 群/人目标缺省时用当前会话，避免模型每次都要问群号
        if ctx.group_id and "group_id" in args and "group_id" not in params:
            params["group_id"] = ctx.group_id
        if ctx.channel_user_id and "user_id" in args and "user_id" not in params:
            params["user_id"] = ctx.channel_user_id
        try:
            result = await actions_service.execute(
                action=action, params=params,
                principal_raw=ctx.principal or "admin",
            )
        except actions_service.ActionError as e:
            if e.status == 409 or "审批" in str(e):
                return ok_result(tool_name, f"这个操作需要管理员确认，已提交审批：{e}")
            return miss_result(tool_name, f"没办成：{e}")
        except Exception as e:
            return miss_result(tool_name, f"没办成：{type(e).__name__}")
        if result.get("status") == "pending_approval":
            return ok_result(tool_name,
                             f"这属于破坏性操作，已送管理员审批（编号 {result.get('approval_id')}）。")
        return ok_result(tool_name, f"办好了。", data=result.get("result"))

    run.__name__ = f"tool_{tool_name}"
    return run


_QQ_ACTION_TOOLS: dict[str, dict] = {
    "qq_group_ban": {
        "action": "set_group_ban", "args": {"group_id": "string", "user_id": "string", "duration": "number"},
        "desc": "禁言群成员（duration 秒，0=解禁）",
    },
    "qq_group_kick": {
        "action": "set_group_kick", "args": {"group_id": "string", "user_id": "string"},
        "desc": "把成员移出群（破坏性，需审批）",
    },
    "qq_group_card": {
        "action": "set_group_card", "args": {"group_id": "string", "user_id": "string", "card": "string"},
        "desc": "改群名片",
    },
    "qq_group_rename": {
        "action": "set_group_name", "args": {"group_id": "string", "group_name": "string"},
        "desc": "改群名",
    },
    "qq_group_notice": {
        "action": "send_group_notice", "args": {"group_id": "string", "content": "string"},
        "desc": "发群公告",
    },
    "qq_recall": {
        "action": "delete_msg", "args": {"message_id": "string"},
        "desc": "撤回一条消息",
    },
    "qq_upload_file": {
        "action": "upload_group_file", "args": {"group_id": "string", "file": "string", "name": "string"},
        "desc": "往群里传文件（file 为本机路径）",
    },
    "qq_friend_handle": {
        "action": "set_friend_add_request", "args": {"flag": "string", "approve": "boolean"},
        "desc": "同意/拒绝加好友请求",
    },
    "qq_group_handle": {
        "action": "set_group_add_request",
        "args": {"flag": "string", "sub_type": "string", "approve": "boolean"},
        "desc": "同意/拒绝入群请求",
    },
    "qq_set_avatar": {
        "action": "set_qq_avatar", "args": {"file": "string"},
        "desc": "换机器人 QQ 头像（file 为本机图片路径或 http 链接）",
    },
    "qq_set_profile": {
        "action": "set_qq_profile",
        "args": {"nickname": "string", "personal_note": "string", "sex": "string"},
        "desc": "改机器人 QQ 昵称 / 签名 / 性别",
    },
}

for _tool_name, _meta in _QQ_ACTION_TOOLS.items():
    _ASYNC_IMPLS[_tool_name] = _qq_tool(_meta["action"], _tool_name, _meta["desc"], _meta["args"])
    TOOL_META[_tool_name] = {"name": _tool_name, "desc": _meta["desc"], "args": _meta["args"]}
