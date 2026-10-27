"""意图直通：要表情包就真发图，不赌模型记不记得标记语法。"""
import asyncio
import json

from core import pipeline, tools
from core.tool_context import ToolContext


def _ctx(monkeypatch, tmp_path, keyword_hit="猫"):
    (tmp_path / "index.json").write_text(json.dumps(
        [{"kind": "image", "summary": "", "data": "C:/x/cat.gif"}], ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "tags.json").write_text(json.dumps({"C:/x/cat.gif": "猫，得意"}, ensure_ascii=False),
                                        encoding="utf-8")
    monkeypatch.setenv("GATEWAY_EMOTE_DIR", str(tmp_path))
    tctx = ToolContext(channel_type="qq_private", channel_user_id="1")
    return {"tools": {"search_emote": tools.tool_search_emote},
            "tools_schema": [{"name": "search_emote", "args": ["keyword"]}],
            "tool_ctx": tctx, "used": {},
            "intent": __import__("core.intent", fromlist=["x"]).classify("发个表情包")}


def test_direct_emote_returns_marker(monkeypatch, tmp_path):
    ctx = _ctx(monkeypatch, tmp_path)
    reply = asyncio.run(pipeline._direct_emote("发个猫咪表情包", ctx, {}))
    assert reply is not None and "[表情包:猫咪]" in reply.text
    assert reply.tool_used == "search_emote"


def test_direct_emote_skips_non_emote_intent(monkeypatch, tmp_path):
    ctx = _ctx(monkeypatch, tmp_path)
    reply = asyncio.run(pipeline._direct_emote("今天天气怎么样", ctx, {}))
    assert reply is None


def test_direct_emote_empty_library_speaks_without_marker(monkeypatch, tmp_path):
    (tmp_path / "index.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("GATEWAY_EMOTE_DIR", str(tmp_path))
    ctx = _ctx(monkeypatch, tmp_path)
    (tmp_path / "index.json").write_text("[]", encoding="utf-8")
    reply = asyncio.run(pipeline._direct_emote("发个表情包", ctx, {}))
    assert reply is not None and "[表情包" not in reply.text
