"""回归：全模块审查发现的 P0/P1 缺陷。

每条都对应一个已复现的真实漏洞，改动实现必须让这里失败。
"""
import asyncio
import json

import pytest

from core import config, guardrail, pipeline, store
from core.actions import authz, service
from core.schemas import LlmReply
from tests.conftest import ADMIN, CHANNEL, create_web_session


# ============================================================
# P0-1 伪造 principal 提权
# ============================================================

@pytest.mark.parametrize("fake_principal", ["admin", "operator:anyone", "qq:123456"])
def test_anonymous_cannot_spoof_principal(client, fake_principal):
    """匿名请求自报身份必须无效——否则任何人写 principal=admin 就是管理员。"""
    client.headers.pop("X-Admin-Token", None)
    try:
        r = client.post("/api/actions/execute", json={
            "action": "send_group_msg",
            "params": {"group_id": "1", "message": "越权"},
            "principal": fake_principal,
        })
        assert r.status_code == 403, f"自报 {fake_principal} 竟然通过：{r.text}"
    finally:
        client.headers.update({"X-Admin-Token": ADMIN})


def test_admin_token_grants_admin(client, monkeypatch):
    from core.actions import client as gw

    async def fake(action, params, timeout_s=10.0):
        return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "call_action", fake)
    r = client.post("/api/actions/execute", json={
        "action": "send_group_msg", "params": {"group_id": "1", "message": "x"},
    })
    assert r.status_code == 200 and r.json()["status"] == "executed"


def test_agent_token_can_declare_agent_identity(client, monkeypatch):
    """服务间调用用 AGENT_TOKEN 证明身份，可以声明 agent@origin。"""
    monkeypatch.setenv("AGENT_TOKEN", "svc-token")
    monkeypatch.setenv("AGENT_TIER", "0")
    from core.actions import client as gw

    async def fake(action, params, timeout_s=10.0):
        return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "call_action", fake)
    r = client.post("/api/actions/execute",
                    json={"action": "get_login_info", "params": {},
                          "principal": "agent@qq:999"},
                    headers={"X-Agent-Token": "svc-token", "X-Admin-Token": ""})
    assert r.status_code == 200


# ============================================================
# P0-2 审批复用 / 跨动作
# ============================================================

def _l2_payload():
    return {"action": "set_group_kick", "params": {"group_id": "1", "user_id": "2"},
            "principal": "admin"}


def test_approval_cannot_be_reused(client, monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    from core.actions import client as gw

    calls = []

    async def fake(action, params, timeout_s=10.0):
        calls.append(action)
        return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "call_action", fake)
    ap = client.post("/api/actions/execute", json=_l2_payload()).json()["approval_id"]
    client.post(f"/api/approvals/{ap}/approve")

    first = client.post("/api/actions/execute", json={**_l2_payload(), "approval_id": ap})
    second = client.post("/api/actions/execute", json={**_l2_payload(), "approval_id": ap})
    assert first.status_code == 200
    assert second.status_code == 409, "同一审批不能执行两次"
    assert len(calls) == 1


def test_approval_cannot_execute_different_action(client, monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    from core.actions import client as gw

    calls = []

    async def fake(action, params, timeout_s=10.0):
        calls.append((action, params))
        return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "call_action", fake)
    ap = client.post("/api/actions/execute", json=_l2_payload()).json()["approval_id"]
    client.post(f"/api/approvals/{ap}/approve")

    r = client.post("/api/actions/execute", json={
        "action": "delete_friend", "params": {"user_id": "999"},
        "principal": "admin", "approval_id": ap,
    })
    assert r.status_code in (403, 409), "批准踢人不能拿去删好友"
    assert calls == []


def test_approval_cannot_execute_different_params(client, monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    from core.actions import client as gw

    calls = []

    async def fake(action, params, timeout_s=10.0):
        calls.append(params)
        return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "call_action", fake)
    ap = client.post("/api/actions/execute", json=_l2_payload()).json()["approval_id"]
    client.post(f"/api/approvals/{ap}/approve")

    r = client.post("/api/actions/execute", json={
        "action": "set_group_kick", "params": {"group_id": "1", "user_id": "777"},
        "principal": "admin", "approval_id": ap,
    })
    assert r.status_code in (403, 409), "批准踢 A 不能改成踢 B"
    assert calls == []


# ============================================================
# P0-3 Agent 身份降级
# ============================================================

