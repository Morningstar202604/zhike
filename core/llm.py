"""LLM 调用（OpenAI 兼容 /chat/completions）：异步、不阻塞事件循环；任何失败返回 None 交给规则降级。"""
import json
import os
import re

import logging

import httpx

from core.http_pool import get_client

from core import config as config_mod

lg = logging.getLogger("cs.events")


def _creds() -> tuple[str, str, str, str]:
    cfg = config_mod.get_config()["llm"]
    key = cfg.get("api_key") or ""
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    source = cfg.get("key_source") or "none"
    return key, base, model, source


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
    if ctx["summary"]:
        parts.append(ctx["summary"])
    # 固定尾巴（ReAct 指令）必须先占预算，否则内容块会把总长顶出去
    tail = (
        f"ReAct 最大步数 {react_steps}。请只输出 JSON：\n"
        '{"thought": str, "tool": name或null, "tool_args": {..}或null, '
        '"answer": str, "citations": [id], "confidence": 0-1, '
        '"escalate": bool, "escalate_reason": str}'
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

    vision_model = (cfg.get("multimodal", {}) or {}).get("vision_model", "") or ""
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
    """单次 chat/completions 调用（含 4xx 时去掉 response_format 的重试）。失败返回 None。"""
    client = get_client(base, timeout=60)
    resp = await client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )
    if resp.status_code in (400, 422):
        retry = dict(payload)
        retry.pop("response_format", None)
        resp = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=retry,
        )
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
    payload = {
        "model": model,
        "messages": messages,
        "temperature": llm_cfg.get("temperature", 0.2),
        "top_p": llm_cfg.get("top_p", 1.0),
        "max_tokens": llm_cfg.get("max_tokens", 512),
    }
    payload["response_format"] = {"type": "json_object"}
    raw = None
    try:
        raw = await _post_chat(base, api_key, payload)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
        lg.warning("llm request failed: %s %s", type(e).__name__, str(e)[:120])
    # 多模型备援：主模型失败且配了 fallback_model 时换模型重试一次
    fallback = (llm_cfg.get("fallback_model", "") or "").strip()
    if raw is None and fallback and fallback != payload["model"]:
        lg.info("llm fallback model engaged: %s", fallback)
        try:
            raw = await _post_chat(base, api_key, {**payload, "model": fallback})
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
            lg.warning("llm fallback failed: %s %s", type(e).__name__, str(e)[:120])
    if raw is None:
        return None
    return _parse_llm_answer(raw)


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
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        if raw:
            lg.info("llm prose answer accepted: %s", raw[:120])
            return {"answer": raw, "confidence": 0.62, "citations": [], "escalate": False}
        return None


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
