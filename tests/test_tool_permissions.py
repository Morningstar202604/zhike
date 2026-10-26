"""工具层权限闸门回归：写类工具必须校验主体，不能只靠提示词。"""
import asyncio

import pytest

from core import tools
from core.tool_context import ToolContext


def test_privileged_tool_denied_for_plain_qq_member(monkeypatch):
    """修复前：群里任何人诱导模型选中 write_file 就直接落到执行器，无档位、无审计。"""
    monkeypatch.delenv("AUTHORIZED_QQ", raising=False)
    ctx = ToolContext(visitor_id="v", channel_type="qq", channel_user_id="99999",
                      principal="qq:99999")
    res = asyncio.run(tools.invoke("write_file", {"path": "C:/x", "content": "x"}, ctx))
    assert res["found"] is False
    assert "管理员权限" in res["note"]


def test_privileged_tool_denied_for_web_visitor():
    ctx = ToolContext(visitor_id="v", principal="visitor:v")
    res = asyncio.run(tools.invoke("read_file", {"path": "C:/Windows/win.ini"}, ctx))
    assert res["found"] is False


def test_privileged_tool_allowed_for_authorized_qq(monkeypatch):
    monkeypatch.setenv("AUTHORIZED_QQ", "12345")
    # 执行器相关工具只在配置了 EXECUTOR_URL 时才出现在可用清单里
    monkeypatch.setenv("EXECUTOR_URL", "http://127.0.0.1:8020")
    ctx = ToolContext(visitor_id="v", channel_type="qq", channel_user_id="12345",
                      principal="qq:12345")
    # 通过桩掉执行器来确认它「走过了权限闸门」，而不是被拒在前一步
    from core import executor_client

    async def fake_invoke(capability, params=None, timeout_s=30.0):
        return {"content": "hello", "bytes": 5}

    monkeypatch.setattr(executor_client, "invoke", fake_invoke)
    res = asyncio.run(tools.invoke("read_file", {"path": "C:/work/a.txt"}, ctx))
    assert res["found"] is True


def test_privileged_tool_allowed_for_admin(monkeypatch):
    monkeypatch.setenv("EXECUTOR_URL", "http://127.0.0.1:8020")
    ctx = ToolContext(visitor_id="v", principal="admin")
    from core import executor_client

    async def fake_invoke(capability, params=None, timeout_s=30.0):
        return {"content": "ok"}

    import asyncio as _a
    orig = executor_client.invoke
    executor_client.invoke = fake_invoke
    try:
        res = _a.run(tools.invoke("read_file", {"path": "C:/work/a.txt"}, ctx))
    finally:
        executor_client.invoke = orig
    assert res["found"] is True


def test_remember_still_available_to_everyone():
    """记忆/提醒只作用于调用者自己，不应被权限闸门挡住。"""
    ctx = ToolContext(visitor_id="v-self", principal="visitor:v-self")
    res = asyncio.run(tools.invoke("remember", {"key": "口味", "value": "少糖"}, ctx))
    assert res["found"] is True