def test_principal_serialize_preserves_origin(monkeypatch):
    monkeypatch.setenv("AUTHORIZED_QQ", "123456")
    monkeypatch.setenv("AGENT_TIER", "2")
    p = authz.parse_principal("agent@qq:123456")
    assert authz.effective_agent_tier(p) == 2

    serialized = p.serialize()
    assert serialized == "agent@qq:123456", "serialize 必须保留 origin"
    assert authz.effective_agent_tier(authz.parse_principal(serialized)) == 2, \
        "字符串往返后档位不能丢失"


def test_agent_label_alone_loses_origin(monkeypatch):
    """反证：用 label 做往返会丢 origin，这正是修复前的 bug。"""
    monkeypatch.setenv("AUTHORIZED_QQ", "123456")
    monkeypatch.setenv("AGENT_TIER", "2")
    p = authz.parse_principal("agent@qq:123456")
    via_label = authz.parse_principal(p.label)
    assert authz.effective_agent_tier(via_label) == 0, "label 丢 origin 是预期行为"


def test_agent_write_allowed_at_t2_after_roundtrip(client, monkeypatch):
    """T2 档位下授权 QQ 触发的 Agent 必须能真正执行写操作。"""
    monkeypatch.setenv("AGENT_TIER", "2")
    monkeypatch.setenv("AUTHORIZED_QQ", "123456")
    from core.actions import client as gw

    async def fake(action, params, timeout_s=10.0):
        return {"data": {"message_id": 1}}

    monkeypatch.setattr(gw, "call_action", fake)
    r = client.post("/api/actions/execute",
                    json={"action": "send_group_msg",
                          "params": {"group_id": "1", "message": "x"},
                          "principal": "agent@qq:123456"},
                    headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200, r.text


# ============================================================
# P0-4 关闭的会话被复活
# ============================================================

def test_closed_session_rejects_new_messages(client):
    sid, secret = create_web_session(client, "closed-1")
    h = {"X-Session-Secret": secret}
    client.post(f"/api/sessions/{sid}/messages", json={"content": "开发票"}, headers=h)
    client.post(f"/api/operator/close/{sid}")

    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "还在吗"}, headers=h)
    assert r.status_code == 409
    assert store.get_session(sid)["status"] == "closed", "关闭状态不能被复活"


def test_closed_session_rejects_escalate(client):
    sid, secret = create_web_session(client, "closed-2")
    client.post(f"/api/operator/close/{sid}")
    r = client.post(f"/api/sessions/{sid}/escalate", headers={"X-Session-Secret": secret})
    assert r.status_code == 409
    assert store.get_session(sid)["status"] == "closed"


def test_mark_escalated_does_not_revive_closed(client):
    sid, _ = create_web_session(client, "closed-3")
    store.close_session(sid)
    store.mark_escalated(sid, "test")
    assert store.get_session(sid)["status"] == "closed"


# ============================================================
# P1 审计链并发
# ============================================================

def test_audit_chain_under_concurrency(client):
    import threading

    errors = []

    def worker(i):
        try:
            for j in range(10):
                store.append_audit(f"w{i}", "test", {"i": i, "j": j})
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert errors == [], f"并发写审计出错：{errors}"
    result = store.verify_audit_chain()
    assert result["ok"] is True, f"并发写导致链分叉：{result}"


# ============================================================
# P1 非 ASCII 令牌导致 500
# ============================================================

def test_constant_time_equals_handles_non_ascii():
    from core.security import constant_time_equals

    assert constant_time_equals("密码", "密码") is True
    assert constant_time_equals("密码", "别的") is False
    assert constant_time_equals("abc", "密码") is False
    assert constant_time_equals("✀", "abc") is False


def test_non_ascii_token_does_not_500(client):
    """非 ASCII 令牌不能让鉴权端点 500。

    httpx 客户端会拒绝发送含非 ASCII 的 header（自己在客户端就抛 UnicodeEncodeError），
    所以这里直接调 security 层验证——真正要保证的是「比较函数不抛异常」。
    """
    from core.security import constant_time_equals

    # 不抛异常即为通过；返回 False 表示"不匹配"
    assert constant_time_equals("密码", "admin-token") is False
    assert constant_time_equals("admin-token", "密码") is False


