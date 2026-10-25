"""LLM 调用（OpenAI 兼容 /chat/completions）：异步、不阻塞事件循环；任何失败返回 None 交给规则降级。

多提供商备援链（failover）：
- 候选顺序 = 主配置 → fallback_model → providers 档案（同站无 Key 沿用主 Key，异站必须自带 Key）；
- 竞速并行：主候选先跑，USER_LLM_HEDGE_SECONDS（默认 10s）内没回就并行拉起下一个候选，
  任何一个先成功立即采用并取消其余——某个模型/网络挂了不用干等，备胎随行待命；
- 冷却熔断：失败的候选进 USER_LLM_COOLDOWN_SECONDS（默认 120s）冷却池，后续请求直接跳过。
"""
import asyncio
import json
import os
import re
import time

import logging

import httpx

from core.http_pool import get_client

from core import config as config_mod

lg = logging.getLogger("cs.events")


def _record_llm(result: str) -> None:
    try:
        from core import metrics

        metrics.record_llm(result)
    except Exception:  # 指标绝不能影响主链路
        pass


def _creds() -> tuple[str, str, str, str]:
    cfg = config_mod.get_config()["llm"]
    key = cfg.get("api_key") or ""
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    source = cfg.get("key_source") or "none"
    return key, base, model, source


# ---- 多提供商备援链 -----------------------------------------------------------

_HEDGE_S = max(0.0, float(os.environ.get("USER_LLM_HEDGE_SECONDS", "10") or 0))
_COOLDOWN_S = max(0.0, float(os.environ.get("USER_LLM_COOLDOWN_SECONDS", "120") or 0))
# 429 短冷却。.env.example 一直在配 USER_LLM_RATE_LIMIT_COOLDOWN_SECONDS，
# 但代码从未定义它：429 分支必抛 NameError 打断整条备援链，配置等于失效。
_RATE_LIMIT_COOLDOWN_S = max(
    0.0, float(os.environ.get("USER_LLM_RATE_LIMIT_COOLDOWN_SECONDS", "5") or 0))
_COOLDOWN: dict[tuple[str, str], float] = {}   # (base_url, model) -> wake_at(monotonic)


class RateLimited(Exception):
    """429：不是模型坏了，是这一秒的配额用完了。不能按普通失败冷却 120s。"""

    def __init__(self, retry_after: float = 0.0):
        super().__init__("rate limited")
        self.retry_after = retry_after


def _cooling(base: str, model: str) -> bool:
    wake = _COOLDOWN.get((base, model))
    return wake is not None and time.monotonic() < wake


def _chain(llm_cfg: dict, model: str, vision: bool = False) -> list[dict]:
    """构建候选链。vision 调用只在主配置上（视觉模型通常绑定特定端点与计费）。"""
    key = llm_cfg.get("api_key") or ""
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    out: list[dict] = []
    if base and key:
        out.append({"base_url": base, "model": model, "api_key": key})
    if vision:
        # 视觉模型通常绑定特定端点与计费，不参与换模型备援
        return out
    fb = (llm_cfg.get("fallback_model") or "").strip()
    if fb and fb != model:
        out.append({"base_url": base, "model": fb, "api_key": key})
    seen = {(c["base_url"], c["model"]) for c in out}
    for p in llm_cfg.get("providers") or []:
        if not isinstance(p, dict):
            continue
        b = (p.get("base_url") or "").rstrip("/")
        m = (p.get("model") or "").strip()
        if not b or not m or (b, m) in seen:
            continue
        # 同站（同 base_url）没填 Key 视为沿用主 Key（同一账号下的多个模型）；
        # 异站没填 Key 无法认证，跳过——省得每次请求都去撞一次 401。
        k = (p.get("api_key") or "").strip() or (key if b == base else "")
        if not k:
            continue
        seen.add((b, m))
        out.append({"base_url": b, "model": m, "api_key": k})
    return out


