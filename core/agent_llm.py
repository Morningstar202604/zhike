"""把 LLM 端点接到 ReAct 循环：一次调用 = 一轮决策。

优先走 OpenAI 官方 function calling（`tools` + `tool_calls`），协议端不支持时
自动退回 JSON 对象模式（response_format=json_object，再退纯文本）。

为什么不再只靠 JSON 提示词：官方工具调用是 OpenAI / DeepSeek / Qwen / GLM
都实现了的事实标准，模型被训练过该协议，参数由服务端按 JSON Schema 约束，
比"让模型自己拼 JSON"稳定得多，也省掉一轮解析失败。
"""
from __future__ import annotations

import json
import logging

from core.http_pool import get_client

from core import config as config_mod

lg = logging.getLogger("cs.events")

# 单轮对话历史上限：超过就丢最早的消息（保留 system）。工具结果可能很长，
# 不做窗口控制会让 6 步任务的上下文无限膨胀，最终撞模型上下文上限。
MAX_HISTORY_MESSAGES = 40
MAX_TOOL_RESULT_CHARS = 4000


def _creds() -> tuple[str, str, str]:
    cfg = config_mod.get_config()["llm"]
    return (
        cfg.get("api_key") or "",
        (cfg.get("base_url") or "").rstrip("/"),
        cfg.get("model") or "",
    )


def _trim_messages(messages: list[dict]) -> list[dict]:
    """上下文窗口管理：system 固定，其余只保留最近 N 条。

    官方 harness（OpenAI Agents SDK / LangGraph）都做窗口裁剪或摘要，
    这里用最简单可靠的窗口法；超长工具结果单独截断。
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return [m for m in messages if _fit_size(m)]
    head = messages[:1] if messages and messages[0].get("role") == "system" else []
    tail = messages[-MAX_HISTORY_MESSAGES:]
    return [m for m in head + tail if _fit_size(m)]


def _fit_size(msg: dict) -> bool:
    content = msg.get("content")
    if isinstance(content, str) and len(content) > MAX_TOOL_RESULT_CHARS * 3:
        msg = dict(msg)
        msg["content"] = content[: MAX_TOOL_RESULT_CHARS * 3] + "…(已截断)"
        return True
    return True


def _parse_arguments(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _from_tool_calls(message: dict) -> dict | None:
    """官方 function calling 响应 → 内部决策结构。"""
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    call = calls[0] if isinstance(calls[0], dict) else {}
    fn = call.get("function") or {}
    name = str(fn.get("name") or "").strip()
    if not name:
        return None
    return {
        "thought": str(message.get("content") or ""),
        "tool": name,
        "args": _parse_arguments(fn.get("arguments")),
        "final": "",
        "finish_reason": "tool_calls",
        # 官方协议要求下一轮把 assistant.tool_calls 与 role=tool 的
        # tool_call_id 配对回传，否则部分服务端直接 400。
        "tool_call_id": str(call.get("id") or ""),
        "raw_tool_call": call,
    }


def _parse_content(content: str) -> dict:
    """JSON 对象 / 纯文本 → 内部决策结构（旧协议兼容路径）。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        # 非 JSON：当成"模型直接给了最终答复"，不让整轮任务失败
        lg.info("agent loop: non-JSON model output treated as final answer")
        return {"thought": "", "tool": None, "final": text, "finish_reason": "stop"}
    if not isinstance(decision, dict):
        return {"thought": "", "tool": None, "final": str(decision), "finish_reason": "stop"}
    # 官方并行工具调用里 tool 可能写成 tool_calls 形态，这里只取第一个
    if not decision.get("tool") and decision.get("tool_calls"):
        first = (decision.get("tool_calls") or [{}])[0]
        fn = first.get("function") or first
        decision["tool"] = fn.get("name") or decision.get("tool")
        decision["args"] = _parse_arguments(fn.get("arguments")) or decision.get("args") or {}
    return decision


async def _post(base: str, api_key: str, payload: dict, timeout_s: float):
    client = get_client(base, timeout=timeout_s)
    return await client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
    )


async def call_llm(messages: list[dict], tools: list[dict], timeout_s: float = 60.0) -> dict:
    """调用一次 chat/completions，返回 {"thought","tool","args","final","usage"}。

    任何失败抛异常，由 agent_loop 转成 error 状态（保留已完成步骤）。
    """
    from core.agent_loop import to_openai_tools

    api_key, base, model = _creds()
    if not api_key or not base:
        raise RuntimeError("LLM 未配置（缺少 api_key 或 base_url）")

    messages = _trim_messages(messages)
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 800,
    }
    if tools:
        payload["tools"] = to_openai_tools(tools)
        payload["tool_choice"] = "auto"

    resp = await _post(base, api_key, payload, timeout_s)

    # 协议端不支持 tools：去掉 tools 再试（不少 OpenAI 兼容端点会 400/422）
    if resp.status_code in (400, 422) and tools:
        fallback = dict(payload)
        fallback.pop("tools", None)
        fallback.pop("tool_choice", None)
        fallback["response_format"] = {"type": "json_object"}
        resp = await _post(base, api_key, fallback, timeout_s)

    # 连 response_format 都不支持：最后退纯文本
    if resp.status_code in (400, 422):
        fallback = dict(payload)
        for key in ("tools", "tool_choice", "response_format"):
            fallback.pop(key, None)
        resp = await _post(base, api_key, fallback, timeout_s)

    resp.raise_for_status()
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    decision = _from_tool_calls(message)
    if decision is None:
        decision = _parse_content(str(message.get("content") or ""))
    decision["finish_reason"] = choice.get("finish_reason") or decision.get("finish_reason", "")

    usage = data.get("usage") or {}
    decision["usage"] = {
        "total_tokens": usage.get("total_tokens") or 0,
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        # 成本按调用方配置折算；未配置时记 0，预算里 max_cost 就不生效
        "cost": float(usage.get("total_tokens") or 0) * float(config_mod.get_config()
                                                             .get("llm", {})
                                                             .get("cost_per_token", 0) or 0),
    }
    return decision
