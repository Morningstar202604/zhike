"""MCP 暴露层回归：协议版本兼容 + 工具清单 + 未启用工具不暴露。"""
import asyncio
import json

import pytest


def test_mcp_exposes_readonly_tools_only(monkeypatch):
    """默认只暴露无上下文可用的只读工具；写工具必须显式开开关。"""
    pytest.importorskip("mcp")
    from core import mcp_server

    if mcp_server.ServerClass is None:
        pytest.skip("mcp not installed")

    monkeypatch.delenv("ZHIKE_MCP_ALLOW_WRITE", raising=False)
    mcp = mcp_server._build_mcp()
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"query_order", "lookup_policy", "web_search", "calc"} <= names
    assert "remember" not in names
    assert "send_image" not in names


def test_mcp_write_tools_opt_in(monkeypatch):
    pytest.importorskip("mcp")
    from core import mcp_server

    if mcp_server.ServerClass is None:
        pytest.skip("mcp not installed")

    monkeypatch.setenv("ZHIKE_MCP_ALLOW_WRITE", "1")
    mcp = mcp_server._build_mcp()
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "remember" in names and "list_reminders" in names


def test_mcp_disabled_tool_reports_not_enabled(monkeypatch):
    """工具被配置关掉时，MCP 调用要明说未启用，不假装成功。"""
    pytest.importorskip("mcp")
    from core import mcp_server, tools as tools_mod

    monkeypatch.setattr(mcp_server.config, "get_config", lambda: {"tools": {}})
    monkeypatch.setattr(tools_mod, "active_tools", lambda cfg: {})
    payload = json.loads(asyncio.run(mcp_server._call("calc", {"expression": "1+1"})))
    assert payload["connected"] is False
    assert "未启用" in payload["note"]
