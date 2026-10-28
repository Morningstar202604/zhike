"""Agent 管线：护栏 → 上下文工程（KB/工具/记忆/历史）→ 规则应答 或 LLM ReAct。

无 LLM 凭据或调用失败时自动降级为「规则 + 检索」，不抛错、不 500。
"""
import asyncio
import json
import random
import re
from dataclasses import dataclass, field

from core import persona as persona_mod
from core import config, guardrail, llm, memory as memory_mod, retrieval, store, tools as tools_mod
from core.schemas import LlmReply
from core import tracing
from core.tool_context import EMPTY, ToolContext


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
    # 触发消息的 OneBot message_id（「给这条点赞/撤回这条」要用）
    message_id: str = ""


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
        "persona": ctx_cfg.get("persona") or persona_mod.default_persona(),
        "modules_on": modules_on,
        "used": used,
    }


_URL_RE = re.compile(r"https?://[^\s，。；、）)\]】>]+")
_READ_HINT = re.compile(r"看|读|总结|内容|讲了|说了|是什么|介绍|分析|打开|翻译")
# 模型声明「发不了图」的典型句式：要表情包时出现这些就等于把功能说死了
_EMOTE_REFUSAL_RE = re.compile(
    r"(没法|不能|无法|没办法|不会)(直接)?(发|发送|贴|给你发).{0,6}(图片|图|表情)|"
    r"(我|这边).{0,6}(发不了|不能发|不支持发).{0,4}(图|表情)")


_FILE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s，。；、）)\]】>]+"            # Windows 绝对路径（正/反斜杠都认）
    r"|~?/(?:Users|home|tmp|var)/[^\s，。；、）)\]】>]+)"  # POSIX 绝对路径
)
_FILE_HINT = re.compile(r"读一下|读读|看看|打开|内容|讲了|说了|写的什么|总结|是什么")


async def _prefetch_file(query: str, ctx: dict, cfg: dict) -> AgentReply | None:
    """消息里给了明确的本地文件路径且要求读内容时，先读再交给模型。

    与 _prefetch_url 同因：模型不会稳定地主动选 read_file，实测会先推脱
    「我碰不到你的 C 盘」。与其反复调 prompt，不如把确定性的事做在前面。
    """
    if not ctx.get("modules_on", {}).get("tools", True):
        return None
    m = _FILE_RE.search(query or "")
    if not m or not _FILE_HINT.search(query or ""):
        return None
    path = m.group(0).rstrip("\"'").strip()
    res = await tools_mod.invoke("read_file", {"path": path}, ctx.get("tool_ctx") or EMPTY)
    if not res.get("found"):
        # 读不到就明确说原因（越权/不存在/执行器离线），不要装傻
        note = res.get("note") or "读不到这个文件。"
        return AgentReply(text=note, mode="rule_react", confidence=0.5,
                          tool_used="read_file", tool_result=res)

    injected = dict(ctx)
    injected["read_block"] = (
        f"以下是你刚读到的文件内容（{path}）。直接基于内容回答，"
        f"不要提“系统日志/工具返回/搜索结果”这类机制：\n{str(res.get('note') or '')[:3000]}")
    mode = cfg.get("engine", {}).get("mode", "auto")
    if mode in ("llm", "auto") and cfg.get("llm", {}).get("has_key"):
        try:
            data = await llm.react(query, injected, cfg)
            if data:
                return await _make_llm_reply(data, injected, cfg)
        except Exception:
            pass
    return AgentReply(text=f"{path} 的内容：\n{str(res.get('note') or '')[:800]}",
                      mode="rule_react", confidence=0.7,
                      tool_used="read_file", tool_result=res)


_REMEMBER_RE = re.compile(r"^(?:请|帮我)?(?:记住|记一下|记下|存一下)[:：，,\s]*(.+)$")
_RECALL_RE = re.compile(r"(?:你还?记得|我之前说过|我上次说|回忆一下|想起来)")


async def _direct_emote(query: str, ctx: dict, cfg: dict) -> AgentReply | None:
    """「发个表情包」这类请求：直接挑图并留下标记，不赌模型记不记得语法。

    用户要的是图，不是一段关于图的解释。模型历史上会回「我没法发图片」，
    多轮 prompt 修补都不稳；这里把它做成确定性路径，成功率 100%。
    """
    from core import intent as intent_mod

    # 以本句原话为准重新定性：ctx 里的 intent 属于整轮上下文，函数被单独调用时不该依赖它
    it = intent_mod.classify(query)
    if it.name != intent_mod.INTENT_EMOTE:
        return None
    if "search_emote" not in (ctx.get("tools") or {}):
        return None
    kw = intent_mod.emote_keyword(query)
    res = await tools_mod.invoke_with(ctx["tools"], "search_emote", {"keyword": kw},
                                      ctx.get("tool_ctx") or EMPTY)
    if not isinstance(res, dict):
        res = {"note": str(res)[:200]}
    if res.get("status") == "empty" or res.get("found") is False and not res.get("note"):
        return AgentReply(text="库里还没攒到图，等我再收几张，喵～", confidence=0.6,
                          mode="rule_react", ctx_used=ctx.get("used"))
    lead = random.choice(["喏。", "给。", "拿去。", "接着。"])
    marker = f"[表情包:{kw or '随便'}]"
    return AgentReply(text=f"{lead}{marker}", tool_used="search_emote", tool_result=res,
                      confidence=0.9, mode="rule_react", ctx_used=ctx.get("used"))


