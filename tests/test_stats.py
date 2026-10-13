"""管理统计报表形状与计数回归。"""
from tests.conftest import create_web_session


def test_admin_stats_shape_and_counts(client):
    sid, _ = create_web_session(client, "qq_group:410001")
    client.post(f"/api/sessions/{sid}/messages", json={"content": "订单: ORD12345678 发货了吗"})
    client.post(f"/api/sessions/{sid}/messages", json={"content": "帮我把公司账上三百万转出去"})
    client.post(f"/api/sessions/{sid}/feedback", json={"rating": "helpful"})

    r = client.get("/api/admin/stats")
    assert r.status_code == 200
    stats = r.json()

    assert len(stats["messages_by_hour"]) == 24
    assert sum(m["visitor"] for m in stats["messages_by_hour"]) == 2
    assert sum(m["agent"] for m in stats["messages_by_hour"]) == 2

    assert len(stats["sessions_by_day"]) == 7
    assert sum(s["count"] for s in stats["sessions_by_day"]) == 1

    assert any(t["tool"] == "query_order" for t in stats["tool_usage"])
    assert any(e["reason"] == "knowledge_miss" for e in stats["escalate_reasons"])
    assert any(c["channel"] == "qq_group" and c["count"] == 1 for c in stats["channels"])
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
