"""QQ 全量接管：动作 → 工具封装必须走同一套鉴权/审批，不能变成后门。"""
import asyncio

import pytest

from core import tools
from core.tool_context import ToolContext


def test_qq_action_tools_registered():
    expected = {"qq_group_ban", "qq_group_kick", "qq_group_card", "qq_group_rename",
                "qq_group_notice", "qq_recall", "qq_upload_file",
                "qq_friend_handle", "qq_group_handle"}
    assert expected <= set(tools._ASYNC_IMPLS)
    for name in expected:
        assert name in tools.TOOL_META


def test_l2_action_tool_reports_pending_approval(monkeypatch):
    """踢人（L2）通过工具调用时仍要进审批，不能因为是「工具」就直通。"""
    from core.actions import service as actions_service

    async def fake_execute(action, params, principal_raw, session_id=None, **kw):
        return {"status": "pending_approval", "approval_id": "ap-1", "action": action}

    monkeypatch.setattr(actions_service, "execute", fake_execute)
    ctx = ToolContext(visitor_id="v", group_id="9", channel_user_id="1", principal="admin")
    res = asyncio.run(tools.invoke("qq_group_kick", {"user_id": "2"}, ctx))
    assert res["found"] is True
    assert "审批" in res["note"]


def test_group_target_defaults_to_current_chat(monkeypatch):
    """不给 group_id 时用当前会话的群，避免模型每次反问群号。"""
    from core.actions import service as actions_service

    seen = {}

    async def fake_execute(action, params, principal_raw, session_id=None, **kw):
        seen.update(params)
        return {"status": "executed", "result": {"ok": True}}

    monkeypatch.setattr(actions_service, "execute", fake_execute)
    ctx = ToolContext(visitor_id="v", group_id="12345", principal="admin")
    asyncio.run(tools.invoke("qq_group_ban", {"user_id": "2", "duration": "60"}, ctx))
    assert seen["group_id"] == "12345"


def test_action_error_becomes_readable_note(monkeypatch):
    from core.actions import service as actions_service

    async def fake_execute(action, params, principal_raw, session_id=None, **kw):
        raise actions_service.ActionError("普通群成员无权执行写操作", 403)

    monkeypatch.setattr(actions_service, "execute", fake_execute)
    ctx = ToolContext(visitor_id="v", principal="qq:999")
    res = asyncio.run(tools.invoke("qq_group_ban", {"user_id": "2", "duration": "60"}, ctx))
    assert res["found"] is False
    assert "没办成" in res["note"]


def test_avatar_and_profile_tools_registered():
    """换头像/改资料也是可逆写动作，必须走同一套鉴权（不能绕过动作层直连协议端）。"""
    for name in ("qq_set_avatar", "qq_set_profile"):
        assert name in tools._ASYNC_IMPLS
        assert name in tools.TOOL_META
    from core.actions import registry

    assert registry.REGISTRY["set_qq_avatar"].risk == 1
    assert registry.REGISTRY["set_qq_profile"].risk == 1


def test_interaction_tools_registered():
    """戳一戳/表情回应/精华/合并转发 都要变成模型可调用的工具。"""
    for name in ("qq_poke", "qq_emoji_like", "qq_essence", "qq_forward"):
        assert name in tools._ASYNC_IMPLS
        assert name in tools.TOOL_META
        assert tools.TOOL_META[name]["args"]


def test_voice_tool_registered_and_privileged():
    assert "send_voice" in tools._ASYNC_IMPLS
    assert "send_voice" in tools.PRIVILEGED_TOOLS


def test_voice_tool_needs_qq_context():
    ctx = ToolContext(visitor_id="v", channel_type="webchat")
    res = asyncio.run(tools.tool_send_voice(ctx, text="你好"))
    assert res["status"] == "need_context"


def test_voice_tool_rejects_empty_text():
    ctx = ToolContext(visitor_id="v", channel_type="qq_private", channel_user_id="1")
    res = asyncio.run(tools.tool_send_voice(ctx))
    assert res["status"] == "need_context"


def test_message_id_defaults_to_current_message(monkeypatch):
    """「给这条点赞」不能要求模型自己知道 message_id——工具层应自动补当前消息 id。"""
    from core.actions import service as actions_service

    seen = {}

    async def fake_execute(action, params, principal_raw, session_id=None, **kw):
        seen.update(params)
        return {"status": "executed", "result": {"ok": True}}

    monkeypatch.setattr(actions_service, "execute", fake_execute)
    ctx = ToolContext(visitor_id="v", principal="admin", message_id="777",
                      channel_type="qq_private", channel_user_id="1")
    asyncio.run(tools.invoke("qq_emoji_like", {"emoji_id": "128077"}, ctx))
    assert seen["message_id"] == "777"
