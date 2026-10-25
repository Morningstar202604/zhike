"""MCP (Model Context Protocol) 工具暴露层。

协议：Model Context Protocol（Anthropic 提出，OpenAI/Google/主流 Agent 运行时均支持）。
把知客的内置工具以 MCP tool 形式暴露，任何 MCP 客户端（Claude Desktop / Cursor /
其他 Agent 运行时）都能直接调用，不必把工具绑死在本进程内。

同时兼容两代官方 SDK：
- mcp >= 2.0：`mcp.server.mcpserver.MCPServer`
- mcp 1.x：`mcp.server.fastmcp.FastMCP`
不写死某一代，避免上游重命名后整套暴露层失效（2.x 就把 FastMCP 改名为 MCPServer）。

设计取舍：
- 只暴露「无会话上下文也能正确工作」的工具。依赖当前会话（发消息、发图、给某个人设提醒）
  的工具不暴露——MCP 客户端没有「当前会话」，硬暴露只会得到必然 need_context 的死工具。
- 写类工具默认不暴露：MCP 调用不经控制台审批，等于绕过 L0/L1/L2 闸门。
  需要时显式设 ZHIKE_MCP_ALLOW_WRITE=1。

启动（可选，不随 core 一起起）：
    pip install -r requirements-mcp.txt
    python -m core.mcp_server

MCP 客户端配置：
    "zhike": { "command": ["python", "-m", "core.mcp_server"] }
"""
from __future__ import annotations

import asyncio
import json
import os

from core import config, tools as tools_mod
from core.tool_context import EMPTY


def _load_server_class():
    """返回 (ServerClass, api_version)。未安装时返回 (None, "")。"""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0
        return MCPServer, "2"
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
        return FastMCP, "1"
    except ImportError:
        return None, ""


ServerClass, _API = _load_server_class()


# 无上下文可用的工具：MCP 客户端调用时按空上下文执行
READONLY_TOOLS = {
    "query_order": "按订单号查询物流/发货状态。入参 order_id 形如 ORD12345678。",
    "initiate_refund": "为订单发起退款申请（进入人工审核，不直接退款）。",
    "lookup_policy": "按主题查业务政策（退货/发货/隐私等）。",
    "web_search": "联网搜索实时信息。入参 query，可选 top_k。",
    "read_webpage": "打开网页并返回正文。入参 url，可选 max_chars。",
    "now": "查询当前日期、时间与星期。",
    "calc": "计算数学表达式（加减乘除、取余、乘方、括号）。",
    "current_context": "读取当前对话上下文（MCP 下通常为空）。",
    "recall_memory": "读取某访客的长期记忆。入参 visitor_id / key。",
}

WRITE_TOOLS = {
    "remember": "记住一条关于某访客的信息。入参 visitor_id / key / value。",
    "remind": "给某访客设提醒。入参 visitor_id / text / when。",
    "list_reminders": "列出某访客待触发的提醒。入参 visitor_id。",
    "cancel_reminder": "取消一条提醒。入参 visitor_id / reminder_id。",
}


async def _call(name: str, args: dict) -> str:
    enabled = tools_mod.active_tools(config.get_config())
    if name not in enabled:
        return json.dumps({"tool": name, "connected": False,
                           "note": f"工具「{name}」未启用。"}, ensure_ascii=False)
    result = await tools_mod.invoke(name, args, EMPTY)
    return json.dumps(result, ensure_ascii=False)


def _build_mcp():
    if ServerClass is None:
        raise RuntimeError(
            "mcp 未安装。请先 `pip install -r requirements-mcp.txt`，再启动本模块。"
        )
    mcp = ServerClass("zhike-tools")

    def register(name: str, description: str) -> None:
        # 用显式签名（**kwargs）注册，参数说明由 docstring 承载；
        # 不同代 SDK 对 callable 签名的推断规则不一致，**kwargs 是两边都稳的写法。
        async def handler(**kwargs) -> str:
            return await _call(name, kwargs)

        handler.__name__ = f"tool_{name}"
        handler.__doc__ = description
        mcp.tool(name=name, description=description)(handler)

    for name, desc in READONLY_TOOLS.items():
        register(name, desc)

    if os.environ.get("ZHIKE_MCP_ALLOW_WRITE", "").lower() in ("1", "true", "yes", "on"):
        for name, desc in WRITE_TOOLS.items():
            register(name, desc)

    return mcp


def main():
    mcp = _build_mcp()
    if _API == "1":
        asyncio.run(mcp.run_async())
    else:
        asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
