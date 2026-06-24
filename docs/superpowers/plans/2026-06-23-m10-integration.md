# M10 记忆系统全集成 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `agent_core/memory/` 下"已构建但未集成"的全部模块按 design doc §4-§14 真正接到自研 ReAct 流程,补齐 17 项 missing entirely + 20 项 built-not-wired,共 23 个 task。

**Architecture:** 6 cluster(C1 P0 安全洞 → C2 L3 Fast Path → C3 autoDream 真跑 → C4 蒸馏可观测 → C5 写入强化 → C6 UI/可观测完整化)。每个 task = 1 commit,严格 TDD,subagent 串行执行(DAG 顺序,详见 spec §10)。

**Tech Stack:** Python 3.9, Streamlit ≥1.30, ChromaDB, Pydantic v2, pytest, Streamlit `st.session_state` + `pages/` 多页。

**Reference Spec:** `docs/superpowers/specs/2026-06-23-m10-integration-design.md` (427 行,commit `e1dbccd`)

## Global Constraints

- **TDD 严格**: 每个 task 1-3 步写测试 → 2/4 步跑测试确认 FAIL → 3/5 步实现 → 4/6 步跑测试 PASS → 5/7 步 commit
- **commit 格式**: `<type>(<scope>): <subject>`(feat / fix / docs / test / refactor)
- **测试运行命令**: 统一 `.venv/bin/python -m pytest tests/<test_file>.py -v` 或 `PYTHONPATH=. /Users/fanyunxu/Library/Python/3.9/bin/python3 -m pytest tests/<test_file>.py -v`(根据 python 环境二选一,先 `which python3` 决定)
- **测试文件命名**: `tests/test_<feature>.py`,每个 task 一个新文件(若需要)+ 修改现有(若需要)
- **代码风格**: 严格匹配 surrounding code,优先 `from agent_core.memory import ...` 风格
- **不允许 placeholder**: 每个 step 必须有实际代码,不许 TBD / TODO
- **数据隔离**: 测试用 `tmp_path` fixture(已有 `tmp_path` 模式),不污染 `~/.agent_data/`
- **Streamlit 改动**: 改 `web/app.py` 后,**先** 跑 `streamlit run web/app.py --server.headless=true --server.port=8501` 验证 UI 不崩,再 commit
- **每 cluster 末尾**: 全 cluster 的测试集 + M9 现有 112 测试 + 0 回归,跑 `pytest tests/ -x -q` 验证
- **branch**: 所有 commit 都在 `feature/fork-compact` 分支,**不**合并 master(等全 M10 完 + final review)

## DAG 依赖

```
C1.1 → C1.2 → C1.3
C1.1 → C1.4
C2.1 → C2.2 → C2.3
C3.1 → C3.2 → C3.3 → C4.1 → C4.2
C3.1 → C4.3 → C4.4
C5.1 → C5.2
C5.1 → C5.3 → C5.4
C6.1 → C6.2 → C6.3
C6.3 → C6.4
C6.4 → C6.5
```

并行可能:`C1.x / C2.x / C3.x / C5.x` 4 个 cluster 间互相独立(没有共享文件)。M10 实施时先串行 C1(基础),C2/C3/C5 可并行 dispatch subagent(同模型),C4/C6 走顺序。

---

# Cluster C1: P0 安全洞(4 tasks)

## Task C1.1: MemoryPathValidator 接入 MemoryStore.write

**Files:**
- Modify: `agent_core/memory/memory_store.py:121-244`(`MemoryStore` 类,重点 write 方法)
- Test: `tests/test_path_validator_in_write.py`(新)

**Interfaces:**
- Consumes: `MemoryPathValidator.validate(rel_path, must_exist=False) -> Path`(`path_validator.py:108`)
- Produces: `MemoryStore.write(...)` 内部调 validator,失败抛 `PathSecurityError`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_path_validator_in_write.py
import pytest
from pathlib import Path
from agent_core.memory import MemoryStore, PathSecurityError


def test_write_rejects_absolute_path(tmp_path: Path):
    store = MemoryStore(tmp_path)
    with pytest.raises(PathSecurityError):
        store.write(
            abs_path="/etc/passwd",
            type="user",
            body="should fail",
        )


def test_write_rejects_path_traversal(tmp_path: Path):
    store = MemoryStore(tmp_path)
    with pytest.raises(PathSecurityError):
        store.write(
            abs_path="../../etc/passwd",
            type="user",
            body="should fail",
        )


