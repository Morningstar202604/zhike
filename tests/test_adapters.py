"""阶段 4：适配器接口 / 事件总线 / 插件清单 回归。"""
import asyncio
import json

import pytest

from gateway.adapters import (
    CAP_GROUP_ADMIN, CAP_SEND_IMAGE, CAP_SEND_TEXT,
    NormalizedMessage, OneBot11Adapter, UnsupportedCapability, require_capability,
    adapter_info, bus, registry,
)
from gateway.adapters.base import ALL_CAPABILITIES
from gateway.adapters.bus import AdapterRegistry, EventBus, EV_MESSAGE_IN
from gateway.adapters.mock import MockChannelAdapter
from gateway.adapters.plugins import PluginError, PluginManifest, PluginRegistry


# ---------------- NormalizedMessage ----------------

def test_normalized_message_properties():
    m = NormalizedMessage(channel="qq_group", chat_id="123", sender_id="456", text="hi")
    assert m.is_group
    assert m.has_content
    assert m.identity == "qq_group:456"
    assert m.session_key == "qq_group:123"


def test_private_message_not_group():
    m = NormalizedMessage(channel="qq_private", chat_id="1", sender_id="1", text="x")
    assert not m.is_group


def test_empty_content_detected():
    assert not NormalizedMessage(channel="webchat", chat_id="1", sender_id="1").has_content
    m = NormalizedMessage(channel="webchat", chat_id="1", sender_id="1", images=["x"])
    assert m.has_content, "有图片也算有内容"


# ---------------- OneBot 适配器 ----------------

def _group_event(**over):
    ev = {
        "post_type": "message", "message_type": "group", "group_id": 88123,
        "user_id": 456, "self_id": 1000, "to_me": True,
        "message": [{"type": "text", "data": {"text": "怎么开发票"}}],
        "sender": {"nickname": "小明"},
    }
    ev.update(over)
    return ev


def test_onebot_parse_group_message():
    a = OneBot11Adapter()
    m = a.parse(_group_event())
    assert m is not None
    assert m.channel == "qq_group"
    assert m.chat_id == "88123"
    assert m.sender_id == "456"
    assert m.text == "怎么开发票"
    assert m.display_name == "小明"


def test_onebot_parse_ignores_non_message():
    a = OneBot11Adapter()
    assert a.parse({"post_type": "meta_event"}) is None


def test_onebot_parse_ignores_self():
    a = OneBot11Adapter()
    assert a.parse(_group_event(user_id=1000)) is None, "自己发的要忽略"


def test_onebot_parse_group_without_at():
    a = OneBot11Adapter()
    ev = _group_event(to_me=False, message=[{"type": "text", "data": {"text": "随便聊聊"}}])
    assert a.parse(ev) is None, "群里没 @ 的不理"


def test_onebot_parse_image_only():
    a = OneBot11Adapter()
    ev = {
        "post_type": "message", "message_type": "private", "user_id": 456, "self_id": 1000,
        "message": [{"type": "image", "data": {"url": "https://example.com/a.png"}}],
    }
    m = a.parse(ev)
    assert m is not None
    assert m.images == ["https://example.com/a.png"]
    assert m.has_content


def test_onebot_capabilities_include_core():
    a = OneBot11Adapter()
    caps = a.capabilities()
    assert CAP_SEND_TEXT in caps
    assert CAP_SEND_IMAGE in caps


def test_onebot_send_without_bot_fails_cleanly():
    a = OneBot11Adapter()
    m = NormalizedMessage(channel="qq_group", chat_id="1", sender_id="1", text="x")
    r = asyncio.run(a.send(m, "hello"))
    assert r.ok is False
    assert "未连接" in (r.error or "")


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_api(self, api, **kw):
        self.calls.append((api, kw))
        return {"message_id": len(self.calls)}


def test_onebot_send_uses_group_api():
    bot = FakeBot()
    a = OneBot11Adapter(bot)
    m = NormalizedMessage(channel="qq_group", chat_id="88123", sender_id="1", text="x")
    r = asyncio.run(a.send(m, "回复内容"))
    assert r.ok and r.message_id == "1"
    assert bot.calls[0][0] == "send_group_msg"
    assert bot.calls[0][1]["group_id"] == 88123, "数字 id 应转 int"
    assert bot.calls[0][1]["message"] == "回复内容"


def test_onebot_send_uses_private_api():
    bot = FakeBot()
    a = OneBot11Adapter(bot)
    m = NormalizedMessage(channel="qq_private", chat_id="456", sender_id="456", text="x")
    asyncio.run(a.send(m, "hi"))
    assert bot.calls[0][0] == "send_private_msg"


def test_onebot_long_message_split():
    bot = FakeBot()
    a = OneBot11Adapter(bot, max_chars=10)
    m = NormalizedMessage(channel="qq_group", chat_id="1", sender_id="1", text="x")
    asyncio.run(a.send(m, "字" * 25))
    assert len(bot.calls) == 3, "超长消息应分段发送"
    assert all(len(c[1]["message"]) <= 10 for c in bot.calls)


def test_onebot_send_error_reported():
    class BrokenBot:
        async def call_api(self, api, **kw):
            raise RuntimeError("协议端断开")

    a = OneBot11Adapter(BrokenBot())
    m = NormalizedMessage(channel="qq_group", chat_id="1", sender_id="1", text="x")
    r = asyncio.run(a.send(m, "x"))
    assert r.ok is False and "协议端断开" in (r.error or "")


# ---------------- 能力校验 ----------------

def test_require_capability_passes():
    a = OneBot11Adapter()
    require_capability(a, CAP_SEND_TEXT)


