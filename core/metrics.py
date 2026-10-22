"""极简指标：不引第三方依赖，输出 Prometheus 文本格式。

为什么要自建而不是上 prometheus_client：
- 只有一个进程、指标维度固定（路径/状态码/耗时），自建 60 行够用，零依赖
- 生产需要更复杂直方图时可无缝替换为 prometheus_client（输出格式兼容）

采集口径：
- 每个 HTTP 请求：计数 + 累计耗时 + 最大耗时，按 (method, path, status) 聚合
- path 做模板化（/api/sessions/{sid}/messages），避免基数爆炸
- LLM 调用单独计数（成功/失败），来自 llm.py 的埋点
"""
from __future__ import annotations

import re
import threading
import time

_LOCK = threading.Lock()
_REQ: dict[tuple[str, str, int], dict] = {}
_LLM = {"ok": 0, "fail": 0, "fallback": 0}
_STARTED = time.time()

_SID_RE = re.compile(r"/[a-f0-9]{16,64}(?=/|$)")


def normalize_path(path: str) -> str:
    """把具体 id 换成占位符，避免指标标签基数随会话数线性增长。"""
    p = _SID_RE.sub("/{id}", path or "/")
    return p[:120] or "/"


def record_request(method: str, path: str, status: int, duration_s: float) -> None:
    key = (method.upper(), normalize_path(path), int(status))
    with _LOCK:
        slot = _REQ.get(key)
        if slot is None:
            _REQ[key] = {"count": 1, "sum": duration_s, "max": duration_s}
        else:
            slot["count"] += 1
            slot["sum"] += duration_s
            if duration_s > slot["max"]:
                slot["max"] = duration_s


def record_llm(result: str) -> None:
    with _LOCK:
        if result in _LLM:
            _LLM[result] += 1


def render() -> str:
    lines = [
        "# HELP zhike_http_requests_total HTTP requests by route and status.",
        "# TYPE zhike_http_requests_total counter",
    ]
    with _LOCK:
        items = list(_REQ.items())
        llm = dict(_LLM)
    for (method, path, status), s in sorted(items):
        labels = f'method="{method}",path="{path}",status="{status}"'
        lines.append(f"zhike_http_requests_total{{{labels}}} {s['count']}")
    lines += [
        "# HELP zhike_http_request_duration_seconds_sum Total time spent in requests.",
        "# TYPE zhike_http_request_duration_seconds_sum counter",
    ]
    for (method, path, status), s in sorted(items):
        labels = f'method="{method}",path="{path}",status="{status}"'
        lines.append(f"zhike_http_request_duration_seconds_sum{{{labels}}} {s['sum']:.6f}")
    lines += [
        "# HELP zhike_http_request_duration_seconds_max Max observed request duration.",
        "# TYPE zhike_http_request_duration_seconds_max gauge",
    ]
    for (method, path, status), s in sorted(items):
        labels = f'method="{method}",path="{path}",status="{status}"'
        lines.append(f"zhike_http_request_duration_seconds_max{{{labels}}} {s['max']:.6f}")
    lines += [
        "# HELP zhike_llm_calls_total LLM calls by result (ok/fail/fallback).",
        "# TYPE zhike_llm_calls_total counter",
    ]
    for k in ("ok", "fail", "fallback"):
        lines.append(f'zhike_llm_calls_total{{result="{k}"}} {llm[k]}')
    lines += [
        "# HELP zhike_uptime_seconds Process uptime.",
        "# TYPE zhike_uptime_seconds gauge",
        f"zhike_uptime_seconds {time.time() - _STARTED:.1f}",
    ]
    return "\n".join(lines) + "\n"
