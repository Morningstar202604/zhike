"""运行时配置中心：默认值来自代码与环境变量，可被 SQLite config 表覆盖，改完即时生效。"""
import json
import os
import threading

from core import store
from core import crypto

def _defaults() -> dict:
    return {
        "llm": {
            "base_url": os.environ.get("USER_LLM_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.environ.get("USER_LLM_MODEL", "deepseek-chat"),
            "fallback_model": os.environ.get("USER_LLM_FALLBACK_MODEL", ""),
            "temperature": 0.2,
            "top_p": 1.0,
            "max_tokens": 512,
            "api_key": None,
            "has_key": False,
            # 已保存的提供商档案（AstrBot 多提供商对标）：不含 Key，Key 只跟随当前激活配置
            "providers": [],
        },
        "retrieval": {
            "kb_top_k": int(os.environ.get("USER_KB_TOP_K", "4")),
            "min_overlap": 0.08,
            "embedding": os.environ.get("USER_KB_EMBEDDING", "auto"),
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
        "persona": "你是知客，用户的私人秘书。语气友好、简洁、可靠，使用中文回答。你会记事、设提醒、查资料、读文件、看网页；不确定时如实说明。",
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
        "vision_model": os.environ.get("USER_VISION_MODEL", ""),
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
            # 是否有真人可以接管：私人秘书场景通常没有，团队客服场景才有。
            # 关掉后不再假装「正在转接」，避免承诺一个不存在的真人。
            # 默认关闭：本产品默认是纯机器人（QQ 群/私聊），没有人工客服坐席。
            "handoff": os.environ.get("USER_HANDOFF_ENABLED", "0").lower() in ("1", "true", "yes", "on"),
        },
        # 渠道准入（chatgpt-on-wechat 群白名单对标）：开启后仅白名单群可获得回复
        "channels": {
            "allowlist_enabled": os.environ.get("CHANNEL_GROUP_ALLOWLIST_ENABLED", "").lower() in ("1", "true", "yes"),
            "group_allowlist": [g.strip() for g in os.environ.get("CHANNEL_GROUP_ALLOWLIST", "").split(",") if g.strip()],
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


# 配置缓存：key = (配置版本号, LLM API Key 环境变量指纹)
# 配置在 DB 里变更时版本号递增，缓存自动失效——保证「改完即时生效」。
_CFG_CACHE: dict = {"key": None, "value": None}
_CFG_LOCK = threading.Lock()


def invalidate_cache() -> None:
    """配置变更后主动失效（update_config 内部调用）。"""
    with _CFG_LOCK:
        _CFG_CACHE["key"] = None
        _CFG_CACHE["value"] = None


def config_version() -> int:
    """配置版本号：存在 meta 表，每次 set_config 递增。"""
    return store.config_version()


def get_config() -> dict:
    """读取运行时配置（带缓存）。

    每个对话请求会调用 2-3 次，原来每次都要深拷贝 + 查库 + Fernet 解密。
    缓存键包含版本号与环境变量指纹：DB 改配置会递增版本，环境变量变更也会失效。
    """
    env_key = os.environ.get("USER_LLM_API_KEY", "")
    key = (config_version(), env_key)
    with _CFG_LOCK:
        if _CFG_CACHE["key"] == key and _CFG_CACHE["value"] is not None:
            # 返回深拷贝：调用方可能修改，不能让他们污染缓存
            return json.loads(json.dumps(_CFG_CACHE["value"]))

    cfg = _defaults()
    stored = store.get_config() or {}
    cfg = _deep_merge(cfg, stored)
    stored_key = cfg["llm"].get("api_key") or ""
    # DB 里存的可能是 Fernet 加密 token，运行时解密
    if stored_key:
        stored_key = crypto.decrypt_key(stored_key)
        cfg["llm"]["api_key"] = stored_key
    # providers 档案的 Key 同样密文落库，读时解密（多模型备援链用）
    for p in cfg["llm"].get("providers") or []:
        if isinstance(p, dict) and p.get("api_key"):
            try:
                p["api_key"] = crypto.decrypt_key(p["api_key"])
            except Exception:
                p["api_key"] = ""
    if stored_key and stored_key != "your-api-key-here":
        cfg["llm"]["key_source"] = "ui"
    else:
        cfg["llm"]["api_key"] = env_key
        cfg["llm"]["key_source"] = "env" if env_key else "none"
    cfg["llm"]["has_key"] = bool(cfg["llm"]["api_key"]) and cfg["llm"]["api_key"] != "your-api-key-here"

    with _CFG_LOCK:
        _CFG_CACHE["key"] = key
        _CFG_CACHE["value"] = cfg
    return json.loads(json.dumps(cfg))


# 每个配置分组允许的字段类型（None 表示任意）
_SCHEMA: dict[str, dict[str, type | tuple]] = {
    "llm": {"base_url": str, "model": str, "fallback_model": str, "api_key": (str, type(None)),
            "temperature": (int, float), "top_p": (int, float), "max_tokens": int,
            "has_key": bool, "key_source": str, "providers": list},
    "retrieval": {"kb_top_k": int, "min_overlap": (int, float), "embedding": str},
    "context": {"history_turns": int, "char_budget": int, "compress_threshold": int,
                "summarize": bool, "relevant_only": bool, "modules": dict, "persona": str},
    "memory": {"enable": bool, "retrieval_top_k": int, "auto_capture": bool,
               "capture_keywords": list, "visit_greeting": bool},
    "tools": {},
    "multimodal": {"enable": bool, "vision_model": str},
    "guardrail": {"injection_block": bool},
    "engine": {"mode": str},
    "escalation": {"confidence_threshold": (int, float), "max_message_len": int, "react_steps": int, "handoff": bool},
    "channels": {"allowlist_enabled": bool, "group_allowlist": list},
}


def validate_patch(patch: dict) -> None:
    """校验配置补丁，类型不符抛 ValueError。

    没有校验时，管理员提交 {"llm": "oops"} 会让 get_config() 后续全部 500，
    且已经落库、重启才恢复（已复现的自伤型 DoS）。
    """
    if not isinstance(patch, dict):
        raise ValueError("配置必须是对象")
    for group, value in patch.items():
        if group not in _SCHEMA:
            continue          # 未知分组忽略（保持向前兼容）
        if not isinstance(value, dict):
            raise ValueError(f"配置分组 {group} 必须是对象，收到 {type(value).__name__}")
        allowed = _SCHEMA[group]
        for key, v in value.items():
            if key not in allowed:
                continue
            expected = allowed[key]
            if expected is dict and not isinstance(v, dict):
                raise ValueError(f"{group}.{key} 必须是对象")
            if expected is list and not isinstance(v, list):
                raise ValueError(f"{group}.{key} 必须是数组")
            if expected not in (dict, list) and not isinstance(v, expected):
                names = expected.__name__ if isinstance(expected, type) else "/".join(t.__name__ for t in expected)
                raise ValueError(f"{group}.{key} 类型应为 {names}，收到 {type(v).__name__}")
    # 关键字段取值合法性
    engine = patch.get("engine", {}).get("mode")
    if engine is not None and engine not in ("rule", "llm", "auto"):
        raise ValueError("engine.mode 必须是 rule/llm/auto")
    top_k = patch.get("retrieval", {}).get("kb_top_k")
    if top_k is not None and not (1 <= int(top_k) <= 50):
        raise ValueError("retrieval.kb_top_k 必须在 1-50 之间")


def update_config(patch: dict) -> dict:
    patch = json.loads(json.dumps(patch or {}))
    validate_patch(patch)
    llm_patch = patch.get("llm") or {}
    if "api_key" in llm_patch and isinstance(llm_patch["api_key"], str) and llm_patch["api_key"].startswith("••"):
        llm_patch["api_key"] = get_config()["llm"].get("api_key") or ""
    # 落库前加密 api_key（Fernet），DB 里不再存明文
    if "api_key" in llm_patch and isinstance(llm_patch["api_key"], str):
        llm_patch["api_key"] = crypto.encrypt_key(llm_patch["api_key"])
    # providers 档案的 Key：掩码回环保留旧值；新值加密；空值删除
    provs = llm_patch.get("providers")
    if isinstance(provs, list):
        cur = get_config()["llm"].get("providers") or []
        for i, p in enumerate(provs):
            if not isinstance(p, dict) or "api_key" not in p:
                continue
            k = p.pop("api_key")
            if not isinstance(k, str):
                continue
            if k.startswith("••"):
                match = next((q for q in cur if isinstance(q, dict)
                              and q.get("base_url") == p.get("base_url")
                              and q.get("model") == p.get("model")
                              and q.get("name") == p.get("name")),
                             cur[i] if i < len(cur) else None)
                k = (match or {}).get("api_key") or ""
            if k.strip():
                p["api_key"] = crypto.encrypt_key(k)
    store.set_config(patch)
    invalidate_cache()
    return get_config()
