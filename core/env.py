"""统一的环境变量加载入口。

为什么需要它：README / DEPLOY / start.sh 都让用户 `cp .env.example .env` 再启动，
但此前没有任何代码读取 `.env`——本地 `python -m uvicorn core.main:app` 时
所有配置项全部落空（ADMIN_TOKEN 为空 → 管理接口 503，LLM Key 为空 → 规则模式）。
Docker 部署靠 compose 的 env_file 掩盖了这个缺陷，本机部署则完全不可用（已复现）。

加载顺序（后者不覆盖前者）：
1. 真实进程环境变量（最高优先级）
2. 工作区 `.env`
3. 仓库根 `.env`（core/env.py 的上两级）

用 python-dotenv（uvicorn[standard] 已随带）而不是自己解析：它处理引号、
多行值、`export` 前缀、BOM 等边界，手写解析必踩坑。
"""
from __future__ import annotations

import os
import pathlib

_LOADED = False


def _candidates() -> list[pathlib.Path]:
    here = pathlib.Path(__file__).resolve()
    return [
        pathlib.Path.cwd() / ".env",
        here.parent.parent / ".env",
    ]


def load_env() -> bool:
    """幂等加载 .env。返回是否至少读到一个文件。"""
    global _LOADED
    if _LOADED:
        return True
    _LOADED = True
    try:
        from dotenv import load_dotenv
    except ImportError:  # 极简环境（没装 uvicorn[standard]）不应阻断启动
        return False
    found = False
    seen: set[str] = set()
    for path in _candidates():
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        # override=False：真实环境变量永远优先，容器 env_file / systemd 注入不会被 .env 覆盖
        load_dotenv(path, override=False, encoding="utf-8")
        found = True
    return found


def env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default
