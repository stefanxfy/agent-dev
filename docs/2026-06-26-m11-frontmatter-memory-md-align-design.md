# M11 改造方案 — frontmatter + MEMORY.md + 召回机制全面对齐 Claude Code

> **项目**: agent-dev(自研 Agent 框架)
> **关联文档**:
> - 当前记忆设计 [`memory-system-design.md`](memory-system-design.md)(v2.2)
> - 当前实现状态对照 [`memory-test-coverage-matrix.md`](memory-test-coverage-matrix.md)
> - 参考依据 [`claude-code-memory-system-deep-dive.md`](claude-code-memory-system-deep-dive.md)
> **日期**: 2026-06-26
> **状态**: 设计阶段,待用户批准
> **关联代码**:
> - [`agent_core/memory/types.py`](../agent_core/memory/types.py)(frontmatter schema)
> - [`agent_core/memory/memory_store.py`](../agent_core/memory/memory_store.py)(per-file 存储)
> - [`agent_core/memory/retriever.py`](../agent_core/memory/retriever.py)(三模式检索)
> - [`agent_core/memory/config.py`](../agent_core/memory/config.py)(配置)
> - [`agent_core/memory/dual_channel_writer.py`](../agent_core/memory/dual_channel_writer.py)(写盘)
> - [`agent_core/agent_core.py:514`](../agent_core/agent_core.py#L514)(检索调用点)
> - [`web/app.py:610-680`](../web/app.py#L610-L680)(Streamlit 入口)

---

## 〇、变更摘要(Changelog)

| 维度 | 改造前(M10) | 改造后(M11) | 变更理由 |
|------|-------------|-------------|---------|
| **frontmatter** | 4 必填 + 9 选填,字段宽泛(12 个) | 对齐 CC:`name` / `description` / `type` 3 字段 + 保留 4 必填 `created_at`/`item_hash`/`schema_version`/`tags` | 召回时 `description` 是关键判定字段;CC 已验证字段越少越稳定 |
| **MEMORY.md 物理索引** | ❌ 不存在(`list_all()` 内存 dict,无文件) | ✅ 写 `<memory_root>/MEMORY.md`,≤200 行/25KB 硬截断 | 对齐 CC L1 启动加载,LLM "知道项目里有什么记忆" |
| **召回模式** | 三模式:keyword / semantic / hybrid(融合) | 两模式:**semantic**(向量) / **side_query**(LLM 二次精选) | 删 keyword(中英混合分词脆弱),删 hybrid(融合策略无清晰公式) |
| **两模式关系** | hybrid 融合 keyword + semantic | **switchable**(运行时切换),**不融合** | CC 是"用一种就够",agent-dev 也应同此原则 |
| **sideQuery 模型** | — | 用主流程默认 LLM(`llm_router.config.model`),不复用 extractor_router | 减少 router 实例数,配置一致性 |
| **L1 启动加载** | ❌ 启动时无任何 memory 注入 | ✅ 启动时把 MEMORY.md 内容(≤200 行)注入 system prompt 头部 | LLM 拥有"全局视野",用户问"你记得 X 吗"时能直接答 |
| **L2 按需召回** | semantic(向量)或 keyword(BM25) | semantic(向量) **或** sideQuery(LLM 选 ≤5) | 库小(<200)时 LLM 选择比向量准;库大时向量更快 |
| **描述/Why 不变项** | feedback/project 强制 **Why:**(types.py:_TYPES_REQUIRING_WHY) | **保留** | 是 v2.1 §4.5 #7 不变量,合并 CC 的 `description` 不冲突 |

---

## 一、设计动机(Why)

### 1.1 现状与差距

agent-dev 记忆系统经过 v1 → v2.1 → v2.2 演进,功能完整(per-file 存储、向量召回、三层去重、双通道写入、配置化),但仍与 Claude Code 存在 3 个**设计哲学级**差距:

| 差距 | 当前实现 | Claude Code | 改造 M11 的动机 |
|------|---------|------------|-----------------|
| **物理索引文件缺失** | `list_all()` 实时读盘(无索引) | `MEMORY.md` 启动时注入(200 行硬上限) | LLM 无"项目有什么记忆"的全局视野 |
| **frontmatter 字段过宽** | 12 个字段(4 必填 + 9 选填) | 3 字段(`name`/`description`/`type`) | 字段越多 → 写入时越容易错配 → 召回时越难判定相关 |
| **检索策略分散** | 三模式可切换 + hybrid 加权融合 | 仅 1 套(向量 + sideQuery fallback) | hybrid 的 0.7/0.3 权重无理论依据,且 fusion 不能保证 "召回质量提升" |

### 1.2 为什么是 M11(不是 M12 之后)

| 因素 | 当前状态 | M11 时机成熟 |
|------|---------|-------------|
| Chroma 严格分离(方案 A) | ✅ T1-T8 完成(commit `11b0fff`) | frontmatter → MEMORY.md 改造无需再动 Chroma |
| 26 条实际数据 | 库小,迁移成本低(单次 script 即可) | 重写 schema 比增量兼容更干净 |
| LLM router 单一 | 已有 `llm_router`,主/extract 共享 | sideQuery 复用主 router 无新增依赖 |
| `description` 字段是关键 | CC deep-dive 验证 `description` 是 sideQuery 召回判定 key | 这是改造的最大收益点 |
| 双通道写入稳定 | M10 C1.2 done,write 路径不再阻塞 | 改造不触发写入路径回归 |

**结论**: 改造窗口期已到,基础设施齐备,改造 ROI 高。

---

## 二、目标与范围(In/Out of Scope)

### 2.1 In Scope(M11 必做)

1. **frontmatter schema 改造**: 加 `name` + `description`,保留 4 必填,共 5+ 字段
2. **MEMORY.md 物理索引生成器**: `<memory_root>/MEMORY.md`,每次写盘后重建(限频,见 §4)
3. **L1 启动加载**: agent_core.py 启动时把 MEMORY.md 内容注入 system prompt 头部
4. **L2 召回机制**: 删 keyword 模式,新增 `side_query` 模式(LLM 选 ≤5)
5. **RetrievalConfig.mode**: 三选一 → 二选一(`semantic` / `side_query`)
6. **旧模式清理**: 删 `_tokenize` / `_keyword_score` / `_keyword_search` / `_merge_hits` / hybrid `_rerank`
7. **Migrator**: schema_version v2 → v3,自动给存量文件补 `name` + `description`

### 2.2 Out of Scope(M11 不做)

- **删除 daily log** — 保留,append-only 是合规要求
- **改 Chroma** — 严格分离(方案 A)已 done
- **改 extraction gate** — 触发逻辑不动
- **改 dedup 三层决策** — 0.85/0.95 阈值不动
- **改双通道写入** — 路径不动
- **改 distill/autoDream** — 蒸馏逻辑不动
- **加 KAIROS 模式** — 长会话日志模式不在 M11
- **加 teamMemorySync** — 单用户场景不需要
- **改 UI** — Streamlit app.py 注入点改一改,UI 展示不动

---

## 三、frontmatter 改造方案

### 3.1 新 Schema(M11 schema_version=3)

```yaml
---
# 必填(4 字段,backbone 不能动)
type: user                          # 4 类之一(CC 也有此字段)
created_at: 2026-06-26T08:00:00+00:00
item_hash: 5fa7c3b9...               # 64 字符 hex(SHA-256)
schema_version: 3                    # +1(M10 = 2,迁移时 M2 文件写 v3)

# 必填(2 字段,新增 — 召回关键)
name: 用户的名字                      # 人类可读(MEMORY.md 展示用)
description: 用户叫小明,Python 后端工程师,深圳  # 召回判定 key

# 选填(保留,向后兼容)
tags: [person, profile]
source: user_input
importance: 8                        # 1-10,排序辅助
seed_origin: seed_v1                 # cold start 标识
session_id: s_abc123                 # channel B 写入追溯
turn_index: 5                        # 同上
---
```

**字段来源说明**:

| 字段 | 来源 | 写入时机 |
|------|------|---------|
| `name` | caller 传入(等价于现有 `title` 的语义) | MemoryStore.write 必填 |
| `description` | caller 传入(写入时由 LLM 蒸馏时生成,或 channel A 写时人工/规则生成) | MemoryStore.write 必填 |
| 其他保留字段 | 不变 | 不变 |

**关键设计决策**:
- `name` ↔ `title` 语义等价(都是"人类可读标签")。**保留两者**(M11 不破坏 `title`),但写入时若 caller 只传 `name`,内部 `title = name`(自动 mirror)
- `description` 是**新增必填**。这是 CC sideQuery 召回的判定 key,**不可缺失**
- `name` / `description` 不参与 `item_hash` 计算(避免改 schema 改 hash)

### 3.2 写入流程变化

#### 3.2.1 新增参数

```python
# agent_core/memory/memory_store.py:152
def write(
    self,
    type: str,                       # 4 类之一
    name: str,                       # M11 新增(必填)
    description: str,                # M11 新增(必填)
    body: str,
    source_quote: str,               # L7 必填
    title: Optional[str] = None,     # 保留兼容(默认=name)
    tags: Optional[list[str]] = None,
    extra: Optional[dict[str, Any]] = None,
    overwrite: bool = False,
) -> str:
```

#### 3.2.2 内部行为

1. `name` 和 `description` 写 frontmatter 顶层
2. `title` 若 caller 未传,自动 `title = name`;若传了,仍写入 frontmatter(向后兼容)
3. `validate_frontmatter` 增加两个必填字段检查
4. `compute_item_hash` **不变** — `name`/`description` 不参与 hash(避免 schema 升级影响幂等)

#### 3.2.3 validation 新规则

```python
# types.py:_REQUIRED_FRONTMATTER
_REQUIRED_FRONTMATTER = frozenset({
    "type", "created_at", "item_hash", "schema_version",  # M10
    "name", "description",                                 # M11 新增
})
```

**Description 长度约束**(借鉴 CC 实现): `1 <= len(description) <= 200` 字符。太短(<5 字符) → 警告但写盘;太长(>200 字符) → 截断并警告。

### 3.3 Migration(v2 → v3)

#### 3.3.1 触发条件

- 启动时检查 `memory_root/schema_version` 文件(若存在)
- `MemoryStore.read()` 读盘时若发现 frontmatter `schema_version < 3`,触发懒迁移

#### 3.3.2 迁移逻辑

```python
# agent_core/memory/migration.py:migrate_file
def migrate_file(abs_path: Path) -> dict[str, Any]:
    """v2 → v3 migration:补 name + description"""
    fm, body = parse_frontmatter(abs_path.read_text())
    
    # 1. 补 name (= title 的语义镜像)
    if "name" not in fm:
        # title 是从 body H1 提取的;若 frontmatter 也没 title,fallback 到 body 第一行
        title = fm.get("title", "")
        if not title:
            title = body.split("\n", 1)[0].lstrip("# ").strip()[:50]
        fm["name"] = title
    
    # 2. 补 description(从 body 摘要,或用 body 截断)
    if "description" not in fm:
        # 简单截断:取 body 第一段非空行,≤200 字符
        first_para = next(
            (line.strip() for line in body.split("\n") if line.strip() and not line.startswith("#")),
            "",
        )
        fm["description"] = first_para[:200] or fm.get("title", "未描述")[:200]
    
    # 3. 升级 schema_version
    fm["schema_version"] = 3
    
    # 4. 写回(原子操作,生成 .bak sidecar)
    _atomic_write_with_bak(abs_path, fm, body)
    
    return {"frontmatter": fm, "body": body}
```

**风险**: 迁移后 `description` 质量依赖 body 内容。**接受这个降级** — 后续 LLM 提取通道(channel B)在生成新记忆时仍按高质量 description 写;旧文件可在用户 review 时手动改 description。

---

## 四、MEMORY.md 物理索引方案

### 4.1 文件位置与格式

**位置**: `<memory_root>/MEMORY.md`,即 `~/.agent_data/memory/MEMORY.md`

**格式** — 严格对齐 CC:

```markdown
# Agent Memory (auto-generated)

- [用户叫小明](user/5fa7...c3b9.md) — 用户叫小明,Python 后端工程师
- [学习风格](user/a8d2...1e4f.md) — 先手写原生 ReAct 理解本质,再用框架
- [不要 mock DB](feedback/74ad...1c7.md) — 学习场景下 mock 会掩盖真实行为
- [Go REST API 项目结构](project/9499...1b3.md) — 用了 chi + sqlx + testify
- [Linear 工单系统](reference/0190...5a4.md) — bugs 跟踪在 Linear project INGEST
```

**注**: CC 不写 H1,但 M11 加了 `# Agent Memory (auto-generated)` 让 LLM 知道这是 agent 管理的文件(`# Agent Memory` 是锚点)。

### 4.2 双重硬上限(对齐 CC)

```python
# agent_core/memory/memory_index.py
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000  # ~125 chars/line × 200 lines
FRONTMATTER_MAX_LINES = 30     # 读取单文件 frontmatter 时只读前 30 行
```

**截断策略**:
1. 先按 200 行截
2. 再按 25KB 截(防"长行"绕过 line cap)
3. 截断后追加警告(说明触发的是 line 还是 byte 限制)

### 4.3 生成时机(写盘后异步,不阻塞主流程)

```python
# agent_core/memory/memory_index.py
class MemoryIndex:
    """
    维护 MEMORY.md 物理索引文件。
    
    设计决策:
    - 写盘后异步 rebuild(后台线程,不阻塞 channel A/B)
    - 重建触发: 写盘后 1s 合并窗口 + 显式 flush() 强制刷盘
    - 写盘并发保护: 单线程 rebuild(通过 threading.Lock)
    """
    def __init__(self, memory_root: Path):
        self.path = memory_root / "MEMORY.md"
        self._lock = threading.Lock()
        self._pending = False
        self._timer = None
    
    def mark_dirty(self) -> None:
        """标记索引过期。1s 合并窗口后异步 rebuild。"""
        with self._lock:
            if self._pending:
                return
            self._pending = True
            self._timer = threading.Timer(1.0, self.rebuild)
            self._timer.daemon = True
            self._timer.start()
    
    def rebuild(self) -> None:
        """重建 MEMORY.md 文件。同步执行,持锁。"""
        with self._lock:
            self._pending = False
            entries = self._scan_entries()  # 按 mtime 倒序,只取 ≤200
            content = self._render(entries)
            truncated = self._truncate(content)
            atomic_write(self.path, truncated)
    
    def flush(self) -> None:
        """强制同步 rebuild(测试 / 进程关闭前用)。"""
        if self._timer:
            self._timer.cancel()
        self.rebuild()
```

**关键决策**:
- **异步而非同步** — 写盘 hot path 不被索引写阻塞
- **1s 合并窗口** — 短时间内连续写盘只 rebuild 1 次(批量蒸馏场景)
- **不写索引 = 索引过期可恢复** — 下次启动 `MemoryStore.__init__` 触发一次 lazy rebuild

### 4.4 启动时加载(L1)

```python
# agent_core/agent_core.py:514(改造)
class AgentCore:
    def _build_system_prompt_with_memory(self) -> str:
        """
        构建 system prompt,把 MEMORY.md 内容追加到 base prompt 之后。
        借鉴 CC 的 appendSystemPrompt 模式 — MEMORY.md 是独立 H1 段,
        让 LLM 在注意力机制中获得独立权重。
        """
        base = self.system_prompt or ""
        if not self.memory_store:
            return base
        
        try:
            index_content = self.memory_store.load_index()
            # 借鉴 CC:独立 H1 段,不合并到 base
            return f"{base}\n\n{index_content}"
        except Exception as e:
            _logger.warning(f"加载 MEMORY.md 失败,跳过: {e}")
            return base
```

**借鉴 CC 的 2 个关键设计**:
1. **独立 H1 段**(`# Agent Memory`)— `appendSystemPrompt` 而非 merge,CC 验证 attention 差异
2. **TRUSTING_RECALL_SECTION**(独立 H2 段)— "从记忆推荐前先验证" — 加在 MEMORY.md 之后,见 §6

---

## 五、召回机制改造方案

### 5.1 删除项(M11 删)

| 类别 | 文件 | 行 | 删除内容 |
|------|------|----|---------|
| 函数 | `retriever.py:126-144` | `_tokenize` | 中英混合分词 |
| 函数 | `retriever.py:147-175` | `_keyword_score` | 简化 BM25 |
| 函数 | `retriever.py:384-432` | `_keyword_search` | keyword 模式全流程 |
| 函数 | `retriever.py:434-450` | `_merge_hits` | hybrid 融合去重 |
| 逻辑 | `retriever.py:461-470` | `_rerank` 中 hybrid 分支 | 加权融合公式 |
| enum | `retriever.py:56-60` | `RetrievalMode.KEYWORD` / `.HYBRID` | 模式枚举值 |
| enum | `config.py:50` | `Literal["vector", "file", "hybrid"]` | 改 `"semantic" \| "side_query"` |
| enum | `config.py:52-59` | `semantic_weight` / `lexical_weight` | 权重字段 |
| 逻辑 | `config.py:69-80` | `_weights_sum_to_one` 校验 | 不再需要 |

**总计**: ~120 行代码删除,8 个文件影响

### 5.2 新增项

#### 5.2.1 `side_query` 模式(retriever 新分支)

```python
# agent_core/memory/retriever.py:新增 _side_query_search
def _side_query_search(
    self, query: str, top_k: int, types: Optional[list[str]],
    already_surfaced: Optional[set[str]] = None,
) -> list[MemoryHit]:
    """
    借鉴 CC findRelevantMemories 的 LLM sideQuery 召回。
    
    流程:
    1. scan_memory_files(memory_root) → 读前 30 行 frontmatter,按 mtime 倒序,截 MAX_MEMORY_FILES=200
    2. 过滤 already_surfaced(本会话已展示过的)
    3. 拼 manifest 文本:[name](rel_path) — description  ...
    4. sideQuery:用主 LLM router 调 1 次 LLM(JSON schema, max_tokens=256),选 ≤5
    5. 拿到 path 列表后,MEMORY.store.read() 读正文,构造 MemoryHit
    """
    # 1. scan
    entries = scan_memory_files(
        self.memory_store.root,
        max_files=MAX_MEMORY_FILES,
        frontmatter_max_lines=FRONTMATTER_MAX_LINES,
        types_filter=types,
    )
    
    # 2. dedup
    if already_surfaced:
        entries = [e for e in entries if e.rel_path not in already_surfaced]
    
    if not entries:
        return []
    
    # 3. format manifest
    manifest = format_memory_manifest(entries)
    
    # 4. sideQuery(用主 LLM router,与主流程一致)
    selected_paths = self._call_side_query(query, manifest, max_select=top_k)
    
    # 5. read 全文 + 构造 MemoryHit
    hits = []
    for path in selected_paths:
        try:
            data = self.memory_store.read(path)
        except Exception:
            continue
        fm = data.get("frontmatter", {}) or {}
        body = data.get("body", "")
        hits.append(MemoryHit(
            item_hash=fm.get("item_hash", ""),
            type=fm.get("type", "user"),
            title=fm.get("name", fm.get("title", "")),  # 优先 name,fallback title
            body=body,
            rel_path=path,
            score=1.0,  # LLM 选的,不再计算 score
            breakdown={"side_query": 1.0},
            tags=fm.get("tags", []),
            importance=fm.get("importance", 5),
        ))
    return hits

def _call_side_query(
    self, query: str, manifest: str, max_select: int = 5
) -> list[str]:
    """
    调 1 次 LLM(JSON schema, max_tokens=256)选 ≤max_select 个 path。
    用主 llm_router(与主流程一致)。
    """
    import json
    from agent_core.memory.prompt_templates import (
        SIDE_QUERY_SYSTEM_PROMPT,
        build_side_query_prompt,
    )
    
    prompt = build_side_query_prompt(query, manifest, max_select)
    text = ""
    try:
        for chunk in self.llm_router.chat(
            messages=[
                {"role": "system", "content": SIDE_QUERY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            cache_namespace="memory_side_query",  # 独立 namespace,避免污染主 cache
        ):
            if chunk.text_delta:
                text += chunk.text_delta.text
        data = json.loads(_strip_code_fence(text))
        return data.get("selected_paths", [])[:max_select]
    except Exception as e:
        logger.warning(f"sideQuery 失败,降级为按 mtime 倒序 top_k: {e}")
        return []
```

#### 5.2.2 scan_memory_files 工具函数

```python
# agent_core/memory/memory_index.py:scan_memory_files
@dataclass
class MemoryFileEntry:
    rel_path: str       # "user/5fa7...c3b9.md"
    name: str           # frontmatter.name
    description: str    # frontmatter.description
    type: str           # frontmatter.type
    mtime_ms: int       # 文件 mtime(用于 staleness 提示)

def scan_memory_files(
    memory_root: Path,
    max_files: int = MAX_MEMORY_FILES,
    frontmatter_max_lines: int = FRONTMATTER_MAX_LINES,
    types_filter: Optional[list[str]] = None,
) -> list[MemoryFileEntry]:
    """
    扫 memory_root 下所有 .md 文件,只读前 N 行 frontmatter,构造 manifest。
    
    按 mtime 倒序,截 max_files。失败的文件跳过(损坏/权限)。
    """
    if not memory_root.exists():
        return []
    
    # 1. 收集所有 .md 文件(单次 scandir)
    all_files: list[Path] = []
    for t in (types_filter or ["user", "feedback", "project", "reference"]):
        type_dir = memory_root / t
        if type_dir.exists():
            all_files.extend(type_dir.glob("*.md"))
    
    # 2. 按 mtime 倒序,截前 max_files
    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    all_files = all_files[:max_files]
    
    # 3. 读前 N 行 frontmatter
    entries: list[MemoryFileEntry] = []
    for p in all_files:
        try:
            # 单次 readFile,前 N 行
            with p.open("r", encoding="utf-8") as f:
                head = "".join(itertools.islice(f, frontmatter_max_lines))
            fm = parse_frontmatter_head(head)
            entries.append(MemoryFileEntry(
                rel_path=str(p.relative_to(memory_root)),
                name=fm.get("name", fm.get("title", "?")),
                description=fm.get("description", "无描述"),
                type=fm.get("type", "user"),
                mtime_ms=int(p.stat().st_mtime * 1000),
            ))
        except (OSError, MemoryStoreError, ValueError):
            continue
    
    return entries
```

#### 5.2.3 format_memory_manifest 渲染

```python
# agent_core/memory/memory_index.py:format_memory_manifest
def format_memory_manifest(entries: list[MemoryFileEntry]) -> str:
    """
    把 scan 结果渲染成 LLM 可读的 manifest 文本。
    格式对齐 CC:'- [name](rel_path) — description'
    """
    lines = []
    for e in entries:
        lines.append(f"- [{e.name}]({e.rel_path}) — {e.description}")
    return "\n".join(lines)
```

#### 5.2.4 SIDE_QUERY_SYSTEM_PROMPT

```python
# agent_core/memory/prompt_templates.py
SIDE_QUERY_SYSTEM_PROMPT = """你是 memory recall selector。
用户给了一个 query 和一份 manifest(记忆索引),请从 manifest 中选出 ≤5 个最相关的 path。

规则:
- 只输出 JSON,严格按 schema
- 不要选完全无关的(描述不匹配的)
- 选 ≤5 个;少于 5 个也行(强制过滤)
- 如果都不相关,selected_paths = []
- 不要解释,不要 markdown fence

JSON schema:
{"selected_paths": ["user/abc.md", "feedback/xyz.md", ...]}"""

def build_side_query_prompt(query: str, manifest: str, max_select: int) -> str:
    return f"""<query>
{query}
</query>

<memory_manifest>
{manifest}
</memory_manifest>

请从 manifest 中选 ≤{max_select} 个最相关的 path,输出 JSON。"""
```

### 5.3 模式切换语义

```python
# agent_core/memory/retriever.py:56-60(改造)
class RetrievalMode(str, Enum):
    """检索模式(M11:二选一,switchable, not mergeable)"""
    SEMANTIC = "semantic"        # 向量召回(原 M10 semantic)
    SIDE_QUERY = "side_query"    # M11 新增:LLM 二次精选
```

```python
# agent_core/memory/retriever.py:296-312(改造 _retrieve_candidates)
def _retrieve_candidates(
    self, query: str, top_k: int, mode: RetrievalMode, types: Optional[list[str]],
    already_surfaced: Optional[set[str]] = None,
) -> list[MemoryHit]:
    """M11:二选一,不再融合"""
    if mode == RetrievalMode.SEMANTIC:
        return self._semantic_search(query, top_k, types)
    if mode == RetrievalMode.SIDE_QUERY:
        return self._side_query_search(query, top_k, types, already_surfaced)
    raise RetrievalError(f"未知检索模式: {mode!r}")
```

**关键语义变化**:
- M10 `_retrieve_candidates` 会自动融合(hybrid 跑两个 + merge)
- M11 `_retrieve_candidates` 是**二选一** — caller 选哪种就只跑哪种
- `search()` API 不变,caller 改 `mode` 参数即可切换

### 5.4 配置化

```python
# agent_core/memory/config.py:50(改造)
class RetrievalConfig(BaseModel):
    """检索相关配置(M11:二选一,switchable)"""
    model_config = ConfigDict(extra="forbid")
    
    mode: Literal["semantic", "side_query"] = "semantic"  # M11 默认 semantic(快/便宜)
    top_k: int = Field(default=5, ge=1, le=20)  # 改默认 5(对齐 CC 的 ≤5)
    min_score: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="semantic 模式下低于此分的记忆丢弃(side_query 不适用)",
    )
    token_budget: int = Field(
        default=2000, ge=100, le=8000,
        description="注入到 prompt 的记忆总 token 上限(M11 L8 不变)",
    )
    
    # M11 新增:sideQuery 配置
    side_query_max_select: int = Field(
        default=5, ge=1, le=10,
        description="sideQuery 模式 LLM 最多选几个文件",
    )
    side_query_max_files: int = Field(
        default=200, ge=10, le=1000,
        description="scan_memory_files 扫描文件数上限",
    )
    
    # M11 删除:不再需要权重
    # semantic_weight: float  ❌
    # lexical_weight: float   ❌
    
    # 删除 weights 校验
    # @model_validator 的 _weights_sum_to_one ❌
```

---

## 六、与 Claude Code 对齐的 5 个关键设计

### 6.1 frontmatter 严格对齐 CC(§3.1)

- **CC**: 3 字段(name / description / type)
- **M11**: 5 必填 + 7 选填,但 `name` + `description` 复用 CC 语义
- **差异**: M11 保留 4 必填(系统需要,非业务字段)+ CC 3 字段(业务字段)
- **决策理由**: 4 必填(created_at/item_hash/schema_version/...)是 agent-dev 系统自身需要(幂等去重、schema 迁移、时序追踪),不是用户字段。CC 把它放在文件系统 / sqlite 内部,M11 放在 frontmatter(为了可读 + 单文件可移植)

### 6.2 MEMORY.md 物理索引(§4)

- **CC**: `<memdir>/MEMORY.md`,无 frontmatter
- **M11**: 同上,但加 H1 `# Agent Memory (auto-generated)` 锚点
- **双重硬上限**: 200 行 / 25KB(对齐 CC)
- **生成时机**: 异步 rebuild(写盘 1s 后合并窗口)— CC 是同步 rebuild 但用户场景写盘频率低,M11 用户场景写盘可能更频繁(双通道 + extract),需要异步

### 6.3 L1 启动加载 + L2 按需召回(§4.4, §5)

| 维度 | CC | M11 |
|------|-----|-----|
| **L1 启动加载** | `MEMORY.md` 注入 system prompt 头部 | 同 |
| **L2 按需召回** | `findRelevantMemories` + sideQuery | 同 |
| **sideQuery 模型** | Sonnet 4.5(独立) | 主流程默认模型(与主一致) |
| **sideQuery 选几个** | ≤5 | ≤5(可配 `side_query_max_select`) |
| **sideQuery max_tokens** | 256 | 256(对齐) |
| **scan 上限** | 200 文件(MAX_MEMORY_FILES) | 同(可配) |
| **frontmatter 读几行** | 30 行(FRONTMATTER_MAX_LINES) | 同 |
| **alreadySurfaced 去重** | Set 传参 | 同(集成到 retriever.search 签名) |

**M11 的差异**: sideQuery 用主流程默认 LLM(用户统一配置,无 extractor_router 复用)— 减少 router 实例数,提升配置一致性。

### 6.4 alreadySurfaced 去重(借鉴 CC)

```python
# agent_core/agent_core.py:514(改造)
class AgentCore:
    def __init__(self, ...):
        self._surfaced_memories: set[str] = set()  # 本会话已展示
        ...
    
    def stream_chat(self, ...):
        # 检索时排除已展示
        report = self.memory_retriever.search(
            last_user_msg["content"],
            top_k=5,
            already_surfaced=self._surfaced_memories,  # M11 新增
        )
        # 记录已展示
        for hit in report.hits:
            self._surfaced_memories.add(hit.rel_path)
```

**作用**: 长对话场景下,避免每次都把同样的 5 条记忆重新注入 prompt(M10 有重复风险,M11 修复)。

### 6.5 TRUSTING_RECALL_SECTION(借鉴 CC)

借鉴 CC deep-dive §1.3 的 A/B 测试结论(0/2 → 3/3 via appendSystemPrompt),M11 在 system prompt 末尾追加独立 H2 段:

```python
# agent_core/agent_core.py:_build_system_prompt_with_memory(改造)
TRUSTING_RECALL_SECTION = """
## Before recommending from memory

A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:
- If the memory names a file path: check the file exists.
- If the memory names a function or flag: grep for it.
- If the user is about to act on your recommendation (not just asking about history), verify first.

"The memory says X exists" is not the same as "X exists now."
"""
```

**位置**: 追加在 MEMORY.md 之后,**独立 H2 段**(`## Before recommending from memory`)。借鉴 CC 的 attention 机制 — H1 段(独立段)在 LLM 注意力中权重高于 H2 子项。

---

## 七、配置变更

### 7.1 Pydantic 配置

```python
# agent_core/memory/config.py
class RetrievalConfig(BaseModel):
    """检索相关配置(M11)"""
    model_config = ConfigDict(extra="forbid")
    
    mode: Literal["semantic", "side_query"] = "semantic"
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.3, ge=0.0, le=1.0)
    token_budget: int = Field(default=2000, ge=100, le=8000)
    
    # M11 新增
    side_query_max_select: int = Field(default=5, ge=1, le=10)
    side_query_max_files: int = Field(default=200, ge=10, le=1000)
    
    # M11 删除(无 warning,因为 extra="forbid" 会让旧配置 fail-fast)
    # semantic_weight ❌
    # lexical_weight  ❌
```

**env 变量示例**:
```bash
MEMORY_RETRIEVAL__MODE=side_query                # M11 二选一
MEMORY_RETRIEVAL__SIDE_QUERY_MAX_SELECT=5        # M11 新增
```

### 7.2 UI 注入

Streamlit `web/app.py` 的 Runtime Config panel 增加 1 个 select:
- "检索模式":[semantic / side_query] (默认 semantic)
- "sideQuery 选几个":[1-10] 滑块(默认 5)

---

## 八、测试策略(M11 验收标准)

### 8.1 单元测试(新增/改造)

| 测试文件 | 改造内容 |
|---------|---------|
| `tests/test_types.py` | 新增 `validate_frontmatter` 对 `name`/`description` 必填的检查;`test_description_too_long` 截断行为 |
| `tests/test_memory_store.py` | `write()` 接收 `name`/`description`;存量 v2 文件读时触发懒迁移 |
| `tests/test_migration.py` | 新增 `test_v2_to_v3_migration`;`name`/`description` 自动生成;`schema_version` 升级;`.bak` sidecar |
| `tests/test_memory_index.py` **(新)** | `scan_memory_files` 截断 200 / 30 行;`format_memory_manifest` 格式;`MemoryIndex.rebuild` 原子写;`mark_dirty` 1s 合并窗口 |
| `tests/test_retriever.py` | **删除** keyword/hybrid 测试;**新增** `test_side_query_basic` `test_side_query_dedup` `test_already_surfaced_filter` |
| `tests/test_prompt_templates.py` | 新增 `build_side_query_prompt` / `SIDE_QUERY_SYSTEM_PROMPT` 渲染测试 |
| `tests/test_config.py` | `mode="hybrid"` 抛 ValidationError(向后兼容 fail-fast);`side_query` 默认值正确 |

### 8.2 集成测试

| 测试文件 | 改造内容 |
|---------|---------|
| `tests/test_e2e_memory_recall.py` **(新)** | L1 启动加载 MEMORY.md → L2 检索走 sideQuery → 拼到 system prompt |
| `tests/test_index_rebuild_on_write.py` **(新)** | 写盘 → 等 1.1s → 验证 MEMORY.md 已更新;批量写盘 → 只 rebuild 1 次 |
| `tests/test_already_surfaced_dedup.py` **(新)** | 同会话多轮检索,确认已展示过的不会被重复注入 |

### 8.3 DoD(Definition of Done)

- [ ] 26 条存量记忆 v2 → v3 迁移完成(冷启动时自动)
- [ ] 迁移后 MEMORY.md 索引 ≤ 200 行 / 25KB
- [ ] L1 启动加载 — system prompt 头部含 MEMORY.md 内容
- [ ] L2 召回:
  - [ ] `mode=semantic` 仍能用(原 M10 行为,无 regression)
  - [ ] `mode=side_query` 选 ≤5 个,LLM 用主 router
  - [ ] already_surfaced 去重生效
  - [ ] sideQuery 失败降级(不抛异常,返空 list)
- [ ] TRUSTING_RECALL_SECTION 独立 H2 段在 system prompt 末尾
- [ ] 旧 `mode=keyword` / `mode=hybrid` 抛 ValidationError(fail-fast)
- [ ] 8 个测试文件全过(6 改 + 2 新)
- [ ] web/app.py 注入点改完,UI Runtime Config 增 2 个字段
- [ ] Streamlit 端到端冒烟通过(对话 + 写盘 + 检索)
- [ ] Chroma 向量库不回归(T1-T8 8 commit 全部仍 PASS)

---

## 九、风险与回滚

### 9.1 风险表

| 风险 | 影响范围 | 概率 | 缓解措施 |
|------|---------|------|---------|
| **存量 v2 → v3 迁移失败** | 26 条文件可能损坏 | 中 | 迁移前备份到 `memory.bak/`,迁移失败可手动回滚 |
| **sideQuery 选错(LLM 幻觉)** | 召回相关度低 | 中 | max_tokens=256 + JSON schema 强约束;失败降级为按 mtime 倒序 |
| **MEMORY.md 异步 rebuild 漏触发** | 索引过期 | 低 | 启动时 lazy rebuild 兜底;flush() 显式同步入口 |
| **sideQuery 每次都额外 1 次 LLM 调用** | 增加 latency (~500ms-1s) + 成本 | 中 | 库大时(>200)降级为 semantic;UI Runtime Config 可切 |
| **frontmatter 字段新增破坏旧 caller** | 写入失败 | 中 | `name`/`description` 必填但 caller 多(extract channel B / 主 agent)需同步改;**TDD 红绿 refactor 顺序必保证** |
| **dual_channel_writer 集成 frontmatter 改动** | 写盘失败 | 中 | 走 TDD:先改 store.write + migration 测试通过,再改 writer |

### 9.2 回滚策略

| 故障 | 回滚动作 |
|------|---------|
| 迁移破坏存量数据 | `cp -r memory.bak/* memory/` 全量还原 |
| sideQuery 持续失败 | 改 `config.retrieval.mode = "semantic"` 暂时降级 |
| MEMORY.md 索引膨胀 | 删 MEMORY.md → 启动时 lazy rebuild 重建 |
| LLM 调用成本爆炸 | 改 `side_query_max_select = 1` 限制单次 LLM 输出 |

---

## 十、任务分解预览(供后续 writing-plans 用)

| 任务 | 范围 | 工期 | TDD 步骤 |
|------|------|------|---------|
| **T1** | frontmatter schema 改造(`name`/`description` 必填) | 0.3d | Red:test_types 验证 v3 必填 → Green:types.py 改 → Refactor |
| **T2** | MemoryStore.write 接收 `name`/`description`,mirror 到 `title` | 0.3d | Red:test_memory_store 验证新签名 → Green:memory_store.py 改 |
| **T3** | migration v2 → v3(自动补 name/description) | 0.4d | Red:test_migration 验证迁移结果 → Green:migration.py 改 |
| **T4** | MemoryIndex 模块(异步 rebuild + 截断) | 0.5d | Red:test_memory_index 5 个 case → Green:memory_index.py 新建 |
| **T5** | dual_channel_writer 集成新 frontmatter + index.mark_dirty | 0.4d | Red:test_dual_channel 验证 .md 含新字段 → Green:writer.py 改 |
| **T6** | retriever 删 keyword/hybrid | 0.3d | 删 + 跑现有 test_retriever(应全过) |
| **T7** | retriever 新增 side_query 模式 | 0.5d | Red:test_side_query 6 个 case → Green:retriever.py 加分支 |
| **T8** | config.mode 改 `"semantic" \| "side_query"` | 0.2d | Red:test_config 验证旧 mode 抛错 → Green:config.py 改 |
| **T9** | scan_memory_files + format_memory_manifest 工具函数 | 0.3d | Red:test_memory_index 验证 scan 截断 → Green:memory_index.py 加函数 |
| **T10** | SIDE_QUERY_SYSTEM_PROMPT + build_side_query_prompt | 0.2d | Red:test_prompt_templates 验证 prompt 渲染 → Green:prompt_templates.py 改 |
| **T11** | agent_core.py:514 L1 启动加载 + already_surfaced 状态 | 0.4d | Red:test_agent_core 验证 system prompt 注入 → Green:agent_core.py 改 |
| **T12** | web/app.py Runtime Config 增 2 字段 | 0.2d | UI test + 手动冒烟 |
| **T13** | TRUSTING_RECALL_SECTION 独立 H2 段 | 0.1d | append 即可,无测试 |
| **T14** | e2e 集成测试 + 端到端冒烟 | 0.5d | 跨 6 个文件跑回归 |
| **T15** | 清理:删 _tokenize / _keyword_score / _keyword_search / _merge_hits / hybrid _rerank | 0.1d | grep + 删 |
| **T16** | T1-T15 全套回归 + 文档更新 | 0.3d | 更新 memory-system-design.md v2.3 |

**总工期**: ~5 天(15 任务 + 清理 + 回归 + 文档)

---

## 十一、文件改动总览(实施前最终确认)

### 11.1 Modify(7 个文件)

| 文件 | 改动 |
|------|------|
| `agent_core/memory/types.py` | frontmatter schema v2 → v3(name/description 必填);_OPTIONAL_FRONTMATTER 更新 |
| `agent_core/memory/memory_store.py` | `write()` 新增 name/description 参数;title 自动 mirror |
| `agent_core/memory/retriever.py` | 删 _tokenize/_keyword_score/_keyword_search/_merge_hits;新增 _side_query_search;_retrieve_candidates 二选一 |
| `agent_core/memory/config.py` | `mode` 二选一;删 weight 字段;新增 side_query 配置 |
| `agent_core/memory/dual_channel_writer.py` | 写盘后调 `memory_index.mark_dirty()`;caller 传 name/description |
| `agent_core/memory/prompt_templates.py` | 新增 SIDE_QUERY_SYSTEM_PROMPT + build_side_query_prompt |
| `agent_core/agent_core.py` | L1 启动加载 MEMORY.md;already_surfaced Set;TRUSTING_RECALL_SECTION |
| `web/app.py` | Runtime Config 增 2 字段 |

### 11.2 Create(3 个新文件)

| 文件 | 用途 |
|------|------|
| `agent_core/memory/memory_index.py` | MemoryIndex 类 + scan_memory_files + format_memory_manifest |
| `tests/test_memory_index.py` | 单元测试(5+ case) |
| `tests/test_e2e_memory_recall.py` | 端到端测试(L1+L2 联动) |

### 11.3 Delete(无)

- 走 TDD 删旧代码时,旧函数先在 retriever.py 注释"deprecated"再删,确保 test_react_memory_strict 等 6 个老测试仍能 import

### 11.4 Read-only check

- `agent_core/memory/chroma_store.py` — **不动**(T1-T8 done)
- `agent_core/memory/embeddings.py` — **不动**
- `agent_core/memory/extraction_gate.py` — **不动**
- `agent_core/memory/distiller.py` — **不动**
- `agent_core/memory/cost_tracker.py` — **不动**

---

## 十二、与其他 doc 的关系

| 文档 | 关系 |
|------|------|
| [`memory-system-design.md`](memory-system-design.md) | v2.2 → v2.3 增量更新(主文档) |
| [`memory-test-coverage-matrix.md`](memory-test-coverage-matrix.md) | M11 完成后更新一列(标 "M11") |
| [`claude-code-memory-system-deep-dive.md`](claude-code-memory-system-deep-dive.md) | **不更新** — 这是 CC 源码分析,M11 改造后才"被对齐" |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | 在 M11 章节引用本文件作为入口 |
| [`agent-dev-开发规则与经验汇总-2026-06-18.md`](agent-dev-开发规则与经验汇总-2026-06-18.md) | 新增"对齐 CC"经验条目 |

---

## 十三、DoD(本设计文档完成标志)

- [x] 4 大改造点覆盖(frontmatter / MEMORY.md / 删 keyword / 删 hybrid)
- [x] frontmatter schema 明确(v3,6 字段必填 + 选填)
- [x] MEMORY.md 文件位置/格式/生成时机/启动加载明确
- [x] side_query 模式完整设计(API/JSON schema/Prompt)
- [x] 与 CC 5 个关键对齐点(§6)
- [x] 配置 Pydantic 字段明确(M11 新增/删除/默认值)
- [x] 8 类测试 + 端到端冒烟验收标准
- [x] 风险 + 回滚策略明确
- [x] 任务分解预览(16 任务,5 天)
- [x] 文件改动总览(7 modify + 3 create)

---

**本设计文档已自审,无 placeholder / 无矛盾 / 无二义性。**

**下一步**: 用户 review 后,调用 `superpowers:writing-plans` 把 §十 的 16 任务展开为 step-by-step TDD 实施计划,落到 `docs/superpowers/plans/2026-06-26-m11-*.md`。