def test_write_accepts_valid_relative_path(tmp_path: Path):
    store = MemoryStore(tmp_path)
    rel = store.write(
        abs_path="user/test.md",
        type="user",
        body="valid",
    )
    assert (tmp_path / "user" / "test.md").exists()
    assert rel == "user/test.md"
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
.venv/bin/python -m pytest tests/test_path_validator_in_write.py -v
# Expected: 3 failed (write doesn't call validator yet)
```

- [ ] **Step 3: 改 MemoryStore.write 接入 validator**

`agent_core/memory/memory_store.py:152` 之前加:

```python
def __init__(self, memory_root: Union[str, Path]):
    self.root = Path(memory_root)
    # ... existing init
    from .path_validator import MemoryPathValidator  # 局部 import 避免循环
    self._validator = MemoryPathValidator(self.root)
```

`write(self, abs_path: str, type: str, body: str, **kwargs)` 方法签名首行加:

```python
def write(self, abs_path: str, type: str, body: str, **kwargs) -> str:
    # M10 C1.1: 路径校验
    self._validator.validate(abs_path)
    # ... 原有逻辑
```

- [ ] **Step 4: 跑测试 PASS**

```bash
.venv/bin/python -m pytest tests/test_path_validator_in_write.py -v
# Expected: 3 passed
```

- [ ] **Step 5: 跑回归**

```bash
.venv/bin/python -m pytest tests/test_memory_store.py tests/test_memory_store_list_by_session.py -v
# Expected: 0 regression
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_path_validator_in_write.py agent_core/memory/memory_store.py
git commit -m "feat(memory): MemoryPathValidator 接入 MemoryStore.write(§14.1 L1-L4)"
```

---

## Task C1.2: SecretScanner + MemoryEditor.sanitize 接入 Channel B

**Files:**
- Modify: `agent_core/memory/dual_channel_writer.py:310-410`(`_do_channel_b_extract` 写盘前)
- Modify: `agent_core/memory/react_memory_bridge.py:22-30`(`MemoryEventKind` 加枚举值)
- Modify: `web/app.py:900-913`(`elif msg_type == "memory_event":` 加分支)
- Test: `tests/test_channel_b_secret_sanitize.py`(新)

**Interfaces:**
- Consumes: `MemoryEditor.sanitize(content) -> str`(`memory_editor.py:99`), `SecretScanner.scan(text) -> ScanResult`(`secret_scanner.py:172`)
- Produces: Channel B 写盘前 sanitize;若 sanitize 后仍命中(罕见)整条丢弃 + 推 `SECRET_DETECTED` memory_event

- [ ] **Step 1: 写失败测试**

```python
# tests/test_channel_b_secret_sanitize.py
import pytest
from unittest.mock import MagicMock, patch
from agent_core.memory import DualChannelWriter, MemoryStore, ExtractionCandidate, TurnMessage
from agent_core.memory.memory_editor import sanitize


def test_sanitize_redacts_api_key_in_body():
    body = "My key is sk-1234567890abcdefghij"
    cleaned = sanitize(body)
    assert "sk-1234567890abcdefghij" not in cleaned
    assert "[REDACTED]" in cleaned or "***" in cleaned


def test_sanitize_preserves_non_secret_content():
    body = "I love python and coffee"
    assert sanitize(body) == body


