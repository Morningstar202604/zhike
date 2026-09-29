"""运行时配置中心：默认值来自代码与环境变量，可被 SQLite config 表覆盖，改完即时生效。"""
import json
import os

from core import store

def _defaults() -> dict:
    return {
        "llm": {
            "base_url": os.environ.get("USER_LLM_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.environ.get("USER_LLM_MODEL", "deepseek-chat"),
            "temperature": 0.2,
            "top_p": 1.0,
            "max_tokens": 512,
            "api_key": None,
            "has_key": False,
        },
        "retrieval": {
            "kb_top_k": int(os.environ.get("USER_KB_TOP_K", "4")),
            "min_overlap": 0.08,
        },
        "context": {
        "history_turns": 10,
        "char_budget": 2400,
        "compress_threshold": 6,
        "summarize": True,
        "relevant_only": True,
        "modules": {
            "kb": True,
            "tools": True,
            "memory": True,
            "persona": True,
            "summary": True,
        },
        "persona": "你是知客，专业的在线客服 Agent，语气友好、简洁、可靠，使用中文回答。",
    },
    "memory": {
        "enable": True,
        "retrieval_top_k": 3,
        "auto_capture": True,
        "capture_keywords": ["喜欢", "常用", "偏好", "每次", "习惯", "总是"],
        "visit_greeting": True,
    },
    "tools": {
        "query_order": True,
        "initiate_refund": True,
        "lookup_policy": True,
    },
    "multimodal": {
        "enable": True,
    },
    "guardrail": {
        "injection_block": True,
    },
    "engine": {
        "mode": "auto",
    },
    "escalation": {
        "confidence_threshold": float(os.environ.get("USER_ESCALATE_THRESHOLD", "0.6")),
        "max_message_len": int(os.environ.get("USER_MAX_MESSAGE_LEN", "1000")),
        "react_steps": int(os.environ.get("USER_REACT_STEPS", "3")),
    },
}


DEFAULTS = _defaults()


def _deep_merge(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_config() -> dict:
    cfg = _defaults()
    stored = store.get_config() or {}
    cfg = _deep_merge(cfg, stored)
    stored_key = cfg["llm"].get("api_key") or ""
    env_key = os.environ.get("USER_LLM_API_KEY", "")
    if stored_key and stored_key != "your-api-key-here":
        cfg["llm"]["key_source"] = "ui"
    else:
        cfg["llm"]["api_key"] = env_key
        cfg["llm"]["key_source"] = "env" if env_key else "none"
    cfg["llm"]["has_key"] = bool(cfg["llm"]["api_key"]) and cfg["llm"]["api_key"] != "your-api-key-here"
    return cfg


def update_config(patch: dict) -> dict:
    patch = json.loads(json.dumps(patch or {}))
    llm_patch = patch.get("llm") or {}
    if "api_key" in llm_patch and isinstance(llm_patch["api_key"], str) and llm_patch["api_key"].startswith("••"):
        llm_patch["api_key"] = get_config()["llm"].get("api_key") or ""
    store.set_config(patch)
    return get_config()
