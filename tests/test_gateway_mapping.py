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
