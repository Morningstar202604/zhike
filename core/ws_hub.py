"""WebSocket 连接中心：访客会话连接与坐席连接的注册、广播、清理。

从 core/main.py 抽出（原文件 2260 行，路由 + WS + KB 文件锁 + SPA 混在一起，
无法单独测试广播语义，也无法在不启动整个 app 的情况下压测）。

设计要点（都是踩过坑才加的）：
- **并发发送**：串行 await 会被一个慢客户端拖住整批（实测 11 连接、1 个卡 3 秒 → 整批 3150ms）；
- **单连接超时**：卡住的客户端 2 秒后放弃并清理，不让它泄漏在注册表里；
- **异常必清理**：任何断开路径（正常/异常/协议错误）都要从注册表移除，
  否则死连接永久占用内存，广播越来越慢。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

ev = logging.getLogger("cs.events")


class Hub:
    def __init__(self) -> None:
        self.visitor_conns: dict[str, set] = {}
        self.operator_conns: set = set()

    # 单连接发送超时：正常 WS 发送是毫秒级，2 秒足够排完本地缓冲
    SEND_TIMEOUT_S = 2.0

    async def _send_one(self, ws, payload: dict, timeout: float) -> bool:
        """给单个连接发消息，返回是否成功。超时/异常都算失败（会被清理）。"""
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=timeout)
            return True
        except Exception:  # 客户端断开/协议错误/超时：统一按失败处理
            return False

    async def publish(self, session_id: str, payload: dict) -> None:
        """推送给某个会话的所有访客连接（并发发送）。"""
        conns = list(self.visitor_conns.get(session_id, ()))
        if not conns:
            return
        results = await asyncio.gather(
            *(self._send_one(ws, payload, self.SEND_TIMEOUT_S) for ws in conns),
            return_exceptions=True,
        )
        for ws, ok in zip(conns, results, strict=False):
            if ok is not True:
                self.visitor_conns.get(session_id, set()).discard(ws)

    async def broadcast_to_operators(self, payload: dict) -> None:
        """广播给所有坐席连接（并发发送，单个失败不影响其他人）。"""
        conns = list(self.operator_conns)
        if not conns:
            return
        results = await asyncio.gather(
            *(self._send_one(ws, payload, self.SEND_TIMEOUT_S) for ws in conns),
            return_exceptions=True,
        )
        for ws, ok in zip(conns, results, strict=False):
            if ok is not True:
                self.operator_conns.discard(ws)

    async def _hold(self, ws: WebSocket, on_close) -> None:
        """挂住连接直到断开。任何异常路径都必须执行清理，否则死连接会永久占用注册表。"""
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as e:  # 客户端异常断开/协议错误：同样要清理
            ev.info("ws closed abnormally: %s: %s", type(e).__name__, str(e)[:80])
        finally:
            on_close()

    async def connect(self, session_id: str, ws: WebSocket, already_accepted: bool = False) -> None:
        if not already_accepted:
            await ws.accept()
        self.visitor_conns.setdefault(session_id, set()).add(ws)
        await self._hold(ws, lambda: self.visitor_conns.get(session_id, set()).discard(ws))

    async def connect_operator(self, ws: WebSocket, already_accepted: bool = False) -> None:
        if not already_accepted:
            await ws.accept()
        self.operator_conns.add(ws)
        await self._hold(ws, lambda: self.operator_conns.discard(ws))

    def stats(self) -> dict:
        return {
            "visitor_sessions": len(self.visitor_conns),
            "visitor_total": sum(len(v) for v in self.visitor_conns.values()),
            "operator_total": len(self.operator_conns),
        }


hub = Hub()
