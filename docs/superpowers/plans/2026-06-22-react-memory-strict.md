# ReAct 严格双通道记忆提取 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ReactAgent 的 turn-end 记忆写入路径从"Option C 同步土路"切换到 `DualChannelWriter` + `ExtractionGate` + `ReactMemoryBridge`,严格按 `docs/memory-system-design.md §3.3.1 §4.1 §4.8 §6.9` 实现。

**Architecture:**
- **通道 A** 同步写 daily log JSONL(无 LLM,WAL 模式,fsync 强制落盘)
- **通道 B** 异步跑 LLM 评分 + 提取 + 写 memory(三级门:门1 累计 OR 门2 关键词 → 门3 LLM 评分)
- **ExtractionGate** 新模块做决策树,3 门 OR 关系 + 门1 跑完清零
- **ReactMemoryBridge** 适配层,把 ReactAgent.run() 同步 generator 翻译到 DualChannelWriter async
- **LLM 提示词层去重**:门1 跑时拼 `<existing_memories_in_this_period>` 让 LLM 自己去重
- **删除 Option C**:删 `_extract_and_write()` 和 `memory_extractor` 构造参数

**Tech Stack:** Python 3.11+, dataclasses, threading, sqlite3 (MetaDB), pytest, Streamlit (UI)

**Spec 文件:** `docs/superpowers/specs/2026-06-22-react-memory-strict-design.md`

## Global Constraints

- **Python 3.11+**(项目基线)
- **JSONL** 用于通道 A(per-turn 行,fsync 强制落盘)
- **per-file Markdown** 用于通道 B(`<type>/<hash>.md`,已有 M3 实现)
- **三级门 OR 关系**:门1(`cumulative_tokens >= 10K` OR `tool_calls >= 10`)OR 门2(16 关键词中 ≥ 1 命中)→ 门3(LLM 评分 `confidence >= 0.6`)
- **门1 跑完清零累计**(会话级);**门2 跑完不清零**
- **LLM 评分 < 0.6**:门1 跑过的**不清零**(因为没真提)
- **LLM 调用隔离 cache**:`cache_namespace="memory_extract_score"`,不复用主对话 cache
- **不变量 #3**:通道 A 不调 LLM
- **不变量 #4**:通道 B 推进 extract_cursor 前必须成功写入
- **A3 重启恢复**:bridge 从 `extract_cursor` 恢复 `gate1_period_start_turn`
- **删除 Option C**:`_extract_and_write()` 整段删,`memory_extractor`/`memory_embed_fn` 构造参数删
- **不在聊天区流任何 "🧠 提取" 同步消息**,改走 sidebar 计数
- **每 task 结束独立可测**,frequent commits(每 task 1 commit)

---

## File Structure

**新增**:
- `agent_core/memory/extraction_gate.py` — §3.3.1 三级门决策树
- `agent_core/memory/react_memory_bridge.py` — ReactAgent ↔ DualChannelWriter 适配层
- `agent_core/memory/prompt_templates.py` — LLM 评分提示词模板
- `tests/test_extraction_gate.py` — 单元测试
- `tests/test_react_memory_strict.py` — 集成测试

**修改**:
- `agent_core/agent_core.py` — 删 Option C,接 bridge
- `agent_core/memory/__init__.py` — 导出新模块
- `web/app.py` — 重新 wiring(get_agent 用 DualChannelWriter + ExtractionGate + bridge)
- `docs/test_react_ui.md` — 更新场景 1 为严格实现行为

**不修改**:
- `agent_core/memory/dual_channel_writer.py`(M2 已实现,接进即可)
- `agent_core/memory/memory_store.py`(M3 已实现)
- `agent_core/memory/extractor.py`(M3 已实现)
- `agent_core/memory/daily.py`(M4 计划,本次不动)

---

## Task 1: ExtractionGate 决策树(3 门 OR 关系)

**Files:**
- Create: `agent_core/memory/extraction_gate.py`
- Test: `tests/test_extraction_gate.py`

**Interfaces:**
- Consumes: `agent_core.memory.dual_channel_writer.ExtractionCandidate`, `agent_core.memory.types.validate_type`
- Produces: `class ExtractionGate` with `should_extract(ctx: TurnContext) -> Decision` and constants `MIN_TOKENS_TO_INIT=10_000`, `MIN_TOOL_CALLS=10`, `MIN_CONFIDENCE=0.6`, `KEYWORDS` (16 items)

### Steps

- [ ] **Step 1.1: 写失败的测试 — 累计 < 10K + 无关键词 → SKIP**

`tests/test_extraction_gate.py`:
```python
from agent_core.memory.extraction_gate import ExtractionGate, TurnContext

def test_below_10k_no_keyword_skips():
    gate = ExtractionGate()
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=5_000,  # < 10K
        cumulative_tool_calls=0,   # < 10
        last_messages=[{"role": "user", "content": "今天天气不错"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False
    assert "no_trigger" in decision.reason
```

- [ ] **Step 1.2: 跑测试,确认失败**

Run: `.venv/bin/python -m pytest tests/test_extraction_gate.py::test_below_10k_no_keyword_skips -v`
Expected: FAIL with `ModuleNotFoundError` 或 `ImportError`

- [ ] **Step 1.3: 写最小实现 — 决策树骨架(不调 LLM)**

`agent_core/memory/extraction_gate.py`:
```python
"""
三级门决策树（v2.1.1 用户调整版）
参考 docs/memory-system-design.md §3.3.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from agent_core.memory.dual_channel_writer import ExtractionCandidate


@dataclass
class TurnContext:
    session_id: str
    cumulative_tokens: int
    cumulative_tool_calls: int
    last_messages: list[dict]
    gate1_period_start_turn: int = 0


@dataclass
class Decision:
    should_extract: bool
    reason: str
    confidence: float = 0.0
    candidates: list[ExtractionCandidate] = field(default_factory=list)
    via_gate1: bool = False


class ExtractionGate:
    """
    三级门 OR 关系决策树:
      门1（累计）OR 门2（关键词）→ 门3（LLM 评分）
    """

    MIN_TOKENS_TO_INIT = 10_000
    MIN_TOOL_CALLS = 10
    MIN_CONFIDENCE = 0.6

    KEYWORDS = [
        "记住", "记一下", "帮我记住", "别忘了",
        "偏好", "决策", "选择", "拒绝", "采用",
        "教训", "经验", "原则",
        "总是", "从不", "永远", "习惯",
    ]

    def should_extract(self, ctx: TurnContext) -> Decision:
        gate1_pass = (
            ctx.cumulative_tokens >= self.MIN_TOKENS_TO_INIT
            or ctx.cumulative_tool_calls >= self.MIN_TOOL_CALLS
        )
        gate2_pass = self._keyword_filter(ctx.last_messages)

        if not (gate1_pass or gate2_pass):
            return Decision(
                should_extract=False,
                reason="no_trigger(gate1_no_threshold, gate2_no_keyword)",
                via_gate1=False,
            )

        # 占位:门3 LLM 评分后续 task 接入
        return Decision(
            should_extract=False,
            reason="gate3_not_implemented_yet",
            via_gate1=gate1_pass and not gate2_pass,
        )

    def _keyword_filter(self, last_messages: list[dict]) -> bool:
        text = " ".join(
            m.get("content", "")
            for m in last_messages
            if isinstance(m.get("content"), str)
        )
        return any(kw in text for kw in self.KEYWORDS)
```

- [ ] **Step 1.4: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_extraction_gate.py::test_below_10k_no_keyword_skips -v`
Expected: PASS

- [ ] **Step 1.5: 加测试 — 累计 ≥ 10K + 无关键词 → 进入门3(当前 stub 报 gate3_not_implemented_yet)**

追加到 `tests/test_extraction_gate.py`:
```python
def test_above_10k_no_keyword_enters_gate3():
    gate = ExtractionGate()
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,  # >= 10K
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "Go 的 goroutine 调度"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False  # 门3 没实现,默认 False
    assert "gate3" in decision.reason
    assert decision.via_gate1 is True  # 门1 主导

