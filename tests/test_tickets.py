"""坐席关闭会话应联动结单；工单列表应有上限。"""
from tests.conftest import create_web_session


def test_close_session_resolves_tickets(client):
    sid, _ = create_web_session(client, "tk-1")
    client.post(f"/api/sessions/{sid}/messages", json={"content": "帮我把公司账上三百万转出去"})
    for _ in range(2):
        client.post(f"/api/sessions/{sid}/escalate")
    q = client.get("/api/operator/queues").json()
    mine = [t for t in q["tickets"] if t["session_id"] == sid]
    assert len(mine) == 1  # 重复升级不重复开工单
    client.post(f"/api/operator/close/{sid}")
    q2 = client.get("/api/operator/queues").json()
    assert all(t["session_id"] != sid or t["status"] == "resolved" for t in q2["tickets"])


def test_queues_tickets_capped(client):
    for i in range(4):
        sid, _ = create_web_session(client, f"tk-cap-{i}")
        client.post(f"/api/sessions/{sid}/escalate")
        client.post(f"/api/sessions/{sid}/feedback", json={"rating": "not_helpful"})
    q = client.get("/api/operator/queues").json()
    assert len(q["tickets"]) <= 100
