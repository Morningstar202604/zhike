"""管理统计报表形状与计数回归。"""
from tests.conftest import create_web_session


def test_admin_stats_shape_and_counts(client):
    # 两个独立会话：转人工后该会话的后续消息只进坐席台，不再产生 Agent 回复，
    # 所以统计用例必须在不同会话里各问一次（这也是真实使用方式）。
    sid1, secret1 = create_web_session(client, "qq_group:410001")
    client.post(f"/api/sessions/{sid1}/messages", json={"content": "订单: ORD12345678 发货了吗"})
    first_msg = client.get(f"/api/sessions/{sid1}/messages").json()["messages"][-1]["id"]
    sid2, _ = create_web_session(client, "qq_group:410002")
    client.post(f"/api/sessions/{sid2}/messages", json={"content": "帮我把公司账上三百万转出去"})
    client.post(f"/api/sessions/{sid1}/feedback", json={"rating": "helpful", "message_id": first_msg},
                headers={"X-Session-Secret": secret1})

    r = client.get("/api/admin/stats")
    assert r.status_code == 200
    stats = r.json()

    assert len(stats["messages_by_hour"]) == 24
    assert sum(m["visitor"] for m in stats["messages_by_hour"]) == 2
    assert sum(m["agent"] for m in stats["messages_by_hour"]) == 2

    assert len(stats["sessions_by_day"]) == 7
    assert sum(s["count"] for s in stats["sessions_by_day"]) == 2

    assert any(t["tool"] == "query_order" for t in stats["tool_usage"])
    assert any(e["reason"] == "knowledge_miss" for e in stats["escalate_reasons"])
    assert any(c["channel"] == "qq_group" and c["count"] == 2 for c in stats["channels"])
    assert stats["feedback"]["helpful"] == 1


def test_admin_stats_empty_db(client):
    r = client.get("/api/admin/stats")
    assert r.status_code == 200
    stats = r.json()
    assert len(stats["messages_by_hour"]) == 24
    assert len(stats["sessions_by_day"]) == 7
    assert stats["tool_usage"] == []
    assert stats["escalate_reasons"] == []


def test_admin_stats_requires_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "abc")
    # 注意：client fixture 已固化了 ADMIN/CHANNEL 默认值，这里验证的是「不带 header 时 401」
    # 用一个裸 TestClient 风格：剥离默认 header
    client.headers.pop("X-Admin-Token", None)
    assert client.get("/api/admin/stats").status_code == 401
    assert client.get("/api/admin/stats", headers={"X-Admin-Token": "abc"}).status_code == 200
    client.headers.update({"X-Admin-Token": "test-admin-token"})
