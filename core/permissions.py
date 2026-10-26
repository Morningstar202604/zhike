"""权限档位：全面放开模式下，只有「核心影响」项锁定。

设计背景（用户原话）：
「这个权限应该是加一个全面放开之下的大部分设置放开可以改，只有核心影响不能改。」

因此权限分两层：
1. **可改层**（界面可编辑，改完即时生效）：模型端点、采样参数、检索、上下文、
   技能、钩子、人设、群白名单、拟人节奏、工具开关、Agent 档位 T0–T2。
2. **锁定层**（界面只读，必须改环境变量 + 重启）：鉴权令牌（ADMIN/CHANNEL/AGENT）、
   密钥与加密盐（ZHIKE_SECRET_KEY）、破坏性动作总开关（ACTION_L2_ENABLED）、
   数据清空（/api/admin/purge）、执行器允许根目录（allow_roots）。

为什么这五项锁定：它们一旦被页面改掉，等于把「谁能进来/能删什么」交给了一个
可能被 XSS 或误操作影响的界面；而这五项恰好都能在环境变量层安全调整。

open_mode=true 时，锁定项在界面上仍然只读，但**非锁定项不再逐项确认**；
open_mode=false 时回到「最小权限」：Agent 档位锁在 T1、工具只开只读、
群白名单强制开启（fail-closed）。这是一个整体行为开关，不是装饰字段。
"""
from __future__ import annotations

# 核心锁定项：id → (界面名称, 改法说明)
LOCKED_ITEMS: dict[str, tuple[str, str]] = {
    "auth": ("鉴权令牌", "改 ADMIN_TOKEN / CHANNEL_TOKEN / AGENT_TOKEN 环境变量后重启"),
    "secrets": ("密钥与加密盐", "改 ZHIKE_SECRET_KEY / USER_LLM_API_KEY 环境变量后重启"),
    "destructive_actions": ("破坏性动作总开关", "改 ACTION_L2_ENABLED 环境变量后重启"),
    "data_purge": ("数据清空接口", "该接口只在服务端调用，界面不提供入口"),
    "executor_roots": ("执行器允许根目录", "改 policy.json 的 fs.allow_roots 后重启执行器"),
}

# 开放层：界面可改，改动需要审计
OPEN_ITEMS: dict[str, str] = {
    "llm": "模型端点 / 模型名 / 采样参数",
    "retrieval": "检索条数与阈值",
    "context": "上下文预算 / 历史轮数 / 人设",
    "memory": "长期记忆开关与触发词",
    "tools": "工具启用状态",
    "skills": "技能启用与自定义技能",
    "hooks": "输入/输出钩子",
    "channels": "群白名单与渠道准入",
    "escalation": "升级阈值与自主步数",
    "engine": "引擎模式（rule / llm / auto）",
    "agent_tier": "Agent 自主度 T0–T2",
    "multimodal": "看图 / 生图模型名",
}


def describe(cfg: dict) -> dict:
    perms = (cfg or {}).get("permissions") or {}
    locked = perms.get("locked") if isinstance(perms.get("locked"), list) else list(LOCKED_ITEMS)
    return {
        "open_mode": bool(perms.get("open_mode", True)),
        "audit_changes": bool(perms.get("audit_changes", True)),
        "locked": [
            {"id": key, "label": LOCKED_ITEMS.get(key, (key, ""))[0],
             "how": LOCKED_ITEMS.get(key, (key, ""))[1]}
            for key in locked if key in LOCKED_ITEMS
        ],
        "open": [{"id": key, "label": label} for key, label in OPEN_ITEMS.items()],
    }


def validate_patch(cfg: dict, body: dict) -> dict:
    """只接受 open_mode / audit_changes 两个开关；locked 列表不可通过接口扩张或收缩。

    允许改 locked 会让「核心影响不能改」这条约束自我失效：界面先把自己解锁，
    再改鉴权——所以这里直接忽略该字段。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是对象")
    patch: dict = {}
    if "open_mode" in body:
        patch["open_mode"] = bool(body["open_mode"])
    if "audit_changes" in body:
        patch["audit_changes"] = bool(body["audit_changes"])
    if "locked" in body:
        current = set((cfg.get("permissions") or {}).get("locked") or LOCKED_ITEMS)
        try:
            requested = set(body["locked"])
        except TypeError as e:
            raise ValueError("locked 必须是字符串数组") from e
        if requested != current:
            raise ValueError("核心锁定项不可通过界面修改（请改环境变量或 policy.json）")
    return patch


def effective_agent_tier(cfg: dict, env_tier: int) -> int:
    """open_mode=false 时把自主度压到 T1（辅助），并要求白名单 fail-closed。"""
    if (cfg.get("permissions") or {}).get("open_mode", True):
        return env_tier
    return min(env_tier, 1)
