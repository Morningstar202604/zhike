"""部分组更新不应清掉同组其他键（如只改 model 时保留 api_key）。"""


def test_partial_group_update_keeps_other_keys(client):
    r = client.put("/api/config", json={"config": {"llm": {"api_key": "sk-keep-me-777", "base_url": "https://x.example/v1"}}})
    assert r.json()["config"]["llm"]["has_key"] is True
    r2 = client.put("/api/config", json={"config": {"llm": {"model": "test-model"}}})
    llm = r2.json()["config"]["llm"]
    assert llm["model"] == "test-model"
    assert llm["has_key"] is True
    assert llm["base_url"] == "https://x.example/v1"
    body = client.get("/api/config").json()["config"]["llm"]
    assert body["api_key"].endswith("777")
