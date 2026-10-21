"""阶段 2：电脑动作集成 —— 执行器路由、拒绝路径、T3 阈值、执行器离线。"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from core import executor_client, store
from core.actions import computer, service
from tests.conftest import ADMIN


@pytest.fixture()
def fake_exec(monkeypatch):
    """打桩执行器：记录调用，按需返回或抛错。"""
    calls = []

    async def invoke(capability, params=None, timeout_s=30.0):
        calls.append((capability, params))
        return {"path": params.get("path"), "ok": True}

    monkeypatch.setattr(executor_client, "invoke", invoke)
    return calls


def _exec(client, action, params, principal="admin", **extra):
    return client.post("/api/actions/execute", json={
        "action": action, "params": params, "principal": principal, **extra,
    })


# ---------- 路由到执行器 ----------

def test_computer_action_routed_to_executor(client, fake_exec):
    r = _exec(client, "fs.list", {"path": "/tmp"})
    assert r.status_code == 200, r.text
    assert fake_exec == [("fs.list", {"path": "/tmp"})]


def test_qq_action_still_routed_to_gateway(client, monkeypatch):
    from core.actions import client as gw

    seen = []

    async def call_action(action, params, timeout_s=10.0):
        seen.append(action)
        return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "call_action", call_action)
    r = _exec(client, "get_login_info", {})
    assert r.status_code == 200
    assert seen == ["get_login_info"], "QQ 动作不能误走执行器"


# ---------- 执行器离线/拒绝/超时 ----------

def test_executor_offline_returns_503(client, monkeypatch):
    async def offline(capability, params=None, timeout_s=30.0):
        raise executor_client.ExecutorOffline("连接被拒绝")

    monkeypatch.setattr(executor_client, "invoke", offline)
    r = _exec(client, "fs.list", {"path": "/tmp"})
    assert r.status_code == 503
    logs = client.get("/api/actions/logs").json()["entries"]
    assert any(e["status"] == "failed" for e in logs)


def test_executor_denied_returns_403(client, monkeypatch):
    async def denied(capability, params=None, timeout_s=30.0):
        raise executor_client.ExecutorDenied("路径不在允许范围内")

    monkeypatch.setattr(executor_client, "invoke", denied)
    r = _exec(client, "fs.read", {"path": "../../etc/passwd"})
    assert r.status_code == 403


def test_executor_timeout_returns_504(client, monkeypatch):
    async def slow(capability, params=None, timeout_s=30.0):
        raise executor_client.ExecutorTimeout("超时")

    monkeypatch.setattr(executor_client, "invoke", slow)
    r = _exec(client, "fs.list", {"path": "/tmp"})
    assert r.status_code == 504


# ---------- 参数校验 ----------

def test_shell_exec_rejects_string_argv(client, fake_exec):
    r = _exec(client, "shell.exec", {"argv": "rm -rf /"})
    assert r.status_code == 400, r.text
    assert fake_exec == [], "字符串命令绝不能被执行"


def test_shell_exec_rejects_empty_argv(client, fake_exec):
    r = _exec(client, "shell.exec", {"argv": []})
    assert r.status_code == 400


def test_shell_exec_accepts_array(client, fake_exec):
    r = _exec(client, "shell.exec", {"argv": ["python", "-c", "print(1)"]})
    assert r.status_code == 200
    assert fake_exec[0][1]["argv"] == ["python", "-c", "print(1)"]


# ---------- 风险分级 ----------

def test_computer_risks():
    from core.actions.registry import get_spec

    assert get_spec("fs.list").risk == 0
    assert get_spec("fs.read").risk == 0
    assert get_spec("fs.write").risk == 1
    assert get_spec("fs.delete").risk == 2
    assert get_spec("proc.kill").risk == 2


def test_delete_requires_approval(client, fake_exec, monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    r = _exec(client, "fs.delete", {"path": "/tmp/x"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending_approval"
    assert fake_exec == [], "删除未审批不得执行"


def test_delete_disabled_by_default(client, fake_exec):
    r = _exec(client, "fs.delete", {"path": "/tmp/x"})
    assert r.status_code == 403
    assert fake_exec == []


# ---------- Agent 档位 ----------

def test_agent_stranger_cannot_write_file(client, fake_exec, monkeypatch):
    monkeypatch.setenv("AGENT_TIER", "3")
    monkeypatch.setenv("ALLOW_TIER3", "true")
    r = _exec(client, "fs.write", {"path": "/tmp/a.txt", "content": "x"},
              principal="agent@qq:123456")
    assert r.status_code == 403, "陌生人触发不得写文件"
    assert fake_exec == []


def test_agent_admin_can_write_at_t2(client, fake_exec, monkeypatch):
    monkeypatch.setenv("AGENT_TIER", "2")
    r = _exec(client, "fs.write", {"path": "/tmp/a.txt", "content": "x"},
              principal="agent@admin")
    assert r.status_code == 200, r.text


def test_agent_read_allowed_at_t0(client, fake_exec, monkeypatch):
    monkeypatch.setenv("AGENT_TIER", "0")
    r = _exec(client, "fs.read", {"path": "/tmp/a.txt"}, principal="agent@qq:999")
    assert r.status_code == 200


# ---------- T3 爆炸半径 ----------

def test_tier3_delete_radius_blocks_after_threshold(client, monkeypatch, tmp_path):
    """累计删除达到阈值后必须中断整个动作，并给出人话原因。"""
    monkeypatch.setattr(computer, "TIER3_MAX_DELETE_FILES", 3)

    async def ok(capability, params=None, timeout_s=30.0):
        return {"path": params.get("path"), "size": 10}

    monkeypatch.setattr(executor_client, "invoke", ok)

    sid = "radius-session"
    # 手工写入已达到阈值的删除记录
    for i in range(3):
        store.action_log_create(f"log-{i}", "fs.delete", 2, {"path": f"/tmp/{i}"},
                                "agent:admin", 3, sid)
        store.action_log_update(f"log-{i}", status="executed", result={"size": 10})

    reason = computer._check_tier3_radius(
        __import__("core.actions.registry", fromlist=["get_spec"]).get_spec("fs.delete"),
        {"path": "/tmp/next"}, sid,
    )
    assert reason and "安全阈值" in reason


def test_delete_radius_not_applied_to_admin(client, fake_exec, monkeypatch):
    """阈值只约束 T3 自主执行，管理员手动删除不受累计限制。"""
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    monkeypatch.setattr(computer, "TIER3_MAX_DELETE_FILES", 1)
    for i in range(5):
        store.action_log_create(f"adm-{i}", "fs.delete", 2, {}, "admin", 3, None)
        store.action_log_update(f"adm-{i}", status="executed", result={"size": 10})

    ap_id = _exec(client, "fs.delete", {"path": "/tmp/x"}).json()["approval_id"]
    client.post(f"/api/approvals/{ap_id}/approve")
    r = _exec(client, "fs.delete", {"path": "/tmp/x"}, approval_id=ap_id)
    assert r.status_code == 200, "admin 手动删除不该被 T3 阈值拦住"


# ---------- 结果打码 ----------

def test_executor_result_is_masked(client, monkeypatch):
    async def leak(capability, params=None, timeout_s=30.0):
        return {"content": "api_key=sk-abcdef1234567890", "api_key": "sk-abcdef1234567890"}

    monkeypatch.setattr(executor_client, "invoke", leak)
    r = _exec(client, "fs.read", {"path": "/tmp/a"})
    body = json.dumps(r.json(), ensure_ascii=False)
    assert "sk-abcdef1234567890" not in body, "执行结果里的密钥必须打码"
    assert "***" in body


# ---------- 目录接口包含电脑动作 ----------

def test_catalog_includes_computer_actions(client):
    names = {a["name"] for a in client.get("/api/actions/catalog").json()["actions"]}
    assert {"fs.read", "fs.write", "fs.delete", "shell.exec", "proc.list"} <= names


# ---------- 健康检查端点分离 ----------

def test_executor_health_hits_executor_not_gateway(client, monkeypatch):
    """回归：/api/executor/health 曾误调网关探测，导致执行器状态永远显示离线。"""
    called = {}

    async def fake_health():
        called["executor"] = True
        return {"online": True, "capabilities": ["fs.read"]}

    monkeypatch.setattr(executor_client, "health", fake_health)
    r = client.get("/api/executor/health")
    assert r.status_code == 200
    assert called.get("executor") is True
    assert r.json()["online"] is True


def test_gateway_health_hits_gateway(client, monkeypatch):
    from core.actions import client as gw

    async def fake_probe():
        return {"online": True, "connected": True, "self_id": "123"}

    monkeypatch.setattr(gw, "probe", fake_probe)
    r = client.get("/api/gateway/health")
    assert r.status_code == 200
    assert r.json()["connected"] is True