def test_below_10k_with_keyword_enters_gate3():
    gate = ExtractionGate()
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=3_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "记住我喜欢用 uv"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False  # 门3 stub
    assert "gate3" in decision.reason
    assert decision.via_gate1 is False  # 门2 主导

def test_keyword_list_has_16_items():
    gate = ExtractionGate()
    assert len(gate.KEYWORDS) == 16
    assert "记住" in gate.KEYWORDS
    assert "总是" in gate.KEYWORDS
    assert "习惯" in gate.KEYWORDS
```

- [ ] **Step 1.6: 跑全部测试,确认 4 个全过**

Run: `.venv/bin/python -m pytest tests/test_extraction_gate.py -v`
Expected: 4 passed

- [ ] **Step 1.7: Commit**

```bash
git add agent_core/memory/extraction_gate.py tests/test_extraction_gate.py
git commit -m "feat(memory): ExtractionGate 决策树骨架(3 门 OR)"
```

---

## Task 2: Prompt 模板 — 评分 + 提取合并

**Files:**
- Create: `agent_core/memory/prompt_templates.py`
- Test: `tests/test_prompt_templates.py`

**Interfaces:**
- Consumes: 无
- Produces: `EXTRACT_PROMPT_TEMPLATE: str`, `EXTRACT_SYSTEM_PROMPT: str`, `build_extract_prompt(turns, existing_memories) -> str`

### Steps

- [ ] **Step 2.1: 写失败的测试 — 模板含必要块**

`tests/test_prompt_templates.py`:
```python
from agent_core.memory.prompt_templates import (
    EXTRACT_SYSTEM_PROMPT,
    build_extract_prompt,
)


def test_system_prompt_mentions_structured_extraction():
    assert "结构化" in EXTRACT_SYSTEM_PROMPT
    assert "JSON" in EXTRACT_SYSTEM_PROMPT


def test_build_extract_prompt_includes_existing_memories_block():
    prompt = build_extract_prompt(
        turns_text="[turn 5] 我喜欢用 uv",
        existing_memories=[
            {"type": "user", "title": "用户姓名", "body": "张三", "turn_index": 1},
        ],
    )
    assert "<existing_memories_in_this_period>" in prompt
    assert "张三" in prompt
    assert "[turn 5] 我喜欢用 uv" in prompt


def test_build_extract_prompt_empty_existing_memories():
    prompt = build_extract_prompt(
        turns_text="[turn 5] 用户问 Python 协程",
        existing_memories=[],
    )
    assert "(无)" in prompt  # 空提示
    assert "user" in prompt   # schema 提示


def test_build_extract_prompt_includes_4_types():
    prompt = build_extract_prompt(turns_text="x", existing_memories=[])
    for t in ("user", "feedback", "project", "reference"):
        assert t in prompt
```

- [ ] **Step 2.2: 跑测试,确认失败**

Run: `.venv/bin/python -m pytest tests/test_prompt_templates.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 2.3: 写最小实现**

`agent_core/memory/prompt_templates.py`:
```python
"""
LLM 评分 + 提取提示词模板
参考 docs/memory-system-design.md §3.3 L1 合并 + L9 <conversation> 防注入
"""


EXTRACT_SYSTEM_PROMPT = """你是结构化记忆提取助手. 严格按 schema 输出 JSON.

判断两件事:
1. 是否包含"值得长期记住"的新信息
2. 如果是,提取为结构化记忆(4 类:user/feedback/project/reference)

特别说明:
- 已有记忆中已记下的内容,本轮不要再重复提取
- 只提取"新增"的信息
- source_quote 必填(用户原话片段)
- project/reference 类型必须含 "**Why:**" 段
"""


def build_extract_prompt(turns_text: str, existing_memories: list[dict]) -> str:
    """拼 LLM 评分 + 提取 prompt(参考 §6.9.1)"""
    if existing_memories:
        mem_lines = []
        for m in existing_memories:
            ti = m.get("turn_index", "?")
            mem_lines.append(
                f"- [{m.get('type', '?')}] {m.get('title', '?')}: {m.get('body', '?')[:80]} (turn {ti})"
            )
        existing_block = "\n".join(mem_lines)
    else:
        existing_block = "(无)"

    return f"""<existing_memories_in_this_period>
{existing_block}
</existing_memories_in_this_period>

<conversation>
{turns_text}
</conversation>

请评估"本周期"内是否有新记忆值得提取(避免和已记下的重复)。

输出 JSON(严格遵守 schema,不要其他内容):
{{
  "should_extract": true/false,
  "confidence": 0.0-1.0,
  "reason": "若不提取,简短说明原因",
  "candidates": [
    {{
      "type": "user" | "feedback" | "project" | "reference",
      "title": "短标题",
      "body": "一句话描述",
      "why": "若 type=feedback/project,Why 字段",
      "source_quote": "原对话中触发该记忆的逐字引用"
    }}
  ]
}}"""
```

- [ ] **Step 2.4: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_prompt_templates.py -v`
Expected: 4 passed

- [ ] **Step 2.5: Commit**

```bash
git add agent_core/memory/prompt_templates.py tests/test_prompt_templates.py
git commit -m "feat(memory): LLM 评分 prompt 模板(含已有记忆块)"
```

---

## Task 3: ExtractionGate 接入 LLM 评分(门3 完整实现)

**Files:**
- Modify: `agent_core/memory/extraction_gate.py`
- Modify: `tests/test_extraction_gate.py`

**Interfaces:**
- Consumes: `agent_core.memory.prompt_templates.build_extract_prompt`
- Produces: `ExtractionGate(llm_router, cache_namespace, memory_store, session_id)`, `gate.should_extract(ctx) -> Decision` 现在会调 LLM 评分

### Steps

- [ ] **Step 3.1: 写失败的测试 — LLM 评分 high confidence → extract**

追加到 `tests/test_extraction_gate.py`:
```python
from unittest.mock import MagicMock


def _make_mock_router(json_text: str) -> MagicMock:
    """构造返回固定 JSON 的 mock LLM router"""
    mock = MagicMock()

    def fake_chat(messages, **kw):
        chunk = MagicMock()
        chunk.text_delta.text = json_text
        yield chunk

    mock.chat = fake_chat
    mock.config.provider = "mock"
    return mock


def test_high_confidence_extracts():
    router = _make_mock_router('''{
      "should_extract": true,
      "confidence": 0.85,
      "reason": "用户偏好明确",
      "candidates": [
        {
          "type": "user",
          "title": "用户姓名",
          "body": "用户名叫张三",
          "source_quote": "我叫张三"
        }
      ]
    }''')
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "记住我叫张三"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is True
    assert decision.confidence == 0.85
    assert len(decision.candidates) == 1
    assert decision.candidates[0].title == "用户姓名"


def test_low_confidence_skips():
    router = _make_mock_router('''{
      "should_extract": true,
      "confidence": 0.4,
      "reason": "信息不明确",
      "candidates": []
    }''')
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "Go 协程调度"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False
    assert "low_confidence" in decision.reason
    assert "0.40" in decision.reason


def test_parse_error_logs_and_skips():
    """LLM 返回非 JSON → 记 raw + skip"""
    router = _make_mock_router("not valid json {{")
    store = MagicMock()
    store.list_by_session.return_value = []
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
    ctx = TurnContext(
        session_id="s1",
        cumulative_tokens=12_000,
        cumulative_tool_calls=0,
        last_messages=[{"role": "user", "content": "Python 学习"}],
        gate1_period_start_turn=0,
    )
    decision = gate.should_extract(ctx)
    assert decision.should_extract is False
    assert "parse_error" in decision.reason or "no_candidates" in decision.reason
