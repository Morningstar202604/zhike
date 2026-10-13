import pytest
from fastapi.testclient import TestClient


ADMIN = "test-admin-token"
CHANNEL = "test-channel-token"


@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("CHANNEL_TOKEN", CHANNEL)
    monkeypatch.delenv("USER_LLM_API_KEY", raising=False)
    from core.main import create_app
    from core import store

    store.init_db()
    return create_app()


@pytest.fixture()
def client(app):
    """默认带 admin/channel 双 token 的测试 client，覆盖大多数管理/渠道端点。"""
    with TestClient(app) as c:
        c.headers.update({"X-Admin-Token": ADMIN, "X-Channel-Token": CHANNEL})
        yield c


def create_web_session(client, visitor_id="anonymous"):
    """创建 web 访客会话并返回 (sid, secret)；后续访客请求必须带 X-Session-Secret。"""
    r = client.post("/api/sessions", json={"visitor_id": visitor_id})
    assert r.status_code == 200, r.text
    data = r.json()
    sid = data["session_id"]
    secret = data["session_secret"]
    # 后续该 client 的访客请求自动带 secret（同一 client 多会话时需注意隔离，测试一般单会话）
    client.headers.update({"X-Session-Secret": secret})
    return sid, secret


def reset_web_session_auth(client):
    client.headers.pop("X-Session-Secret", None)
