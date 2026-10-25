"""控制台访问令牌引导 / 消息搜索 / 会话导出。

这三条是「本机免输令牌」与「对话可检索、可带走」的基础能力，
共同点是：一旦行为退化，用户会静默地进不去或搜不到，必须靠测试盯住。
"""
from __future__ import annotations

from tests.conftest import create_web_session


# ------------------------------------------------------------
# 本机令牌引导
# ------------------------------------------------------------

def test_local_token_bootstrap_returns_admin_token_locally(client):
    """本机（回环）访问：直接下发令牌，用户无需手输。"""
    r = client.get("/api/bootstrap/local-token")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["token"] == "test-admin-token"


def test_local_token_bootstrap_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("CS_AGENT_LOCAL_TOKEN_BOOTSTRAP", "0")
    r = client.get("/api/bootstrap/local-token")
    assert r.status_code == 403


def test_local_token_bootstrap_available_flag_when_token_missing(monkeypatch):
    """服务端没配 ADMIN_TOKEN 时不能返回空 token 让前端误以为登录成功。"""
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from core.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        r = c.get("/api/bootstrap/local-token")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is False and d["token"] == ""


# ------------------------------------------------------------
# 消息搜索
# ------------------------------------------------------------

def test_search_messages_finds_content_across_sessions(client):
    sid1, _ = create_web_session(client, "search-a")
    client.post(f"/api/sessions/{sid1}/messages", json={"content": "我想问下退款流程"})

    sid2, _ = create_web_session(client, "search-b")
    client.post(f"/api/sessions/{sid2}/messages", json={"content": "发票怎么开"})

    r = client.get("/api/search/messages", params={"q": "退款"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results, "应能搜到含『退款』的消息"
    assert all("退款" in (x["content"] or "") for x in results)


def test_search_messages_empty_query_returns_nothing(client):
    r = client.get("/api/search/messages", params={"q": "  "})
    assert r.status_code == 200
    assert r.json()["results"] == []


def test_search_messages_respects_visitor_filter(client):
    sid1, _ = create_web_session(client, "filter-a")
    client.post(f"/api/sessions/{sid1}/messages", json={"content": "同一个关键词 hello"})
    sid2, _ = create_web_session(client, "filter-b")
    client.post(f"/api/sessions/{sid2}/messages", json={"content": "同一个关键词 hello"})

    r = client.get("/api/search/messages", params={"q": "hello", "visitor_id": "filter-a"})
    rows = r.json()["results"]
    assert rows and all(x["visitor_id"] == "filter-a" for x in rows)


def test_search_messages_escapes_like_wildcards(client):
    """% 不能被当成通配符把整表命中。"""
    sid, _ = create_web_session(client, "wild")
    client.post(f"/api/sessions/{sid}/messages", json={"content": "普通消息"})
    r = client.get("/api/search/messages", params={"q": "%"})
    assert r.json()["results"] == []


# ------------------------------------------------------------
# 会话导出
# ------------------------------------------------------------

def test_export_session_markdown(client):
    sid, _ = create_web_session(client, "export-me")
    client.post(f"/api/sessions/{sid}/messages", json={"content": "导出测试内容"})

    r = client.get(f"/api/sessions/{sid}/export")
    assert r.status_code == 200
    d = r.json()
    assert "导出测试内容" in d["markdown"]
    assert d["filename"].endswith(".md")
    # 说话人标签用产品语汇，不是内部角色名
    assert "用户" in d["markdown"] and "秘书" in d["markdown"]


def test_export_session_json(client):
    sid, _ = create_web_session(client, "export-json")
    client.post(f"/api/sessions/{sid}/messages", json={"content": "json 导出"})
    r = client.get(f"/api/sessions/{sid}/export", params={"fmt": "json"})
    assert r.status_code == 200
    d = r.json()
    assert d["session"]["id"] == sid
    assert any("json 导出" in (m["content"] or "") for m in d["messages"])


def test_export_unknown_session_404(client):
    r = client.get("/api/sessions/nonexistent-session/export")
    assert r.status_code == 404


def test_export_requires_auth_or_session_secret(client):
    """导出含完整对话，不能匿名可拿。"""
    from tests.conftest import reset_web_session_auth

    sid, _ = create_web_session(client, "secret-only")
    reset_web_session_auth(client)      # 去掉 session secret
    client.headers.pop("X-Admin-Token")  # 去掉管理员令牌
    r = client.get(f"/api/sessions/{sid}/export")
    assert r.status_code == 401