async def _race_chain(chain: list[dict], messages: list, base_payload: dict,
                      parse=None) -> dict | None:
    """备援链竞速。规则：
    - 冷却中的候选直接跳过；
    - 始终保持至多 2 个候选并行（主 + 1 备），主候选 HEDGE 秒没回就拉起备胎；
    - 任何候选先成功 → 取消其余并返回；失败者进冷却池，继续等在跑的/补新的；
    - parse 可调用时，内容级失败（空答案/解析不出）同样视作候选失败，换下一个。
    """
    cands = [c for c in chain if not _cooling(c["base_url"], c["model"])]
    if not cands:
        lg.warning("llm chain: 全部候选都在冷却中")
        return None
    max_parallel = 2 if _HEDGE_S > 0 else 1
    pending: dict[asyncio.Task, dict] = {}
    result: dict | None = None
    try:
        while (cands or pending) and result is None:
            while cands and len(pending) < max_parallel:
                cand = cands.pop(0)
                t = asyncio.ensure_future(_post_chat(
                    cand["base_url"], cand["api_key"],
                    {**base_payload, "model": cand["model"], "messages": messages}))
                pending[t] = cand
            if not pending:
                break
            # 单候选在跑且还有后备时用 HEDGE 超时触发加注；否则等到有结果
            wait_kwargs = ({"timeout": _HEDGE_S}
                           if len(pending) == 1 and cands and _HEDGE_S > 0 else {})
            done, _ = await asyncio.wait(set(pending), return_when=asyncio.FIRST_COMPLETED,
                                         **wait_kwargs)
            for d in done:
                cand = pending.pop(d)
                err = d.exception()
                text = None if err is not None else d.result()
                parsed = None
                if text is not None:
                    try:
                        parsed = parse(text) if parse else ({"_raw": text} if text.strip() else None)
                    except Exception:  # 解析器本身炸了也算候选失败
                        parsed = None
                if parsed is not None:
                    result = parsed
                    _COOLDOWN.pop((cand["base_url"], cand["model"]), None)
                    lg.info("llm via %s@%s", cand["model"], cand["base_url"].split("//")[-1])
                elif isinstance(err, RateLimited):
                    # 配额瞬时打满：短冷却（默认 5s，或服务端 retry-after），不按 120s 处理
                    wait = min(max(err.retry_after or 0.0, _RATE_LIMIT_COOLDOWN_S), 60.0)
                    _COOLDOWN[(cand["base_url"], cand["model"])] = time.monotonic() + wait
                    lg.warning("llm rate limited, short cooldown %.0fs: %s@%s",
                               wait, cand["model"], cand["base_url"].split("//")[-1])
                else:
                    _COOLDOWN[(cand["base_url"], cand["model"])] = \
                        time.monotonic() + _COOLDOWN_S
                    lg.warning("llm candidate failed (%s), cooldown %ss: %s@%s",
                               type(err).__name__ if err else ("empty" if text is not None else "bad-content"),
                               _COOLDOWN_S, cand["model"], cand["base_url"].split("//")[-1])
    finally:
        for t in pending:
            if not t.done():
                t.cancel()
    return result


def _data_url(b64: str) -> str:
    """裸 base64 → data URL：按魔数识别常见图片 MIME（OpenAI 多模态内容格式的通用做法）。"""
    head = (b64 or "")[:16].lstrip()
    if head.startswith("/9j/"):
        mime = "image/jpeg"
    elif head.startswith("iVBORw0KGgo"):
        mime = "image/png"
    elif head.startswith("R0lGOD"):
        mime = "image/gif"
    elif head.startswith("UklGR"):
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{b64}"