def test_non_ascii_session_secret_rejected(client):
    """会话 secret 传非 ASCII 要安全拒绝，不能抛异常。

    HTTP 层发不出非 ASCII header（httpx 客户端自己就报错），
    所以直接验证底层校验函数——这是真正会崩的地方。
    """
    sid, _ = create_web_session(client, "nonascii")
    assert store.check_session_secret(sid, "密码测试") is False
    assert store.check_session_secret(sid, "") is False
    assert store.check_session_secret("not-a-session", "任意") is False


# ============================================================
# P1 配置类型校验
# ============================================================

@pytest.mark.parametrize("bad", [
    {"llm": "oops"},
    {"retrieval": {"kb_top_k": "abc"}},
    {"engine": {"mode": "nonsense"}},
    {"retrieval": {"kb_top_k": 999}},
    {"llm": {"temperature": "hot"}},
])
def test_invalid_config_rejected(client, bad):
    r = client.put("/api/config", json={"config": bad})
    assert r.status_code == 400, f"非法配置应被拒：{r.text}"
    assert client.get("/api/agent/health").status_code == 200, "拒绝后系统仍须可用"


def test_valid_config_still_accepted(client):
    r = client.put("/api/config", json={"config": {"llm": {"temperature": 0.7}}})
    assert r.status_code == 200


# ============================================================
# P1 上下文预算
# ============================================================

def test_context_budget_keeps_later_modules(monkeypatch):
    """KB 块超预算时，tools/memory 不能一起被丢掉。"""
    from core import llm as llm_mod

    # 预算要大于固定开销（persona + ReAct 指令约 250 字符），否则任何策略都塞不下三个模块。
    # 这里用 600：KB 块 5000 字符仍远超预算，足以验证"超长 KB 不挤掉其他模块"。
    cfg = {
        "llm": {"model": "m"},
        "context": {"char_budget": 600},
        "escalation": {"react_steps": 3},
        "multimodal": {},
    }
    ctx = {
        "persona": "你是客服。",
        "modules_on": {"kb": True, "tools": True, "memory": True},
        "kb_block": "X" * 5000,          # 远超预算
        "tools_schema": [{"name": "query_order", "args": ["order_id"], "desc": "查订单"}],
        "memory_block": "偏好：喜欢顺丰",
        "summary": "",
        "history": [],
        "used": {},
    }
    msgs, _ = llm_mod._build_messages("我的订单", ctx, cfg)
    system = msgs[0]["content"]
    assert "query_order" in system, "tools 不能因 KB 超长而丢失"
    assert "顺丰" in system, "memory 不能因 KB 超长而丢失"
    assert len(system) < 900, "整体仍应受预算约束"


# ============================================================
# P1 记忆 opt-out 持久化
# ============================================================

def test_opt_out_persists_and_blocks_capture(client):
    sid, secret = create_web_session(client, "optout-persist")
    h = {"X-Session-Secret": secret}
    client.post(f"/api/sessions/{sid}/messages", json={"content": "请忘记我的记忆"}, headers=h)
    assert store.memory_opted_out("optout-persist") is True

    # 之后再说话不能再被采集
    client.post(f"/api/sessions/{sid}/messages", json={"content": "我喜欢蓝色"}, headers=h)
    assert store.get_memory("optout-persist") == {}, "opt-out 后不得再采集"


def test_opt_out_does_not_affect_other_visitors(client):
    sid1, s1 = create_web_session(client, "opt-a")
    sid2, s2 = create_web_session(client, "opt-b")
    client.post(f"/api/sessions/{sid1}/messages", json={"content": "不要记住我"},
                headers={"X-Session-Secret": s1})
    client.post(f"/api/sessions/{sid2}/messages", json={"content": "我习惯用顺丰"},
                headers={"X-Session-Secret": s2})
    assert store.get_memory("opt-a") == {}
    assert any(k.startswith("pref_") for k in store.get_memory("opt-b")), "他人记忆不受影响"


# ============================================================
# P1 记忆关闭时不得影响检索
# ============================================================

def test_disabled_memory_does_not_rewrite_query(monkeypatch):
    from core import store as st

    cfg = {
        "retrieval": {"kb_top_k": 3, "min_overlap": 0.0, "embedding": "off"},
        "context": {"modules": {"memory": False}, "char_budget": 2400},
        "memory": {"enable": False},
    }
    captured = {}

    def fake_retrieve(query, top_k, min_overlap, use_embedding=None):
        captured["q"] = query
        return []

    import core.retrieval as rt
    monkeypatch.setattr(rt, "retrieve_scored", fake_retrieve)
    st.set_memory("mem-off", "last_topic", "上次聊的发票")

    pipeline.build_context("它怎么弄", [], cfg, "mem-off")
    assert captured["q"] == "它怎么弄", "记忆关闭时不得用 last_topic 改写检索"


