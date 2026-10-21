"""回归：横向审计发现的 N1-N11 健壮性缺陷。"""
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from core import events_log, pipeline, ratelimit, retrieval, store
from tests.conftest import ADMIN, create_web_session, reset_web_session_auth


# ---------- N1 日志 handler 幂等 ----------

def test_events_log_attach_is_idempotent():
    before = len(logging.getLogger("cs.events").handlers)
    for _ in range(5):
        events_log.attach()
    after = len(logging.getLogger("cs.events").handlers)
    assert after <= before + 1, f"attach 重复挂载泄漏 handler：{before} -> {after}"


def test_events_log_no_duplicate_entries():
    """同一事件只应记录一次（此前每次 create_app 都会多挂一个 handler）。"""
    events_log.attach()
    events_log.attach()
    ev = logging.getLogger("cs.events")
    marker = f"dup-check-{os.getpid()}"
    ev.warning(marker)
    hits = [e for e in events_log.entries(limit=200) if marker in e["message"]]
    assert len(hits) == 1, f"同一条日志被记录了 {len(hits)} 次"


# ---------- N2 向量 rowid 稳定 ----------

def test_kb_rowid_is_stable_across_processes():
    """rowid 必须跨进程一致：用内置 hash 会因 PYTHONHASHSEED 随机化而错位。"""
    code = ("import sys; sys.path.insert(0, %r);"
            "from core.retrieval import _kb_rowid; print(_kb_rowid('KB001'))" % os.getcwd())
    outs = set()
    for _ in range(3):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={**os.environ, "PYTHONHASHSEED": "random"})
        outs.add(r.stdout.strip())
    assert len(outs) == 1, f"跨进程 rowid 不一致：{outs}"


def test_kb_rowid_distinct_for_different_ids():
    assert retrieval._kb_rowid("KB001") != retrieval._kb_rowid("KB002")
    assert retrieval._kb_rowid("KB001") >= 0


# ---------- N3 流式 reply 为 None ----------

def test_stream_survives_pipeline_without_final(client, monkeypatch):
    """run_stream 不产出 final 时不能 500，要返回可读兜底。"""
    async def broken_stream(*a, **kw):
        yield ("delta", "半句")
        # 故意不产出 final

    monkeypatch.setattr(pipeline, "run_stream", broken_stream)
    sid, _ = create_web_session(client, "n3-stream")
    r = client.post(f"/api/sessions/{sid}/chat/stream", json={"content": "在吗"})
    assert r.status_code == 200, r.text
    assert "event: done" in r.text
    # 必须按事件名取 done 块：ack 事件也带 message_id 字段，按子串匹配会取错行
    done_block = [blk for blk in r.text.split("\n\n") if blk.startswith("event: done")][0]
    done = [ln for ln in done_block.split("\n") if ln.startswith("data: ")][0]
    payload = json.loads(done[6:])
    assert payload["text"], "必须有兜底文案，不能是空字符串"
    assert payload["escalate"] is True, "管线异常应转人工"


# ---------- N4/N9 限流 ----------

def test_rate_limiter_has_ts_index():
    limiter = ratelimit.SQLiteRateLimiter()
    with store.get_conn() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM rate_hits WHERE ts < ?", (0,)).fetchall()
    detail = " ".join(str(tuple(r)) for r in plan)
    assert "idx_rate_hits_ts" in detail, f"过期清理未走 ts 索引：{detail}"


def test_rate_limiter_enforces_limit_under_concurrency():
    limiter = ratelimit.SQLiteRateLimiter()
    allowed = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            if limiter.allow("n9-key", window_s=60, max_n=5):
                with lock:
                    allowed.append(1)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(allowed) == 5, f"上限 5 实际放行 {len(allowed)}"


# ---------- N5 全局异常兜底 ----------

def test_unhandled_exception_returns_json_not_traceback(monkeypatch):
    """全局兜底：未预料的异常返回结构化 JSON + 落日志，不把 traceback 抛给客户端。"""
    from fastapi.testclient import TestClient
    from core.main import create_app
    from core import retrieval as r

    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("CHANNEL_TOKEN", "chan")

    def boom(*a, **kw):
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(r, "load_kb", boom)
    app = create_app()
    # raise_server_exceptions=False：只有关掉它才能观察到真实的 500 响应
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/kb", headers={"X-Admin-Token": ADMIN})
    assert resp.status_code == 500
    assert "detail" in resp.json()
    assert "Traceback" not in resp.text


# ---------- N6 pipeline 异常降级 ----------

def test_pipeline_exception_becomes_handoff_not_500(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(pipeline, "run", boom)
    sid, _ = create_web_session(client, "n6-fail")
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "怎么开发票"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] and body["escalate"] is True
    assert body["escalate_reason"] == "pipeline_error"
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["visitor", "agent"], "访客消息与兜底回复都要落库"


# ---------- N8 索引签名跨进程 ----------

def test_index_signature_persisted_in_db(monkeypatch, tmp_path):
    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps({"kb": [{"id": "KB1", "title": "t", "text": "x", "answer": "a", "tags": []}]}),
                  encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb)
    conn = retrieval._ensure_conn()
    assert conn is not None
    retrieval._INDEX_STATE["sig"] = ""   # 模拟新进程：内存里没有签名
    retrieval.rebuild_index_if_needed(conn, retrieval.load_kb())
    stored = retrieval._read_sig(conn)
    assert stored, "签名必须落库，否则多 worker 会互相重建索引"
    assert str(retrieval.TOKENIZER_VERSION) in stored
    conn.close()


# ---------- N10 体积护栏 ----------

def test_oversized_text_rejected(client):
    sid, _ = create_web_session(client, "n10-text")
    big = "啊" * 30000
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": big})
    assert r.status_code == 413, r.text


def test_oversized_image_rejected(client):
    sid, _ = create_web_session(client, "n10-img")
    r = client.post(f"/api/sessions/{sid}/messages",
                    json={"content": "看图", "image": "A" * 9_000_000})
    assert r.status_code == 413, r.text


def test_oversized_ingest_rejected(client):
    r = client.post("/v1/ingest", json={
        "channel_type": "qq_group", "channel_user_id": "big",
        "group_id": "big-g", "text": "啊" * 30000,
    })
    assert r.status_code == 413, r.text


def test_kb_import_item_limit(client, monkeypatch, tmp_path):
    """导入体积上限。必须用临时 KB：否则会往仓库真实知识库里写测试条目。"""
    from core import main as main_mod

    kb_file = tmp_path / "kb.json"
    kb_file.write_text(json.dumps({"kb": []}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb_file)
    retrieval._KB_CACHE = None
    monkeypatch.setattr(main_mod, "MAX_KB_IMPORT_ITEMS", 3)

    text = "\n\n".join(f"标题{i}\n内容{i}" for i in range(6))
    r = client.post("/api/kb/import", json={"text": text})
    assert r.status_code == 413, r.text
    # 超限请求不应写入任何条目
    assert json.loads(kb_file.read_text(encoding="utf-8"))["kb"] == []
    retrieval._KB_CACHE = None


# ---------- N11 WS 清理 ----------

def test_ws_registry_cleanup_on_disconnect(client):
    """正常断开后，注册表里不应残留连接。"""
    from core.main import hub

    sid, secret = create_web_session(client, "n11-ws")
    reset_web_session_auth(client)
    with client.websocket_connect(f"/ws/{sid}?secret={secret}") as ws:
        assert sum(len(v) for v in hub.visitor_conns.values()) >= 1
    assert sum(len(v) for v in hub.visitor_conns.values()) == 0, "断开后连接未被清理"