def _build_messages(query: str, ctx: dict, cfg: dict, image_b64: str = "") -> tuple[list[dict], str]:
    """组装 system+history+user 消息，返回 (messages, 所用模型名)。

    图片 + multimodal.vision_model 时按 OpenAI 多模态 content 数组（业界统一范式）改用视觉模型。
    """
    llm_cfg = cfg.get("llm", {})
    react_steps = cfg.get("escalation", {}).get("react_steps", 3)
    budget = cfg.get("context", {}).get("char_budget", 2400)
    modules_on = ctx["modules_on"]
    system = ctx["persona"] + "\n"
    parts = []
    if modules_on.get("kb", True):
        parts.append("知识库条目：\n" + ctx["kb_block"])
    if modules_on.get("tools", True):
        parts.append("可用工具：\n" + json.dumps(ctx["tools_schema"], ensure_ascii=False))
    if modules_on.get("memory", True):
        parts.append("访客相关记忆：\n" + ctx["memory_block"])
    if ctx.get("read_block"):
        # 已经替你打开/读到的原文。中性表述，避免模型说"系统日志/搜索结果"这类机制腔。
        parts.append(str(ctx["read_block"]))
    if ctx.get("web_block"):
        parts.append("联网搜索结果（仅供参考，引用时说明来源）：\n" + ctx["web_block"])
    if ctx.get("ambient_block"):
        parts.append(str(ctx["ambient_block"]))
    if ctx.get("group_name"):
        parts.append(f"当前场景：QQ 群「{ctx['group_name']}」。")
    if ctx.get("self_fix"):
        parts.append(str(ctx["self_fix"]))
    if ctx["summary"]:
        parts.append(ctx["summary"])
    # 固定尾巴（ReAct 指令）必须先占预算，否则内容块会把总长顶出去
    tail = (
        f"ReAct 最大步数 {react_steps}。请只输出 JSON：\n"
        '{"thought": str, "tool": name或null, "tool_args": {..}或null, '
        '"answer": str, "citations": [id], "confidence": 0-1, '
        '"escalate": bool, "escalate_reason": str}\n'
        "注意：你没有人类同事，也没有任何人工客服/坐席可转接。"
        "escalate 只表示「这条需要管理员后续补充资料」，绝不能对用户说转人工、转接真人或叫人来处理。"
    )
    # 可用于内容块的额度 = 总预算 - persona - 固定尾巴
    content_budget = max(120, budget - len(ctx["persona"]) - len(tail) - 4)

    body = ""
    n = max(len(parts), 1)
    overhead = len("…（已截断）")
    # 预算不足时宁可每个模块只放一小段，也不能整块丢掉：
    # 丢掉工具清单会让模型以为"没有工具可用"，丢掉记忆会让它重复问已知信息。
    per_part = max(60, (content_budget - 2 * n) // n - overhead)
    for idx, part in enumerate(parts):
        remaining = content_budget - len(body) - 2
        if remaining <= 30:
            # 预算耗尽：塞一个极短占位，让模型知道该模块存在
            head = part.split("：", 1)[0][:12]
            stub = f"{head}：…（预算不足）"
            body += stub + "\n"
            continue
        quota = remaining if idx == len(parts) - 1 else min(per_part, remaining)
        chunk = part if len(part) <= quota else part[:quota] + "…（已截断）"
        body += chunk + "\n\n"
    system += body
    system += tail

    messages = [{"role": "system", "content": system}]
    for m in ctx["history"]:
        role = "assistant" if m.get("role") in ("agent", "operator") else "user"
        messages.append({"role": role, "content": m.get("content")})

    # 未显式配置视觉模型时沿用主模型：agnes/deepseek 之外多数 OpenAI 兼容端点
    # 的旗舰模型本身支持图片输入，留空就没图看属于白丢能力。
    vision_model = ((cfg.get("multimodal", {}) or {}).get("vision_model", "") or "").strip()         or (llm_cfg.get("model") or "").strip()
    if image_b64 and vision_model:
        img = image_b64
        if img.startswith("data:"):
            img = img.split(",", 1)[-1]
        if len(img) <= 8_000_000:  # ~6MB 载荷上限，超大图拒走视觉
            messages.append({"role": "user", "content": [
                {"type": "text", "text": query or "请看图回答用户的问题。"},
                {"type": "image_url", "image_url": {"url": _data_url(img)}},
            ]})
            return messages, vision_model
    messages.append({"role": "user", "content": query or "请看图回答用户的问题。"})
    return messages, llm_cfg.get("model", "deepseek-chat")


async def _post_chat(base: str, api_key: str, payload: dict) -> str | None:
    """单次 chat/completions 调用（含 4xx 时去掉 response_format 的重试）。

    429 单独抛 RateLimited：调用方要短退避重试，而不是把整条候选链冷却 120 秒——
    一次并发高峰就让机器人彻底沉默 2 分钟，是最难解释给用户的故障。
    """
    client = get_client(base, timeout=60)
    resp = await client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )
    if resp.status_code == 429:
        try:
            retry_after = float(resp.headers.get("retry-after") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        raise RateLimited(retry_after)
    if resp.status_code in (400, 422):
        retry = dict(payload)
        retry.pop("response_format", None)
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=retry,
        )
        if resp.status_code == 429:
            raise RateLimited(1.0)
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()


