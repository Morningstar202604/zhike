"""网关乒乓循环护栏：对面是机器人就闭嘴（道别复读 / 回声 / 问题连发 / 对刷超量）。
"""
import json

from gateway.loop_guard import (
    LoopGuard,
    containment,
    is_bot_name,
    is_closing,
    is_question,
    is_trivial,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def _guard(clock, **kw):
    return LoopGuard(None, now=clock, **kw)


def _turn(g, clock, text, *, chat="qq_group:1", peer="bot2", name="路人",
          reply_to=None, gap=60.0):
    """模拟一轮：我们先发出上一句，隔 gap 秒后对面回过来。"""
    g.record_outgoing(chat, reply_to or f"out-{clock.t:.0f}", "上一句")
    clock.tick(gap)
    if reply_to:
        text = f"[引用:{reply_to}]\n{text}"
    return g.inspect(channel=chat.split(":")[0], chat_id=chat.split(":")[1],
                     peer_id=peer, text=text, name=name, is_group=True)


# ---------------- 纯函数 ----------------

def test_closing_and_question_detection():
    assert is_closing("晚安，睡了")
    assert is_closing("早点睡吧")
    assert not is_closing("今天吃了什么")
    assert is_question("你在吗？")
    assert is_question("这是什么")
    assert not is_question("我吃过了")


def test_bot_name_detection():
    assert is_bot_name("XXBot")
    assert is_bot_name("自动回复小助手")
    assert is_bot_name("ChatGPT")
    assert not is_bot_name("零&雪/彼岸双生")


def test_containment_measures_echo():
    assert containment("今天真的好累啊", "今天真的好累") > 0.9
    assert containment("完全无关的内容", "今天真的好累") == 0.0


# ---------------- 三道主判据 ----------------

def test_silent_on_normal_chat():
    """正常闲聊连说十句不该被静音——误杀比漏杀更伤体验。"""
    c = Clock()
    g = _guard(c)
    lines = ["今天好累啊", "老板又加班", "晚饭吃啥", "你说房价还会涨吗",
             "帮我看下这个报错", "Python 的 GIL 是什么", "算了不问了", "睡了先"]
    muted = [bool(_turn(g, c, t).muted) for t in lines]
    assert not any(muted), muted


def test_closing_loop_fires_after_repeated_goodnights():
    """实测场景：两个 bot 互道晚安 40+ 轮，每轮约 55 秒。第 4 轮就该闭嘴。"""
    c = Clock()
    g = _guard(c)
    lines = ["行，回笼觉睡好，晚安", "知道了，快睡吧，晚安", "拜师就免了，晚安",
             "真睡吧，晚安，明早见"]
    muted = []
    for i, t in enumerate(lines):
        muted.append(_turn(g, c, t, reply_to=f"m{i}", gap=55).muted)
    assert muted[:3] == [False, False, False], muted
    assert muted[3] is True, "连续第四条回我们的道别语必须触发静默"


def test_closing_without_quotes_needs_more_turns():
    """看不到引用时降低置信度：得 6 条才判（避免把普通群聊的晚安接龙误杀）。"""
    c = Clock()
    g = _guard(c)
    lines = ["晚安"] * 8
    muted = [_turn(g, c, t, gap=50).muted for t in lines]
    assert muted[:5] == [False] * 5, muted
    assert muted[5] is True


def test_echo_loop_fires():
    """对面开始复读我们的话（bigram 高度重合）。"""
    c = Clock()
    g = _guard(c)
    g.record_outgoing("qq_group:1", "m1", "本喵耳朵尾巴一起盖被子装死")
    c.tick(30)
    assert not g.inspect(channel="qq_group", chat_id="1", peer_id="p",
                         text="耳朵尾巴一起盖被子装死", name="x", is_group=True).muted
    g.record_outgoing("qq_group:1", "m2", "本喵耳朵尾巴一起盖被子装死")
    c.tick(30)
    assert not g.inspect(channel="qq_group", chat_id="1", peer_id="p",
                         text="耳朵尾巴一起盖被子装死", name="x", is_group=True).muted
    g.record_outgoing("qq_group:1", "m3", "本喵耳朵尾巴一起盖被子装死")
    c.tick(30)
    assert g.inspect(channel="qq_group", chat_id="1", peer_id="p",
                     text="耳朵尾巴一起盖被子装死", name="x", is_group=True).muted


def test_question_flood_fires():
    """连续追问，且句句在回我们：典型的机器人心跳式提问。"""
    c = Clock()
    g = _guard(c)
    muted = []
    for i in range(7):
        muted.append(_turn(g, c, f"[引用:m{i}] 这是什么？", reply_to=f"m{i}", gap=20).muted)
    assert muted[:5] == [False] * 5, muted
    assert muted[5] is True, muted


def test_question_flood_slower_but_longer():
    """问得慢一点（40s 一轮）也不放过：攒到 10 条照样判给机器人。"""
    c = Clock()
    g = _guard(c)
    muted = []
    for i in range(11):
        muted.append(_turn(g, c, f"第{i}个问题是什么？", gap=40).muted)
    assert muted[:9] == [False] * 9, muted
    assert muted[9] is True, muted


def test_long_chatty_human_is_not_muted():
    """连聊 20 多轮、但每句都在问正经事：这是真人，不是对刷。"""
    c = Clock()
    g = _guard(c)
    muted = []
    for i in range(26):
        muted.append(_turn(g, c, f"第{i}个问题想请教一下，这个参数应该怎么配", reply_to=f"m{i}", gap=40).muted)
    assert not any(muted), muted


def test_rapid_pingpong_fires():
    """秒回连击：每次都 3 秒内回一句 20 字左右，真人打不了这么快。"""
    c = Clock()
    g = _guard(c)
    muted = []
    for i in range(13):
        muted.append(_turn(g, c, f"收到，第{i}条消息已经处理完毕", reply_to=f"m{i}", gap=3).muted)
    assert muted[-1] is True, muted
    assert muted[:10] == [False] * 10, muted


def test_fast_human_typing_not_muted():
    """真人打字快：8 秒回一句、40 字以上——不能因为「快」就判机器人。"""
    c = Clock()
    g = _guard(c)
    for i in range(20):
        long_text = f"第{i}条，我把这个问题的来龙去脉都说一遍：" + "因为所以" * 8
        assert not _turn(g, c, long_text, reply_to=f"m{i}", gap=8).muted, i


def test_trivial_punctuation_loop_fires():
    """对刷到词穷：连续三条只剩「。」「……」「。」——真人也干不出这事。"""
    c = Clock()
    g = _guard(c)
    muted = [_turn(g, c, t, gap=40).muted for t in ("。", "……", "。")]
    assert muted == [False, False, True], muted
    assert g.mutes()[0]["reason"] == "trivial_loop"


def test_two_char_replies_are_normal_chat():
    """「你好」「晚安」「在吗」是正常聊天，不能被当成复读（主人 128 轮里出现过这种连发）。"""
    c = Clock()
    g = _guard(c)
    for t in ("你好", "在吗", "晚安", "嗯嗯"):
        assert not _turn(g, c, t, gap=40).muted, t


def test_trivial_run_broken_by_real_message():
    c = Clock()
    g = _guard(c)
    assert not _turn(g, c, "。", gap=40).muted
    assert not _turn(g, c, "。", gap=40).muted
    assert not _turn(g, c, "帮我算一下 128*37", gap=40).muted
    assert not _turn(g, c, "。", gap=40).muted, "被正常消息打断后重新计数"


def test_bot_nickname_alone_is_not_enough_but_repeat_is():
    """昵称带 bot 且连续搭话三轮才静音（只凭昵称会误杀叫 bot 的真人）。"""
    c = Clock()
    g = _guard(c)
    v1 = _turn(g, c, "你好", name="小bot", reply_to="m1", gap=60)
    assert not v1.muted
    _turn(g, c, "在吗", name="小bot", reply_to="m2", gap=60)
    v3 = _turn(g, c, "说话", name="小bot", reply_to="m3", gap=60)
    assert v3.muted and v3.reason == "bot_name"


# ---------------- 静默期与恢复 ----------------

def test_mute_covers_follow_up_messages_without_recount():
    """静默期内后续消息一律不回复，且不需要重新触发判定。"""
    c = Clock()
    g = _guard(c, mute_s=60)
    for i in range(4):
        _turn(g, c, "晚安", reply_to=f"m{i}", gap=55)
    assert g.mutes()
    before = len(g._state)
    v = _turn(g, c, "随便说点什么", gap=1)
    assert v.muted and v.reason == "closing_loop"
    assert len(g._state) == before, "静默期不该继续累积状态"


def test_mute_expires_and_peer_can_talk_again():
    c = Clock()
    g = _guard(c, mute_s=60)
    for i in range(4):
        _turn(g, c, "晚安", reply_to=f"m{i}", gap=50)
    assert _turn(g, c, "在吗", gap=1).muted
    c.tick(120)
    assert not _turn(g, c, "今天天气不错", gap=1).muted


def test_repeat_offender_gets_longer_mute():
    c = Clock()
    g = _guard(c, mute_s=600)
    for i in range(4):
        _turn(g, c, "晚安", reply_to=f"m{i}", gap=50)
    first = g.mutes()[0]["until"] - c.t
    c.tick(601)
    for i in range(6):
        _turn(g, c, "晚安", reply_to=f"x{i}", gap=50)
    second = g.mutes()[0]["until"] - c.t
    assert second > first, (first, second)


def test_manual_unmute_and_listing():
    c = Clock()
    g = _guard(c)
    g.mute("qq_group:100000002", "100000003")
    items = g.mutes()
    assert items and items[0]["chat"] == "qq_group:100000002"
    assert items[0]["peer"] == "100000003"
    assert g.unmute("qq_group:100000002", "100000003") is True
    assert g.mutes() == []
    assert g.unmute("qq_group:100000002", "100000003") is False


def test_owner_private_chat_not_muted_by_volume():
    """私聊连问十几个问题很常见（主人测功能），不能因此静音。"""
    c = Clock()
    g = _guard(c)
    for i in range(30):
        v = g.inspect(channel="qq_private", chat_id="100000001", peer_id="100000001",
                      text=f"第{i}个问题：这个怎么弄？", name="上小鸟", is_group=False)
        assert not v.muted, (i, v.reason)


def test_state_persists_across_restart(tmp_path):
    """静默期落盘：网关重启后不会立刻又跟对面刷起来。"""
    path = tmp_path / "loop_guard.json"
    c = Clock()
    g = LoopGuard(path, now=c)
    g.mute("qq_group:1", "bot2", reason="closing_loop")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "qq_group:1:bot2" in saved["mutes"]

    g2 = LoopGuard(path, now=c)
    assert g2.mutes() and g2.mutes()[0]["reason"] == "closing_loop"


def test_state_file_survives_garbage(tmp_path):
    path = tmp_path / "loop_guard.json"
    path.write_text("not json", encoding="utf-8")
    g = LoopGuard(path, now=Clock())
    assert g.mutes() == []
