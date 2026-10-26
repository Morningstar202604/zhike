"""运行日志：内存环（管理端实时看）+ 可选文件轮转（重启后仍可追溯）。

为什么两者都要：
- 内存环用于控制台 3 秒刷新的实时流，进程重启即清空；
- 文件 handler 用 RotatingFileHandler 落盘，排障时能回看历史告警。
  没有文件 handler 时，应用级 INFO（DNS overrides / KB seed / 模型冷却原因）
  只出现在启动它的控制台里，出问题后无法追溯（已经被现场验证过）。
"""
import logging
import logging.handlers
import os
import threading
from collections import deque
from datetime import datetime, timezone

_lock = threading.Lock()
_seq = 0
_ring: deque = deque(maxlen=1000)
_FILE_HANDLER: logging.Handler | None = None


class RingHandler(logging.Handler):
    def emit(self, record):
        global _seq
        entry = {
            "id": 0,
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        with _lock:
            _seq += 1
            entry["id"] = _seq
            _ring.append(entry)


def _attach_file_handler() -> None:
    """落盘 handler：路径由 CS_AGENT_LOG_FILE 指定，不配则只保留内存环。

    默认路径 = CS_AGENT_DB 同目录下的 logs/app.log：常驻部署不用额外配置就有落盘日志。
    容器里如已有 stdout 采集，可用 CS_AGENT_LOG_FILE=off 关掉。
    """
    global _FILE_HANDLER
    raw = (os.environ.get("CS_AGENT_LOG_FILE") or "").strip()
    if raw.lower() in ("off", "0", "false", "no"):
        return
    if raw:
        path = raw
    else:
        db = (os.environ.get("CS_AGENT_DB") or "cs_agent.db").strip()
        base = os.path.dirname(os.path.abspath(db)) or os.getcwd()
        path = os.path.join(base, "logs", "app.log")
    if _FILE_HANDLER is not None:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=8 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        _FILE_HANDLER = handler
    except OSError as e:  # 磁盘满/权限不足：不能因此让服务起不来
        logging.getLogger("cs.events").warning("log file unavailable: %s", type(e).__name__)


def file_path() -> str:
    """当前落盘日志路径（未启用返回空串），供 /api/agent/health 展示。"""
    return str(getattr(_FILE_HANDLER, "baseFilename", "") or "")


def attach() -> None:
    """挂载内存环（+ 可选文件）handler。幂等：重复建 app 不会重复记录或泄漏。"""
    _attach_file_handler()
    for name in ("cs.events", "core"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        # 内存环与文件 handler 各自判断：之前用 continue 一次性跳过，
        # 导致「已有内存环」时文件 handler 永远挂不上（日志目录只有空文件）。
        if not any(isinstance(h, RingHandler) for h in lg.handlers):
            lg.addHandler(RingHandler())
        if _FILE_HANDLER is not None and _FILE_HANDLER not in lg.handlers:
            lg.addHandler(_FILE_HANDLER)


def entries(limit: int = 200, level: str | None = None) -> list[dict]:
    with _lock:
        items = list(_ring)
    if level:
        items = [e for e in items if e["level"] == level.upper()]
    return list(reversed(items))[:limit]

