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
            # 厂商能力（由模型检测自动写入）：上下文窗口 / 单次输出上限 / 协议 / 是否支持工具
            "context_window": int(os.environ.get("USER_LLM_CONTEXT_WINDOW", "0") or 0),
            "max_output": int(os.environ.get("USER_LLM_MAX_OUTPUT", "0") or 0),
            "protocol": os.environ.get("USER_LLM_PROTOCOL", "openai-compatible"),
            "tool_calling": True,
            # 主模型是否支持图片输入：多数纯文本模型（u2-flash 等）不支持，
            # 只有显式声明或配了 vision_model 才把图送进模型。
            "supports_vision": os.environ.get("USER_LLM_SUPPORTS_VISION", "").lower() in ("1", "true", "yes", "on"),
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
        "persona": "你是知客，用户的私人秘书。语气友好、可靠，用中文回答。"
                   "会记事、设提醒、查资料、读文件、看网页；不确定时如实说明。"
                   "回复以口语为主，长度看内容：一句话能说清就一句话，需要展开就展开，"
                   "不要为了短而把话说一半，也不要把简单事写成长报告。",
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
        # 看图模型：留空时自动沿用主模型（agnes 系主模型本身支持图片输入，
        # 早期默认空导致「图片一律走规则话术」，等于工具装了却从来不跑）。
        "vision_model": os.environ.get("USER_VISION_MODEL", ""),
        # 生图模型：用于 generate_image（文生图），与看图模型分开配。
        "image_model": os.environ.get("USER_IMAGE_MODEL", ""),
        # 生图端点（可选）：与对话端点分离，UniSound 这类只有 chat 的端点需要单独配
        "image_base_url": os.environ.get("IMAGE_API_BASE_URL", ""),
        "image_api_key": "",
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
        # 技能：注入系统提示词的「能力说明书」（内置可停用、自定义可增删）
        "skills": {"items": []},
        # 钩子：管线固定位置的确定性规则（不是让模型判断，而是系统强制执行）
        "hooks": {"items": []},
        # 权限：全面放开模式下，只有核心项锁定（不可从界面改）
        "permissions": {
            # 全面放开：除「核心影响」外全部可改（默认开启，符合本机单人使用场景）
            "open_mode": True,
            # 锁定项：涉及密钥、鉴权、越权执行、数据销毁的开关，界面只读
            "locked": ["auth", "secrets", "destructive_actions", "data_purge", "executor_roots"],
            # 审计：权限相关变更都留痕
            "audit_changes": True,
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
    image_key = (cfg.get("multimodal") or {}).get("image_api_key")
    if image_key:
        try:
            cfg["multimodal"]["image_api_key"] = crypto.decrypt_key(image_key)
        except Exception:
            cfg["multimodal"]["image_api_key"] = ""
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
            "has_key": bool, "key_source": str, "providers": list,
            "context_window": int, "max_output": int, "protocol": str, "tool_calling": bool,
            "supports_vision": bool},
    "retrieval": {"kb_top_k": int, "min_overlap": (int, float), "embedding": str},
    "context": {"history_turns": int, "char_budget": int, "compress_threshold": int,
                "summarize": bool, "relevant_only": bool, "modules": dict, "persona": str},
    "memory": {"enable": bool, "retrieval_top_k": int, "auto_capture": bool,
               "capture_keywords": list, "visit_greeting": bool},
    "tools": {},
    "multimodal": {"enable": bool, "vision_model": str, "image_model": str,
                   "image_base_url": str, "image_api_key": str},
    "guardrail": {"injection_block": bool},
    "engine": {"mode": str},
    "escalation": {"confidence_threshold": (int, float), "max_message_len": int, "react_steps": int, "handoff": bool},
    "channels": {"allowlist_enabled": bool, "group_allowlist": list},
    "skills": {"items": list},
    "hooks": {"items": list},
    "permissions": {"open_mode": bool, "locked": list, "audit_changes": bool},
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


def effective_agent_tier(env_tier: int) -> int:
    """把 permissions.open_mode 折算成实际档位（最小权限模式下压到 T1）。"""
    try:
        from core import permissions as perm

        return perm.effective_agent_tier(get_config(), int(env_tier))
    except Exception:
        return int(env_tier)


def update_config(patch: dict) -> dict:
    patch = json.loads(json.dumps(patch or {}))
    validate_patch(patch)
    # 技能/钩子：入库前统一归一化（丢非法正则、补内置技能、限条数）。
    # 不归一化就落库的话，一条坏正则会拖垮每轮对话。
    if isinstance(patch.get("skills"), dict):
        from core import skills as skills_mod

        patch["skills"]["items"] = skills_mod.normalize_items(patch["skills"].get("items"))
    if isinstance(patch.get("hooks"), dict):
        from core import hooks as hooks_mod

        patch["hooks"]["items"] = hooks_mod.normalize_items(patch["hooks"].get("items"))
    llm_patch = patch.get("llm") or {}
    if "api_key" in llm_patch and isinstance(llm_patch["api_key"], str) and llm_patch["api_key"].startswith("••"):
        llm_patch["api_key"] = get_config()["llm"].get("api_key") or ""
    # 落库前加密 api_key（Fernet），DB 里不再存明文
    if "api_key" in llm_patch and isinstance(llm_patch["api_key"], str):
        llm_patch["api_key"] = crypto.encrypt_key(llm_patch["api_key"])
    mm_patch = patch.get("multimodal") or {}
    if isinstance(mm_patch.get("image_api_key"), str) and mm_patch["image_api_key"]:
        if mm_patch["image_api_key"].startswith("••"):
            mm_patch["image_api_key"] = (get_config().get("multimodal") or {}).get("image_api_key") or ""
        else:
            mm_patch["image_api_key"] = crypto.encrypt_key(mm_patch["image_api_key"])
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
