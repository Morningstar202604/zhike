"""Agent 管线：护栏 → 上下文工程（KB/工具/记忆/历史）→ 规则应答 或 LLM ReAct。

无 LLM 凭据或调用失败时自动降级为「规则 + 检索」，不抛错、不 500。
"""
import json
import re
from dataclasses import dataclass, field

from core import config, guardrail, llm, memory as memory_mod, retrieval, store, tools as tools_mod
from core.schemas import LlmReply
from core import tracing


@dataclass
class ChannelMessage:
    text: str
    channel_type: str = "webchat"
    channel_user_id: str = "anonymous"
    group_id: str | None = None
    display_name: str = ""
    image_b64: str = ""
    # 本次请求是否启用联网搜索（由前端/渠道指定，默认关闭）
    web_search: bool = False


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
    # 模型推理过程（LLM 模式下有值，规则模式为空）
    thought: str = ""
    # 访客明确要求忘记自己时置 True：调用方不得再把本轮文本写回记忆
    opt_out: bool = False


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


async def build_context_async(query: str, history: list[dict], cfg: dict, visitor_id: str,
                              web_search: bool = False) -> dict:
    """build_context 的异步版：额外支持联网搜索。

    搜索失败不影响主流程——把失败原因写进 used["web"]，但不抛异常。
    """
    ctx = build_context(query, history, cfg, visitor_id)
    ctx["web_block"] = ""
    ctx["used"]["web"] = {"enabled": bool(web_search), "hits": 0, "error": None}
    if not web_search or not query.strip():
        return ctx

    from core import websearch

    if not websearch.available():
        ctx["used"]["web"]["error"] = "未配置搜索服务"
        return ctx

    try:
        result = await websearch.search(query, top_k=4)
    except Exception as e:  # 搜索永远不能拖垮对话
        ctx["used"]["web"]["error"] = f"{type(e).__name__}"
        return ctx

    if result.get("ok"):
        block = websearch.format_for_context(result)
        ctx["web_block"] = block
        ctx["used"]["web"]["hits"] = len(result.get("results") or [])
        ctx["used"]["web"]["provider"] = result.get("provider")
    else:
        ctx["used"]["web"]["error"] = result.get("error")
    return ctx


