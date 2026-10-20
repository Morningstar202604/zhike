"""阶段 1：动作层回归 —— 注册表、权限矩阵、审批流、审计链、打码。

覆盖计划 7.3.3 的放行矩阵（T0-T3 × L0-L2 × 主体），以及阶段 1 DoD 全部条目。
"""
import json
import os
import uuid

import pytest

from core import redact, store
from core.actions import authz, registry, service
from tests.conftest import ADMIN, create_web_session


# ---------- 注册表完整性 ----------

def test_registry_has_all_risks():
    counts = authz.risk_summary()
    assert counts["L0"] >= 10 and counts["L1"] >= 10 and counts["L2"] >= 5


def test_every_action_has_summary_and_group():
    for spec in registry.REGISTRY.values():
        assert spec.summary, f"{spec.name} 缺描述"
        assert spec.group, f"{spec.name} 缺分组"
        assert spec.risk in (0, 1, 2)
        assert spec.timeout_s > 0


def test_l2_actions_always_require_approval():
    for spec in registry.REGISTRY.values():
        if spec.risk == 2:
            assert spec.requires_approval, f"{spec.name} 是 L2 但未标记需要审批"


def test_required_params_are_declared():
    for spec in registry.REGISTRY.values():
        for p in spec.required:
            assert p in spec.args, f"{spec.name} 必填参数 {p} 不在 args 白名单里"


def test_unknown_action_rejected():
    with pytest.raises(registry.UnknownAction):
        registry.get_spec("definitely_not_an_action")


def test_validate_params_drops_unknown():
    spec = registry.get_spec("get_group_info")
    cleaned = registry.validate_params(spec, {"group_id": "1", "evil": "x"})
    assert cleaned["group_id"] == "1"
    assert "evil" not in cleaned
    assert cleaned["__dropped__"] == ["evil"]


def test_validate_params_missing_required():
    spec = registry.get_spec("get_group_info")
    with pytest.raises(registry.InvalidParams):
        registry.validate_params(spec, {})


# ---------- 权限矩阵 ----------

def _p(raw):
    return authz.parse_principal(raw)


@pytest.mark.parametrize("tier,risk,principal,allowed,approval", [
    # 管理员：L0/L1 直接，L2 需审批
    (1, 0, "admin", True, False),
    (1, 1, "admin", True, False),
    # 普通群成员：仅只读
    (1, 0, "qq:111", True, False),
    (1, 1, "qq:111", False, False),
    # Agent 由陌生人触发：即使部署 T3 也只读
    (3, 0, "agent@qq:111", True, False),
    (3, 1, "agent@qq:111", False, False),
    # Agent 由管理员触发，T1：只读
    (1, 0, "agent@admin", True, False),
    # Agent T2：可逆写放行
    (2, 1, "agent@admin", True, False),
])
def test_permission_matrix(monkeypatch, tier, risk, principal, allowed, approval):
    monkeypatch.setenv("AGENT_TIER", str(tier))
    monkeypatch.setenv("ALLOW_TIER3", "true")
    name = {0: "get_group_list", 1: "send_group_msg", 2: "set_group_kick"}[risk]
    d = authz.decide(registry.get_spec(name), _p(principal))
    assert d.allowed is allowed, f"{tier}/{risk}/{principal}: {d.reason}"
    assert d.needs_approval is approval


def test_agent_cannot_escalate_beyond_trigger(monkeypatch):
    """核心安全不变量：陌生人触发的 Agent 即使部署 T3 也只有 T0。"""
    monkeypatch.setenv("AGENT_TIER", "3")
    monkeypatch.setenv("ALLOW_TIER3", "true")
    monkeypatch.setenv("AUTHORIZED_QQ", "999")
    stranger = authz.effective_agent_tier(_p("agent@qq:111"))
    trusted = authz.effective_agent_tier(_p("agent@qq:999"))
    assert stranger == 0, "陌生人触发必须压到 T0"
    assert trusted == 3


def test_tier3_requires_double_switch(monkeypatch):
    monkeypatch.setenv("AGENT_TIER", "3")
    monkeypatch.delenv("ALLOW_TIER3", raising=False)
    assert authz.agent_tier() == 1, "只设档位不设开关必须降级"


def test_l2_disabled_blocks_everyone(monkeypatch):
    monkeypatch.delenv("ACTION_L2_ENABLED", raising=False)
    for principal in ("admin", "agent@admin"):
        d = authz.decide(registry.get_spec("set_group_kick"), _p(principal))
        assert d.allowed is False, "L2 未启用时任何人都不应放行"


