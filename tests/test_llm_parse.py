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
