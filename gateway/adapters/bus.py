"""事件总线 + 适配器注册表。

进程内用 asyncio 队列分发；事件类型固定，订阅者按类型注册。
跨进程场景（多 worker）在计划里留给 outbox 表，这里先做单进程版。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from gateway.adapters.base import ChannelAdapter, adapter_info

# 事件类型
EV_MESSAGE_IN = "message.inbound"
EV_REPLY_OUT = "reply.outbound"
EV_ACTION_REQ = "action.requested"
EV_ACTION_DONE = "action.finished"
EV_APPROVAL = "approval.pending"

ALL_EVENTS = {EV_MESSAGE_IN, EV_REPLY_OUT, EV_ACTION_REQ, EV_ACTION_DONE, EV_APPROVAL}

Handler = Callable[[dict], Awaitable[None]]


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)


class EventBus:
    """极简发布-订阅。单进程、异步、失败隔离。

    一个订阅者抛异常不影响其他订阅者，也不影响发布方。
    """

    def __init__(self):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._errors: list[dict] = []

    def subscribe(self, event_type: str, handler: Handler) -> None:
        if event_type not in ALL_EVENTS and event_type != "*":
            raise ValueError(f"未知事件类型：{event_type}")
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: str, payload: dict | None = None) -> int:
        """发布事件，返回成功处理的订阅者数量。"""
        delivered = 0
        for handler in list(self._handlers.get(event_type, [])) + list(self._handlers.get("*", [])):
            try:
                await handler({"type": event_type, "payload": payload or {}})
                delivered += 1
            except Exception as e:  # 订阅者失败不影响其他订阅者
                self._errors.append({
                    "event": event_type,
                    "handler": getattr(handler, "__name__", repr(handler))[:60],
                    "error": f"{type(e).__name__}: {str(e)[:160]}",
                })
                if len(self._errors) > 100:
                    self._errors = self._errors[-100:]
        return delivered

    def errors(self) -> list[dict]:
        return list(self._errors)


class AdapterRegistry:
    """渠道适配器注册表：一平台一实现，按 name 查找。"""

    def __init__(self):
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        name = getattr(adapter, "name", None)
        if not name:
            raise ValueError("适配器必须有 name")
        if name in self._adapters:
            raise ValueError(f"适配器已注册：{name}")
        self._adapters[name] = adapter

    def get(self, name: str) -> ChannelAdapter | None:
        return self._adapters.get(name)

    def require(self, name: str) -> ChannelAdapter:
        a = self.get(name)
        if a is None:
            raise KeyError(f"未注册的渠道适配器：{name}")
        return a

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def describe(self) -> list[dict]:
        """给控制台看的适配器能力矩阵。"""
        return [adapter_info(a) for a in self._adapters.values()]

    def find_by_capability(self, cap: str) -> list[str]:
        return [n for n, a in self._adapters.items() if cap in a.capabilities()]


# 全局实例：网关与测试共用
bus = EventBus()
registry = AdapterRegistry()


class OutboxBridge:
    """事件总线 ↔ outbox 表：多 worker / 跨进程场景的事件通道。

    单进程时总线足够；多 worker 时各进程的订阅者收不到彼此的进程内事件，
    这时用 outbox 落库 + 轮询，保证至少一次投递。
    """

    def __init__(self, bus: EventBus, poll_interval_s: float = 1.0):
        self._bus = bus
        self._interval = poll_interval_s
        self._task = None
        self._running = False

    async def _run(self):
        from core import store

        while self._running:
            try:
                events = store.outbox_claim(limit=20)
                for ev in events:
                    try:
                        await self._bus.publish(ev["topic"], ev.get("payload") or {})
                        store.outbox_done(ev["id"])
                    except Exception as e:  # 单条失败不影响批次
                        store.outbox_fail(ev["id"], f"{type(e).__name__}: {e}")
            except Exception:
                pass  # 轮询本身出错不应终止循环
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @staticmethod
    def publish(topic: str, payload: dict) -> int:
        """写入 outbox（供其他进程消费）。"""
        from core import store

        return store.outbox_put(topic, payload)
