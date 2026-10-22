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
