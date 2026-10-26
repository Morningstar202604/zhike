"""档案 / 黑话：沉淀、增量更新、注入提示词、API 契约。"""
import pytest

from core import profiles


def test_profile_created_and_stats_accumulate(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "p.db"))
    from core import store
    store._SCHEMA_READY.clear()
    store.init_db()

    p1 = profiles.upsert_profile("qq:1", "user", display_name="小明", alias="明明",
                                 tags=["夜猫子"], stats_delta={"messages": 1})
    assert p1["display_name"] == "小明" and p1["aliases"] == ["明明"]
    p2 = profiles.upsert_profile("qq:1", "user", alias="三儿", stats_delta={"messages": 2})
    assert p2["aliases"] == ["明明", "三儿"]
    assert p2["stats"]["messages"] == 3
    assert p2["tags"] == ["夜猫子"], "没传 tags 时不能清空已有标签"


def test_group_profile_and_block(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "p2.db"))
    from core import store
    store._SCHEMA_READY.clear()
    store.init_db()

    profiles.upsert_profile("qq_group:9", "group", display_name="吹水群", tags=["嘴毒", "夜聊"])
    block = profiles.profile_block("qq:1", "qq_group:9")
    assert "吹水群" in block and "嘴毒" in block


def test_lexicon_learn_from_definition(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "p3.db"))
    from core import store
    store._SCHEMA_READY.clear()
    store.init_db()

    got = profiles.learn_from_message("绝绝子就是非常厉害的意思", "qq_group:9")
    assert got and got[0]["term"] == "绝绝子"
    assert "非常厉害" in got[0]["meaning"]
    block = profiles.lexicon_block("qq_group:9")
    assert "绝绝子" in block


def test_lexicon_ignores_plain_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "p4.db"))
    from core import store
    store._SCHEMA_READY.clear()
    store.init_db()

    assert profiles.learn_from_message("今天天气不错啊", "qq_group:9") == []


def test_lexicon_hits_increment(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "p5.db"))
    from core import store
    store._SCHEMA_READY.clear()
    store.init_db()

    profiles.add_lexicon("g", "yyds", "永远的神")
    profiles.add_lexicon("g", "yyds", "")
    items = profiles.list_lexicon("g")
    assert items[0]["term"] == "yyds" and items[0]["hits"] == 2
    assert items[0]["meaning"] == "永远的神", "重复记录不能把已有释义清空"


def test_profiles_api_roundtrip(client):
    r = client.put("/api/profiles/qq:42", json={
        "kind": "user", "display_name": "老王", "tags": ["话痨"], "note": "喜欢聊车"})
    assert r.status_code == 200, r.text
    listed = client.get("/api/profiles?kind=user").json()["profiles"]
    assert any(p["key"] == "qq:42" for p in listed)

    add = client.post("/api/lexicon", json={
        "scope": "qq_group:9", "term": "上头", "meaning": "很带感"})
    assert add.status_code == 200
    entries = client.get("/api/lexicon?scope=qq_group:9").json()["entries"]
    assert any(e["term"] == "上头" for e in entries)


def test_pipeline_auto_creates_profile(monkeypatch, tmp_path):
    """每轮对话都要给用户/群自动建档并累加消息数：只读不写等于没建档。"""
    import asyncio

    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "auto.db"))
    from core import config as config_mod
    from core import pipeline, store
    from core.pipeline import ChannelMessage

    store._SCHEMA_READY.clear()
    store.init_db()
    cfg = config_mod.get_config()
    cfg["engine"] = {"mode": "rule"}
    monkeypatch.setattr(config_mod, "get_config", lambda: cfg)

    msg = ChannelMessage(text="你好", channel_type="qq_group",
                         channel_user_id="555", group_id="777",
                         display_name="阿强", group_name="测试群")
    asyncio.run(pipeline.run(msg, [], "qq:555"))
    asyncio.run(pipeline.run(msg, [], "qq:555"))

    u = profiles.get_profile("qq:555")
    g = profiles.get_profile("qq_group:777")
    assert u is not None and u["stats"]["messages"] == 2
    assert g is not None and g["stats"]["messages"] == 2
    assert u["aliases"] == ["阿强"]
