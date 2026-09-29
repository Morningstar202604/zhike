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
    handler = RingHandler()
    for name in ("cs.events", "core"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)


def entries(limit: int = 200, level: str | None = None) -> list[dict]:
    with _lock:
        items = list(_ring)
    if level:
        items = [e for e in items if e["level"] == level.upper()]
    return list(reversed(items))[:limit]
