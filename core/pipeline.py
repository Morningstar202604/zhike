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
    # 群聊氛围：@ 之前群里最近几条闲聊（网关收集），让机器人知道大家在聊什么
    ambient: list[str] = field(default_factory=list)
    # 群名（网关 get_group_info 提供），注入场景感知
    group_name: str = ""


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
    tool_missed = False
    if tool_name and tool_name in tools:
        args = parsed.tool_args or {}
        try:
            tool_result = tools[tool_name](**args)
        except TypeError:
            tool_result = {"tool": tool_name, "status": "bad_args", "note": "工具参数不合法。"}
        parsed.answer = (tool_result or {}).get("note") or parsed.answer or "工具已执行。"
        # 工具自己说「没接入 / 没查到」时必须真的转人工：否则对客话术承诺了
        # 「已转人工」，而系统里既没有工单也没有升级状态（LLM 模式下已复现）。
        tool_missed = (tool_result or {}).get("connected") is False or (tool_result or {}).get("found") is False

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
    if tool_missed and not answer.escalate:
        answer.escalate = True
        answer.escalate_reason = "knowledge_miss"
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


_FACT_RE = __import__("re").compile(
    r"(什么|为什么|怎么|如何|是谁|多少|几点|最新|今天|昨天|明天|新闻|价格|多少钱|"
    r"发布|上线|版本|哪一年|介绍一下|讲讲|了解|知道|查|攻略|教程|对比|评测|新闻|"
    r"who|what|when|where|why|how|latest|news|price)", __import__("re").IGNORECASE,
)


def _wants_facts(query: str) -> bool:
    """轻启发式：判断一条渠道消息是否像在寻求事实/资讯（值得自动联网）。"""
    q = (query or "").strip()
    return len(q) >= 4 and bool(_FACT_RE.search(q))


