"""共享的 httpx 客户端池。

每次调用都新建 AsyncClient 会重新做 TCP + TLS 握手（公网端点约 100-300ms）。
LLM 对话、动作调用、执行器调用都是高频路径，连接复用收益明显。

注意：超时必须按请求设置（不同场景 5s~120s 不等），
所以这里只复用连接池，不锁死 timeout。

**事件循环隔离**：httpx.AsyncClient 的连接池绑定创建它的循环。池子跨循环复用
会抛 `RuntimeError: Event loop is closed` / `Future attached to a different loop`
（实测：asyncio.run 的脚本连续两次调用工具，第二次必炸）。所以按
(loop, base_url) 分桶；旧循环的客户端在下次取用时直接丢弃。

**不能用 id(loop) 当键**：CPython 会复用已释放对象的内存地址，
`asyncio.run()` 连续两次拿到的 loop 可能落在同一地址 → 键碰撞 → 复用已关闭的池
（已被单测抓到）。这里改成给每个 loop 挂一个整型代号，存在 weakref 回调里随 loop 一起回收。
"""
from __future__ import annotations

import asyncio
import itertools
import weakref

import httpx

# key = (事件循环代号, base_url)：一个循环一套连接池
_clients: dict[tuple[int, str, float], httpx.AsyncClient] = {}
_loop_ids: "weakref.WeakKeyDictionary[object, int]" = weakref.WeakKeyDictionary()
_loop_seq = itertools.count(1)
_NO_LOOP = 0


def _loop_key() -> int:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的循环（同步调用方）：用固定桶，调用方自己负责
        return _NO_LOOP
    ident = _loop_ids.get(loop)
    if ident is None:
        ident = next(_loop_seq)
        _loop_ids[loop] = ident
        # loop 关闭时主动清掉它的客户端，不等 GC：
        # WeakKeyDictionary 只保证键会消失，但 _clients 里还留着那批已废客户端，
        # 而 CPython 可能把新 loop 分配到同一地址 → 键碰撞 → 拿到已关闭的池（已复现）。
        def _cleanup(_ident=ident):
            for k in [k for k in _clients if k[0] == _ident]:
                _clients.pop(k, None)
        weakref.finalize(loop, _cleanup)
    return ident


def get_client(base_url: str = "", timeout: float = 30.0) -> httpx.AsyncClient:
    """按 (事件循环, base_url, timeout) 取共享客户端。

    timeout 必须进 key：httpx 的 timeout 固定在 client 上，若只按 base_url 复用，
    先建的健康检查（5s）会把后续 shell.exec（120s）也锁成 5s——长任务全被误杀。
    """
    key = (_loop_key(), (base_url or "").rstrip("/"), float(timeout))
    client = _clients.get(key)
    if client is not None and client.is_closed:
        client = None
    if client is None:
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            follow_redirects=False,
        )
        _clients[key] = client
    return client


def drop_stale_loops() -> int:
    """丢弃不属于当前事件循环的客户端（同步 close，不做 await）。

    在长生命周期进程里由定时任务调用；测试里也能用来避免跨用例串味。
    """
    cur = _loop_key()
    stale = [k for k in _clients if k[0] != cur]
    for k in stale:
        _clients.pop(k, None)
    return len(stale)


async def close_all() -> None:
    """关闭所有共享客户端（应用退出时调用）。"""
    for c in list(_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _clients.clear()
