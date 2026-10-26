"""原生 function calling 回归：工具必须真的被调用，而不是被模型无视。

修复前的真实故障：工具清单只以 JSON 文本塞进 system prompt，模型在联网题上
稳定无视清单直接编新闻/编链接，37 个工具形同虚设（用户反馈「到底有没有用」）。
"""
import asyncio

from core import llm


def _ctx(schema=None):
    return {
        "persona": "你是零。",
        "modules_on": {"kb": False, "tools": True, "memory": False},
        "kb_block": "",
        "tools_schema": schema if schema is not None else [
            {"name": "web_search", "args": ["query", "top_k"], "desc": "搜索实时信息"},
            {"name": "now", "args": {}, "desc": "看时间"},
        ],
        "memory_block": "",
        "summary": "",
        "history": [],
        "tools": {},
        "used": {},
    }


def _cfg(**over):
    llm_cfg = {"has_key": True, "api_key": "k", "base_url": "https://a.cn/v1",
               "model": "m", "tool_calling": True, "temperature": 0.2,
               "top_p": 1.0, "max_tokens": 256}
    llm_cfg.update(over)
    return {"llm": llm_cfg, "multimodal": {}, "context": {"char_budget": 2400},
            "escalation": {"react_steps": 3}}


def test_openai_tools_schema_shape():
    tools = llm._openai_tools(_ctx()["tools_schema"])
    assert [t["function"]["name"] for t in tools] == ["web_search", "now"]
    web = tools[0]["function"]["parameters"]
    assert web["type"] == "object"
    assert set(web["properties"]) == {"query", "top_k"}
    assert web["required"] == ["query"]
    # 无参工具不能造出必填字段
    assert tools[1]["function"]["parameters"]["required"] == []


def test_react_sends_native_tools_and_parses_tool_calls(monkeypatch):
    sent = {}

    async def fake_post(base, key, payload):
        sent["payload"] = payload
        import json as _json
        return _json.dumps({
            "tool": "web_search",
            "tool_args": {"query": "科技新闻"},
            "answer": "", "thought": "", "citations": [],
            "confidence": 0.6, "escalate": False, "escalate_reason": "",
        }, ensure_ascii=False)

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    llm._COOLDOWN.clear()
    data = asyncio.run(llm.react("查新闻", _ctx(), _cfg()))
    payload = sent["payload"]
    assert payload.get("tools"), "必须走原生 tools 参数"
    assert "response_format" not in payload, "tools 与 json_object 不能同时要"
    assert data["tool"] == "web_search"
    # prompt 里不再重复塞一份工具清单（两份会打架）
    system = payload["messages"][0]["content"]
    assert "可用工具：" not in system


def test_react_without_tool_calling_uses_json_mode(monkeypatch):
    sent = {}

    async def fake_post(base, key, payload):
        sent["payload"] = payload
        return '{"answer": "好", "confidence": 0.9}'

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    llm._COOLDOWN.clear()
    data = asyncio.run(llm.react("你好", _ctx(), _cfg(tool_calling=False)))
    assert "tools" not in sent["payload"]
    assert sent["payload"]["response_format"] == {"type": "json_object"}
    assert "可用工具：" in sent["payload"]["messages"][0]["content"]
    assert data["answer"] == "好"


def test_post_chat_falls_back_when_endpoint_rejects_tools(monkeypatch):
    """端点 400 时逐级降级：去 tools → 去 response_format，不能整轮失败。"""
    calls = []

    class _Resp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise llm.httpx.HTTPStatusError("x", request=None, response=None)  # type: ignore[arg-type]

        def json(self):
            return self._body

    class _Client:
        async def post(self, url, headers=None, json=None):
            calls.append(dict(json or {}))
            if "tools" in (json or {}):
                return _Resp(400, {})
            return _Resp(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm, "get_client", lambda *a, **k: _Client())
    out = asyncio.run(llm._post_chat("https://a.cn/v1", "k", {
        "model": "m", "messages": [], "tools": [{"type": "function"}]}))
    assert out == "ok"
    assert len(calls) == 2 and "tools" not in calls[-1]


def test_decision_from_streamed_tool_calls():
    calls = {0: {"id": "c1", "name": "web_search", "arguments": '{"query": "x"}'}}
    d = llm._decision_from_calls(calls, prose="")
    assert d["tool"] == "web_search"
    assert d["tool_args"] == {"query": "x"}
