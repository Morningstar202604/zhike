"""回归：Agent 三能力 — 流式管线 / 视觉多模态 / 混合检索 / SSE 端到端。"""
from tests.conftest import create_web_session, reset_web_session_auth
import asyncio
import json

import pytest

from core import llm as llm_mod, pipeline, retrieval


def _init_store(monkeypatch, tmp_path):
    """直接跑管线的用例需要隔离 DB（build_context 会读访客记忆）。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "cap.db"))
    from core import store

    store.init_db()


def _cfg(**over):
    base = {
        "engine": {"mode": "auto"},
        "llm": {"has_key": True, "api_key": "k", "base_url": "http://x", "model": "m", "temperature": 0.2, "top_p": 1.0, "max_tokens": 512},
        "retrieval": {"kb_top_k": 3, "min_overlap": 0.0, "embedding": "off"},
        "context": {"char_budget": 2400},
        "escalation": {"confidence_threshold": 0.6},
        "guardrail": {"injection_block": True},
        "multimodal": {"enable": True, "vision_model": ""},
    }
    base.update(over)
    return base


async def _collect(agen):
    return [x async for x in agen]


# ---------- 流式 answer 提取状态机 ----------

def test_scanner_json_deltas():
    s = llm_mod._AnswerStreamer()
    out = []
    for piece in ['{"thought": "查库", "answer": "你好', '，很高兴', '为您服务", "confidence": 0.9}']:
        out += s.feed(piece)
    assert "".join(out) == "你好，很高兴为您服务"
    assert s.done


def test_scanner_escapes_and_unicode():
    s = llm_mod._AnswerStreamer()
    out = s.feed('{"answer": "第一行\\n第二行 \\"引用\\" \\u4e2d\\u6587"}')
    assert "".join(out) == "第一行\n第二行 \"引用\" 中文"


def test_scanner_prose_passthrough():
    s = llm_mod._AnswerStreamer()
    out = s.feed("我是知客，您的智能在线客服。")
    assert "".join(out) == "我是知客，您的智能在线客服。"


def test_scanner_answer_not_first_key():
    s = llm_mod._AnswerStreamer()
    out = s.feed('{"citations": ["KB001"], "answer": "答案内容"}')
    assert "".join(out) == "答案内容"


# ---------- 流式管线 ----------

def test_run_stream_llm_deltas_and_final(monkeypatch, tmp_path):
    _init_store(monkeypatch, tmp_path)
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "get_config", lambda: _cfg())

    async def fake_stream(query, ctx, cfg, image_b64=""):
        yield {"type": "delta", "text": "你好，"}
        yield {"type": "delta", "text": "世界"}
        yield {"type": "final", "data": {"answer": "你好，世界", "citations": [], "confidence": 0.9, "escalate": False}}

    monkeypatch.setattr(llm_mod, "react_stream", fake_stream)
    events = asyncio.run(_collect(pipeline.run_stream(pipeline.ChannelMessage(text="在吗"), [], "v1")))
    kinds = [k for k, _ in events]
    # LLM 正常产出 final 时不应再补发一次 delta：
    # 补发会让前端把「流式半截 + 完整文本」拼起来重复显示（已修复）
    assert kinds == ["delta", "delta", "final"], f"实际事件序列：{kinds}"
    assert events[0][1] == "你好，" and events[1][1] == "世界"
    final = events[-1][1]
    assert final.text == "你好，世界" and final.mode == "llm_react"


def test_run_stream_llm_error_falls_back_to_rule(monkeypatch, tmp_path):
    _init_store(monkeypatch, tmp_path)
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "get_config", lambda: _cfg())

    async def fake_stream(query, ctx, cfg, image_b64=""):
        yield {"type": "delta", "text": "部分"}
        yield {"type": "error", "error": "boom"}

    monkeypatch.setattr(llm_mod, "react_stream", fake_stream)
    events = asyncio.run(_collect(pipeline.run_stream(pipeline.ChannelMessage(text="发货多久"), [], "v2")))
    final = events[-1][1]
    assert final.mode == "rule_react" and final.text  # auto 模式下 LLM 失败静默回退规则


def test_run_stream_llm_mode_error_marks_fallback(monkeypatch, tmp_path):
    """engine.mode=llm 时 LLM 失败 → 显式 rule_fallback 标记。"""
    _init_store(monkeypatch, tmp_path)
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "get_config", lambda: _cfg(engine={"mode": "llm"}))

    async def fake_stream(query, ctx, cfg, image_b64=""):
        yield {"type": "error", "error": "boom"}

    monkeypatch.setattr(llm_mod, "react_stream", fake_stream)
    events = asyncio.run(_collect(pipeline.run_stream(pipeline.ChannelMessage(text="发货多久"), [], "v2b")))
    assert events[-1][1].mode == "rule_fallback"


# ---------- 视觉多模态 ----------

def _mini_ctx():
    return {
        "persona": "你是知客。",
        "modules_on": {"kb": False, "tools": False, "memory": False},
        "kb_block": "（无相关条目）",
        "tools_schema": {},
        "memory_block": "",
        "summary": "",
        "history": [],
        "used": {},
    }


def test_data_url_mime_detection():
    assert llm_mod._data_url("iVBORw0KGgoAAAANSUhEUg").startswith("data:image/png;base64,")
    assert llm_mod._data_url("/9j/4AAQSkZJRg").startswith("data:image/jpeg;base64,")
    assert llm_mod._data_url("R0lGODlhAQAB").startswith("data:image/gif;base64,")
    assert llm_mod._data_url("UklGRh4A").startswith("data:image/webp;base64,")


def test_build_messages_vision_routing():
    cfg = _cfg(multimodal={"enable": True, "vision_model": "glm-4v-flash"})
    msgs, model = llm_mod._build_messages("这是什么", _mini_ctx(), cfg, image_b64="iVBORw0KGgoAAAANSUhEUg")
    assert model == "glm-4v-flash"
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "这是什么"}
    assert content[1]["type"] == "image_url" and content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "data:data:" not in content[1]["image_url"]["url"]

    cfg2 = _cfg()
    msgs2, model2 = llm_mod._build_messages("你好", _mini_ctx(), cfg2, image_b64="iVBORw0KGgo")
    assert model2 == "m" and isinstance(msgs2[-1]["content"], str)


def test_pipeline_image_with_vision_model_goes_llm(monkeypatch, tmp_path):
    _init_store(monkeypatch, tmp_path)
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "get_config", lambda: _cfg(multimodal={"enable": True, "vision_model": "glm-4v-flash"}))
    called = {}

    async def fake_react(query, ctx, cfg, image_b64=""):
        called["image"] = image_b64
        return {"answer": "图里是一张退货政策截图。", "confidence": 0.9, "escalate": False}

    monkeypatch.setattr(llm_mod, "react", fake_react)
    reply = asyncio.run(pipeline.run(pipeline.ChannelMessage(text="", image_b64="iVBORw0KGgo"), [], "v3"))
    assert reply.mode == "llm_react" and "退货政策" in reply.text
    assert called["image"] == "iVBORw0KGgo"


def test_pipeline_image_without_vision_still_escalates(monkeypatch, tmp_path):
    _init_store(monkeypatch, tmp_path)
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "get_config", lambda: _cfg(multimodal={"enable": True, "vision_model": ""}))
    reply = asyncio.run(pipeline.run(pipeline.ChannelMessage(text="", image_b64="iVBORw0KGgo"), [], "v4"))
    assert reply.escalate and reply.escalate_reason == "image_needs_human"


# ---------- 混合检索 ----------

def test_retrieve_fallback_without_embedding_backend(monkeypatch, tmp_path):
    kb_file = tmp_path / "kb.json"
    kb_file.write_text(json.dumps({"kb": [
        {"id": "KB001", "title": "退货", "text": "退货政策 7 天", "answer": "7 天内可退", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb_file)
    retrieval._KB_CACHE = None
    retrieval._EMB_STATE["failed"] = True  # 模拟依赖不可用
    try:
        hits = retrieval.retrieve("退货政策", top_k=3, use_embedding=None)
        assert hits and hits[0]["id"] == "KB001"
    finally:
        retrieval._EMB_STATE["failed"] = False


def test_retrieve_hybrid_embedding_boost(monkeypatch, tmp_path):
    kb_file = tmp_path / "kb.json"
    kb_file.write_text(json.dumps({"kb": [
        {"id": "KB001", "title": "退货", "text": "退货政策 7 天", "answer": "7 天内可退", "tags": []},
        {"id": "KB002", "title": "营业时间", "text": "营业时间 周末", "answer": "周末 9-18 点营业", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb_file)
    retrieval._KB_CACHE = None
    retrieval._INDEX_STATE["sig"] = ""  # 强制索引失效
    q = "索取发票凭证"  # 与 KB002 零字面重叠，只有语义能捞回
    # 模拟向量检索：KB002 第 0 名，KB001 不命中
    monkeypatch.setattr(retrieval, "_vec_search", lambda conn, query, kb, top_k: [(0.1, "KB002")])
    monkeypatch.setattr(retrieval, "_fts_search", lambda conn, query, top_k: [])
    monkeypatch.setattr(retrieval, "_ensure_conn", lambda: object())  # dummy conn，走 FTS+vec+RRF 分支
    monkeypatch.setattr(retrieval, "rebuild_index_if_needed", lambda conn, kb: None)  # 跳过真建索引
    hits = retrieval.retrieve_scored(q, top_k=2, min_overlap=0.0, use_embedding=True)
    assert [e["id"] for _, e in hits] == ["KB002"]
    # 关闭向量 → 零命中（bigram 兜底也捞不回）
    hits2 = retrieval.retrieve_scored(q, top_k=2, min_overlap=0.0, use_embedding=False)
    assert all(e["id"] != "KB002" for _, e in hits2)


# ---------- SSE 端到端（真管线，仅桩掉 LLM 流式） ----------

def test_chat_stream_endpoint_sse(client, monkeypatch):
    import core.config as config_mod

    monkeypatch.setattr(config_mod, "get_config", lambda: _cfg())
    # 语义检索关掉，避免仓库默认 KB 干扰（此处只验流式链路）
    async def fake_stream(query, ctx, cfg, image_b64=""):
        yield {"type": "delta", "text": "您好，"}
        yield {"type": "delta", "text": "请问有什么可以帮您？"}
        yield {"type": "final", "data": {"answer": "您好，请问有什么可以帮您？", "citations": [], "confidence": 0.9, "escalate": False}}

    monkeypatch.setattr(llm_mod, "react_stream", fake_stream)
    sid = create_web_session(client, "sse-v")[0]
    r = client.post(f"/api/sessions/{sid}/chat/stream", json={"content": "在吗"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "event: delta" in r.text and "event: done" in r.text
    done_line = [l for l in r.text.split("\n") if l.startswith("data: ") and "message_id" in l][0]
    d = json.loads(done_line[6:])
    assert d["text"] == "您好，请问有什么可以帮您？"
    assert d["message_id"]


def test_chat_stream_persists_messages(client, monkeypatch):
    import core.config as config_mod

    monkeypatch.setattr(config_mod, "get_config", lambda: _cfg())

    async def fake_stream(query, ctx, cfg, image_b64=""):
        yield {"type": "final", "data": {"answer": "好的", "citations": [], "confidence": 0.9, "escalate": False}}

    monkeypatch.setattr(llm_mod, "react_stream", fake_stream)
    sid = create_web_session(client, "sse-p")[0]
    client.post(f"/api/sessions/{sid}/chat/stream", json={"content": "测试持久化"})
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["visitor", "agent"]


def test_delete_message_endpoint(client):
    """访客删除自己的消息：需要会话 secret，删除后历史里标记 deleted。"""
    sid, _secret = create_web_session(client, "del-v")
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "这条要删"})
    assert r.status_code == 200, r.text
    mid = r.json()["message_id"]

    r = client.delete(f"/api/sessions/{sid}/messages/{mid}")
    assert r.status_code == 200, r.text
    msgs = client.get(f"/api/sessions/{sid}/messages").json()["messages"]
    target = [m for m in msgs if m["id"] == mid][0]
    assert target["deleted"] is True

    # 别人没有 secret 不能删
    reset_web_session_auth(client)
    r = client.delete(f"/api/sessions/{sid}/messages/{mid}")
    assert r.status_code == 401