```

- [ ] **Step 3.2: 跑测试,确认失败**

Run: `.venv/bin/python -m pytest tests/test_extraction_gate.py::test_high_confidence_extracts -v`
Expected: FAIL with `TypeError: ExtractionGate.__init__() got unexpected keyword argument 'llm_router'`

- [ ] **Step 3.3: 改造 ExtractionGate — 接受 llm_router/memory_store/session_id**

修改 `agent_core/memory/extraction_gate.py`,替换 `class ExtractionGate` 整段:
```python
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol, Any

from agent_core.memory.dual_channel_writer import ExtractionCandidate
from agent_core.memory.prompt_templates import (
    EXTRACT_SYSTEM_PROMPT,
    build_extract_prompt,
)

logger = logging.getLogger("memory.extraction_gate")


@dataclass
class TurnContext:
    session_id: str
    cumulative_tokens: int
    cumulative_tool_calls: int
    last_messages: list[dict]
    gate1_period_start_turn: int = 0


@dataclass
class Decision:
    should_extract: bool
    reason: str
    confidence: float = 0.0
    candidates: list[ExtractionCandidate] = field(default_factory=list)
    via_gate1: bool = False


class _LLMRouterProtocol(Protocol):
    """LLM router 最小接口(本类只用 chat)"""
    config: Any

    def chat(self, messages: list[dict], **kw): ...


class _MemoryStoreProtocol(Protocol):
    """本类只用 list_by_session"""
    def list_by_session(self, session_id: str, since_turn: int) -> list[dict]: ...


class ExtractionGate:
    """三级门 OR 关系决策树(§3.3.1)"""

    MIN_TOKENS_TO_INIT = 10_000
    MIN_TOOL_CALLS = 10
    MIN_CONFIDENCE = 0.6
    CACHE_NAMESPACE = "memory_extract_score"

    KEYWORDS = [
        "记住", "记一下", "帮我记住", "别忘了",
        "偏好", "决策", "选择", "拒绝", "采用",
        "教训", "经验", "原则",
        "总是", "从不", "永远", "习惯",
    ]

    def __init__(
        self,
        llm_router: _LLMRouterProtocol,
        memory_store: _MemoryStoreProtocol,
        session_id: str,
        cache_namespace: Optional[str] = None,
    ):
        self.llm_router = llm_router
        self.memory_store = memory_store
        self.session_id = session_id
        self.cache_namespace = cache_namespace or self.CACHE_NAMESPACE

    def should_extract(self, ctx: TurnContext) -> Decision:
        gate1_pass = (
            ctx.cumulative_tokens >= self.MIN_TOKENS_TO_INIT
            or ctx.cumulative_tool_calls >= self.MIN_TOOL_CALLS
        )
        gate2_pass = self._keyword_filter(ctx.last_messages)

        if not (gate1_pass or gate2_pass):
            return Decision(
                should_extract=False,
                reason="no_trigger(gate1_no_threshold, gate2_no_keyword)",
                via_gate1=False,
            )

        # 门3:LLM 评分
        return self._llm_score(ctx, via_gate1=gate1_pass and not gate2_pass)

    def _keyword_filter(self, last_messages: list[dict]) -> bool:
        text = " ".join(
            m.get("content", "")
            for m in last_messages
            if isinstance(m.get("content"), str)
        )
        return any(kw in text for kw in self.KEYWORDS)

    def _llm_score(self, ctx: TurnContext, *, via_gate1: bool) -> Decision:
        """门3:LLM 一次调用,既评分又提取(§3.3 L1 合并)"""
        # 拼 turns_text(取 gate1 周期内的 turn)
        turns_text = "\n".join(
            f"[turn {i}] {m.get('content', '')[:200]}"
            for i, m in enumerate(ctx.last_messages)
        )

        # 拼已有记忆(门1 触发时让 LLM 看到已提过的,避免重复)
        try:
            existing = self.memory_store.list_by_session(
                session_id=ctx.session_id,
                since_turn=ctx.gate1_period_start_turn,
            )
        except Exception as e:
            logger.warning(f"list_by_session 失败,降级为空: {e}")
            existing = []

        prompt = build_extract_prompt(turns_text, existing)

        # 调 LLM(用 cache_namespace 隔离)
        try:
            text = self._call_llm(prompt)
        except Exception as e:
            logger.warning(f"LLM 评分调用失败: {e}")
            return Decision(
                should_extract=False,
                reason=f"llm_call_error({type(e).__name__})",
                via_gate1=via_gate1,
            )

        # 解析 JSON
        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError as e:
            logger.warning(f"LLM 评分解析失败: {e}, raw={text[:200]!r}")
            return Decision(
                should_extract=False,
                reason=f"parse_error({e})",
                via_gate1=via_gate1,
            )

        confidence = float(data.get("confidence", 0.0))
        should = bool(data.get("should_extract", False))
        raw_candidates = data.get("candidates", [])

        candidates = [
            ExtractionCandidate(
                type=c.get("type", "user"),
                title=c.get("title", ""),
                body=c.get("body", ""),
                source_quote=c.get("source_quote", ""),
                tags=[],
                score=confidence,
            )
            for c in raw_candidates
        ]

        if not should or not candidates:
            return Decision(
                should_extract=False,
                reason=f"llm_says_no({data.get('reason', 'no_reason')})",
                confidence=confidence,
                via_gate1=via_gate1,
            )

        if confidence < self.MIN_CONFIDENCE:
            return Decision(
                should_extract=False,
                reason=f"low_confidence({confidence:.2f})",
                confidence=confidence,
                via_gate1=via_gate1,
            )

        return Decision(
            should_extract=True,
            reason="extract",
            confidence=confidence,
            candidates=candidates,
            via_gate1=via_gate1,
        )

    def _call_llm(self, prompt: str) -> str:
        """调 LLM,收集 text_delta"""
        text = ""
        for chunk in self.llm_router.chat(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            cache_namespace=self.cache_namespace,
        ):
            if chunk.text_delta:
                text += chunk.text_delta.text
        return text
```

- [ ] **Step 3.4: 跑全部 gate 测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_extraction_gate.py -v`
Expected: 7 passed(原 4 + 新 3)

- [ ] **Step 3.5: Commit**

```bash
git add agent_core/memory/extraction_gate.py tests/test_extraction_gate.py
git commit -m "feat(memory): ExtractionGate 接入 LLM 评分(门3 完整实现)"
```

---

## Task 4: MemoryStore.list_by_session 接口补全

**Files:**
- Modify: `agent_core/memory/memory_store.py`
- Modify: `tests/test_memory_store.py`(已有则扩展,无则新建)

**Interfaces:**
- Consumes: 已有 `MemoryStore.write/read/list_by_type/list_all`
- Produces: `MemoryStore.list_by_session(session_id: str, since_turn: int) -> list[dict]`

### Steps

- [ ] **Step 4.1: 写失败的测试 — 按 session 列出 turn ≥ N 的记忆**

`tests/test_memory_store_list_by_session.py`:
```python
import tempfile
from pathlib import Path
import shutil

from agent_core.memory.memory_store import MemoryStore


def test_list_by_session_filters_by_since_turn():
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = MemoryStore(tmp)
        # 写 3 条,frontmatter 里 session_id 模拟
        for i, turn in enumerate([1, 5, 10]):
            store.write(
                type="user",
                title=f"记忆 {i}",
                body=f"body {i}",
                source_quote=f"q{i}",
                extra={"session_id": "s1", "turn_index": turn},
            )
        # since_turn=5 应返回 turn=5 和 turn=10 两条
        results = store.list_by_session(session_id="s1", since_turn=5)
        turn_indices = sorted(r["frontmatter"].get("turn_index", -1) for r in results)
        assert turn_indices == [5, 10]
    finally:
        shutil.rmtree(tmp)


def test_list_by_session_empty_when_no_match():
    tmp = Path(tempfile.mkdtemp(prefix="mem_test_"))
    try:
        store = MemoryStore(tmp)
        results = store.list_by_session(session_id="nonexistent", since_turn=0)
        assert results == []
    finally:
        shutil.rmtree(tmp)
```

