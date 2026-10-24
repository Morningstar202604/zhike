"""core.llm 多提供商备援链：构建顺序 / 冷却熔断 / 竞速接棒。"""
import asyncio

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
