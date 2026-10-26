import pytest

from core.llm import _parse_llm_answer


def test_json_dict_passes():
    d = _parse_llm_answer('{"answer": "你好", "confidence": 0.9}')
    assert d["answer"] == "你好"


def test_fenced_json_passes():
    raw = "```json\n" + '{"answer": "你好"}' + "\n```"
    d = _parse_llm_answer(raw)
    assert d["answer"] == "你好"


def test_prose_becomes_answer():
    d = _parse_llm_answer("我是知客，您的智能在线客服。")
    assert d["answer"] == "我是知客，您的智能在线客服。"
    assert d["confidence"] == 0.62 and d["escalate"] is False


def test_empty_returns_none():
    assert _parse_llm_answer("") is None
    assert _parse_llm_answer("   ") is None


def test_json_array_is_none():
    assert _parse_llm_answer("[1,2,3]") is None


def test_mixed_prose_and_json_uses_embedded_decision():
    """实测 u2-flash 输出「我需要先搜索…」+ 完整 JSON：必须取 JSON，不能整段发给用户。"""
    from core.llm import _parse_llm_answer

    raw = ('我需要先搜索两家最新的 API 定价信息，确保数据准确。让我查一下。\n\n'
           '{"thought": "需要联网", "tool": "web_search", '
           '"tool_args": {"query": "DeepSeek API 定价"}, "answer": "", "confidence": 0}')
    data = _parse_llm_answer(raw)
    assert data and data["tool"] == "web_search"
    assert data["tool_args"]["query"] == "DeepSeek API 定价"


def test_prose_preamble_is_stripped():
    from core.llm import _parse_llm_answer

    data = _parse_llm_answer("我需要先查一下资料。\n\n其实这个很简单的：1+1=2。")
    assert data and data["answer"] == "其实这个很简单的：1+1=2。"


def test_stream_scanner_drops_trailing_decision_json():
    """流式：散文打头再贴 JSON 时，JSON 一个字符都不能进打字机。"""
    from core.llm import _AnswerStreamer

    s = _AnswerStreamer()
    out = "".join(s.feed("我需要先查一下。\n\n"))
    out += "".join(s.feed('{"thought": "x", "tool": "web_search", "tool_args": {}}'))
    out += "".join(s.finish())
    assert out.strip() == "我需要先查一下。"
    assert s.stopped is True


def test_stream_scanner_keeps_plain_braces():
    """正常正文里的花括号不能被误判成决策 JSON。"""
    from core.llm import _AnswerStreamer

    s = _AnswerStreamer()
    out = "".join(s.feed("集合 {1,2,3} 有三个元素。"))
    out += "".join(s.finish())
    assert out == "集合 {1,2,3} 有三个元素。"
    assert s.stopped is False


def test_stream_scanner_flushes_tail():
    """流结束时必须冲刷缓冲，否则结尾几十个字符会丢。"""
    from core.llm import _AnswerStreamer

    s = _AnswerStreamer()
    text = "这是一段普通回答，没有 JSON，但结尾必须完整保留。"
    out = "".join(s.feed(text))
    out += "".join(s.finish())
    assert out == text


def test_tool_call_with_empty_answer_is_not_a_failure():
    """模型选了工具时 answer 本来就空：判成 empty 会让整条备援链把它冷却掉（实测）。"""
    from core.llm import _parse_llm_answer

    data = _parse_llm_answer(
        '{"thought":"要搜","tool":"web_search","tool_args":{"query":"x"},"answer":""}')
    assert data and data["tool"] == "web_search"


def test_empty_answer_without_tool_still_fails():
    from core.llm import _parse_llm_answer

    assert _parse_llm_answer('{"thought":"嗯","tool":null,"answer":""}') is None


def test_system_prompt_contains_real_clock():
    """模型没有时钟，不注入真实时间就会编日期（实测把 10-26 说成 5-12）。"""
    from datetime import datetime

    from core import config, llm, pipeline

    cfg = config.get_config()
    ctx = pipeline.build_context("现在几点", [], cfg, "clock-probe")
    msgs, _ = llm._build_messages("现在几点", ctx, cfg)
    system = msgs[0]["content"]
    assert "【当前时间】" in system
    assert datetime.now().strftime("%Y-%m-%d") in system
