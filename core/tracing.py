"""OpenTelemetry GenAI tracing（可选依赖）。

业界事实标准（OpenTelemetry GenAI semantic conventions）。装了 opentelemetry-sdk 后：
- pipeline 每个关键节点（guardrail / retrieval / llm_call / tool_call）自动生成 span
- 支持 OTLP gRPC/HTTP 导出到 Jaeger/Tempo/Datadog/阿里云 SLS 等
- 没装时全部 no-op，零开销

启用：
    pip install opentelemetry-sdk opentelemetry-exporter-otlp
    环境变量 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

logger = logging.getLogger("cs.events")

_TRACER = None
_INITIALIZED = False


def _get_tracer():
    global _TRACER, _INITIALIZED
    if _INITIALIZED:
        return _TRACER
    _INITIALIZED = True
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") and not os.environ.get("OTEL_SERVICE_NAME"):
        return None
    try:
        # 只做可用性探测：真正的 tracer 在下面用 _trace.get_tracer 拿
        import opentelemetry.trace  # noqa: F401
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter  # type: ignore
    except ImportError:
        logger.info("opentelemetry-sdk not installed, tracing disabled")
        return None
    try:
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        from opentelemetry import trace as _trace
        _trace.set_tracer_provider(provider)
        _TRACER = _trace.get_tracer("zhike")
        logger.info("OpenTelemetry tracing enabled")
    except Exception as e:
        logger.info("tracing init failed: %s", str(e)[:100])
        _TRACER = None
    return _TRACER


@contextmanager
def span(name: str, attrs: dict | None = None):
    """无侵入 span 上下文管理器；未启用时 yield None。"""
    t = _get_tracer()
    if t is None:
        yield None
        return
    with t.start_as_current_span(name) as s:
        if attrs:
            for k, v in attrs.items():
                try:
                    s.set_attribute(k, v)
                except Exception:
                    pass
        yield s
