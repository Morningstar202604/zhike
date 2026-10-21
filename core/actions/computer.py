"""电脑操作动作：把执行器能力注册进同一套动作体系。

这样电脑操作和 QQ 操作共享同一套「权限档位 + 审批 + 审计」，不需要两套机制。
风险分级原则：
- 读（list/read/stat/proc.list）：L0
- 可逆写（write/mkdir/move）：L1
- 破坏性（delete/proc.kill）：L2
- 命令执行：L1（命令本身在策略白名单内，但可造成广泛影响，由 T3 阈值兜底）
"""
from __future__ import annotations

import os
import time

from core import executor_client, redact, store
from core.actions import authz
from core.actions.registry import ActionSpec, RISK_L0, RISK_L1, RISK_L2

# T3 爆炸半径阈值（不可通过配置关闭，见计划 7.3.2）
TIER3_MAX_DELETE_FILES = int(os.environ.get("TIER3_MAX_DELETE_FILES", "50"))
TIER3_MAX_DELETE_BYTES = int(os.environ.get("TIER3_MAX_DELETE_BYTES", str(500 * 1024 * 1024)))
TIER3_MAX_L2_PER_RUN = int(os.environ.get("TIER3_MAX_L2_PER_RUN", "5"))

COMPUTER_ACTIONS: list[ActionSpec] = [
    ActionSpec("fs.list", "列出目录内容", RISK_L0, ("path", "depth"), ("path",),
               group="computer", timeout_s=30),
    ActionSpec("fs.read", "读取文本文件", RISK_L0, ("path", "encoding"), ("path",),
               group="computer", timeout_s=30),
    ActionSpec("fs.stat", "查看文件/目录信息", RISK_L0, ("path",), ("path",),
               group="computer", timeout_s=20),
    ActionSpec("fs.write", "写入文件（覆盖前自动留快照）", RISK_L1,
               ("path", "content", "encoding"), ("path", "content"),
               group="computer", timeout_s=30),
    ActionSpec("fs.mkdir", "创建目录", RISK_L1, ("path",), ("path",),
               group="computer", timeout_s=20),
    ActionSpec("fs.move", "移动或重命名", RISK_L1, ("src", "dst"), ("src", "dst"),
               group="computer", timeout_s=30),
    ActionSpec("fs.delete", "删除文件或目录（默认进回收站）", RISK_L2,
               ("path", "mode"), ("path",), group="computer", timeout_s=30),
    ActionSpec("shell.exec", "执行白名单命令", RISK_L1,
               ("argv", "cwd", "timeout_s"), ("argv",), group="computer", timeout_s=120),
    ActionSpec("proc.list", "列出运行中的进程", RISK_L0, ("filter",), group="computer", timeout_s=20),
    ActionSpec("proc.kill", "结束进程", RISK_L2, ("pid", "name"), ("pid",),
               group="computer", timeout_s=20),
]

# 动作名 → 执行器能力名（当前 1:1，保留映射便于将来改名）
_CAP_MAP = {spec.name: spec.name for spec in COMPUTER_ACTIONS}


def register_into(registry: dict) -> None:
    """把电脑动作并入主注册表（在 registry 模块加载后调用一次）。"""
    for spec in COMPUTER_ACTIONS:
        registry.setdefault(spec.name, spec)


async def dispatch(spec: ActionSpec, params: dict, principal_label: str,
                   effective_tier: int, session_id: str | None) -> dict:
    """执行一个电脑动作：阈值检查 → 调执行器 → 结果打码。"""
    cleaned = dict(params)

    # ---- T3 爆炸半径：删除类动作累计到阈值就强制中断 ----
    if spec.risk == RISK_L2 and effective_tier >= 3:
        guard = _check_tier3_radius(spec, cleaned, session_id)
        if guard:
            raise _RadiusExceeded(guard)

    capability = _CAP_MAP[spec.name]
    invocation = dict(cleaned)

    # shell.exec 的 argv 必须是数组；从 JSON 里可能是字符串，这里兜底拒绝
    if spec.name == "shell.exec":
        argv = invocation.get("argv")
        if isinstance(argv, str):
            # 不接受字符串命令，避免调用方误以为可以传 shell 命令行
            raise _BadArgs("argv 必须是字符串数组，不接受整条命令行")
        if not isinstance(argv, list) or not argv:
            raise _BadArgs("argv 必须是非空数组")

    try:
        result = await executor_client.invoke(capability, invocation, timeout_s=spec.timeout_s)
    except executor_client.ExecutorDenied as e:
        raise _Denied(str(e))
    except executor_client.ExecutorTimeout as e:
        raise _Timeout(str(e))

    # 结果里可能含路径与内容：打码后再回传与落库
    return redact.mask_value(result)


class _RadiusExceeded(Exception):
    pass


class _Denied(Exception):
    pass


class _Timeout(Exception):
    pass


class _BadArgs(Exception):
    pass


def _check_tier3_radius(spec: ActionSpec, params: dict, session_id: str | None) -> str | None:
    """检查本次会话/运行的累计破坏量是否超阈值。返回拒绝原因，None 表示放行。

    用 action_log 里的历史记录统计（同一 session 内已执行的 L2 动作）。
    """
    if spec.name != "fs.delete":
        # 非删除类的 L2（proc.kill 等）只计数
        count = _count_l2_in_session(session_id)
        if count >= TIER3_MAX_L2_PER_RUN:
            return f"本次会话已执行 {count} 次破坏性动作，达到上限（{TIER3_MAX_L2_PER_RUN}）"
        return None

    count, total_bytes = _delete_stats(session_id)
    if count >= TIER3_MAX_DELETE_FILES:
        return (f"本次会话已删除 {count} 个文件，达到安全阈值上限"
                f"（{TIER3_MAX_DELETE_FILES}），已暂停等待确认")
    if total_bytes >= TIER3_MAX_DELETE_BYTES:
        return (f"本次会话已删除 {total_bytes // 1024} KB，达到体积阈值上限，已暂停等待确认")
    return None


def _count_l2_in_session(session_id: str | None) -> int:
    import json as _json

    if not session_id:
        # 无会话上下文时按最近 1 小时全量计
        sql = ("SELECT COUNT(*) c FROM action_log WHERE risk = 2 AND status = 'executed' "
               "AND created_at >= datetime('now', '-1 hour')")
        params: tuple = ()
    else:
        sql = ("SELECT COUNT(*) c FROM action_log WHERE risk = 2 AND status = 'executed' "
               "AND session_id = ?")
        params = (session_id,)
    with store.get_conn() as conn:
        return conn.execute(sql, params).fetchone()["c"]


def _delete_stats(session_id: str | None) -> tuple[int, int]:
    """统计已执行的删除动作数量与累计字节数。"""
    import json as _json

    if session_id:
        sql = ("SELECT params, result FROM action_log WHERE action = 'fs.delete' "
               "AND status = 'executed' AND session_id = ?")
        params: tuple = (session_id,)
    else:
        sql = ("SELECT params, result FROM action_log WHERE action = 'fs.delete' "
               "AND status = 'executed' AND created_at >= datetime('now', '-1 hour')")
        params = ()
    files = 0
    total = 0
    with store.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    for r in rows:
        files += 1
        try:
            res = _json.loads(r["result"] or "{}")
            total += int(res.get("size") or 0)
        except (ValueError, TypeError):
            pass
    return files, total


def radius_limits() -> dict:
    return {
        "max_delete_files": TIER3_MAX_DELETE_FILES,
        "max_delete_bytes": TIER3_MAX_DELETE_BYTES,
        "max_l2_per_run": TIER3_MAX_L2_PER_RUN,
    }
