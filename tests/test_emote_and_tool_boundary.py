"""表情包与工具异常边界回归。

真实故障：emotes/index.json 是数组，tool_search_emote 按 {items:[...]} 解析，
AttributeError 一路抛到管线，整轮掉回「这个我暂时答不上来」——
用户看到的就是「让机器人发表情包它说答不上来」。
"""
import json

import pytest

from core import pipeline, tools
from core.tool_context import ToolContext


def _ctx(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text(json.dumps([
        {"kind": "image", "summary": "[动画表情]", "data": "C:/x/a.jpg"},
        {"kind": "image", "summary": "猫猫无语", "data": "C:/x/b.gif"},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("GATEWAY_EMOTE_DIR", str(tmp_path))
    return ToolContext(visitor_id="v", session_id="", channel_type="qq_private",
                       channel_user_id="1", group_id=None, group_name="",
                       message_text="", image_b64="", history=())


def test_search_emote_handles_list_index(tmp_path, monkeypatch):
    r = tools.tool_search_emote(_ctx(tmp_path, monkeypatch), keyword="猫")
    assert "猫猫无语" in r["note"]
    assert r["sample_path"].endswith("b.gif")


def test_search_emote_empty_keyword_returns_recent(tmp_path, monkeypatch):
    r = tools.tool_search_emote(_ctx(tmp_path, monkeypatch), keyword="")
    assert r.get("found") is not False
    assert "表情包" in r["note"]


def test_tool_exception_does_not_kill_the_round(monkeypatch):
    """工具抛异常必须被吞掉并转成可解释的 note，而不是整轮降级。"""
    async def boom(*a, **k):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(tools, "invoke_with", boom)
    ctx = {"tools": {"search_emote": boom}, "tools_schema": [{"name": "search_emote"}],
           "used": {}, "tool_ctx": None}
    import asyncio
    reply = asyncio.run(pipeline._make_llm_reply(
        {"tool": "search_emote", "tool_args": {}, "answer": ""}, ctx,
        {"escalation": {"confidence_threshold": 0.6}}))
    assert "工具执行出错" in reply.text
    assert "答不上来" not in reply.text


def test_emote_intent_gets_marker_even_if_model_refuses(tmp_path, monkeypatch):
    """模型说「我没法发图」时，必须把 [表情包:kw] 补进正文，否则网关无从发图。"""
    import asyncio

    ctx = {"tools": {"search_emote": object()}, "tools_schema": [{"name": "search_emote"}],
           "used": {}, "tool_ctx": None}

    async def fake_invoke(registry, name, args, tctx):
        return {"tool": name, "status": "ok", "note": "库里有这些：猫猫无语（image）",
                "found": True}

    monkeypatch.setattr(tools, "invoke_with", fake_invoke)
    reply = asyncio.run(pipeline._make_llm_reply(
        {"tool": "search_emote", "tool_args": {"keyword": "猫"},
         "answer": "我这边没法直接发图片，给你来段文字表情吧"}, ctx,
        {"escalation": {"confidence_threshold": 0.6}}))
    assert "[表情包:猫]" in reply.text
    assert "没法直接发图片" not in reply.text
