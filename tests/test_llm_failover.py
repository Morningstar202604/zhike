"""core.llm 多提供商备援链：构建顺序 / 冷却熔断 / 竞速接棒。"""
import asyncio
import json

from core import llm


def _cfg(**kw):
    base = {"api_key": "k", "base_url": "https://a.cn/v1", "model": "m1",
            "fallback_model": "", "providers": []}
    base.update(kw)
    return base


def test_chain_order_and_key_inheritance():
    chain = llm._chain(_cfg(
        fallback_model="m2",
        providers=[
            {"name": "same", "base_url": "https://a.cn/v1/", "model": "m3"},      # 同站：沿用主 Key
            {"name": "other-nokey", "base_url": "https://b.cn/v1", "model": "m4"},  # 异站无 Key：跳过
            {"name": "other-key", "base_url": "https://c.cn/v1", "model": "m5", "api_key": "k2"},
        ]), "m1")
    assert [(c["model"], c["api_key"]) for c in chain] == [
        ("m1", "k"), ("m2", "k"), ("m3", "k"), ("m5", "k2")]


def test_chain_dedup_primary():
    chain = llm._chain(_cfg(providers=[{"name": "x", "base_url": "https://a.cn/v1", "model": "m1"}]), "m1")
    assert len(chain) == 1


def test_chain_vision_primary_only():
    chain = llm._chain(_cfg(fallback_model="m2"), "vm", vision=True)
    assert len(chain) == 1 and chain[0]["model"] == "vm"


def test_race_failover_and_cooldown():
    calls: list[str] = []

    async def fake_post(base, key, payload):
        calls.append(payload["model"])
        if payload["model"] == "m1":
            raise RuntimeError("network down")
        return "ok:" + payload["model"]

    orig_post, orig_hedge = llm._post_chat, llm._HEDGE_S
    llm._post_chat, llm._HEDGE_S = fake_post, 0.02
    llm._COOLDOWN.clear()
    try:
        raw = asyncio.run(llm._race_chain(llm._chain(_cfg(fallback_model="m2"), "m1"),
                                          [], {}, parse=lambda t: {"a": t}))
        assert raw == {"a": "ok:m2"}
        assert llm._cooling("https://a.cn/v1", "m1")
        # 冷却中的候选被跳过：下一次请求直接命中备胎，不再撞墙
        raw2 = asyncio.run(llm._race_chain(llm._chain(_cfg(fallback_model="m2"), "m1"),
                                           [], {}, parse=lambda t: {"a": t}))
        assert raw2 == {"a": "ok:m2"}
        assert calls.count("m1") == 1
    finally:
        llm._post_chat, llm._HEDGE_S = orig_post, orig_hedge
        llm._COOLDOWN.clear()


def test_race_empty_answer_counts_as_failure():
    """HTTP 200 但内容解析不出（空答案/截断 JSON）→ 视作候选失败，换下一个。"""
    async def fake_post(base, key, payload):
        if payload["model"] == "m1":
            return '{"answer": "", "confidence": 0.9}'   # 200 但空答案
        return '{"answer": "ok", "confidence": 0.9}'

    orig_post, orig_hedge = llm._post_chat, llm._HEDGE_S
    llm._post_chat, llm._HEDGE_S = fake_post, 0.02
    llm._COOLDOWN.clear()
    try:
        raw = asyncio.run(llm._race_chain(llm._chain(_cfg(fallback_model="m2"), "m1"),
                                          [], {}, parse=llm._parse_llm_answer))
        assert raw == {"answer": "ok", "confidence": 0.9}
        assert llm._cooling("https://a.cn/v1", "m1")
    finally:
        llm._post_chat, llm._HEDGE_S = orig_post, orig_hedge
        llm._COOLDOWN.clear()


def test_race_all_cooling_returns_none():
    llm._COOLDOWN[("https://a.cn/v1", "m1")] = float("inf")
    try:
        raw = asyncio.run(llm._race_chain(llm._chain(_cfg(), "m1"), [], {}))
        assert raw is None
    finally:
        llm._COOLDOWN.clear()


def test_react_end_to_end_failover(monkeypatch):
    async def fake_post(base, key, payload):
        if payload["model"] == "m1":
            raise RuntimeError("x")
        return '{"answer":"好","confidence":0.9,"citations":[],"escalate":false}'

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    monkeypatch.setattr(llm, "_HEDGE_S", 0.02)
    llm._COOLDOWN.clear()
    llm_cfg = _cfg(fallback_model="m2")
    llm_cfg["has_key"] = True
    ctx = {"persona": "p", "modules_on": {"kb": False, "tools": False, "memory": False},
           "kb_block": "", "tools_schema": {}, "memory_block": "", "summary": "", "history": []}
    data = asyncio.run(llm.react("q", ctx, {"llm": llm_cfg, "multimodal": {}}))
    assert data and data["answer"] == "好"
    llm._COOLDOWN.clear()


def test_truncated_mid_answer_retries_and_keeps_longer_reply(monkeypatch):
    """内容非空但 finish_reason=length 时必须重试，不能把半句话发给用户。

    回归背景：实测「退货怎么弄」只回了「那我把步骤交出来喵」就断了，后面的
    退货步骤全丢。早期实现只在 content 为空时处理截断，于是「有内容但被截断」
    被当成正常成功直接返回——用户看到的是半截话，还以为机器人敷衍。
    """
    import asyncio

    import httpx

    orig_client = httpx.AsyncClient

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body.get("max_tokens"))
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "那我把步骤交出来喵"},
                "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "那我把步骤交出来喵：签收后 7 天内无理由退货，质量问题 15 天内换货。"},
            "finish_reason": "stop"}]})

    fake = orig_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm, "get_client", lambda base, timeout=180: fake)
    out = asyncio.run(llm._post_chat("http://x", "k", {"max_tokens": 512, "messages": []}))
    assert "签收后 7 天内" in out, f"应当采用补全后的长回复，实际: {out}"
    assert len(calls) == 2, "应当重试一次"
    assert calls[1] > calls[0], "重试必须提高 max_tokens"


def test_truncated_mid_answer_keeps_original_when_retry_shorter(monkeypatch):
    """重试结果更短时保留原答案，不能被端点带偏。"""
    import asyncio

    import httpx

    orig_client = httpx.AsyncClient

    seq = iter([
        "第一版比较完整的一段话，虽然被截断",
        "短",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{
            "message": {"content": next(seq)}, "finish_reason": "length"}]})

    fake = orig_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm, "get_client", lambda base, timeout=180: fake)
    out = asyncio.run(llm._post_chat("http://x", "k", {"max_tokens": 512, "messages": []}))
    assert out == "第一版比较完整的一段话，虽然被截断"