async def _direct_tool(query: str, ctx: dict, cfg: dict) -> AgentReply | None:
    """确定性工具意图 → 直接执行，不等模型决定。

    只拦截语义无歧义的表达（提醒/记住/回忆/待办列表）。
    拿不到必要信息时返回 None，让正常引擎去追问，不硬造参数。
    """
    if not ctx.get("modules_on", {}).get("tools", True):
        return None
    q = (query or "").strip()
    tctx = ctx.get("tool_ctx") or EMPTY

    name, args = tools_mod.detect_tool(q, ctx["tools"])
    if not name:
        m = _REMEMBER_RE.match(q)
        if m and "remember" in ctx["tools"]:
            pair = m.group(1).strip()
            # 「记住 X 是 Y」「记住我喜欢喝美式」→ key 取最短可识别前缀
            key, value = _split_memory(pair)
            if key and value:
                name, args = "remember", {"key": key, "value": value}
    if not name:
        return None

    res = await tools_mod.invoke_with(ctx["tools"], name, args, tctx)
    note = res.get("note") or ""
    if not note:
        return None
    missed = res.get("found") is False or res.get("connected") is False
    # need_context（追问参数）与 empty（没待办/没记录）都是正常回合，不升级。
    # 这里曾只判 need_context，导致「列一下我的待办」在空列表时开出一张工单。
    benign = res.get("status") in ("need_context", "empty")
    reply = AgentReply(
        text=note,
        confidence=0.4 if missed else 0.95,
        tool_used=name,
        tool_result=res,
        escalate=missed and not benign,
        escalate_reason="knowledge_miss" if (missed and not benign) else "",
        ctx_used=ctx["used"],
        mode="rule_react",
    )
    # 让回复保留人设腔调：把工具结果交给模型润色（失败就用原文）
    if cfg.get("llm", {}).get("has_key") and not missed and not benign:
        try:
            styled = dict(ctx)
            styled["read_block"] = (
                f"你已经替用户办好了这件事，结果是：{note}\n"
                f"用你的口吻回一句确认，保持简短（一两句），不要提工具/系统/数据库。")
            data = await llm.react(query, styled, cfg)
            if data:
                polished = await _make_llm_reply(data, styled, cfg)
                polished.tool_used = name
                polished.tool_result = res
                polished.escalate = False
                return polished
        except Exception:
            pass
    return reply


