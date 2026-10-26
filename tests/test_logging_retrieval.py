"""可观测性回归：日志落盘、检索模式可见、令牌不落日志。

Windows 注意：打开的 SQLite 连接与 file handler 会锁住文件，pytest 清理
tmp_path 时报 WinError 32。所以用例结束必须显式关闭自己打开的资源。
"""
import logging
import pathlib

import pytest


def test_events_log_writes_to_file(tmp_path, monkeypatch):
    """配了 CS_AGENT_LOG_FILE 就必须真落盘：否则重启后历史告警无从追溯。"""
    log_file = tmp_path / "app.log"
    monkeypatch.setenv("CS_AGENT_LOG_FILE", str(log_file))
    from core import events_log

    previous = events_log._FILE_HANDLER
    for name in ("cs.events", "core"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            if isinstance(h, logging.FileHandler):
                lg.removeHandler(h)
                h.close()
    events_log._FILE_HANDLER = None
    try:
        events_log.attach()
        logging.getLogger("cs.events").warning("probe-file-line")
        handler = events_log._FILE_HANDLER
        if handler is not None:
            handler.flush()
        assert log_file.exists()
        assert "probe-file-line" in log_file.read_text(encoding="utf-8")
    finally:
        for name in ("cs.events", "core"):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                if isinstance(h, logging.FileHandler):
                    lg.removeHandler(h)
                    h.close()
        events_log._FILE_HANDLER = previous


def test_retrieval_mode_reports_actual_backend(tmp_path, monkeypatch):
    from core import retrieval

    info = retrieval.mode()
    assert info["mode"] in ("hybrid", "fts5", "bigram")
    assert isinstance(info["fts5"], bool) and isinstance(info["vector"], bool)

    import importlib.util
    if not importlib.util.find_spec("sqlite_vec"):
        pytest.skip("sqlite-vec 未安装，跳过向量后端断言")

    monkeypatch.setattr(retrieval, "RETRIEVAL_DB", tmp_path / "r.db")
    retrieval._INDEX_STATE.update({"sig": "", "fts5": False, "vec": False})
    conn = retrieval._ensure_conn()
    try:
        assert conn is not None
        assert retrieval.mode()["vector"] is True
    finally:
        if conn is not None:
            conn.close()
        retrieval._RET_LOCAL.conn = None
        retrieval._INDEX_STATE.update({"sig": "", "fts5": False, "vec": False})


def test_health_exposes_retrieval_mode(client):
    body = client.get("/api/agent/health").json()
    assert "retrieval" in body
    assert body["retrieval"]["mode"] in ("hybrid", "fts5", "bigram")
    assert "log_file" in body


def test_no_token_in_access_log_pattern():
    """回归：admin_token 不允许出现在 URL 里（access log 会原样记下）。"""
    repo = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for p in list(repo.glob("core/**/*.py")) + list(repo.glob("frontend/src/**/*.ts*")):
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "admin_token=" in text:
            hits.append(str(p.relative_to(repo)))
    assert hits == [], f"URL 里带 admin_token 会把令牌写进访问日志：{hits}"


def test_file_path_reports_actual_path(tmp_path, monkeypatch):
    """health 的 log_path 必须反映真实落盘位置，不能只看环境变量。"""
    log_file = tmp_path / "custom.log"
    monkeypatch.setenv("CS_AGENT_LOG_FILE", str(log_file))
    from core import events_log

    previous = events_log._FILE_HANDLER
    events_log._FILE_HANDLER = None
    try:
        events_log.attach()
        assert events_log.file_path() == str(log_file)
    finally:
        h = events_log._FILE_HANDLER
        if h is not None:
            import logging as _lg
            for name in ("cs.events", "core"):
                lg = _lg.getLogger(name)
                if h in lg.handlers:
                    lg.removeHandler(h)
            h.close()
        events_log._FILE_HANDLER = previous


def test_file_path_empty_when_disabled(monkeypatch):
    monkeypatch.setenv("CS_AGENT_LOG_FILE", "off")
    from core import events_log

    previous = events_log._FILE_HANDLER
    events_log._FILE_HANDLER = None
    try:
        events_log.attach()
        assert events_log.file_path() == ""
    finally:
        events_log._FILE_HANDLER = previous
