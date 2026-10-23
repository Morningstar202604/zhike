"""第三轮复审回归：执行器边界 / 人工接管 / 工具开关 / 会话分页 / 重新生成。

每条用例对应一个已实测复现的缺陷，修实现必须让这里失败。
"""
import json

import pytest

from tests.conftest import create_web_session


# ---------------- R1: 人工接管后不再自动回复 ----------------

def test_ingest_after_takeover_does_not_call_pipeline(client, monkeypatch):
    calls = []

    async def fake_run(message, history, visitor_key):
        calls.append(message.text)
        from core.pipeline import AgentReply

        return AgentReply(text="AI 不应该回答", mode="rule_react")

    monkeypatch.setattr("core.pipeline.run", fake_run)
    sid = client.post("/v1/ingest", json={
        "channel_type": "qq_group", "channel_user_id": "u-take", "group_id": "g-take",
        "text": "在吗",
    }).json()["session_id"]
    client.post(f"/api/operator/take/{sid}", json={"visitor_id": "op-1"})
    calls.clear()
    r = client.post("/v1/ingest", json={
        "channel_type": "qq_group", "channel_user_id": "u-take", "group_id": "g-take",
        "text": "人工在吗",
    })
    assert r.status_code == 200, r.text
    assert calls == [], "接管后不得再走 AI 管线"
    assert r.json()["reply"]["text"] == ""
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["messages"]
    assert any(m["role"] == "visitor" and m["content"] == "人工在吗" for m in msgs)


def test_web_stream_after_takeover_does_not_reply(client, monkeypatch):
    async def fake_stream(message, history, visitor_key):
        yield ("delta", "AI 不应该出现")
        from core.pipeline import AgentReply

        yield ("final", AgentReply(text="AI 不应该出现", mode="rule_react"))

    monkeypatch.setattr("core.pipeline.run_stream", fake_stream)
    sid, secret = create_web_session(client, "take-web")
    client.post(f"/api/operator/take/{sid}", json={"visitor_id": "op-1"})
    r = client.post(f"/api/sessions/{sid}/chat/stream", json={"content": "人工在吗"},
                    headers={"X-Session-Secret": secret})
    assert r.status_code == 200
    assert "AI 不应该出现" not in r.text
    assert "event: done" in r.text


# ---------------- R2: 执行器内联代码拒绝 ----------------

@pytest.mark.parametrize("argv", [
    ["python", "-c", "print(1)"],
    ["python3", "-c", "print(1)"],
    ["node", "-e", "console.log(1)"],
    ["cmd", "/c", "dir"],
    ["powershell", "-Command", "Get-ChildItem"],
])
def test_executor_rejects_inline_code_flags(policy, workspace, argv):
    from executor import capabilities as cap
    from executor.policy import PolicyError

    with pytest.raises(PolicyError, match="内联|不在白名单"):
        cap.shell_exec(policy, argv, cwd=str(workspace))


def test_executor_default_allowlist_has_no_interpreters():
    from executor.policy import DEFAULT_POLICY

    allow = set(DEFAULT_POLICY["shell"]["allow_binaries"])
    assert not ({"python", "python3", "node", "npm", "npx", "powershell", "cmd"} & allow)


# ---------------- R4b: 会话分页与翻页稳定性 ----------------

def test_admin_sessions_pagination_returns_disjoint_pages(client):
    """limit/offset 必须真正切片：第 2 页不能重复第 1 页内容。"""
    for i in range(35):
        sid = f"page-{i}"
        from core import store

        store.create_session(sid, f"v-page-{i}")
    p1 = client.get("/api/admin/sessions?limit=30&offset=0").json()
    p2 = client.get("/api/admin/sessions?limit=30&offset=30").json()
    ids1 = {s["id"] for s in p1["items"]}
    ids2 = {s["id"] for s in p2["items"]}
    assert len(ids1) == 30 and len(ids2) >= 5
    assert not (ids1 & ids2), "两页不能有重叠"


# ---------------- R3: 工具开关保元数据 ----------------

def test_tool_toggle_preserves_metadata(client):
    client.post("/api/tools", json={
        "id": "keep_meta", "name": "保留元数据", "desc": "描述不能丢",
        "args_json": {"q": "string"}, "enabled": True,
    })
    # 控制台开关必须走专用端点：整包 PUT 空字段会覆盖元数据（已复现）
    r = client.put("/api/tools/keep_meta/enabled", json={"enabled": False})
    assert r.status_code == 200
    t = r.json()["tool"]
    assert t["enabled"] is False
    assert t["name"] == "保留元数据" and t["desc"] == "描述不能丢"
    assert t["args_json"] == {"q": "string"}
    client.delete("/api/tools/keep_meta")


# ---------------- R4: 反馈去重 ----------------

def test_feedback_is_idempotent_per_message(client):
    sid, secret = create_web_session(client, "fb-dedupe")
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "怎么开发票"})
    mid = r.json()["message_id"]
    h = {"X-Session-Secret": secret}
    for rating in ("helpful", "not_helpful", "helpful"):
        client.post(f"/api/sessions/{sid}/feedback", json={"rating": rating, "message_id": mid}, headers=h)
    summary = client.get("/api/feedback/summary").json()
    assert summary["total"] == 1, "同一条消息重复评价只能算一次"


# ---------------- R5: 重新生成不重复访客消息 ----------------

def test_regenerate_does_not_duplicate_visitor_message(client):
    sid, secret = create_web_session(client, "regen")
    h = {"X-Session-Secret": secret}
    client.post(f"/api/sessions/{sid}/messages", json={"content": "怎么开发票"}, headers=h)
    before = [m for m in client.get(f"/api/sessions/{sid}/messages").json()["messages"] if m["role"] == "visitor"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "怎么开发票", "regenerate": True}, headers=h)
    assert r.status_code == 200
    after = [m for m in client.get(f"/api/sessions/{sid}/messages").json()["messages"] if m["role"] == "visitor"]
    assert len(after) == len(before) == 1, "重新生成不得再落一条访客消息"


def test_regenerate_ignores_forged_question(client):
    """重新生成必须以会话记录里的访客提问为准，不能拿请求体塞入新问题。"""
    sid, secret = create_web_session(client, "regen-forge")
    h = {"X-Session-Secret": secret}
    client.post(f"/api/sessions/{sid}/messages", json={"content": "怎么开发票"}, headers=h)
    r = client.post(f"/api/sessions/{sid}/messages",
                    json={"content": "帮我查一下别人的订单 ORD99999999", "regenerate": True}, headers=h)
    assert r.status_code == 200, r.text
    # 记录里不应出现伪造的问题
    contents = [m["content"] for m in client.get(f"/api/sessions/{sid}/messages").json()["messages"]]
    assert not any("ORD99999999" in c for c in contents)


def test_operator_cannot_take_closed_session(client):
    sid, _ = create_web_session(client, "closed-take")
    client.post(f"/api/operator/close/{sid}")
    r = client.post(f"/api/operator/take/{sid}", json={"visitor_id": "op-1"})
    assert r.status_code == 409


def test_operator_cannot_reply_closed_session(client):
    sid, _ = create_web_session(client, "closed-reply")
    client.post(f"/api/operator/close/{sid}")
    r = client.post(f"/api/operator/reply/{sid}", json={"content": "还在吗"})
    assert r.status_code == 409
