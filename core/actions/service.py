"""动作执行服务：授权 → 审计落库 → 审批闸门 → 调用网关 → 回填结果。

执行顺序不可调换：
1. 先校验动作与参数（未注册/参数非法直接拒绝，不落审计表也行，但要记哈希链）
2. 再判权限（拒绝也要留痕）
3. 审计先写 pending 再执行——进程崩了也能查到"意图"
4. L2 需要审批：返回 pending_approval，由管理员决策后二次调用
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from core import redact, store
from core.actions import authz, client
from core.actions.registry import (
    RISK_LABEL,
    ActionSpec,
    InvalidParams,
    UnknownAction,
    get_spec,
    validate_params,
)

APPROVAL_TTL_S = int(os.environ.get("APPROVAL_TTL_S", "300"))


class ActionError(Exception):
    """带 HTTP 语义的动作错误，由路由层转成响应。"""

    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _is_computer_action(name: str) -> bool:
    return name.startswith(("fs.", "shell.", "proc."))


async def _run_action(spec: ActionSpec, params: dict, principal_label: str,
                      agent_autonomous_tier: int, session_id: str | None) -> dict:
    """把动作分发到对应执行端：电脑动作走执行器，QQ 动作走网关。

    agent_autonomous_tier 只在"Agent 自主执行"时非 0；
    管理员/坐席手动操作传 0，因此不会触发 T3 的爆炸半径阈值。
    """
    if _is_computer_action(spec.name):
        from core.actions import computer

        try:
            return {"data": await computer.dispatch(spec, params, principal_label,
                                                    agent_autonomous_tier, session_id)}
        except computer._RadiusExceeded as e:
            raise ActionError(str(e), 429)
        except computer._Denied as e:
            raise ActionError(f"执行器策略拒绝：{e}", 403)
        except computer._Timeout as e:
            raise ActionError(str(e), 504)
        except computer._BadArgs as e:
            raise ActionError(str(e), 400)
        except Exception as e:
            from core import executor_client

            if isinstance(e, executor_client.ExecutorOffline):
                raise ActionError(str(e), 503)
            if isinstance(e, executor_client.ExecutorError):
                raise ActionError(str(e), 502)
            raise
    return await client.call_action(spec.name, params, timeout_s=spec.timeout_s)


async def execute(
    action: str,
    params: dict,
    principal_raw: str,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    approval_id: str | None = None,
) -> dict:
    """执行一次动作。返回结构化结果，失败抛 ActionError。"""
    # ---- 1. 动作与参数 ----
    try:
        spec = get_spec(action)
    except UnknownAction:
        raise ActionError(f"未注册的动作：{action}", 404)

    try:
        cleaned = validate_params(spec, params or {})
    except InvalidParams as e:
        raise ActionError(str(e), 400)

    dropped = cleaned.pop("__dropped__", None)

    # ---- 2. 幂等：同一 key 直接返回首次结果，不重复执行 ----
    if idempotency_key:
        prev = store.action_log_by_idempotency(idempotency_key)
        if prev:
            return {
                "status": prev["status"],
                "result": prev.get("result"),
                "action": prev["action"],
                "action_log_id": prev["id"],
                "idempotent_replay": True,
            }

    principal = authz.parse_principal(principal_raw)

    # ---- 3. 权限判定 ----
    decision = authz.decide(spec, principal)
    log_id = uuid.uuid4().hex
    safe_params = redact.mask_value(cleaned)

    if not decision.allowed:
        store.action_log_create(log_id, action, spec.risk, safe_params, principal.label,
                                decision.effective_tier, session_id, idempotency_key)
        store.action_log_update(log_id, status="denied", error=decision.reason)
        store.append_audit(principal.label, "action.denied", {
            "action": action, "reason": decision.reason, "risk": spec.risk,
            "session_id": session_id,
        })
        raise ActionError(decision.reason, 403, {"action_log_id": log_id})

    # ---- 4. 审计先于执行 ----
    store.action_log_create(log_id, action, spec.risk, safe_params, principal.label,
                            decision.effective_tier, session_id, idempotency_key)

    # ---- 5. 审批闸门 ----
    if decision.needs_approval and not approval_id:
        ap_id = uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        expires = (datetime.now(timezone.utc) + timedelta(seconds=APPROVAL_TTL_S)).isoformat()
        summary = redact.summarize_for_approval(action, cleaned, spec.summary)
        store.approval_create(ap_id, log_id, action, summary, {
            "params": safe_params, "risk": spec.risk, "risk_label": RISK_LABEL[spec.risk],
            "principal": principal.label, "session_id": session_id,
            "dropped_params": dropped,
        }, _nonce_hash(nonce), expires)
        store.action_log_update(log_id, status="pending_approval", approval_id=ap_id)
        store.append_audit(principal.label, "action.pending_approval", {
            "action": action, "approval_id": ap_id, "summary": summary,
        })
        return {
            "status": "pending_approval",
            "approval_id": ap_id,
            "action": action,
            "summary": summary,
            "expires_at": expires,
            "action_log_id": log_id,
            # nonce 只在创建时返回一次，用于确认执行时的二次校验
            "nonce": nonce,
        }

    # ---- 6. 校验审批凭据（带 approval_id 的二次调用）----
    if approval_id:
        ap = store.approval_get(approval_id)
        if not ap:
            store.action_log_update(log_id, status="failed", error="审批不存在")
            raise ActionError("审批不存在", 404)

        # 6a. 状态必须已批准
        if ap["status"] == "pending":
            store.action_log_update(log_id, status="pending_approval", approval_id=approval_id)
            raise ActionError("审批尚未批准", 409, {"approval_id": approval_id})
        if ap["status"] == "denied":
            store.action_log_update(log_id, status="denied", error="审批被拒绝")
            raise ActionError("审批已被拒绝", 403, {"approval_id": approval_id})
        if ap["status"] == "consumed":
            store.action_log_update(log_id, status="denied", error="审批已被使用")
            raise ActionError("该审批已被使用，不能重复执行", 409, {"approval_id": approval_id})
        if ap["status"] == "expired" or _is_expired(ap["expires_at"]):
            store.approval_expire(approval_id)
            store.action_log_update(log_id, status="failed", error="审批已过期")
            raise ActionError("审批已过期", 410, {"approval_id": approval_id})

        # 6b. 审批必须与本次调用完全一致：动作、参数、主体
        #     否则「批准踢 A」可以被拿去执行「删好友」「踢 B」（已复现的提权路径）
        detail = ap.get("detail") or {}
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = {}

        if ap["action"] != action:
            store.action_log_update(log_id, status="denied", error="审批与动作不匹配")
            raise ActionError(
                f"该审批只批准了 {ap['action']}，不能用于 {action}", 403,
                {"approval_id": approval_id},
            )

        approved_params = detail.get("params") or {}
        if approved_params != safe_params:
            store.action_log_update(log_id, status="denied", error="审批与参数不匹配")
            raise ActionError("审批参数与本次调用不一致，已拒绝", 403, {"approval_id": approval_id})

        approved_principal = detail.get("principal")
        if approved_principal and approved_principal != principal.label:
            store.action_log_update(log_id, status="denied", error="审批与主体不匹配")
            raise ActionError("审批主体与本次调用不一致，已拒绝", 403, {"approval_id": approval_id})

        # 6c. 一次性消费：先原子占用，抢占失败说明已被别人用掉
        if not store.approval_consume(approval_id):
            store.action_log_update(log_id, status="denied", error="审批已被使用")
            raise ActionError("该审批已被使用，不能重复执行", 409, {"approval_id": approval_id})

    # ---- 7. 执行 ----
    started = datetime.now(timezone.utc)
    # 只有 Agent 自主执行时才把档位传下去（用于 T3 爆炸半径阈值）；
    # 管理员/坐席手动操作不受该阈值约束。
    autonomous_tier = decision.effective_tier if principal.kind == authz.PRINCIPAL_AGENT else 0
    try:
        result = await _run_action(spec, cleaned, principal.label, autonomous_tier, session_id)
    except ActionError as e:
        # 任何执行失败（网关/执行器/阈值）都必须落账，否则审计里只有成功记录
        store.action_log_update(log_id, status="failed", error=str(e),
                                executed_at=_now(), duration_ms=_elapsed_ms(started))
        store.append_audit(principal.label, "action.failed", {
            "action": action, "error": str(e)[:300], "status": e.status,
        })
        e.extra.setdefault("action_log_id", log_id)
        raise
    except client.GatewayTimeout as e:
        store.action_log_update(log_id, status="failed", error=str(e),
                                executed_at=_now(), duration_ms=_elapsed_ms(started))
        store.append_audit(principal.label, "action.failed", {
            "action": action, "error": str(e), "timeout": True,
        })
        raise ActionError(str(e), 504, {"action_log_id": log_id})
    except client.GatewayError as e:
        store.action_log_update(log_id, status="failed", error=str(e),
                                executed_at=_now(), duration_ms=_elapsed_ms(started))
        store.append_audit(principal.label, "action.failed", {"action": action, "error": str(e)})
        status = 503 if isinstance(e, client.GatewayOffline) else 502
        raise ActionError(str(e), status, {"action_log_id": log_id})

    duration = _elapsed_ms(started)
    safe_result = redact.mask_value(result.get("data"))
    store.action_log_update(log_id, status="executed", result=safe_result,
                            executed_at=_now(), duration_ms=duration)
    store.append_audit(principal.label, "action.executed", {
        "action": action, "risk": spec.risk, "params": safe_params,
        "result_preview": str(safe_result)[:300], "duration_ms": duration,
    })
    return {
        "status": "executed",
        "action": action,
        "result": safe_result,
        "action_log_id": log_id,
        "duration_ms": duration,
        "dropped_params": dropped or [],
    }


def _is_expired(expires_at: str) -> bool:
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def _elapsed_ms(started: datetime) -> int:
    return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)


def approve(approval_id: str, decided_by: str) -> dict:
    ap = store.approval_get(approval_id)
    if not ap:
        raise ActionError("审批不存在", 404)
    if ap["status"] != "pending":
        raise ActionError(f"审批已处理（{ap['status']}）", 409)
    if _is_expired(ap["expires_at"]):
        store.approval_expire(approval_id)
        raise ActionError("审批已过期", 410)
    d = store.approval_decide(approval_id, True, decided_by)
    if not d or not d.get("_won"):
        raise ActionError("审批已被其他人处理", 409)
    store.action_log_update(ap["action_log_id"], status="approved")
    store.append_audit(decided_by, "approval.approved", {
        "approval_id": approval_id, "action": ap["action"],
    })
    return {"approval_id": approval_id, "status": "approved", "action": ap["action"],
            "action_log_id": ap["action_log_id"]}


def deny(approval_id: str, decided_by: str, reason: str = "") -> dict:
    ap = store.approval_get(approval_id)
    if not ap:
        raise ActionError("审批不存在", 404)
    if ap["status"] != "pending":
        raise ActionError(f"审批已处理（{ap['status']}）", 409)
    d = store.approval_decide(approval_id, False, decided_by)
    if not d or not d.get("_won"):
        raise ActionError("审批已被其他人处理", 409)
    store.action_log_update(ap["action_log_id"], status="denied", error=reason or "审批被拒绝")
    store.append_audit(decided_by, "approval.denied", {
        "approval_id": approval_id, "action": ap["action"], "reason": reason,
    })
    return {"approval_id": approval_id, "status": "denied", "action": ap["action"]}
