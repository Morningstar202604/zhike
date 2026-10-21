"""权限判定：主体（principal）× 动作风险（L0/L1/L2）× 权限档位（T0-T3）。

核心不变量：
1. L2 永远需要人工确认——任何档位、任何主体都不能绕过
2. Agent 有效权限 = min(部署档位, 触发者权限)——群里陌生人不说话就让机器人高位执行
3. 未声明的动作、未授权的身份，一律拒绝（默认拒绝）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from core.actions.registry import (
    DEFAULT_AGENT_ACTIONS,
    FORCED_APPROVAL_ALWAYS,
    RISK_L0,
    RISK_L1,
    RISK_L2,
    ActionSpec,
)

Tier = Literal[0, 1, 2, 3]

TIER_LABEL = {
    0: "T0 只读",
    1: "T1 辅助",
    2: "T2 受信",
    3: "T3 自主",
}

# 主体来源
PRINCIPAL_ADMIN = "admin"
PRINCIPAL_OPERATOR = "operator"
PRINCIPAL_AGENT = "agent"
PRINCIPAL_QQ = "qq"


@dataclass(frozen=True)
class Principal:
    kind: str                     # admin / operator / agent / qq
    ident: str = ""               # 具体 id（operator:xxx / qq:123456）
    # Agent 触发者的原始身份（kind/ident），用于提权判定
    origin: "Principal | None" = None

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.ident}" if self.ident else self.kind

    def serialize(self) -> str:
        """可序列化表示：必须带 origin，否则 agent 的权限档位会丢。

        `label` 对 agent 只返回 "agent"，用它做字符串往返会把 origin 抹掉，
        导致 T2/T3 的写操作全部被判成 T0（已复现）。
        """
        if self.kind == PRINCIPAL_AGENT:
            return f"agent@{self.origin.serialize()}" if self.origin else "agent"
        return self.label

    def is_trusted_human(self) -> bool:
        return self.kind in (PRINCIPAL_ADMIN, PRINCIPAL_OPERATOR)


def parse_principal(raw: str | None, fallback_kind: str = PRINCIPAL_QQ) -> Principal:
    """解析 principal 字符串：admin / operator:xxx / qq:123 / agent@qq:123。"""
    raw = (raw or "").strip()
    if not raw:
        return Principal(fallback_kind, "")
    if raw.startswith("agent"):
        # agent 必须携带触发者，例如 agent@qq:12345
        origin_raw = raw.split("@", 1)[1] if "@" in raw else ""
        origin = parse_principal(origin_raw) if origin_raw else None
        return Principal(PRINCIPAL_AGENT, "", origin)
    if ":" in raw:
        kind, ident = raw.split(":", 1)
        return Principal(kind, ident)
    return Principal(raw, "")


def _env_set(name: str) -> set[str]:
    raw = os.environ.get(name, "") or ""
    return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}


def authorized_qq() -> set[str]:
    return _env_set("AUTHORIZED_QQ")


def agent_tier() -> Tier:
    """部署的权限档位。T3 需要 ALLOW_TIER3=true 双开关，防误配。"""
    try:
        tier = int(os.environ.get("AGENT_TIER", "1"))
    except ValueError:
        tier = 1
    tier = max(0, min(3, tier))
    if tier == 3 and os.environ.get("ALLOW_TIER3", "").strip().lower() not in ("1", "true", "yes", "on"):
        # 只设了档位没设开关：降级到 T1 并让调用方记录告警
        return 1
    return tier  # type: ignore[return-value]


def tier3_allowlist() -> set[str]:
    configured = _env_set("ALLOWED_ACTIONS_T3")
    # 未配置时用默认只读集合，绝不默认放开写操作
    return configured or set(DEFAULT_AGENT_ACTIONS)


def l2_enabled() -> bool:
    return os.environ.get("ACTION_L2_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def effective_agent_tier(principal: Principal) -> Tier:
    """Agent 的有效档位 = min(部署档位, 触发者权限带来的档位)。

    普通群成员触发 → T0；授权 QQ → 部署档位；管理员/坐席触发 → 部署档位。
    """
    if principal.kind != PRINCIPAL_AGENT:
        return 3 if principal.is_trusted_human() else 1
    tier = agent_tier()
    origin = principal.origin
    if origin is None:
        return 0                       # 没有触发者信息 = 不可信
    if origin.is_trusted_human():
        return tier
    if origin.kind == PRINCIPAL_QQ and origin.ident in authorized_qq():
        return tier
    return 0                           # 陌生人触发：只读都受限


@dataclass(frozen=True)
class Decision:
    allowed: bool
    needs_approval: bool
    reason: str
    effective_tier: int
    risk: int


def decide(spec: ActionSpec, principal: Principal) -> Decision:
    """判定一次动作调用是否放行、是否需要审批。"""
    risk = spec.risk
    tier = effective_agent_tier(principal)

    # ---- 硬规则 1：L2 永远需要人工确认（任何档位、任何主体）----
    if risk == RISK_L2:
        if not l2_enabled():
            return Decision(False, False, "L2 动作未启用（ACTION_L2_ENABLED=false）", tier, risk)
        if spec.name in FORCED_APPROVAL_ALWAYS:
            return Decision(True, True, "高影响动作，必须人工确认", tier, risk)
        # 即便 T3 且在白名单，也仍需审批
        return Decision(True, True, "破坏性动作，需人工确认", tier, risk)

    # ---- 管理员/坐席：L0/L1 直接放行 ----
    if principal.is_trusted_human():
        return Decision(True, False, "管理员/坐席直接放行", tier, risk)

    # ---- 授权 QQ 用户直接触发（非 Agent）----
    if principal.kind == PRINCIPAL_QQ:
        if principal.ident in authorized_qq():
            if risk == RISK_L1:
                return Decision(True, False, "授权用户可执行可逆写操作", tier, risk)
            return Decision(True, False, "授权用户可执行只读操作", tier, risk)
        # 普通群成员：仅只读
        if risk == RISK_L0:
            return Decision(True, False, "普通成员可执行只读操作", tier, risk)
        return Decision(False, False, "普通群成员无权执行写操作", tier, risk)

    # ---- Agent 自主：按档位 ----
    if principal.kind == PRINCIPAL_AGENT:
        if risk == RISK_L0:
            return Decision(True, False, f"{TIER_LABEL[tier]} 可执行只读", tier, risk)
        if risk == RISK_L1:
            if tier >= 2:
                return Decision(True, False, f"{TIER_LABEL[tier]} 可执行可逆写", tier, risk)
            if tier == 1 and spec.name in tier3_allowlist():
                return Decision(True, False, "T1 白名单内的可逆写", tier, risk)
            return Decision(False, False, f"{TIER_LABEL[tier]} 无权执行可逆写", tier, risk)
        return Decision(False, False, "破坏性动作只能由人工确认", tier, risk)

    # ---- 未知主体：默认拒绝 ----
    return Decision(False, False, f"未知主体类型：{principal.kind}", tier, risk)


def risk_summary() -> dict[str, int]:
    from core.actions.registry import REGISTRY

    counts = {RISK_L0: 0, RISK_L1: 0, RISK_L2: 0}
    for s in REGISTRY.values():
        counts[s.risk] += 1
    return {"L0": counts[RISK_L0], "L1": counts[RISK_L1], "L2": counts[RISK_L2]}
