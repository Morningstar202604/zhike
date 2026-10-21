"""调用 gateway 的 /internal/action 端点。

要点：
- 只连本机/内网，端口不对外发布
- 带 X-Channel-Token（与核心共享），常量时间比较在网关侧
- 网关离线/超时立刻失败，绝不缓冲重放（动作有时效，重放会误操作）
"""
from __future__ import annotations

import os

import httpx

from core.http_pool import get_client

DEFAULT_URL = "http://127.0.0.1:8011"


class GatewayError(RuntimeError):
    """网关不可达或返回错误。上层据此给出可读失败原因。"""


class GatewayOffline(GatewayError):
    """连不上网关（未启动/网络不通）。"""


class GatewayTimeout(GatewayError):
    """网关已收到请求但未在超时内返回。"""


def _base_url() -> str:
    return (os.environ.get("GATEWAY_ACTION_URL") or DEFAULT_URL).rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("CHANNEL_TOKEN", "").strip()
    return {"X-Channel-Token": token} if token else {}


def action_url() -> str:
    return f"{_base_url()}/internal/action"


def status_url() -> str:
    return f"{_base_url()}/internal/status"


async def call_action(action: str, params: dict, timeout_s: float = 10.0) -> dict:
    """向网关发起一次动作调用，返回 {"data": ...}。

    失败一律抛 GatewayError 子类，由上层转成结构化错误。
    """
    timeout = timeout_s or float(os.environ.get("GATEWAY_ACTION_TIMEOUT_S", "10"))
    try:
        client = get_client(timeout=timeout)
        resp = await client.post(
            action_url(),
            json={"action": action, "params": params or {}},
            headers=_headers(),
        )
    except httpx.TimeoutException as e:
        raise GatewayTimeout(f"网关响应超时（{timeout}s）") from e
    except httpx.HTTPError as e:
        raise GatewayOffline(f"网关不可达：{type(e).__name__}") from e

    if resp.status_code == 401:
        raise GatewayError("网关拒绝了渠道令牌（CHANNEL_TOKEN 不一致）")
    if resp.status_code == 503:
        raise GatewayOffline("网关尚未连接协议端（机器人未登录）")

    try:
        payload = resp.json()
    except ValueError as e:
        raise GatewayError(f"网关返回非 JSON（HTTP {resp.status_code}）") from e

    if not payload.get("ok"):
        raise GatewayError(str(payload.get("error") or f"网关返回失败（HTTP {resp.status_code}）"))
    return {"data": payload.get("data")}


async def probe() -> dict:
    """探测网关是否在线，供控制台展示。"""
    try:
        client = get_client(timeout=5)
        resp = await client.get(status_url(), headers=_headers())
        resp.raise_for_status()
        return {"online": True, **resp.json()}
    except Exception as e:
        return {"online": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