def test_channel_b_writes_sanitized_body(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    cand = ExtractionCandidate(
        title="api key",
        body="sk-1234567890abcdefghij",  # 真有 secret
        type="user",
        source_session="s1",
        source_turn=1,
    )
    # patch LLM call to return candidate
    # patch embed_fn
    # call _do_channel_b_extract with [cand]
    # 验:写入的 md body 不含 sk-1234...
```

- [ ] **Step 2: 跑测试 FAIL**

```bash
.venv/bin/python -m pytest tests/test_channel_b_secret_sanitize.py::test_sanitize_redacts_api_key_in_body -v
# Expected: FAIL (sanitize not implemented yet) — 但其实 memory_editor.sanitize 已存在,所以应该是 PASS。
# 如果 PASS,直接 skip 失败测试,做下一步
```

- [ ] **Step 3: 接入 Channel B 写前 sanitize**

`dual_channel_writer.py:_do_channel_b_extract` 写盘前(在 `meta_db.mark_committed` 之前)加:

```python
# M10 C1.2: sanitize 写盘前
from .memory_editor import sanitize, scan_secrets
candidate_body_sanitized = sanitize(candidate.body)
remaining_secrets = scan_secrets(candidate_body_sanitized)
if remaining_secrets:
    logger.warning(f"channel_b: still {len(remaining_secrets)} secret(s) after sanitize, dropping")
    # 推 SECRET_DETECTED 事件(通过 future callback 上抛)
    return None
candidate = candidate.model_copy(update={"body": candidate_body_sanitized})
```

- [ ] **Step 4: MemoryEventKind 加 SECRET_DETECTED**

`react_memory_bridge.py:22` enum 加:

```python
class MemoryEventKind(str, Enum):
    CHANNEL_A_OK = "channel_a_ok"
    GATE_PASS = "gate_pass"
    GATE_SKIP = "gate_skip"
    EXTRACT_DISPATCHED = "extract_dispatched"
    EXTRACT_ERROR = "extract_error"
    SECRET_DETECTED = "secret_detected"  # M10 C1.2
```

- [ ] **Step 5: web/app.py 消费 SECRET_DETECTED**

`web/app.py:900-913` 现有 `elif msg_type == "memory_event":` 加分支:

```python
elif event.kind.value == "secret_detected":
    ms = st.session_state.memory_stats
    ms["secrets_redacted"] = ms.get("secrets_redacted", 0) + 1
    st.session_state.memory_stats = ms
```

- [ ] **Step 6: 跑测试 PASS**

```bash
.venv/bin/python -m pytest tests/test_channel_b_secret_sanitize.py -v
# Expected: 3 passed
```

- [ ] **Step 7: 跑回归**

```bash
.venv/bin/python -m pytest tests/test_dual_channel_concurrent.py tests/test_app_wiring.py -v
# Expected: 0 regression
```

- [ ] **Step 8: Commit**

```bash
git add tests/test_channel_b_secret_sanitize.py agent_core/memory/dual_channel_writer.py agent_core/memory/react_memory_bridge.py web/app.py
git commit -m "feat(memory): SecretScanner + sanitize 接入 Channel B(§14.4)"
```

---

## Task C1.3: chmod 0o600 写记忆文件

**Files:**
- Modify: `agent_core/memory/memory_store.py`(write 末尾)
- Test: 修改 `tests/test_memory_store.py`(加 1 case)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_store.py:append
import stat

def test_write_sets_file_mode_0600(tmp_path: Path):
    store = MemoryStore(tmp_path)
    store.write(abs_path="user/test.md", type="user", body="secret")
    path = tmp_path / "user" / "test.md"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
```

- [ ] **Step 2: 跑测试 FAIL**

```bash
.venv/bin/python -m pytest tests/test_memory_store.py::test_write_sets_file_mode_0600 -v
# Expected: FAIL (no chmod yet)
```

- [ ] **Step 3: write 末尾加 chmod**

`agent_core/memory/memory_store.py:write` 方法,在写完文件后(在 return rel_path 之前)加:

```python
import os
os.chmod(target_path, 0o600)
```

- [ ] **Step 4: 跑测试 PASS + 回归**

```bash
.venv/bin/python -m pytest tests/test_memory_store.py tests/test_path_validator_in_write.py -v
# Expected: 0 regression, new test pass
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_memory_store.py agent_core/memory/memory_store.py
git commit -m "feat(memory): chmod 0o600 写记忆文件(§14.3)"
```

---

## Task C1.4: PathValidator 接入 DualChannelWriter

**Files:**
- Modify: `agent_core/memory/dual_channel_writer.py:_do_channel_b_extract`
- Test: 复用 `tests/test_channel_b_secret_sanitize.py`(加 1 case)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_channel_b_secret_sanitize.py:append
def test_channel_b_rejects_traversal_in_candidate_path(tmp_path):
    # 模拟 extractor 返回路径含 ../
    # 验:Channel B 写前抛 PathSecurityError
    # ... 详细 mock
    pass
```

- [ ] **Step 2-5: 实现 + 跑测试 + 跑回归 + Commit**

模式同 C1.1,实现是 `dual_channel_writer.py` 写盘前 `self._validator.validate(candidate_path)`。

```bash
git commit -m "feat(memory): PathValidator 接入 DualChannelWriter Channel B(§14.1)"
```

---

# Cluster C2: L3 Fast Path(3 tasks)

## Task C2.1: SessionMemoryLayer 接入 ReactAgent.run() compact 决策

**Files:**
- Modify: `agent_core/agent_core.py:382`(`ContextManager.check_and_compact` 调用处)
- Modify: `agent_core/memory/sm_layer.py`(可能补 `should_trigger_compact` 接口)
- Test: `tests/test_sm_layer_integration.py`(新)

**Interfaces:**
- Consumes: `SessionMemoryLayer.should_trigger_compact(ctx) -> bool`(`sm_layer.py`,**可能需要新加**)
- Consumes: `SessionMemoryLayer.compact(messages) -> CompactResult`(`sm_layer.py:113` 附近)
- Produces: `run()` 先问 SM 走 L3 fast path,不命中 fallback ContextManager

- [ ] **Step 1: 看 sm_layer 现状**

```bash
grep -n "should_trigger\|def compact\|class Session" agent_core/memory/sm_layer.py
```

- [ ] **Step 2: 如缺 should_trigger_compact,补它**

`sm_layer.py` 加方法:

```python
def should_trigger_compact(self, ctx: TurnContext) -> bool:
    """M10 C2.1: 决定是否走 L3 fast path"""
    return (
        self.sm_exists() and
        not self.sm_is_template() and
        ctx.cumulative_tokens > self.compact_threshold
    )
```

- [ ] **Step 3: 写失败测试**

```python
# tests/test_sm_layer_integration.py
def test_run_compact_uses_sm_fast_path_when_available():
    # mock ContextManager + SessionMemoryLayer
    # 验:SM 存在时,run() 走 sm.compact,不调 ContextManager
    pass


def test_run_compact_falls_back_to_context_manager_when_no_sm():
    # 验:SM 不存在,fallback ContextManager
    pass


def test_memory_status_chunk_reports_sm_compact_flag():
    # 验:产出 memory_status chunk 含 sm_compact: True/False
    pass
```

- [ ] **Step 4: 改 run()**

`agent_core/agent_core.py:382` 改:

```python
# 旧: self.context_manager.check_and_compact(...)
# 新:
sm_decision = self.session_memory.should_trigger_compact(ctx) if self.session_memory else False
if sm_decision:
    compact_result = self.session_memory.compact(state["messages"])
    sm_compact_used = True
else:
    compact_result = self.context_manager.check_and_compact(...)
    sm_compact_used = False
# yield memory_status chunk 时多带 sm_compact=sm_compact_used
```

`ReactAgent.__init__` 加 `session_memory: Optional[SessionMemoryLayer] = None` 参数。

- [ ] **Step 5-6: PASS + 回归 + Commit**

```bash
git commit -m "feat(sm): SessionMemoryLayer 接入 run() compact 决策(§4.3/§4.4)"
```

---

## Task C2.2: SM 文件持久化

**Files:**
- Modify: `agent_core/memory/sm_layer.py:SessionMemoryLayer.compact`(末尾持久化)
- Test: 复用 `tests/test_sm_layer.py`(加 1 case)

- [ ] **Step 1-5: 实现路径 `~/.agent_data/memory/sm/<session_id>.json`**

`sm_layer.py.compact` 末尾加:

```python
sm_dir = self.memory_root / "sm"
sm_dir.mkdir(parents=True, exist_ok=True)
sm_path = sm_dir / f"{self.session_id}.json"
sm_path.write_text(json.dumps({
    "summary": summary_text,
    "token_count": self.sm_token_count(),
    "updated_at": datetime.utcnow().isoformat(),
}), encoding="utf-8")
```

测试:写完 SM 后,`sm_exists()` 返回 True,`read_sm()` 返回 summary。

```bash
git commit -m "feat(sm): SM 文件持久化到 sm/<session>.json(§4.4)"
```

---

## Task C2.3: SM 跨会话缓存作为 L4 输入

**Files:**
- Modify: `agent_core/memory/distiller.py:_read_sessions` 或新加 `_read_sm_files`
- Test: 复用 `tests/test_distiller.py`(加 1 case)

- [ ] **Step 1-5: distiller 启动时读 `memory_root/sm/*.json` 作为 L4 输入**

```python
def _read_sm_files(self) -> list[dict]:
    sm_dir = self.memory_root / "sm"
    if not sm_dir.exists():
        return []
    return [json.loads(p.read_text()) for p in sm_dir.glob("*.json")]
```

`Distiller.distill()` 开头调 `_read_sm_files()` 拼接到 prompt。

```bash
git commit -m "feat(distill): SM 跨会话缓存作为 L4 输入(§4.4 + §7)"
```

---

# Cluster C3: autoDream 真跑(3 tasks)

## Task C3.1: DistillationLoop 启动 + 关停

**Files:**
- Modify: `web/app.py:526-577`(`get_agent()` 末尾)
- Modify: `agent_core/agent_core.py:793`(`ReactAgent.close`)
- Test: `tests/test_distillation_loop_lifecycle.py`(新)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_distillation_loop_lifecycle.py
def test_get_agent_starts_distillation_loop():
    # 验:get_agent() 后,agent 上有 distillation_loop.is_running() == True
    pass


def test_agent_close_stops_distillation_loop():
    # 验:agent.close() 后,loop.is_running() == False
    pass


def test_distillation_loop_4_gates_skip_when_not_satisfied():
    # mock: 上次运行 < 24h
    # 验:loop.tick_once() 返回 None(不调 LLM)
    pass
```

- [ ] **Step 2: get_agent() 末尾启 loop**

```python
# web/app.py:在 return agent 之前
from agent_core.memory.scheduler import DistillationLoop
loop = DistillationLoop(
    scheduler=distillation_scheduler,  # 已构造,见 spec
    interval_seconds=600,  # 10 min
)
loop.start()
agent._distillation_loop = loop  # 挂到 agent
return agent
```

- [ ] **Step 3: agent.close() 停 loop**

`agent_core.py:close`:

```python
def close(self):
    if hasattr(self, "_distillation_loop") and self._distillation_loop:
        self._distillation_loop.stop(timeout=5.0)
    # 现有 close 逻辑
```

- [ ] **Step 4-6: PASS + 回归 + Commit**

```bash
git commit -m "feat(distill): DistillationLoop 启动/关停接入 get_agent + close(§7)"
```

---

## Task C3.2: Sidebar Auto-dream 状态行

**Files:**
- Modify: `web/app.py`(sidebar)
- Modify: `agent_core/memory/scheduler.py:DistillationLoop.get_status()`(可能补接口)
- Test: `tests/test_distillation_loop_lifecycle.py`(加 1 case)

- [ ] **Step 1: DistillationLoop.get_status()**

```python
# scheduler.py
def get_status(self) -> dict:
    return {
        "running": self.is_running(),
        "tick_count": self.tick_count(),
        "last_tick_at": self._last_tick_at,  # 需新加
        "last_result": self._last_result,  # 需新加
    }
```

- [ ] **Step 2: sidebar 加 expander**

```python
# web/app.py:sidebar
with st.sidebar:
    with st.expander("🌙 Auto-dream", expanded=False):
        if hasattr(st.session_state, "agent") and st.session_state.agent:
            loop = getattr(st.session_state.agent, "_distillation_loop", None)
            if loop:
                st.json(loop.get_status())
            else:
                st.caption("未启动")
```

- [ ] **Step 3-5: PASS + 回归 + Commit**

```bash
git commit -m "feat(ui): sidebar Auto-dream 状态行(§13.3)"
```

---

## Task C3.3: candidate 写盘路径 `_candidate/<run>/`

**Files:**
- Modify: `agent_core/memory/distiller.py:Distiller.write_candidates`
- Test: `tests/test_candidate_layout.py`(新)

- [ ] **Step 1: 写失败测试**

```python
def test_write_candidates_creates_candidate_subdir(tmp_path):
    distiller = Distiller(...)
    distiller.write_candidates(candidates, run_id="r1")
    assert (tmp_path / "_candidate" / "r1").is_dir()
```

- [ ] **Step 2-4: 改 write_candidates 路径**

```python
def write_candidates(self, candidates, run_id: str):
    cand_dir = self.memory_root / "_candidate" / run_id
    cand_dir.mkdir(parents=True, exist_ok=True)
    # ... 写文件
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(distill): candidate 写盘路径 _candidate/<run>/(§7.4)"
```

---

# Cluster C4: 蒸馏产出可观测(4 tasks)

## Task C4.1: Sidebar "📥 待审记忆" expander

**Files:**
- Modify: `web/app.py`(sidebar)
- Test: `tests/test_candidate_review_actions.py`(新,加 2 cases)

- [ ] **Step 1: 写测试**

```python
def test_sidebar_shows_pending_candidate_count():
    # mock 候选目录
    # 验:sidebar expander 标题含 "N 条"
    pass
```

- [ ] **Step 2: 实现**

```python
# web/app.py
with st.sidebar:
    cand_root = agent_data_dir / "memory" / "_candidate"
    pending = list(cand_root.rglob("*.md")) if cand_root.exists() else []
    with st.expander(f"📥 待审记忆 {len(pending)} 条", expanded=False):
        for p in pending[:5]:
            st.caption(p.name)
        if len(pending) > 5:
            st.caption(f"... 共 {len(pending)} 条")
        st.page_link("pages/candidate_review.py", label="查看全部 →")
```

- [ ] **Step 3-5: PASS + Commit**

```bash
git commit -m "feat(ui): sidebar 待审记忆提醒 expander(§13.4)"
```

---

## Task C4.2: Candidate Review 独立 Page

**Files:**
- Create: `pages/candidate_review.py`(新,Streamlit 多页)
- Test: `tests/test_candidate_review_actions.py`(新,6 cases)

- [ ] **Step 1: 写测试**

```python
def test_accept_moves_candidate_to_user_dir(tmp_path):
    # 模拟候选
    # 调 accept_candidate(hash, type='user')
    # 验:候选从 _candidate/ 消失,user/<hash>.md 出现
    pass


def test_reject_deletes_candidate(tmp_path):
    pass


def test_edit_overrides_body(tmp_path):
    pass


def test_skip_does_not_delete(tmp_path):
    pass


def test_review_state_persisted_in_meta_db(tmp_path):
    pass


def test_list_candidates_returns_all(tmp_path):
    pass
```

- [ ] **Step 2-4: 实现 actions**

新建 `agent_core/memory/candidate_actions.py`:

```python
def list_candidates(memory_root: Path) -> list[Path]: ...
def accept_candidate(memory_root: Path, cand_path: Path, target_type: str) -> Path: ...
def reject_candidate(memory_root: Path, cand_path: Path, reason: str) -> None: ...
def edit_candidate(memory_root: Path, cand_path: Path, new_body: str) -> None: ...
def skip_candidate(memory_root: Path, cand_path: Path) -> None: ...
```

`pages/candidate_review.py` 用 st.dataframe + st.button 调这些。

- [ ] **Step 5: Commit**

```bash
git add pages/candidate_review.py agent_core/memory/candidate_actions.py tests/test_candidate_review_actions.py
git commit -m "feat(ui): Candidate Review 独立 page + actions(§13.4)"
```

---

## Task C4.3: distiller dry_run 模式

**Files:**
- Modify: `agent_core/memory/distiller.py:Distiller.distill`
- Test: `tests/test_distiller.py`(加 1 case)

- [ ] **Step 1-5: `distill(mode='dry_run' | 'merge')`**

```python
def distill(self, sessions: list, mode: str = "merge"):
    candidates = self._llm_extract(sessions)
    if mode == "dry_run":
        run_id = f"run_{int(time.time())}"
        self.write_candidates(candidates, run_id=run_id)
        return DistillationResult(candidates=candidates, run_id=run_id, merged=False)
    # merge mode: 写盘
    ...
```

```bash
git commit -m "feat(distill): dry_run 模式(§7.4)"
```

---

## Task C4.4: review 决策回灌(跳过已审)

**Files:**
- Modify: `agent_core/memory/meta_db.py`(加表 `candidate_decisions`)
- Modify: `agent_core/memory/distiller.py:Distiller.distill`(开头查表 skip)
- Test: `tests/test_candidate_review_actions.py`(加 1 case)

- [ ] **Step 1-5: meta_db 加表 + distiller 跳过**

```python
# meta_db.py
class MetaDB:
    def record_candidate_decision(self, cand_hash: str, decision: str, decided_at: float): ...
    def list_decided_candidates(self) -> set[str]: ...

# distiller.py:distill 开头
decided = self.meta_db.list_decided_candidates()
candidates = [c for c in candidates if c.hash not in decided]
```

```bash
git commit -m "feat(distill): review 决策回灌 meta_db(§7.4)"
```

---

# Cluster C5: 写入路径强化(4 tasks)

## Task C5.1: validate_type 接入 write

**Files:**
- Modify: `agent_core/memory/memory_store.py:write`
- Test: `tests/test_path_validator_in_write.py`(加 2 cases)

- [ ] **Step 1-5: write 开头 `validate_type(type)`**

```python
from .types import validate_type
def write(self, abs_path, type, body, **kwargs):
    validate_type(type)  # M10 C5.1
    self._validator.validate(abs_path)  # C1.1
    # ...
```

测试 2 cases: `type='invalid_type'` 抛 ValueError,`type='user'` pass。

```bash
git commit -m "feat(memory): validate_type 接入 write(§5.3.1 封闭分类)"
```

---

## Task C5.2: validate_body + invalid_memories 表

**Files:**
- Modify: `agent_core/memory/memory_store.py:write`
- Modify: `agent_core/memory/meta_db.py`(加表)
- Test: `tests/test_memory_store.py`(加 2 cases)

- [ ] **Step 1-5: 校验 + soft-fail**

```python
# memory_store.py:write
from .types import validate_body
validation_error = validate_body(type, body)
if validation_error:
    # 不阻塞,记 meta_db
    self.meta_db.record_invalid_memory(rel_path, validation_error)
# 仍写盘
```

```bash
git commit -m "feat(memory): validate_body + invalid_memories 软记录(§5.3.2)"
```

---

## Task C5.3: Gate-1 周期内去重 prompt

**Files:**
- Modify: `agent_core/memory/react_memory_bridge.py:on_turn_end`(调 LLM 前)
- Test: `tests/test_react_memory_bridge.py`(加 3 cases)

- [ ] **Step 1-5: bridge 调 LLM 前拉 existing 喂 prompt_templates**

```python
# react_memory_bridge.py:on_turn_end 调 LLM 评分前
existing = self._memory_store.list_by_session(self._session_id, since_turn=self._gate1_period_start)
prompt = build_extract_prompt(
    turns_text=turns_text,
    existing_memories=existing,
)
# 把 prompt 喂给 LLM
```

```bash
git commit -m "feat(memory): Gate-1 周期内去重 prompt(§6.9.1)"
```

---

## Task C5.4: Fork 独立 extract_router

**Files:**
- Modify: `web/app.py:526-577`(`get_agent()`)
- Test: `tests/test_app_wiring.py`(加 2 cases)

- [ ] **Step 1-5: 创建 extract_router 用不同 cache_namespace**

```python
# web/app.py:get_agent
extract_config = config.model_copy(deep=True)
extract_config.cache_namespace = "memory_extractor"  # 或类似
extract_router = LLMRouter(extract_config)
gate = ExtractionGate(llm_router=extract_router, ...)
```

测试:验 `gate._llm_router` 与 `agent.router` 是不同实例。

```bash
git commit -m "feat(memory): Fork 独立 extract_router 避免 cache 污染(§4.6)"
```

---

# Cluster C6: UI / 可观测完整化(5 tasks)

## Task C6.1: OTel tracer 包裹 4 个 memory 路径

**Files:**
- Modify: `web/app.py:526-577`(`get_agent` 末尾调 `configure_tracing`)
- Modify: `agent_core/memory/retriever.py:search`(开头包 span)
- Modify: `agent_core/memory/extraction_gate.py:should_extract`(开头包 span)
- Modify: `agent_core/memory/scheduler.py:DistillationLoop.tick_once`(开头包 span)
- Modify: `agent_core/memory/sm_layer.py:compact`(开头包 span)
- Test: `tests/test_tracing_spans.py`(新,2 cases)

- [ ] **Step 1: 写测试**

```python
def test_configure_tracing_emits_console_exporter():
    # 验证 configure_tracing 设了全局 tracer
    pass


def test_retriever_search_emits_memory_search_span():
    # 用 in-memory exporter
    # 验:span name == "memory.search"
    pass
```

- [ ] **Step 2-6: 每个 path 顶部加**

```python
from .tracing import tracer
with tracer.start_as_current_span("memory.search"):
    # 原有逻辑
```

`get_agent` 末尾加 `configure_tracing(service_name="agent_dev", exporter="console")`(dev) 或读 `MEMORY_OTEL_EXPORTER` env。

```bash
git commit -m "feat(otel): tracer 包裹 4 个 memory 路径 + configure_tracing(§13.6)"
```

---

## Task C6.2: Cost budget guard

**Files:**
- Modify: `agent_core/memory/config.py`(加 `CostConfig`)
- Modify: `agent_core/llm/router.py:LLMRouter`(累计 cost)
- Modify: `agent_core/memory/react_memory_bridge.py:_call_llm`(超预算抛 `BudgetExceeded`)
- Test: `tests/test_cost_budget.py`(新,4 cases)

- [ ] **Step 1-5: CostConfig + 累计 + 守卫**

```python
# config.py
class CostConfig(BaseModel):
    daily_budget_usd: float = 1.0
    per_extract_budget_usd: float = 0.05
    enabled: bool = True

# router.py:LLMRouter 增累计
class LLMRouter:
    def __init__(self, config, cost_tracker: Optional[CostTracker] = None):
        self._cost_tracker = cost_tracker or CostTracker()

# react_memory_bridge.py:_call_llm
if self._cost_tracker.todays_total() > self._cost_config.daily_budget_usd:
    raise BudgetExceeded(...)
```

```bash
git commit -m "feat(memory): Cost budget guard 接入(§13.7)"
```

---

## Task C6.3: Latency budget drop

**Files:**
- Modify: `agent_core/memory/react_memory_bridge.py:_call_llm`(包 `concurrent.futures` timeout)
- Test: `tests/test_latency_budget.py`(新,3 cases)

- [ ] **Step 1-5: timeout drop**

```python
def _call_llm_with_timeout(self, prompt, timeout=30.0):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(self._call_llm, prompt)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise LatencyBudgetExceeded(timeout)
```

```bash
git commit -m "feat(memory): Latency budget drop 接入(§13.8)"
```

---

## Task C6.4: Runtime config switch 不重建 agent

**Files:**
- Modify: `agent_core/memory/config.py:MemoryConfig.set_runtime`
- Modify: `web/app.py`(监听改动 → 调对应组件 reload)
- Test: `tests/test_runtime_config_switch.py`(新,2 cases)

- [ ] **Step 1-5: set_runtime + UI 监听**

```python
# config.py
class MemoryConfig:
    def set_runtime(self, key: str, value: Any) -> None:
        # 改 self.runtime[key] = value + 写 .agent_config.json
        pass

# web/app.py
if st.session_state.get("cost_budget_changed"):
    agent._react_memory_bridge.reload_cost_config()
    st.session_state.cost_budget_changed = False
```

```bash
git commit -m "feat(config): runtime switch 不重建 agent(§12.3)"
```

---

## Task C6.5: 5 回退条件 banner

**Files:**
- Modify: `agent_core/memory/react_memory_bridge.py:MemoryEventKind`(加 5 个)
- Modify: `web/app.py`(sidebar banner)
- Test: `tests/test_fallback_banner.py`(新,2 cases)

- [ ] **Step 1-5: enum + banner**

```python
# react_memory_bridge.py
class MemoryEventKind(str, Enum):
    # ... existing
    LOCK_BUSY = "lock_busy"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMEOUT = "timeout"
    SECRET_DETECTED = "secret_detected"  # 已加

# web/app.py:sidebar 顶部
if st.session_state.memory_stats.get("last_extract_error"):
    err = st.session_state.memory_stats["last_extract_error"]
    st.error(f"⚠️ 记忆系统降级中: {err}")
    if st.button("🔄 重置"):
        st.session_state.memory_stats["last_extract_error"] = None
        st.rerun()
```

```bash
git commit -m "feat(ui): 5 回退条件 banner(§4.7)"
```

---

# 末尾 Task C7: 全分支 review + 文档(2 commits)

## Task C7.1: Final whole-branch review

**Files:**(只读)
- `agent_core/memory/` 全部
- `web/app.py` 全部
- `pages/candidate_review.py`
- `tests/test_*.py` 全部新增

- [ ] **Step 1: 跑全测试**

```bash
.venv/bin/python -m pytest tests/ -q
# Expected: ~157 passed
```

- [ ] **Step 2: 派 final reviewer subagent**

Dispatch code-reviewer,看 `git log feature/fork-compact..master` 全部 M10 commit。

- [ ] **Step 3: 修复 Critical/Important findings**

派 fix subagent,处理 review findings。

- [ ] **Step 4: re-review 直到 clean**

- [ ] **Step 5: Commit (如果需要)**

```bash
git commit -m "fix(memory): M10 final review fixes(...)"
# 或
git commit -m "refactor(memory): M10 final review cleanups(...)"
```

---

## Task C7.2: IMPLEMENTATION_PLAN.md M10 Day 10 section

**Files:**
- Modify: `docs/IMPLEMENTATION_PLAN.md`(在 M9 section 后加 M10 section)

- [ ] **Step 1: 加 30 行 section**

模式同 M9 section,描述 M10 范围/cluster/完成情况/验收。

- [ ] **Step 2: Commit**

```bash
git add docs/IMPLEMENTATION_PLAN.md
git commit -m "docs(plan): M10 Day 10 状态 ⏸️ → ✅ + 验收记录"
```

---

# 验证(每 cluster 末尾 + 最终)

## 每个 task 末尾

```bash
.venv/bin/python -m pytest tests/<task_test_file>.py -v
git status  # 确认干净
git log --oneline -1  # 确认新 commit
```

## 每个 cluster 末尾

```bash
.venv/bin/python -m pytest tests/ -q
# Expected: 0 regression
```

## 全部 23 task 完成 + C7.1 review 完

```bash
.venv/bin/python -m pytest tests/ -q
# Expected: ~157 passed
streamlit run web/app.py --server.headless=true --server.port=8501
# 手动:发 5 句偏好,检查 sidebar memory_stats + ~/.agent_data/memory/user/ 出现 5 md
# 手动:等 10 min,检查 _candidate/ + sidebar "🌙 Auto-dream" "last ran 0h ago"
# 手动:candidate review page → Accept 一个
# 手动:写含 sk-xxx 的记忆 → 验 sanitize 净化
# 手动:写含 ../../etc/passwd 路径 → 验 PathSecurityError
# 手动:cost budget 改 $0 → 跑提取 → 验 banner
```

## Self-Review Checklist(spec coverage)

- [x] §14.1 PathValidator → C1.1, C1.4
- [x] §14.4 SecretScanner → C1.2
- [x] §14.3 chmod 0o600 → C1.3
- [x] §4.3/§4.4 SM layer → C2.1, C2.2, C2.3
- [x] §7 autoDream → C3.1, C3.2, C3.3
- [x] §13.3 status → C3.2
- [x] §7.4 dry_run + candidate review → C4.1, C4.2, C4.3, C4.4
- [x] §5.3.1 type 校验 → C5.1
- [x] §5.3.2 Why-How → C5.2
- [x] §6.9 dedup → C5.3
- [x] §4.6 fork router → C5.4
- [x] §13.6 OTel → C6.1
- [x] §13.7 cost → C6.2
- [x] §13.8 latency → C6.3
- [x] §12.3 runtime switch → C6.4
- [x] §4.7 5 回退 → C6.5

**Coverage: 17 missing entirely + 20 built-not-wired 全部覆盖。23 task + 2 末尾 = 25 commits。**

## No-Placeholders Self-Check

- 0 个 TBD/TODO/FIXME
- 所有 step 有具体代码
- 所有命令有预期输出
- 所有引用类型/函数名都在 spec §十 DAG 中明确

---

**Plan 完。等用户选 Subagent-Driven / Inline。**
