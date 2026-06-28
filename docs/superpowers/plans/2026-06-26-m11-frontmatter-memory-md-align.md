# M11 实施计划 — frontmatter + MEMORY.md + sideQuery 对齐 Claude Code

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 agent-dev 记忆系统从 M10(12 字段 frontmatter + keyword/semantic/hybrid 三模式)改造为 M11(对齐 Claude Code:`name`/`description` 必填 + MEMORY.md 物理索引 + semantic/sideQuery 二选一)。

**Architecture:**
- **frontmatter**:加 2 必填字段(`name` / `description`),保留 4 必填系统字段;`schema_version` 2→3
- **MEMORY.md**:`<memory_root>/MEMORY.md` 物理索引,200 行 / 25KB 硬上限,写盘 1s 后异步 rebuild
- **L1 启动加载**:agent_core 启动时把 MEMORY.md 注入 system prompt 头部(独立 H1 段)
- **L2 召回**:`semantic`(向量) **或** `sideQuery`(LLM 选 ≤5);不融合
- **sideQuery 模型**:主流程默认 LLM(`llm_router`),不复用 extractor_router

**Tech Stack:** Python 3.11+ / Pydantic v2 (ConfigDict extra="forbid") / ChromaDB(已 done T1-T8) / threading.Timer 异步 / SHA-256 item_hash

## Global Constraints

1. **不动 `web/app_langgraph.py`**(MEMORY.md 约束)
2. **不动 `chroma_store.py` / `embeddings.py` / `extraction_gate.py` / `distiller.py` / `cost_tracker.py`**(M11 不涉及)
3. **TDD red-green-refactor**,每 task 1+ 个 commit,失败测试先写
4. **`item_hash` 保留**(M10 决策,SHA-256,`name`/`description` 不参与 hash 计算)
5. **`description` 长度约束**:`1 <= len <= 200`,太长截断并 warning,太短(<5)写盘但 warning
6. **`compute_item_hash` 不变**(避免 schema 升级影响幂等)
7. **测试只跑受影响文件,不全量**(用户偏好)
8. **Pydantic ConfigDict `extra="forbid"`**(fail-fast 旧配置)
9. **sideQuery 用主 LLM router**(`self.llm_router`,`cache_namespace="memory_side_query"` 隔离)
10. **MEMORY.md 不阻塞写盘 hot path**(异步 + 1s 合并窗口)

---

## 文件结构总览

| 类型 | 文件 | Task |
|------|------|------|
| Modify | `agent_core/memory/types.py` | T1 |
| Modify | `agent_core/memory/memory_store.py` | T2 |
| Modify | `agent_core/memory/migration.py` | T3 |
| **Create** | `agent_core/memory/memory_index.py` | T4, T9 |
| Modify | `agent_core/memory/dual_channel_writer.py` | T5 |
| Modify | `agent_core/memory/retriever.py` | T6, T7 |
| Modify | `agent_core/memory/config.py` | T8 |
| Modify | `agent_core/memory/prompt_templates.py` | T10 |
| Modify | `agent_core/agent_core.py` | T11, T13 |
| Modify | `web/app.py` | T12 |
| **Create** | `tests/test_memory_index.py` | T4, T9 |
| **Create** | `tests/test_e2e_memory_recall.py` | T14 |
| Modify | `tests/test_types.py` | T1 |
| Modify | `tests/test_memory_store.py` | T2 |
| Modify | `tests/test_migration.py` | T3 |
| Modify | `tests/test_dual_channel_concurrent.py` | T5 |
| Modify | `tests/test_retriever.py` | T6, T7 |
| Modify | `tests/test_config.py` | T8 |
| Modify | `tests/test_prompt_templates.py` | T10 |
| Modify | `tests/test_agent_core.py` | T11 |
| Modify | `tests/conftest.py` | T11(fixtures) |

---

## Task 1: frontmatter schema 升 v3(`name` / `description` 必填)

**Files:**
- Modify: `agent_core/memory/types.py`(`_REQUIRED_FRONTMATTER`, `CURRENT_SCHEMA_VERSION`, `validate_frontmatter`)
- Test: `tests/test_types.py`

**Interfaces:**
- Consumes: 现有 `validate_frontmatter(data: dict) -> None`
- Produces: `_REQUIRED_FRONTMATTER = {"type", "created_at", "item_hash", "schema_version", "name", "description"}`;`CURRENT_SCHEMA_VERSION: int = 3`;`description` 长度 1-200 截断逻辑

- [ ] **Step 1: 写失败测试** — 在 `tests/test_types.py` 新增:

```python
def test_validate_frontmatter_v3_requires_name_and_description():
    """M11 schema v3 必填 name + description"""
    from agent_core.memory.types import validate_frontmatter, FrontmatterError
    fm = {
        "type": "user",
        "created_at": "2026-06-26T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 3,
        # 缺 name, description
    }
    with pytest.raises(FrontmatterError, match="name"):
        validate_frontmatter(fm)


def test_validate_frontmatter_v3_accepts_full():
    """M11 v3 6 字段必填齐全 → 通过"""
    from agent_core.memory.types import validate_frontmatter
    fm = {
        "type": "user",
        "created_at": "2026-06-26T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 3,
        "name": "用户叫小明",
        "description": "Python 后端工程师",
    }
    validate_frontmatter(fm)  # 不抛


def test_description_too_long_truncated_with_warning():
    """description > 200 字符 → 截断 + warning(caplog)"""
    import logging
    from agent_core.memory.types import validate_frontmatter
    fm = {
        "type": "user",
        "created_at": "2026-06-26T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 3,
        "name": "x",
        "description": "a" * 500,
    }
    with caplog.at_level(logging.WARNING, logger="agent_core.memory.types"):
        validate_frontmatter(fm)
    assert len(fm["description"]) == 200
    assert "description 截断" in caplog.text or "truncated" in caplog.text.lower()
```

(fixture `caplog` 由 pytest 提供;若不在 `test_types.py` imports 里,加 `import pytest`)

- [ ] **Step 2: 跑 → 期望 FAIL**

```bash
.venv/bin/python -m pytest tests/test_types.py::test_validate_frontmatter_v3_requires_name_and_description -v
```

期望:`FrontmatterError: 缺少必填字段: name`(当前 `_REQUIRED_FRONTMATTER` 不含 `name`)

- [ ] **Step 3: 改 `types.py`**

```python
# agent_core/memory/types.py

CURRENT_SCHEMA_VERSION: int = 3  # M10=2 → M11=3

_REQUIRED_FRONTMATTER = frozenset({
    "type", "created_at", "item_hash", "schema_version",
    "name", "description",  # M11 新增
})

DESCRIPTION_MAX_LEN = 200
DESCRIPTION_MIN_LEN = 1  # < 5 也允许(只 warning)


def validate_frontmatter(data: dict) -> None:
    """校验 frontmatter 完整性。M11 v3 schema。"""
    if not isinstance(data, dict):
        raise FrontmatterError(f"frontmatter 必须是 dict,实际为 {type(data).__name__}")

    missing = _REQUIRED_FRONTMATTER - set(data.keys())
    if missing:
        raise FrontmatterError(f"缺少必填字段: {', '.join(sorted(missing))}")

    # description 长度约束
    desc = data.get("description", "")
    if not isinstance(desc, str):
        raise FrontmatterError(f"description 必须是 str,实际为 {type(desc).__name__}")
    if len(desc) < DESCRIPTION_MIN_LEN:
        import logging
        logging.getLogger(__name__).warning(
            f"description 过短(<{DESCRIPTION_MIN_LEN} 字符): {desc!r}"
        )
    elif len(desc) > DESCRIPTION_MAX_LEN:
        import logging
        logging.getLogger(__name__).warning(
            f"description 过长(>{DESCRIPTION_MAX_LEN} 字符),截断到 {DESCRIPTION_MAX_LEN}"
        )
        data["description"] = desc[:DESCRIPTION_MAX_LEN]

    # name 至少非空
    if not data.get("name", "").strip():
        raise FrontmatterError("name 必填且非空")

    # schema_version 必须 ≥ 3
    sv = data.get("schema_version", 0)
    if sv < 3:
        raise FrontmatterError(f"schema_version 必须是 ≥3,实际 {sv}")
```

- [ ] **Step 4: 跑测试 → 期望 PASS**

```bash
.venv/bin/python -m pytest tests/test_types.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/types.py tests/test_types.py
git commit -m "types: frontmatter schema v2 → v3, name/description 必填"
```

---

## Task 2: MemoryStore.write 接收 `name` / `description`

**Files:**
- Modify: `agent_core/memory/memory_store.py`(`write()` 签名)
- Test: `tests/test_memory_store.py`

