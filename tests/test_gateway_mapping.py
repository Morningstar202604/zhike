from gateway.mapping import from_onebot_v11, ingest_payload, reply_segments


def _group_event(segments, self_id=1000, user_id=456):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": 88123,
        "user_id": user_id,
        "self_id": self_id,
        "message": segments,
        "raw_message": "",
    }


def test_group_at_message_maps():
    ev = from_onebot_v11(
        _group_event(
            [
                {"type": "at", "data": {"qq": "1000"}},
                {"type": "text", "data": {"text": " 订单什么时候发货"}},
            ]
        )
    )
    assert ev is not None
    assert ev.should_reply is True
    assert ev.channel_type == "qq_group"
    assert ev.chat_id == "88123"
    assert ev.user_id == "456"
    assert ev.text == "订单什么时候发货"


def test_group_without_at_ignored():
    ev = from_onebot_v11(_group_event([{"type": "text", "data": {"text": "随便聊聊"}}]))
    assert ev is None


def test_group_at_stripped_by_adapter_still_maps():
    """适配器 _check_at_me 命中后会弹出 at 段；bot.py 以事件级 to_me 兜底，群聊不得被误丢。"""
    ev = from_onebot_v11(
        {**_group_event([{"type": "text", "data": {"text": "这个图片是什么"}}]), "to_me": True}
    )
    assert ev is not None, "to_me=True 的群聊消息即使 at 段已被剥离也必须通过"
    assert ev.channel_type == "qq_group" and ev.text == "这个图片是什么"


def test_group_image_only_with_to_me_maps():
    """纯图片群聊（@ 已剥离）：文本占位 [图片] + 图片 URL，进入转人工/视觉链路。"""
    ev = from_onebot_v11(
        {
            **_group_event([{"type": "image", "data": {"url": "https://gchat.qpic.cn/a.png"}}]),
            "to_me": True,
        }
    )
    assert ev is not None
    assert ev.image_url == "https://gchat.qpic.cn/a.png"
    assert ev.should_reply is True, "纯图片也要能触发回复（转人工或视觉应答）"


def test_private_message_always_replies():
    ev = from_onebot_v11(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 456,
            "self_id": 1000,
            "message": [{"type": "text", "data": {"text": "怎么开发票"}}],
        }
    )
    assert ev is not None
    assert ev.should_reply is True
    assert ev.channel_type == "qq_private"


def test_ignore_self_message():
    ev = from_onebot_v11(_group_event([{"type": "text", "data": {"text": "hi"}}], user_id=1000))
    assert ev is None


def test_non_message_post_ignored():
    assert from_onebot_v11({"post_type": "meta_event", "self_id": 1000}) is None


def test_string_message_fallback():
    ev = from_onebot_v11(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 456,
            "self_id": 1000,
            "message": "[CQ:at,qq=1000] 怎么开发票",
            "raw_message": "[CQ:at,qq=1000] 怎么开发票",
        }
    )
    assert ev is not None
    assert "怎么开发票" in ev.text


def test_ingest_payload_shape():
    ev = from_onebot_v11(
        _group_event(
            [
                {"type": "at", "data": {"qq": "1000"}},
                {"type": "text", "data": {"text": "退货政策是什么"}},
            ]
        )
    )
    payload = ingest_payload(ev)
    assert payload["channel_type"] == "qq_group"
    assert payload["channel_user_id"] == "456"
    assert payload["group_id"] == "88123"
    assert payload["text"] == "退货政策是什么"


def test_reply_segments_splits_long_text():
    parts = reply_segments("字" * 6000)
    assert len(parts) == 2
    assert all(len(p) <= 4500 for p in parts)
    assert "".join(parts) == "字" * 6000


def test_reply_segments_short_text_single():
    assert reply_segments("正常回复") == ["正常回复"]


def test_image_url_extracted_from_segments():
    ev = from_onebot_v11(
        _group_event(
            [
                {"type": "at", "data": {"qq": "1000"}},
                {"type": "text", "data": {"text": "帮我看下这个截图"}},
                {"type": "image", "data": {"url": "https://example.com/a.jpg"}},
            ]
        )
    )
    assert ev is not None
    assert "[图片]" in ev.text
    assert ev.image_url == "https://example.com/a.jpg"


