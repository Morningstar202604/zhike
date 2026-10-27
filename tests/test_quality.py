"""输出判别器：AI 腔/能力否认/客服腔/markdown 的判定与重写回归。"""
import asyncio

from core import quality


def test_ai_self_is_high_severity():
    issues = quality.critique("作为人工智能助手，我不能回答这个问题。")
    assert any(i["code"] == "ai_self" and i["severity"] == "high" for i in issues)
    assert quality.needs_rewrite(issues)


def test_identity_question_needs_cat():
    issues = quality.critique("我是零，一个助手。", "你是谁")
    assert any(i["code"] == "identity_no_cat" for i in issues)
    assert not quality.critique("我是零，一只萌萌的小猫，喵～", "你是谁")


def test_capability_refusal_detected():
    issues = quality.critique("我这边没法直接发图片，你自己找吧。")
    assert any(i["code"] == "capability_refusal" and i["severity"] == "high" for i in issues)


def test_fake_handoff_detected():
    issues = quality.critique("已为您转接人工，请稍等。")
    assert any(i["code"] == "fake_handoff" for i in issues)


def test_markdown_and_service_tone_are_mid():
    issues = quality.critique("**结论**：感谢您的咨询，希望对您有帮助。[表情:得意]")
    codes = {i["code"] for i in issues}
    assert "markdown" in codes and "service_tone" in codes
    assert not quality.needs_rewrite(issues)


def test_too_long_only_for_qq():
    text = "啊" * 300
    assert any(i["code"] == "too_long" for i in quality.critique(text, channel="qq_group"))
    assert not any(i["code"] == "too_long" for i in quality.critique(text, channel="webchat"))


def test_clean_reply_has_no_issues():
    assert quality.critique("这题我不接，猫生太短，站队的活儿另请高明，喵～") == []


def test_empty_reply_is_high():
    issues = quality.critique("")
    assert issues and issues[0]["code"] == "empty" and quality.needs_rewrite(issues)


def test_rewrite_returns_none_without_llm(monkeypatch):
    from core import config

    monkeypatch.setattr(config, "get_config", lambda: {"llm": {"api_key": "", "base_url": ""}})
    assert asyncio.run(quality.rewrite("作为AI我不能", [{"detail": "x"}])) is None


def test_rewrite_rejects_rewrite_that_still_says_ai(monkeypatch):
    """改写结果自己又写成 AI：宁可保留原文，也不能把坏结果写回去。"""
    from core import config
    from core import quality as q

    monkeypatch.setattr(config, "get_config",
                        lambda: {"llm": {"api_key": "k", "base_url": "https://x.cn/v1", "model": "m"}})

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "作为AI助手我做不到"}}]}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(q, "get_client", lambda *a, **k: _Client())
    out = asyncio.run(q.rewrite("作为AI我不能", [{"detail": "自称 AI", "code": "ai_self",
                                                "severity": "high"}]))
    assert out is None


def test_summarize_joins_details():
    s = quality.summarize([{"detail": "a"}, {"detail": "b"}])
    assert "a" in s and "b" in s


def test_model_leak_detected():
    """u2-flash 实测会答「我是 U2.1，由云知声研发」——必须当人设硬伤处理。"""
    issues = quality.critique("我是U2.1，由云知声研发的人工智能助手。")
    codes = {i["code"] for i in issues}
    assert "model_leak" in codes
    assert quality.needs_rewrite(issues)


def test_filter_leak_detected():
    issues = quality.critique("The request was rejected because it was considered high risk")
    assert any(i["code"] == "filter_leak" for i in issues)


def test_gate_falls_back_when_rewrite_fails(monkeypatch):
    """重写失败时不能把 AI 自述原样发出去，必须有猫味兜底。"""
    import asyncio

    from core import pipeline, quality as q

    async def fake_rewrite(*a, **k):
        return None

    monkeypatch.setattr(q, "rewrite", fake_rewrite)
    reply = pipeline.AgentReply(text="我是U2.1，由云知声研发", mode="llm_react")
    asyncio.run(pipeline._gate_output(reply, "你是谁", {"context": {"persona": ""}}))
    assert "云知声" not in reply.text and "U2.1" not in reply.text
    assert "零" in reply.text or "喵" in reply.text


