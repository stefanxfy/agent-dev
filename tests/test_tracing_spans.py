"""
M10 C6.1 — OTel tracer span tests for 3 memory paths.

覆盖:
1. retriever.search → memory.search span
2. extraction_gate.should_extract → memory.extract.gate span
3. sm_layer.compact → memory.sm.compact span

采用 InMemorySpanExporter 直接验证 span 触发(OpenTelemetry 官方 pattern)。

隔离策略说明:
OTel API 的 set_tracer_provider 是 once-only 的(进程内首次生效,后续调用被忽略并 warn)。
因此本测试用 monkeypatch 替换 3 个源模块的 `tracer` 属性,让它们走本地
TracerProvider 的 tracer —— 这样每个 test 拿到独立的 exporter。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_core.memory.tracing import TRACER_NAME


# ─────────────────────────────────────
# Fixtures
# ─────────────────────────────────────

@pytest.fixture
def in_memory_exporter(monkeypatch):
    """返回 (exporter, cleanup_fn)。

    实现:
    1. 建本地 TracerProvider + InMemorySpanExporter
    2. 拿本地 tracer(同名 TRACER_NAME,保证 instrumentation scope 对得上)
    3. monkeypatch 3 个源模块的 `tracer` 属性,让 span 流向本地 exporter
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local_tracer = provider.get_tracer(TRACER_NAME)

    # 替换 3 个源模块里的 tracer(让 span 走到本地 exporter)
    from agent_core.memory import retriever
    from agent_core.memory import extraction_gate
    from agent_core.memory import sm_layer
    monkeypatch.setattr(retriever, "tracer", local_tracer)
    monkeypatch.setattr(extraction_gate, "tracer", local_tracer)
    monkeypatch.setattr(sm_layer, "tracer", local_tracer)

    yield exporter


def _get_span_names(exporter):
    return [span.name for span in exporter.get_finished_spans()]


def _get_spans_by_name(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ─────────────────────────────────────
# A: retriever.search span
# ─────────────────────────────────────

def test_retriever_search_emits_memory_search_span(in_memory_exporter, tmp_path):
    """retriever.search() 触发 memory.search span"""
    from agent_core.memory.retriever import MemoryRetriever

    # mock:不依赖真实向量库
    embed_fn = MagicMock()
    embed_fn.encode.return_value = [0.0] * 4
    retriever = MemoryRetriever(
        memory_store=MagicMock(),
        vector_store=MagicMock(),
        embed_fn=embed_fn,
    )
    retriever.search("test query", top_k=3)

    span_names = _get_span_names(in_memory_exporter)
    assert "memory.search" in span_names, (
        f"期望 memory.search span 出现,实际 spans: {span_names}"
    )

    # span 来自我们的 TRACER_NAME tracer
    matching = _get_spans_by_name(in_memory_exporter, "memory.search")
    assert len(matching) == 1
    # 验证 instrumentation scope = 我们的 TRACER_NAME
    assert matching[0].instrumentation_scope.name == TRACER_NAME


# ─────────────────────────────────────
# B: extraction_gate.should_extract span
# ─────────────────────────────────────

def test_extraction_gate_emits_memory_extract_gate_span(in_memory_exporter):
    """ExtractionGate.should_extract() 触发 memory.extract.gate span"""
    from agent_core.memory.extraction_gate import ExtractionGate, TurnContext

    gate = ExtractionGate(
        llm_router=MagicMock(),
        memory_store=MagicMock(),
        session_id="s1",
    )
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=20_000,  # 超 MIN_TOKENS_TO_INIT,过 gate1
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "记住 X"}],
    )
    gate.should_extract(ctx)

    span_names = _get_span_names(in_memory_exporter)
    assert "memory.extract.gate" in span_names, (
        f"期望 memory.extract.gate span 出现,实际 spans: {span_names}"
    )
    matching = _get_spans_by_name(in_memory_exporter, "memory.extract.gate")
    assert len(matching) == 1
    assert matching[0].instrumentation_scope.name == TRACER_NAME


# ─────────────────────────────────────
# C: sm_layer.compact span
# ─────────────────────────────────────

def test_sm_layer_compact_emits_span(in_memory_exporter, tmp_path):
    """SessionMemoryLayer.compact() 触发 memory.sm.compact span"""
    from agent_core.memory.sm_layer import SessionMemoryLayer
    from agent_core.memory.config import MemoryConfig

    sm_md = tmp_path / "sm.md"
    # 写一份"有实质内容"的 SM 文件(否则 compact 返 None)
    sm_md.write_text(
        "---\nsession_id: s1\nschema_version: 1\nlast_compacted_msg_id: null\n"
        "last_compacted_at: null\n---\n\n"
        "# Session Memory\n\n## Context\n真实内容(否则 sm_is_template=True 会直接返 None)\n",
        encoding="utf-8",
    )
    sm = SessionMemoryLayer(
        session_id="s1",
        sm_path=sm_md,
        config=MemoryConfig().compact,
    )
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    sm.compact(messages, context_window=128_000)

    span_names = _get_span_names(in_memory_exporter)
    assert "memory.sm.compact" in span_names, (
        f"期望 memory.sm.compact span 出现,实际 spans: {span_names}"
    )
    matching = _get_spans_by_name(in_memory_exporter, "memory.sm.compact")
    assert len(matching) == 1
    assert matching[0].instrumentation_scope.name == TRACER_NAME
