"""共享的 httpx 客户端池。

每次调用都新建 AsyncClient 会重新做 TCP + TLS 握手（公网端点约 100-300ms）。
LLM 对话、动作调用、执行器调用都是高频路径，连接复用收益明显。

注意：超时必须按请求设置（不同场景 5s~120s 不等），
所以这里只复用连接池，不锁死 timeout。
"""
from __future__ import annotations

import httpx

# 一个进程一个客户端，按 base_url 复用连接池
_clients: dict[str, httpx.AsyncClient] = {}


def get_client(base_url: str = "", timeout: float = 30.0) -> httpx.AsyncClient:
    """按 base_url 取共享客户端。

    timeout 只在首次创建时生效；后续请求请用 client.post(..., timeout=...)。
    """
    key = (base_url or "").rstrip("/")
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            follow_redirects=False,
        )
        _clients[key] = client
    return client


async def close_all() -> None:
    """关闭所有共享客户端（应用退出时调用）。"""
    for c in list(_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _clients.clear()