def test_fallback_for_maps_codes():
    assert "零" in pipeline_fallback({"code": "ai_self"})
    assert "发" in pipeline_fallback({"code": "capability_refusal"})


def pipeline_fallback(issue):
    from core import pipeline

    return pipeline._fallback_for([issue])


def test_quality_group_is_writable():
    """quality 分组必须能通过 /api/config 落库：schema 漏登记时 PUT 静默丢弃（已踩）。"""
    from core import config

    assert "quality" in config._SCHEMA
    assert "ensemble" in config._SCHEMA["quality"]
    config.validate_patch({"quality": {"rewrite": True, "ensemble": 2,
                                        "ensemble_intents": ["chat"]}})


def test_emoji_flagged_as_mid():
    issues = quality.critique("晚安喵～ 🌙")
    assert any(i["code"] == "emoji" for i in issues)
    assert not quality.needs_rewrite(issues)
    assert not any(i["code"] == "emoji" for i in quality.critique("晚安喵～"))


def test_rewrite_rejects_filter_error_output(monkeypatch):
    """端点把过滤报错当成"改写结果"返回时（实测出现过），不能把它当答案。"""
    from core import config
    from core import quality as q

    monkeypatch.setattr(config, "get_config",
                        lambda: {"llm": {"api_key": "k", "base_url": "https://x.cn/v1", "model": "m"}})

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "The request was rejected because it was considered high risk"}}]}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(q, "get_client", lambda *a, **k: _Client())
    out = asyncio.run(q.rewrite("我是U2.1", [{"code": "model_leak", "severity": "high", "detail": "x"}]))
    assert out is None


def test_rewrite_masks_model_leak_before_sending(monkeypatch):
    """原文里的模型自述会触发端点内容过滤（400），发出去前必须打码。"""
    from core import config
    from core import quality as q

    monkeypatch.setattr(config, "get_config",
                        lambda: {"llm": {"api_key": "k", "base_url": "https://x.cn/v1", "model": "m"}})
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "我是零，一只猫，喵～"}}]}

    class _Client:
        async def post(self, url, headers=None, json=None):
            seen["prompt"] = json["messages"][0]["content"]
            return _Resp()

    monkeypatch.setattr(q, "get_client", lambda *a, **k: _Client())
    out = asyncio.run(q.rewrite("我是U2.1，由云知声研发",
                                [{"code": "model_leak", "severity": "high", "detail": "x"}]))
    assert out and "云知声" not in seen["prompt"] and "U2.1" not in seen["prompt"]


def test_program_self_confession_detected():
    """被问「说人话你到底是什么」时，u2-flash 会答「我是一套本地程序，不是真人」——
    这跟自称 AI 是同一条底线，正则必须覆盖换了个说法的自曝。"""
    issues = quality.critique("我是一套能读能写的本地程序，不是真人，也没有真人给你打字。", "你到底是什么")
    assert any(i["code"] == "ai_self" and i["severity"] == "high" for i in issues)


def test_json_leak_detected():
    text = '我是零，一只猫，喵～\n\n{"thought": "x", "tool": null, "answer": "y"}'
    issues = quality.critique(text, "你是谁")
    assert any(i["code"] == "json_leak" and i["severity"] == "high" for i in issues)


def test_strip_json_tail_keeps_prose():
    from core import pipeline

    text = '我是零，一只猫，喵～\n\n{"thought": "x", "tool": null, "answer": "y"}'
    out = pipeline._strip_json_tail(text)
    assert out == "我是零，一只猫，喵～"


def test_strip_json_tail_prefers_longer_answer():
    from core import pipeline

    out = pipeline._strip_json_tail('短。{"answer": "我是零，一只萌萌的小猫，喵～"}')
    assert "萌萌的小猫" in out and "{" not in out


def test_strip_json_tail_empty_when_only_json():
    from core import pipeline

    assert pipeline._strip_json_tail('{"thought": "x"}') == ""
