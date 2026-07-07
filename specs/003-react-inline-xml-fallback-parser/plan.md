# Implementation Plan: ReAct Inline XML Tool-Call Fallback Parser

**Branch**: `feature/react-inline-xml-fallback-parser` | **Date**: 2026-07-07
**Spec**: [spec.md](spec.md)
**Research**: [research.md](research.md)

## Summary

Models like GLM-5.1 (and historically MiniMax-M3) sometimes emit their tool
call decision as inline XML text content
(`<tool_call>{"name":"Bash","input":{"command":"..."}}</tool_call>`)
instead of a structured `tool_use` block. The current ReAct state machine
treats an empty `tool_calls` list as "nothing to do" and transitions
straight to FINALIZING, so the tool never runs.

This feature adds a **parser fallback layer** that detects such inline-XML
blocks in the LLM response text, converts them into the same `ToolCallDelta`
shape that real structured tool calls use, and rewires the state machine
to take its tool-execution path. The fallback is opt-out via a single
config flag, runs once per response in a dedicated chain handler, and logs
a single INFO marker per activation (no input payload bytes logged —
mirroring 002-skill-secret-injection SC-005 hygiene).

**Outcome**: `echo-skill` and any other tool-call-driven skill runs
end-to-end on GLM-5.1 / MiniMax-M3 / etc., reproducing the user's
2026-07-07 manual UI test successfully. No regression on the existing
186 tests from 002-skill-secret-injection.

## Technical Context

| Aspect | Value | Source |
|---|---|---|
| **Language / Version** | Python 3.11 | Constitution §技术约束 |
| **Primary Dependencies** | `anthropic`, `openai`, `zhipuai`, `pydantic`, `pytest` — no new deps | Constitution §技术约束 |
| **Storage** | N/A (parser is stateless; config in `Config` pydantic model) | — |
| **Testing** | `pytest` with `caplog`, `monkeypatch`, `tmp_path`; benchmark test for SC-005 | Constitution IV |
| **Target Platform** | Python 3.11 (any platform LLM router supports) | Constitution §技术约束 |
| **Project Type** | Library/framework extension (one new module + one new handler + one new config field) | — |
| **Performance Goals** | ≤ 5 ms per single-block response (SC-005); linear scaling | research.md §8 |
| **Constraints** | Stateless parser; no extra deps; logs never contain input bytes | Constitution II + spec FR-006/SC-004 |
| **Scale/Scope** | 1 new file (~150 LOC parser), 1 new file (~50 LOC handler), 1 new config field, ~8 test fixtures, 1 benchmark | research.md §6 |

