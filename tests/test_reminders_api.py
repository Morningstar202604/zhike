import pytest


def test_reminders_api_list_create_cancel_compatibility(client):
    empty = client.get("/api/reminders", params={"visitor_id": "v-api"})
    assert empty.status_code == 200
    assert empty.json() == {"reminders": []}

    created = client.post("/api/reminders", json={
        "text": "给客户回电话",
        "when": "1天后",
        "visitor_id": "v-api",
        "chat_key": "webchat:v-api",
        "channel_type": "webchat",
    })
    assert created.status_code == 200, created.text
    reminder = created.json()
    assert reminder["text"] == "给客户回电话"
    assert reminder["visitor_id"] == "v-api"
    assert reminder["status"] == "pending"
    rid = reminder["id"]

    listed = client.get("/api/reminders", params={"visitor_id": "v-api", "limit": 0})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["reminders"]] == [rid]

    cancelled = client.delete(f"/api/reminders/{rid}")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"cancelled": rid}

    after = client.get("/api/reminders", params={"visitor_id": "v-api"})
    assert after.status_code == 200
    assert after.json() == {"reminders": []}


def test_reminders_api_preserves_validation_errors(client):
    empty_text = client.post("/api/reminders", json={"text": " ", "when": "1天后"})
    assert empty_text.status_code == 400
    assert empty_text.json()["detail"] == "提醒内容不能为空"

    bad_time = client.post("/api/reminders", json={"text": "交周报", "when": "有空的时候"})
    assert bad_time.status_code == 400
    assert "看不懂时间表达" in bad_time.json()["detail"]

    missing = client.delete("/api/reminders/999999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "提醒不存在或已触发"


def test_reminders_api_tick_returns_sent_count(client, monkeypatch):
    async def fake_tick():
        return 7

    monkeypatch.setattr("core.reminders.tick", fake_tick)
    response = client.post("/api/reminders/tick")
    assert response.status_code == 200
    assert response.json() == {"sent": 7}


@pytest.mark.parametrize("method, path, kwargs", [
    ("get", "/api/reminders", {}),
    ("post", "/api/reminders", {"json": {"text": "喝水", "when": "1天后"}}),
    ("delete", "/api/reminders/1", {}),
    ("post", "/api/reminders/tick", {}),
])
def test_reminders_api_requires_admin(app, method, path, kwargs):
    from fastapi.testclient import TestClient

    with TestClient(app) as unauthenticated:
        response = getattr(unauthenticated, method)(path, **kwargs)
    assert response.status_code == 401