def test_l2_needs_approval_even_for_admin(monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    d = authz.decide(registry.get_spec("set_group_kick"), _p("admin"))
    assert d.allowed is True and d.needs_approval is True


def test_forced_approval_actions(monkeypatch):
    """退群/删好友：T3 也必须人工确认（硬规则）。

    名单硬编码在测试里：若实现把集合清空，测试必须失败。
    用 registry.FORCED_APPROVAL_ALWAYS 遍历是自证——集合空了循环就不跑，断言等于没写。
    """
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    monkeypatch.setenv("AGENT_TIER", "3")
    monkeypatch.setenv("ALLOW_TIER3", "true")
    required = {"set_group_leave", "delete_friend"}
    assert required <= registry.FORCED_APPROVAL_ALWAYS,         f"强制审批名单缺项：{required - registry.FORCED_APPROVAL_ALWAYS}"
    for name in required:
        d = authz.decide(registry.get_spec(name), _p("agent@admin"))
        assert d.needs_approval is True, f"{name} 必须强制审批"


def test_unknown_principal_denied():
    d = authz.decide(registry.get_spec("get_group_list"), _p("hacker:1"))
    assert d.allowed is False


# ---------- 打码 ----------

def test_redact_masks_sensitive_keys():
    out = redact.mask_value({"api_key": "sk-abc", "token": "xyz", "group_id": "123"})
    assert out["api_key"] == "***"
    assert out["token"] == "***"
    assert out["group_id"] == "123"


def test_redact_masks_token_patterns_in_text():
    out = redact.mask_value("使用 sk-1234567890abcdef 调用")
    assert "sk-1234567890abcdef" not in out
    assert "***" in out


def test_redact_truncates_huge_values():
    out = redact.mask_value({"image": "A" * 5000})
    assert len(out["image"]) < 600
    assert "truncated" in out["image"]


def test_redact_nested_and_lists():
    out = redact.mask_value({"a": {"password": "p", "items": [{"secret": "s"}]}})
    assert out["a"]["password"] == "***"
    assert out["a"]["items"][0]["secret"] == "***"


# ---------- 执行链路（打桩网关）----------

class FakeGateway:
    def __init__(self, data=None, error=None):
        self.data = data or {"ok": True}
        self.error = error
        self.calls = []

    async def __call__(self, action, params, timeout_s=10.0):
        self.calls.append((action, params))
        if self.error:
            raise self.error
        return {"data": self.data}


@pytest.fixture()
def fake_gw(monkeypatch):
    from core.actions import client

    gw = FakeGateway({"user_id": 1000, "nickname": "bot"})
    monkeypatch.setattr(client, "call_action", gw)
    return gw


def test_execute_success_records_audit(client, fake_gw, monkeypatch):
    monkeypatch.setenv("AGENT_TIER", "2")
    r = client.post("/api/actions/execute", json={
        "action": "get_login_info", "params": {}, "principal": "admin",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "executed"
    assert body["result"]["nickname"] == "bot"
    assert body["action_log_id"]
    assert fake_gw.calls == [("get_login_info", {})]

    logs = client.get("/api/actions/logs").json()["entries"]
    assert any(e["status"] == "executed" and e["action"] == "get_login_info" for e in logs)
    assert client.get("/api/admin/audit/verify").json()["ok"] is True


def test_execute_without_gateway_returns_503(client, monkeypatch):
    from core.actions import client as gw_client

    async def offline(action, params, timeout_s=10.0):
        raise gw_client.GatewayOffline("网关不可达")

    monkeypatch.setattr(gw_client, "call_action", offline)
    r = client.post("/api/actions/execute", json={
        "action": "get_login_info", "params": {}, "principal": "admin",
    })
    assert r.status_code == 503
    logs = client.get("/api/actions/logs").json()["entries"]
    assert any(e["status"] == "failed" for e in logs), "失败必须留痕"


def test_execute_timeout_returns_504(client, monkeypatch):
    from core.actions import client as gw_client

    async def slow(action, params, timeout_s=10.0):
        raise gw_client.GatewayTimeout("超时")

    monkeypatch.setattr(gw_client, "call_action", slow)
    r = client.post("/api/actions/execute", json={
        "action": "get_login_info", "params": {}, "principal": "admin",
    })
    assert r.status_code == 504


def test_forbidden_action_is_denied_and_logged(client):
    """普通 QQ 主体执行写操作必须被拒并留痕。需去掉 admin 头，否则主体是 admin。"""
    n_before = len(client.get("/api/actions/logs").json()["entries"])
    client.headers.pop("X-Admin-Token", None)
    try:
        r = client.post("/api/actions/execute", json={
            "action": "send_group_msg", "params": {"group_id": "1", "message": "hi"},
            "principal": "qq:999999",
        })
        assert r.status_code == 403, r.text
    finally:
        client.headers.update({"X-Admin-Token": ADMIN})
    logs = client.get("/api/actions/logs").json()["entries"]
    assert len(logs) == n_before + 1
    assert logs[0]["status"] == "denied"


def test_unknown_action_returns_404(client):
    r = client.post("/api/actions/execute", json={"action": "rm_rf", "params": {}, "principal": "admin"})
    assert r.status_code == 404


def test_missing_required_param_returns_400(client):
    r = client.post("/api/actions/execute", json={
        "action": "get_group_info", "params": {}, "principal": "admin",
    })
    assert r.status_code == 400


# ---------- L2 审批闭环 ----------

def test_l2_approval_flow(client, fake_gw, monkeypatch):
    """L2 完整审批闭环。

    关键：必须同时验证「spec 声明了 requires_approval」和「运行时确实拦住了」。
    只验证运行时拦截的话，把 requires_approval 改成 False 后
    这条测试仍可能因其他原因通过（变异测试证实过）。
    """
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")

    spec = registry.get_spec("set_group_kick")
    assert spec.risk == 2
    assert spec.requires_approval is True, "L2 动作必须在注册表层面声明需要审批"

    payload = {"action": "set_group_kick", "params": {"group_id": "1", "user_id": "2"},
               "principal": "admin"}

    # 1) 发起：进入待审批，未执行
    r1 = client.post("/api/actions/execute", json=payload)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["status"] == "pending_approval"
    ap_id = b1["approval_id"]
    assert fake_gw.calls == [], "未批准前绝不能执行"

    # 2) 待审批列表可见
    pending = client.get("/api/approvals?status=pending").json()["entries"]
    assert any(a["id"] == ap_id for a in pending)

    # 3) 批准
    assert client.post(f"/api/approvals/{ap_id}/approve").status_code == 200

    # 4) 带 approval_id 二次执行
    r2 = client.post("/api/actions/execute", json={**payload, "approval_id": ap_id})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "executed"
    assert len(fake_gw.calls) == 1, "批准后应恰好执行一次"


def test_l2_cannot_execute_before_approval(client, fake_gw, monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    payload = {"action": "set_group_kick", "params": {"group_id": "1", "user_id": "2"},
               "principal": "admin"}
    b = client.post("/api/actions/execute", json=payload).json()
    ap_id = b["approval_id"]
    r = client.post("/api/actions/execute", json={**payload, "approval_id": ap_id})
    assert r.status_code == 409, "未批准时带 approval_id 调用必须被拒"
    assert fake_gw.calls == []


def test_l2_denied_blocks_execution(client, fake_gw, monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    payload = {"action": "set_group_kick", "params": {"group_id": "1", "user_id": "2"},
               "principal": "admin"}
    b = client.post("/api/actions/execute", json=payload).json()
    ap_id = b["approval_id"]
    assert client.post(f"/api/approvals/{ap_id}/deny", json={"reason": "误操作"}).status_code == 200
    r = client.post("/api/actions/execute", json={**payload, "approval_id": ap_id})
    assert r.status_code == 403
    assert fake_gw.calls == []


def test_double_approve_only_executes_once(client, fake_gw, monkeypatch):
    """并发双击批准：第二次必须失败，动作只执行一次。"""
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    payload = {"action": "set_group_kick", "params": {"group_id": "1", "user_id": "2"},
               "principal": "admin"}
    ap_id = client.post("/api/actions/execute", json=payload).json()["approval_id"]
    first = client.post(f"/api/approvals/{ap_id}/approve")
    second = client.post(f"/api/approvals/{ap_id}/approve")
    assert first.status_code == 200
    assert second.status_code == 409, "重复批准必须拒绝"
    client.post("/api/actions/execute", json={**payload, "approval_id": ap_id})
    assert len(fake_gw.calls) == 1


def test_l2_requires_feature_flag(client, fake_gw, monkeypatch):
    monkeypatch.delenv("ACTION_L2_ENABLED", raising=False)
    r = client.post("/api/actions/execute", json={
        "action": "set_group_kick", "params": {"group_id": "1", "user_id": "2"}, "principal": "admin",
    })
    assert r.status_code == 403
    assert fake_gw.calls == []


# ---------- 幂等 ----------

def test_idempotency_replay(client, fake_gw):
    key = f"idem-{uuid.uuid4().hex}"
    p = {"action": "get_login_info", "params": {}, "principal": "admin", "idempotency_key": key}
    r1 = client.post("/api/actions/execute", json=p)
    r2 = client.post("/api/actions/execute", json=p)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("idempotent_replay") is True
    assert len(fake_gw.calls) == 1, "同一幂等键只能真正执行一次"


# ---------- 参数不透传 ----------

def test_extra_params_not_forwarded(client, fake_gw):
    r = client.post("/api/actions/execute", json={
        "action": "get_group_info",
        "params": {"group_id": "1", "injected": "evil"},
        "principal": "admin",
    })
    assert r.status_code == 200
    assert fake_gw.calls[0][1] == {"group_id": "1"}, "多余参数不得透传给协议端"


# ---------- 审计链 ----------

def test_audit_chain_detects_tampering(client, fake_gw):
    """篡改中间一行必须导致该行及其之后全部失效。

    只断言「有断点」不够：单行哈希校验也能发现篡改，
    那样即使 prev_hash 串联被删掉测试仍会通过（已用变异测试验证过）。
    真正的链式证明是：篡改第 N 行后，第 N+1 行的 prev_hash 对不上。
    """
    # 产生至少 4 条审计记录
    for _ in range(3):
        client.post("/api/actions/execute", json={"action": "get_login_info", "params": {}, "principal": "admin"})
    assert client.get("/api/admin/audit/verify").json()["ok"] is True

    with store.get_conn() as conn:
        rows = conn.execute("SELECT id, prev_hash, hash FROM audit_chain ORDER BY id").fetchall()
    assert len(rows) >= 3, "需要多条记录才能验证链式关系"
    # 前置断言：相邻记录必须真的串联（这本身就是链式证据）
    for prev, cur in zip(rows, rows[1:]):
        assert cur["prev_hash"] == prev["hash"], "审计记录未串联"

    # 篡改中间一行
    target = rows[1]["id"]
    with store.get_conn() as conn:
        conn.execute("UPDATE audit_chain SET payload = ? WHERE id = ?", ('{"tampered":true}', target))

    result = client.get("/api/admin/audit/verify").json()
    assert result["ok"] is False
    assert result["broken_at"] == target, "断点应定位到被篡改的那一行"


# ---------- 主体推断 ----------

def test_anonymous_principal_is_readonly(client, fake_gw):
    """不带令牌也不传 principal：默认匿名 qq，写操作被拒。"""
    client.headers.pop("X-Admin-Token", None)
    try:
        r = client.post("/api/actions/execute", json={
            "action": "send_group_msg", "params": {"group_id": "1", "message": "hi"},
        })
        assert r.status_code == 403
    finally:
        client.headers.update({"X-Admin-Token": ADMIN})


def test_constant_time_equals_rejects_wrong_token():
    """令牌比较必须真的比较，不是恒真。

    直接测 security.constant_time_equals：错误的令牌必须判否。
    只靠接口层测试抓不到「比较退化成恒真」——因为还有别的层兜着。
    """
    from core import security

    assert security.constant_time_equals("abc", "abc") is True
    assert security.constant_time_equals("abc", "abd") is False
    assert security.constant_time_equals("abc", "abcd") is False
    assert security.constant_time_equals("", "abc") is False
    assert security.constant_time_equals(None, "abc") is False


def test_admin_endpoint_rejects_wrong_admin_token(client):
    """错误管理员令牌必须 401，证明校验真的生效。"""
    client.headers["X-Admin-Token"] = "definitely-wrong"
    try:
        assert client.get("/api/admin/overview").status_code == 401
    finally:
        client.headers.update({"X-Admin-Token": ADMIN})


def test_admin_token_alone_marks_admin(client, fake_gw):
    """带管理员令牌不传 principal → 视为 admin，可执行只读。"""
    r = client.post("/api/actions/execute", json={"action": "get_login_info", "params": {}})
    assert r.status_code == 200


# ---------- 目录接口 ----------

def test_catalog_shape(client):
    d = client.get("/api/actions/catalog").json()
    assert len(d["actions"]) >= 30
    assert set(d["risks"]) == {"L0", "L1", "L2"}
    assert d["agent_tier"] in (0, 1, 2, 3)
    one = d["actions"][0]
    assert {"name", "summary", "risk", "risk_label", "group"} <= set(one)