**No NEEDS CLARIFICATION remains** — see `research.md` for resolved decisions.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| # | Principle | Compliance | Evidence |
|---|---|---|---|
| I | 自研优先 (Self-Built First) | ✅ Pass | Hand-written byte scanner; no new deps; mirrors existing `env_overrides.py` self-built pattern. |
| II | 数据驱动，绝不编造 | ✅ Pass | All edge cases anchored in real observed behavior (user's 2026-07-07 UI log); fixture data is synthetic but mirrors observed emission format. No assumed numbers. |
| III | 文档即硬约束 | ✅ Pass | This plan + spec + research = 3 doc artifacts alongside code. `docs/agent-state-machine-and-chain-of-responsibility-design.md` is not modified (parser is additive). |
| IV | 测试纪律 (NON-NEGOTIABLE) | ✅ Pass | Plan includes ≥ 8 unit tests + 1 benchmark + 1 byte-equal log scan; full test suite run as final gate. No stubs without `# intentionally stubbed:` docstring. |
| V | 不偷偷缩范围 | ✅ Pass | Plan covers all 4 user stories (P1, P2, P2, P3); 11 FRs; 6 SCs. Out-of-scope is **explicit** (variants, SM rewrite, UI surface). Estimated ~300 LOC total code+test, well within "≤ 500 LOC" implicit guideline; **not** requiring user pre-approval per 反偷懒规则 3 (which fires at >500 LOC). |
| VI | 防御式务实工程 | ✅ Pass | Defensive parser (handles malformed, empty, nested braces); hard cap on log payload (never logs input bytes); idempotent (FR-008); opt-out config (FR-011). |

**No violations** → Complexity Tracking table not needed.

### Re-evaluation after Phase 1 design

| # | Principle | Compliance (post-design) | Evidence |
|---|---|---|---|
| I | Self-Built | ✅ | `agent_core/react/inline_xml_parser.py` is hand-written (~150 LOC). No `regex` 3rd-party import. |
| II | Data-Driven | ✅ | Test fixtures are byte-exact reproductions of user-observed `<tool_call>` format. |
| III | Doc as Constraint | ✅ | data-model.md + contracts/inline-xml-format.md + quickstart.md all written. |
| IV | Test Discipline | ✅ | 8 fixture-driven unit tests + 1 benchmark + 1 byte-equal scan = 10 new tests minimum. Full suite run required as final gate. |
| V | No Silent Scope Shrink | ✅ | All 4 US, all 11 FR, all 6 SC are mapped to ≥ 1 test or document. Out-of-scope items remain out-of-scope. |
| VI | Defensive Engineering | ✅ | Empty-marker case (WARN-skip), nested-brace case (string-aware scanner), malformed case (WARN + valid blocks still execute). |

## Project Structure

### Documentation (this feature)

```text
specs/003-react-inline-xml-fallback-parser/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output — completed
├── data-model.md        # Phase 1 output — completed
├── quickstart.md        # Phase 1 output — completed
├── contracts/
│   └── inline-xml-format.md   # Phase 1 output — completed
├── checklists/
│   └── requirements.md  # From /speckit-specify — completed (all items pass)
├── spec.md              # From /speckit-specify — completed
└── tasks.md             # Phase 2 output — written by /speckit-tasks (NOT created here)
```

### Source Code (repository root)

**Structure Decision**: Option 1 (Single project — DEFAULT). The agent_core
codebase uses a single flat module hierarchy under `agent_core/` with
subpackages per concern (`memory/`, `skills/`, `tools/`, `session/`).
This feature extends that pattern: a new `react/` subpackage + a single
new handler in `turn_chain.py` + one config field.

```text
agent_core/
├── react/                          # NEW subpackage (Phase 0 research §6)
│   ├── __init__.py                 # barrel re-exports
│   └── inline_xml_parser.py        # parser + types (~150 LOC)
├── turn_chain.py                   # MODIFIED — add InlineXmlFallbackHandler + import (~50 LOC delta)
└── config.py                       # MODIFIED — add InlineXmlFallbackConfig + Config field (~20 LOC delta)

tests/
├── test_inline_xml_parser.py       # NEW — 8+ unit tests driven by fixtures
├── test_inline_xml_handler.py      # NEW — handler-level tests + benchmark
├── test_inline_xml_log_audit.py    # NEW — byte-equal log scan (mirrors 002 SC-005)
└── fixtures/
    └── inline_xml/
        ├── 01_single_block.jsonl
        ├── 02_multiple_blocks.jsonl
        ├── 03_malformed_inside_valid.jsonl
        ├── 04_empty_markers.jsonl
        ├── 05_nested_braces_in_input.jsonl
        ├── 06_mixed_with_structured.jsonl
        ├── 07_adjacent_other_xml.jsonl
        └── 08_all_providers.jsonl

docs/
└── (no change to existing docs — design.md update tracked as task T-DOC-1)
```

### Estimated Effort & Completion Definitions (per 反偷懒规则 3)

| Phase | Task | Effort | Completion Definition |
|---|---|---|---|
| P0 | `agent_core/react/__init__.py` + `inline_xml_parser.py` (parser, types, scanner) | ~150 LOC, 60 min | All 8 fixtures parse to expected `tool_calls` list; 8 unit tests pass. |
| P0 | `InlineXmlFallbackConfig` in `config.py` + Config field | ~20 LOC, 15 min | `Config().inline_xml_fallback.enabled == True`; `from_yaml` parses new key; test for opt-out path. |
| P0 | `InlineXmlFallbackHandler` in `turn_chain.py` (chain wiring) | ~50 LOC, 30 min | Handler runs after `ChunkParseHandler`; test that empty `tool_calls` + inline-XML → non-empty `tool_calls` after handler; test no-op on structured calls. |
| P0 | Test fixtures (8 files) | ~80 LOC JSON, 30 min | All 8 fixture files created with realistic input/output. |
| P0 | Unit tests (parser + handler + audit + benchmark) | ~250 LOC, 90 min | 10+ tests pass; benchmark < 5 ms; byte-equal log scan passes. |
| P1 | Doc sync (design doc note + checklist pass) | ~30 LOC, 20 min | `docs/agent-state-machine-and-chain-of-responsibility-design.md` §? (or new doc) mentions fallback; checklist re-checked. |
| P1 | Final gate: full test suite | 5 min | `python3 -m pytest -q` exits 0 (existing 186 + new 10+ = ≥ 196 tests). |
| P1 | Report 三件套 (completion / tests / deviations) | 5 min | User-facing summary with all 3 sections. |

**Total estimated**: ~255 min focused work (~4.25 hr). **Under 500-line plan threshold** but still listing per 反偷懒规则 第 3 条 ("6+ 步 列表").

### Reporting cadence (per 反偷懒规则 第 4 条)

After each phase, report three things:
1. **完成什么** — files created/modified, line counts
2. **测试结果** — pytest output for the relevant slice, before/after counts
3. **偏差说明** — any deviation from this plan, with reason

## Implementation Strategy (high-level — see tasks.md for per-task detail)

1. **Parser first, handler last** — TDD order: fixtures → parser unit tests →
   parser implementation → handler unit tests → handler implementation →
   integration test (one-shot run simulating GLM-5.1 emission).
2. **Defensive defaults everywhere** — every "if" has an `else` log or
   return-noop; no path may raise that wasn't supposed to.
3. **Idempotent handler** — running the handler twice on the same input
   produces the same output (FR-008). Tested explicitly.
4. **No regression** — full test suite (`pytest -q`) MUST exit 0 before
   any commit; 002-skill-secret-injection's 186 tests are the canary.

## Complexity Tracking

No violations → table intentionally omitted.

## Done When

- [x] Plan written, Constitution Check passes (pre + post design)
- [x] Research resolves all NEEDS CLARIFICATION (9 sections)
- [ ] `/speckit-tasks` produces task breakdown
- [ ] Implementation matches this plan (no silent scope shrink)
- [ ] All tests pass (full suite, exit 0)
- [ ] Doc sync done (design doc note + checklist re-check)
- [ ] User-facing 三件套 report delivered