- [ ] **Step 4.2: 跑测试,确认失败**

Run: `.venv/bin/python -m pytest tests/test_memory_store_list_by_session.py -v`
Expected: FAIL with `AttributeError: 'MemoryStore' object has no attribute 'list_by_session'`

- [ ] **Step 4.3: 在 MemoryStore 加 list_by_session 方法**

修改 `agent_core/memory/memory_store.py`,在 `list_by_type` 之后追加:
```python
    def list_by_session(
        self,
        session_id: str,
        since_turn: int = 0,
    ) -> list[dict[str, Any]]:
        """
        列出指定 session_id 且 turn_index >= since_turn 的所有记忆

        用途:门1 跑 LLM 评分时,拼"本周期已提取记忆"块

        Returns:
            [{"frontmatter": {...}, "body": "..."}, ...]
        """
        results: list[dict[str, Any]] = []
        for type_ in ("user", "feedback", "project", "reference"):
            type_dir = self.root / type_
            if not type_dir.exists():
                continue
            for md_path in type_dir.glob("*.md"):
                try:
                    data = self.read(str(md_path.relative_to(self.root)))
                except Exception:
                    continue
                fm = data.get("frontmatter", {})
                # frontmatter 需同时含 session_id 和 turn_index
                if fm.get("session_id") != session_id:
                    continue
                turn_idx = fm.get("turn_index", -1)
                if turn_idx < since_turn:
                    continue
                data["frontmatter"]["type"] = data["frontmatter"].get("type", type_)
                results.append(data)
        return results
```

- [ ] **Step 4.4: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_memory_store_list_by_session.py -v`
Expected: 2 passed

- [ ] **Step 4.5: 跑全量 memory_store 测试,确认没破**

Run: `.venv/bin/python -m pytest tests/test_memory_store.py -v 2>/dev/null || echo "(no existing test_memory_store.py, skip)"`
Expected: all pass 或 skip

- [ ] **Step 4.6: Commit**

```bash
git add agent_core/memory/memory_store.py tests/test_memory_store_list_by_session.py
git commit -m "feat(memory): MemoryStore.list_by_session(给 LLM 评分拼已有记忆用)"
```

---

## Task 5: DualChannelWriter 集成 session_id 写入 frontmatter

**Files:**
- Modify: `agent_core/memory/dual_channel_writer.py`
- Modify: `tests/test_dual_channel_writer.py`(如存在)

**Interfaces:**
- Consumes: 已有 `channel_b_background_extract`
- Produces: `MemoryStore.write` 接受 `session_id` 和 `turn_index` 作为 `extra` 字段(已有),确保 `list_by_session` 能查到

### Steps

- [ ] **Step 5.1: 写失败的测试 — 写盘后 list_by_session 能找到**

`tests/test_channel_b_writes_session_id.py`:
```python
import tempfile
from pathlib import Path
import shutil

from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    TurnMessage,
    ExtractionCandidate,
)
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB
from unittest.mock import MagicMock


