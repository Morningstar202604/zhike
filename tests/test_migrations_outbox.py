"""回归：迁移机制 / outbox / 数据保留期。

这些测试都经过变异验证：删掉实现里的关键逻辑，测试必须失败。
"""
import os
import sqlite3

import pytest

from core import migrations, store


# ---------------- 迁移机制 ----------------

def test_fresh_db_reaches_latest_version(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "fresh.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    assert store.schema_version() == migrations.LATEST_VERSION
    assert migrations.LATEST_VERSION >= 6


def test_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "idem.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    v1 = store.schema_version()
    store.init_db()
    store.init_db()
    assert store.schema_version() == v1, "重复启动不应重复迁移"


def test_migration_preserves_existing_data(tmp_path, monkeypatch):
    """老库升级不能丢数据——这是迁移最基本的承诺。"""
    db = tmp_path / "old.db"
    monkeypatch.setenv("CS_AGENT_DB", str(db))
    store._SCHEMA_READY.clear()
    store.init_db()
    store.create_session("keep-me", "visitor-1")
    store.add_message("keep-me", "visitor", "升级前写入的消息")

    # 模拟回退版本号，再升级一次
    with store.get_conn() as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    store.init_db()

    assert store.schema_version() == migrations.LATEST_VERSION
    msgs = store.get_messages("keep-me")
    assert len(msgs) == 1 and "升级前" in msgs[0]["content"]


def test_migration_failure_rolls_back(tmp_path, monkeypatch):
    """迁移中途失败必须回滚，不允许半迁移状态。"""
    db = tmp_path / "fail.db"
    monkeypatch.setenv("CS_AGENT_DB", str(db))
    store._SCHEMA_READY.clear()
    store.init_db()

    def boom(conn):
        raise RuntimeError("故意失败")

    # 插入一个必失败的迁移
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [(99, "坏迁移", boom)])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 99)

    with store.get_conn() as conn:
        conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
        result = migrations.migrate(conn)

    assert result["ok"] is False
    assert "故意失败" in result["error"]
    assert result.get("rolled_back") is True, "迁移失败必须显式回滚（不能依赖连接关闭的隐式行为）"
    # 关键断言：版本号没有前进。无论是显式 rollback 还是连接关闭时隐式回滚，
    # 结果都必须一致——这里验证的是「结果」，不是「实现手段」。
    with store.get_conn() as conn:
        assert migrations.current_version(conn) == 6, "失败后版本号不能前进"


def test_migration_creates_outbox_table(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "ob.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    with store.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outbox'"
        ).fetchone()
    assert row is not None


def test_schema_indexes_created(tmp_path, monkeypatch):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "idx.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    with store.get_conn() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
    assert "idx_audit_chain_actor" in names, "迁移 0002 的索引缺失"
    assert "idx_action_log_action_time" in names, "迁移 0003 的索引缺失"


# ---------------- outbox ----------------

def test_outbox_put_and_claim(client):
    eid = store.outbox_put("approval.pending", {"approval_id": "ap1"})
    assert eid > 0
    claimed = store.outbox_claim()
    assert len(claimed) == 1
    assert claimed[0]["topic"] == "approval.pending"
    assert claimed[0]["payload"]["approval_id"] == "ap1"


def test_outbox_claim_is_exclusive(client):
    """认领后不能被第二个消费者再拿到——至少一次投递不能变成重复投递。"""
    store.outbox_put("action.requested", {"n": 1})
    first = store.outbox_claim()
    second = store.outbox_claim()
    assert len(first) == 1
    assert second == [], "同一条事件不能被认领两次"


def test_outbox_done_removes_from_pending(client):
    eid = store.outbox_put("action.finished", {})
    store.outbox_claim()
    store.outbox_done(eid)
    assert store.outbox_stats().get("done") == 1
    assert store.outbox_claim() == []


