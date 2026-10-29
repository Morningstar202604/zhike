"""HTTP/LLM 指标：基于 prometheus_client（业界事实标准）。

替换掉此前手写的 60 行注册表，换来四个实打实的能力：
1. **直方图**：分位数（p50/p95/p99）由客户端计算，比"平均值 + 最大值"更能定位长尾；
2. **多进程安全**：uvicorn --workers N 时用 PROMETHEUS_MULTIPROC_DIR 聚合，
   手写字典在多 worker 下每个进程各报一份，看板永远只看到 1/N 的流量；
3. **标准命名与 exposition**：Grafana 现成模板、Prometheus 抓取规则、告警表达式可直接用；
4. **活跃请求/异常计数**：Gauge 与 Counter 类型语义明确，不靠 Python 字典模拟。

保留旧口径：`zhike_http_requests_total{method,path,status}` 名字与标签完全不变，
已有看板与测试无需改动；新增 `zhike_http_request_duration_seconds` 直方图。

标签基数控制：path 先模板化（/api/sessions/{id}/messages），并限制标签值长度。
"""
from __future__ import annotations

import os
import re
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# 独立 registry：不污染默认 registry（避免测试里重复注册报 Duplicated timeseries）
REGISTRY = CollectorRegistry(auto_describe=True)

_SID_RE = re.compile(r"/[a-f0-9]{16,64}(?=/|$)")
_KB_RE = re.compile(r"/KB[-_]?[A-Za-z0-9]+", re.IGNORECASE)
_NUM_RE = re.compile(r"/\d{2,}(?=/|$)")

REQUEST_COUNT = Counter(
    "zhike_http_requests_total",
    "HTTP requests by route and status.",
    ["method", "path", "status"],
    registry=REGISTRY,
)
REQUEST_DURATION = Histogram(
    "zhike_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)
IN_FLIGHT = Gauge(
    "zhike_http_in_flight_requests",
    "Requests currently being served.",
    registry=REGISTRY,
)
LLM_CALLS = Counter(
    "zhike_llm_calls_total",
    "LLM calls by result.",
    ["result"],
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "zhike_llm_latency_seconds",
    "LLM call latency in seconds.",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 80.0),
    registry=REGISTRY,
)
TOOL_CALLS = Counter(
    "zhike_tool_calls_total",
    "Tool invocations by tool and status.",
    ["tool", "status"],
    registry=REGISTRY,
)
RETRIEVAL_LATENCY = Histogram(
    "zhike_retrieval_latency_seconds",
    "Knowledge-base retrieval latency.",
    ["backend"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=REGISTRY,
)

_STARTED = time.time()
_UPTIME = Gauge("zhike_uptime_seconds", "Process uptime in seconds.", registry=REGISTRY)
_UPTIME.set_function(lambda: time.time() - _STARTED)

# ---- 兼容旧接口（tests/test_security_gaps.py 直接调用） ----

def normalize_path(path: str) -> str:
    """把具体 id 换成占位符，避免指标标签基数随会话数线性增长。"""
    p = path or "/"
    p = _SID_RE.sub("/{id}", p)
    p = _KB_RE.sub("/{kb_id}", p)
    p = _NUM_RE.sub("/{n}", p)
    return p[:120] or "/"


def record_request(method: str, path: str, status: int, duration_s: float) -> None:
    label = normalize_path(path)
    REQUEST_COUNT.labels(method.upper(), label, str(int(status))).inc()
    REQUEST_DURATION.labels(method.upper(), label).observe(max(duration_s, 0.0))


def record_llm(result: str) -> None:
    LLM_CALLS.labels(result if result in ("ok", "fail", "fallback") else "fail").inc()


def record_llm_latency(model: str, seconds: float) -> None:
    LLM_LATENCY.labels((model or "unknown")[:64]).observe(max(seconds, 0.0))


def record_tool(tool: str, status: str) -> None:
    TOOL_CALLS.labels((tool or "unknown")[:64], (status or "unknown")[:32]).inc()


def record_retrieval(backend: str, seconds: float) -> None:
    RETRIEVAL_LATENCY.labels((backend or "unknown")[:32]).observe(max(seconds, 0.0))


def render() -> str:
    """Prometheus exposition 文本（Content-Type 由调用方设置）。"""
    data = generate_latest(REGISTRY)
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


def multiprocess_enabled() -> bool:
    return bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))
