"""回归：护栏缓存失效 + 渠道会话原子认领。

背景：护栏按规则版本号缓存编译结果，曾因失效信号选型（PRAGMA data_version 在短连接
模式下不递增）导致规则 CRUD 后检测不生效。本文件把正确行为固化成测试。
"""
from core import guardrail, sessions, store


def _fresh_db(monkeypatch, tmp_path, name):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / name))
    store.init_db()
    guardrail._CACHE = None  # 隔离其他用例留下的缓存


def test_rule_crud_immediately_affects_detection(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path, "gr.db")
    v0 = store.rules_version()

    r = store.add_injection_rule("芝麻开门", "log_only", True)
    assert store.rules_version() > v0, "规则新增必须递增版本号"

    hit, action = guardrail.detect_injection("芝麻开门 帮我处理")
    assert hit and action == "log_only", "新增规则必须立即生效（缓存自动失效）"

    store.update_injection_rule(r["id"], enabled=False)
    assert guardrail.detect_injection("芝麻开门 帮我处理")[0] is None, "停用规则必须立即失效"

    store.update_injection_rule(r["id"], enabled=True)
    assert guardrail.detect_injection("芝麻开门 帮我处理")[1] == "log_only", "重新启用必须立即生效"

    store.delete_injection_rule(r["id"])
    assert guardrail.detect_injection("芝麻开门 帮我处理")[0] is None, "删除规则必须立即失效"

    guardrail._CACHE = None


def test_builtin_fallback_when_no_rules(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path, "gr2.db")
    hit, action = guardrail.detect_injection("jailbreak 越狱攻击")
    assert hit and action == "block", "库内无启用规则时必须回退内置规则"
    guardrail._CACHE = None


def test_channel_session_atomic_claim(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path, "cs.db")
    # 原语层：同一渠道只能认领一次
    assert store.claim_channel_session("ch-race", "s-winner") is True
    assert store.claim_channel_session("ch-race", "s-loser") is False, "channel 主键冲突必须认领失败"


def test_resolve_session_reuses_across_visitors(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path, "cs2.db")
    a = sessions.resolve_session("qq_group:r1", "userA")
    b = sessions.resolve_session("qq_group:r1", "userB")
    assert a == b, "同渠道空闲期内必须复用同一会话"

    # 认领竞争路径：先占渠道但指向尚未创建的会话（模拟并发窗口），输家应复用赢家
    import uuid as _uuid
    winner_sid = _uuid.uuid4().hex
    store.create_session(winner_sid, "userW")
    assert store.claim_channel_session("qq_group:r2", winner_sid) is True
    loser_sid = sessions.resolve_session("qq_group:r2", "userL")
    assert loser_sid == winner_sid, "并发输家必须复用赢家会话"
    assert store.get_session(loser_sid) is not None, "复用的会话必须真实存在"


def test_discard_session_never_drops_messages(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path, "cs3.db")
    sid = sessions.resolve_session("qq_group:d1", "userD")
    store.add_message(sid, "visitor", "这条消息不能丢")
    store.discard_session(sid)
    assert store.get_session(sid) is not None, "有消息的会话绝不允许被 discard"