def test_outbox_fail_retries_then_gives_up(client):
    eid = store.outbox_put("reply.outbound", {})
    store.outbox_claim()
    # 前 4 次失败应回到 pending
    for _ in range(4):
        store.outbox_fail(eid, "boom", retry_in_s=0, max_attempts=5)
        store.outbox_claim()
    store.outbox_fail(eid, "boom", retry_in_s=0, max_attempts=5)
    stats = store.outbox_stats()
    assert stats.get("failed") == 1, "超过重试上限应标记 failed"


def test_outbox_payload_is_json_roundtrip(client):
    payload = {"nested": {"a": [1, 2, 3]}, "中文": "值"}
    store.outbox_put("test.topic", payload)
    got = store.outbox_claim()[0]["payload"]
    assert got == payload


# ---------------- 数据保留 ----------------

def test_purge_removes_old_action_logs(client):
    store.action_log_create("old-1", "get_login_info", 0, {}, "admin", 1, None)
    store.action_log_update("old-1", status="executed")
    with store.get_conn() as conn:
        conn.execute("UPDATE action_log SET created_at = '2000-01-01T00:00:00+00:00' WHERE id='old-1'")

    removed = store.purge_old_data(days=30)
    assert removed["action_log"] == 1
    assert store.action_log_get("old-1") is None


def test_purge_never_deletes_audit_chain(client):
    """哈希链必须完整——清理绝不能动 audit_chain。

    注意：不改 ts。ts 参与哈希计算，改了链就真的断了（那是另一条独立的正确行为）。
    这里只验证「清理不删除审计行、也不弄坏链」。
    """
    store.append_audit("admin", "test.event", {"x": 1})
    before = store.verify_audit_chain()["checked"]
    assert before >= 1

    # 把审计记录的时间改到很久以前也没用：ts 参与哈希，改了链就断。
    # 换个可靠办法——直接断言 purge 的 SQL 不包含 audit_chain。
    import inspect

    src = inspect.getsource(store.purge_old_data)
    assert "audit_chain" not in src.replace("审计链 audit_chain 永不删除", ""),         "清理函数不得对 audit_chain 执行任何删除"

    store.purge_old_data(days=1)
    after = store.verify_audit_chain()["checked"]
    assert after == before, "审计链不允许被清理"
    assert store.verify_audit_chain()["ok"] is True, "清理不得破坏哈希链"


def test_purge_keeps_pending_actions(client):
    """未完成/待审批的动作不能删——删了审批就断了。"""
    store.action_log_create("pending-1", "set_group_kick", 2, {}, "admin", 3, None)
    store.action_log_update("pending-1", status="pending_approval")
    with store.get_conn() as conn:
        conn.execute("UPDATE action_log SET created_at = '2000-01-01T00:00:00+00:00' WHERE id='pending-1'")

    store.purge_old_data(days=1)
    assert store.action_log_get("pending-1") is not None


def test_purge_rejects_invalid_days(client):
    with pytest.raises(ValueError):
        store.purge_old_data(days=0)


# ---------------- 管理接口 ----------------

def test_schema_endpoint(client):
    d = client.get("/api/admin/schema").json()
    assert d["schema_version"] == migrations.LATEST_VERSION
    assert d["tables"]["sessions"] >= 0


def test_purge_endpoint(client):
    r = client.post("/api/admin/purge?days=3650")
    assert r.status_code == 200
    assert "removed" in r.json()
    # 清理动作本身要进审计
    entries = client.get("/api/admin/audit/entries").json()["entries"]
    assert any(e["kind"] == "data.purge" for e in entries)


def test_purge_endpoint_rejects_bad_days(client):
    assert client.post("/api/admin/purge?days=0").status_code == 400


def test_outbox_endpoint(client):
    store.outbox_put("test.topic", {})
    d = client.get("/api/admin/outbox").json()
    assert "stats" in d and d["stats"].get("pending", 0) >= 1
