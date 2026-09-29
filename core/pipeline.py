"""Agent 管线：护栏 → 上下文工程（KB/工具/记忆/历史）→ 规则应答 或 LLM ReAct。

无 LLM 凭据或调用失败时自动降级为「规则 + 检索」，不抛错、不 500。
"""
import json
from dataclasses import dataclass, field

from core import config, guardrail, llm, memory as memory_mod, retrieval, store, tools as tools_mod


@dataclass
class ChannelMessage:
    text: str
    channel_type: str = "webchat"
    channel_user_id: str = "anonymous"
    group_id: str | None = None
    display_name: str = ""
    image_b64: str = ""


@dataclass
class AgentReply:
    text: str
    citations: list[str] = field(default_factory=list)
    confidence: float = 0.0
    escalate: bool = False
    escalate_reason: str = ""
    mode: str = "rule"
    tool_used: str | None = None
    tool_result: dict | None = None
    memory_note: str | None = None
    ctx_used: dict | None = None


def _relevant_history(history: list[dict], query: str, turns: int, relevant_only: bool) -> list[dict]:
    tail = history[-turns:] if turns > 0 else []
    if not relevant_only or len(history) <= turns:
        return tail
    q_big = retrieval.bigrams(query)
    scored = []
    for m in history[:-turns]:
        overlap = len(q_big & retrieval.bigrams(m.get("content", "")))
        if overlap > 0:
            scored.append((overlap, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:3]] + tail


