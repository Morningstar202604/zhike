from fastapi.testclient import TestClient


def _make_app(tmp_path, monkeypatch, extra=None):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)
    from core.main import create_app
    from core import store

    store.init_db()
    return create_app()


def _fake_dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>cs-agent-spa</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_static_spa_serving(tmp_path, monkeypatch):
    dist = _fake_dist(tmp_path)
    app = _make_app(tmp_path, monkeypatch, {"CS_AGENT_STATIC_DIR": str(dist)})
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200 and "cs-agent-spa" in r.text
        r2 = c.get("/dashboard")
        assert r2.status_code == 200 and "cs-agent-spa" in r2.text
        assert c.get("/assets/app.js").status_code == 200
        assert c.get("/api/agent/health").status_code == 200
        assert c.get("/healthz").json()["ok"] is True


def test_static_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CS_AGENT_STATIC_DIR", raising=False)
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as c:
        assert c.get("/").status_code == 404


def test_cors_whitelist(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, {"CS_AGENT_CORS_ORIGINS": "https://cs.example.com"})
    with TestClient(app) as c:
        ok = c.get("/api/agent/health", headers={"Origin": "https://cs.example.com"})
        assert ok.headers.get("access-control-allow-origin") == "https://cs.example.com"
        bad = c.get("/api/agent/health", headers={"Origin": "https://evil.example"})
        assert bad.headers.get("access-control-allow-origin") is None


def test_llm_test_without_key(client):
    r = client.post("/api/admin/llm-test")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False and body["ok"] is False
    assert "USER_LLM_API_KEY" in body["error"]


def test_llm_test_unreachable_endpoint(client, monkeypatch):
    monkeypatch.setenv("USER_LLM_API_KEY", "sk-test-only")
    monkeypatch.setenv("USER_LLM_BASE_URL", "http://127.0.0.1:59999/v1")
    r = client.post("/api/admin/llm-test")
    body = r.json()
    assert body["configured"] is True and body["ok"] is False
    assert body["error"]


def test_llm_test_requires_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "abc")
    assert client.post("/api/admin/llm-test").status_code == 401
    assert client.post("/api/admin/llm-test", headers={"X-Admin-Token": "abc"}).status_code == 200
