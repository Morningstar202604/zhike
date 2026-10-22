"""把 LLM 端点接到 ReAct 循环：一次调用 = 一轮决策。

输出格式与 agent_loop.build_system_prompt 约定一致（JSON 对象）。
兼容性：优先用 response_format=json_object，4xx 时自动去掉重试（与 llm.react 同策略）。
"""
from __future__ import annotations

import json
import logging


from core.http_pool import get_client

from core import config as config_mod

lg = logging.getLogger("cs.events")


def _creds() -> tuple[str, str, str]:
    cfg = config_mod.get_config()["llm"]
    return (
        cfg.get("api_key") or "",
        (cfg.get("base_url") or "").rstrip("/"),
        cfg.get("model") or "",
    )


async def call_llm(messages: list[dict], tools: list[dict], timeout_s: float = 60.0) -> dict:
    """调用一次 chat/completions，返回 {"thought","tool","args","final","usage"}。

    任何失败抛异常，由 agent_loop 转成 error 状态（保留已完成步骤）。
    """
    api_key, base, model = _creds()
    if not api_key or not base:
        raise RuntimeError("LLM 未配置（缺少 api_key 或 base_url）")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }

    client = get_client(base, timeout=timeout_s)
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
    data = resp.json()

    content = (data["choices"][0]["message"]["content"] or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        decision = json.loads(content)
    except json.JSONDecodeError:
        # 非 JSON：当成"模型直接给了最终答复"，不让整轮任务失败
        lg.info("agent loop: non-JSON model output treated as final answer")
        decision = {"thought": "", "tool": None, "final": content}

    if not isinstance(decision, dict):
        decision = {"thought": "", "tool": None, "final": str(decision)}

    usage = data.get("usage") or {}
    decision["usage"] = {
        "total_tokens": usage.get("total_tokens") or 0,
        # 成本按调用方配置折算；未配置时记 0，预算里 max_cost 就不生效
        "cost": float(usage.get("total_tokens") or 0) * float(config_mod.get_config()
                                                             .get("llm", {})
                                                             .get("cost_per_token", 0) or 0),
    }
    return decision