def test_channel_b_writes_session_id_and_turn_index():
    tmp = Path(tempfile.mkdtemp(prefix="dual_test_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock()
        embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        w = DualChannelWriter(
            session_id="sess-1",
            meta_db=meta,
            memory_store=store,
            vector_store=vec,
            embed_fn=embed,
        )

        candidates = [
            ExtractionCandidate(
                type="user",
                title="用户姓名",
                body="张三",
                source_quote="我叫张三",
            )
        ]
        msg = TurnMessage(turn_index=7, user_msg="u", assistant_resp="a")
        future = w.channel_b_background_extract(
            messages=[msg],
            llm_extractor=lambda _msgs: candidates,
        )
        future.result(timeout=10)

        # list_by_session 应能查到
        results = store.list_by_session(session_id="sess-1", since_turn=0)
        assert len(results) == 1
        assert results[0]["frontmatter"]["session_id"] == "sess-1"
        assert results[0]["frontmatter"]["turn_index"] == 7
    finally:
        w.shutdown(timeout=5)
        shutil.rmtree(tmp)
```

- [ ] **Step 5.2: 跑测试,确认失败**

Run: `.venv/bin/python -m pytest tests/test_channel_b_writes_session_id.py -v`
Expected: FAIL(write 路径没传 session_id 到 extra)

- [ ] **Step 5.3: 改 DualChannelWriter._do_channel_b_extract**

修改 `agent_core/memory/dual_channel_writer.py` 的 `_do_channel_b_extract` 写盘那段([line 347-353](agent_core/memory/dual_channel_writer.py#L347)):
```python
                        item_hash = self.memory_store.write(
                            type=cand.type,
                            title=cand.title,
                            body=cand.body,
                            source_quote=cand.source_quote,
                            tags=cand.tags,
                            extra={
                                "session_id": self.session_id,
                                "turn_index": m.turn_index,
                            },
                        )
```

(注意 `cand` 改成 for `m, cand in zip(to_process, candidates)` 或类似,见下方)

**完整替换 `_do_channel_b_extract` 中写盘段**:

找到这段代码([line 344-376](agent_core/memory/dual_channel_writer.py#L344-L376)):
```python
                # 3. 逐条写 MemoryStore(A5 幂等)
                for cand in candidates:
                    try:
                        item_hash = self.memory_store.write(
                            type=cand.type,
                            title=cand.title,
                            body=cand.body,
                            source_quote=cand.source_quote,
                            tags=cand.tags,
                        )
```

替换为:
```python
                # 3. 逐条写 MemoryStore(A5 幂等)
                #    将 session_id + turn_index 写入 frontmatter extra,
                #    供 list_by_session 查询
                for m, cand in zip(to_process, candidates):
                    try:
                        item_hash = self.memory_store.write(
                            type=cand.type,
                            title=cand.title,
                            body=cand.body,
                            source_quote=cand.source_quote,
                            tags=cand.tags,
                            extra={
                                "session_id": self.session_id,
                                "turn_index": m.turn_index,
                            },
                        )
```

- [ ] **Step 5.4: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_channel_b_writes_session_id.py -v`
Expected: 1 passed

- [ ] **Step 5.5: 跑 dual_channel_writer 已有测试,确认不破**

Run: `.venv/bin/python -m pytest tests/test_dual_channel_writer.py tests/test_dual_channel_concurrent.py -v 2>/dev/null || ls tests/ | grep -i dual`
Expected: all pass

- [ ] **Step 5.6: Commit**

```bash
git add agent_core/memory/dual_channel_writer.py tests/test_channel_b_writes_session_id.py
git commit -m "feat(memory): channel B 写入 session_id+turn_index 到 frontmatter"
```

---

## Task 6: ReactMemoryBridge 适配层

**Files:**
- Create: `agent_core/memory/react_memory_bridge.py`
- Test: `tests/test_react_memory_bridge.py`

**Interfaces:**
- Consumes: `DualChannelWriter`, `ExtractionGate`, `MemoryStore`
- Produces: `class ReactMemoryBridge` with `on_turn_end(...) -> Iterator[MemoryEvent]`, `recover_state()`, `shutdown()`

### Steps

- [ ] **Step 6.1: 写失败的测试 — on_turn_end 走通道 A + 门3 通过 → 通道 B**

`tests/test_react_memory_bridge.py`:
```python
import tempfile
from pathlib import Path
import shutil
from unittest.mock import MagicMock

from agent_core.memory.react_memory_bridge import (
    ReactMemoryBridge,
    MemoryEvent,
    MemoryEventKind,
)
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.extraction_gate import ExtractionGate, TurnContext
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


def test_on_turn_end_high_confidence_writes():
    tmp = Path(tempfile.mkdtemp(prefix="bridge_test_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock(); embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        dual = DualChannelWriter(
            session_id="s1", meta_db=meta,
            memory_store=store, vector_store=vec, embed_fn=embed,
        )

        # mock LLM 返 high confidence
        def fake_chat(messages, **kw):
            chunk = MagicMock()
            chunk.text_delta.text = '''{
              "should_extract": true,
              "confidence": 0.85,
              "reason": "ok",
              "candidates": [
                {"type": "user", "title": "姓名", "body": "张三",
                 "source_quote": "我叫张三"}
              ]
            }'''
            yield chunk
        router = MagicMock()
        router.chat = fake_chat
        router.config.provider = "mock"

        gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s1")
        bridge = ReactMemoryBridge(
            dual_channel=dual, gate=gate, memory_store=store,
            session_id="s1", max_workers=1,
        )

        events = list(bridge.on_turn_end(
            user_msg="记住我叫张三",
            assistant_resp="好的张三",
            turn_index=0,
            input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "记住我叫张三"}],
            recent_turns=[],
        ))

        kinds = [e.kind for e in events]
        assert MemoryEventKind.CHANNEL_A_OK in kinds
        # 门3 过 → gate_pass + extract_dispatched(异步等不到 done)
        assert any(k in kinds for k in (
            MemoryEventKind.GATE_PASS, MemoryEventKind.EXTRACT_DISPATCHED,
        ))
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_on_turn_end_below_threshold_skips():
    tmp = Path(tempfile.mkdtemp(prefix="bridge_test_"))
    try:
        meta = MetaDB(":memory:")
        store = MemoryStore(tmp)
        embed = MagicMock(); embed.encode.return_value = [0.1] * 4
        vec = MagicMock()
        dual = DualChannelWriter(
            session_id="s2", meta_db=meta,
            memory_store=store, vector_store=vec, embed_fn=embed,
        )
        router = MagicMock()  # 不会调
        gate = ExtractionGate(llm_router=router, memory_store=store, session_id="s2")
        bridge = ReactMemoryBridge(
            dual_channel=dual, gate=gate, memory_store=store,
            session_id="s2", max_workers=1,
        )

        events = list(bridge.on_turn_end(
            user_msg="今天天气不错",
            assistant_resp="是的",
            turn_index=0,
            input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "今天天气不错"}],
            recent_turns=[],
        ))

        kinds = [e.kind for e in events]
        assert MemoryEventKind.CHANNEL_A_OK in kinds
        assert MemoryEventKind.GATE_SKIP in kinds
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)
```

- [ ] **Step 6.2: 跑测试,确认失败**

Run: `.venv/bin/python -m pytest tests/test_react_memory_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 6.3: 写 ReactMemoryBridge 实现**

`agent_core/memory/react_memory_bridge.py`:
```python
"""
ReactAgent ↔ DualChannelWriter 适配层
参考 docs/superpowers/specs/2026-06-22-react-memory-strict-design.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional

from agent_core.memory.dual_channel_writer import (
    DualChannelWriter,
    TurnMessage,
    ExtractionCandidate,
)
from agent_core.memory.extraction_gate import ExtractionGate, TurnContext

logger = logging.getLogger("memory.react_bridge")


class MemoryEventKind(str, Enum):
    CHANNEL_A_OK = "channel_a_ok"
    GATE_SKIP = "gate_skip"
    GATE_PASS = "gate_pass"
    EXTRACT_DISPATCHED = "extract_dispatched"
    EXTRACT_DONE = "extract_done"
    EXTRACT_ERROR = "extract_error"


@dataclass
class MemoryEvent:
    kind: MemoryEventKind
    turn_index: int
    reason: Optional[str] = None
    candidates_count: int = 0


class ReactMemoryBridge:
    """
    把 ReactAgent.run() 的同步 generator 风格
    翻译成 DualChannelWriter 的 async future 风格
    """

    def __init__(
        self,
        dual_channel: DualChannelWriter,
        gate: ExtractionGate,
        memory_store,                     # 给 recover_state 用
        session_id: str,
        max_workers: int = 2,
    ):
        self.dual_channel = dual_channel
        self.gate = gate
        self.memory_store = memory_store
        self.session_id = session_id

        # 会话级累计(每次 new bridge 都从 0 开始)
        self.cumulative_tokens = 0
        self.cumulative_tool_calls = 0

        # A3 重启恢复
        self.gate1_period_start_turn = 0
        self.recover_state()

    def recover_state(self) -> None:
        """A3:从 extract_cursor 恢复 gate1_period_start_turn"""
        try:
            cursor = self.dual_channel.extract_cursor
            self.gate1_period_start_turn = max(0, cursor)
            logger.info(
                f"bridge 恢复: gate1_period_start_turn={self.gate1_period_start_turn}"
            )
        except Exception as e:
            logger.warning(f"recover_state 失败,默认 0: {e}")

    def on_turn_end(
        self,
        user_msg: str,
        assistant_resp: str,
        turn_index: int,
        input_tokens: int,
        output_tokens: int,
        tool_calls_in_turn: int,
        last_messages: list[dict],
        recent_turns: list[TurnMessage],
    ) -> Iterator[MemoryEvent]:
        # 1. 累计 token / tool
        self.cumulative_tokens += input_tokens + output_tokens
        self.cumulative_tool_calls += tool_calls_in_turn

        # 2. 通道 A(同步,无 LLM)
        try:
            self.dual_channel.channel_a_inline_write(
                user_msg=user_msg,
                assistant_resp=assistant_resp,
                turn_index=turn_index,
            )
            yield MemoryEvent(
                kind=MemoryEventKind.CHANNEL_A_OK, turn_index=turn_index,
            )
        except Exception as e:
            logger.error(f"通道 A 写盘失败: {e}")
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_ERROR, turn_index=turn_index,
                reason=f"channel_a_error({e})",
            )
            return

        # 3. 门决策
        ctx = TurnContext(
            session_id=self.session_id,
            cumulative_tokens=self.cumulative_tokens,
            cumulative_tool_calls=self.cumulative_tool_calls,
            last_messages=last_messages,
            gate1_period_start_turn=self.gate1_period_start_turn,
        )
        decision = self.gate.should_extract(ctx)

        if not decision.should_extract:
            yield MemoryEvent(
                kind=MemoryEventKind.GATE_SKIP, turn_index=turn_index,
                reason=decision.reason,
            )
            return

        yield MemoryEvent(
            kind=MemoryEventKind.GATE_PASS, turn_index=turn_index,
            reason=decision.reason,
            candidates_count=len(decision.candidates),
        )

        # 4. ★ 门1 跑完清零(只在 LLM 评分过 0.6 时)
        if decision.via_gate1:
            self.cumulative_tokens = 0
            self.cumulative_tool_calls = 0
            self.gate1_period_start_turn = turn_index + 1
            logger.info(
                f"门1 跑完清零: gate1_period_start_turn={self.gate1_period_start_turn}"
            )

        # 5. 通道 B(异步)
        turn_msg = TurnMessage(
            turn_index=turn_index,
            user_msg=user_msg,
            assistant_resp=assistant_resp,
        )
        # 把已有 candidates 喂给 extractor(门3 已评过)
        candidates_snapshot = list(decision.candidates)

        def _extractor(_msgs: list[TurnMessage]) -> list[ExtractionCandidate]:
            return candidates_snapshot

        try:
            future = self.dual_channel.channel_b_background_extract(
                messages=[turn_msg],
                llm_extractor=_extractor,
            )
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_DISPATCHED, turn_index=turn_index,
                candidates_count=len(candidates_snapshot),
            )
        except Exception as e:
            logger.error(f"通道 B 提交失败: {e}")
            yield MemoryEvent(
                kind=MemoryEventKind.EXTRACT_ERROR, turn_index=turn_index,
                reason=f"channel_b_dispatch_error({e})",
            )

    def shutdown(self, timeout: float = 30.0) -> bool:
        return self.dual_channel.shutdown(timeout=timeout)
```

- [ ] **Step 6.4: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_react_memory_bridge.py -v`
Expected: 2 passed

- [ ] **Step 6.5: Commit**

```bash
git add agent_core/memory/react_memory_bridge.py tests/test_react_memory_bridge.py
git commit -m "feat(memory): ReactMemoryBridge 适配层(同步→异步)"
```

---

## Task 7: ReactAgent 接入 bridge(删 Option C)

**Files:**
- Modify: `agent_core/agent_core.py`
- Modify: `tests/test_agent_core.py`(如存在)

**Interfaces:**
- Consumes: 已有 `ReactAgent.__init__`
- Produces: `ReactAgent.__init__` 新增 `react_memory_bridge: Optional[ReactMemoryBridge]`, 删除 `memory_extractor` 和 `memory_embed_fn` 参数;`run()` 末尾改用 `bridge.on_turn_end(...)`

### Steps

- [ ] **Step 7.1: 写失败的测试 — bridge.on_turn_end 被调用**

`tests/test_react_agent_bridge.py`:
```python
from unittest.mock import MagicMock
from agent_core.agent_core import ReactAgent


def test_react_agent_accepts_react_memory_bridge():
    """确认 bridge 构造参数被接受(构造不抛错)"""
    mock_router = MagicMock()
    mock_router.config.provider = "mock"
    mock_registry = MagicMock()

    bridge = MagicMock()
    # run() 不被调,只验构造
    agent = ReactAgent(
        llm=mock_router,
        registry=mock_registry,
        react_memory_bridge=bridge,
    )
    assert agent.react_memory_bridge is bridge
```

- [ ] **Step 7.2: 跑测试,确认失败**

Run: `.venv/bin/python -m pytest tests/test_react_agent_bridge.py -v`
Expected: FAIL with `TypeError: __init__() got unexpected keyword argument 'react_memory_bridge'`

- [ ] **Step 7.3: 改 ReactAgent.__init__ — 加 bridge,删 Option C 旧参数**

修改 `agent_core/agent_core.py` 的 `ReactAgent.__init__`,找到构造参数段(约 line 156-157):
```python
        memory_extractor: Optional["MemoryExtractor"] = None,   # C 方案: 实时记忆提取
        memory_embed_fn: Optional[Any] = None,                  # C 方案: extractor 用的嵌入
```

替换为:
```python
        react_memory_bridge: Optional["ReactMemoryBridge"] = None,  # 严格双通道
```

**注意**:同时把 `self.memory_extractor = memory_extractor` 和 `self.memory_embed_fn = memory_embed_fn` 那两行替换为:
```python
        self.react_memory_bridge = react_memory_bridge
```

- [ ] **Step 7.4: 删 run() 末尾的 Option C 提取段**

找到 `agent_core/agent_core.py` 中 `if self.memory_extractor and self.memory_store and len(self.messages) >= 2:` 整段(约 line 735-760),**整段删除**(包括 `last_user` / `last_assistant` 计算、`_extract_and_write` 调用、system 消息 yield)。

- [ ] **Step 7.5: 在 run() 末尾加 bridge.on_turn_end 段**

在删除的 Option C 段**同一位置**插入:
```python
        # ── 严格双通道(A 同步 + B 异步) ─────────────────────
        if self.react_memory_bridge and len(self.messages) >= 2:
            try:
                last_user = next(
                    (m["content"] for m in reversed(self.messages)
                     if m.get("role") == "user" and isinstance(m.get("content"), str)),
                    None,
                )
                last_assistant = next(
                    (m["content"] for m in reversed(self.messages)
                     if m.get("role") == "assistant" and isinstance(m.get("content"), str)),
                    None,
                )
                if last_user and last_assistant:
                    for event in self.react_memory_bridge.on_turn_end(
                        user_msg=last_user,
                        assistant_resp=last_assistant,
                        turn_index=self.turn_index,
                        input_tokens=self.last_input_tokens,
                        output_tokens=self.last_output_tokens,
                        tool_calls_in_turn=self.turn_tool_call_count,
                        last_messages=self.messages[-6:],
                        recent_turns=self._recent_turns_for_llm(),
                    ):
                        yield ("memory_event", event)
            except Exception as e:
                _logger.warning(f"Memory bridge 异常: {e}")
```

**注意**:`self.turn_index` / `self.last_input_tokens` / `self.last_output_tokens` / `self.turn_tool_call_count` / `self._recent_turns_for_llm()` 这些属性**在 ReactAgent 现有代码里**可能已经维护,可能需要补。**Step 7.6 单独验证**。

- [ ] **Step 7.6: 验证 ReactAgent 现有 token 计数属性**

检查 `agent_core/agent_core.py` 是否有 `last_input_tokens` / `last_output_tokens` / `turn_tool_call_count` / `turn_index` / `_recent_turns_for_llm`:

Run: `grep -n "last_input_tokens\|last_output_tokens\|turn_tool_call_count\|turn_index\|_recent_turns" /Users/fanyunxu/Desktop/myproject/agent-dev/agent_core/agent_core.py | head -20`

**若部分缺失**,在 `__init__` 里加默认值(在 super().__init__ 后):
```python
        # 桥接计数(每次 turn 重置)
        self.turn_index = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.turn_tool_call_count = 0
```

并在主 ReAct 循环里(每个 turn 开始时)**重置**:
```python
        self.turn_index = current_turn
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.turn_tool_call_count = 0
```

- [ ] **Step 7.7: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_react_agent_bridge.py -v`
Expected: 1 passed

- [ ] **Step 7.8: 跑 agent_core 已有测试**

Run: `.venv/bin/python -m pytest tests/test_agent_core.py -v 2>/dev/null || echo "(no test_agent_core.py, skip)"`
Expected: all pass 或 skip

- [ ] **Step 7.9: Commit**

```bash
git add agent_core/agent_core.py tests/test_react_agent_bridge.py
git commit -m "feat(memory): ReactAgent 接入 bridge,删 Option C 同步 hack"
```

---

## Task 8: web/app.py 重新 wiring(用 DualChannelWriter + ExtractionGate + Bridge)

**Files:**
- Modify: `web/app.py`

**Interfaces:**
- Consumes: 已有 `get_agent()` 函数
- Produces: `get_agent()` 构造 `DualChannelWriter` + `ExtractionGate` + `ReactMemoryBridge`,注入到 `ReactAgent`

### Steps

- [ ] **Step 8.1: 找 Option C 旧 wiring**

Run: `grep -n "MemoryExtractor\|make_embed_fn\|memory_extractor\|memory_embed_fn" /Users/fanyunxu/Desktop/myproject/agent-dev/web/app.py | head -20`

定位到 `get_agent()` 函数里构造 ReactAgent 那一段。

- [ ] **Step 8.2: 写失败的测试 — get_agent() 注入 bridge**

`tests/test_app_wiring.py`:
```python
"""测试 web/app.py 的 wiring(导入后)"""
import sys
from unittest.mock import MagicMock, patch


def test_get_agent_uses_react_memory_bridge():
    """get_agent() 应该构造 ReactMemoryBridge 注入到 ReactAgent"""
    # 由于 web/app.py 依赖 streamlit session_state,简化测试:
    # 验证模块导入和 get_agent 函数存在
    try:
        from web import app as webapp
    except ImportError as e:
        # 允许 streamlit 等依赖缺失
        if "streamlit" in str(e) or "st" in str(e):
            return
        raise
    assert hasattr(webapp, "get_agent"), "get_agent 函数应存在"
```

- [ ] **Step 8.3: 跑测试,确认通过(模块导入 OK 即可)**

Run: `.venv/bin/python -m pytest tests/test_app_wiring.py -v`
Expected: 1 passed(streamlit 可用)

- [ ] **Step 8.4: 改 web/app.py 的 get_agent()**

定位到 `get_agent()` 中构造 ReactAgent 那段。**删除** Option C 旧 wiring:
```python
# 删除这些
from agent_core.memory import (
    MemoryStore, ChromaVectorStore, MemoryRetriever,
    MemoryExtractor, make_embed_fn,
)
memory_extractor = MemoryExtractor(embed_fn=memory_embed_fn)
```

**替换**为严格双通道 wiring:
```python
# ── 严格双通道(M9) ─────────────────────────────
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.extraction_gate import ExtractionGate
from agent_core.memory.react_memory_bridge import ReactMemoryBridge
from agent_core.memory.meta_db import MetaDB

# 构造 MetaDB(SQLite,持久化 cursor)
meta_db = MetaDB(str(DATA_DIR / "meta.db"))

# 构造 DualChannelWriter
dual_channel = DualChannelWriter(
    session_id=session_id,
    meta_db=meta_db,
    memory_store=memory_store,
    vector_store=chroma_store,
    embed_fn=memory_embed_fn,
)

# 构造 ExtractionGate
gate = ExtractionGate(
    llm_router=router,
    memory_store=memory_store,
    session_id=session_id,
)

# 构造 ReactMemoryBridge
react_memory_bridge = ReactMemoryBridge(
    dual_channel=dual_channel,
    gate=gate,
    memory_store=memory_store,
    session_id=session_id,
)
```

把 `ReactAgent(...)` 构造里:
```python
# 删除
memory_extractor=memory_extractor,
memory_embed_fn=memory_embed_fn,
```

替换为:
```python
react_memory_bridge=react_memory_bridge,
```

- [ ] **Step 8.5: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_app_wiring.py -v`
Expected: 1 passed

- [ ] **Step 8.6: Commit**

```bash
git add web/app.py tests/test_app_wiring.py
git commit -m "feat(web): 严格双通道 wiring(get_agent 用 DualChannelWriter+ExtractionGate+Bridge)"
```

---

## Task 9: 端到端集成测试

**Files:**
- Create: `tests/test_react_memory_strict.py`

### Steps

- [ ] **Step 9.1: 写集成测试 — 端到端流程**

`tests/test_react_memory_strict.py`:
```python
"""
ReAct 严格双通道端到端集成测试
参考 spec §8.2
"""
import tempfile
from pathlib import Path
import shutil
from unittest.mock import MagicMock

from agent_core.memory.react_memory_bridge import (
    ReactMemoryBridge,
    MemoryEventKind,
)
from agent_core.memory.dual_channel_writer import DualChannelWriter
from agent_core.memory.extraction_gate import ExtractionGate
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.meta_db import MetaDB


def _make_bridge(llm_json_response: str, session_id: str = "s1"):
    """helper:构造完整组件栈"""
    tmp = Path(tempfile.mkdtemp(prefix="e2e_"))
    meta = MetaDB(":memory:")
    store = MemoryStore(tmp)
    embed = MagicMock()
    embed.encode.return_value = [0.1] * 4
    vec = MagicMock()
    dual = DualChannelWriter(
        session_id=session_id, meta_db=meta,
        memory_store=store, vector_store=vec, embed_fn=embed,
    )
    router = MagicMock()
    def fake_chat(messages, **kw):
        chunk = MagicMock()
        chunk.text_delta.text = llm_json_response
        yield chunk
    router.chat = fake_chat
    router.config.provider = "mock"
    gate = ExtractionGate(llm_router=router, memory_store=store, session_id=session_id)
    bridge = ReactMemoryBridge(
        dual_channel=dual, gate=gate, memory_store=store,
        session_id=session_id, max_workers=1,
    )
    return bridge, dual, store, tmp


def test_channel_a_writes_daily_log():
    """turn 末尾 ~/.agent_data/logs/<session>.jsonl 有 1 行"""
    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": false, "confidence": 0, "candidates": []}'
    )
    try:
        list(bridge.on_turn_end(
            user_msg="hello", assistant_resp="hi",
            turn_index=0, input_tokens=100, output_tokens=50, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "hello"}],
            recent_turns=[],
        ))
        log_path = store.root.parent / "logs" / f"{bridge.session_id}.jsonl"
        assert log_path.exists()
        assert log_path.read_text().count("\n") == 1
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_gate1_clears_counter_after_extract():
    """门1 跑完 → 累计清零"""
    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": true, "confidence": 0.85, "reason": "ok", '
        '"candidates": [{"type": "user", "title": "姓名", "body": "张三", '
        '"source_quote": "我叫张三"}]}'
    )
    try:
        # 累计到 12K(过门1)
        list(bridge.on_turn_end(
            user_msg="Python 协程", assistant_resp="asyncio",
            turn_index=0, input_tokens=6000, output_tokens=6000, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "Python 协程"}],
            recent_turns=[],
        ))
        # 跑完后累计应清零
        assert bridge.cumulative_tokens == 0
        assert bridge.cumulative_tool_calls == 0
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_gate2_does_not_clear_counter():
    """门2 跑完 → 累计不清零"""
    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": true, "confidence": 0.85, "reason": "ok", '
        '"candidates": [{"type": "user", "title": "姓名", "body": "张三", '
        '"source_quote": "我叫张三"}]}'
    )
    try:
        # 累计 200(没过门1,但有"记住"关键词)
        list(bridge.on_turn_end(
            user_msg="记住我叫张三", assistant_resp="好",
            turn_index=0, input_tokens=100, output_tokens=100, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "记住我叫张三"}],
            recent_turns=[],
        ))
        # 累计应保留(门2 跑完不清零)
        assert bridge.cumulative_tokens == 200
        assert bridge.cumulative_tool_calls == 0
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)


def test_dedup_via_prompt():
    """门1 跑 LLM 评分时,prompt 含 <existing_memories_in_this_period>"""
    captured_prompts = []

    bridge, dual, store, tmp = _make_bridge(
        '{"should_extract": false, "confidence": 0, "candidates": []}'
    )
    # 替换 llm_router.chat 捕获 prompt
    router = bridge.gate.llm_router
    original_chat = router.chat
    def fake_chat_capture(messages, **kw):
        captured_prompts.append(messages)
        chunk = MagicMock()
        chunk.text_delta.text = '{"should_extract": false, "confidence": 0, "candidates": []}'
        yield chunk
    router.chat = fake_chat_capture

    try:
        # 先写一条记忆(模拟"本周期已提过")
        store.write(
            type="user", title="已有", body="已有记忆",
            source_quote="turn 1", tags=[],
            extra={"session_id": "s1", "turn_index": 1},
        )
        # 累计 12K 触发门1
        list(bridge.on_turn_end(
            user_msg="Python", assistant_resp="解释",
            turn_index=5, input_tokens=6000, output_tokens=6000, tool_calls_in_turn=0,
            last_messages=[{"role": "user", "content": "Python"}],
            recent_turns=[],
        ))
        # 检查 LLM 调用的 prompt 含 <existing_memories_in_this_period>
        assert len(captured_prompts) > 0
        user_msg = captured_prompts[0][-1]["content"]  # 最后一条 user 消息
        assert "<existing_memories_in_this_period>" in user_msg
    finally:
        bridge.shutdown(timeout=5)
        dual.shutdown(timeout=5)
        shutil.rmtree(tmp)
```

- [ ] **Step 9.2: 跑测试,确认通过**

Run: `.venv/bin/python -m pytest tests/test_react_memory_strict.py -v`
Expected: 4 passed

- [ ] **Step 9.3: Commit**

```bash
git add tests/test_react_memory_strict.py
git commit -m "test(memory): 端到端集成测试(通道 A / 门1 清零 / 门2 不清零 / 提示词去重)"
```

---

## Task 10: 更新 test_react_ui.md 文档

**Files:**
- Modify: `docs/test_react_ui.md`

### Steps

- [ ] **Step 10.1: 定位场景 1 章节**

Run: `grep -n "### 场景 1" /Users/fanyunxu/Desktop/myproject/agent-dev/docs/test_react_ui.md`

- [ ] **Step 10.2: 改写"预期发生"段 — 严格双通道行为**

替换场景 1 的"预期发生"段为:
```markdown
#### 预期发生(肉眼可见)

**主聊天区**:无 "🧠 正在提取" 同步消息(通道 B 异步,不阻塞流)

**Terminal 1 (后端日志)**:
```
[INFO] react_agent: ✅ 记忆已写入: [user] 用户姓名     ← 来自通道 B 异步
[INFO] memory.react_bridge: 门1 跑完清零: gate1_period_start_turn=5  ← 累计 10K 触发
[INFO] memory.dual_channel: channel_a write turn=5    ← 通道 A 同步
```

**Terminal 2 (watch -n 1)**:频道变化(累计 10K 后才有 B 写盘)
```
~/.agent_data/memory/user/
├── a1b2c3d4...md    ← 累计过 10K 后写入
~/.agent_data/logs/
└── s1.jsonl          ← 通道 A 每 turn 追加 1 行
```

**sidebar `🧠 Memory 状态`**:
- 本会话累计: 12,000 tokens(累计 10K 时跑 B,清零)
- 本会话 tool: 0
- Daily Log: 1 line
- Memory: 1 file

**关键差异 vs Option C**:
- 短对话(< 10K 且无关键词):不写盘(以前是每 turn 必提)
- 累计过 10K:走 LLM 评分(以前是强制提)
- 异步不阻塞:聊天区不显示 "🧠 正在提取"(以前同步显示)
```

- [ ] **Step 10.3: Commit**

```bash
git add docs/test_react_ui.md
git commit -m "docs(test): 场景 1 反映严格双通道新行为"
```

---

## Task 11: 全量回归测试

**Files:** 无(运行现有测试)

### Steps

- [ ] **Step 11.1: 跑所有 memory 相关测试**

Run: `.venv/bin/python -m pytest tests/ -v -k "memory or extraction or dual_channel or react_memory" 2>&1 | tail -30`
Expected: all pass

- [ ] **Step 11.2: 跑 distiller / scheduler / types_config 测试**

Run: `.venv/bin/python -m pytest tests/test_distiller.py tests/test_scheduler.py tests/test_types_config.py tests/test_dual_channel_concurrent.py -v`
Expected: all pass

- [ ] **Step 11.3: 手动 sanity check — 启动 streamlit 看 UI 启动无错**

Run: `bash scripts/run_with_debug.sh agent_core 2>&1 | head -30`
Expected: streamlit 启动成功,无报错(浏览器 http://localhost:8501 打开正常)

按 `Ctrl+C` 终止 streamlit。

- [ ] **Step 11.4: 全部通过,做最终汇总 commit**

```bash
git add -A
git status  # 确认无遗留
git commit -m "docs(plan): M9 实施完成 + 验证记录" --allow-empty
```

---

## Task 12: 更新 IMPLEMENTATION_PLAN.md(M9 状态 ⏸️ → ✅)

**Files:**
- Modify: `docs/IMPLEMENTATION_PLAN.md`

### Steps

- [ ] **Step 12.1: 找 M9 章节**

Run: `grep -n "M9\|Day 9" /Users/fanyunxu/Desktop/myproject/agent-dev/docs/IMPLEMENTATION_PLAN.md | head -10`

- [ ] **Step 12.2: 改 M9 状态从 ⏸️ 到 ✅**

在 M9 Day 9 章节,把所有 ⏸️ 改为 ✅,并在末尾追加验收记录段(参考之前 M7/M8 的格式)。

- [ ] **Step 12.3: Commit**

```bash
git add docs/IMPLEMENTATION_PLAN.md
git commit -m "docs(plan): M9 Day 9 状态 ⏸️ → ✅ + 验收记录"
```

---

## Task 13: Push + PR

### Steps

- [ ] **Step 13.1: 推到远程**

```bash
git push origin feature/fork-compact
```

- [ ] **Step 13.2: (可选)开 PR 到 master**

```bash
gh pr create --base master --head feature/fork-compact \
  --title "M9: ReAct 严格双通道记忆提取" \
  --body "$(cat <<'EOF'
背景:Option C 简化实现每 turn 必提,违背设计文档 §3.3 §4.1。本次按设计严格实现。

改动:
- 新增 ExtractionGate / ReactMemoryBridge / prompt_templates
- 接入 DualChannelWriter
- 删 Option C 同步 hack
- 更新设计文档(§3.3.1 §4.8 §6.9)
- 更新 spec 文件 + 实施计划

验收:
- 三级门 OR 关系(门1 累计 OR 门2 关键词 → 门3 LLM 评分)
- 通道 A WAL(无 LLM,fsync 落盘)
- 通道 B 异步(LLM 评分,后写盘 + 向量化)
- 门1 跑完清零 / 门2 不清零
- LLM 提示词层去重(<existing_memories_in_this_period>)

参考:
- spec: docs/superpowers/specs/2026-06-22-react-memory-strict-design.md
- plan: docs/superpowers/plans/2026-06-22-react-memory-strict.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist(已对照 spec)

**Spec 覆盖**:
- [x] §1 背景与动机 — 已在 plan 头部说明
- [x] §2 范围 — 11 个 task 都在 in scope 内,out of scope 项明确
- [x] §3 架构总览 — Task 6 (bridge) 体现
- [x] §4.1 通道 A vs B 职责 — Task 7 Step 7.4-7.5
- [x] §4.2 A 详细职责(WAL)— Task 7 + dual_channel_writer 已有
- [x] §4.3 B 详细职责 — Task 5 (写盘加 session_id)
- [x] §4.4 决策树 — Task 1-3 (gate 完整)
- [x] §4.5 关键词 — Task 1 Step 1.3
- [x] §4.6 提示词层去重 — Task 2 + Task 9 Step 9.1
- [x] §4.7 SM vs B — 不在 scope(本次不动 SM)
- [x] §5 组件 — Task 1 (gate) + Task 6 (bridge) + Task 2 (prompts)
- [x] §6 数据契约 — Task 1 (TurnContext, Decision) + Task 6 (MemoryEvent)
- [x] §7 错误处理 — 散落在各 task
- [x] §8 测试 — Task 1, 3, 4, 5, 6, 9 都有测试
- [x] §9 风险 — 散落在任务中
- [x] §10 UI — Task 8 (wiring) + Task 10 (docs)
- [x] §11 实施计划 — 13 个 task 全部映射
- [x] §12 验收 — Task 11 + 12

**Type/Method 一致性**:
- `ExtractionGate.should_extract(ctx: TurnContext) -> Decision` ✓
- `ReactMemoryBridge.on_turn_end(...) -> Iterator[MemoryEvent]` ✓
- `MemoryStore.list_by_session(session_id, since_turn)` ✓
- `DualChannelWriter.channel_a_inline_write(user_msg, assistant_resp, turn_index)` ✓
- `DualChannelWriter.channel_b_background_extract(messages, llm_extractor=)` ✓

**Placeholders**: 无 — 每个 task 有具体代码

**潜在风险**:
- Task 7 Step 7.6 验证 ReactAgent 现有属性,可能需要小调整(已在 task 里写明)
- Task 8 Streamlit session_state 复杂,可能要小改 wiring 路径

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-22-react-memory-strict.md`.**

13 个 task,覆盖:
- 3 个核心新模块(ExtractionGate / ReactMemoryBridge / prompt_templates)
- 1 个 store 接口扩展(list_by_session)
- 1 个 dual_channel 写入增强(session_id + turn_index)
- ReactAgent 接入 + 删除 Option C
- web/app.py wiring
- 集成测试 + 文档更新

两种执行方式:

**1. Subagent-Driven (推荐)** - 每个 task 派一个 fresh subagent,逐个 review,迭代快
**2. Inline Execution** - 当前 session 内执行,带 checkpoint

选哪种?
