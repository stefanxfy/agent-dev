---
description: "Task list for ReAct Inline XML Tool-Call Fallback Parser"
---

# Tasks: ReAct Inline XML Tool-Call Fallback Parser

**Input**: Design documents from `/specs/003-react-inline-xml-fallback-parser/`
- [spec.md](spec.md) — 4 user stories (P1, P2, P2, P3), 11 FRs, 6 SCs
- [plan.md](plan.md) — Technical Context, Constitution Check, structure
- [research.md](research.md) — 9 decisions resolved (parser module, scanner, integration point, config, logging)
- [data-model.md](data-model.md) — 5 entities, validation rules
- [contracts/inline-xml-format.md](contracts/inline-xml-format.md) — parser contract
- [quickstart.md](quickstart.md) — 8 validation scenarios

**Tests**: Tests are included — spec FRs/SCs demand verifiable behavior and Constitution Principle IV is NON-NEGOTIABLE.

**Organization**: Tasks grouped by user story so each story is independently implementable, testable, and deliverable.

**Reporting cadence** (per 反偷懒规则 第 4 条): after each phase, report 完成什么 / 测试结果 / 偏差说明.

## Format: `[ID] [P?] [Story] Description with file path`

- **[P]** = parallelizable (different files, no deps on incomplete tasks)
- **[US#]** = which user story this task belongs to (US1..US4)
- All file paths are absolute or relative to repo root.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project skeleton + test fixtures before any code.

- [x] T001 Create test fixture directory `tests/fixtures/inline_xml/`
- [x] T002 [P] Create 8 fixture JSONL files in `tests/fixtures/inline_xml/`: `01_single_block.jsonl`, `02_multiple_blocks.jsonl`, `03_malformed_inside_valid.jsonl`, `04_empty_markers.jsonl`, `05_nested_braces_in_input.jsonl`, `06_mixed_with_structured.jsonl`, `07_adjacent_other_xml.jsonl`, `08_all_providers.jsonl` (each file: realistic assistant text + expected `tool_calls` list + provider tag; see research.md §9)
- [x] T003 [P] Create new package `agent_core/react/` with empty `__init__.py`

**Checkpoint**: Repo structure ready; `python3 -m pytest -q` still passes (no new test files added yet).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Parser + config that ALL user stories depend on. No US work can start until this phase is complete.

- [x] T004 Implement parser module in `agent_core/react/inline_xml_parser.py`: `InlineToolCall` dataclass, `ParseOutcome` dataclass, hand-written byte scanner with balanced-brace matching + string-literal awareness, public `parse_inline_xml_tool_calls(text: str) -> ParseOutcome` function (per research.md §2 + data-model.md)
- [x] T005 [P] Add `InlineXmlFallbackConfig` pydantic model in `agent_core/config.py` (fields: `enabled: bool = True`, `log_provider_hash: bool = True`; per research.md §5)
- [x] T006 [P] Add top-level `inline_xml_fallback: InlineXmlFallbackConfig` field to `Config` in `agent_core/config.py` (with `Field(default_factory=InlineXmlFallbackConfig)`)
- [x] T007 Update `agent_core/react/__init__.py` to re-export `parse_inline_xml_tool_calls`, `InlineToolCall`, `ParseOutcome`, `InlineXmlFallbackConfig`

**Checkpoint**: `from agent_core.react import parse_inline_xml_tool_calls, InlineXmlFallbackConfig` works; `Config().inline_xml_fallback.enabled == True`. Existing 186 tests still pass.

---

## Phase 3: User Story 1 — Inline-XML tool call gets executed end-to-end (Priority: P1) 🎯 MVP

**Goal**: When an LLM emits inline-XML `<tool_call>` markers, the agent extracts them into structured `tool_calls` so the ReAct SM transitions to `EXECUTING_TOOLS` instead of `FINALIZING`.

**Independent Test**: Reproduce user-observed scenario — feed a fake LLM response with single inline-XML block into `ChunkParseHandler` + handler, assert `stage_outputs.tool_calls` length == 1, `stop_reason` == `"tool_use"`, synthetic `tool_use_id == "inline_xml_0"`.

### Tests for User Story 1 (write FIRST, verify they FAIL before T013/T014)

- [ ] T008 [P] [US1] Create `tests/test_inline_xml_parser.py::test_single_block_parses_to_one_tool_call` (uses fixture `01_single_block.jsonl`; asserts `len(tool_calls) == 1`, `tool_name == "Bash"`, `tool_input == {"command": "echo hi"}`, `tool_use_id == "inline_xml_0"`)
- [ ] T009 [P] [US1] Create `tests/test_inline_xml_parser.py::test_parser_produces_tool_call_delta_shape` (asserts returned `ToolCallDelta` has all 4 fields, `is_final == True`)
- [x] T010 [P] [US1] Create `tests/test_inline_xml_handler.py::test_handler_no_op_when_structured_calls_present` (asserts fallback NOT activated when `stage_outputs.tool_calls` is non-empty; fixture `06_mixed_with_structured.jsonl`)

### Implementation for User Story 1

- [x] T011 [US1] Implement `InlineXmlFallbackHandler` skeleton in `agent_core/turn_chain.py`: handler class with `name = "inline_xml_fallback"`, `__init__(self, agent)`, `handle(self, ctx)` reads `ctx.stage_outputs` and is a no-op (so T010 passes)
- [x] T012 [US1] Wire `InlineXmlFallbackHandler` immediately after `ChunkParseHandler` in llm_chain in `agent_core/builder.py` (insert into `agent._llm_chain` list)
- [x] T013 [US1] Add full fallback logic to `InlineXmlFallbackHandler.handle` in `agent_core/turn_chain.py`: if `stage_outputs.tool_calls` is empty AND `Config.inline_xml_fallback.enabled` is True, call `parse_inline_xml_tool_calls(stage_outputs.full_text)`, convert `InlineToolCall` → `ToolCallDelta` (synthetic id `inline_xml_{idx}`), append to `stage_outputs.tool_calls`, override `stage_outputs.stop_reason = "tool_use"` (per research.md §4)

**Checkpoint**: US1 fully testable. `python3 -m pytest -q tests/test_inline_xml_parser.py::test_single_block_parses_to_one_tool_call tests/test_inline_xml_handler.py::test_handler_no_op_when_structured_calls_present` exits 0.

---

## Phase 4: User Story 2 — Multiple inline-XML blocks in one response (Priority: P2)

**Goal**: Parser extracts all `<tool_call>` blocks in document order; handler appends all of them.

**Independent Test**: Feed fixture `02_multiple_blocks.jsonl` (3 blocks); assert `tool_calls` length == 3 in document order; assert no duplicates dropped.

### Tests for User Story 2

- [x] T014 [P] [US2] Create `tests/test_inline_xml_parser.py::test_multiple_blocks_parse_in_document_order` (fixture `02_multiple_blocks.jsonl`; asserts length == 3, IDs `inline_xml_0`/`1`/`2`, all three `tool_name` distinct, document order preserved)
- [x] T015 [P] [US2] Create `tests/test_inline_xml_parser.py::test_five_blocks_linear_scaling` (fixture: ad-hoc text with 5 blocks; asserts length == 5; documents the linear scaling)

### Implementation for User Story 2

- [x] T016 [US2] Extend `parse_inline_xml_parser` scanner to return all blocks in a single pass (already inherent to the design from T004 — verify by test, no code change if T004 scanner is correctly multi-block)

**Checkpoint**: US2 fully testable; no regression on US1 tests.

---

## Phase 5: User Story 3 — Malformed / partial inline-XML degrades safely (Priority: P2)

**Goal**: Invalid blocks (broken JSON, missing fields, wrong types, empty markers) are skipped with WARN; valid blocks still process; no crash; no silent drop.

**Independent Test**: Feed mixed valid + malformed; assert valid count, malformed indices captured, WARN log present, no exception.

### Tests for User Story 3

- [x] T017 [P] [US3] Create `tests/test_inline_xml_parser.py::test_malformed_inside_valid_skips_bad_keeps_good` (fixture `03_malformed_inside_valid.jsonl`; asserts `tool_calls` length == 1, `malformed_blocks == [1]`, no raise)
- [x] T018 [P] [US3] Create `tests/test_inline_xml_parser.py::test_empty_markers_skipped` (fixture `04_empty_markers.jsonl`; asserts `tool_calls` is empty, both block indices in `malformed_blocks`)
- [x] T019 [P] [US3] Create `tests/test_inline_xml_parser.py::test_only_malformed_blocks_returns_empty_with_warn` (asserts parser returns empty `tool_calls`, fallback activated == False if no valid blocks found, WARN logged)
- [x] T020 [P] [US3] Create `tests/test_inline_xml_parser.py::test_missing_required_field_rejected` (synthesized text with `<tool_call>{"name":"Bash"}</tool_call>` — no `input`; asserts malformed)
- [x] T021 [P] [US3] Create `tests/test_inline_xml_parser.py::test_wrong_type_for_input_rejected` (synthesized text with `input: "not an object"`; asserts malformed)

### Implementation for User Story 3

- [x] T022 [US3] Add malformed-block WARN logging in `parse_inline_xml_parser` in `agent_core/react/inline_xml_parser.py`: format `🧩 react.inline_xml_fallback: malformed block index=N offset=M (reason=<class_name>)`; uses sub-logger `agent_core.react.inline_xml_fallback`; **MUST NOT include input bytes**

**Checkpoint**: US3 fully testable; US1/US2 tests still pass; no input bytes leaked to logs.

---

## Phase 6: User Story 4 — Observable audit trail (Priority: P3)

**Goal**: One INFO marker per fallback activation, no input bytes, opt-out path via config, performance within budget.

**Independent Test**: Capture `caplog` during a fallback activation; assert exactly one INFO line, with required fields present and no input bytes; assert config-disabled path emits DEBUG not INFO and does not activate.

### Tests for User Story 4

- [x] T023 [P] [US4] Create `tests/test_inline_xml_handler.py::test_activation_logs_single_info_marker` (uses `caplog`; asserts exactly one INFO record from `agent_core.react.inline_xml_fallback`; format matches `🧩 react.inline_xml_fallback: provider=<hash> blocks=<int> first_tool=<name> original_stop=<stop>`)
- [x] T024 [P] [US4] Create `tests/test_inline_xml_log_audit.py::test_byte_equal_log_scan_no_input_bytes` (drives handler on fixture `02_multiple_blocks.jsonl` with input containing a sentinel string `"CANARY_SECRET_XYZ"`; scans captured log bytes; asserts CANARY substring absent — mirrors `scripts/verify_skill_secrets_audit.py` pattern from 002-skill-secret-injection SC-005)
- [x] T025 [P] [US4] Create `tests/test_inline_xml_handler.py::test_disabled_by_config_no_op_with_debug_log` (sets `cfg.inline_xml_fallback.enabled = False`; asserts handler is no-op, no INFO log, exactly one DEBUG log)
- [x] T026 [P] [US4] Create `tests/test_inline_xml_handler.py::test_single_block_under_5ms_benchmark` (1000-iter mean over fixture `01_single_block.jsonl`; asserts mean < 5 ms; per SC-005)

### Implementation for User Story 4

- [x] T027 [US4] Add INFO activation log marker to `InlineXmlFallbackHandler.handle` in `agent_core/turn_chain.py`: emit exactly one INFO line on activation (format per T023); never include input bytes (defensive: log `first_tool_name` only, never `tool_input`)
- [x] T028 [US4] Add config-disabled DEBUG path to `InlineXmlFallbackHandler.handle` in `agent_core/turn_chain.py`: when `cfg.inline_xml_fallback.enabled is False`, log DEBUG `🧩 react.inline_xml_fallback: disabled by config (inline_xml_fallback.enabled=False)` and return no-op
- [x] T029 [P] [US4] Create `tests/test_inline_xml_handler.py::test_handler_is_idempotent_on_second_pass` (calls handler twice on the same `stage_outputs`; asserts: `tool_calls` length unchanged after second call; no second INFO log emitted; no append-side-effect on second pass — covers FR-008)

**Checkpoint**: All 4 user stories fully testable; FR-008 idempotency verified. Byte-equal audit passes (no canary leak). Benchmark passes (< 5ms).

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Documentation sync + final regression gate + reporting.

- [x] T030 [P] Update `docs/agent-state-machine-and-chain-of-responsibility-design.md`: add a short section noting `InlineXmlFallbackHandler` as a new llm_chain handler, with rationale (parser fallback for inline-XML emission by some providers)
- [x] T031 [P] Update `docs/agent_core-skill-system-design.md` if any cross-reference to llm_chain handlers is needed (per Constitution III: doc sync gate)
- [x] T032 Final regression gate: run `python3 -m pytest -q` from repo root; assert exit code 0; report total test count (expected: 186 from 002 + ≥ 15 new from this branch)
- [x] T033 Verify quickstart.md scenarios 1-7 are all covered by the test suite (cross-check test IDs to scenario IDs); update quickstart.md if any scenario is uncovered
- [x] T034 Deliver user-facing 三件套 report (完成什么 / 测试结果 / 偏差说明 per 反偷懒规则 第 4 条)

**Checkpoint**: All user stories + polish complete; ready for commit + push.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3-6 (User Stories)**: All depend on Phase 2 completion
  - US1 → US2 → US3 → US4 can proceed in priority order
  - Or in parallel if team capacity allows (different test files, different fixtures)
- **Phase 7 (Polish)**: Depends on all user stories complete

### User Story Dependencies

- **US1 (P1)**: Depends on Phase 2 — independent of other stories
- **US2 (P2)**: Depends on US1 parser module (same `parse_inline_xml_tool_calls`); independent at handler level
- **US3 (P2)**: Depends on US1 parser module (WARN log is parser-internal); independent at handler level
- **US4 (P3)**: Depends on US1 handler (INFO log is handler-internal); independent of US2/US3

### Within Each User Story

- Tests written FIRST, verified to FAIL before implementation (TDD discipline per Constitution IV)
- Models / parser before handler
- Handler before wire-in
- Wire-in before integration test

### Parallel Opportunities

- **Phase 1**: T001, T002 (different files), T003 (different file) — all [P]
- **Phase 2**: T004, T005, T006 — T005/T006 [P] (both touch `config.py` but distinct fields); T004 [P] (different file)
- **Phase 3 tests**: T008, T009, T010 — all [P]
- **Phase 4 tests**: T014, T015 — [P]
- **Phase 5 tests**: T017, T018, T019, T020, T021 — all [P]
- **Phase 6 tests**: T023, T024, T025, T026 — all [P] (different test files)
- **Phase 7**: T030, T031 — [P] (different docs)

---

## Parallel Example: User Story 1

```bash
# Launch all US1 tests together (different test methods, same file but parametrize-safe):
T008 [P] [US1] tests/test_inline_xml_parser.py::test_single_block_parses_to_one_tool_call
T009 [P] [US1] tests/test_inline_xml_parser.py::test_parser_produces_tool_call_delta_shape
T010 [P] [US1] tests/test_inline_xml_handler.py::test_handler_no_op_when_structured_calls_present

# After tests written & failing, implement in order:
T011 [US1] InlineXmlFallbackHandler skeleton in agent_core/turn_chain.py
T012 [US1] Wire into llm_chain in agent_core/builder.py
T013 [US1] Full fallback logic in InlineXmlFallbackHandler.handle
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (Setup — fixtures + skeleton)
2. Complete Phase 2 (Foundational — parser + config)
3. Complete Phase 3 (US1 — single-block fallback activates)
4. **STOP and VALIDATE**: run US1 tests; reproduce user scenario end-to-end
5. If MVP sufficient for the user's reported issue (echo-skill + GLM-5.1), push & demo before continuing to US2-4

### Incremental Delivery

1. Setup + Foundational → foundation ready
2. + US1 → single-block case works (echo-skill passes!)
3. + US2 → multi-block case works (model can call parallel tools via inline-XML)
4. + US3 → defensive degradation (malformed blocks don't crash runs)
5. + US4 → observable + auditable (operators can grep for fallback activations)
6. + Phase 7 → docs in sync, full suite green, user-facing report

### Parallel Team Strategy

With one developer this can be done serially in ~4 hours. With multiple:
1. Team completes Phase 1 + 2 together (~30 min)
2. Developer A: US1 (parser + handler + wiring — ~90 min)
3. After US1: Developer B picks up US2 + US3 tests/impl in parallel (different fixtures), Developer A picks up US4

---

## Notes

- **[P]** tasks = different files OR different test methods; no shared mutable state
- **[US#]** label maps task to its user story for traceability in completion report
- Each user story is independently completable + testable
- Tests fail before implementation per TDD (Constitution IV)
- Commit after each logical group (one task or one story phase)
- Stop at any checkpoint to validate story independently
- **三件套 report** (per 反偷懒规则 第 4 条) after each phase

## Effort Summary

| Phase | Tasks | Estimated Effort |
|---|---|---|
| Phase 1: Setup | 3 | 30 min |
| Phase 2: Foundational | 4 | 90 min |
| Phase 3: US1 (P1) | 6 | 90 min |
| Phase 4: US2 (P2) | 3 | 20 min |
| Phase 5: US3 (P2) | 6 | 45 min |
| Phase 6: US4 (P3) | 7 | 50 min |
| Phase 7: Polish | 5 | 20 min |
| **Total** | **34** | **~5.5 hr** |

**Note**: 34 tasks is at the boundary of "6+ 步 plan" per 反偷懒规则 第 3 条. Splitting was deliberate — TDD demands one-test-per-task granularity to enforce the write-tests-first discipline.

## Done When

- [ ] All 34 tasks marked [X]
- [ ] Every user story has at least one passing test
- [ ] Full test suite exits 0 (≥ 205 tests = 186 from 002 + ≥ 19 new)
- [ ] Byte-equal log audit passes (no canary leak)
- [ ] Benchmark passes (< 5 ms single-block)
- [ ] Doc sync complete (T030 + T031)
- [ ] 三件套 report delivered (T034)