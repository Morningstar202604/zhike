"""LLM 调用（OpenAI 兼容 /chat/completions）：异步、不阻塞事件循环；任何失败返回 None 交给规则降级。"""
import json
import os
import re

import logging

import httpx

from core import config as config_mod

lg = logging.getLogger("cs.events")


def _creds() -> tuple[str, str, str, str]:
    cfg = config_mod.get_config()["llm"]
    key = cfg.get("api_key") or ""
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    source = cfg.get("key_source") or "none"
    return key, base, model, source


async def react(query: str, ctx: dict, cfg: dict) -> dict | None:
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("has_key"):
        return None
    api_key = llm_cfg.get("api_key") or ""
    if not api_key:
        return None
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    if not base:
        return None

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
    body = ""
    for p in parts:
        if len(body) + len(p) + 2 > budget:
            break
        body += p + "\n\n"
    system += body
    system += (
        f"ReAct 最大步数 {react_steps}。请只输出 JSON：\n"
        '{"thought": str, "tool": name或null, "tool_args": {..}或null, '
        '"answer": str, "citations": [id], "confidence": 0-1, '
        '"escalate": bool, "escalate_reason": str}'
    )

    messages = [{"role": "system", "content": system}]
    for m in ctx["history"]:
        role = "assistant" if m.get("role") in ("agent", "operator") else "user"
        messages.append({"role": role, "content": m.get("content")})
    messages.append({"role": "user", "content": query})

    payload = {
        "model": llm_cfg.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": llm_cfg.get("temperature", 0.2),
        "top_p": llm_cfg.get("top_p", 1.0),
        "max_tokens": llm_cfg.get("max_tokens", 512),
    }
    payload["response_format"] = {"type": "json_object"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            if resp.status_code in (400, 422):
                payload.pop("response_format", None)
                resp = await client.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            resp.raise_for_status()
            raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
        lg.warning("llm request failed: %s %s", type(e).__name__, str(e)[:120])
        return None
    return _parse_llm_answer(raw)


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
        async with httpx.AsyncClient(timeout=15) as client:
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
        async with httpx.AsyncClient(timeout=20) as client:
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
