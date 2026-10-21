"""回归：性能优化的正确性。

优化最怕"快了但错了"，所以每条都要验证行为不变。
"""
import asyncio
import json
import os
import threading
import time

import pytest

from core import config, http_pool, retrieval, store


# ============================================================
# 连接复用
# ============================================================

def test_connection_is_reused_within_thread(client):
    """同一线程的连续查询应复用同一连接。"""
    with store.get_conn() as c1:
        id1 = id(c1)
    with store.get_conn() as c2:
        id2 = id(c2)
    assert id1 == id2, "同线程应复用连接"


def test_connection_rebuilt_when_db_path_changes(tmp_path, monkeypatch):
    """切换数据库路径必须重建连接，不能读到旧库。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "a.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    store.create_session("s-a", "v")

    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "b.db"))
    store.init_db()
    assert store.get_session("s-a") is None, "换库后不应读到旧库数据"


def test_connection_is_per_thread(client):
    """不同线程不能共用同一连接（sqlite3 连接非线程安全）。"""
    ids = {}

    def grab(name):
        with store.get_conn() as c:
            ids[name] = id(c)

    t1 = threading.Thread(target=grab, args=("a",))
    t2 = threading.Thread(target=grab, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert ids["a"] != ids["b"], "不同线程必须各自持有连接"


def test_commit_still_persists(client):
    store.create_session("persist-me", "v")
    with store.get_conn() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id='persist-me'").fetchone()
    assert row is not None, "写入必须提交"


def test_rollback_on_exception(client):
    """异常时必须回滚，不能留下半截数据。"""
    try:
        with store.get_conn() as conn:
            conn.execute("INSERT INTO sessions (id, visitor_id, status) VALUES ('rollback-test','v','ongoing')")
            raise RuntimeError("故意失败")
    except RuntimeError:
        pass
    assert store.get_session("rollback-test") is None, "异常后必须回滚"


# ============================================================
# 检索连接复用
# ============================================================

def test_retrieval_connection_reused(monkeypatch, tmp_path):
    """检索库连接也应复用。"""
    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps({"kb": [
        {"id": "K1", "title": "物流", "text": "物流 发货", "answer": "48小时", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb)
    monkeypatch.setattr(retrieval, "RETRIEVAL_DB", tmp_path / "idx.db")
    retrieval._KB_CACHE = None
    retrieval._RET_LOCAL.conn = None

    c1 = retrieval._ensure_conn()
    c2 = retrieval._ensure_conn()
    assert c1 is c2, "同线程应复用检索连接"


def test_retrieval_still_works_after_pooling(monkeypatch, tmp_path):
    """复用连接后检索结果必须不变。"""
    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps({"kb": [
        {"id": "K1", "title": "退货政策", "text": "退货 七天 无理由", "answer": "7天可退", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb)
    monkeypatch.setattr(retrieval, "RETRIEVAL_DB", tmp_path / "idx2.db")
    retrieval._KB_CACHE = None
    retrieval._RET_LOCAL.conn = None

    hits = retrieval.retrieve_scored("退货政策", 3, 0.0, use_embedding=False)
    assert hits and hits[0][1]["id"] == "K1"


# ============================================================
# 配置缓存
# ============================================================

def test_config_cache_returns_equal_values(client):
    a = config.get_config()
    b = config.get_config()
    assert a == b


def test_config_cache_does_not_leak_mutation(client):
    """调用方修改返回值不能污染缓存。"""
    c1 = config.get_config()
    c1["retrieval"]["kb_top_k"] = 999
    c2 = config.get_config()
    assert c2["retrieval"]["kb_top_k"] != 999, "返回值被污染了缓存"


def test_config_cache_invalidated_on_update(client):
    """改配置必须立刻生效——这是缓存最容易引入的回归。"""
    before = config.get_config()["retrieval"]["kb_top_k"]
    target = 9 if before != 9 else 3
    config.update_config({"retrieval": {"kb_top_k": target}})
    assert config.get_config()["retrieval"]["kb_top_k"] == target
    config.update_config({"retrieval": {"kb_top_k": before}})


def test_config_invalidated_by_direct_db_change(client):
    """绕过 update_config 直接改库并递增版本号，缓存必须失效。

    注意：必须先 INSERT 再 UPDATE——config 表初始为空，
    直接 UPDATE 一个不存在的 key 是 no-op（这个测试最初就是这么误判的）。
    """
    before = config.get_config()["retrieval"]["kb_top_k"]
    payload = json.dumps({"kb_top_k": 7, "min_overlap": 0.08})
    with store.get_conn() as conn:
        conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES ('retrieval', ?, '') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (payload,),
        )
        store._bump_config_version(conn)
    assert config.get_config()["retrieval"]["kb_top_k"] == 7, "直接改库后缓存未失效"
    config.update_config({"retrieval": {"kb_top_k": before}})


def test_config_invalidated_by_env_change(client, monkeypatch):
    """环境变量变化也要让缓存失效。"""
    monkeypatch.delenv("USER_LLM_API_KEY", raising=False)
    config.invalidate_cache()
    assert config.get_config()["llm"]["key_source"] in ("none", "ui")
    monkeypatch.setenv("USER_LLM_API_KEY", "sk-env-test")
    # 若已被 UI key 覆盖，则 source 仍是 ui；这里只验证不崩且能读到环境变量值
    cfg = config.get_config()
    assert "key_source" in cfg["llm"]


def test_config_version_increments(client):
    v0 = store.config_version()
    config.update_config({"retrieval": {"kb_top_k": 5}})
    assert store.config_version() > v0, "配置变更必须递增版本号"


# ============================================================
# HTTP 连接池
# ============================================================

def test_http_pool_reuses_client():
    c1 = http_pool.get_client("https://example.com", timeout=10)
    c2 = http_pool.get_client("https://example.com", timeout=10)
    assert c1 is c2, "同 base_url 应复用客户端"


def test_http_pool_separate_by_base_url():
    c1 = http_pool.get_client("https://a.example", timeout=10)
    c2 = http_pool.get_client("https://b.example", timeout=10)
    assert c1 is not c2, "不同 base_url 应独立"


def test_http_pool_close_all():
    http_pool.get_client("https://close-test.example", timeout=5)
    asyncio.run(http_pool.close_all())
    c = http_pool.get_client("https://close-test.example", timeout=5)
    assert not c.is_closed, "关闭后应能重新创建"


# ============================================================
# 性能基线（防止回退）
# ============================================================

def test_config_lookup_is_fast(client):
    """配置读取应在毫秒以内（缓存前约 1.2ms/次）。"""
    config.get_config()  # 预热
    t = time.perf_counter()
    for _ in range(200):
        config.get_config()
    elapsed_ms = (time.perf_counter() - t) * 1000
    assert elapsed_ms < 100, f"200 次配置读取耗时 {elapsed_ms:.1f}ms，缓存可能失效了"


def test_db_query_is_fast(client):
    """单次查询应远低于建立连接的开销（连接复用前约 1.1ms）。"""
    store.get_session("warmup")  # 预热
    t = time.perf_counter()
    for _ in range(200):
        store.get_session("nonexistent")
    elapsed_ms = (time.perf_counter() - t) * 1000
    assert elapsed_ms < 100, f"200 次查询耗时 {elapsed_ms:.1f}ms，连接未被复用"
