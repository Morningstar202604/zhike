import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("USER_LLM_API_KEY", raising=False)
    from core.main import create_app
    from core import store

    store.init_db()
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c
