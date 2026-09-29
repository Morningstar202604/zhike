import json


def _put_config(client, patch):
    return client.put("/api/config", json={"config": patch})


def test_put_api_key_stored_and_masked_in_get(client):
    r = _put_config(client, {"llm": {"api_key": "sk-test-abcd1234"}})
    assert r.status_code == 200
    body = r.json()["config"]["llm"]
    assert body["has_key"] is True
    assert body["api_key"].startswith("••")
    assert body["api_key"].endswith("1234")
    assert "sk-test" not in body["api_key"]


def test_put_masked_value_keeps_existing(client):
    _put_config(client, {"llm": {"api_key": "sk-live-9999"}})
    _put_config(client, {"llm": {"api_key": "••••9999"}})
    body = client.get("/api/config").json()["config"]["llm"]
    assert body["has_key"] is True
    assert body["api_key"].endswith("9999")


def test_put_empty_clears_key(client):
    _put_config(client, {"llm": {"api_key": "sk-temp-1"}})
    _put_config(client, {"llm": {"api_key": ""}})
    body = client.get("/api/config").json()["config"]["llm"]
    assert body["has_key"] is False


def test_models_endpoint_without_creds(client):
    r = client.get("/api/admin/llm-models")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False and body["models"] == []


def test_models_endpoint_unreachable(client, monkeypatch):
    monkeypatch.setenv("USER_LLM_API_KEY", "sk-env-1")
    monkeypatch.setenv("USER_LLM_BASE_URL", "http://127.0.0.1:59999/v1")
    r = client.get("/api/admin/llm-models")
    body = r.json()
    assert body["configured"] is True and body["ok"] is False and body["error"]


def test_llm_test_uses_db_key(client, monkeypatch):
    monkeypatch.delenv("USER_LLM_API_KEY", raising=False)
    _put_config(client, {"llm": {"api_key": "sk-db-1", "base_url": "http://127.0.0.1:59999/v1"}})
    r = client.post("/api/admin/llm-test")
    body = r.json()
    assert body["configured"] is True and body["ok"] is False
    assert body["source"] == "ui"
    assert body["base_url"] == "http://127.0.0.1:59999/v1"


def test_llm_models_requires_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "abc")
    assert client.get("/api/admin/llm-models").status_code == 401
    assert client.get("/api/admin/llm-models", headers={"X-Admin-Token": "abc"}).status_code == 200
