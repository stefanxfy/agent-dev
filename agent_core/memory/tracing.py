"""
OpenTelemetry tracing (M6 / Day 6)

设计原则：
1. 默认 NoOp:不依赖任何 exporter / collector,纯 import-time 安全
2. 按需启用:检测 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境变量或显式 `configure_tracing()`
3. 单一 tracer:`agent_core.memory`,所有 M5+ 模块共用

不在本模块范围:
- ❌ 真实 OTLP 收集器验证(M7 集成阶段才有 collector)
- ❌ 链路采样策略(default: 全采集)
- ❌ 自定义 span processor

Public API:
- `tracer`: `opentelemetry.trace.Tracer` 实例(无配置时为 NoOp)
- `configure_tracing(...)`: 检测 env 或显式参数,启用真 tracer;返回 True/False
"""

from __future__ import annotations

import os
from typing import Optional

from opentelemetry import trace

# 默认 tracer — 没设 TracerProvider 时 OpenTelemetry API 自动返回 NoOpTracer
# (span 都是 NullSpan,无开销,无副作用)
TRACER_NAME = "agent_core.memory"
tracer = trace.get_tracer(TRACER_NAME)

__all__ = ["tracer", "configure_tracing", "TRACER_NAME"]


def configure_tracing(
    service_name: str = "agent-dev",
    otlp_endpoint: Optional[str] = None,
) -> bool:
    """
    启用真 OTel tracer(配 OTLP exporter)。

    触发条件(任一满足即启用):
    - 显式传入 `otlp_endpoint` 参数
    - 环境变量 `OTEL_EXPORTER_OTLP_ENDPOINT` 非空

    Returns:
        bool: True = 已配置真 tracer;False = 仍 NoOp(没检测到 endpoint)

    用法:
        # 应用启动时:
        from agent_core.memory.tracing import configure_tracing
        if configure_tracing():
            print("✅ OTel 已启用,上报到", os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])

        # 业务代码:
        from agent_core.memory.tracing import tracer
        with tracer.start_as_current_span("memory.extract") as span:
            span.set_attribute("memory.candidates", 3)
            ...
    """
    # 1. 检测 endpoint 来源
    endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False

    # 2. 避免重复初始化(检查现有 provider 是否已配 OTLP)
    current_provider = trace.get_tracer_provider()
    # OTel ProxyTracerProvider 检测略复杂,简单做法:用标记位
    if getattr(configure_tracing, "_initialized", False):
        return True

    # 3. 延迟 import SDK(避免硬依赖 + 启动开销)
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, OTLPSpanExporter

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    trace.set_tracer_provider(provider)

    # 4. 标记已初始化
    configure_tracing._initialized = True  # type: ignore[attr-defined]
    return True


# 类型注解:让 IDE / mypy 知道 configure_tracing 有 _initialized 属性
configure_tracing._initialized = False  # type: ignore[attr-defined]