def test_image_url_extracted_from_cq_string():
    ev = from_onebot_v11(
        _group_event(
            "[CQ:at,qq=1000] 看图 [CQ:image,file=abc.jpg,url=https://gchat.qpic.cn/a?term=2&amp;is_origin=0]"
        )
    )
    assert ev is not None
    assert ev.image_url == "https://gchat.qpic.cn/a?term=2&is_origin=0", "CQ 码转义的 &amp; 必须还原"
    assert ev.image_url not in ev.text


def test_ingest_payload_has_no_image_field():
    ev = from_onebot_v11(_group_event([{"type": "at", "data": {"qq": "1000"}}, {"type": "text", "data": {"text": "hi"}}]))
    payload = ingest_payload(ev)
    assert "image_b64" not in payload, "image_b64 由网关拉取后单独附加，映射层不携带"

# ---------------- 网关稳定性（去重 / 串行 / 重试） ----------------

def test_gateway_dedup_drops_replayed_message():
    """协议端重连会重放最近事件；同一 message_id 只能处理一次。"""
    from gateway import bot as gw_bot

    gw_bot._SEEN_IDS.clear()
    assert gw_bot._dedup("m-100") is True
    assert gw_bot._dedup("m-100") is False
    assert gw_bot._dedup(None) is True


def test_gateway_dedup_lru_bounded():
    from gateway import bot as gw_bot

    gw_bot._SEEN_IDS.clear()
    for i in range(gw_bot._SEEN_MAX + 50):
        gw_bot._dedup(f"m-{i}")
    assert len(gw_bot._SEEN_IDS) == gw_bot._SEEN_MAX, "去重表必须有界，否则长跑会吃满内存"


def test_gateway_chat_lock_is_per_chat():
    """同一会话拿到同一把锁（保证顺序），不同会话互不影响（保证并发）。"""
    import asyncio
    from gateway import bot as gw_bot

    gw_bot._PER_CHAT_LOCKS.clear()
    async def check():
        a1 = gw_bot._chat_lock("qq_group:1")
        a2 = gw_bot._chat_lock("qq_group:1")
        b1 = gw_bot._chat_lock("qq_group:2")
        return a1 is a2, a1 is b1
    same, distinct = asyncio.run(check())
    assert same is True
    assert distinct is False


def test_face_and_mface_become_readable_tokens():
    """表情段以前被直接丢弃，模型只看到空文本 —— 这是"表情识别不了"的根因。"""
    from gateway.mapping import from_onebot_v11

    ev = from_onebot_v11({
        "post_type": "message", "message_type": "group", "self_id": "1",
        "user_id": "2", "group_id": "9", "to_me": True, "message_id": "m1",
        "sender": {"nickname": "小明"},
        "message": [
            {"type": "text", "data": {"text": "笑死"}},
            {"type": "face", "data": {"id": "13"}},
            {"type": "mface", "data": {"summary": "[猫猫头]"}},
        ],
    })
    assert ev is not None
    assert "笑死" in ev.text
    assert "[表情:呲牙]" in ev.text
    assert "[表情:猫猫头]" in ev.text


def test_cq_string_face_is_converted():
    from gateway.mapping import from_onebot_v11

    ev = from_onebot_v11({
        "post_type": "message", "message_type": "group", "self_id": "1",
        "user_id": "2", "group_id": "9", "to_me": True, "message_id": "m2",
        "sender": {"nickname": "小明"},
        "message": "好啊[CQ:face,id=14]",
    })
    assert ev is not None and "[表情:微笑]" in ev.text


def test_voice_only_message_is_not_dropped():
    """只有语音没有文字的消息必须放行：否则「发条语音问事」永远进不了管线。"""
    from gateway.mapping import from_onebot_v11

    ev = from_onebot_v11({
        "post_type": "message", "message_type": "private", "self_id": "1000",
        "user_id": "2", "message_id": 9,
        "message": [{"type": "record", "data": {"url": "https://x.cn/a.amr"}}],
    })
    # 文本里保留 [语音] 占位（网关转写成功后用它替换），record_url 单独带出去
    assert ev is not None and ev.text == "[语音]" and ev.record_url == "https://x.cn/a.amr"
    assert ev.should_reply is True


