# 记忆系统测试覆盖矩阵

> **用途**：盘点 `docs/memory-system-design.md` (v2.3) 中描述的所有功能,与 `tests/` 下实际测试做交叉对比,**识别无人守门的设计不变量与高风险漏测路径**。
>
> **日期**：2026-06-26
> **口径**：以 2026-06-26 当天代码为准(branch: `feature/fork-compact`)
> **关联文档**:
> - 设计文档: [`memory-system-design.md`](memory-system-design.md)
> - M11 计划: `superpowers/plans/2026-06-26-m11-frontmatter-memory-md-align.md`
> - 测试文件: `tests/test_*.py` + `tests/e2e/test_*.py`

## 〇、v2.3 (M11) 增量覆盖

| 功能 | 测试 | 状态 |
|------|------|------|
| frontmatter schema v3 (`name`/`description` 必填) | `test_types_config.py::TestSchemaV3*` | ✅ |
| `MemoryStore.write` 接收 `name`/`description` + fallback | `test_memory_store.py::test_write_v3_*` | ✅ |
| v2 → v3 migration (`_v2_to_v3`) | `test_migration.py::test_v2_to_v3_*` | ✅ |
| `MemoryIndex` rebuild / `MEMORY.md` 200 行上限 | `test_memory_index.py::test_memory_index_max_200_lines` | ✅ |
| `mark_dirty` 1s 异步 coalesce | `test_memory_index.py::test_mark_dirty_1s_coalesce` | ✅ |
| `scan_memory_files` mtime desc / types_filter | `test_memory_index.py::test_scan_*` | ✅ |
| `format_memory_manifest` yaml-aware (quoted description) | `test_memory_index.py::test_scan_quoted_description` | ✅ |
| `RetrievalConfig.mode = semantic / side_query` (Pydantic Literal) | `test_types_config.py::TestRetrievalConfig*` | ✅ |
| 旧 `keyword` / `hybrid` / `vector` / `file` 模式必抛 `ValidationError` | `test_types_config.py::test_invalid_modes_rejected` | ✅ |
| `RetrievalConfig` extra='forbid' (删 `semantic_weight`/`lexical_weight`) | `test_types_config.py::test_extra_fields_forbidden` | ✅ |
| Retriever 旧 mode 运行时必抛 `RetrievalError` | `test_retriever.py::test_keyword_mode_rejected` / `test_hybrid_mode_rejected` | ✅ |
| Retriever sideQuery 模式 + LLM 选 path + 读全文 | `test_retriever.py::test_side_query_basic` | ✅ |
| sideQuery `already_surfaced` 过滤 | `test_retriever.py::test_side_query_already_surfaced_filter` | ✅ |
| sideQuery LLM 失败降级返空 | `test_retriever.py::test_side_query_failure_returns_empty` | ✅ |
| sideQuery 无 llm_router 降级返空 | `test_retriever.py::test_side_query_no_llm_router_returns_empty` | ✅ |
| sideQuery `cache_namespace="memory_side_query"` 隔离 | `test_retriever.py::test_side_query_uses_cache_namespace` | ✅ |
| `SIDE_QUERY_SYSTEM_PROMPT` / `build_side_query_prompt` | `test_prompt_templates.py::TestSideQueryPrompt*` | ✅ |
| `ReactAgent.memory_index` L1 启动加载 (有 store / 无 store) | `test_agent_core.py::test_agent_core_creates_memory_index_*` / `test_no_memory_index_*` | ✅ |
| `_build_system_prompt_with_memory` 拼接 base + MEMORY.md + TRUSTING_RECALL_SECTION | `test_agent_core.py::test_build_system_prompt_*` | ✅ |
| `_surfaced_memories` set 累加 + 跨轮注入 `already_surfaced` | `test_agent_core.py::test_surfaced_memories_accumulate` / `test_already_surfaced_passed_to_retriever` | ✅ |
| `TRUSTING_RECALL_SECTION` 始终出现在 system prompt 末尾 | `test_agent_core.py::test_*_trust_section_*` | ✅ |
| `DualChannelWriter` 写盘后触发 `mark_dirty` | `test_dual_channel_concurrent.py` | ✅ |
| 端到端 L1 启动加载 → L2 sideQuery 召回 | `test_e2e_memory_recall.py::test_e2e_l1_loads_*` / `test_e2e_l2_*` | ✅ |
| 端到端 already_surfaced round-trip | `test_e2e_memory_recall.py::test_e2e_l1_l2_already_surfaced_round_trip` | ✅ |
| 端到端写盘后 1.1s 内 MEMORY.md 更新 | `test_e2e_memory_recall.py::test_e2e_write_triggers_index_rebuild` | ✅ |
| Web Runtime Config 加 mode select + side_query_max_select slider | 手动冒烟(无 Streamlit 单测) | 🟡 |



---

## 〇、使用说明

### 〇.1 状态定义

| 状态 | 含义 | 行动建议 |
|------|------|---------|
| ✅ **覆盖** | 至少 1 个 test case 命中此功能点 | 维护,跟随设计演进 |
| 🟡 **部分** | 有测试但仅 happy path 或仅部分维度(类型/边界/异常) | 优先补缺 |
| ❌ **无测** | 完全无对应测试 | 必须补 |
| ⚠️ **设计未实现** | 文档有描述但代码无对应实体 | 决策:删除/实现 |

### 〇.2 优先级定义