def _split_memory(pair: str) -> tuple[str, str]:
    """「我喜欢喝美式」→ (喜欢喝, 美式)；「我下周三出差」→ (下周三, 出差)。

    没有可靠切分点时返回 ("", "")——宁可让模型去追问，不塞错键。
    """
    text = (pair or "").strip("，,。. ")
    if not text:
        return "", ""
    m = re.match(r"^(?:我)?(喜欢|爱|讨厌|不吃|吃|喝|住|在|的|叫|是)(.+)$", text)
    if m:
        return m.group(1), m.group(2).strip()
    m = re.match(r"^([^，,。]{1,6}?)(?:是|在|要|有)(.+)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # 兜底：整句作为值，键用「备注」
    return "备注", text


async def _prefetch_url(query: str, ctx: dict, cfg: dict) -> AgentReply | None:
    """消息里带明确链接且意图是「看内容」时，先抓正文再让模型总结。

    返回 None 表示没触发（交给正常引擎分支）。
    抓不到（内网/打不开/没文字）也返回 None——不能让一次抓取失败吞掉正常对话。
    """
    if not ctx.get("modules_on", {}).get("tools", True):
        return None
    m = _URL_RE.search(query or "")
    if not m or not _READ_HINT.search(query or ""):
        return None
    url = m.group(0)
    res = await tools_mod.invoke("read_webpage", {"url": url}, ctx.get("tool_ctx") or EMPTY)
    if not res.get("found"):
        return None
    body = str(res.get("note") or "")
    if len(body) < 40:
        return None

    injected = dict(ctx)
    injected["read_block"] = (
        f"以下是你刚打开的网页正文（{url}）。直接基于内容回答，"
        f"不要提“系统日志/工具返回/搜索结果”这类机制：\n{body[:3000]}")
    mode = cfg.get("engine", {}).get("mode", "auto")
    if mode in ("llm", "auto") and cfg.get("llm", {}).get("has_key"):
        try:
            data = await llm.react(query, injected, cfg)
            if data:
                return await _make_llm_reply(data, injected, cfg)
        except Exception:
            pass
    return AgentReply(
        text=f"{url} 的内容：\n{body[:800]}",
        mode="rule_react", confidence=0.7,
        tool_used="read_webpage", tool_result=res,
    )


async def _rule_react(query: str, ctx: dict, cfg: dict) -> AgentReply:
    hits = ctx.get("hits") or []  # 复用 build_context 的检索结果，避免同参数二次检索
    tool_name, tool_args = tools_mod.detect_tool(query, ctx["tools"])
    if tool_name:
        result = await tools_mod.invoke_with(ctx.get("tools"), tool_name, tool_args,
                                            ctx.get("tool_ctx") or EMPTY)
        note = result.get("note") or json.dumps(result, ensure_ascii=False)
        # 工具明确说"没找到"或"未接入"时必须标记升级——
        # 否则对客承诺与系统行为不一致，或让用户误以为查到了真数据。
        # status=empty（没待办/没记录）是正常答案，不是知识缺口，不能开升级工单。
        missed = (result.get("found") is False or result.get("connected") is False)
        # need_context（缺参数）追问用户；empty（本来就空）据实回答；两者都不升级
        benign = result.get("status") in ("need_context", "empty")
        return AgentReply(
            text=note,
            confidence=0.4 if missed else 0.9,
            tool_used=tool_name,
            tool_result=result,
            escalate=missed and not benign,
            escalate_reason="knowledge_miss" if (missed and not benign) else "",
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
    # 规则兜底也不能是客服腔：模型挂了、知识库没命中时，这句话是用户唯一能看到的
    # 内容。写成「这个我暂时答不上来」等于当场承认自己是机器人。
    return AgentReply(
        text="这个我真没接住，换个说法或者把背景多给两句，我再来一遍，喵～",
        confidence=0.2,
        escalate=True,
        escalate_reason="knowledge_miss",
        memory_note=ctx["used"].get("memory", {}).get("greeting", ""),
        ctx_used=ctx["used"],
        mode="rule_react",
    )


async def _llm_answer(query: str, ctx: dict, cfg: dict, image_b64: str = "",
                       first: dict | None = None, prefer: str = "") -> AgentReply | None:
    """单轮 LLM + 最多两轮工具回填（ReAct 的 OBSERVE 补齐）。

    为什么必须有这层：单轮时代模型选完工具就被当成「回答完了」，
    `_make_llm_reply` 只能拿工具 note 当答复——用户看到的是「正在为您查询」，
    或者干脆一串空文本（实测 u2-flash 在联网题上稳定复现）。
    这里按 ReAct 语义执行工具并把 observation 回填，直到模型给出正文；
    最后再用「无工具模式」逼一次答案，避免无限要工具。

    first：调用方已经拿到的一次决策（流式路径复用，省掉一次重复请求）。
    """
    visible = {t.get("name") for t in (ctx.get("tools_schema") or []) if isinstance(t, dict)}
    tools_map = ctx.get("tools") or {}
    observations: list[str] = []
    working = dict(ctx)
    if prefer:
        # 择优候选的偏好通过 ctx 传下去，不改 react() 的调用签名
        working["_prefer"] = prefer
    # 工具执行轨迹要带回 AgentReply：控制台/日志靠它证明「真的调用了搜索」，
    # 之前无论调了多少工具都写 tool_used=None，看起来像整套工具全失效。
    used_tools: list[str] = []
    last_result: dict | None = None

    def _with_tools(reply: AgentReply) -> AgentReply:
        if used_tools:
            reply.tool_used = used_tools[-1]
            reply.tool_result = last_result
            if len(used_tools) > 1:
                reply.tool_result = {**(last_result or {}), "tool_chain": used_tools}
        return reply

    # 最多 2 轮：推理模型每轮 30-90s，3 轮会顶到网关超时（实测 95.7s 后整体失败）
    for round_no in range(2):
        data = first if (round_no == 0 and first is not None) else await llm.react(
            query, working, cfg, image_b64)
        if data is None:
            return None
        answer = str(data.get("answer") or "").strip()
        tool_name = str(data.get("tool") or "").strip()
        if tool_name and tool_name in visible and tool_name in tools_map:
            args = data.get("tool_args") or {}
            # 单个工具的异常绝不能打断整轮对话：历史上 search_emote 按 dict 解析
            # 数组索引抛 AttributeError，整轮直接掉回「这个我暂时答不上来」。
            try:
                result = await tools_mod.invoke_with(tools_map, tool_name, args,
                                                     ctx.get("tool_ctx") or EMPTY)
            except Exception as exc:  # aqg: tool boundary
                result = {"tool": tool_name, "status": "error",
                          "note": f"工具执行出错（{type(exc).__name__}），换个方式回答用户。"}
            if not isinstance(result, dict):
                result = {"tool": tool_name, "note": str(result)[:500]}
            note = str((result or {}).get("note") or "")
            observations.append(f"[{tool_name}] {note[:1500]}")
            used_tools.append(tool_name)
            last_result = result if isinstance(result, dict) else None
            if (result or {}).get("connected") is False:
                # 工具没接入：再让模型选工具也没有新信息，直接进「无工具作答」分支
                break
            working = dict(working)
            working["read_block"] = (
                "你刚才调用的工具结果如下，直接据此回答，不要再提工具/系统："
                + chr(10) + chr(10).join(observations[-3:]))
            if answer and not llm.is_promise_only(answer):
                data["tool"] = None
                return _with_tools(await _make_llm_reply(data, working, cfg))
            continue
        if answer and not llm.is_promise_only(answer):
            data["tool"] = None
            return _with_tools(await _make_llm_reply(data, working, cfg))
        break

    # 兜底：关掉工具再问一次，模型只能自己作答
    forced = dict(ctx)
    forced["tools_schema"] = []
    forced["read_block"] = (
        "现在没有可用工具，也不要表示要去查询或让用户等待。"
        "用你已经知道的信息直接回答；不确定的数字明确写「这一点需你确认」，不要编造。"
        + (chr(10) + "已知工具结果：" + chr(10) + chr(10).join(observations[-3:]) if observations else ""))
    try:
        final = await llm.react(query, forced, cfg, image_b64)
    except Exception:
        final = None
    forced_answer = str((final or {}).get("answer") or "").strip()
    if forced_answer and not llm.is_promise_only(forced_answer):
        final["tool"] = None
        return _with_tools(await _make_llm_reply(final, forced, cfg))
    # 连「不许再拖」都只换来一句「我这就去查」：不能再把空话发给用户。
    # 明确说明能力边界，比假装正在处理诚实。
    honest = AgentReply(
        text=("这次没查到能直接引用的实时数据，我不瞎编。"
              "你可以把要对比的页面或截图发我，我读完再给结论；"
              "或者我先按已有常识给一版框架，具体数字你核对官方页面。"),
        confidence=0.35,
        mode="llm_react",
        tool_used=None,
        ctx_used=(ctx or {}).get("used"),
    )
    return _with_tools(honest)


async def _make_llm_reply(data: dict, ctx: dict, cfg: dict) -> AgentReply:
    """LLM ReAct 结果 → AgentReply（含工具执行与低置信度兜底话术）；run() 与 run_stream() 共用。"""
    tools = ctx["tools"]
    parsed = LlmReply.safe_parse(data)  # 结构化输出校验，替代裸 dict.get
    tool_name = parsed.tool
    tool_result = None
    tool_missed = False
    # 只认「展示给模型的工具清单」里的名字：模型有时会凭记忆点一个没配置的工具
    # （如没配搜索时硬选 web_search）。那种情况不该去执行——执行结果必然是
    # 「未接入」，还会把用户答案顶掉。直接当作没有工具，让模型的正文生效。
    # 只在「调用方给了明确可见清单」时做白名单过滤：
    # 空清单意味着调用方（单测/自定义实现）直接给了 tools 映射，
    # 这时按映射判断即可，不能因为 schema 为空就把工具全否掉。
    schema_list = [t for t in (ctx.get("tools_schema") or []) if isinstance(t, dict)]
    if schema_list:
        visible_tools = {t.get("name") for t in schema_list}
        if tool_name and tool_name not in visible_tools:
            tool_name = None
    if tool_name and tool_name in tools:
        args = parsed.tool_args or {}
        try:
            tool_result = await tools_mod.invoke_with(ctx.get("tools"), tool_name, args,
                                                      ctx.get("tool_ctx") or EMPTY)
        except Exception as exc:  # aqg: tool boundary
            tool_result = {"tool": tool_name, "status": "error",
                           "note": f"工具执行出错（{type(exc).__name__}）。"}
        if not isinstance(tool_result, dict):
            tool_result = {"tool": tool_name, "note": str(tool_result)[:500]}
        if tool_result.get("status") == "bad_args":
            tool_result = {"tool": tool_name, "status": "bad_args", "note": "工具参数不合法。"}
        # 工具「没接入/没配置」时不能把它的提示当最终答复发给用户：
        # 模型本来可以基于已有资料回答，拿 note 顶替等于对用户说「我做不了」。
        unavailable = (tool_result or {}).get("connected") is False
        note = (tool_result or {}).get("note") or ""
        if unavailable and (parsed.answer or "").strip():
            pass  # 保留模型自己给出的答案
        else:
            parsed.answer = note or parsed.answer or "工具已执行。"
        # 工具自己说「没接入 / 没查到」时标记升级：对客话术必须与系统状态一致，
        # 不承诺「已转人工」——这里没有客服团队，只落一条待补充的工单。
        # status=empty（没待办/没记录）是正常答案，不算缺口。
        benign = (tool_result or {}).get("status") in ("need_context", "empty")
        tool_missed = not benign and (
            (tool_result or {}).get("connected") is False
            or (tool_result or {}).get("found") is False)
        if tool_name == "search_emote" and not tool_missed:
            # 表情包必须在正文里留下 [表情包:关键词]，网关才会真的挑图发出去。
            # 模型拿到库清单后仍会说「我没法发图片」（实测 u2-flash 稳定复现），
            # 于是用户要表情包，得到一句道歉——这就是「表情包发不出去」的真相。
            kw = str((parsed.tool_args or {}).get("keyword") or "").strip()
            if "[表情包" not in (parsed.answer or ""):
                if _EMOTE_REFUSAL_RE.search(parsed.answer or ""):
                    parsed.answer = ""
                marker = f"[表情包:{kw or '猫咪'}]"
                parsed.answer = ((parsed.answer or "").strip() + " " + marker).strip()

    # 输出侧护栏：LLM 回答若泄露系统提示/Key，替换为安全话术并标记升级
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
    # 输出钩子统一在 run()/run_stream() 出口应用（这里再加一次会让「追加落款」叠两遍）
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
    """run()/run_stream() 公共前置：截断、上下文、护栏/升级/图片规则拦截。拦截时直接返回 AgentReply。"""
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

    # 输入钩子：用户消息进管线前的确定性规则（拦截/改写），优先于一切模型判断
    try:
        from core import hooks as hooks_mod

        hook_res = hooks_mod.run_pre_input(query, cfg)
        if hook_res["action"] == "block":
            ctx = await build_context_async(query, history, cfg, visitor_key, False)
            return AgentReply(text=hook_res["text"], confidence=1.0, mode="hook",
                              ctx_used=ctx["used"], tool_used="hook:pre_input")
        if hook_res["action"] == "rewrite" and hook_res["text"] != query:
            message = ChannelMessage(**{**message.__dict__, "text": hook_res["text"]})
            query = hook_res["text"]
    except Exception as exc:  # 钩子故障不影响对话
        import logging as _logging
        _logging.getLogger("cs.events").warning("hook pre_input failed: %s", type(exc).__name__)

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
    # 先看知识库能否回答：命中本地 KB 就不搜网——"退货政策是什么"这类含疑问词
    # 的业务问题曾被启发式误判，每次白跑 4 次搜索、还拖慢 3~5 秒。
    web = message.web_search
    if not web:
        from core import websearch
        if websearch.available() and _wants_facts(query):
            # 先探一次知识库：命中就不搜网（检索结果随后 build_context 会复用同一份缓存）
            cfg_ret = cfg.get("retrieval", {})
            emb_flag = cfg_ret.get("embedding", "auto")
            kb_hits = retrieval.retrieve_scored(
                query, 1, cfg_ret.get("min_overlap", 0.0),
                use_embedding=None if emb_flag == "auto" else (emb_flag == "on"),
            )
            web = not kb_hits

    ctx = await build_context_async(query, history, cfg, visitor_key, web)
    # 技能说明书：告诉模型「什么时候用哪个工具」——只有工具没有技能时，
    # 模型经常知道有 read_file 却不知道先列目录，等于工具白装。
    try:
        from core import skills as skills_mod

        ctx["skills_block"] = skills_mod.render(cfg)
    except Exception:  # 技能渲染失败绝不能影响主链路
        ctx["skills_block"] = ""
    # 人物/群档案 + 黑话：让模型「先认得人再开口」，不用每轮从零推断。
    try:
        from core import profiles as profiles_mod

        # channel_type 实际取值是 qq_group / qq_private（不是 "qq"），
        # 用 startswith 判断，否则 QQ 用户会被错记成 web:qq:123 两条档案。
        is_qq = str(message.channel_type or "").startswith("qq") or message.channel_type == "onebot"
        user_key = ("qq:" + str(message.channel_user_id)) if is_qq else ("web:" + str(visitor_key))
        group_key = ("qq_group:" + str(message.group_id)) if message.group_id else ""
        # 自动建档：每轮把「谁在说、在哪个群说」增量写进档案（首见创建，之后累加计数）。
        # 只读不写的话，档案永远是空的——这正是「建档不生效」的根因。
        profiles_mod.upsert_profile(
            user_key, "user", display_name=str(message.channel_user_id or "")[:20],
            alias=str(getattr(message, "display_name", "") or "")[:20],
            stats_delta={"messages": 1})
        if group_key:
            profiles_mod.upsert_profile(
                group_key, "group",
                display_name=str(getattr(message, "group_name", "") or "")[:40],
                stats_delta={"messages": 1})
        ctx["profile_block"] = profiles_mod.profile_block(user_key, group_key)
        ctx["lexicon_block"] = profiles_mod.lexicon_block(group_key or user_key)
        profiles_mod.learn_from_message(query, group_key or user_key)
    except Exception:  # 档案失败不能影响对话
        ctx["profile_block"] = ""
        ctx["lexicon_block"] = ""
    if query:
        # 引用回复：把用户这一句原话交给模型，避免"答非所问还答得理直气壮"
        ctx["quote_block"] = "【用户这一句原话】" + query[:300]
        # 输入意图识别：规则先定性，再决定这一轮该用哪个档位说话。
        # 不注入的话，模型遇到「你是谁」会背客服简历、遇到「发个表情包」会说发不了图。
        try:
            from core import intent as intent_mod

            ctx["intent"] = intent_mod.classify(query)
            ctx["intent_block"] = intent_mod.prompt_hint(ctx["intent"])
        except Exception:  # 意图识别只是增强，判不出来不影响回答
            ctx["intent"] = None
            ctx["intent_block"] = ""
    # 工具上下文：让工具知道"谁在问、在哪个群、刚才说了什么、有没有带图"
    ctx["tool_ctx"] = ToolContext(
        visitor_id=visitor_key or "",
        session_id=getattr(message, "session_id", "") or "",
        channel_type=message.channel_type,
        channel_user_id=message.channel_user_id,
        group_id=message.group_id,
        group_name=getattr(message, "group_name", "") or "",
        message_text=query,
        image_b64=message.image_b64 or "",
        history=tuple(history or ()),
        message_id=str(getattr(message, "message_id", "") or ""),
        # 工具层的权限主体：QQ 渠道按触发者 QQ 号，其余按访客身份。
        # 只有管理员/授权 QQ（AUTHORIZED_QQ）能触达读写主机文件、以机器人身份发消息。
        # channel_type 实际取值是 qq_group / qq_private（不是 "qq"）：旧判断用
        # in ("qq","onebot") 恒为假，于是主人账号被当成 visitor，
        # 所有特权工具一律拒绝——「主人账号说话机器人不听」的根因之一。
        principal=(f"qq:{message.channel_user_id}"
                   if str(message.channel_type or "").startswith("qq")
                   or message.channel_type == "onebot"
                   else f"visitor:{visitor_key or 'anonymous'}"),
    )
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

    if "转人工" in query or "人工客服" in query or "找个人" in query:
        if cfg.get("escalation", {}).get("handoff", True):
            return AgentReply(
                text="行，我把人叫过来，你稍等。",
                confidence=1.0, escalate=True, escalate_reason="visitor_requested",
                ctx_used=ctx["used"], mode="rule_react",
            )
        return AgentReply(
            text="这儿就我一个人盯着，没别人可转。有事直接说，能办的我给你办。",
            confidence=1.0, escalate=False,
            ctx_used=ctx["used"], mode="rule_react",
        )

    if message.image_b64 and cfg.get("multimodal", {}).get("enable", True):
        # 视觉能力判定要看「模型真的支持图片」，而不是「配了模型名」。
        # 主模型 u2-flash 明确不支持 image_url（端点 400），之前只判断"有模型名"
        # 就把图丢给它，结果整轮失败掉回规则引擎——用户看到的就是"识别不了"。
        vision_model = (cfg.get("multimodal", {}) or {}).get("vision_model") or ""
        vision_ready = bool(vision_model) and bool(cfg.get("llm", {}).get("has_key", False))
        if not vision_ready:
            # 主模型支持多模态时可沿用主模型
            vision_ready = bool((cfg.get("llm", {}) or {}).get("supports_vision"))                 and bool(cfg.get("llm", {}).get("has_key", False))
        if not vision_ready:
            return AgentReply(
                text="图收到了，但我这会儿看不了图。你用文字说一下图里有啥？",
                confidence=0.3,
                escalate=False,
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


def _apply_post_hooks(reply: AgentReply, cfg: dict) -> AgentReply:
    """输出钩子：回复落库前的确定性改写/追加/替换。

    与输出侧护栏分工：护栏管「不许泄露」，钩子管「运营要求」（加免责声明、
    统一签名、特定话题替换话术）。两者都在模型之外强制执行，不依赖模型配合。
    """
    try:
        from core import hooks as hooks_mod

        res = hooks_mod.run_post_reply(reply.text or "", cfg)
    except Exception:
        return reply
    if res["action"] == "pass":
        return reply
    reply.text = res["text"]
    if res["action"] in ("replace", "block"):
        reply.tool_used = "hook:post_reply"
    return reply


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


async def _gate_output(reply, query: str, cfg: dict) -> None:
    """输出判别器：规则判问题，严重问题重写一次，结果写回 reply 并记账。

    与 `_maybe_critique` 分工：那边只统计「格式类」高频毛病，这边管「人设硬伤」——
    自称 AI、把已有能力说死、承诺转人工，这些出现在群里一次就够丢人。
    """
    from core import quality

    try:
        issues = quality.critique(reply.text or "", query,
                                  channel=str(cfg.get("_channel") or ""),
                                  persona=str((cfg.get("context") or {}).get("persona") or ""))
    except Exception:  # 判别器故障绝不影响回复
        return
    if not issues:
        return
    if quality.needs_rewrite(issues):
        # 决策 JSON 漏进正文属于「可直接确定性修复」的：切掉 JSON 尾巴即可，
        # 正文本身是好的，没必要花一次模型调用重写（重写还可能把好答案改坏）。
        if any(i.get("code") == "json_leak" for i in issues):
            stripped = _strip_json_tail(reply.text or "")
            if stripped:
                reply.text = stripped
                issues = [i for i in issues if i.get("code") != "json_leak"]
        if quality.needs_rewrite(issues):
            new_text = await quality.rewrite(
                reply.text or "", issues, query=query,
                persona=str((cfg.get("context") or {}).get("persona") or ""))
            if new_text:
                reply.text = new_text
            else:
                # 重写没成功（模型也答不出来）：至少不能把底层模型自述/英文过滤报错发出去。
                # 这里给的是"人设兜底"而不是客服话术——宁可用一句短的猫话，也不露馅。
                reply.text = _fallback_for(issues)
                reply.mode = "llm_react_guarded"
    try:
        store.critique_add("quality", len(reply.text or ""),
                           [f"[{i['severity']}] {i['detail']}" for i in issues])
    except Exception:
        pass


def _strip_json_tail(text: str) -> str:
    """切掉混在正文后面的决策 JSON。

    实测 u2-flash 会先正常回一句，再原样贴一段
    `{"thought": ..., "tool": null, "answer": "..."}`。整段发出去等于把内部字段
    给用户看；正文是好答案，只需要砍掉从第一个 `{` 开始的部分。
    """
    t = (text or "").strip()
    idx = t.find("{")
    if idx <= 0:
        return ""
    head = t[:idx].rstrip()
    if not head:
        return ""
    # JSON 里带 answer 时，用 answer 的值兜底（正文可能被截断/为空）
    import json as _json

    try:
        tail = _json.loads(t[idx:])
        if isinstance(tail, dict):
            inner = str(tail.get("answer") or "").strip()
            if inner and len(inner) > len(head):
                return inner
    except ValueError:
        pass
    return head


def _fallback_for(issues: list[dict]) -> str:
    codes = {i.get("code") for i in issues}
    if "identity_no_cat" in codes:
        return "本喵叫零，一只猫，喵～ 问这么多干嘛，先说说你是谁。"
    if "filter_leak" in codes:
        return "这话我接不住，换个说法，喵～"
    if "ai_self" in codes or "model_leak" in codes:
        return "我是零，一只猫。别的身份？你猜，猜对了也不告诉你，喵～"
    if "capability_refusal" in codes:
        return "能发，你要什么直接说，喵～"
    return "这话我说不利索，换个问法，喵～"


async def run(message: ChannelMessage, history: list[dict], visitor_key: str) -> AgentReply:
    with tracing.span("zhike.pipeline.run", {"channel": message.channel_type, "visitor": visitor_key}):
        prepared = await _prepare(message, history, visitor_key)
        if isinstance(prepared, AgentReply):
            return prepared
        cfg, query, ctx = prepared
        reply = await _run_body(message, history, visitor_key, cfg, query, ctx)
        # 输出判别器：人设硬伤（自称 AI/能力否认/假转接）在这里拦一次。
        # 只对 LLM 产物生效——规则兜底与标注答案是人写的，不该被改写。
        if str(reply.mode or "").startswith("llm"):
            cfg = {**cfg, "_channel": message.channel_type}
            await _gate_output(reply, query, cfg)
        # 输出钩子统一在出口应用：所有分支（标注/直连工具/README/LLM/规则）一视同仁，
        # 分支内部各自加一遍必然会漏。
        return _apply_post_hooks(reply, cfg)


async def _run_body(message: ChannelMessage, history: list[dict], visitor_key: str,
                cfg: dict, query: str, ctx: dict) -> AgentReply:

    # 标注回复（Dify annotations 对标）：命中管理员标注的标准答案，直接回复，
    # 不走 KB/LLM——最便宜、最可控、零延迟。放在所有引擎分支之前。
    ann = store.annotation_hit(query)
    if ann:
        return AgentReply(
            text=ann["answer"], mode="annotation", confidence=1.0,
            citations=[f"标注#{ann['id']}"],
        )

    # 工具意图直通：提醒/记忆/待办这类语义极其明确的请求，直接执行工具，
    # 不赌模型会不会选工具。实测 LLM 在 QQ 群里经常推脱「我没这功能」——
    # 明明注册了工具，模型却宁可道歉也不调用，用户体验是致命的。
    emote = await _direct_emote(query, ctx, cfg)
    if emote is not None:
        return emote
    direct = await _direct_tool(query, ctx, cfg)
    if direct is not None:
        return direct

    # 链接直读：消息里给了明确 URL 且要求看内容时，直接抓取再交给模型总结。
    # 不能指望模型每轮都主动选 read_webpage——实测它会先吐槽"占位符网站"
    # 而不是真去读（模型没有"必须调用工具"的硬约束）。
    web_reply = await _prefetch_url(query, ctx, cfg)
    if web_reply is not None:
        return web_reply
    file_reply = await _prefetch_file(query, ctx, cfg)
    if file_reply is not None:
        return file_reply

    # 没接通模型时不要用规则话术硬撑着回 QQ：那等于对外宣称"我是机器人但我不懂"。
    # mode="no_model" 让网关直接静默（Web 端仍保留兜底话术，不影响网页访客体验）。
    if str(message.channel_type or "").startswith("qq") and not cfg.get("llm", {}).get("has_key"):
        return AgentReply(text="", confidence=0.0, mode="no_model", ctx_used=ctx["used"])
    mode = cfg.get("engine", {}).get("mode", "auto")
    llm_reply = None
    if mode in ("llm", "auto") and cfg.get("llm", {}).get("has_key"):
        with tracing.span("zhike.llm", {"model": cfg["llm"].get("model", "")}):
            try:
                llm_reply = await _llm_answer_best(query, ctx, cfg,
                                                   image_b64=message.image_b64,
                                                   channel=message.channel_type)
            except Exception:
                llm_reply = None
        if llm_reply is None and mode == "llm":
            rule = await _rule_react(query, ctx, cfg)
            rule.mode = "rule_fallback"
            return rule
    if llm_reply:
        _maybe_critique(llm_reply, ctx, visitor_key)
        return llm_reply
    return await _rule_react(query, ctx, cfg)


async def _llm_answer_best(query: str, ctx: dict, cfg: dict, *, image_b64: str = "",
                           channel: str = "") -> AgentReply | None:
    """多模型择优（集群）：并行取两版，按规则打分挑更好的一版。

    只在闲聊类意图开启（quality.ensemble>1 且意图在清单内），工具/知识类不走——
    那些场景多跑一遍模型既不提升正确率，又白烧算力、拖慢响应。
    任何一路失败都退回单路结果，不因为择优把整轮搞挂。
    """
    from core import ensemble, intent as intent_mod

    qcfg = cfg.get("quality") or {}
    n = int(qcfg.get("ensemble") or 1)
    it = ctx.get("intent") or intent_mod.classify(query)
    allowed = qcfg.get("ensemble_intents") or []
    if n <= 1 or image_b64 or it.name not in allowed:
        return await _llm_answer(query, ctx, cfg, image_b64=image_b64)

    chain = llm_mod_chain_models(cfg)
    if len(chain) < 2:
        return await _llm_answer(query, ctx, cfg, image_b64=image_b64)

    picks = chain[:max(2, min(n, len(chain)))]
    tasks = [asyncio.create_task(_llm_answer(query, ctx, cfg, image_b64=image_b64, prefer=m))
             for m in picks]
    got: list[dict] = []
    for t in asyncio.as_completed(tasks):
        try:
            r = await t
        except Exception:
            r = None
        if r is not None and (r.text or "").strip():
            got.append({"answer": r.text, "reply": r, "model": ""})
    for t in tasks:
        if not t.done():
            t.cancel()
    if not got:
        return None
    if len(got) == 1:
        return got[0]["reply"]
    best, scores = ensemble.choose(got, query, channel)
    if best < 0:
        return got[0]["reply"]
    reply = got[best]["reply"]
    try:
        store.critique_add("ensemble", len(reply.text or ""),
                           [f"择优：{len(got)} 版，分 {scores}"])
    except Exception:
        pass
    return reply


def llm_mod_chain_models(cfg: dict) -> list[str]:
    """备援链里的模型名（去重、保持顺序），供择优挑选。"""
    llm_cfg = cfg.get("llm") or {}
    out: list[str] = []
    for c in llm._chain(llm_cfg, llm_cfg.get("model") or ""):
        if c["model"] not in out:
            out.append(c["model"])
    return out


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
        # 流式只跑了一轮，必须补 OBSERVE：
        # - 模型要工具 → 执行并把结果回填
        # - 模型只说「我这就去查」→ 逼一轮真实答案
        # 否则用户看到的是空回复或一句空话（实测 u2-flash 两种都复现）。
        if data is not None:
            wants_tool = bool(str(data.get("tool") or "").strip())
            promise_only = llm.is_promise_only(str(data.get("answer") or ""))
            if wants_tool or promise_only:
                final = await _llm_answer(query, ctx, cfg,
                                          image_b64=message.image_b64, first=data)
            else:
                final = await _make_llm_reply(data, ctx, cfg)
        elif not deltas_sent:
            # 流式未产出任何内容：按非流式再试一次（内部含多模型备援），仍失败才回规则
            try:
                final = await _llm_answer(query, ctx, cfg, image_b64=message.image_b64)
            except Exception:  # aqg: top-level boundary
                final = None
    if final is None:
        final = await _rule_react(query, ctx, cfg)
        if mode == "llm" and cfg.get("llm", {}).get("has_key"):
            final.mode = "rule_fallback"
    if str(final.mode or "").startswith("llm"):
        await _gate_output(final, query, {**cfg, "_channel": message.channel_type})
    # 输出钩子：与非流式入口同一套规则（final.text 是权威内容，前端会以它覆盖打字机片段）
    final = _apply_post_hooks(final, cfg)
    # 已向客户端发过 delta 时不再补发 delta：
    # 否则前端会把「半截 LLM 文本 + 规则话术」拼在一起显示（已复现）。
    # final 仍完整返回，前端以它为准覆盖。
    if not deltas_sent:
        yield ("delta", final.text)
    if final is not None and str(final.mode or "").startswith("llm"):
        _maybe_critique(final, ctx, visitor_key)
    yield ("final", final)
