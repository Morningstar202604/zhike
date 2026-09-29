from fastapi.testclient import TestClient

from core.security import RateLimiter


def test_admin_token_required(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "abc")
    from core.main import create_app
    from core import store

    store.init_db()
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/operator/queues").status_code == 401
        assert c.get("/api/operator/queues", headers={"X-Admin-Token": "abc"}).status_code == 200
        assert c.get("/api/admin/overview", headers={"X-Admin-Token": "abc"}).status_code == 200


def test_admin_open_mode_warns(caplog, monkeypatch, tmp_path):
    import logging

    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "t2.db"))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from core.main import create_app
    from core import store

    store.init_db()
    app = create_app()
    with caplog.at_level(logging.WARNING, logger="core"):
        with TestClient(app) as c:
            assert c.get("/api/operator/queues").status_code == 200
    assert any("ADMIN_TOKEN" in r.message for r in caplog.records)


def test_rate_limiter_window():
    import time

    limiter = RateLimiter()
    assert limiter.allow("k", window_s=0.1, max_n=1) is True
    assert limiter.allow("k", window_s=0.1, max_n=1) is False
    assert limiter.allow("other", window_s=0.1, max_n=1) is True
    time.sleep(0.15)
    assert limiter.allow("k", window_s=0.1, max_n=1) is True
