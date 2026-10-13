"""回归：P1（报表聚合 / 坐席答案回流）+ P2（查询改写 / 多模型备援）。"""
from tests.conftest import create_web_session
import asyncio
import json

import pytest

from core import llm as llm_mod, pipeline, store


async def _collect(agen):
    return [x async for x in agen]


def test_admin_report_totals(client, monkeypatch, tmp_path):
    sid = create_web_session(client, "rep-v1")[0]
    client.post(f"/api/sessions/{sid}/messages", json={"content": "退货政策是什么"})
    store.add_feedback(sid, None, "helpful")
    r = client.get("/api/admin/report?days=7")
    assert r.status_code == 200
    d = r.json()
    assert len(d["daily"]) == 7
    t = d["totals"]
    assert t["sessions"] >= 1 and t["agent_replies"] >= 1
    assert t["kb_hit_rate"] is not None and t["csat"] is not None and t["csat_n"] >= 1
    assert all("day" in row for row in d["daily"])


def test_kb_save_note_allocates_id_and_tag(client, monkeypatch, tmp_path):
    """存 KB 测试用临时知识库文件，避免污染仓库真实 KB。"""
    from core import retrieval

    kb_file = tmp_path / "kb.json"
    kb_file.write_text(json.dumps({"kb": [
        {"id": "KB001", "title": "种子", "text": "种子", "answer": "种子", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb_file)
    retrieval._KB_CACHE = None
    r = client.post("/api/kb/save-note", json={"text": "夜间客服电话 400-000-0000"})
    assert r.status_code == 200
    d = r.json()
    assert d["entry"]["id"] == "KB101" and "坐席回流" in d["entry"]["tags"] and d["total"] == 2
    saved = retrieval.load_kb()[-1]
    assert saved["answer"].startswith("夜间客服电话")
    retrieval._KB_CACHE = None


def test_kb_save_note_empty_rejected(client):
    assert client.post("/api/kb/save-note", json={"text": "  "}).status_code == 400


def test_query_rewrite_uses_last_topic(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "rw.db"))
    from core import store as store_mod

    store_mod.init_db()
    store_mod.set_memory("rw-v", "last_topic", "发票如何开具")
    captured = {}

    def fake_retrieve(query, top_k, min_overlap, use_embedding=None):
        captured["q"] = query
        return []

    from core import retrieval
    monkeypatch.setattr(retrieval, "retrieve_scored", fake_retrieve)
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "get_config", lambda: {
        "retrieval": {"kb_top_k": 3, "min_overlap": 0.0, "embedding": "off"},
        "context": {"modules": {}, "char_budget": 2400},
        "memory": {},
    })
    pipeline.build_context("它怎么弄", [], config_mod.get_config(), "rw-v")
    assert "发票如何开具" in captured["q"], "短指代问句必须用最近话题补全检索"
    # 正常长度且无指代的问句不改写
    pipeline.build_context("退货政策具体是什么内容", [], config_mod.get_config(), "rw-v")
    assert captured["q"] == "退货政策具体是什么内容"


def test_react_fallback_model_engaged(monkeypatch, tmp_path):
    from core import docparse  # noqa: F401  保持模块加载一致性

    calls = []

    async def fake_post_chat(base, key, payload):
        calls.append(payload["model"])
        if payload["model"] == "primary-model":
            raise llm_mod.httpx.HTTPError("boom")
        return '{"answer": "备援回复", "confidence": 0.9, "escalate": false}'

    monkeypatch.setattr(llm_mod, "_post_chat", fake_post_chat)
    cfg = {
        "llm": {"has_key": True, "api_key": "k", "base_url": "http://x", "model": "primary-model",
                "fallback_model": "backup-model", "temperature": 0.2, "top_p": 1.0, "max_tokens": 512},
        "retrieval": {"kb_top_k": 3, "min_overlap": 0.0, "embedding": "off"},
        "context": {"char_budget": 2400, "modules": {}},
        "escalation": {"confidence_threshold": 0.6},
    }
    ctx = {"persona": "p", "modules_on": {}, "kb_block": "", "tools_schema": [], "memory_block": "",
           "summary": "", "history": [], "tools": {}, "used": {}}
    data = asyncio.run(llm_mod.react("你好", ctx, cfg))
    assert data["answer"] == "备援回复"
    assert calls == ["primary-model", "backup-model"]


def test_run_stream_silent_failure_retries_non_stream(monkeypatch, tmp_path):
    """流式空手而归（未发 delta）→ 走非流式兜底；已发过 delta 则直接回规则，不重复请求。"""
    store_mod = store
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "sf.db"))
    store_mod.init_db()
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "get_config", lambda: {
        "engine": {"mode": "llm"},
        "llm": {"has_key": True, "api_key": "k", "base_url": "http://x", "model": "m",
                "temperature": 0.2, "top_p": 1.0, "max_tokens": 512},
        "retrieval": {"kb_top_k": 3, "min_overlap": 0.0, "embedding": "off"},
        "context": {"char_budget": 2400, "modules": {}},
        "escalation": {"confidence_threshold": 0.6, "max_message_len": 500},
        "guardrail": {"injection_block": True},
        "multimodal": {"enable": True, "vision_model": ""},
    })

    async def dead_stream(query, ctx, cfg, image_b64=""):
        yield {"type": "error", "error": "down"}

    monkeypatch.setattr(llm_mod, "react_stream", dead_stream)

    async def ok_react(query, ctx, cfg, image_b64=""):
        return {"answer": "非流式兜底成功", "confidence": 0.9, "escalate": False}

    monkeypatch.setattr(llm_mod, "react", ok_react)
    events = asyncio.run(_collect(pipeline.run_stream(pipeline.ChannelMessage(text="发货多久"), [], "sf-v")))
    kinds = [k for k, _ in events]
    final = events[-1][1]
    assert final.mode == "llm_react" and final.text == "非流式兜底成功"
    assert "delta" in kinds
