"""OneBot v11 动作注册表：动作定义、参数 schema、风险分级。

设计要点：
- 动作名与 OneBot v11 规范同名，网关侧不需要二次映射（单一事实源）
- 风险分级决定放行策略，不是装饰性字段：
    L0 只读   —— 查询类，可被 Agent 自主调用
    L1 可逆写 —— 发消息/撤回/禁言等，可撤销或影响有限
    L2 破坏性 —— 踢人/退群/删好友/提权，永远需要人工确认
- 每个动作带参数白名单：未声明的参数直接拒绝，防止把额外字段透传给协议端
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Risk = Literal[0, 1, 2]

RISK_L0, RISK_L1, RISK_L2 = 0, 1, 2
RISK_LABEL = {RISK_L0: "只读", RISK_L1: "可逆写", RISK_L2: "破坏性"}


@dataclass(frozen=True)
class ActionSpec:
    name: str                       # 与 OneBot v11 action 同名
    summary: str                    # 人话描述（用于审批摘要与控制台）
    risk: Risk
    args: tuple[str, ...] = ()      # 允许透传的参数
    required: tuple[str, ...] = ()  # 必填参数
    timeout_s: float = 10.0
    group: str = "general"          # 分类，便于控制台分组

    @property
    def requires_approval(self) -> bool:
        """L2 永远需要审批；这是硬规则，不接受配置覆盖。"""
        return self.risk == RISK_L2


# ---- 动作清单 ----
_SPECS: list[ActionSpec] = [
    # ===== L0：账号与自检 =====
    ActionSpec("get_login_info", "查询机器人自身账号信息", RISK_L0, group="account", timeout_s=5),
    ActionSpec("get_status", "查询机器人运行状态", RISK_L0, group="account", timeout_s=5),
    ActionSpec("get_version_info", "查询协议端版本", RISK_L0, group="account", timeout_s=5),

    # ===== L0：群与好友信息 =====
    ActionSpec("get_group_list", "获取加入的群列表", RISK_L0, group="group", timeout_s=10),
    ActionSpec("get_group_info", "获取单个群的信息", RISK_L0, ("group_id",), ("group_id",),
               group="group", timeout_s=8),
    ActionSpec("get_group_member_list", "获取群成员列表", RISK_L0, ("group_id",), ("group_id",),
               group="group", timeout_s=15),
    ActionSpec("get_group_member_info", "获取单个群成员信息", RISK_L0,
               ("group_id", "user_id", "no_cache"), ("group_id", "user_id"), group="group"),
    ActionSpec("get_friend_list", "获取好友列表", RISK_L0, group="friend", timeout_s=10),
    ActionSpec("get_stranger_info", "获取陌生人信息", RISK_L0, ("user_id", "no_cache"), ("user_id",),
               group="friend"),

    # ===== L0：消息与文件 =====
    ActionSpec("get_msg", "按消息 ID 获取消息内容", RISK_L0, ("message_id",), ("message_id",),
               group="message"),
    ActionSpec("get_forward_msg", "获取合并转发消息内容", RISK_L0, ("id",), ("id",), group="message"),
    ActionSpec("get_group_msg_history", "获取群历史消息", RISK_L0,
               ("group_id", "message_seq", "count"), ("group_id",), group="message", timeout_s=15),
    ActionSpec("get_group_file_list", "获取群文件列表", RISK_L0,
               ("group_id", "file_count"), ("group_id",), group="file", timeout_s=15),
    ActionSpec("get_group_file_url", "获取群文件下载链接", RISK_L0,
               ("group_id", "file_id", "busid"), ("group_id", "file_id"), group="file"),

    # ===== L1：消息发送 =====
    ActionSpec("send_msg", "发送消息（自动识别私聊/群聊）", RISK_L1,
               ("message_type", "user_id", "group_id", "message", "auto_escape"), ("message",),
               group="message", timeout_s=15),
    ActionSpec("send_group_msg", "发送群消息", RISK_L1,
               ("group_id", "message", "auto_escape"), ("group_id", "message"), group="message", timeout_s=15),
    ActionSpec("send_private_msg", "发送私聊消息", RISK_L1,
               ("user_id", "message", "auto_escape"), ("user_id", "message"), group="message", timeout_s=15),
    ActionSpec("delete_msg", "撤回消息", RISK_L1, ("message_id",), ("message_id",), group="message"),

    # ===== L1：群管理（可逆） =====
    ActionSpec("set_group_ban", "群禁言/解除禁言（duration=0 为解禁）", RISK_L1,
               ("group_id", "user_id", "duration"), ("group_id", "user_id", "duration"), group="group_admin"),
    ActionSpec("set_group_whole_ban", "全员禁言开关", RISK_L1,
               ("group_id", "enable"), ("group_id", "enable"), group="group_admin", timeout_s=15),
    ActionSpec("set_group_card", "修改群名片", RISK_L1,
               ("group_id", "user_id", "card"), ("group_id", "user_id"), group="group_admin"),
    ActionSpec("set_group_name", "修改群名称", RISK_L1,
               ("group_id", "group_name"), ("group_id", "group_name"), group="group_admin"),
    ActionSpec("send_group_notice", "发布群公告", RISK_L1,
               ("group_id", "content"), ("group_id", "content"), group="group_admin", timeout_s=15),
    ActionSpec("upload_group_file", "上传群文件", RISK_L1,
               ("group_id", "file", "name", "folder"), ("group_id", "file", "name"),
               group="file", timeout_s=60),
    ActionSpec("upload_private_file", "上传私聊文件", RISK_L1,
               ("user_id", "file", "name"), ("user_id", "file", "name"), group="file", timeout_s=60),

    # ===== L1：请求处理 =====
    ActionSpec("set_friend_add_request", "处理加好友请求", RISK_L1,
               ("flag", "approve", "remark"), ("flag", "approve"), group="request"),
    ActionSpec("set_group_add_request", "处理加群请求/邀请", RISK_L1,
               ("flag", "sub_type", "approve", "reason"), ("flag", "sub_type", "approve"), group="request"),

    # ===== L2：破坏性（永远需要人工确认） =====
    ActionSpec("set_group_kick", "将成员移出群聊", RISK_L2,
               ("group_id", "user_id", "reject_add_request"), ("group_id", "user_id"),
               group="destructive", timeout_s=20),
    ActionSpec("set_group_leave", "机器人退出群聊", RISK_L2,
               ("group_id", "is_dismiss"), ("group_id",), group="destructive", timeout_s=20),
    ActionSpec("delete_friend", "删除好友", RISK_L2, ("user_id",), ("user_id",),
               group="destructive", timeout_s=20),
    ActionSpec("set_group_admin", "设置/取消群管理员（提权）", RISK_L2,
               ("group_id", "user_id", "enable"), ("group_id", "user_id", "enable"),
               group="destructive", timeout_s=20),
    ActionSpec("set_group_special_title", "设置群成员专属头衔", RISK_L2,
               ("group_id", "user_id", "special_title"), ("group_id", "user_id"),
               group="destructive", timeout_s=20),
    ActionSpec("set_restart", "重启协议端（会导致机器人短暂离线）", RISK_L2,
               ("delay",), (), group="destructive", timeout_s=30),
]

REGISTRY: dict[str, ActionSpec] = {s.name: s for s in _SPECS}


def register(spec: ActionSpec) -> None:
    """并入外部定义的动作（电脑操作等）。已存在则跳过，避免重复注册。"""
    REGISTRY.setdefault(spec.name, spec)


def register_many(specs) -> None:
    for s in specs:
        register(s)


class UnknownAction(ValueError):
    pass


class InvalidParams(ValueError):
    pass


def get_spec(name: str) -> ActionSpec:
    spec = REGISTRY.get(name)
    if spec is None:
        raise UnknownAction(name)
    return spec


def validate_params(spec: ActionSpec, params: dict) -> dict:
    """只保留声明过的参数，校验必填项。多余参数直接丢弃（不透传给协议端）。"""
    if not isinstance(params, dict):
        raise InvalidParams("params 必须是对象")
    allowed = set(spec.args)
    cleaned = {k: v for k, v in params.items() if k in allowed}
    missing = [k for k in spec.required if cleaned.get(k) in (None, "")]
    if missing:
        raise InvalidParams(f"缺少必填参数：{', '.join(missing)}")
    extra = sorted(set(params) - allowed)
    if extra:
        # 不报错，但要记下来：多余字段往往意味着调用方理解有偏差
        cleaned["__dropped__"] = extra
    return cleaned


def catalog() -> list[dict]:
    """给控制台/Agent 看的动作清单。"""
    return [
        {
            "name": s.name,
            "summary": s.summary,
            "risk": s.risk,
            "risk_label": RISK_LABEL[s.risk],
            "group": s.group,
            "args": list(s.args),
            "required": list(s.required),
            "requires_approval": s.requires_approval,
        }
        for s in sorted(REGISTRY.values(), key=lambda x: (x.risk, x.group, x.name))
    ]


# 爆炸半径硬阈值：即使 T3 档位也不可配置关闭（见计划 7.3.2）
FORCED_APPROVAL_ALWAYS = frozenset({
    "set_group_leave",   # 退群
    "delete_friend",     # 删好友
})

# 默认允许 Agent 自主执行的动作（T1 档位）：只放查询类，写操作需要显式配置
DEFAULT_AGENT_ACTIONS = frozenset({
    "get_login_info", "get_status", "get_version_info",
    "get_group_list", "get_group_info", "get_group_member_list", "get_group_member_info",
    "get_friend_list", "get_stranger_info",
    "get_msg", "get_forward_msg", "get_group_msg_history",
    "get_group_file_list", "get_group_file_url",
})