def build_context(query: str, history: list[dict], cfg: dict, visitor_id: str) -> dict:
    ctx_cfg = cfg.get("context", {})
    mem_cfg = cfg.get("memory", {})
    top_k = cfg.get("retrieval", {}).get("kb_top_k", 4)
    min_overlap = cfg.get("retrieval", {}).get("min_overlap", 0.0)
    modules_on = ctx_cfg.get("modules", {})
    used: dict = {}

    emb_flag = cfg.get("retrieval", {}).get("embedding", "auto")
    use_emb = None if emb_flag == "auto" else (emb_flag == "on")
    used["kb"] = {"enabled": bool(modules_on.get("kb", True)), "hits": []}
    # 查询改写（零成本规则版）：短问句/指代开头时，用记忆里的最近话题补全检索意图
    # 记忆模块关闭时不能再用 last_topic 改写——否则「关闭记忆」形同虚设，
    # 且会把历史话题带进本次检索（隐私问题，已复现）。
    ret_query = query
    memory_on = modules_on.get("memory", True) and cfg.get("memory", {}).get("enable", True)
    if memory_on and query and (len(query) < 8 or re.match(r"^(它|这|那|该|此|怎么弄|怎么办|为什么|多少)", query)):
        last = (store.get_memory(visitor_id) or {}).get("last_topic", "").strip()
        if last and last not in query:
            ret_query = f"{last} {query}"
            used["kb"]["rewritten"] = last
    hits = retrieval.retrieve_scored(query, top_k, min_overlap, use_embedding=use_emb) if modules_on.get("kb", True) else []
    if ret_query != query:
        # 改写查询与原查询各检一遍，按条目取 max 合并：补语义召回的同时不稀释字面命中
        merged: dict[str, tuple[float, dict]] = {e["id"]: (s, e) for s, e in hits}
        for s, e in retrieval.retrieve_scored(ret_query, top_k, min_overlap, use_embedding=use_emb):
            if e["id"] not in merged or s > merged[e["id"]][0]:
                merged[e["id"]] = (s, e)
        hits = sorted(merged.values(), key=lambda x: x[0], reverse=True)[:top_k]
    kb_block = "\n".join(f"- [{h['id']}] {h['title']}: {h['answer']}" for _, h in hits) if hits else "（无相关条目）"
    used["kb"]["hits"] = [e["id"] for _, e in hits]

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
        "hits": hits,
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
    hits = ctx.get("hits") or []  # 复用 build_context 的检索结果，避免同参数二次检索
    tools = ctx["tools"]
    tool_name, tool_args = tools_mod.detect_tool(query, tools)
    if tool_name:
        result = tools[tool_name](**tool_args)
        note = result.get("note") or json.dumps(result, ensure_ascii=False)
        # 工具明确说"没找到"或"未接入"时必须真的转人工——
        # 否则对客承诺与系统行为不一致，或让用户误以为查到了真数据。
        missed = result.get("found") is False or result.get("connected") is False
        return AgentReply(
            text=note,
            confidence=0.4 if missed else 0.9,
            tool_used=tool_name,
            tool_result=result,
            escalate=missed,
            escalate_reason="knowledge_miss" if missed else "",
            memory_note=ctx["used"].get("memory", {}).get("greeting", ""),
            ctx_used=ctx["used"],
            mode="rule_react",
        )
    if hits:
        top_score, top = hits[0]
        conf = 0.6 + min(top_score, 0.6) + 0.05 * min(len(hits), 2)
        return AgentReply(
            # 面向用户的自然表达：不再把内部标题和 KB 编号直接甩给用户。
            # 引用信息由 citations 字段承载，前端会以徽章形式展示。
            text=top["answer"],
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


async def _llm_answer(query: str, ctx: dict, cfg: dict, image_b64: str = "") -> AgentReply | None:
    data = await llm.react(query, ctx, cfg, image_b64)
    if data is None:
        return None
    return _make_llm_reply(data, ctx, cfg)


def _make_llm_reply(data: dict, ctx: dict, cfg: dict) -> AgentReply:
    """LLM ReAct 结果 → AgentReply（含工具执行与低置信度兜底转人工）；run() 与 run_stream() 共用。"""
    tools = ctx["tools"]
    parsed = LlmReply.safe_parse(data)  # 结构化输出校验，替代裸 dict.get
    tool_name = parsed.tool
    tool_result = None
    if tool_name and tool_name in tools:
        args = parsed.tool_args or {}
        try:
            tool_result = tools[tool_name](**args)
        except TypeError:
            tool_result = {"tool": tool_name, "status": "bad_args", "note": "工具参数不合法。"}
        parsed.answer = (tool_result or {}).get("note") or parsed.answer or "工具已执行。"

    # 输出侧护栏：LLM 回答若泄露系统提示/Key，替换为安全话术并转人工
    bad_output = guardrail.detect_output(parsed.answer)
    if bad_output:
        parsed.answer = "抱歉，我无法回答这个问题。请问您还有其他关于订单、退款或政策的问题吗？"
        parsed.escalate = True
        parsed.escalate_reason = "output_guardrail"

    answer = AgentReply(
        text=parsed.answer,
        citations=parsed.citations,
        confidence=parsed.confidence,
        escalate=parsed.escalate,
        escalate_reason=parsed.escalate_reason,
        tool_used=tool_name,
        tool_result=tool_result,
        ctx_used=ctx["used"],
        mode="llm_react",
        thought=(parsed.thought or "")[:2000],
    )
    threshold = cfg.get("escalation", {}).get("confidence_threshold", 0.6)
    if answer.confidence < threshold and not answer.escalate:
        answer.escalate = True
        answer.escalate_reason = answer.escalate_reason or "low_confidence"
    return answer


# 记忆 opt-out 触发词（访客说"不要记住我/忘记我"时清记忆）
# 注意：中文里"记住我"中间隔了"住"，早期写成 (记|保存)我 会漏掉最常见的说法。
# 另用负向先行排除"不要忘记我"——语义相反，不能当退出处理。
_MEMORY_OPT_OUT_PATTERNS = [
    re.compile(r"(不要|别|停止|不用)(再)?.{0,4}(记(住)?|保存|存储|保留).{0,2}我(?!的生日)", re.IGNORECASE),
    re.compile(r"(不要|别)记住我", re.IGNORECASE),
    re.compile(r"忘记(我|所有|一切)(关于我)?的?(记忆|信息|偏好|资料)", re.IGNORECASE),
    re.compile(r"(清除|删除|清空).{0,4}(我的)?(记忆|偏好|个人信息)", re.IGNORECASE),
    re.compile(r"(关闭|关掉|停用).{0,4}(记忆|个性化)", re.IGNORECASE),
]

# 反向语义：出现这些说法时不做 opt-out（"不要忘记我"是挽留，不是退出）
_MEMORY_OPT_OUT_NEGATIVE = [
    re.compile(r"(不要|别)忘记我", re.IGNORECASE),
]


def _maybe_opt_out_memory(query: str, visitor_id: str) -> str | None:
    if any(pat.search(query) for pat in _MEMORY_OPT_OUT_NEGATIVE):
        return None
    for pat in _MEMORY_OPT_OUT_PATTERNS:
        if pat.search(query):
            store.clear_memory(visitor_id, opt_out=True)
            return "好的，已为您清除所有记忆，后续不会主动记录您的偏好。"
    return None


async def _prepare(message: ChannelMessage, history: list[dict], visitor_key: str):
    """run()/run_stream() 公共前置：截断、上下文、护栏/转人工/图片规则拦截。拦截时直接返回 AgentReply。"""
    cfg = config.get_config()
    query = (message.text or "").strip()
    max_len = cfg.get("escalation", {}).get("max_message_len", 1000)
    if len(query) > max_len:
        query = query[:max_len]

    # 记忆 opt-out：访客说"不要记我/忘记我"直接拦截并清记忆
    opt_out_reply = _maybe_opt_out_memory(query, visitor_key)
    if opt_out_reply:
        ctx = await build_context_async(query, history, cfg, visitor_key, message.web_search)
        return AgentReply(
            text=opt_out_reply,
            confidence=1.0,
            mode="rule_react",
            ctx_used=ctx["used"],
            opt_out=True,
        )

    ctx = await build_context_async(query, history, cfg, visitor_key, message.web_search)

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
        # 配了视觉模型且有 Key → 交给 LLM 看图；否则规则模式只能转人工
        vision_ready = bool((cfg.get("multimodal", {}) or {}).get("vision_model")) and cfg.get("llm", {}).get("has_key", False)
        if not vision_ready:
            return AgentReply(
                text="已收到图片，当前为规则模式暂无法理解图片内容，正在为您转接人工坐席查看。",
                confidence=0.3,
                escalate=True,
                escalate_reason="image_needs_human",
                tool_used="vision",
                ctx_used=ctx["used"],
                mode="rule_react",
            )
    return cfg, query, ctx


async def run(message: ChannelMessage, history: list[dict], visitor_key: str) -> AgentReply:
    with tracing.span("zhike.pipeline.run", {"channel": message.channel_type, "visitor": visitor_key}):
        prepared = await _prepare(message, history, visitor_key)
        if isinstance(prepared, AgentReply):
            return prepared
        cfg, query, ctx = prepared

        mode = cfg.get("engine", {}).get("mode", "auto")
        llm_reply = None
        if mode in ("llm", "auto") and cfg.get("llm", {}).get("has_key"):
            with tracing.span("zhike.llm", {"model": cfg["llm"].get("model", "")}):
                try:
                    llm_reply = await _llm_answer(query, ctx, cfg, image_b64=message.image_b64)
                except Exception:
                    llm_reply = None
            if llm_reply is None and mode == "llm":
                rule = _rule_react(query, ctx, cfg)
                rule.mode = "rule_fallback"
                return rule
        if llm_reply:
            return llm_reply
        return _rule_react(query, ctx, cfg)


async def run_stream(message: ChannelMessage, history: list[dict], visitor_key: str):
    """流式管线：产出 ("delta", str) 增量与 ("final", AgentReply) 终态。

    delta 仅供打字机呈现；final.text 为权威内容（可能因工具调用/降级与流式片段不同，消费方须以终态替换）。
    """
    prepared = await _prepare(message, history, visitor_key)
    if isinstance(prepared, AgentReply):
        yield ("delta", prepared.text)
        yield ("final", prepared)
        return
    cfg, query, ctx = prepared

    mode = cfg.get("engine", {}).get("mode", "auto")
    final: AgentReply | None = None
    # 必须在分支外初始化：规则模式（无 LLM Key）走不到 if 内部，
    # 后面 `if not deltas_sent` 会 UnboundLocalError —— 而规则模式正是默认路径，
    # 等于「没配 Key 时流式对话必崩」（已复现，前端只看到兜底话术）。
    deltas_sent = False
    if mode in ("llm", "auto") and cfg.get("llm", {}).get("has_key"):
        data = None
        try:
            async for ev in llm.react_stream(query, ctx, cfg, message.image_b64):
                if ev["type"] == "delta":
                    deltas_sent = True
                    yield ("delta", ev["text"])
                elif ev["type"] == "final":
                    data = ev["data"]
                    break
                elif ev["type"] == "error":
                    break
        except Exception:  # aqg: top-level boundary
            data = None
        if data is not None:
            final = _make_llm_reply(data, ctx, cfg)
        elif not deltas_sent:
            # 流式未产出任何内容：按非流式再试一次（内部含多模型备援），仍失败才回规则
            try:
                final = await _llm_answer(query, ctx, cfg, image_b64=message.image_b64)
            except Exception:  # aqg: top-level boundary
                final = None
    if final is None:
        final = _rule_react(query, ctx, cfg)
        if mode == "llm" and cfg.get("llm", {}).get("has_key"):
            final.mode = "rule_fallback"
    # 已向客户端发过 delta 时不再补发 delta：
    # 否则前端会把「半截 LLM 文本 + 规则话术」拼在一起显示（已复现）。
    # final 仍完整返回，前端以它为准覆盖。
    if not deltas_sent:
        yield ("delta", final.text)
    yield ("final", final)
