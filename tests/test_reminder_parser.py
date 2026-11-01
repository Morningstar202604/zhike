from datetime import datetime, timezone

import pytest

from core import reminder_parser


NOW = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)


def test_parser_due_accepts_relative_and_absolute_forms():
    assert reminder_parser.parse_due("十分钟后", NOW) == datetime(2026, 11, 1, 8, 10, tzinfo=timezone.utc)
    assert reminder_parser.parse_due("明天早上八点", NOW) == datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    assert reminder_parser.parse_due("2026-12-01T09:30:00+00:00", NOW) == datetime(
        2026, 12, 1, 9, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", ["回头再说", "", "明天二十五点"])
def test_parser_due_is_fail_closed(text):
    assert reminder_parser.parse_due(text, NOW) is None


def test_parser_clock_and_occurrences():
    assert reminder_parser.parse_clock("下午三点半") == (15, 30)
    assert reminder_parser.parse_clock("下午二十五点") is None
    assert reminder_parser.next_occurrence("daily", 9, 0, NOW) == datetime(
        2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    assert reminder_parser.next_occurrence("weekday", 9, 0, NOW).weekday() < 5


def test_parser_repeat_preserves_original_text():
    rule, cleaned = reminder_parser.parse_repeat("每月三十一号提醒我交房租")
    assert rule == "monthly:31"
    assert cleaned == "提醒我交房租"
    rule, cleaned = reminder_parser.parse_repeat("每周三提醒我复盘")
    assert rule == "weekly:2"
    assert cleaned == "提醒我复盘"


def test_parser_has_no_service_dependencies():
    """纯函数模块不得反向依赖 tools/store/main，否则抽取会引入循环导入。"""
    import inspect

    source = inspect.getsource(reminder_parser)
    assert "import core.tools" not in source
    assert "from core import tools" not in source
    assert "from core import store" not in source