def test_voice_url_from_cq_string():
    from gateway.mapping import from_onebot_v11

    ev = from_onebot_v11({
        "post_type": "message", "message_type": "private", "self_id": "1000",
        "user_id": "2", "message_id": 10,
        "message": "[CQ:record,file=a.amr,url=https://x.cn/a.amr]",
    })
    assert ev is not None and "x.cn/a.amr" in ev.record_url


def test_empty_message_still_dropped():
    from gateway.mapping import from_onebot_v11

    assert from_onebot_v11({
        "post_type": "message", "message_type": "private", "self_id": "1000",
        "user_id": "2", "message_id": 11, "message": [{"type": "text", "data": {"text": "  "}}],
    }) is None


# ---------------- 连接池复用 / 核心掉线补投 ----------------

def test_gateway_reuses_pooled_http_clients():
    """同循环 + 同超时要拿到同一个客户端（每请求新建连接是重复造轮子）。"""
    from gateway import bot as gw_bot

    a = gw_bot.http_pool.get_client(timeout=15)
    b = gw_bot.http_pool.get_client(timeout=15)
    c = gw_bot.http_pool.get_client(timeout=90)
    assert a is b, "同参数必须复用同一个客户端"
    assert a is not c, "不同超时必须分桶（timeout 在 key 里）"


def test_ingest_failure_schedules_exactly_one_retry(monkeypatch):
    """核心短暂不可达时安排一次延迟补投，而不是把消息悄悄丢掉。"""
    import asyncio
    import types

    import httpx

    from gateway import bot as gw_bot

    msg = types.SimpleNamespace(channel="qq_group", chat_id="123",
                                is_group=True, message_id="m-retry-1")

    async def boom(payload):
        raise httpx.ConnectError("core down")

    monkeypatch.setattr(gw_bot, "_post_ingest", boom)
    monkeypatch.setattr(gw_bot, "_INGEST_RETRY_DELAY_S", 0.0)

    created = []

    def fake_create_task(coro):
        created.append(coro)
        coro.close()  # 不真正执行，避免测试里等待 30s/触发真实发送
        return None

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    asyncio.run(gw_bot._deliver({"text": "hi"}, msg, "m-retry-1", retry_left=1))
    assert len(created) == 1, "失败后应安排恰好一次补投"

    created.clear()
    asyncio.run(gw_bot._deliver({"text": "hi"}, msg, "m-retry-1", retry_left=0))
    assert created == [], "retry_left=0（补投自身失败）不得再排新任务"


def test_ingest_retry_queue_is_bounded(monkeypatch):
    """补投并发有上限：核心长时间挂掉时任务数不能随掉线消息线性膨胀。"""
    import types

    from gateway import bot as gw_bot

    msg = types.SimpleNamespace(channel="qq_group", chat_id="cap",
                                is_group=True, message_id="m-cap")
    monkeypatch.setattr(gw_bot, "_ingest_retry_tasks", gw_bot._INGEST_RETRY_MAX_TASKS)

    called = []

    def fake_create_task(coro):
        called.append(coro)
        coro.close()
        return None

    import asyncio as _aio
    orig = _aio.create_task
    _aio.create_task = fake_create_task
    try:
        gw_bot._schedule_retry({"text": "x"}, msg, None, 0)
    finally:
        _aio.create_task = orig
    assert called == [], "队列满时不得再创建补投任务"


def test_preflight_probe_sends_channel_token(monkeypatch):
    """启动自检的 token 探测必须带上 X-Channel-Token：
    core 对无令牌请求一律 401，裸探测会把带着正确令牌的网关也误杀（真实踩过）。"""

    from gateway import bot as gw_bot

    posted = {}

    class R:
        def __init__(self, status_code):
            self.status_code = status_code

    def fake_get(url, timeout):
        return R(200)  # healthz 正常

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        posted["headers"] = headers or {}
        return R(422)  # 空载荷校验错误 ≠ 令牌错误

    monkeypatch.setattr(gw_bot.httpx, "get", fake_get)
    monkeypatch.setattr(gw_bot.httpx, "post", fake_post)
    monkeypatch.setattr(gw_bot, "CHANNEL_TOKEN", "test-channel-token")

    try:
        gw_bot._preflight_core()
    except SystemExit as e:
        raise AssertionError(f"preflight 不应退出，code={e.code}")

    assert posted["headers"].get("X-Channel-Token") == "test-channel-token", \
        "探测请求必须带 X-Channel-Token"
