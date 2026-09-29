"""工具注册表：内置实现 + DB 数据驱动开关；未知工具走检索或占位执行器。"""
import json
import re

from core import retrieval, store


def tool_query_order(order_id: str) -> dict:
    return {
        "order_id": order_id,
        "status": "shipped",
        "carrier": "SF Express",
        "tracking_no": "SF" + order_id[-10:],
        "eta": "2 天内送达",
        "note": f"订单 {order_id} 已发货，快递 {order_id[-6:]}，预计 2 天内送达。",
    }


def tool_initiate_refund(order_id: str, reason: str = "") -> dict:
    return {
        "order_id": order_id,
        "refund_id": f"R-{order_id[-8:]}",
        "status": "pending_review",
        "reason": reason or "未说明",
        "note": f"退款申请 R-{order_id[-8:]} 已提交，坐席将在 1 个工作日内审核。",
    }


def tool_lookup_policy(topic: str) -> dict:
    hits = retrieval.retrieve(topic, top_k=2)
    return {
        "topic": topic,
        "found": bool(hits),
        "entries": [
            {"id": h["id"], "title": h["title"], "answer": h["answer"]} for h in hits
        ],
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
    def run(**kw):
        return {
            "tool": t["id"],
            "status": "stub",
            "note": f"工具「{t.get('name') or t['id']}」已注册但尚未接入真实后端，返回占位结果。",
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