| 优先级 | 含义 | 触达条件 |
|--------|------|---------|
| **P0** | 核心不变量 / 数据丢失风险 / 启动阻塞 | 违反即系统失效 |
| **P1** | 重要功能未测,生产可能踩雷 | 偶发但成本高 |
| **P2** | 锦上添花 / 边界 / 可观测性 | 调试友好 |
| **P3** | 文档规划但代码未落地 | 决策后再说 |

---

## 一、测试覆盖总览(按章节)

| 章节 | 功能数 | ✅ 覆盖 | 🟡 部分 | ❌ 无测 | 覆盖度 |
|------|-------|---------|----------|----------|--------|
| §3 触发机制 | 7 | 4 | 2 | 1 | ~71% |
| §4 双通道+压缩 | 11 | 4 | 3 | 4 | ~50% |
| §5 存储 | 8 | 3 | 1 | 4 | ~44% |
| §6 检索 | 9 | 5 | 1 | 3 | ~61% |
| §7 蒸馏 | 9 | 7 | 1 | 1 | ~83% |
| §8 文件结构 | - | - | - | - | (设计/无需测) |
| §9 依赖与嵌入 | 4 | 2 | 1 | 1 | ~63% |
| §12 配置 | 4 | 2 | 1 | 1 | ~63% |
| §13 UI 可观测性 | 7 | 1 | 1 | 5 | ~21% |
| §14 安全 | 6 | 2 | 1 | 3 | ~42% |
| §15 决策 | - | - | - | - | (文档) |
| **总计** | **65** | **30** | **12** | **23** | **~55%** |

> ⚠️ **总体评估**:覆盖度约 55%,其中 P0 级别功能完整覆盖约 65%,**P0 中仍有 4 条核心不变量无人守门**(见 §二)。

---

## 二、无人守门的设计不变量(🔴 必修)

这 5 条是 `memory-system-design.md §4.5` 明确列为"安全网"的不变量,但**没有任何 test case 验证它们**。一旦违反,系统行为即静默漂移。

| # | 不变量内容 | 文档位置 | 现状 | 风险 |
|---|----------|---------|------|------|
| **N1** | `feedback` / `project` 类记忆必须含 `**Why:**` 段 | §5.3.2 / §4.5 #7 | ❌ `MemoryEditor.validate_why_required` 完全无测试 | 用户硬规则被无声丢弃,后期召回看到"裸规则无原因",违反文档承诺的"rules without reasons decay fast" |
| **N2** | 提取 LLM 的 prompt **不含**已有记忆(去重下沉到写盘前) | §3.3 / §4.5 #8 / §6.9 | ✅ `test_prompt_templates.test_build_extract_prompt_no_existing_memories_block` + `test_react_memory_strict.test_extract_prompt_has_no_existing_memories_block` 已守门 | — |
| **N3** | 语义去重**绝不阻断**持久化(向量/LLM 异常 → 返回 False) | §4.5 #9 / §6.9.4 | ❌ happy path 已测,异常路径**未测** | 一次 Chroma 抖动就可能让用户的所有新记忆丢失 |
| **N4** | SM 文件**永不被重新生成**,只通过 Edit 增量更新 | §4.5 #1 / §4.3 | 🟡 `test_sm_layer.py` 有但未明确测"不应被覆盖" | 有人手贱覆写 SM 文件,后续 compact 信息全丢 |
| **N5** | `compact` **不调 LLM**(只读 SM + 截断) | §4.5 #2 / §4.3 | ❌ `compact()` 本身被测,但**"调用过程中无 LLM 调用"**未被断言 | 后续重构误加 LLM 调用,文档承诺被打破,延迟从 100ms 变 5s+ |

**修复优先级**:N1 = N3 > N4 = N5 (按"违反即数据/性能损失"排序)

---

## 三、§3 触发机制详细覆盖

| 设计点 | 文档位置 | 测试用例 | 状态 | 缺口 |
|--------|---------|---------|------|------|
| 通道 A 触发(用户说"记住") | §3.2 / §4.1 | `test_react_memory_strict.test_channel_a_writes_daily_log` | ✅ | — |
| 通道 B 触发(token ≥ 10K 或 tool ≥ 10) | §3.2 / §4.1 | `test_extraction_gate.test_above_10k_no_keyword_enters_gate3` | ✅ | — |
| 关键词命中触发(门2) | §3.3.1 | `test_extraction_gate.test_below_10k_with_keyword_enters_gate3` | ✅ | — |
| 16 个关键词清单完整性 | §3.3.1 | `test_extraction_gate.test_keyword_list_has_16_items` | ✅ | — |
| 门3 LLM 评分 ≥ 0.6 → 提交 | §3.3.1 | `test_extraction_gate.test_high_confidence_extracts` / `test_low_confidence_skips` | ✅ | — |
| 门1 触发后**清零累计** | §3.3.1 修订 | `test_react_memory_strict.test_gate1_clears_counter_after_extract` | ✅ | — |
| 门2 触发后**不清零累计** | §3.3.1 修订 | `test_react_memory_strict.test_gate2_does_not_clear_counter` | ✅ | — |
| gate 决策 reasoning 输出 | §3.3.1 | 无独立 case | ❌ | `Decision.reason` 字段未被断言(已间接走通) |
| LLM 解析失败容错 | §3.3 | `test_extraction_gate.test_parse_error_logs_and_skips` / `test_markdown_code_fence_stripped` / `test_markdown_fence_without_json_lang` | ✅ | — |
| 关键词触发但置信度 < 0.6 时的累计行为 | §3.3.1 修订 | 部分覆盖 | 🟡 | 修订后"门1 触发后 < 0.6 不清零"**需单独 case** |
| Gate 触发的可观测性(metrics / 日志) | §13.5 | 无 | ❌ | `extract_total` 计数器无单测 |