def _summarize_history(history: list[dict]) -> str:
    if not history:
        return ""
    points = []
    seen = set()
    for m in history:
        c = (m.get("content") or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        points.append(f"{m.get('role')}: {c[:40]}")
    return "历史要点：" + "；".join(points[:8])


def build_context(query: str, history: list[dict], cfg: dict, visitor_id: str) -> dict:
    ctx_cfg = cfg.get("context", {})
    mem_cfg = cfg.get("memory", {})
    top_k = cfg.get("retrieval", {}).get("kb_top_k", 4)
    min_overlap = cfg.get("retrieval", {}).get("min_overlap", 0.0)
    modules_on = ctx_cfg.get("modules", {})
    used: dict = {}

    hits = retrieval.retrieve_scored(query, top_k, min_overlap) if modules_on.get("kb", True) else []
    kb_block = "\n".join(f"- [{h['id']}] {h['title']}: {h['answer']}" for _, h in hits) if hits else "（无相关条目）"
    used["kb"] = {"enabled": bool(modules_on.get("kb", True)), "hits": [e["id"] for _, e in hits]}

    tools = tools_mod.active_tools(cfg) if modules_on.get("tools", True) else {}
    tools_schema = tools_mod.tool_schema_block(cfg) if modules_on.get("tools", True) else []
    used["tools"] = {"enabled": bool(modules_on.get("tools", True)), "active": list(tools.keys())}

    mem: dict = {}
    if modules_on.get("memory", True) and mem_cfg.get("enable", True) and visitor_id:
        mem = store.get_memory(visitor_id)
        mem_hits = memory_mod.retrieve_relevant(query, mem, mem_cfg.get("retrieval_top_k", 3))
        mem_block = "；".join(f"{k}: {v}" for k, v in mem_hits) if mem_hits else "（暂无相关记忆）"
        used["memory"] = {"enabled": True, "hits": [k for k, _ in mem_hits]}
        if mem_cfg.get("visit_greeting", True) and store.session_count(visitor_id) > 1 and mem:
            used["memory"]["greeting"] = "欢迎回来！注意到您之前咨询过：" + "、".join(list(mem.keys())[:3])
    else:
        mem_block = "（记忆模块关闭）"

    turns = ctx_cfg.get("history_turns", 10)
    rel_hist = _relevant_history(history, query, turns, ctx_cfg.get("relevant_only", True))
    summary = ""
    if ctx_cfg.get("summarize", True) and len(history) > ctx_cfg.get("compress_threshold", 6) and modules_on.get("summary", True):
        summary = _summarize_history(history)
    used["history"] = {"turns": len(rel_hist), "relevant_only": ctx_cfg.get("relevant_only", True), "summarized": bool(summary)}

    return {
        "kb_block": kb_block,
        "tools_schema": tools_schema,
        "tools": tools,
        "memory_block": mem_block,
        "memory": mem,
        "history": rel_hist,
        "summary": summary,
        "persona": ctx_cfg.get("persona", "你是知客，专业的在线客服 Agent。"),
        "modules_on": modules_on,
        "used": used,
    }


def _rule_react(query: str, ctx: dict, cfg: dict) -> AgentReply:
    hits = retrieval.retrieve_scored(
        query,
        cfg.get("retrieval", {}).get("kb_top_k", 4),
        cfg.get("retrieval", {}).get("min_overlap", 0.0),
    )
    tools = ctx["tools"]
    tool_name, tool_args = tools_mod.detect_tool(query, tools)
    if tool_name:
        result = tools[tool_name](**tool_args)
        note = result.get("note") or json.dumps(result, ensure_ascii=False)
        return AgentReply(
            text=note,
            confidence=0.9,
            tool_used=tool_name,
            tool_result=result,
            memory_note=ctx["used"].get("memory", {}).get("greeting", ""),
            ctx_used=ctx["used"],
            mode="rule_react",
        )
    if hits:
        top_score, top = hits[0]
        conf = 0.6 + min(top_score, 0.6) + 0.05 * min(len(hits), 2)
        return AgentReply(
            text=f"【{top['title']}】{top['answer']}（依据 {top['id']}）",
            citations=[e["id"] for _, e in hits[:3]],
            confidence=min(conf, 0.95),
            memory_note=ctx["used"].get("memory", {}).get("greeting", ""),
            ctx_used=ctx["used"],
            mode="rule_react",
        )
    return AgentReply(
        text="这个问题我需要人工坐席进一步为您处理，正在为您转接…",
        confidence=0.2,
        escalate=True,
        escalate_reason="knowledge_miss",
        memory_note=ctx["used"].get("memory", {}).get("greeting", ""),
        ctx_used=ctx["used"],
        mode="rule_react",
    )


async def _llm_answer(query: str, ctx: dict, cfg: dict) -> AgentReply | None:
    data = await llm.react(query, ctx, cfg)
    if data is None:
        return None
    tools = ctx["tools"]
    tool_name = data.get("tool")
    tool_result = None
    if tool_name and tool_name in tools:
        args = data.get("tool_args") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            tool_result = tools[tool_name](**args)
        except TypeError:
            tool_result = {"tool": tool_name, "status": "bad_args", "note": "工具参数不合法。"}
        data["answer"] = (tool_result or {}).get("note") or data.get("answer") or "工具已执行。"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    answer = AgentReply(
        text=data.get("answer", ""),
        citations=data.get("citations", []) if isinstance(data.get("citations"), list) else [],
        confidence=confidence,
        escalate=bool(data.get("escalate", False)),
        escalate_reason=data.get("escalate_reason", ""),
        tool_used=tool_name,
        tool_result=tool_result,
        ctx_used=ctx["used"],
        mode="llm_react",
    )
    threshold = cfg.get("escalation", {}).get("confidence_threshold", 0.6)
    if answer.confidence < threshold and not answer.escalate:
        answer.escalate = True
        answer.escalate_reason = answer.escalate_reason or "low_confidence"
    return answer


async def run(message: ChannelMessage, history: list[dict], visitor_key: str) -> AgentReply:
    cfg = config.get_config()
    query = (message.text or "").strip()
    max_len = cfg.get("escalation", {}).get("max_message_len", 1000)
    if len(query) > max_len:
        query = query[:max_len]

    ctx = build_context(query, history, cfg, visitor_key)

    if cfg.get("guardrail", {}).get("injection_block", True):
        inj, inj_action = guardrail.detect_injection(query)
        if inj and inj_action == "block":
            return AgentReply(
                text="检测到包含越权/提示词注入的输入，本次消息已拦截并记录。请就具体问题咨询，我们会正常为您服务。",
                confidence=1.0,
                tool_used="injection_guard",
                mode="guardrail",
                ctx_used=ctx["used"],
            )

    if "转人工" in query:
        return AgentReply(
            text="好的，正在为您转接人工坐席，请稍候。",
            confidence=1.0,
            escalate=True,
            escalate_reason="visitor_requested",
            ctx_used=ctx["used"],
            mode="rule_react",
        )

    if message.image_b64 and cfg.get("multimodal", {}).get("enable", True):
        if not cfg.get("llm", {}).get("has_key", False):
            return AgentReply(
                text="已收到图片，当前为规则模式暂无法理解图片内容，正在为您转接人工坐席查看。",
                confidence=0.3,
                escalate=True,
                escalate_reason="image_needs_human",
                tool_used="vision",
                ctx_used=ctx["used"],
                mode="rule_react",
            )

    mode = cfg.get("engine", {}).get("mode", "auto")
    llm_reply = None
    if mode in ("llm", "auto") and cfg.get("llm", {}).get("has_key"):
        try:
            llm_reply = await _llm_answer(query, ctx, cfg)
        except Exception:  # aqg: top-level boundary
            llm_reply = None
        if llm_reply is None and mode == "llm":
            rule = _rule_react(query, ctx, cfg)
            rule.mode = "rule_fallback"
            return rule
    if llm_reply:
        return llm_reply
    return _rule_react(query, ctx, cfg)