# ============================================================
# P1 工具未命中要真转人工
# ============================================================

def test_tool_miss_escalates():
    r = asyncio.run(pipeline.run(
        pipeline.ChannelMessage(text="政策 火星基地"), [], "tool-miss"
    ))
    assert r.escalate is True, "工具明确说没找到时必须转人工"
    assert r.escalate_reason == "knowledge_miss"


# ============================================================
# P1 LLM 输出解析健壮性
# ============================================================

def test_llm_reply_keeps_answer_when_confidence_bad():
    r = LlmReply.safe_parse({"answer": "正常回答", "confidence": "high"})
    assert r.answer == "正常回答", "confidence 类型错不能把 answer 一起清空"


def test_llm_reply_single_citation_string():
    r = LlmReply.safe_parse({"answer": "答", "citations": "KB001"})
    assert r.citations == ["KB001"], "字符串 citation 不能被拆成单字符"


def test_llm_reply_confidence_bounded():
    assert LlmReply.safe_parse({"confidence": 5}).confidence == 1.0
    assert LlmReply.safe_parse({"confidence": -1}).confidence == 0.0


# ============================================================
# P1 Agent 异常必须收尾
# ============================================================

def test_agent_run_never_stuck_running(monkeypatch):
    from core import agent_loop

    async def bad(messages, tools):
        return {"usage": "not-a-dict", "thought": "x", "tool": None, "final": "y"}

    r = asyncio.run(agent_loop.run_agent("异常任务", "admin", bad))
    assert r.status == "error"
    runs = store.agent_runs_list(limit=10)
    assert runs[0]["status"] != "running", "异常后不能永久停留在 running"


# ============================================================
# P1 坐席端点不存在会话
# ============================================================

def test_operator_endpoints_404_for_missing_session(client):
    assert client.post("/api/operator/take/nope", json={"visitor_id": "op"}).status_code == 404
    assert client.post("/api/operator/reply/nope", json={"content": "x"}).status_code == 404
    assert client.post("/api/operator/close/nope").status_code == 404


# ============================================================
# P1 反馈归属校验
# ============================================================

def test_feedback_rejects_message_from_other_session(client):
    a = client.post("/api/sessions", json={"visitor_id": "fb-a"}).json()
    b = client.post("/api/sessions", json={"visitor_id": "fb-b"}).json()
    client.post(f"/api/sessions/{a['session_id']}/messages",
                json={"content": "开发票"}, headers={"X-Session-Secret": a["session_secret"]})
    msgs = client.get(f"/api/sessions/{a['session_id']}/messages",
                      headers={"X-Session-Secret": a["session_secret"]}).json()["messages"]
    mid = next(m["id"] for m in msgs if m["role"] == "agent")

    r = client.post(f"/api/sessions/{b['session_id']}/feedback",
                    json={"rating": "helpful", "message_id": mid},
                    headers={"X-Session-Secret": b["session_secret"]})
    assert r.status_code == 400, "不能给其他会话的消息打分"


# ============================================================
# P1 知识库并发写
# ============================================================

def test_concurrent_kb_writes_do_not_lose_entries(client, tmp_path, monkeypatch):
    """多个实例并发写知识库，一条都不能丢。"""
    import threading

    from core import retrieval

    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps({"kb": []}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb)
    retrieval._KB_CACHE = None

    errors = []
    from core.main import create_app
    from fastapi.testclient import TestClient

    def worker(i):
        try:
            with TestClient(create_app()) as c:
                c.headers.update({"X-Admin-Token": ADMIN, "X-Channel-Token": CHANNEL})
                for j in range(5):
                    r = c.post("/api/kb", json={
                        "id": f"C{i}{j}", "title": "t", "text": "x", "answer": "a", "tags": [],
                    })
                    if r.status_code != 200:
                        errors.append((i, j, r.status_code))
        except Exception as e:
            errors.append((i, type(e).__name__))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    data = json.loads(kb.read_text(encoding="utf-8"))["kb"]
    assert errors == [], f"并发写报错：{errors}"
    assert len(data) == 20, f"应写入 20 条，实际 {len(data)} 条（丢更新）"
    retrieval._KB_CACHE = None