---

## 四、§4 双通道+压缩详细覆盖

### 4.1 通道 A / B

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| 通道 A 写 daily log + 推 daily_cursor | `test_channel_a_writes_daily_log` | ✅ | — |
| 通道 B 跑批 + 写 memory 文件 | `test_dual_channel_concurrent.TestChannelBMultiCandidate` | ✅ | — |
| 跨子进程 flock 串行化 | `test_dual_channel_concurrent.test_two_subprocs_serialized_by_flock` | ✅ | — |
| Channel B 多候选全部写盘 | `test_all_candidates_written_not_just_first` | ✅ | — |
| Channel B cursor 推进只推进一次 | `test_channel_b_second_call_processes_zero_after_cursor_advance` | ✅ | — |
| Channel B 日志输出(info + debug) | `test_channel_b_emits_info_logs` | ✅ | — |
| 跨进程 IPC 锁互斥(A4) | `test_two_subprocs_serialized_by_flock` | ✅ | — |
| Cursor 持久化到 MetaDB(A3) | `test_pending_recovery.*` + `test_react_memory_bridge.test_second_turn_persists_when_run_local_turn_index_resets` | ✅ | — |
| Pending recovery(崩溃后重启) | `test_pending_recovery.TestBFixRecoverPendingRetries` | ✅ | — |
| `_extraction_in_progress` 60s 超时(A10) | `test_dual_channel_concurrent.test_stuck_extraction_force_reset_by_watchdog` | ✅ | — |
| `atexit` graceful_shutdown(A9) | 无 | ❌ | 进程退出时 in-flight 任务丢失 |
| WAL JSONL fsync(防 turn 丢失) | 隐式通过 happy path | 🟡 | 显式测 fsync 行为 |
| Channel A 与 SessionManager 的 .jsonl 隔离 | 无 | ❌ | 两条 jsonl 路径冲突场景未测 |

### 4.2 通道 B 关键路径(并发与崩溃)

| 场景 | 测试用例 | 状态 | 缺口 |
|------|---------|------|------|
| 进程内双线程并发 | `test_dual_channel_concurrent` 多个 | ✅ | — |
| 进程内 channel_a 幂等检查 | `test_pending_recovery.TestBug1FixChannelAAutoTurnIndex` | ✅ | — |
| 进程内 channel_b cursor 不重复推 | `test_pending_recovery.TestBug2FixRecoveryDoesNotAdvanceCursor` | ✅ | — |
| 全流程集成 | `test_pending_recovery.TestIntegrationFullCycle` | ✅ | — |
| Race fix attempts 计数 | `test_pending_recovery.TestCFixAttemptsIncrement` | ✅ | — |
| Race fix 异常日志 | `test_pending_recovery.TestDFixExceptionLogging` | ✅ | — |
| 崩溃后增量恢复(只补跑未完成部分) | `TestBug12Integration` / `TestIntegrationFullCycle` | ✅ | — |

