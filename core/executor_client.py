"""core 侧执行器客户端：通过 HTTP 调用独立执行器进程。

core 进程内**不出现** subprocess / os.system —— 所有电脑操作都在执行器侧完成，
这里只有 HTTP。签名用 HMAC(EXECUTOR_TOKEN)，带时间戳防重放。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import httpx


class ExecutorError(RuntimeError):
    pass


class ExecutorOffline(ExecutorError):
    pass


class ExecutorTimeout(ExecutorError):
    pass


class ExecutorDenied(ExecutorError):
    """策略拒绝（403）。"""


def _base() -> str:
    return (os.environ.get("EXECUTOR_URL") or "http://127.0.0.1:8020").rstrip("/")


def _token() -> str:
    return os.environ.get("EXECUTOR_TOKEN", "").strip()


def _sign(body: bytes) -> tuple[str, str]:
    ts = str(int(time.time()))
    token = _token()
    if not token:
        return ts, ""
    sig = hmac.new(token.encode("utf-8"), ts.encode("utf-8") + body, hashlib.sha256).hexdigest()
    return ts, sig


async def invoke(capability: str, params: dict | None = None, timeout_s: float = 30.0) -> dict:
    """调用一个执行器能力。失败抛 Executor* 异常，由上层转结构化错误。"""
    import json as _json

    payload = _json.dumps({"capability": capability, "params": params or {}},
                          ensure_ascii=False).encode("utf-8")
    ts, sig = _sign(payload)
    headers = {"Content-Type": "application/json", "X-Executor-Ts": ts}
    if sig:
        headers["X-Executor-Sig"] = sig

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{_base()}/invoke", content=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise ExecutorTimeout(f"执行器响应超时（{timeout_s}s）") from e
    except httpx.HTTPError as e:
        raise ExecutorOffline(f"执行器不可达：{type(e).__name__}") from e

    if resp.status_code == 403:
        raise ExecutorDenied(_detail(resp) or "策略拒绝")
    if resp.status_code == 404:
        raise ExecutorError(_detail(resp) or f"未知能力：{capability}")
    if resp.status_code >= 400:
        raise ExecutorError(_detail(resp) or f"执行器返回 HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        raise ExecutorError("执行器返回非 JSON") from e
    return data.get("result", data)


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail") or "")
    except Exception:
        return resp.text[:200]


async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_base()}/healthz")
            resp.raise_for_status()
            return {"online": True, **resp.json()}
    except Exception as e:
        return {"online": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
