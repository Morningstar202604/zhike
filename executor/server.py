"""执行器 HTTP 服务：独立进程，只监听回环地址。

安全约束（缺一不可）：
1. 只绑 127.0.0.1；要绑 0.0.0.0 必须同时设置 EXECUTOR_TOKEN，否则拒绝启动
2. 每个请求带 HMAC-SHA256 签名 + 时间戳，超 5 分钟视为重放
3. 能力白名单：未注册的能力直接 404
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import capabilities as cap  # noqa: E402
from executor.policy import PolicyError, load_policy  # noqa: E402

logger = logging.getLogger("executor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s executor: %(message)s")

TOKEN = os.environ.get("EXECUTOR_TOKEN", "")
POLICY_PATH = os.environ.get("EXECUTOR_POLICY_PATH", "")
REPLAY_WINDOW_S = int(os.environ.get("EXECUTOR_REPLAY_WINDOW_S", "300"))

_policy = load_policy(POLICY_PATH or None)


class InvokeIn(BaseModel):
    capability: str
    params: dict = {}


def _signature(body: bytes, ts: str) -> str:
    return hmac.new(TOKEN.encode("utf-8"), ts.encode("utf-8") + body, hashlib.sha256).hexdigest()


def _verify(body: bytes, ts: str | None, sig: str | None) -> None:
    """校验签名与时间戳。未设置 TOKEN 时（纯本地开发）跳过校验。"""
    if not TOKEN:
        return
    if not ts or not sig:
        raise HTTPException(401, "缺少签名")
    try:
        age = abs(time.time() - float(ts))
    except ValueError:
        raise HTTPException(401, "时间戳非法")
    if age > REPLAY_WINDOW_S:
        raise HTTPException(401, "请求已过期（可能重放）")
    if not hmac.compare_digest(_signature(body, ts), sig):
        raise HTTPException(401, "签名不匹配")


CAPABILITIES = {
    "fs.stat": cap.fs_stat,
    "fs.list": cap.fs_list,
    "fs.read": cap.fs_read,
    "fs.write": cap.fs_write,
    "fs.mkdir": cap.fs_mkdir,
    "fs.move": cap.fs_move,
    "fs.delete": cap.fs_delete,
    "shell.exec": cap.shell_exec,
    "proc.list": cap.proc_list,
    "proc.kill": cap.proc_kill,
}


def create_app() -> FastAPI:
    app = FastAPI(title="Zhike Executor", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz():
        return {
            "ok": True,
            "capabilities": sorted(CAPABILITIES),
            "allow_roots": [str(r) for r in _policy._roots()],
            "ui_enabled": _policy.ui_enabled(),
            "signed": bool(TOKEN),
        }

    @app.post("/invoke")
    async def invoke(body: InvokeIn,
                     request: Request,
                     x_ts: str | None = Header(default=None, alias="X-Executor-Ts"),
                     x_sig: str | None = Header(default=None, alias="X-Executor-Sig")):
        raw = await request.body()
        _verify(raw, x_ts, x_sig)

        fn = CAPABILITIES.get(body.capability)
        if fn is None:
            raise HTTPException(404, f"未知能力：{body.capability}")

        started = time.time()
        try:
            result = fn(_policy, **(body.params or {}))
        except PolicyError as e:
            logger.warning("policy denied %s: %s", body.capability, e)
            raise HTTPException(403, f"策略拒绝：{e}")
        except cap.CapabilityError as e:
            logger.info("capability failed %s: %s", body.capability, e)
            raise HTTPException(400, str(e))
        except TypeError as e:
            raise HTTPException(400, f"参数不匹配：{e}")
        except Exception as e:  # 兜底：执行器内部错误不能把栈抛给调用方
            logger.exception("capability crashed %s", body.capability)
            raise HTTPException(500, f"执行器内部错误：{type(e).__name__}")

        logger.info("capability %s ok in %dms", body.capability,
                    int((time.time() - started) * 1000))
        return {"ok": True, "capability": body.capability, "result": result,
                "duration_ms": int((time.time() - started) * 1000)}

    return app


def _guard_bind(host: str) -> None:
    """非回环绑定必须配 token，否则拒绝启动（防止手滑把执行器挂到公网）。"""
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not TOKEN:
        raise SystemExit(
            "拒绝启动：EXECUTOR_BIND 不是回环地址，但未设置 EXECUTOR_TOKEN。\n"
            "执行器能执行任意白名单命令，绝不允许无鉴权暴露到网络。"
        )


def main() -> None:
    import uvicorn

    host = os.environ.get("EXECUTOR_BIND", "127.0.0.1")
    port = int(os.environ.get("EXECUTOR_PORT", "8020"))
    _guard_bind(host)
    if not _policy._roots():
        logger.warning("allow_roots 为空：所有文件与命令操作都会被拒绝。"
                       "请配置 EXECUTOR_POLICY_PATH 指向 policy.json")
    logger.info("executor listening on %s:%s (signed=%s)", host, port, bool(TOKEN))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