async def _prepare(message: ChannelMessage, history: list[dict], visitor_key: str):
    """run()/run_stream() 公共前置：截断、上下文、护栏/转人工/图片规则拦截。拦截时直接返回 AgentReply。"""
    cfg = config.get_config()
    # 角色卡（SillyTavern chara_card 对标）：必须在 build_context 之前合成——
    # ctx["persona"] 由 cfg["context"]["persona"] 派生，晚了就定型了。
    try:
        chat_key = f"{message.channel_type}:{message.group_id or message.channel_user_id}"
        card = store.card_for_chat(chat_key)
    except Exception:
        card = None
    if card and card.get("persona"):
        cfg["context"] = {**cfg.get("context", {}), "persona": str(card["persona"])}
        if card.get("temperature") is not None:
            cfg["llm"] = {**cfg.get("llm", {}), "temperature": float(card["temperature"])}
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

    # 联网自启（chatgpt-on-wechat「不懂就查」对标）：渠道消息像在寻求事实/资讯
    # 且搜索服务可用时自动开搜，用户无需手动开开关。失败静默，不影响主流程。
    web = message.web_search
    if not web:
        from core import websearch
        if websearch.available() and _wants_facts(query):
            web = True

    ctx = await build_context_async(query, history, cfg, visitor_key, web)
    # 反思修正：把"最近高频毛病"作为自我修正提醒注入上下文（带缓存，60s 内不重复查库）
    try:
        ctx["self_fix"] = _self_fix_hint()
    except Exception:  # 自检绝不影响主链路
        ctx["self_fix"] = ""
    # 群聊氛围与群名：@ 之前大家在聊什么、这是哪个群——场景感知
    if getattr(message, "ambient", None):
        lines = [ln for ln in message.ambient if ln and ln.strip()][-8:]
        if lines:
            block = "群聊氛围（@ 你之前群里最近在聊的，供你接梗用，别逐条回复）：\n" + "\n".join(lines)
            ctx["ambient_block"] = block[:1200]
    if getattr(message, "group_name", ""):
        ctx["group_name"] = message.group_name

    if cfg.get("guardrail", {}).get("injection_block", True):
        inj, inj_action = guardrail.detect_injection(query)
        if inj and inj_action == "block":
            return AgentReply(
                # 用户可见文本刻意中性且不点破机制（说了"拦截/记录"等于向攻击者确认防线存在）；
                # 审计留痕在入口侧完成，这里只管体面地闭麦。
                text="这个话题就到此为止吧。有别的想聊的，随时开口。",
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


# ---- 反思修正：回复自检 + 高频毛病动态注入（从自己的错误里进化）----


def _critique_reply(text: str, history: list[dict]) -> list[str]:
    """规则自检一条回复，返回毛病清单（免费、毫秒级、零 LLM 成本）。"""
    text = text or ""
    issues: list[str] = []
    if len(text) > 240:
        issues.append("回复过长——群聊闲聊三四句就该说完，别写小作文")
    if "**" in text or "##" in text or "`" in text:
        issues.append("回复里出现了 markdown 符号（QQ 是纯文字环境）")
    if text.count("\n\n") >= 2:
        issues.append("分段太多——聊天不是写作文")
    if len(re.findall(r"\[[^\[\]]{1,6}\]", text)) > 3:
        issues.append("表情标记堆太多——一次最多一两个")
    # 复读检测：与最近一条 agent/坐席回复开头雷同
    for m in reversed(history or []):
        if m.get("role") in ("agent", "operator"):
            prev = (m.get("content") or "").strip()
            if prev and text.strip()[:24] == prev[:24] and len(text) > 30:
                issues.append("开头和上一条回答一模一样——换个说法，别复读")
            break
    return issues


_SELF_FIX_CACHE: tuple[float, str] = (0.0, "")


def _self_fix_hint() -> str:
    """高频毛病 → system prompt 自我修正提醒（60s 缓存）。"""
    import time as _time

    now = _time.monotonic()
    if now - _SELF_FIX_CACHE[0] < 60:
        return _SELF_FIX_CACHE[1]
    hint = ""
    try:
        issues = store.critique_summary()
        if issues:
            hint = "自我修正提醒（这是你最近回复里反复出现的问题，这一轮必须避免）：\n" + \
                   "\n".join(f"- {i}" for i in issues[:3])
    except Exception:
        hint = ""
    _SELF_FIX_CACHE[:] = (now, hint)
    return hint


def _maybe_critique(reply, ctx: dict, visitor_key: str) -> None:
    """LLM 回复落库前自检，毛病写入 reply_critiques 供反思修正。绝不抛异常。"""
    try:
        issues = _critique_reply(reply.text or "", ctx.get("history") or [])
        store.critique_add(visitor_key, len(reply.text or ""), issues)
    except Exception:  # aqg: 自检故障不影响回复
        pass


async def run(message: ChannelMessage, history: list[dict], visitor_key: str) -> AgentReply:
    with tracing.span("zhike.pipeline.run", {"channel": message.channel_type, "visitor": visitor_key}):
        prepared = await _prepare(message, history, visitor_key)
        if isinstance(prepared, AgentReply):
            return prepared
        cfg, query, ctx = prepared

        # 标注回复（Dify annotations 对标）：命中管理员标注的标准答案，直接回复，
        # 不走 KB/LLM——最便宜、最可控、零延迟。放在所有引擎分支之前。
        ann = store.annotation_hit(query)
        if ann:
            return AgentReply(
                text=ann["answer"], mode="annotation", confidence=1.0,
                citations=[f"标注#{ann['id']}"],
            )

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
            _maybe_critique(llm_reply, ctx, visitor_key)
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

    # 标注回复：流式入口同样先查标注（与 run() 语义一致）
    ann = store.annotation_hit(query)
    if ann:
        reply = AgentReply(
            text=ann["answer"], mode="annotation", confidence=1.0,
            citations=[f"标注#{ann['id']}"],
        )
        yield ("delta", reply.text)
        yield ("final", reply)
        return

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
    if final is not None and str(final.mode or "").startswith("llm"):
        _maybe_critique(final, ctx, visitor_key)
    yield ("final", final)
