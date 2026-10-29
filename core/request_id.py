"""请求关联 ID + 结构化错误响应。

为什么需要：排障时最耗时的一步是"把用户报的一次失败和几十条日志对上"。
没有关联 ID 时只能靠时间戳猜，多用户并发下几乎不可能。

方案（业界标准做法，等价于 asgi-correlation-id）：
- 入站读 `X-Request-ID`（网关/反代已带就沿用），否则生成 12 位随机串；
- 放进来日志 record 的 `request_id` 字段（events_log 自动带出）；
- 回写响应头 `X-Request-ID`，前端报错时可直接带上；
- 用 contextvars 而不是线程局部变量：async 任务切换时上下文自动跟随。

为什么不用 asgi-correlation-id 包：它只在 middleware 层可用，而本项目的
工具调用、提醒调度、后台 agent run 都在 middleware 之外执行；contextvars
能覆盖所有 async 上下文，且不引入额外依赖。
"""
from __future__ import annotations

import contextvars
import re
import uuid

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def set_request_id(value: str | None) -> str:
    """设置当前上下文 ID；非法值（含换行/超长）一律重新生成，防日志注入。"""
    rid = (value or "").strip()
    if not rid or not _SAFE.match(rid):
        rid = new_request_id()
    _REQUEST_ID.set(rid)
    return rid


def current_request_id() -> str:
    return _REQUEST_ID.get()
