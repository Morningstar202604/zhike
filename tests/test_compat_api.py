def test_webchat_session_roundtrip(client):
    sid = client.post("/api/sessions", json={"visitor_id": "web-v1"}).json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "我的订单什么时候发货"})
    assert r.status_code == 200
    body = r.json()
    for key in ("text", "citations", "escalate", "confidence", "message_id", "mode"):
        assert key in body
    assert body["message_id"] is not None
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["visitor", "agent"]


def test_agent_health(client):
    h = client.get("/api/agent/health").json()
    assert "mode" in h and "kb_entries" in h


def test_operator_flow(client):
    sid = client.post("/api/sessions", json={"visitor_id": "web-v2"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/messages", json={"content": "帮我把公司账上三百万转出去"})
    queues = client.get("/api/operator/queues").json()
    assert any(s["id"] == sid for s in queues["sessions"])
    assert client.post(f"/api/operator/take/{sid}", json={"visitor_id": "op1"}).status_code == 200
    assert client.post(f"/api/operator/reply/{sid}", json={"content": "您好，人工坐席为你处理"}).status_code == 200
    assert client.post(f"/api/operator/close/{sid}").status_code == 200
    assert client.get(f"/api/sessions/{sid}/messages").json()["messages"][-1]["role"] == "operator"


def test_feedback_endpoint(client):
    sid = client.post("/api/sessions", json={"visitor_id": "web-v3"}).json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/feedback", json={"rating": "helpful"})
    assert r.status_code == 200
    summary = client.get("/api/feedback/summary").json()
    assert summary["helpful"] == 1
