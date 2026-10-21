"""工具注册表：内置实现 + DB 数据驱动开关。

**关于内置工具的真实性**：
`query_order` / `initiate_refund` 默认是「未接入」状态——返回明确的未接入提示，
而不是编造快递单号。要接入真实业务，通过环境变量配置 HTTP 端点：

    ORDER_QUERY_URL=https://your-erp/api/order/{order_id}
    ORDER_QUERY_TOKEN=xxx
    REFUND_API_URL=https://your-erp/api/refund

未配置时，工具返回 `connected: false`，管线会据此转人工——
不会让用户看到假的物流信息。
"""
from __future__ import annotations

import json
import os
import re

import httpx

from core import retrieval, store


def _order_endpoint() -> tuple[str, str]:
    url = (os.environ.get("ORDER_QUERY_URL") or "").strip()
    token = (os.environ.get("ORDER_QUERY_TOKEN") or "").strip()
    return url, token


def _refund_endpoint() -> tuple[str, str]:
    url = (os.environ.get("REFUND_API_URL") or "").strip()
    token = (os.environ.get("REFUND_API_TOKEN") or "").strip()
    return url, token


def _not_connected(tool: str, hint: str) -> dict:
    """未接入真实后端时的统一返回。

    关键：不编造业务数据。调用方（pipeline）看到 connected=False 会转人工。
    """
    return {
        "tool": tool,
        "connected": False,
        "found": False,
        "note": f"{hint} 已为您转接人工坐席查询。",
    }


def tool_query_order(order_id: str) -> dict:
    """查询订单物流。未配置 ORDER_QUERY_URL 时返回未接入提示，不伪造单号。"""
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
        return {
            "tool": "query_order", "connected": True, "found": False,
            "error": f"{type(e).__name__}",
            "note": "订单系统暂时不可用，已为您转接人工坐席。",
        }

    status = str(data.get("status") or "")
    tracking = str(data.get("tracking_no") or "")
    carrier = str(data.get("carrier") or "")
    note = f"订单 {order_id} 当前状态：{status}。"
    if carrier or tracking:
        note += f" 物流：{carrier} {tracking}。"
    return {
        "tool": "query_order", "connected": True, "found": True,
        "order_id": order_id, "status": status,
        "carrier": carrier, "tracking_no": tracking,
        "eta": str(data.get("eta") or ""),
        "note": note,
    }


def tool_initiate_refund(order_id: str, reason: str = "") -> dict:
    """发起退款。未配置 REFUND_API_URL 时返回未接入提示。"""
    url, token = _refund_endpoint()
    if not url:
        return _not_connected("initiate_refund", "退款申请暂未接入自助渠道，")

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"order_id": order_id, "reason": reason or "未说明"}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return {
            "tool": "initiate_refund", "connected": True, "found": False,
            "error": f"{type(e).__name__}",
            "note": "退款系统暂时不可用，已为您转接人工坐席。",
        }

    refund_id = str(data.get("refund_id") or "")
    status = str(data.get("status") or "submitted")
    return {
        "tool": "initiate_refund", "connected": True, "found": True,
        "order_id": order_id, "refund_id": refund_id, "status": status,
        "note": f"退款申请已提交（单号 {refund_id or '待生成'}），当前状态：{status}。",
    }


def tool_lookup_policy(topic: str) -> dict:
    """按主题检索知识库。这是真实实现——直接查 KB。"""
    hits = retrieval.retrieve(topic, top_k=2)
    entries = [
        {"id": h["id"], "title": h["title"], "answer": h["answer"]} for h in hits
    ]
    if entries:
        note = "；".join(f"【{e['title']}】{e['answer']}（依据 {e['id']}）" for e in entries)
    else:
        note = f"未找到与「{topic}」相关的政策条款，已为您转人工核实。"
    return {
        "tool": "lookup_policy",
        "connected": True,
        "found": bool(hits),
        "topic": topic,
        "entries": entries,
        "note": note,
    }


def _builtin_impls() -> dict:
    return {
        "query_order": tool_query_order,
        "initiate_refund": tool_initiate_refund,
        "lookup_policy": tool_lookup_policy,
    }


def _make_kb_search_tool(t: dict):
    args_schema = t.get("args_json", {})
    search_arg = next(iter(args_schema.keys()), "topic")

    def run(**kw):
        return tool_lookup_policy(str(kw.get(search_arg, "")))
    return run


def _make_stub_tool(t: dict):
    """自定义工具未接入实现时的占位执行器。

    明确返回 stub 状态，并在 note 里说清"尚未接入"，
    不假装执行成功。
    """
    def run(**kw):
        return {
            "tool": t["id"],
            "connected": False,
            "found": False,
            "status": "not_implemented",
            "note": f"工具「{t.get('name') or t['id']}」尚未接入实际后端，已转人工处理。",
            "args": kw,
        }
    return run


def active_tools(cfg: dict) -> dict:
    tools_cfg = cfg.get("tools", {})
    out = {}
    for t in store.active_tools():
        tid = t["id"]
        if not tools_cfg.get(tid, True):
            continue
        if tid in _builtin_impls():
            out[tid] = _builtin_impls()[tid]
        elif t.get("kind") == "kb_search" or "kb_search" in json.dumps(t.get("args_json", {})):
            out[tid] = _make_kb_search_tool(t)
        else:
            out[tid] = _make_stub_tool(t)
    return out


def tool_schema_block(cfg: dict) -> list[dict]:
    out = []
    for t in store.active_tools():
        tid = t["id"]
        if not cfg.get("tools", {}).get(tid, True):
            continue
        args = tool_args(tid) or list(t.get("args_json", {}).keys())
        out.append({"name": tid, "args": args, "desc": t.get("desc") or t.get("name") or tid})
    return out


def tool_args(name: str) -> list[str]:
    builtin = {"query_order": ["order_id"], "initiate_refund": ["order_id", "reason"], "lookup_policy": ["topic"]}
    if name in builtin:
        return builtin[name]
    t = store.get_tool(name)
    return list(t.get("args_json", {}).keys()) if t else []


def detect_tool(query: str, tools: dict | None = None) -> tuple[str | None, dict | None]:
    enabled = tools if tools is not None else active_tools({"tools": {}})
    order_match = re.search(r"(?:订单|order|单号)\s*[:：]?\s*([A-Za-z0-9]{6,12})", query)
    if order_match and ("退款" in query or "退货" in query) and "initiate_refund" in enabled:
        return "initiate_refund", {"order_id": order_match.group(1), "reason": "规则路由"}
    if order_match and ("发货" in query or "物流" in query or "快递" in query or "查询" in query or "什么时候" in query or "状态" in query) and "query_order" in enabled:
        return "query_order", {"order_id": order_match.group(1)}
    if ("政策" in query or "规定" in query or "条款" in query) and "lookup_policy" in enabled:
        return "lookup_policy", {"topic": query}
    return None, None
