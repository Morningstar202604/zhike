"""内存日志环：捕获 Agent 运行事件，供管理端实时查看。"""
import logging
import threading
from collections import deque
from datetime import datetime, timezone

_lock = threading.Lock()
_seq = 0
_ring: deque = deque(maxlen=1000)


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


def attach() -> None:
    """挂载环形缓冲 handler。幂等：重复调用（测试反复建 app）不会重复记录或泄漏 handler。"""
    for name in ("cs.events", "core"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        if any(isinstance(h, RingHandler) for h in lg.handlers):
            continue
        lg.addHandler(RingHandler())


def entries(limit: int = 200, level: str | None = None) -> list[dict]:
    with _lock:
        items = list(_ring)
    if level:
        items = [e for e in items if e["level"] == level.upper()]
    return list(reversed(items))[:limit]