async def react(query: str, ctx: dict, cfg: dict, image_b64: str = "") -> dict | None:
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("has_key"):
        return None
    api_key = llm_cfg.get("api_key") or ""
    if not api_key:
        return None
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    if not base:
        return None

    messages, model = _build_messages(query, ctx, cfg, image_b64)
    vision_model = (cfg.get("multimodal", {}) or {}).get("vision_model", "") or ""
    payload = {
        "model": model,
        "temperature": llm_cfg.get("temperature", 0.2),
        "top_p": llm_cfg.get("top_p", 1.0),
        "max_tokens": llm_cfg.get("max_tokens", 512),
        "response_format": {"type": "json_object"},
    }
    # 备援链竞速：主配置 → fallback_model → providers；视觉调用只走主配置。
    # 解析并入竞速：空答案/截断 JSON 也算候选失败，自动换下一个模型。
    parsed = await _race_chain(_chain(llm_cfg, model, vision=bool(image_b64 and vision_model)),
                               messages, payload, parse=_parse_llm_answer)
    _record_llm("ok" if parsed is not None else "fail")
    return parsed


class _AnswerStreamer:
    """字符级状态机：从 LLM 增量输出中抽取 answer 字段值；非 JSON（prose）输出自动直通。"""

    def __init__(self):
        self.mode = "detect"   # detect | json | prose
        self.buf = ""          # json 模式：等待 '"answer":"' 出现
        self.in_str = False    # 已进入 answer 字符串
        self.esc = ""          # 转义等待态（"" | "\\" | "u"）
        self.hexbuf = ""
        self.done = False

    def feed(self, piece: str) -> list[str]:
        out: list[str] = []
        for ch in piece:
            if self.mode == "detect":
                if ch.isspace():
                    continue
                self.mode = "prose" if ch != "{" else "json"
                if self.mode == "prose":
                    out.append(ch)
                continue
            if self.mode == "prose":
                out.append(ch)
                continue
            if self.done:
                continue
            if not self.in_str:
                self.buf += ch
                if len(self.buf) > 512:
                    self.buf = self.buf[-64:]
                m = re.search(r'"answer"\s*:\s*"', self.buf)
                if m:
                    self.in_str = True
                    self.buf = ""
                continue
            if self.esc == "\\":
                self.esc = ""
                mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
                if ch in mapping:
                    out.append(mapping[ch])
                elif ch == "u":
                    self.esc = "u"
                    self.hexbuf = ""
                else:
                    out.append(ch)
                continue
            if self.esc == "u":
                self.hexbuf += ch
                if len(self.hexbuf) == 4:
                    try:
                        out.append(chr(int(self.hexbuf, 16)))
                    except ValueError:
                        pass
                    self.esc = ""
                continue
            if ch == "\\":
                self.esc = "\\"
                continue
            if ch == '"':
                self.done = True
                continue
            out.append(ch)
        return out


