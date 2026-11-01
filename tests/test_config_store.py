import pytest

from core import store


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "config.db"))
    store._SCHEMA_READY.clear()
    store.init_db()


def test_config_store_roundtrip_and_shallow_merge():
    from core import config_store

    config_store.set_config({"retrieval": {"kb_top_k": 7, "min_overlap": 0.2}})
    config_store.set_config({"retrieval": {"kb_top_k": 9}})

    assert config_store.get_config() == {"retrieval": {"kb_top_k": 9, "min_overlap": 0.2}}


def test_config_store_version_increments():
    from core import config_store

    before = config_store.config_version()
    config_store.set_config({"engine": {"mode": "rule"}})

    assert config_store.config_version() > before


def test_store_configuration_api_remains_compatible():
    from core import config_store

    config_store.set_config({"engine": {"mode": "llm"}})

    assert store.get_config() == config_store.get_config()
    assert store.config_version() == config_store.config_version()