def test_require_capability_raises():
    a = MockChannelAdapter()
    with pytest.raises(UnsupportedCapability):
        require_capability(a, CAP_GROUP_ADMIN)


def test_adapter_info_shape():
    info = adapter_info(OneBot11Adapter())
    assert info["name"] == "onebot_v11"
    assert set(info["supports"]) == ALL_CAPABILITIES


# ---------------- 事件总线 ----------------

def test_bus_delivers_to_subscriber():
    b = EventBus()
    got = []

    async def handler(ev):
        got.append(ev)

    b.subscribe(EV_MESSAGE_IN, handler)
    n = asyncio.run(b.publish(EV_MESSAGE_IN, {"text": "hi"}))
    assert n == 1
    assert got[0]["payload"]["text"] == "hi"


def test_bus_isolates_failures():
    b = EventBus()
    ok = []

    async def bad(ev):
        raise RuntimeError("订阅者崩了")

    async def good(ev):
        ok.append(1)

    b.subscribe(EV_MESSAGE_IN, bad)
    b.subscribe(EV_MESSAGE_IN, good)
    asyncio.run(b.publish(EV_MESSAGE_IN))
    assert ok == [1], "一个订阅者失败不能影响其他"
    assert b.errors() and b.errors()[0]["event"] == EV_MESSAGE_IN


def test_bus_wildcard():
    b = EventBus()
    got = []

    async def h(ev):
        got.append(ev["type"])

    b.subscribe("*", h)
    asyncio.run(b.publish(EV_MESSAGE_IN))
    assert got == [EV_MESSAGE_IN]


def test_bus_rejects_unknown_event():
    with pytest.raises(ValueError):
        EventBus().subscribe("nonsense.event", lambda e: None)


# ---------------- 适配器注册表 ----------------

def test_registry_register_and_get():
    r = AdapterRegistry()
    a = OneBot11Adapter()
    r.register(a)
    assert r.get("onebot_v11") is a
    assert r.names() == ["onebot_v11"]


def test_registry_rejects_duplicate():
    r = AdapterRegistry()
    r.register(OneBot11Adapter())
    with pytest.raises(ValueError):
        r.register(OneBot11Adapter())


def test_registry_require_missing():
    with pytest.raises(KeyError):
        AdapterRegistry().require("wecom")


def test_registry_find_by_capability():
    r = AdapterRegistry()
    r.register(OneBot11Adapter())
    r.register(MockChannelAdapter())
    senders = r.find_by_capability(CAP_SEND_TEXT)
    assert set(senders) == {"onebot_v11", "mock"}
    admins = r.find_by_capability(CAP_GROUP_ADMIN)
    assert admins == [], "mock 没声明群管理，不应出现"


def test_registry_describe():
    r = AdapterRegistry()
    r.register(OneBot11Adapter())
    d = r.describe()
    assert d[0]["name"] == "onebot_v11" and "capabilities" in d[0]


# ---------------- 插件清单 ----------------

def test_manifest_parses_permissions():
    m = PluginManifest.from_dict({
        "name": "wecom-kf", "version": "0.1.0",
        "provides": ["channel:wecom"], "requires": ["channel.send"],
        "permissions": {"actions": ["send_msg"], "fs": [], "shell": []},
    })
    assert m.allowed_actions() == {"send_msg"}
    assert m.allowed_shell() == set()


def test_manifest_requires_name():
    with pytest.raises(PluginError):
        PluginManifest.from_dict({"version": "1"})


def test_manifest_rejects_bad_trust():
    with pytest.raises(PluginError):
        PluginManifest.from_dict({"name": "x", "trust": "root"})


def test_plugin_registry_load_and_check():
    reg = PluginRegistry()
    m = PluginManifest.from_dict({"name": "p1", "permissions": {"actions": ["send_msg"]}})
    reg.load(m)
    reg.check_action("p1", "send_msg")  # 已声明，通过

    with pytest.raises(PluginError):
        reg.check_action("p1", "set_group_kick")
    assert reg.denials()[0]["action"] == "set_group_kick", "越权尝试要留痕"


def test_plugin_registry_unknown_plugin():
    with pytest.raises(PluginError):
        PluginRegistry().check_action("nope", "send_msg")


def test_plugin_registry_duplicate_load():
    reg = PluginRegistry()
    m = PluginManifest.from_dict({"name": "dup"})
    reg.load(m)
    with pytest.raises(PluginError):
        reg.load(m)


# ---------------- 关键验证：新渠道零改内核 ----------------

def test_new_channel_needs_no_core_changes():
    """接入新渠道只需一个适配器类 + 注册。内核代码一行不改。"""
    r = AdapterRegistry()
    adapter = MockChannelAdapter()
    r.register(adapter)

    # 收
    msg = adapter.parse({"event": "message", "text": "你好", "from": "u1", "chat_id": "c1"})
    assert msg is not None and msg.text == "你好"

    # 发
    res = asyncio.run(adapter.send(msg, "你好，有什么可以帮你"))
    assert res.ok
    assert adapter.sent == [("c1", "你好，有什么可以帮你")]

    # 能力差异被正确暴露
    assert CAP_SEND_TEXT in adapter.capabilities()
    assert CAP_SEND_IMAGE not in adapter.capabilities()
    with pytest.raises(UnsupportedCapability):
        require_capability(adapter, CAP_SEND_IMAGE)


def test_onebot_adapter_conforms_to_protocol():
    """结构化类型检查：OneBot 适配器满足 ChannelAdapter 契约。"""
    from gateway.adapters.base import ChannelAdapter

    a = OneBot11Adapter()
    for member in ("capabilities", "parse", "send", "execute", "verify"):
        assert hasattr(a, member), f"缺少成员 {member}"
    assert isinstance(a, ChannelAdapter)
