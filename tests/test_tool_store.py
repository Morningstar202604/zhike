import pytest

from core import store


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "tools.db"))
    store._SCHEMA_READY.clear()
    store.init_db()


def test_tool_store_path_isolation(monkeypatch, tmp_path):
    from core import tool_store

    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    monkeypatch.setenv("CS_AGENT_DB", str(first))
    store._SCHEMA_READY.clear()
    store.init_db()
    tool_store.upsert_tool("only-first", "first", "first", {"topic": "string"})
    assert any(t["id"] == "only-first" for t in tool_store.active_tools())

    monkeypatch.setenv("CS_AGENT_DB", str(second))
    store._SCHEMA_READY.clear()
    store.init_db()
    assert all(t["id"] != "only-first" for t in tool_store.active_tools())


def test_tool_store_returns_deep_copies():
    from core import tool_store

    tool_store.upsert_tool("nested", "nested", "nested", {"payload": {"items": [1]}})
    tool_store.active_tools()
    second = tool_store.active_tools()
    second_tool = next(t for t in second if t["id"] == "nested")
    second_tool["args_json"]["payload"]["items"].append(2)

    third_tool = next(t for t in tool_store.active_tools() if t["id"] == "nested")
    assert third_tool["args_json"] == {"payload": {"items": [1]}}


def test_tool_store_mutations_bump_only_on_real_changes():
    from core import tool_store

    before = tool_store.tools_version()
    tool_store.upsert_tool("versioned", "versioned", "versioned", {})
    after_upsert = tool_store.tools_version()
    assert after_upsert > before

    tool_store.set_tool_enabled("versioned", False)
    after_disable = tool_store.tools_version()
    assert after_disable > after_upsert
    tool_store.set_tool_enabled("versioned", False)
    assert tool_store.tools_version() == after_disable
    tool_store.set_tool_enabled("missing", False)
    assert tool_store.tools_version() == after_disable

    assert tool_store.delete_tool("versioned") is True
    after_delete = tool_store.tools_version()
    assert after_delete > after_disable
    assert tool_store.delete_tool("versioned") is False
    assert tool_store.tools_version() == after_delete


def test_store_tool_api_remains_compatible():
    from core import tool_store

    tool_store.upsert_tool("compat-tool", "compat", "compat", {"x": "string"})
    assert store.get_tool("compat-tool") == tool_store.get_tool("compat-tool")
    assert store.list_tools() == tool_store.list_tools()
    assert store.active_tools() == tool_store.active_tools()