### 4.3 SM 层 + Compact

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| SM 文件不存在/模板态/正常态 | `TestSMFileState` (5 case) | ✅ | — |
| 触发条件 token / tool_count | `TestTrigger` (3 case) | ✅ | — |
| 5 条回退条件 | `TestFallbackConditions` (≥3 case) | ✅ | — |
| Compact 截断长 section | `TestCompact.test_compact_truncates_long_sections` | ✅ | — |
| Compact 返回 None(无 SM / 模板) | `test_compact_returns_none_when_no_sm` / `test_compact_returns_none_when_template` | ✅ | — |
| Extract 初始化 SM | `test_extract_initializes_sm_if_missing` | ✅ | — |
| Extract 推进 last_id | `test_extract_with_no_callback_advances_last_id` | ✅ | — |
| **compact 不调 LLM**(不变量 #2) | 无显式断言 | ❌ | **N5** |
| **SM 永不被重新生成**(不变量 #1) | 无显式断言 | ❌ | **N4** |
| SM 增量更新 vs 全量替换区分 | `test_extract_skips_when_no_new_messages` | 🟡 | 增量语义需更显式 |

---

## 五、§5 存储详细覆盖

### 5.1 Layer 1 (daily log)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| `DailyLogger.log` 写一行 | `test_react_memory_strict.test_channel_a_writes_daily_log`(隐式) | 🟡 | 没有 `DailyLogger` 直接单测 |
| fsync 防丢失 | 无 | ❌ | WAL 强制落盘行为未显式测 |
| 日志格式 / frontmatter | 无 | ❌ | 路径/格式未锁定 |

### 5.2 Layer 2 (向量索引 + metadata)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| `MemoryStore.write` 基础 | `test_retriever.populated` fixture | ✅ | — |
| 4 类封闭 type 校验 | `test_channel_b_rejects_unknown_type` / `test_types_config` 系列 | ✅ | — |
| `list_by_session` | `test_memory_store_list_by_session` (2 case) | ✅ | — |
| 空 tags Chroma 异常 | `test_chroma_empty_tags` | ✅ | — |
| 重复 id 写入幂等性 | `test_idempotency`(types_config) | ✅ | — |
| frontmatter 解析 | `test_types_config.TestValidateFrontmatter` | ✅ | — |
| 实际 Chroma 重启持久化 | 无 | ❌ | Chroma 落盘后 reload 行为未测 |
| `update_access` (access_count + 1) | `test_retriever` 隐式 | 🟡 | 单独断言需要 |
| `confidence` 时间衰减 | 无 | ❌ | §5.2 schema 字段,衰减逻辑无单测 |

### 5.3 Layer 3 (per-file 长期记忆)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| MEMORY.md 索引生成 | `test_memory_editor._rebuild_index` (隐式) | 🟡 | 单独测 index 重建逻辑 |
| `feedback` / `project` **必含 Why** | 无 | ❌ | **N1** |
| Schema 演进(`MigrationRegistry`) | 无 | ❌ | **migration.py 整个模块零测试** |
| `_v0_to_v1` / `_v1_to_v2` | 无 | ❌ | 真实用户升级路径 |
| sidecar 缓存 `.md.migrated.json` | 无 | ❌ | 懒迁移机制无保护 |
| 启动时全表扫策略 | 无 | ❌ | 文档承诺"懒迁移"无测试 |
| `_candidate/` 写盘 | `test_distillation_loop_status_and_candidate_path` | ✅ | — |
| `_archive/` 移动 | 隐式 | 🟡 | 单独测 |
| Path validator 4 层 | `test_path_validator_in_write`(4 case) | ✅ | — |
| Path validator: NFC trick / symlink 逃逸 | `test_write_unicode_null_in_type_blocked_by_path_validator` 部分 | 🟡 | 完整的 L1-L4 4 层独立边界未各测 |
| Path validator: 符号链接攻击 | 无 | ❌ | `realpath` 防软链未单独测 |

---

## 六、§6 检索详细覆盖

### 6.1 基础检索

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| keyword 模式 | `test_keyword_search_finds_relevant` | ✅ | — |
| semantic 模式 | `test_semantic_search_finds_relevant` | ✅ | — |
| hybrid 模式 | `test_hybrid_search_finds_relevant` / `test_hybrid_uses_keyword_when_vec_empty` | ✅ | — |
| 三模式之外的 invalid_mode | `test_invalid_mode_raises` | ✅ | — |
| top_k 限制 | `test_top_k_limits_results` | ✅ | — |
| 空 query / 空库 | `test_empty_query_returns_empty` / `test_whitespace_query_returns_empty` | ✅ | — |
| 类型过滤 | `TestTypeFilter` (≥3 case) | ✅ | — |
| Secret 标记过滤 | `TestSecretFilter` (≥3 case) | ✅ | — |
| 排序按分数降序 | `test_results_sorted_by_score_desc` | ✅ | — |
| `get_by_hash` | `TestGetByHash` (≥2 case) | ✅ | — |
| Report metadata | `TestReport` (≥4 case) | ✅ | — |
| 三模式报告 breakdown | `test_breakdown_populated` | ✅ | — |

### 6.2 文件模式 + Hybrid 模式 (§6.6)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| **file-only 模式(读 MEMORY.md + LLM 选)** | 无 | ❌ | §6.6 文件路径实现完全无测 |
| **hybrid(向量粗筛 → LLM 精排)** | `test_hybrid_*` 已覆盖模式触发 | 🟡 | **LLM 精排的精度提升**未量化 |
| A/B 测试 `vector_only` vs `file_only` vs `hybrid` | 无 | ❌ | §6.6.5 Jaccard 对比无测 |

### 6.3 Token budget + inject mode (§6.4)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| `inject_mode=summary` 截断 | 无显式 | 🟡 | summary 模式字段未被断言 |
| `inject_mode=full` 全文注入 | 无显式 | 🟡 | full 模式字段未被断言 |
| `max_injection_tokens` 超限截断 | 无 | ❌ | 重要不变量无保护 |
| 0 命中时的可观测性 | `test_empty_query_returns_empty` 隐式 | 🟡 | `zero_hit_total` metric 需断言 |

### 6.4 Prompt Cache (§6.7)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| `cache_safe_params` 行为 | 无 | ❌ | §6.7 整个机制无测 |
| `cache_namespace` 跨调用复用 | 无 | ❌ | §6.8 契约无单测 |
| `cache_namespace` 不同隔离 | 无 | ❌ | — |
| cache hit rate 测量 | 无 | ❌ | §6.7 / §13.6 量化指标 |

### 6.5 LLM Integration Contract (§6.8)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| `LLMUsage` 字段透传 | 无 | ❌ | cache_read/cache_creation 字段无断言 |
| `cache_namespace` 行为契约 | 无 | ❌ | §6.8 给的示例未落地 |
| `trace_span_name` 透传 | 无 | ❌ | OTel 桥接未单测 |

### 6.6 语义去重 (§6.9)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| 三层决策边界(>=/<) | `test_decide_action_three_bands` | ✅ | — |
| Auto threshold 跳过 | `test_auto_duplicate_skips_without_llm` | ✅ | — |
| Judge band 调 LLM | `test_judge_band_*` (2 case) | ✅ | — |
| Judge band LLM 失败返回 False | `test_llm_judge_failure_returns_false` | ✅ | — |
| Judge band LLM 拒绝则写盘 | `test_judge_band_writes_when_not_duplicate` | ✅ | — |
| Low similarity 写盘 | `test_low_similarity_writes_without_llm` | ✅ | — |
| dedup 关 → 全写 | `test_dedup_disabled_writes_everything` | ✅ | — |
| Code fence 解析 | `test_llm_judge_parses_false_and_handles_code_fence` | ✅ | — |
| **vector query 异常不阻断** | 无 | ❌ | **N3** |
| **LLM 异常不阻断(放行)** | `test_llm_judge_failure_returns_false` 部分覆盖 | 🟡 | 异常路径仅在 LLM 返回非 JSON 时测,网络/超时未测 |
| item_hash 幂等兜底(同 source_quote 第二次) | 隐式通过 `is_pending_written` | 🟡 | 显式断言需要 |
| Debug 日志(`memory.dedup` / `memory.dual_channel`) | `test_channel_b_emits_info_logs` 部分 | 🟡 | 完整 debug 链路需 caplog 断言 |

---

## 七、§7 蒸馏详细覆盖

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| 锁正常获取/释放 | `test_concurrent_acquire_only_one_wins` / `test_double_acquire_same_process` | ✅ | — |
| Stale PID 强占 | `test_acquire_lock_succeeds_when_holder_pid_dead` / `test_stale_pid_recoverable` | ✅ | — |
| Stale mtime 强占 | `test_acquire_lock_succeeds_when_mtime_exceeds_stale_threshold` / `test_stale_mtime_recoverable` | ✅ | — |
| Lock envelope (PID+host+started_at+schema_version) | `test_envelope_round_trip` / `test_garbage_rejected` / `test_missing_envelope` | ✅ | — |
| 门0 gate_disabled | `test_gate_disabled` | ✅ | — |
| 门1 too_soon(无锁/有锁) | `test_gate_too_soon_no_lock` / `test_gate_too_soon_with_fresh_lock` | ✅ | — |
| 门2 few_sessions | `test_gate_few_sessions` | ✅ | — |
| 门3 ok | `test_gate_ok` | ✅ | — |
| 门4 busy wins | `test_gate_busy_wins_over_too_soon` | ✅ | — |
| Fresh lock busy | `test_fresh_lock_busy` | ✅ | — |
| 蒸馏成功推进 mtime | `test_success_advances_mtime` | ✅ | — |
| 蒸馏失败回滚 mtime | `test_run_failure_rolls_back_last_distill_mtime` / `test_failure_preserves_prior_mtime` | ✅ | — |
| 蒸馏 dry_run 不写 | `test_dry_run_skips_write` | ✅ | — |
| 无 LLM skip | `test_run_with_no_llm_skipped` | ✅ | — |
| Distill 写 candidate 目录 | `test_write_candidates_to_candidate_dir` / `test_distill_returns_candidates` | ✅ | — |
| Distill 跳过已决定 | `test_c43_run_id_and_c44_review_feedback.test_scheduler_run_skips_decided_candidates` | ✅ | — |
| Scheduler run_id 透传 | `test_scheduler_run_passes_run_id_when_writing` | ✅ | — |
| Distill accept/edit/reject/skip | `test_candidate_review_actions` (8 case) + `test_c43_*` 系列 | ✅ | — |
| Accept 后 distill 跳过 end-to-end | `test_accept_then_distill_skips_accepted_end_to_end` | ✅ | — |
| Lock 过期并发 | `test_concurrent_lock_skip_returns_empty_report` | ✅ | — |
| **用户可拒绝候选** | `test_reject_candidate_records_decision_in_meta_db` | ✅ | — |
| Distiller callback 注入 | `test_distiller_accepts_callback` / `test_extract_with_callback_invokes_llm` | ✅ | — |
| 蒸馏生成 candidate 后 UI review → atomic replace | 隐式(accept 系列) | 🟡 | 完整 UI → atomic 流程无 e2e |

---

## 八、§9 依赖与嵌入详细覆盖

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| bge-m3 编码 | `TestBGEM3EmbedFn` (≥3 case) | ✅ | — |
| MiniLM fallback | `TestMiniLMEmbedFn` (≥2 case) | ✅ | — |
| `make_embed_fn` 工厂 | `TestFactory` | ✅ | — |
| 模型版本不一致检测 | 无 | ❌ | `embedding_model_version` 字段无单测 |
| 模型加载失败回退 | 无 | ❌ | `EmbeddingError` 抛出条件未测 |
| Chroma 持久化重启 | 无 | ❌ | `ChromaVectorStore` 真实 round-trip 无测 |

---

## 九、§12 配置详细覆盖

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| Pydantic 校验 | `test_types_config.TestMemoryConfig` / `TestRetrievalConfig` / `TestPathValidator` | ✅ | — |
| 所有子 config 默认值可构造 | `test_all_subconfigs_default_constructible` | ✅ | — |
| from_dict 基础/缺字段/非法 type | `test_from_dict_basic/missing_type/invalid_type/with_tags` | ✅ | — |
| 跨字段校验(weight 之和 = 1.0) | 隐式 | 🟡 | 显式断言 |
| 运行时切换(`config.set("retrieval.mode", "file")`) | 无 | ❌ | §12.3 动态切换无测 |
| 环境变量紧急开关 `DISABLE_AUTO_MEMORY=1` | 无 | ❌ | §12.2 总开关未测 |

---

## 十、§13 UI 与可观测性详细覆盖

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| 记忆系统 span 注入 | `test_tracing_spans.test_retriever_search_emits_memory_search_span` | ✅ | — |
| Extract gate span | `test_extraction_gate_emits_memory_extract_gate_span` | ✅ | — |
| SM compact span | `test_sm_layer_compact_emits_span` | ✅ | — |
| **memory.extract span** | 无 | ❌ | §13.6 关键 span 未测 |
| **memory.dream span** | 无 | ❌ | 蒸馏链路追踪未测 |
| **memory.sm_compact span** | `test_sm_layer_compact_emits_span` | 🟡 | 仅 compact span,未覆盖 fallback span |
| Cache hit rate 测量 | 无 | ❌ | §6.7 指标未测 |
| 成本预算触发降级 | `test_cost_tracker_check_budget_returns_exception_when_exceeded` / `test_extraction_gate_raises_budget_exceeded` | ✅ | — |
| 成本预算触发降级到 `mode=file` | 无 | ❌ | §13.7 自动降级无测 |
| 延迟预算告警(extract > 5s 触发丢弃) | 无 | ❌ | §13.8 延迟预算告警无测 |
| **e2e 状态面板渲染** | `tests/e2e/test_02_chat_page.*` 隐式 | 🟡 | 显式断言面板字段 |
| **e2e 候选 review UI** | `tests/e2e/test_04_candidate_review.*` | ✅ | — |
| **e2e 端到端:对话触发记忆 → review 页可见** | `tests/e2e/test_05_chat_scenarios.test_chat_scenario` | ✅ | (parametrize scenario,需确认覆盖) |

---

## 十一、§14 安全详细覆盖

### 11.1 路径安全(4 层防御)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| L1 相对路径 | `test_write_valid_path_succeeds` / `test_write_invalid_type_blocked_by_path_validator` | ✅ | — |
| L1 null byte | `test_write_unicode_null_in_type_blocked_by_path_validator` | ✅ | — |
| L1 `..` 路径穿越 | `test_write_path_traversal_in_type_blocked_by_path_validator` | ✅ | — |
| L2 NFC 归一化 | 隐式 | 🟡 | 单独测 `unicodedata.normalize("NFC")` 边界 |
| L3 `realpath` 符号链接 | 无 | ❌ | symlink 逃逸未测 |
| L4 `is_within` separator 后缀 | 隐式 | 🟡 | Windows/macOS separator 行为需测 |
| 文件权限 0600/0700 | `test_write_sets_file_mode_0600` | ✅ | — |

### 11.2 工具沙箱(§14.2)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| Edit 工具白名单 | 无 | ❌ | `MemoryFileEditor.create_tool` 未测 |
| 类别非法拒收 | `test_channel_b_rejects_unknown_type` | ✅ | — |
| **feedback/project 必含 Why** | 无 | ❌ | **N1** |
| source_quote 必填(L7 防幻觉) | 无 | ❌ | §14.2 关键不变量 |
| **prompt injection 正则拦截** | 无 | ❌ | §14.2 5 种攻击模式未各测 |
| MemoryVerifier 二次校验 | 无 | ❌ | §14.2 异步校验无测 |
| 连续 3 次 suspicious 丢弃 | 无 | ❌ | — |

### 11.3 SecretScanner(§14.4)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| OpenAI sk- pattern | `TestOpenAIPattern` (≥3 case) | ✅ | — |
| Anthropic sk-ant- | `TestAnthropicPattern` | ✅ | — |
| GitHub PAT | `TestGitHubPattern` | ✅ | — |
| Placeholder 不命中 | `TestPlaceholders` (≥5 case) | ✅ | — |
| 边界长度 | `TestBoundary` | ✅ | — |
| 替换时保留前 N 位 | `test_hit_match_is_masked` | ✅ | — |
| **Channel B 写盘前调用 scanner** | `test_channel_b_sanitizes_secret_before_write` | ✅ | — |
| **retriever 召回时标记 has_secret** | `test_hit_with_secret_marked` / `test_clean_hit_not_marked` | ✅ | — |
| Placeholder 与真 key 区分(`sk-xxx`) | `TestPlaceholders` | ✅ | — |
| scanner 模块级默认 | `TestModuleLevel` / `test_get_default_scanner_singleton` | ✅ | — |
| `assert_clean` 抛 / pass | `TestModuleLevel.test_assert_clean_*` | ✅ | — |

### 11.4 Data Lifecycle(§14.5)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| `daily_backup` 创建目录 | `TestDailyBackup.test_backup_creates_directory_with_files` | ✅ | — |
| 同天幂等(不覆盖) | `test_backup_is_idempotent_same_day` | ✅ | — |
| `.bak` 文件跳过 | `test_backup_skips_bak_files` | ✅ | — |
| 滚动删除(7 天保留) | `test_backup_prunes_old` | ✅ | — |
| `restore_backup` 覆盖恢复 | `TestRestoreBackup.test_restore_overwrites_existing` | ✅ | — |
| 备份不存在报错 | `test_restore_missing_backup_raises` | ✅ | — |
| 日期格式错报错 | `test_restore_invalid_date_format_raises` | ✅ | — |
| 健康数据 is_healthy=True | `TestIntegrityCheck.test_healthy_data_passes` | ✅ | — |
| 损坏 frontmatter 检测 | `test_detects_broken_frontmatter` | ✅ | — |
| 老 schema_version 检测 | `test_detects_old_schema_version` | ✅ | — |
| 无 meta_db 不报错 | `test_no_meta_db_is_ok` | ✅ | — |
| 容量未超 noop | `TestCapacityGovern.test_under_threshold_is_noop` | ✅ | — |
| 容量超阈值按 importance 淘汰 | `test_over_threshold_prunes_by_importance_then_age` | ✅ | — |
| 容量超阈值 importance_min=0 全淘汰 | `test_over_threshold_no_importance_min_prunes_all` | ✅ | — |
| 不存在 root 空 report | `test_capacity_govern_handles_missing_root` | ✅ | — |
| `list_backups` 倒序 | `test_lists_sorted_desc` | ✅ | — |
| `list_backups` 不存在根 → 空 | `test_no_backup_root_returns_empty` | ✅ | — |
| SQLite `PRAGMA integrity_check` 失败 raise | 无 | ❌ | 健康度真出问题(数据库损坏)的测试 |

### 11.5 文件权限(§14.3)

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| 写入时设置 0600 | `test_write_sets_file_mode_0600` | ✅ | — |
| 目录 0700 | 无 | ❌ | 目录权限未被断言 |

---

## 十二、§3.3.1 三门决策树修订详细覆盖

| 设计点 | 测试用例 | 状态 | 缺口 |
|--------|---------|------|------|
| 门1(10K/token) **不**要求关键词 | `test_above_10k_no_keyword_enters_gate3` | ✅ | — |
| 门2(关键词) **不**要求 token | `test_below_10k_with_keyword_enters_gate3` | ✅ | — |
| 门3 评分 ≥ 0.6 → 提交 | `test_high_confidence_extracts` | ✅ | — |
| 门3 评分 < 0.6 → skip | `test_low_confidence_skips` | ✅ | — |
| 门1 触发后清零累计 | `test_gate1_clears_counter_after_extract` | ✅ | — |
| 门2 触发后**不**清零 | `test_gate2_does_not_clear_counter` | ✅ | — |
| 跨 session 不累计 | 隐式 | 🟡 | 显式测 |
| **门1 触发后 < 0.6 修订后不清零** | 无 | ❌ | 文档 §3.3.1 修订条款明确测 |

---

## 十三、集成与端到端

| 测试文件 | 状态 | 覆盖 |
|---------|------|------|
| `tests/test_app_wiring.py` | ✅ | `web/app.py` 装配 sanity,7 case |
| `tests/test_react_memory_bridge.py` | ✅ | on_turn_end / gate 集成,4 case |
| `tests/test_react_memory_strict.py` | ✅ | M10 端到端,4 case |
| `tests/test_dual_channel_minimal.py` | ✅ | smoke |
| `tests/test_dual_channel_concurrent.py` | ✅ | 并发+崩溃 |
| `tests/test_pending_recovery.py` | ✅ | 崩溃恢复 |
| `tests/test_race_pending_extract.py` | ✅ | race fix |
| `tests/e2e/test_01_home_loads.py` | ✅ | 页面加载 |
| `tests/e2e/test_02_chat_page.py` | ✅ | chat UI |
| `tests/e2e/test_03_session_management.py` | ✅ | session UI |
| `tests/e2e/test_04_candidate_review.py` | ✅ | review UI |
| `tests/e2e/test_05_chat_scenarios.py` | ✅ | parametrize scenario(需查 param 数据) |

---

## 十四、e2e 场景覆盖详查(test_05_chat_scenarios)

> ⚠️ **以下数据需进一步验证**——`test_chat_scenario` 是 parametrize 模板,实际场景从 fixture/conftest 注入。

需要人工核对的:
- `chat_session` fixture 的具体行为(启 streamlit / 直接调函数)
- parametrize scenario 列表里**有没有**包含:
  - "用户说'记住 X'" → 期望 review 页可见
  - "累积 10K token" → 期望自动提取
  - "跨会话召回" → 期望 A 会话记忆 B 会话可见
  - "编辑记忆 → 重新召回" → 期望新内容
  - "删除记忆" → 期望下次召回无此条

(以上需打开 `tests/e2e/conftest.py` + `tests/e2e/data/` 目录确认)

---

## 十五、高优先级补测清单(按修复成本排序)

### P0 — 必须本周补

| # | 测试 | 目标 | 估计时间 |
|---|------|------|---------|
| 1 | `test_memory_editor.py` 新建,覆盖 `MemoryFileEditor.validate_why_required` | N1: Why 必填 | 1h |
| 2 | `test_dedup.py` 加 `test_vector_query_exception_does_not_block_write` / `test_llm_judge_timeout_returns_false` | N3: 不阻断 | 30min |
| 3 | `test_sm_layer.py` 加 `test_compact_does_not_call_llm`(mock 整个 router) | N5: compact 零 LLM | 30min |
| 4 | `test_sm_layer.py` 加 `test_sm_file_never_rewritten`(手动改 SM 后 extract 不覆盖) | N4: SM 永不被重新生成 | 30min |
| 5 | `test_migration.py` 新建,覆盖 `_v0_to_v1` / `_v1_to_v2` / `MigrationRegistry` 链式迁移 | §5.3.4 整章 | 2h |

### P1 — 应在两周内补

| # | 测试 | 目标 | 估计时间 |
|---|------|------|---------|
| 6 | `test_extraction_gate.py` 加 `test_gate1_low_confidence_does_not_clear_cumulative` | §3.3.1 修订条款 | 20min |
| 7 | `test_path_validator_in_write.py` 加 `test_symlink_escape_blocked` / `test_nfc_normalization_attack_blocked` | §14.1 L2/L3 边界 | 1h |
| 8 | `test_memory_editor.py` 加 `test_source_quote_required` / `test_prompt_injection_patterns_blocked`(5 种 regex 各一 case) | §14.2 L7/L9 | 1h |
| 9 | `test_retriever.py` 加 `test_max_injection_tokens_truncates` / `test_summary_mode_strips_body` | §6.4 token budget | 1h |
| 10 | `test_tracing_spans.py` 加 `test_dream_emits_memory_dream_span` / `test_extract_emits_memory_extract_span` | §13.6 缺测 span | 1h |
| 11 | `test_distiller.py` 加 `test_consecutive_three_suspicious_discards`(MemoryVerifier) | §14.2 L7 | 30min |
| 12 | `test_chroma_store.py` 新建,覆盖 `add/query/count` 真实 round-trip | §5.2 Chroma 持久化 | 2h |

### P2 — 季度内补

| # | 测试 | 目标 | 估计时间 |
|---|------|------|---------|
| 13 | `test_dedup.py` 加 `test_item_hash_dedup_fallback`(相同 source_quote 第二次跳过) | §6.9.4 兜底 | 30min |
| 14 | `test_runtime_config_switch.py` 加 `test_retrieval_mode_can_switch_at_runtime` | §12.3 动态切换 | 30min |
| 15 | `test_lifecycle.py` 加 `test_sqlite_corruption_raises` | §14.5 真损坏 | 30min |
| 16 | `test_app_wiring.py` 加 `test_disable_auto_memory_env_var` | §12.2 env 开关 | 20min |
| 17 | `test_cost_budget.py` 加 `test_over_budget_downgrades_to_file_mode` | §13.7 自动降级 | 1h |
| 18 | `test_retriever.py` 加 `test_jaccard_vector_vs_hybrid` | §6.6.5 A/B | 1h |
| 19 | `test_dual_channel_concurrent.py` 加 `test_graceful_shutdown_waits_for_inflight` | §4.1 A9 | 1h |
| 20 | `test_sm_layer.py` 加 `test_post_compact_token_exceeds_threshold_falls_back` | §4.4 第 5 回退条件 | 30min |

---

## 十六、覆盖率目标建议

| 阶段 | 目标 | 时间 |
|------|------|------|
| **2026-Q3 中** | P0 全部 ✅(消除 N1-N5 5 条无守门不变量) | 本月 |
| **2026-Q3 末** | P1 全部 ✅,总覆盖度 ≥ 75% | 下季度 |
| **2026-Q4 中** | P2 全部 ✅,总覆盖度 ≥ 85% | 季度中 |
| **长期** | 100% P0/P1,≥ 90% P2,与设计文档严格 1:1 | — |

---

## 十七、与其他文档关系

| 文档 | 关系 |
|------|------|
| [`memory-system-design.md`](memory-system-design.md) | **设计源头** — 本矩阵的"功能列表"完全从 design §1-§15 提取 |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | 实施时间表,与本矩阵的优先级对齐 |
| `tests/test_*.py` | **测试证据** — 本矩阵的"已覆盖"列直接 grep 自 test 文件 |

---

## 附录 A:测试文件统计

| 测试文件 | 用例数 | 覆盖模块 |
|---------|-------|---------|
| `test_dedup.py` | 12 | dedup 三层决策 + channel B 集成 |
| `test_extraction_gate.py` | 9 | gate 决策树 |
| `test_react_memory_strict.py` | 4 | M10 端到端 |
| `test_react_memory_bridge.py` | 4 | on_turn_end 集成 |
| `test_dual_channel_concurrent.py` | ~10 | 并发 + 锁 + watchdog |
| `test_pending_recovery.py` | ~8 | 崩溃恢复 |
| `test_race_pending_extract.py` | ~3 | race fix |
| `test_prompt_templates.py` | 4 | prompt 模板 |
| `test_distiller.py` | ~25 | 蒸馏核心 + 锁 |
| `test_scheduler.py` | ~10 | scheduler |
| `test_sm_layer.py` | ~22 | SM 层 + compact |
| `test_sm_layer_integration.py` | 4 | SM 集成 |
| `test_sm_persistence_and_distill.py` | 4 | SM 持久化 |
| `test_retriever.py` | ~20 | 检索 |
| `test_lifecycle.py` | 14 | lifecycle 完整 |
| `test_cold_start.py` | ~17 | seed 加载 |
| `test_secret_scanner.py` | ~24 | scanner 完整 |
| `test_path_validator_in_write.py` | 5 | 路径校验 |
| `test_channel_b_secret_sanitize.py` | 2 | channel B + secret |
| `test_channel_b_writes_session_id.py` | 1 | session_id 写入 |
| `test_cost_budget.py` | 4 | cost tracker |
| `test_embeddings.py` | ~8 | embedding 工厂 |
| `test_tracing_spans.py` | 3 | OTel spans |
| `test_app_wiring.py` | 7 | app 装配 |
| `test_types_config.py` | ~22 | Pydantic config |
| `test_candidate_review_actions.py` | 8 | candidate 4 动作 |
| `test_c43_run_id_and_c44_review_feedback.py` | 9 | run_id + review feedback |
| `test_logging_setup.py` | 4 | 日志配置 |
| **单元小计** | **~250** | — |
| `tests/e2e/test_*.py` | ~18 | Playwright e2e |
| **总计** | **~270** | — |

---

## 附录 B:本矩阵生成方法

```bash
# 1. 列出所有 test 文件 + 函数
grep -hE "^def test_|^class Test" tests/*.py tests/e2e/*.py

# 2. 列出所有 memory 模块
ls agent_core/memory/

# 3. 设计文档章节
grep -E "^## |^### " docs/memory-system-design.md
```

> **维护建议**:每次修改 design.md 的"功能点"列表时,同步更新本矩阵的"现状"列。每次新增 test case 时,同步勾掉对应行的 ❌/🟡。