async def react_stream(query: str, ctx: dict, cfg: dict, image_b64: str = ""):
    """流式 ReAct（OpenAI SSE 流式范式）。产出事件字典：
    {"type": "delta", "text": str}   answer 字段增量片段（或 prose 原文），仅供前端打字机呈现
    {"type": "final", "data": dict}  完整结构化结果（同 react() 返回值），权威终态
    {"type": "error", "error": str}  请求失败，调用方应走规则降级
    """
    llm_cfg = cfg.get("llm", {})
    api_key = llm_cfg.get("api_key") or ""
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    if not llm_cfg.get("has_key") or not api_key or not base:
        yield {"type": "error", "error": "llm not configured"}
        return
    messages, model = _build_messages(query, ctx, cfg, image_b64)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": llm_cfg.get("temperature", 0.2),
        "top_p": llm_cfg.get("top_p", 1.0),
        "max_tokens": llm_cfg.get("max_tokens", 512),
        "stream": True,
    }
    scanner = _AnswerStreamer()
    buf = ""
    try:
        client = get_client(base, timeout=90)
        async with client.stream(
            "POST", f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"}, json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = json.loads(chunk)["choices"][0].get("delta", {}) or {}
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                piece = delta.get("content")
                if piece:
                    buf += piece
                    for t in scanner.feed(piece):
                        if t:
                            yield {"type": "delta", "text": t}
    except (httpx.HTTPError, ValueError) as e:
        lg.warning("llm stream failed: %s %s", type(e).__name__, str(e)[:120])
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
        return
    data = _parse_llm_answer(buf)
    if data is None:
        yield {"type": "error", "error": "empty llm answer"}
        return
    yield {"type": "final", "data": data}


def _parse_llm_answer(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw).rstrip("`").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 半截 JSON（max_tokens 截断/模型跑偏）：先抢救 answer 字段，救不回来才当 prose。
        # 绝不能把 "{...半截 JSON 原文" 发给用户。
        if raw.startswith("{"):
            m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            if m and m.group(1).strip():
                try:
                    answer = json.loads('"' + m.group(1) + '"')
                except json.JSONDecodeError:
                    answer = m.group(1)
                lg.info("llm truncated-json answer salvaged: %s", str(answer)[:120])
                return {"answer": answer, "confidence": 0.6, "citations": [], "escalate": False}
            return None  # 连 answer 都没有的残 JSON：宁走降级也不给用户看乱码
        if raw:
            lg.info("llm prose answer accepted: %s", raw[:120])
            return {"answer": raw, "confidence": 0.62, "citations": [], "escalate": False}
        return None
    if not isinstance(data, dict):
        return None
    # 空答案当失败：交给规则降级，而不是把沉默发给用户
    if not str(data.get("answer") or "").strip():
        return None
    return data


async def probe() -> dict:
    """LLM 连通性自检：小请求实测端点，返回 configured/ok/error/latency。"""
    import time as _time

    api_key, base, model, source = _creds()
    if not api_key:
        return {"configured": False, "ok": False, "error": "未配置 API Key（设置页或 USER_LLM_API_KEY）", "source": source}
    if not base:
        return {"configured": True, "ok": False, "error": "未配置 Base URL", "source": source}
    start = _time.monotonic()
    try:
        client = get_client(base, timeout=15)
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4},
        )
        resp.raise_for_status()
        reply = (resp.json()["choices"][0]["message"]["content"] or "")[:60]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
        return {"configured": True, "ok": False, "error": f"{type(e).__name__}: {e}",
                "base_url": base, "model": model, "source": source}
    return {"configured": True, "ok": True, "error": None, "base_url": base, "model": model,
            "latency_ms": int((_time.monotonic() - start) * 1000), "reply": reply, "source": source}


async def list_models() -> dict:
    """拉取账号可用模型列表：GET {base}/models。"""
    api_key, base, model, source = _creds()
    if not api_key:
        return {"configured": False, "ok": False, "models": [],
                "error": "未配置 API Key（设置页或 USER_LLM_API_KEY）", "source": source}
    if not base:
        return {"configured": True, "ok": False, "models": [], "error": "未配置 Base URL", "source": source}
    try:
        client = get_client(base, timeout=20)
        resp = await client.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        models = sorted(m.get("id", "") for m in data if isinstance(m, dict) and m.get("id"))
    except (httpx.HTTPError, ValueError) as e:
        return {"configured": True, "ok": False, "models": [],
                "error": f"{type(e).__name__}: {e}", "base_url": base, "source": source}
    return {"configured": True, "ok": True, "models": models, "count": len(models),
            "base_url": base, "model": model, "source": source}
