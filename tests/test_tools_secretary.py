"""秘书工具层测试：上下文 / 记忆 / 提醒 / 网页安全 / 结果契约。

这些用例保护的是「工具真的能干活」这件事：
- 缺上下文时必须追问，而不是瞎猜一个群号发出去
- 内网地址必须拒绝（SSRF），否则 `read_webpage` 就是提权入口
- 提醒必须能落库、能取消、到点能被调度器捞出来
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from core import tools
from core.tool_context import EMPTY, ToolContext


# ------------------------------------------------------------
# 结果契约
# ------------------------------------------------------------

def test_result_contract_shapes():
    ok = tools.ok_result("t", "note", extra=1)
    assert ok["connected"] is True and ok["found"] is True and ok["note"] == "note"
    assert ok["extra"] == 1

    miss = tools.miss_result("t", "nope")
    assert miss["connected"] is True and miss["found"] is False

    off = tools.off_result("t", "no backend")
    assert off["connected"] is False and off["found"] is False

    need = tools.need_ctx("t", "群号")
    assert need["status"] == "need_context"
    assert "群号" in need["note"]


# ------------------------------------------------------------
# 上下文
# ------------------------------------------------------------

def test_current_context_uses_history_and_scene():
    ctx = ToolContext(
        visitor_id="v1", session_id="s1", channel_type="qq", group_id="123456",
        group_name="测试群", history=({"role": "visitor", "content": "我明天要出差"},
                                     {"role": "agent", "content": "记下了"}),
    )
    res = tools.tool_current_context(ctx=ctx)
    assert res["found"] is True
    assert "我明天要出差" in res["note"]
    assert "测试群" in res["scene"]
    assert res["message_count"] == 2


def test_current_context_asks_when_empty():
    res = tools.tool_current_context(ctx=EMPTY)
    assert res["status"] == "need_context"


# ------------------------------------------------------------
# 记忆
# ------------------------------------------------------------

def test_remember_then_recall():
    ctx = ToolContext(visitor_id="v-mem-tool")
    saved = tools.tool_remember(ctx=ctx, key="常用地址", value="杭州西湖区")
    assert saved["found"] is True

    got = tools.tool_recall_memory(ctx=ctx, key="常用地址")
    assert "杭州西湖区" in got["note"]

    all_mem = tools.tool_recall_memory(ctx=ctx)
    assert "常用地址" in all_mem["note"]


def test_recall_without_visitor_asks_context():
    res = tools.tool_recall_memory(ctx=EMPTY)
    assert res["status"] == "need_context"


def test_recall_missing_key_is_miss():
    ctx = ToolContext(visitor_id="v-mem-none")
    res = tools.tool_recall_memory(ctx=ctx, key="不存在")
    assert res["found"] is False


# ------------------------------------------------------------
# 提醒时间解析
# ------------------------------------------------------------

NOW = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text,expected", [
    ("10分钟后", NOW + timedelta(minutes=10)),
    ("明天早上9点", datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)),
    ("明天下午3点", datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)),
    ("今晚8点", datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)),
    ("后天晚上8点", datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc)),
    ("2小时后", NOW + timedelta(hours=2)),
    ("3天后", NOW + timedelta(days=3)),
    ("1周后", NOW + timedelta(weeks=1)),
])
def test_parse_due_relative(text, expected):
    assert tools._parse_due(text, NOW) == expected


def test_parse_due_absolute_iso():
    assert tools._parse_due("2026-12-01T09:30:00+00:00", NOW) == datetime(
        2026, 12, 1, 9, 30, tzinfo=timezone.utc)


def test_parse_due_rejects_vague_input():
    """看不懂就返回 None，让工具去追问——绝不默认一个时间。"""
    assert tools._parse_due("回头再说", NOW) is None
    assert tools._parse_due("", NOW) is None


def test_remind_requires_parseable_time():
    ctx = ToolContext(visitor_id="v-rem-bad", channel_type="webchat")
    res = tools.tool_remind(ctx=ctx, text="交周报", when="有空的时候")
    assert res["status"] == "need_context"
    assert "没看懂" in res["note"]


def test_remind_persists_and_can_be_listed():
    ctx = ToolContext(visitor_id="v-rem-ok", channel_type="qq", group_id="999",
                      channel_user_id="888")
    res = tools.tool_remind(ctx=ctx, text="给客户回电话", when="1天后")
    assert res["found"] is True
    rid = res["reminder_id"]

    listed = tools.tool_list_reminders(ctx=ctx)
    assert "给客户回电话" in listed["note"]

    cancelled = tools.tool_cancel_reminder(ctx=ctx, reminder_id=str(rid))
    assert cancelled["found"] is True
    assert "已取消" in cancelled["note"]


def test_cancel_cannot_touch_another_visitors_reminder():
    owner = ToolContext(visitor_id="v-owner", channel_type="webchat")
    rid = tools.tool_remind(ctx=owner, text="别人的事", when="1天后")["reminder_id"]

    intruder = ToolContext(visitor_id="v-intruder", channel_type="webchat")
    res = tools.tool_cancel_reminder(ctx=intruder, reminder_id=str(rid))
    assert res["found"] is False
    # 原主人还能看到
    assert "别人的事" in tools.tool_list_reminders(ctx=owner)["note"]


# ------------------------------------------------------------
# 网页安全（SSRF）
# ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8010/api/config",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/",
    "file:///C:/Windows/win.ini",
    "ftp://example.com/x",
])
def test_url_guard_blocks_internal_and_bad_schemes(url):
    from core import url_guard

    with pytest.raises(url_guard.UnsafeUrl):
        url_guard.check_url(url)


def test_read_webpage_refuses_internal_without_network(monkeypatch):
    """内网地址必须在发起请求之前就被拒绝（不能依赖网络层报错）。"""
    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **kw):
            called["n"] += 1

    monkeypatch.setattr("httpx.AsyncClient", _Boom)
    res = asyncio.run(tools.tool_read_webpage(url="http://127.0.0.1:8010/"))
    assert res["found"] is False
    assert called["n"] == 0, "拒绝应发生在建连之前"


def test_html_to_text_strips_scripts_and_tags():
    html = "<html><head><style>.a{}</style><script>alert(1)</script></head>" \
           "<body><h1>标题</h1><p>正文&nbsp;内容</p></body></html>"
    text = tools._html_to_text(html)
    assert "alert" not in text and ".a{" not in text
    assert "标题" in text and "正文" in text


# ------------------------------------------------------------
# 缺后端时的诚实降级
# ------------------------------------------------------------

def test_read_file_reports_offline_when_executor_down(monkeypatch):
    from core import executor_client

    async def boom(*a, **kw):
        raise executor_client.ExecutorOffline("down")

    monkeypatch.setattr(executor_client, "invoke", boom)
    res = asyncio.run(tools.tool_read_file(path="C:/tmp/x.txt"))
    assert res["connected"] is False
    assert "执行器" in res["note"]


def test_describe_image_without_vision_model(monkeypatch):
    from core import config

    monkeypatch.setattr(config, "get_config",
                        lambda: {"multimodal": {"vision_model": ""}, "llm": {"has_key": True}})
    res = asyncio.run(tools.tool_describe_image(ctx=EMPTY))
    assert res["connected"] is False
    assert "视觉模型" in res["note"]


def test_qq_send_without_target_asks_context():
    res = asyncio.run(tools.tool_qq_send(ctx=ToolContext(visitor_id="v", channel_type="webchat"),
                                         text="hi"))
    assert res["status"] == "need_context"


# ------------------------------------------------------------
# 规则路由
# ------------------------------------------------------------

def test_detect_tool_routes_reminder_intent():
    name, args = tools.detect_tool("10分钟后提醒我关火")
    assert name == "remind"
    assert "关火" in args["text"]
    assert "10分钟" in args["when"]


def test_detect_tool_routes_webpage_read():
    name, args = tools.detect_tool("帮我看看这个 https://example.com/a 讲了什么")
    assert name == "read_webpage"
    assert args["url"].startswith("https://example.com")


def test_detect_tool_returns_none_for_chitchat():
    assert tools.detect_tool("今天心情不错") == (None, None)


# ------------------------------------------------------------
# 调度器
# ------------------------------------------------------------

def test_reminder_due_and_mark_sent():
    from core import store

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    rec = store.reminder_add("v-sched", "webchat:v-sched", "该喝水了", past, "webchat")
    due = store.reminder_due(datetime.now(timezone.utc).isoformat())
    assert any(r["id"] == rec["id"] for r in due)

    store.reminder_mark(rec["id"], "sent")
    due_after = store.reminder_due(datetime.now(timezone.utc).isoformat())
    assert all(r["id"] != rec["id"] for r in due_after)


def test_reminder_failure_increments_attempts(monkeypatch):
    from core import reminders, store

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    rec = store.reminder_add("v-fail", "qq_group:123", "发不出去的提醒", past, "qq_group", "123")

    async def boom(*a, **kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(reminders, "_deliver_qq", boom)
    asyncio.run(reminders.tick())

    after = store.reminder_get(rec["id"])
    assert after["attempts"] == 1
    assert after["status"] == "pending"  # 还会重试
    assert "gateway down" in (after["last_error"] or "")


# ------------------------------------------------------------
# 确定性前置：URL / 文件路径不该指望模型主动选工具
# ------------------------------------------------------------

def test_prefetch_url_extracts_url_and_hint():
    from core import pipeline

    q = "帮我看看这个网页讲了什么 https://example.com/a/b"
    m = pipeline._URL_RE.search(q)
    assert m and m.group(0) == "https://example.com/a/b"
    assert pipeline._READ_HINT.search(q)


def test_prefetch_file_matches_both_slash_styles():
    from core import pipeline

    for q in [
        "读一下 C:/Users/x/README.md 前几行",
        r"读一下 C:\Users\x\README.md 前几行",
        "打开 /home/x/a.txt 内容",
    ]:
        m = pipeline._FILE_RE.search(q)
        assert m is not None, q


def test_prefetch_file_returns_none_without_hint():
    """没有「读/看」意图时不要自作主张去读盘。"""
    from core import pipeline

    ctx = {"modules_on": {"tools": True}, "tool_ctx": EMPTY}
    res = asyncio.run(pipeline._prefetch_file("C:/Users/x/a.txt 这个路径对不对", ctx, {}))
    assert res is None


def test_prefetch_file_reports_permission_denial(monkeypatch):
    """执行器拒绝时要如实说没权限，不要装傻说「我碰不到你的磁盘」。"""
    from core import pipeline
    from core import tools as tools_mod

    async def denied(name, args, ctx=EMPTY, *a, **kw):
        return tools.miss_result("read_file", "没有权限读这个文件：allow_roots 之外")

    monkeypatch.setattr(tools_mod, "invoke", denied)
    ctx = {"modules_on": {"tools": True}, "tool_ctx": EMPTY}
    res = asyncio.run(pipeline._prefetch_file("读一下 C:/Windows/win.ini 内容", ctx, {}))
    assert res is not None
    assert "没有权限" in res.text


def test_detect_tool_keeps_full_time_expression():
    """「明天早上9点」必须整体作为 when，不能切成 when=明天 + text=早上9点。"""
    name, args = tools.detect_tool("明天早上9点提醒我交周报")
    assert name == "remind"
    assert args["when"] == "明天早上9点"
    assert args["text"] == "交周报"


def test_detect_tool_routes_group_history():
    name, _ = tools.detect_tool("群里最近在聊什么")
    assert name == "qq_group_history"


# ------------------------------------------------------------
# 空结果不能升级：问「我有什么待办」不该开出一张工单
# ------------------------------------------------------------

def test_empty_list_reminders_does_not_escalate():
    """没有任何待办是正常答案，不是知识缺口。"""
    from core import pipeline

    ctx = ToolContext(visitor_id="v-empty-rem")
    args = {"text": "", "when": ""}
    res = tools.tool_list_reminders(ctx=ctx)
    assert res["status"] == "empty"
    assert res["found"] is False
    _ = args

    async def run():
        return await pipeline._rule_react(
            "列一下我的待办",
            {"hits": [], "tools": {"list_reminders": lambda: res}, "used": {}, "tool_ctx": ctx},
            {"escalation": {"confidence_threshold": 0.6}},
        )

    reply = asyncio.run(run())
    assert reply.escalate is False, "空待办不能触发升级工单"
    assert reply.tool_used == "list_reminders"


def test_empty_result_contract_is_not_a_miss():
    """empty 与 miss 是两种语义：只有 miss 才代表"没查到"。"""
    empty = tools.empty_result("list_reminders", "你现在没有待提醒的事。")
    miss = tools.miss_result("lookup_policy", "资料里没有相关内容。")
    assert empty["status"] == "empty" and empty["found"] is False
    assert "status" not in miss


def test_real_miss_still_escalates():
    """防止上面那条把真正的「查不到」也一起放过了。"""
    from core import pipeline

    res = tools.miss_result("lookup_policy", "资料里没有相关内容。")

    async def run():
        return await pipeline._rule_react(
            "帮我查一下根本不存在的神秘服务",
            {"hits": [], "tools": {"lookup_policy": lambda: res}, "used": {}, "tool_ctx": EMPTY},
            {"escalation": {"confidence_threshold": 0.6}},
        )

    reply = asyncio.run(run())
    assert reply.escalate is True
    assert reply.escalate_reason == "knowledge_miss"


def test_direct_tool_empty_list_does_not_escalate():
    """直连工具路径（_direct_tool）同样不能把空待办当知识缺口。

    这条路径独立于 _rule_react：模型有 Key 时「列一下我的待办」走这里，
    修复前会开出一张待接管工单（实测 ticket 已生成）。
    """
    from core import pipeline

    ctx = {
        "modules_on": {"tools": True},
        "tools": {"list_reminders": lambda: tools.empty_result(
            "list_reminders", "你现在没有待提醒的事。")},
        "tool_ctx": ToolContext(visitor_id="v-direct-empty"),
        "used": {},
    }
    reply = asyncio.run(pipeline._direct_tool("列一下我的待办", ctx, {"llm": {}}))
    assert reply is not None
    assert reply.escalate is False
    assert reply.tool_used == "list_reminders"


def test_rule_react_empty_list_does_not_escalate():
    """规则路径同样如此（无 Key 部署走这里）。"""
    from core import pipeline

    ctx = {
        "hits": [],
        "tools": {"list_reminders": lambda: tools.empty_result(
            "list_reminders", "你现在没有待提醒的事。")},
        "tool_ctx": ToolContext(visitor_id="v-rule-empty"),
        "used": {},
    }
    reply = asyncio.run(pipeline._rule_react("列一下我的待办", ctx, {}))
    assert reply.escalate is False


# ------------------------------------------------------------
# 时间 / 计算：模型没有时钟，也不该靠 eval
# ------------------------------------------------------------

def test_now_reports_date_and_weekday():
    res = tools.tool_now(ctx=EMPTY)
    assert res["found"] is True
    assert res["date"] and res["weekday"] in "一二三四五六日"
    assert "现在" in res["note"]


@pytest.mark.parametrize("expr,expected", [
    ("1+1", 2), ("(3+5)*2/4", 4), ("2**10", 1024), ("10%3", 1),
    ("3.5*2", 7), ("2**-3", 0.125), ("-3 + 4", 1),
])
def test_calc_basic_math(expr, expected):
    res = tools.tool_calc(expression=expr)
    assert res["found"] is True, res
    assert res["value"] == expected


@pytest.mark.parametrize("expr", [
    '__import__("os").system("echo hi")',
    "open('x')",
    "[1,2,3]",
    "1 if 1 else 2",
    "9**9**9",
])
def test_calc_rejects_non_arithmetic(expr):
    """calc 绝不能变成任意代码执行入口，也不能被大指数算死。"""
    res = tools.tool_calc(expression=expr)
    assert res["found"] is False
    assert res.get("status") != "empty"


def test_calc_division_by_zero_is_readable():
    res = tools.tool_calc(expression="1/0")
    assert res["found"] is False
    assert "ZeroDivision" in res["note"]


def test_detect_tool_routes_time_and_calc():
    enabled = {"now": 1, "calc": 1}
    assert tools.detect_tool("现在几点了", enabled)[0] == "now"
    assert tools.detect_tool("今天几号", enabled)[0] == "now"
    assert tools.detect_tool("算一下 1+1", enabled) == ("calc", {"expression": "1+1"})
    assert tools.detect_tool("3*7等于多少", enabled) == ("calc", {"expression": "3*7"})
    # 不能把正文里的数字误判成算式
    assert tools.detect_tool("帮我解释一下 3*7 是什么意思", enabled)[0] is None


# ------------------------------------------------------------
# 发图 / 生图 / 文档：没有 QQ 上下文时必须追问，不瞎发
# ------------------------------------------------------------

def test_send_image_needs_qq_context():
    res = asyncio.run(tools.tool_send_image(ctx=ToolContext(visitor_id="v"), source="C:/tmp/a.png"))
    assert res.get("status") == "need_context"


def test_send_image_requires_source():
    res = asyncio.run(tools.tool_send_image(ctx=ToolContext(group_id="1"), source=""))
    assert res.get("status") == "need_context"


def test_send_image_blocks_private_url():
    """图片链接同样走 SSRF 校验：内网地址不能发。"""
    res = asyncio.run(tools.tool_send_image(ctx=ToolContext(group_id="1"),
                                            source="http://127.0.0.1:8010/api/config"))
    assert res["found"] is False
    assert "不能发" in res["note"]


def test_generate_image_needs_prompt():
    res = asyncio.run(tools.tool_generate_image(ctx=ToolContext(group_id="1"), prompt="  "))
    assert res.get("status") == "need_context"


def test_parse_document_needs_source():
    res = asyncio.run(tools.tool_parse_document(ctx=EMPTY))
    assert res.get("status") == "need_context"


def test_parse_document_markdown_via_executor(monkeypatch):
    from core import executor_client

    async def fake_invoke(cap, params, timeout_s=30.0):
        import base64
        assert cap == "fs.read_b64"
        return {"b64": base64.b64encode("# 标题\n\n正文内容".encode()).decode()}

    monkeypatch.setattr(executor_client, "invoke", fake_invoke)
    res = asyncio.run(tools.tool_parse_document(ctx=EMPTY, path="C:/tmp/a.md"))
    assert res["found"] is True
    assert "正文内容" in res["note"]


def test_qq_group_members_needs_group():
    res = asyncio.run(tools.tool_qq_group_members(ctx=ToolContext(visitor_id="v")))
    assert res.get("status") == "need_context"


# ------------------------------------------------------------
# 视觉模型回落：不配 vision_model 时用主模型，不能静默「看不了图」
# ------------------------------------------------------------

def test_llm_build_messages_uses_main_model_for_vision_when_unset():
    from core import llm

    cfg = {"llm": {"model": "m-main"}, "multimodal": {"vision_model": ""},
           "context": {"char_budget": 2400}, "escalation": {"react_steps": 3}}
    ctx = {"persona": "p", "modules_on": {}, "kb_block": "", "tools_schema": [],
           "memory_block": "", "summary": "", "history": []}
    msgs, model = llm._build_messages("看图", ctx, cfg, image_b64="iVBORw0KGgo")
    assert model == "m-main"
    assert isinstance(msgs[-1]["content"], list), "未配视觉模型时应按多模态消息发送"


def test_llm_build_messages_prefers_explicit_vision_model():
    from core import llm

    cfg = {"llm": {"model": "m-main"}, "multimodal": {"vision_model": "v-model"},
           "context": {"char_budget": 2400}, "escalation": {"react_steps": 3}}
    ctx = {"persona": "p", "modules_on": {}, "kb_block": "", "tools_schema": [],
           "memory_block": "", "summary": "", "history": []}
    _, model = llm._build_messages("看图", ctx, cfg, image_b64="iVBORw0KGgo")
    assert model == "v-model"


# ------------------------------------------------------------
# 周期提醒
# ------------------------------------------------------------

def test_parse_repeat_variants():
    cases = {
        "每天8点": "daily",
        "每日提醒": "daily",
        "每个工作日9点": "weekday",
        "每周三10点": "weekly:2",
        "每月15号9点": "monthly:15",
        "明天9点": "",
    }
    for text, expected in cases.items():
        rule, _cleaned = tools._parse_repeat(text)
        assert rule == expected, f"{text} -> {rule} (期望 {expected})"


def test_parse_repeat_strips_rule_from_text():
    rule, cleaned = tools._parse_repeat("每天提醒我吃药")
    assert rule == "daily"
    assert "每天" not in cleaned


def test_next_due_daily_and_weekday():
    from core.reminders import next_due

    base = "2026-10-30T09:00:00+00:00"  # 周五
    assert next_due("daily", base).startswith("2026-10-31")
    assert next_due("weekday", base).startswith("2026-11-02")  # 跳过周末


def test_next_due_weekly_lands_on_target_weekday():
    from core.reminders import next_due

    nxt = next_due("weekly:2", "2026-10-25T09:00:00+00:00")  # 周日 → 下周三
    assert nxt.startswith("2026-10-28")


def test_next_due_monthly_clamps_to_month_end(monkeypatch):
    """1/31 + 每月31号 → 2 月必须退到 28 日（2026 非闰年），不能生成非法日期。

    注意：next_due 会把锚点推进到"最近一个未来时刻"，所以这里把"现在"固定在锚点前一刻，
    否则锚点若早已过去，函数会一路推到当前月份（那是正确行为，但测不到夹紧逻辑）。
    """
    from datetime import datetime as _dt, timezone as _tz

    from core import reminders

    class _Frozen(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 1, 31, 8, 0, tzinfo=_tz.utc)

    monkeypatch.setattr(reminders, "datetime", _Frozen)
    nxt = reminders.next_due("monthly:31", "2026-01-31T09:00:00+00:00")
    assert nxt.startswith("2026-02-28")


def test_next_due_returns_none_for_unknown_rule():
    from core.reminders import next_due

    assert next_due("", "2026-10-25T09:00:00+00:00") is None
    assert next_due("yearly", "2026-10-25T09:00:00+00:00") is None


def test_recurring_reminder_reschedules_after_send(monkeypatch):
    """周期提醒发完必须回到 pending 且 due_at 前移，不能变成 sent 结束。"""
    from core import reminders, store

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    rec = store.reminder_add("v-rep", "webchat:v-rep", "吃药", past, "webchat",
                             repeat_rule="daily")

    async def fake_web(rec_, text):
        return True, ""

    monkeypatch.setattr(reminders, "_deliver_web", fake_web)
    asyncio.run(reminders.tick())

    after = store.reminder_get(rec["id"])
    assert after["status"] == "pending"
    assert after["due_at"] > past


# ------------------------------------------------------------
# 文件写入
# ------------------------------------------------------------

def test_write_file_reports_permission_denial(monkeypatch):
    from core import executor_client

    async def denied(*a, **kw):
        raise executor_client.ExecutorDenied("路径不在允许范围内")

    monkeypatch.setattr(executor_client, "invoke", denied)
    res = asyncio.run(tools.tool_write_file(path="C:/Windows/x.txt", content="x"))
    assert res["found"] is False
    assert "没有权限" in res["note"]


def test_write_file_requires_content():
    res = asyncio.run(tools.tool_write_file(path="C:/Users/x/a.txt", content=""))
    assert res["status"] == "need_context"


# ------------------------------------------------------------
# 周期提醒：必须按规则算"下一次"，且用本地时区
# ------------------------------------------------------------

def _remind(when: str, text: str = "喝水"):
    ctx = ToolContext(visitor_id="v-recur", channel_type="webchat")
    return tools.tool_remind(ctx=ctx, text=text, when=when)


def test_recurring_daily_uses_local_timezone():
    """「每天8点」必须是本地 08:00。曾用 UTC 构造，整体偏 8 小时（16:00）。"""
    res = _remind("每天8点")
    assert res["repeat_rule"] == "daily"
    due = datetime.fromisoformat(res["due_at"])
    assert due.hour == 8 and due.minute == 0
    assert due.utcoffset() == datetime.now().astimezone().utcoffset()


def test_recurring_weekly_hits_target_weekday():
    res = _remind("每周三下午3点")
    due = datetime.fromisoformat(res["due_at"])
    assert due.weekday() == 2 and due.hour == 15
    assert res["repeat_rule"] == "weekly:2"


def test_recurring_monthly_does_not_fire_today():
    """「每月15号」今天是 25 号 → 下一次必须是下个月，不能算成今天。"""
    res = _remind("每月15号")
    assert res["repeat_rule"] == "monthly:15"
    due = datetime.fromisoformat(res["due_at"])
    assert due.day == 15 and due > datetime.now().astimezone()


def test_recurring_monthly_31_clamps_in_short_month():
    """每月31号在 2 月要落到当月最后一天（不能抛 ValueError）。"""
    from core import tools as t

    now = datetime(2026, 1, 31, 12, 0).astimezone()
    due = t._next_occurrence("monthly:31", 9, 0, now)
    assert due is not None
    assert due.month == 2 and due.day == 28


def test_recurring_weekday_skips_weekend():
    res = _remind("工作日上午9点")
    due = datetime.fromisoformat(res["due_at"])
    assert due.weekday() < 5 and due.hour == 9
    assert res["repeat_rule"] == "weekday"


def test_detect_tool_routes_recurring_reminders():
    enabled = {"remind": 1}
    assert tools.detect_tool("每天8点提醒我喝水", enabled) == ("remind", {"text": "喝水", "when": "每天8点"})
    assert tools.detect_tool("工作日上午9点提醒我打卡", enabled) == ("remind", {"text": "打卡", "when": "工作日上午9点"})
    assert tools.detect_tool("每月31号提醒我交房租", enabled) == ("remind", {"text": "交房租", "when": "每月31号"})
