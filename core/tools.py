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

import asyncio
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
        return miss_result("recall_memory", "我这儿还没有关于你的记录。")
    if key:
        val = mem.get(key)
        if val is None:
            return miss_result("recall_memory", f"没有「{key}」这条记录。", keys=list(mem)[:20])
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
    now = now or datetime.now(timezone.utc)
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


def tool_remind(ctx: ToolContext, text: str, when: str, **_kw) -> dict:
    """加一条提醒。when 支持「10 分钟后 / 明天 9 点 / ISO」。"""
    if not ctx.visitor_id:
        return need_ctx("remind", "访客身份")
    text = (text or "").strip()[:200]
    if not text:
        return need_ctx("remind", "要提醒的内容")
    due = _parse_due(when)
    if due is None:
        return need_ctx("remind", f"提醒时间（「{when}」没看懂，请说清几点或多久后）")
    rec = store.reminder_add(
        visitor_id=ctx.visitor_id, chat_key=ctx.chat_key, text=text,
        due_at=due.isoformat(), channel_type=ctx.channel_type,
        group_id=ctx.group_id, channel_user_id=ctx.channel_user_id,
    )
    local = due.astimezone().strftime("%m-%d %H:%M")
    return ok_result("remind", f"好，{local} 提醒你：{text}", reminder_id=rec.get("id"),
                     due_at=due.isoformat())


def tool_list_reminders(ctx: ToolContext, **_kw) -> dict:
    if not ctx.visitor_id:
        return need_ctx("list_reminders", "访客身份")
    rows = store.reminder_list(ctx.visitor_id)
    if not rows:
        return miss_result("list_reminders", "你现在没有待提醒的事。")
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
        return miss_result("cancel_reminder", f"找不到 #{rid} 这条提醒。")
    if not store.reminder_cancel(rid):
        return miss_result("cancel_reminder", f"#{rid} 已经发过或已取消。")
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


# ============================================================
# 图片 / 表情包
# ============================================================

async def tool_describe_image(ctx: ToolContext, source: str = "", question: str = "", **_kw) -> dict:
    """看图。source 为空表示「用户刚发的那张」。"""
    import base64

    from core import config, llm

    cfg = config.get_config()
    vision_model = (cfg.get("multimodal", {}) or {}).get("vision_model", "") or ""
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
        return off_result("search_emote", "还没有收藏表情包。")
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return miss_result("search_emote", "表情包索引读不出来。")
    items = data.get("items") or data.get("emotes") or []
    if not items:
        return miss_result("search_emote", "表情库是空的。")
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
        return miss_result("qq_group_history", "这个群最近没有消息。", group_id=gid)
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
}

# 异步实现
_ASYNC_IMPLS: dict[str, Callable[..., Any]] = {
    "read_webpage": tool_read_webpage,
    "web_search": tool_web_search,
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "describe_image": tool_describe_image,
    "qq_group_history": tool_qq_group_history,
    "qq_send": tool_qq_send,
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
}

WRITE_TOOLS = frozenset({"remember", "remind", "cancel_reminder", "qq_send"})


def _builtin_impls() -> dict:
    return dict(_SYNC_IMPLS)


def is_async_tool(name: str) -> bool:
    return name in _ASYNC_IMPLS


async def invoke_with(registry_map: dict | None, name: str, args: dict | None,
                      ctx: ToolContext = EMPTY) -> dict:
    """按调用方给定的工具表执行，兼容 monkeypatch / 自定义工具。

    `registry_map` 来自 build_context 的 ctx["tools"]（已按配置过滤启用项）；
    当它包含该工具时以它为准——否则测试替身与用户自定义实现都会被内置表盖掉。
    """
    args = dict(args or {})
    fn = (registry_map or {}).get(name)
    if fn is not None and not is_async_tool(name):
        try:
            sig = inspect.signature(fn)
            kwargs = {k: v for k, v in args.items() if k in sig.parameters}
            if "ctx" in sig.parameters:
                kwargs["ctx"] = ctx
            res = fn(**kwargs)
            if inspect.isawaitable(res):
                res = await res
            return res
        except TypeError as e:
            return miss_result(name, f"参数不对：{e}")
    return await invoke(name, args, ctx)


async def invoke(name: str, args: dict | None, ctx: ToolContext = EMPTY) -> dict:
    """统一调用入口：同步/异步工具都能用。"""
    args = dict(args or {})
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
            sig = inspect.signature(fn)
            kwargs = {k: v for k, v in args.items() if k in sig.parameters}
            if "ctx" in sig.parameters:
                kwargs["ctx"] = ctx
            return fn(**kwargs)
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


def tool_schema_block(cfg: dict) -> list[dict]:
    out = []
    enabled_cfg = cfg.get("tools", {})
    for t in store.active_tools():
        tid = t["id"]
        if not enabled_cfg.get(tid, True):
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
        # 时间片段要按「最长可识别形态」匹配：先试「明天早上9点」，再退到裸「明天」。
        # 反过来会把 "明天早上9点" 切成 when=明天 + text=早上9点，时间就丢了。
        tm = (
            re.search(r"(\d+\s*(?:分钟|小时|天|周|秒)后)", q)
            or re.search(r"((?:明天|后天|今天|今晚)\s*(?:早上|上午|中午|下午|晚上)?\s*\d{0,2}\s*[点:：]\s*\d{0,2})", q)
            or re.search(r"(明天|后天|今天|今晚)", q)
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

    url = re.search(r"https?://[^\s，。；）)]+", q)
    if url and ("看" in q or "读" in q or "打开" in q or "总结" in q or "内容" in q) and "read_webpage" in enabled:
        return "read_webpage", {"url": url.group(0)}

    return None, None