**Interfaces:**
- Consumes: 现有 `write(type, body, source_quote, title=..., tags=..., extra=..., overwrite=...)`
- Produces: `write(..., name: str, description: str, ...)`,若 `title` 未传则自动 `title = name`,`name`/`description` 写入 frontmatter

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_store.py
def test_memory_store_write_v3_requires_name_description(tmp_path):
    """M11 v3:write 必须传 name + description,否则抛错"""
    from agent_core.memory.memory_store import MemoryStore
    store = MemoryStore(root=tmp_path / "memory")
    # 缺 name → 抛 ValidationError / FrontmatterError
    with pytest.raises(Exception):  # 精确匹配看 store 实现
        store.write(
            type="user", body="x", source_quote="x",
            description="desc",  # 缺 name
        )


def test_memory_store_write_v3_mirrors_name_to_title(tmp_path):
    """M11 v3:caller 没传 title 时,title 自动 = name"""
    from agent_core.memory.memory_store import MemoryStore
    store = MemoryStore(root=tmp_path / "memory")
    item_hash = store.write(
        type="user",
        name="用户叫小明",
        description="Python 后端",
        body="小明是 Python 工程师",
        source_quote="小明是 Python 工程师",
    )
    data = store.read(f"user/{item_hash}.md")
    fm = data["frontmatter"]
    assert fm["name"] == "用户叫小明"
    assert fm["description"] == "Python 后端"
    assert fm["title"] == "用户叫小明"  # mirror


def test_memory_store_write_v3_explicit_title_kept(tmp_path):
    """M11 v3:caller 传 title 时,保留 title(不覆盖)"""
    from agent_core.memory.memory_store import MemoryStore
    store = MemoryStore(root=tmp_path / "memory")
    item_hash = store.write(
        type="user",
        name="用户叫小明",
        description="Python 后端",
        body="x", source_quote="x",
        title="客户档案",  # 显式 title
    )
    data = store.read(f"user/{item_hash}.md")
    assert data["frontmatter"]["title"] == "客户档案"
    assert data["frontmatter"]["name"] == "用户叫小明"
```

- [ ] **Step 2: 跑 → FAIL**(当前 `write()` 不接 `name`/`description`,写出的 .md 没这俩字段,`validate_frontmatter` 在 read 时会抛)

- [ ] **Step 3: 改 `memory_store.py:write`**

```python
# agent_core/memory/memory_store.py

def write(
    self,
    type: str,
    body: str,
    source_quote: str,
    name: Optional[str] = None,        # M11 新增(必填,但 Optional 兼容旧 caller,内部 raise)
    description: Optional[str] = None,  # M11 新增
    title: Optional[str] = None,
    tags: Optional[list[str]] = None,
    extra: Optional[dict[str, Any]] = None,
    overwrite: bool = False,
) -> str:
    """写入一条记忆(M11 v3 schema)。"""
    if not name:
        raise FrontmatterError("name 必填(M11 v3 schema)")
    if not description:
        raise FrontmatterError("description 必填(M11 v3 schema)")

    # mirror name → title(若 caller 未传)
    if title is None:
        title = name

    # ... 原有 hash 计算逻辑(item_hash 不变) ...
    item_hash = self.compute_item_hash(type, body, source_quote)

    # 构造 frontmatter
    fm = {
        "type": type,
        "created_at": _now_iso(),
        "item_hash": item_hash,
        "schema_version": 3,  # M11
        "name": name,           # M11 新增
        "description": description,  # M11 新增
        "title": title,         # 保留兼容
        "tags": tags or [],
        **(extra or {}),
    }
    # 校验(自动截断长 description)
    validate_frontmatter(fm)

    # ... 原有写盘逻辑 ...
```

(若 `write()` 已有 frontmatter 构造逻辑,在原处插入 `name`/`description` 两个字段;`validate_frontmatter(fm)` 替换原 inline 校验。)

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_memory_store.py -v
```

注意:旧测试若用 `write()` 不传 `name`/`description`,会开始 FAIL → 必须在 T5 改造 dual_channel_writer 之前,把 store 的旧 caller 一起改。**临时方案**:把旧 `write()` 调用在 Task 2 阶段允许 `name`/`description` 默认为 `"placeholder"` + `"legacy entry"`,T5 改完 writer 后删除 placeholder 路径。**推荐方案**:在 Task 2 一次性找到所有 `store.write` 调用点,补上 `name`/`description`(用 body 第一行截断作为 fallback),T5 时再替换为正确值。

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/memory_store.py tests/test_memory_store.py
git commit -m "memory_store: write() 接收 name/description (M11 v3 schema)"
```

---

## Task 3: migration v2 → v3(自动补 `name` / `description`)

**Files:**
- Modify: `agent_core/memory/migration.py`(`MigrationRegistry.register(2, _v2_to_v3)`)
- Test: `tests/test_migration.py`

**Interfaces:**
- Consumes: 现有 `MigrationRegistry.register(from_v, fn)`;存量 v2 frontmatter
- Produces: `_v2_to_v3(fm, body) -> (fm, body)` 自动补 `name`/`description`,升级 `schema_version` 到 3

- [ ] **Step 1: 写失败测试**

```python
# tests/test_migration.py
def test_v2_to_v3_fills_name_and_description(tmp_path):
    """v2 → v3 迁移:自动补 name (= title) + description(从 body 摘要)"""
    from agent_core.memory.migration import MigrationRegistry
    fm_v2 = {
        "type": "user",
        "created_at": "2026-06-01T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 2,
        "title": "客户档案",
        "tags": ["person"],
    }
    body = "用户叫张三,深圳,Python 工程师"
    fn = MigrationRegistry._registry[2]
    fm_new, body_new = fn(fm_v2, body)
    assert fm_new["schema_version"] == 3
    assert fm_new["name"] == "客户档案"           # mirror title
    assert "张三" in fm_new["description"]        # 从 body 摘要
    assert len(fm_new["description"]) <= 200


def test_v2_to_v3_fills_description_from_title_if_body_empty(tmp_path):
    """body 为空时,description fallback 到 title"""
    from agent_core.memory.migration import MigrationRegistry
    fm_v2 = {
        "type": "user",
        "created_at": "2026-06-01T00:00:00+00:00",
        "item_hash": "a" * 64,
        "schema_version": 2,
        "title": "客户档案",
        "tags": [],
    }
    fn = MigrationRegistry._registry[2]
    fm_new, _ = fn(fm_v2, "")
    assert fm_new["description"] == "客户档案"  # fallback


def test_migrate_file_creates_bak_sidecar(tmp_path):
    """迁移写盘前生成 .bak sidecar"""
    from agent_core.memory.migration import migrate_file
    md = tmp_path / "test.md"
    md.write_text("""---
type: user
created_at: 2026-06-01T00:00:00+00:00
item_hash: aaaa
schema_version: 2
title: 旧记忆
tags: []
---
旧 body""", encoding="utf-8")
    migrate_file(md)
    assert (tmp_path / "test.md.bak").exists()
    fm = parse_frontmatter(md.read_text(encoding="utf-8"))
    assert fm["schema_version"] == 3
    assert "name" in fm and "description" in fm
```

- [ ] **Step 2: 跑 → FAIL**(当前 `MigrationRegistry` 只注册了 v0→v1 和 v1→v2,没 v2→v3)

- [ ] **Step 3: 改 `migration.py`**

```python
# agent_core/memory/migration.py

def _v2_to_v3(fm: dict, body: str) -> tuple[dict, str]:
    """v2 → v3:补 name + description,升 schema_version"""
    fm = dict(fm)  # copy,避免 mutate

    # 1. name:从 title mirror
    if "name" not in fm or not fm["name"]:
        fm["name"] = fm.get("title") or _first_meaningful_line(body)[:50]

    # 2. description:从 body 摘要
    if "description" not in fm or not fm["description"]:
        first_para = next(
            (line.strip() for line in body.split("\n")
             if line.strip() and not line.startswith("#")),
            "",
        )
        fm["description"] = first_para[:200] or fm.get("title", "未描述")[:200]

    # 3. schema_version 升 3
    fm["schema_version"] = 3

    return fm, body


def _first_meaningful_line(body: str) -> str:
    for line in body.split("\n"):
        s = line.strip().lstrip("# ").strip()
        if s:
            return s
    return ""


# 在文件末尾注册:
MigrationRegistry.register(2, _v2_to_v3)
```

(检查现有 `migrate_file` 是否已用 `MigrationRegistry.migrate(from_v, data)` — 若是,新注册会自动被调用;若是直接 list 调度,需要把 `_v2_to_v3` 加入 chain)

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_migration.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/migration.py tests/test_migration.py
git commit -m "migration: v2 → v3 自动补 name/description"
```

---

## Task 4: MemoryIndex 模块(异步 rebuild + 截断)

**Files:**
- Create: `agent_core/memory/memory_index.py`
- Create: `tests/test_memory_index.py`

