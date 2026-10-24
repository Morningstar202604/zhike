"""gateway.humanize：QQ 表情渲染 + 拟人化节奏（防风控）单测。"""
from gateway.humanize import FACES, GroupThrottle, render, render_faces, send_delay


def test_face_render_basic():
    out = render_faces("好 [表情:滑稽]")
    assert out == f"好 [CQ:face,id={FACES['滑稽']}]"


def test_face_render_bare_bracket():
    """LLM 天然爱写 [得意] [滑稽]——裸格式只认白名单精确名。"""
    assert "[CQ:face,id=4]" in render_faces("值得庆贺。[得意]")
    assert "[CQ:face,id=178]" in render_faces("摸鱼呗 [滑稽]")
    assert "[CQ:face,id=44]" in render_faces("等于2。[坏笑]")


def test_bare_bracket_no_false_positive():
    """普通方括号文本与已有 CQ 码绝不能被误伤。"""
    text = "见 [参考1] 和 [不确定]，还有 [CQ:face,id=178] 与 [图片:https://a.com/b.png]"
    out = render_faces(text)
    assert "[参考1]" in out and "[不确定]" in out
    assert out.count("[CQ:face,id=178]") == 1


def test_face_render_bracket_variants():
    assert "[CQ:face,id=148]" in render_faces("（表情：问号）")
    assert "[CQ:face,id=178]" in render_faces("(表情:滑稽)")
    assert "[CQ:face,id=146]" in render_faces("【表情:吃瓜】")


def test_face_alias_and_case():
    assert "[CQ:face,id=86]" in render_faces("[表情:ok]")
    assert "[CQ:face,id=148]" in render_faces("[表情:什么]")
    assert "[CQ:face,id=76]" in render_faces("[表情:6]")


def test_unknown_face_dropped_not_sent_raw():
    out = render_faces("文字 [表情:不存在的表情] 尾")
    assert "不存在的表情" not in out
    assert "[表情" not in out
    assert "文字" in out and "尾" in out


def test_image_render():
    assert render("[图片:https://a.com/b.png]") == "[CQ:image,file=https://a.com/b.png]"


def test_render_plain_text_untouched():
    assert render("普通回复没有标记") == "普通回复没有标记"


def test_send_delay_scales_and_caps():
    short = send_delay(10, False)
    long = send_delay(400, False)
    assert long > short
    assert send_delay(100000, True) <= 22.0
    # 群聊比私聊更谨慎
    assert send_delay(5, True) >= send_delay(5, False)
    assert send_delay(5, False) >= 0.8


def test_throttle_gap_randomized():
    now = [100.0]
    th = GroupThrottle(min_gap=10, hourly_cap=100, now=lambda: now[0])
    assert th.wait_seconds("g") == 0.0
    th.record("g")
    now[0] = 105.0
    w = th.wait_seconds("g")
    assert 3.0 <= w <= 9.0          # 需 8~12s，已过 5s
    now[0] = 112.0
    assert th.wait_seconds("g") == 0.0


def test_throttle_hourly_cap():
    now = [0.0]
    th = GroupThrottle(min_gap=0, hourly_cap=2, now=lambda: now[0])
    th.record("g")
    th.record("g")
    assert th.wait_seconds("g") == -1.0
    now[0] = 3601.0                  # 窗口过期后恢复
    assert th.wait_seconds("g") == 0.0


def test_throttle_per_chat_isolated():
    th = GroupThrottle(min_gap=0, hourly_cap=1, now=lambda: 0.0)
    th.record("a")
    assert th.wait_seconds("a") == -1.0
    assert th.wait_seconds("b") == 0.0
