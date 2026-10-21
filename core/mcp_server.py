"""MCP (Model Context Protocol) 工具暴露层。

业界事实标准（Anthropic/OpenAI/Google 全支持，2026-07 mcp v2.0 稳定）。
把知客内置的三个客服工具（query_order / initiate_refund / lookup_policy）暴露为 MCP tool，
任何 MCP 客户端（Claude Desktop / Cursor / 其他 Agent 运行时）都能远程调用，不再锁死在本进程内。

启动方式（可选，不随 core 一起起）：
    pip install mcp
    python -m core.mcp_server

或在你的 MCP 客户端配置里加：
    "zhike": { "command": ["python", "-m", "core.mcp_server"] }
"""
from __future__ import annotations

import asyncio
import json

from core import config, tools as tools_mod


try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # 没装 mcp 时给出明确提示
    FastMCP = None  # type: ignore[assignment]


def _build_mcp():
    if FastMCP is None:
        raise RuntimeError(
            "mcp 未安装。请先 `pip install mcp`（requirements-mcp.txt），再启动本模块。"
        )
    mcp = FastMCP("zhike-cs-tools")

    @mcp.tool()
    def query_order(order_id: str) -> str:
        """按订单号查询物流/发货状态。入参 order_id 形如 ORD12345678。"""
        fn = tools_mod.active_tools(config.get_config()).get("query_order")
        if not fn:
            return "订单查询工具未启用。"
        return json.dumps(fn(order_id=order_id), ensure_ascii=False)

    @mcp.tool()
    def initiate_refund(order_id: str, reason: str) -> str:
        """为订单发起退款申请（不直接退，进入人工审核）。"""
        fn = tools_mod.active_tools(config.get_config()).get("initiate_refund")
        if not fn:
            return "退款工具未启用。"
        return json.dumps(fn(order_id=order_id, reason=reason), ensure_ascii=False)

    @mcp.tool()
    def lookup_policy(topic: str) -> str:
        """按主题查客服政策（退货/发货/隐私等）。"""
        fn = tools_mod.active_tools(config.get_config()).get("lookup_policy")
        if not fn:
            return "政策查询工具未启用。"
        return json.dumps(fn(topic=topic), ensure_ascii=False)

    return mcp


def main():
    mcp = _build_mcp()
    asyncio.run(mcp.run_async())


if __name__ == "__main__":
    main()