**Interfaces:**
- Consumes: `memory_root: Path`(单 type 目录或根目录都行)
- Produces:
  - `MemoryIndex(root: Path)` 类,方法 `mark_dirty()`, `flush()`, `rebuild()`
  - 常量 `MAX_ENTRYPOINT_LINES=200`, `MAX_ENTRYPOINT_BYTES=25_000`, `FRONTMATTER_MAX_LINES=30`
  - `load_index() -> str` 同步读取 MEMORY.md 内容(agent_core L1 注入用)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_index.py
import threading
import time
from pathlib import Path
import pytest


@pytest.fixture
def memory_root_with_entries(tmp_path):
    """准备 5 条记忆(2 user + 1 feedback + 1 project + 1 reference)"""
    root = tmp_path / "memory"
    for t in ("user", "feedback", "project", "reference"):
        (root / t).mkdir(parents=True)
    fm_template = """---
type: {type}
created_at: 2026-06-26T00:00:00+00:00
item_hash: {hash}
schema_version: 3
name: {name}
description: {desc}
title: {name}
tags: []
---
body for {name}
"""
    entries = [
        ("user", "a" * 64, "记忆1", "第一条描述"),
        ("user", "b" * 64, "记忆2", "第二条描述"),
        ("feedback", "c" * 64, "反馈1", "反馈描述"),
        ("project", "d" * 64, "项目1", "项目描述"),
        ("reference", "e" * 64, "参考1", "参考描述"),
    ]
    for t, h, name, desc in entries:
        (root / t / f"{h}.md").write_text(
            fm_template.format(type=t, hash=h, name=name, desc=desc),
            encoding="utf-8",
        )
    return root, entries


def test_memory_index_rebuild_writes_file(memory_root_with_entries):
    """rebuild() 生成 MEMORY.md"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    idx.rebuild()
    assert (root / "MEMORY.md").exists()
    content = (root / "MEMORY.md").read_text(encoding="utf-8")
    assert "# Agent Memory (auto-generated)" in content
    for _, _, name, desc in memory_root_with_entries[1]:
        assert f"[{name}]" in content


def test_memory_index_max_200_lines(tmp_path):
    """>200 条记忆时 MEMORY.md ≤ 200 行"""
    from agent_core.memory.memory_index import MemoryIndex, MAX_ENTRYPOINT_LINES
    root = tmp_path / "memory"
    (root / "user").mkdir(parents=True)
    for i in range(250):
        h = f"{i:064x}"
        (root / "user" / f"{h}.md").write_text(
            f"---\ntype: user\ncreated_at: 2026-06-26T00:00:00+00:00\n"
            f"item_hash: {h}\nschema_version: 3\n"
            f"name: 记忆{i}\ndescription: 描述{i}\ntitle: 记忆{i}\ntags: []\n---\nbody\n",
            encoding="utf-8",
        )
    idx = MemoryIndex(root)
    idx.rebuild()
    content = (root / "MEMORY.md").read_text(encoding="utf-8")
    assert len(content.splitlines()) <= MAX_ENTRYPOINT_LINES


def test_memory_index_mark_dirty_1s_coalesce(memory_root_with_entries):
    """mark_dirty 1s 内多次调用 → 只 rebuild 1 次"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    rebuild_count = [0]
    original_rebuild = idx.rebuild
    def counting_rebuild():
        rebuild_count[0] += 1
        original_rebuild()
    idx.rebuild = counting_rebuild
    for _ in range(10):
        idx.mark_dirty()
    time.sleep(1.2)
    # 至少 1 次,但不应 > 2(只有 1 个 Timer)
    assert 1 <= rebuild_count[0] <= 2


def test_memory_index_flush_cancels_timer(memory_root_with_entries):
    """flush() 立即 rebuild 并取消 pending timer"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    idx.mark_dirty()
    idx.flush()  # 立即 rebuild
    assert idx._pending is False  # type: ignore[attr-defined]
    # 此时 MEMORY.md 已存在
    assert (root / "MEMORY.md").exists()


def test_load_index_returns_content(memory_root_with_entries):
    """load_index() 同步返回 MEMORY.md 字符串"""
    from agent_core.memory.memory_index import MemoryIndex
    root, _ = memory_root_with_entries
    idx = MemoryIndex(root)
    idx.rebuild()
    content = idx.load_index()
    assert "记忆1" in content
```

- [ ] **Step 2: 跑 → FAIL**(模块不存在)

- [ ] **Step 3: 创建 `memory_index.py`**

```python
# agent_core/memory/memory_index.py
"""
MEMORY.md 物理索引(M11)

借鉴 Claude Code 的 MEMORY.md 模式:
- <memory_root>/MEMORY.md
- 双重硬上限: 200 行 / 25KB
- 写盘后异步 rebuild(1s 合并窗口)
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000
FRONTMATTER_MAX_LINES = 30
MARK_DIRTY_DELAY = 1.0  # 1s 合并窗口
MEMORY_FILE_NAME = "MEMORY.md"
MEMORY_FILE_HEADER = "# Agent Memory (auto-generated)\n"

# ──────────────────────────────────────────────────────────────────
# scan_memory_files(T9 加进来;Task 4 先用简易版本)
# ──────────────────────────────────────────────────────────────────

_VALID_TYPES = ("user", "feedback", "event", "project", "reference")


def scan_memory_files(
    memory_root: Path,
    max_files: int = 200,
    frontmatter_max_lines: int = FRONTMATTER_MAX_LINES,
    types_filter: Optional[list[str]] = None,
) -> list["MemoryFileEntry"]:
    """扫 memory_root 下所有 .md,只读前 N 行 frontmatter,按 mtime 倒序截 max_files"""
    if not memory_root.exists():
        return []

    all_files: list[Path] = []
    for t in (types_filter or list(_VALID_TYPES)):
        type_dir = memory_root / t
        if type_dir.exists():
            all_files.extend(type_dir.glob("*.md"))

    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    all_files = all_files[:max_files]

    entries: list[MemoryFileEntry] = []
    for p in all_files:
        try:
            with p.open("r", encoding="utf-8") as f:
                head = "".join(itertools.islice(f, frontmatter_max_lines))
            fm = _parse_frontmatter_head(head)
            entries.append(MemoryFileEntry(
                rel_path=str(p.relative_to(memory_root)),
                name=fm.get("name", fm.get("title", "?")),
                description=fm.get("description", "无描述"),
                type=fm.get("type", "user"),
                mtime_ms=int(p.stat().st_mtime * 1000),
            ))
        except (OSError, ValueError, KeyError):
            continue
    return entries


def _parse_frontmatter_head(head: str) -> dict:
    """极简 frontmatter 解析 — 仅 Task 4 用,T9 会替换为正式版"""
    fm = {}
    in_fm = False
    for line in head.splitlines():
        if line.strip() == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if in_fm and ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


# ──────────────────────────────────────────────────────────────────
# MemoryFileEntry
# ──────────────────────────────────────────────────────────────────

@dataclass
class MemoryFileEntry:
    rel_path: str
    name: str
    description: str
    type: str
    mtime_ms: int


# ──────────────────────────────────────────────────────────────────
# MemoryIndex
# ──────────────────────────────────────────────────────────────────

class MemoryIndex:
    """维护 MEMORY.md 物理索引(异步 rebuild,1s 合并窗口)"""

    def __init__(self, memory_root: Path):
        self.root = Path(memory_root)
        self.path = self.root / MEMORY_FILE_NAME
        self._lock = threading.Lock()
        self._pending = False
        self._timer: Optional[threading.Timer] = None

    def mark_dirty(self) -> None:
        """标记过期,1s 后异步 rebuild(合并窗口)"""
        with self._lock:
            if self._pending:
                return
            self._pending = True
            self._timer = threading.Timer(MARK_DIRTY_DELAY, self.rebuild)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """强制同步 rebuild(测试 / 进程关闭前)"""
        with self._lock:
            if self._timer:
                self._timer.cancel()
        self.rebuild()

    def rebuild(self) -> None:
        """重建 MEMORY.md 文件(同步,持锁)"""
        with self._lock:
            self._pending = False
            entries = scan_memory_files(self.root, max_files=MAX_ENTRYPOINT_LINES)
            content = self._render(entries)
            truncated = self._truncate(content)
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self.path.write_text(truncated, encoding="utf-8")
                logger.debug(f"MEMORY.md rebuilt: {len(entries)} entries, {len(truncated)} bytes")
            except OSError as e:
                logger.warning(f"MEMORY.md 写盘失败: {e}")

    def load_index(self) -> str:
        """同步读取 MEMORY.md 内容(L1 启动加载用)"""
        if not self.path.exists():
            self.rebuild()
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def _render(self, entries: list[MemoryFileEntry]) -> str:
        lines = [MEMORY_FILE_HEADER]
        for e in entries:
            lines.append(f"- [{e.name}]({e.rel_path}) — {e.description}")
        return "\n".join(lines) + "\n"

    def _truncate(self, content: str) -> str:
        """双重硬上限:先按行截,再按字节截"""
        lines = content.splitlines()
        if len(lines) > MAX_ENTRYPOINT_LINES:
            content = "\n".join(lines[:MAX_ENTRYPOINT_LINES]) + "\n"
            logger.info(
                f"MEMORY.md 超过 {MAX_ENTRYPOINT_LINES} 行,截断"
            )
        if len(content.encode("utf-8")) > MAX_ENTRYPOINT_BYTES:
            content = content.encode("utf-8")[:MAX_ENTRYPOINT_BYTES].decode("utf-8", errors="ignore")
            logger.info(
                f"MEMORY.md 超过 {MAX_ENTRYPOINT_BYTES} 字节,截断"
            )
        return content
```

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_memory_index.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/memory_index.py tests/test_memory_index.py
git commit -m "memory_index: MEMORY.md 物理索引 + 异步 rebuild"
```

---

## Task 5: dual_channel_writer 集成新 frontmatter + `index.mark_dirty`

**Files:**
- Modify: `agent_core/memory/dual_channel_writer.py`(找到所有 `self.memory_store.write(...)` 调用点,补 `name`/`description`;在写盘后调 `index.mark_dirty()`)
- Test: `tests/test_dual_channel_concurrent.py`

**Interfaces:**
- Consumes: Task 2 新 `store.write(name, description, ...)`;Task 4 `MemoryIndex(root).mark_dirty()`
- Produces: writer 每次成功写盘后触发 `index.mark_dirty()`,并把 `name`/`description` 传透

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dual_channel_concurrent.py
def test_dual_channel_write_emits_v3_frontmatter():
    """channel A 写盘后 .md frontmatter 含 name + description + schema_version=3"""
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from tests.conftest import FakeEmbedFn
    # ... 构造 writer,触发一次 extract 候选 ...
    # 读 .md 验证:
    md_files = list((tmp / "memory" / "user").glob("*.md"))
    assert md_files
    fm = parse_frontmatter(md_files[0].read_text(encoding="utf-8"))
    assert fm.get("schema_version") == 3
    assert fm.get("name")
    assert fm.get("description")


def test_dual_channel_write_triggers_memory_index_mark_dirty(tmp_path):
    """写盘后 MemoryIndex 被 mark_dirty(MEMORY.md 1.1s 后出现)"""
    from agent_core.memory.dual_channel_writer import DualChannelWriter
    from agent_core.memory.memory_index import MemoryIndex
    from tests.conftest import FakeEmbedFn
    # ... 构造 writer with memory_index 参数 ...
    # 触发写盘
    # 1.1s 后 MEMORY.md 应存在
    time.sleep(1.2)
    assert (tmp / "memory" / "MEMORY.md").exists()
```

- [ ] **Step 2: 跑 → FAIL**(当前 writer 写出的 .md 缺 `name`/`description`;不调 `mark_dirty`)

- [ ] **Step 3: 改 `dual_channel_writer.py`**

定位所有 `self.memory_store.write(` 调用点(grep "memory_store.write"),每个调用点:
1. 补 `name=` 和 `description=` 参数(`name` 用 cand.title,`description` 从 cand.body 截断 200 字符或由 extraction prompt 预生成)
2. 在 `write()` 成功后,若 writer 持有 `self.memory_index`,调 `self.memory_index.mark_dirty()`

```python
# dual_channel_writer.py: 在 __init__ 加可选参数
def __init__(
    self,
    memory_store,
    vector_store,
    embed_fn,
    memory_index: Optional[MemoryIndex] = None,  # M11
    ...
):
    self.memory_index = memory_index

# 在所有 write 成功后:
self.memory_store.write(
    type=...,
    name=cand.title or "(无名)",
    description=cand.body[:200] or cand.title or "(无描述)",
    body=...,
    source_quote=...,
    ...
)
# 写盘后:
if self.memory_index:
    self.memory_index.mark_dirty()
```

(具体调用点依 writer 现有结构而定,grep 后逐处补)

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_dual_channel_concurrent.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/dual_channel_writer.py tests/test_dual_channel_concurrent.py
git commit -m "dual_channel_writer: 写盘传 name/description + index.mark_dirty"
```

---

## Task 6: retriever 删 keyword / hybrid 代码

**Files:**
- Modify: `agent_core/memory/retriever.py`(删 `_tokenize`, `_keyword_score`, `_keyword_search`, `_merge_hits`, hybrid `_rerank`)
- Modify: `tests/test_retriever.py`(删 keyword / hybrid 相关测试)

- [ ] **Step 1: 删测试** — `tests/test_retriever.py` 删:
- `test_keyword_search_finds_relevant`
- `test_hybrid_search_finds_relevant`
- `test_hybrid_uses_keyword_when_vec_empty`
- 以及其他任何含 `mode="keyword"` / `mode="hybrid"` 的 fixture 或测试

- [ ] **Step 2: 跑现有测试 → 应有部分 FAIL**(因为 mode 字面量改了)

```bash
.venv/bin/python -m pytest tests/test_retriever.py -v
```

- [ ] **Step 3: 删 `retriever.py` 代码**

从 `retriever.py` 删除:
- L56-60:`RetrievalMode.KEYWORD` 和 `RetrievalMode.HYBRID`(只留 SEMANTIC,SIDE_QUERY 在 T7 加)
- L126-175:`_tokenize`, `_keyword_score`(含 docstring)
- L384-432:`_keyword_search`(含 docstring)
- L434-450:`_merge_hits`(含 docstring)
- L461-470:`_rerank` 中 `if mode == RetrievalMode.HYBRID:` 整段(`sem_w`, `kw_w` 字段也删)
- L298-312(`_retrieve_candidates`):删 `if mode == RetrievalMode.KEYWORD:` 和 HYBRID 融合分支,只留 SEMANTIC(T7 再加 SIDE_QUERY)
- L252:把 `top_k * 3` 改回 `top_k`(不再需要 3x 候选)
- `RetrievalError` message 中移除 "keyword"/"hybrid" 引用

简化后的 `RetrievalMode` 和 `_retrieve_candidates`:

```python
class RetrievalMode(str, Enum):
    SEMANTIC = "semantic"
    # SIDE_QUERY = "side_query"  # T7 加


def _retrieve_candidates(self, query, top_k, mode, types):
    if mode == RetrievalMode.SEMANTIC:
        return self._semantic_search(query, top_k, types)
    raise RetrievalError(f"未知检索模式: {mode!r}")  # T7 加 SIDE_QUERY 分支
```

`_rerank` 简化为:

```python
def _rerank(self, candidates, mode):
    return sorted(candidates, key=lambda h: h.score, reverse=True)
```

(无 hybrid 加权,所有模式都用原 score 排序)

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_retriever.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/retriever.py tests/test_retriever.py
git commit -m "retriever: 删除 keyword / hybrid 模式"
```

---

## Task 7: retriever 新增 `side_query` 模式

**Files:**
- Modify: `agent_core/memory/retriever.py`(加 `RetrievalMode.SIDE_QUERY`, `_side_query_search`, `_call_side_query`,接受 `already_surfaced`, `llm_router`)
- Modify: `tests/test_retriever.py`(新增 3 个测试)

**Interfaces:**
- Consumes: `self.llm_router`(新依赖,在 `__init__` 加,向后兼容 Optional);`scan_memory_files`, `format_memory_manifest`, `SIDE_QUERY_SYSTEM_PROMPT`, `build_side_query_prompt`(T9/T10 出)
- Produces: `retriever.search(..., mode="side_query", already_surfaced=set())`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_retriever.py
def test_side_query_basic(memory_root_with_llm_stub):
    """sideQuery 模式:LLM 选 path,读全文,构造 MemoryHit"""
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    ms, vec, embed, llm_router = memory_root_with_llm_stub
    retriever = MemoryRetriever(ms, vec, embed, llm_router=llm_router)
    report = retriever.search("用户叫什么", top_k=2, mode=RetrievalMode.SIDE_QUERY)
    assert report.mode == RetrievalMode.SIDE_QUERY
    assert 0 < len(report.hits) <= 2
    for hit in report.hits:
        assert hit.breakdown == {"side_query": 1.0}


def test_side_query_already_surfaced_filter(memory_root_with_llm_stub):
    """already_surfaced 过滤已展示过的记忆"""
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    ms, vec, embed, llm_router = memory_root_with_llm_stub
    retriever = MemoryRetriever(ms, vec, embed, llm_router=llm_router)
    # 第一次检索拿 top1 的 rel_path
    r1 = retriever.search("user", top_k=1, mode=RetrievalMode.SIDE_QUERY)
    surfaced = {r1.hits[0].rel_path}
    # 第二次带 already_surfaced
    r2 = retriever.search("user", top_k=1, mode=RetrievalMode.SIDE_QUERY,
                          already_surfaced=surfaced)
    for hit in r2.hits:
        assert hit.rel_path not in surfaced


def test_side_query_failure_returns_empty(memory_root_with_broken_llm):
    """LLM 失败时 sideQuery 降级返空(不抛)"""
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    ms, vec, embed, llm_router = memory_root_with_broken_llm
    retriever = MemoryRetriever(ms, vec, embed, llm_router=llm_router)
    report = retriever.search("user", top_k=2, mode=RetrievalMode.SIDE_QUERY)
    assert report.hits == []
```

(fixtures `memory_root_with_llm_stub` / `memory_root_with_broken_llm` 在 conftest.py 加,或在本测试文件加 local fixture,stub 一个返回固定 JSON 的 `llm_router`)

- [ ] **Step 2: 跑 → FAIL**(当前没 SIDE_QUERY 模式,没 `llm_router` 参数)

- [ ] **Step 3: 改 `retriever.py`**

```python
# retriever.py

class RetrievalMode(str, Enum):
    SEMANTIC = "semantic"
    SIDE_QUERY = "side_query"  # M11 新增


class MemoryRetriever:
    def __init__(
        self,
        memory_store,
        vector_store,
        embed_fn,
        config=None,
        secret_scanner=None,
        llm_router=None,  # M11 新增(sideQuery 用)
    ):
        ...
        self.llm_router = llm_router

    def search(
        self,
        query,
        top_k=5,
        mode="semantic",
        types=None,
        min_score=0.0,
        already_surfaced=None,  # M11 新增
    ):
        ...
        candidates = self._retrieve_candidates(query, top_k, mode, types, already_surfaced)
        ranked = self._rerank(candidates, mode)
        filtered = self._filter_secrets(ranked)
        final = filtered[:top_k]
        ...

    def _retrieve_candidates(self, query, top_k, mode, types, already_surfaced=None):
        if mode == RetrievalMode.SEMANTIC:
            return self._semantic_search(query, top_k, types)
        if mode == RetrievalMode.SIDE_QUERY:
            return self._side_query_search(query, top_k, types, already_surfaced)
        raise RetrievalError(f"未知检索模式: {mode!r}")

    def _side_query_search(self, query, top_k, types, already_surfaced):
        from agent_core.memory.memory_index import (
            scan_memory_files, format_memory_manifest, MAX_ENTRYPOINT_LINES,
        )
        from agent_core.memory.prompt_templates import (
            SIDE_QUERY_SYSTEM_PROMPT, build_side_query_prompt,
        )

        max_files = getattr(self.config.retrieval, "side_query_max_files", 200)
        max_select = getattr(self.config.retrieval, "side_query_max_select", top_k)

        entries = scan_memory_files(
            self.memory_store.root, max_files=max_files,
            types_filter=types,
        )
        if already_surfaced:
            entries = [e for e in entries if e.rel_path not in already_surfaced]
        if not entries:
            return []

        manifest = format_memory_manifest(entries)
        selected = self._call_side_query(query, manifest, max_select)

        hits = []
        for path in selected:
            try:
                data = self.memory_store.read(path)
            except Exception:
                continue
            fm = data.get("frontmatter", {}) or {}
            body = data.get("body", "")
            hits.append(MemoryHit(
                item_hash=fm.get("item_hash", ""),
                type=fm.get("type", "user"),
                title=fm.get("name", fm.get("title", "")),
                body=body,
                rel_path=path,
                score=1.0,
                breakdown={"side_query": 1.0},
                tags=fm.get("tags", []),
                importance=fm.get("importance", 5),
            ))
        return hits

    def _call_side_query(self, query, manifest, max_select):
        import json
        from agent_core.memory.prompt_templates import (
            SIDE_QUERY_SYSTEM_PROMPT, build_side_query_prompt,
        )
        if not self.llm_router:
            logger.warning("sideQuery 需要 llm_router,当前为 None,降级返空")
            return []
        prompt = build_side_query_prompt(query, manifest, max_select)
        text = ""
        try:
            for chunk in self.llm_router.chat(
                messages=[
                    {"role": "system", "content": SIDE_QUERY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                cache_namespace="memory_side_query",
            ):
                if chunk.text_delta:
                    text += chunk.text_delta.text
            data = json.loads(_strip_code_fence(text))
            return data.get("selected_paths", [])[:max_select]
        except Exception as e:
            logger.warning(f"sideQuery 失败,降级返空: {e}")
            return []


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()
```

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_retriever.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/retriever.py tests/test_retriever.py
git commit -m "retriever: 新增 side_query 模式 + already_surfaced 去重"
```

---

## Task 8: config.mode 改 `"semantic" | "side_query"`(删 weight 字段)

**Files:**
- Modify: `agent_core/memory/config.py`(`RetrievalConfig`)
- Modify: `tests/test_config.py`(验证旧 mode 抛错)

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py
def test_retrieval_config_old_modes_rejected():
    """M11:旧 mode='vector'/'file'/'hybrid'/'keyword' 必抛 ValidationError"""
    from agent_core.memory.config import RetrievalConfig
    from pydantic import ValidationError
    for old_mode in ("vector", "file", "hybrid", "keyword"):
        with pytest.raises(ValidationError):
            RetrievalConfig(mode=old_mode)


def test_retrieval_config_new_modes_accepted():
    """M11:新 mode='semantic'/'side_query' 通过"""
    from agent_core.memory.config import RetrievalConfig
    assert RetrievalConfig(mode="semantic").mode == "semantic"
    assert RetrievalConfig(mode="side_query").mode == "side_query"


def test_retrieval_config_no_weight_fields():
    """M11:删 semantic_weight / lexical_weight"""
    from agent_core.memory.config import RetrievalConfig
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RetrievalConfig(semantic_weight=0.7)
    with pytest.raises(ValidationError):
        RetrievalConfig(lexical_weight=0.3)


def test_retrieval_config_side_query_defaults():
    """M11 新增 side_query_max_select / side_query_max_files 默认值"""
    from agent_core.memory.config import RetrievalConfig
    cfg = RetrievalConfig()
    assert cfg.side_query_max_select == 5
    assert cfg.side_query_max_files == 200
```

- [ ] **Step 2: 跑 → FAIL**(当前 `mode=Literal["vector", "file", "hybrid"]` 接受旧值)

- [ ] **Step 3: 改 `config.py`**

```python
# agent_core/memory/config.py

class RetrievalConfig(BaseModel):
    """检索相关配置(M11:二选一,switchable)"""
    model_config = ConfigDict(extra="forbid")

    mode: Literal["semantic", "side_query"] = "semantic"
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.3, ge=0.0, le=1.0)
    token_budget: int = Field(default=2000, ge=100, le=8000)

    # M11 新增
    side_query_max_select: int = Field(default=5, ge=1, le=10)
    side_query_max_files: int = Field(default=200, ge=10, le=1000)

    # M11 删除:semantic_weight / lexical_weight / _weights_sum_to_one
```

(删除 `semantic_weight` / `lexical_weight` 字段定义和 `@model_validator` 的 `_weights_sum_to_one`)

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/config.py tests/test_config.py
git commit -m "config: mode 改 semantic/side_query 二选一,删 weight 字段"
```

---

## Task 9: `scan_memory_files` + `format_memory_manifest` 完整版(T4 已有雏形)

**Files:**
- Modify: `agent_core/memory/memory_index.py`(把 T4 的简易 `_parse_frontmatter_head` 替换为正式版)
- Modify: `tests/test_memory_index.py`(增加正式版解析的边界测试)

**Interfaces:**
- Consumes: 现有 `scan_memory_files`(T4 已实现)
- Produces: 复用 `parse_frontmatter` 从 `memory_store.py`(更准确解析 YAML),`format_memory_manifest(entries) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_memory_index.py
def test_scan_memory_files_respects_types_filter(tmp_path):
    """types_filter 只扫指定 type"""
    from agent_core.memory.memory_index import scan_memory_files
    root = tmp_path / "memory"
    for t in ("user", "feedback", "project"):
        (root / t).mkdir(parents=True)
        (root / t / f"{t}1.md").write_text(
            f"---\ntype: {t}\nname: n\ndescription: d\nschema_version: 3\n"
            f"item_hash: {'x'*64}\ncreated_at: 2026-06-26T00:00:00+00:00\n---\nbody\n",
            encoding="utf-8",
        )
    entries = scan_memory_files(root, types_filter=["user"])
    assert len(entries) == 1
    assert entries[0].type == "user"


def test_scan_memory_files_sorted_by_mtime_desc(tmp_path):
    """按 mtime 倒序"""
    import time
    from agent_core.memory.memory_index import scan_memory_files
    root = tmp_path / "memory" / "user"
    root.mkdir(parents=True)
    for i, name in enumerate(["old", "mid", "new"]):
        p = root / f"{i}.md"
        p.write_text(
            f"---\ntype: user\nname: {name}\ndescription: d\nschema_version: 3\n"
            f"item_hash: {'y'*64}\ncreated_at: 2026-06-26T00:00:00+00:00\n---\nb\n",
            encoding="utf-8",
        )
        time.sleep(0.01)  # 错开 mtime
    entries = scan_memory_files(root)
    assert [e.name for e in entries] == ["new", "mid", "old"]


def test_format_memory_manifest_renders_correctly():
    """format_memory_manifest 渲染格式对齐 CC"""
    from agent_core.memory.memory_index import MemoryFileEntry, format_memory_manifest
    entries = [
        MemoryFileEntry(rel_path="user/abc.md", name="用户",
                        description="小明", type="user", mtime_ms=0),
        MemoryFileEntry(rel_path="feedback/xyz.md", name="反馈",
                        description="不要 mock", type="feedback", mtime_ms=0),
    ]
    out = format_memory_manifest(entries)
    assert out == (
        "- [用户](user/abc.md) — 小明\n"
        "- [反馈](feedback/xyz.md) — 不要 mock"
    )
```

- [ ] **Step 2: 跑 → FAIL**(T4 的 `_parse_frontmatter_head` 是极简版,可能对 quoted / multiline 解析失败;`format_memory_manifest` 还没单独 export)

- [ ] **Step 3: 改 `memory_index.py`**

```python
# memory_index.py:替换 _parse_frontmatter_head 为复用 store 的解析

def scan_memory_files(
    memory_root, max_files=200, frontmatter_max_lines=FRONTMATTER_MAX_LINES,
    types_filter=None,
):
    # ... (同 T4) ...
    for p in all_files:
        try:
            with p.open("r", encoding="utf-8") as f:
                head = "".join(itertools.islice(f, frontmatter_max_lines))
            # 复用 memory_store 的 parse_frontmatter(更准)
            from agent_core.memory.memory_store import parse_frontmatter
            fm, _ = parse_frontmatter(head)
            entries.append(MemoryFileEntry(...))
        except (OSError, ValueError, KeyError):
            continue
    return entries


def format_memory_manifest(entries: list[MemoryFileEntry]) -> str:
    """'- [name](rel_path) — description' per line"""
    return "\n".join(
        f"- [{e.name}]({e.rel_path}) — {e.description}"
        for e in entries
    )
```

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_memory_index.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/memory_index.py tests/test_memory_index.py
git commit -m "memory_index: scan 复用 store parse_frontmatter,format_memory_manifest 导出"
```

---

## Task 10: `SIDE_QUERY_SYSTEM_PROMPT` + `build_side_query_prompt`

**Files:**
- Modify: `agent_core/memory/prompt_templates.py`
- Modify: `tests/test_prompt_templates.py`

**Interfaces:**
- Produces:
  - `SIDE_QUERY_SYSTEM_PROMPT: str`
  - `build_side_query_prompt(query: str, manifest: str, max_select: int) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_prompt_templates.py
def test_side_query_system_prompt_has_json_schema():
    """SIDE_QUERY_SYSTEM_PROMPT 含 selected_paths JSON 字段说明"""
    from agent_core.memory.prompt_templates import SIDE_QUERY_SYSTEM_PROMPT
    assert "selected_paths" in SIDE_QUERY_SYSTEM_PROMPT
    assert "JSON" in SIDE_QUERY_SYSTEM_PROMPT


def test_build_side_query_prompt_includes_query_and_manifest():
    """build_side_query_prompt 把 query + manifest 拼到 prompt"""
    from agent_core.memory.prompt_templates import build_side_query_prompt
    prompt = build_side_query_prompt("用户叫啥", "- [用户](user/x.md) — 张三", 5)
    assert "用户叫啥" in prompt
    assert "- [用户](user/x.md)" in prompt
    assert "5" in prompt


def test_build_side_query_prompt_max_select_in_instruction():
    """prompt 里有 ≤max_select 提示"""
    from agent_core.memory.prompt_templates import build_side_query_prompt
    prompt = build_side_query_prompt("q", "m", 3)
    assert "≤3" in prompt or "≤ 3" in prompt
```

- [ ] **Step 2: 跑 → FAIL**

- [ ] **Step 3: 改 `prompt_templates.py`**

```python
# agent_core/memory/prompt_templates.py

SIDE_QUERY_SYSTEM_PROMPT = """你是 memory recall selector。
用户给了一个 query 和一份 manifest(记忆索引),请从 manifest 中选出 ≤{max_select} 个最相关的 path。

规则:
- 只输出 JSON,严格按 schema
- 不要选完全无关的(描述不匹配的)
- 少于 {max_select} 个也行(强制过滤)
- 如果都不相关,selected_paths = []
- 不要解释,不要 markdown fence

JSON schema:
{{"selected_paths": ["user/abc.md", "feedback/xyz.md", ...]}}"""


def build_side_query_prompt(query: str, manifest: str, max_select: int) -> str:
    """拼 sideQuery prompt(注入到 user message)"""
    return f"""<query>
{query}
</query>

<memory_manifest>
{manifest}
</memory_manifest>

请从 manifest 中选 ≤{max_select} 个最相关的 path,输出 JSON。"""
```

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_prompt_templates.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/memory/prompt_templates.py tests/test_prompt_templates.py
git commit -m "prompt_templates: SIDE_QUERY_SYSTEM_PROMPT + build_side_query_prompt"
```

---

## Task 11: agent_core.py:514 L1 启动加载 + `already_surfaced` Set

**Files:**
- Modify: `agent_core/agent_core.py`(`AgentCore.__init__` 加 `memory_index` 和 `_surfaced_memories`;`stream_chat` / `chat` 检索点调 `search(..., already_surfaced=self._surfaced_memories)`;`_build_system_prompt_with_memory` 加载 MEMORY.md)
- Modify: `tests/conftest.py`(加 `llm_router_stub` fixture)
- Modify: `tests/test_agent_core.py`(新测试)

**Interfaces:**
- Consumes: Task 4 `MemoryIndex(root).load_index()`;Task 7 retriever `already_surfaced` 参数
- Produces: `AgentCore._surfaced_memories: set[str]`, 每次检索后自动更新

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent_core.py
def test_agent_core_injects_memory_index_into_system_prompt(tmp_path):
    """L1:AgentCore 启动时把 MEMORY.md 内容注入 system prompt"""
    from agent_core.agent_core import AgentCore
    # ... 准备 memory_root, 有一条记忆,rebuild MEMORY.md ...
    # ... 构造 AgentCore ...
    # ... 调用 _build_system_prompt_with_memory() ...
    prompt = core._build_system_prompt_with_memory()
    assert "记忆1" in prompt
    assert prompt.startswith(base_system_prompt) or prompt.endswith(MEMORY_HEADER)


def test_agent_core_tracks_surfaced_memories(tmp_path):
    """stream_chat 后 _surfaced_memories 含本次注入的记忆"""
    from agent_core.agent_core import AgentCore
    # 准备 memory,有 3 条记忆
    # mock retriever 返回 2 个 hit
    core = AgentCore(...)
    core.stream_chat("test message")
    assert len(core._surfaced_memories) > 0


def test_agent_core_already_surfaced_passed_to_retriever(tmp_path):
    """第二次 stream_chat 调 retriever.search 时 already_surfaced 非空"""
    from agent_core.agent_core import AgentCore
    core = AgentCore(...)
    seen_calls = []
    real_search = core.memory_retriever.search
    def spy_search(*args, **kwargs):
        seen_calls.append(kwargs.get("already_surfaced"))
        return real_search(*args, **kwargs)
    core.memory_retriever.search = spy_search
    core.stream_chat("m1")
    core.stream_chat("m2")
    assert seen_calls[0] is None or seen_calls[0] == set()
    assert seen_calls[1] is not None and len(seen_calls[1]) > 0
```

- [ ] **Step 2: 跑 → FAIL**(当前 `AgentCore` 没 `memory_index` / `_surfaced_memories` 字段)

- [ ] **Step 3: 改 `agent_core.py`**

```python
# agent_core/agent_core.py

from agent_core.memory.memory_index import MemoryIndex

class AgentCore:
    def __init__(self, ..., memory_store=None, memory_retriever=None, ...):
        # ... 原有 ...
        self._surfaced_memories: set[str] = set()
        # M11 新增:memory_index(若 memory_store 已提供)
        if memory_store and not hasattr(self, "memory_index"):
            self.memory_index = MemoryIndex(memory_store.root)
        # L1:首次加载 MEMORY.md
        if self.memory_index:
            try:
                self.memory_index.rebuild()  # lazy rebuild 兜底
            except Exception as e:
                logger.warning(f"MEMORY.md lazy rebuild 失败: {e}")

    def _build_system_prompt_with_memory(self) -> str:
        """L1:启动加载 + base prompt 拼接"""
        base = self.system_prompt or ""
        if not self.memory_index:
            return base
        try:
            index_content = self.memory_index.load_index()
        except Exception as e:
            logger.warning(f"MEMORY.md 加载失败,跳过: {e}")
            return base
        # 独立 H1 段(借鉴 CC appendSystemPrompt)
        return f"{base}\n\n{index_content}"

    def stream_chat(self, message):
        # 检索时排除已展示
        report = self.memory_retriever.search(
            message["content"], top_k=5,
            already_surfaced=self._surfaced_memories,
        )
        # 记录已展示
        for hit in report.hits:
            self._surfaced_memories.add(hit.rel_path)
        # ... 原 LLM 调用逻辑 ...
```

(具体位置:`agent_core.py:514` 是检索调用点,前后需 `try/except` 包裹 `search` 和 `add` 操作)

- [ ] **Step 4: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_agent_core.py tests/conftest.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent_core/agent_core.py tests/test_agent_core.py tests/conftest.py
git commit -m "agent_core: L1 MEMORY.md 启动加载 + already_surfaced Set"
```

---

## Task 12: web/app.py Runtime Config 增 2 字段

**Files:**
- Modify: `web/app.py`(Runtime Config panel 增加 `mode` select 和 `side_query_max_select` slider)

- [ ] **Step 1: 定位 Runtime Config panel**

`grep -n "retrieval" web/app.py` 和 `grep -n "config" web/app.py` — 找到 Runtime Config 的 streamlit widget 渲染处(在 sidebar 或 expander 中)

- [ ] **Step 2: 加 2 个 widget**

```python
# web/app.py:Runtime Config section
# 找到已有类似 st.selectbox("检索模式", ...) 的代码,替换为:

retrieval_mode = st.selectbox(
    "检索模式(M11)",
    options=["semantic", "side_query"],
    index=0 if st.session_state.get("retrieval_mode", "semantic") == "semantic" else 1,
    help="semantic: 向量召回(side_query: LLM 二次精选,≤5 个)",
)

side_query_max_select = st.slider(
    "sideQuery 最多选几个",
    min_value=1, max_value=10,
    value=st.session_state.get("side_query_max_select", 5),
    help="仅 sideQuery 模式生效",
)

# 把这两个值塞进 session_state,后续传给 AgentCore / MemoryConfig
st.session_state["retrieval_mode"] = retrieval_mode
st.session_state["side_query_max_select"] = side_query_max_select
```

(找到 `AgentCore(...)` 构造点,把 `retrieval_mode` / `side_query_max_select` 透传成 `RetrievalConfig(mode=..., side_query_max_select=...)`)

- [ ] **Step 3: 手动冒烟**

```bash
.venv/bin/python -m streamlit run web/app.py
```

验证:
1. Runtime Config 面板出现 2 个新字段
2. 切换 mode 不报错
3. sideQuery 模式下对话能触发 LLM sideQuery 路径(logs/agent.log 含 `sideQuery` 字样)

- [ ] **Step 4: Commit**

```bash
git add web/app.py
git commit -m "app: Runtime Config 增 retrieval mode + side_query_max_select"
```

---

## Task 13: TRUSTING_RECALL_SECTION 独立 H2 段

**Files:**
- Modify: `agent_core/agent_core.py`(`_build_system_prompt_with_memory` 末尾追加)

- [ ] **Step 1: 加常量 + 拼接**

```python
# agent_core/agent_core.py

TRUSTING_RECALL_SECTION = """
## Before recommending from memory

A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:
- If the memory names a file path: check the file exists.
- If the memory names a function or flag: grep for it.
- If the user is about to act on your recommendation (not just asking about history), verify first.

"The memory says X exists" is not the same as "X exists now."
"""


def _build_system_prompt_with_memory(self) -> str:
    base = self.system_prompt or ""
    if not self.memory_index:
        return base + "\n" + TRUSTING_RECALL_SECTION
    try:
        index_content = self.memory_index.load_index()
    except Exception:
        return base + "\n" + TRUSTING_RECALL_SECTION
    return f"{base}\n\n{index_content}\n\n{TRUSTING_RECALL_SECTION}"
```

- [ ] **Step 2: 跑现有 agent_core 测试**

```bash
.venv/bin/python -m pytest tests/test_agent_core.py -v
```

(若有测试断言 system prompt 完整内容,需更新断言;若是 substring 断言,无影响)

- [ ] **Step 3: Commit**

```bash
git add agent_core/agent_core.py
git commit -m "agent_core: TRUSTING_RECALL_SECTION 独立 H2 段追加"
```

---

## Task 14: e2e 集成测试 + 端到端冒烟

**Files:**
- Create: `tests/test_e2e_memory_recall.py`(端到端 L1+L2 联动)
- Modify: `tests/test_index_rebuild_on_write.py`(若已有,改写;否则新增)

**Interfaces:**
- Consumes: `AgentCore`, `MemoryIndex`, `MemoryRetriever`, `MemoryStore`, 全部 wired
- Produces: 端到端测试 L1 启动加载 → L2 sideQuery 检索 → 写入触发 rebuild → MEMORY.md 更新

- [ ] **Step 1: 写 e2e 测试**

```python
# tests/test_e2e_memory_recall.py
def test_e2e_l1_l2_side_query(tmp_path):
    """L1 启动加载 → L2 sideQuery 召回 → 拼到 system prompt"""
    from agent_core.agent_core import AgentCore
    from agent_core.memory.memory_store import MemoryStore
    from agent_core.memory.memory_index import MemoryIndex
    from agent_core.memory.retriever import MemoryRetriever, RetrievalMode
    from agent_core.memory.config import RetrievalConfig

    # 准备 3 条记忆
    memory_root = tmp_path / "memory"
    store = MemoryStore(root=memory_root)
    store.write(type="user", name="用户叫小明",
                description="Python 后端工程师,深圳",
                body="小明是 Python 工程师,深圳",
                source_quote="小明是 Python 工程师")
    store.write(type="feedback", name="不要 mock DB",
                description="学习阶段 mock 会掩盖真实行为",
                body="feedback 内容", source_quote="feedback")
    store.write(type="project", name="Go REST 项目结构",
                description="用了 chi + sqlx + testify",
                body="项目结构笔记", source_quote="chi + sqlx")

    index = MemoryIndex(memory_root)
    index.flush()  # 立即重建 MEMORY.md

    # L1:加载 MEMORY.md
    content = index.load_index()
    assert "用户叫小明" in content
    assert "不要 mock DB" in content
    assert "Go REST 项目结构" in content

    # L2:sideQuery 检索(stub llm_router)
    stub_router = _make_stub_router(selected_paths=["user/小明.md", "feedback/不要mockDB.md"])
    vec = _make_fake_vec()
    retriever = MemoryRetriever(
        store, vec, FakeEmbedFn(),
        config=RetrievalConfig(mode="side_query"),
        llm_router=stub_router,
    )
    report = retriever.search("Python 工程师", top_k=2, mode=RetrievalMode.SIDE_QUERY)
    assert len(report.hits) == 2
    names = [h.title for h in report.hits]
    assert "用户叫小明" in names


def test_e2e_write_triggers_index_rebuild(tmp_path):
    """写盘后 MEMORY.md 1.1s 内更新"""
    from agent_core.memory.memory_store import MemoryStore
    from agent_core.memory.memory_index import MemoryIndex
    import time

    root = tmp_path / "memory"
    store = MemoryStore(root=root)
    index = MemoryIndex(root)

    # 写一条
    store.write(type="user", name="新记忆",
                description="新描述", body="b", source_quote="b")
    index.mark_dirty()

    # 1.1s 后 MEMORY.md 含新记忆
    time.sleep(1.2)
    content = (root / "MEMORY.md").read_text(encoding="utf-8")
    assert "新记忆" in content
```

(`_make_stub_router` / `_make_fake_vec` 是本地 helper,stub 一个返回固定 JSON 的 `llm_router`,以及一个空的 `vec`(sideQuery 模式不需要 vec))

- [ ] **Step 2: 跑测试 → PASS**

```bash
.venv/bin/python -m pytest tests/test_e2e_memory_recall.py -v
```

- [ ] **Step 3: 端到端冒烟**

```bash
.venv/bin/python -m streamlit run web/app.py
```

手动验证:
1. 写一条 user 记忆("我叫张三")
2. 切换 Runtime Config 为 side_query 模式
3. 发对话"你记得我叫什么吗"
4. 看 logs/agent.log:
   - `sideQuery` 字样出现
   - LLM 响应中提到"用户叫张三"
5. 关闭浏览器,重启 streamlit
6. 重新发同样对话
7. 验证 _surfaced_memories 生效(第二次查询不带"张三")

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_memory_recall.py
git commit -m "tests: e2e memory recall (L1 + L2 sideQuery + index rebuild)"
```

---

## Task 15: 清理(grep + 删残留 keyword/hybrid 引用)

**Files:**
- Modify: 多文件 grep 检查

- [ ] **Step 1: grep 残留引用**

```bash
grep -rn "_tokenize\|_keyword_score\|_keyword_search\|_merge_hits\|semantic_weight\|lexical_weight" \
    agent_core/ tests/ web/
```

期望:除 `chroma_store.py` 外(若有引用)无残留

- [ ] **Step 2: 逐个清理**

对每个 grep 命中:
- 若是测试 fixture / 文档:`Edit` 删除
- 若是注释:`Edit` 删除或更新
- 若是 import:`Edit` 删除

- [ ] **Step 3: 跑全套 memory 相关测试**

```bash
.venv/bin/python -m pytest tests/test_types.py tests/test_memory_store.py \
    tests/test_migration.py tests/test_memory_index.py \
    tests/test_dual_channel_concurrent.py tests/test_retriever.py \
    tests/test_config.py tests/test_prompt_templates.py \
    tests/test_agent_core.py tests/test_e2e_memory_recall.py \
    tests/test_chroma_empty_tags.py tests/test_chroma_payload_contract.py \
    tests/test_extract_candidates_wal.py tests/test_cold_start.py \
    tests/test_dedup.py -v
```

期望:全过

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "cleanup: 移除 keyword/hybrid 残留引用"
```

---

## Task 16: 全套回归 + 文档更新

**Files:**
- Modify: `docs/memory-system-design.md`(v2.2 → v2.3 增量更新 — 主文档)
- Modify: `docs/memory-test-coverage-matrix.md`(M11 列追加)

- [ ] **Step 1: 全套回归**

```bash
.venv/bin/python -m pytest tests/ -v -k "memory or extraction or dual_channel or react_memory or chroma or config"
```

(只跑 memory 相关,不全量;参考用户偏好)

期望:全过;若 FAIL,回去修对应 task

- [ ] **Step 2: 更新 memory-system-design.md**

在文档顶部加 M11 changelog,引用本计划文件:

```markdown
## v2.3(M11, 2026-06-26)

对齐 Claude Code memory 系统的 4 大改造:
- frontmatter schema v3:`name` / `description` 必填
- MEMORY.md 物理索引(200 行 / 25KB 硬上限)
- 删除 keyword / hybrid 模式,只保留 semantic + sideQuery 二选一
- L1 启动加载 + L2 sideQuery 召回 + already_surfaced 去重

详见 [2026-06-26-m11-frontmatter-memory-md-align-design.md](../2026-06-26-m11-frontmatter-memory-md-align-design.md)
```

- [ ] **Step 3: 更新 test-coverage-matrix**

在 matrix 表 M11 列填:✅ (T1-T15 全部完成)

- [ ] **Step 4: 最终冒烟**

```bash
.venv/bin/python -m streamlit run web/app.py
```

完整跑一遍:
1. semantic 模式对话
2. sideQuery 模式对话
3. 写一条记忆 → 等 1.2s → 验证 MEMORY.md 更新
4. 多轮对话 → 验证 _surfaced_memories 生效

- [ ] **Step 5: Commit**

```bash
git add docs/memory-system-design.md docs/memory-test-coverage-matrix.md
git commit -m "docs: memory-system-design.md v2.3 + test-coverage matrix M11"
```

---

## 复用资产

| 资产 | 位置 | 复用 Task |
|------|------|---------|
| `MemoryStore.write` | `agent_core/memory/memory_store.py` | T2,T3,T5 |
| `MemoryStore.read` | `agent_core/memory/memory_store.py` | T3,T7 |
| `MigrationRegistry.register(2, fn)` | `agent_core/memory/migration.py` | T3 |
| `MemoryStore.parse_frontmatter` | `agent_core/memory/memory_store.py` | T9 |
| `MemoryHit` dataclass | `agent_core/memory/retriever.py` | T7 |
| `RetrievalConfig` (Pydantic) | `agent_core/memory/config.py` | T8,T7 |
| `FakeEmbedFn`(1024-dim) | `tests/conftest.py` | T7,T14 |
| `SecretScanner` L4 | `agent_core/memory/secret_scanner.py` | T7(已有过滤) |

---

## DoD(完成标准)

- [ ] 16 个 task 全完成(15 实施 + 1 文档/回归)
- [ ] `tests/test_types.py` + `test_memory_store.py` + `test_migration.py` 全过
- [ ] `tests/test_memory_index.py`(新文件)全过
- [ ] `tests/test_retriever.py` keyword/hybrid 测试已删,sideQuery 测试已加,全过
- [ ] `tests/test_config.py` 旧 mode 抛错 + 新 mode 通过,全过
- [ ] `tests/test_prompt_templates.py` SIDE_QUERY 测试通过
- [ ] `tests/test_agent_core.py` L1 注入 + already_surfaced 测试通过
- [ ] `tests/test_e2e_memory_recall.py`(新文件)L1+L2 联动测试通过
- [ ] 旧 `mode="vector"/"file"/"hybrid"/"keyword"` 抛 `ValidationError`
- [ ] MEMORY.md 启动时自动 rebuild + 写盘后 1s 内更新
- [ ] `_surfaced_memories` 同会话去重生效
- [ ] TRUSTING_RECALL_SECTION 出现在 system prompt 末尾
- [ ] web/app.py Runtime Config 增 2 字段
- [ ] Streamlit 端到端冒烟通过(semantic + sideQuery 双模式)
- [ ] Chroma 严格分离(T1-T8)8 commit 全部仍 PASS
- [ ] `web/app_langgraph.py` 未触碰
- [ ] 文档更新:`memory-system-design.md` v2.3 + `test-coverage-matrix` M11 列

---

## 风险与回滚

| 风险 | 回滚动作 |
|------|---------|
| migration 破坏存量 26 条 | `cp -r memory.bak/* memory/`(Task 3 走 .bak sidecar,失败可还原) |
| sideQuery 持续失败/选错 | 改 `config.retrieval.mode = "semantic"`(M11 语义模式与 M10 一致) |
| MEMORY.md 膨胀 / 损坏 | 删 `<memory_root>/MEMORY.md` → 下次 `load_index()` lazy rebuild |
| `_surfaced_memories` 误清空导致重复 | 改 `set` 为可序列化的 `list`,或在 chat session 结束时 dump |
| LLM 调用成本爆炸 | `side_query_max_select=1` + `cache_namespace="memory_side_query"`(已 cache) |

---

## 验证步骤(端到端冒烟)

```bash
# 1. 单元测试(各 task 验证)
.venv/bin/python -m pytest tests/test_types.py tests/test_memory_store.py \
    tests/test_migration.py tests/test_memory_index.py -v
# 2. retriever / config / prompt
.venv/bin/python -m pytest tests/test_retriever.py tests/test_config.py \
    tests/test_prompt_templates.py -v
# 3. agent_core / dual_channel
.venv/bin/python -m pytest tests/test_agent_core.py \
    tests/test_dual_channel_concurrent.py -v
# 4. e2e
.venv/bin/python -m pytest tests/test_e2e_memory_recall.py -v
# 5. 全套回归(只 memory 相关)
.venv/bin/python -m pytest tests/ -v -k "memory or extraction or dual_channel or react_memory or chroma or config"
# 6. Streamlit 端到端
.venv/bin/python -m streamlit run web/app.py
# 7. 观察 logs/agent.log
#    - MEMORY.md 启动时 rebuild
#    - 写盘后 1s 内 mark_dirty → rebuild
#    - sideQuery 模式日志
#    - _surfaced_memories 去重
```

---

## 实施时间表

| Task | 范围 | 工期 | 依赖 |
|------|------|------|------|
| T1 | frontmatter schema v3 | 0.3d | — |
| T2 | MemoryStore.write 改造 | 0.3d | T1 |
| T3 | migration v2→v3 | 0.4d | T1 |
| T4 | MemoryIndex 模块 | 0.5d | T1 |
| T5 | dual_channel_writer 集成 | 0.4d | T2,T4 |
| T6 | 删 keyword/hybrid | 0.3d | — |
| T7 | retriever sideQuery | 0.5d | T8,T9,T10 |
| T8 | config 二选一 | 0.2d | — |
| T9 | scan + format 完整版 | 0.3d | T4 |
| T10 | SIDE_QUERY prompts | 0.2d | — |
| T11 | agent_core L1 + surfaced | 0.4d | T4,T7 |
| T12 | web/app.py UI | 0.2d | T8 |
| T13 | TRUSTING_RECALL_SECTION | 0.1d | T11 |
| T14 | e2e 测试 + 冒烟 | 0.5d | T1-T13 |
| T15 | 清理 | 0.1d | T6 |
| T16 | 回归 + 文档 | 0.3d | T1-T15 |
| **总** | | **~5.0d** | |

---

**计划完成,保存于 `docs/superpowers/plans/2026-06-26-m11-frontmatter-memory-md-align.md`**

**两种执行方式可选**:

**1. Subagent-Driven(推荐)** — 我为每个 task dispatch 一个独立的 implementer subagent,完成 + review 之间不中断,快速迭代。subagent 看完 task brief 即可开工,无需读整个计划文件。

**2. Inline Execution** — 在当前 session 按顺序执行 task,每 task 完成后做 checkpoint review,context 连续但慢。

选哪种方式?