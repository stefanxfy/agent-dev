# Research — ReAct Inline XML Tool-Call Fallback Parser

**Branch**: `feature/react-inline-xml-fallback-parser`
**Date**: 2026-07-07
**Spec**: [spec.md](spec.md)

> Phase 0 research artifacts. Decisions below resolve every NEEDS CLARIFICATION
> in the plan's Technical Context, plus the spec's open architecture questions.

## 1. Integration timing — where in the chain does the fallback run?

### Decision

Add a new `InlineXmlFallbackHandler` to `llm_chain` (the chain that includes
`LLMCallHandler` and `ChunkParseHandler`), positioned **immediately after
`ChunkParseHandler`** and **before** the next downstream handler.

### Rationale

- The ReAct state machine reads `ctx.turn_ctx.stage_outputs.tool_calls` in
  `LLMThinkingPhase.next`. `ChunkParseHandler` is the **only** writer of
  `stage_outputs` in the normal LLM-response path (turn_chain.py:1178).
- The fallback MUST see the **fully-aggregated** `full_text` from
  `ChunkParseHandler` (so it can't run inline mid-stream — partial
  responses would falsely split `<tool_call>` blocks across chunks).
- It MUST run **before** `LLMThinkingPhase.next` is called, which is the
  next event-loop iteration after `llm_chain.run()` returns. Handler-chain
  placement is the cleanest cut-point that satisfies both constraints.

### Alternatives considered

| Option | Rejected because |
|---|---|
| Embed inside `ChunkParseHandler.handle()` (right after the `tool_calls=tool_calls` line) | Violates SRP that 2026-07-02 refactor explicitly established; hard to test parser in isolation; conflates stream-parsing with text-recovery. |
| Embed inside `LLMThinkingPhase.enter()` | Wrong layer (SM is supposed to be a pure decision-maker, not a parser); also runs **after** PermissionCheckHandler, so tool execution already started by then. |
| Subclass `ChunkParseHandler` | Fragile inheritance; future changes to chunk aggregation would silently affect the parser. |

## 2. Regex vs hand-written scanner for balanced `{...}`?

### Decision

Hand-written byte scanner (no `regex`). The parser walks the assistant text
once, looking for the literal `<tool_call>` prefix, then scans forward
matching balanced `{...}` (respecting string literals and escapes), then
looks for `</tool_call>`.

### Rationale

- Python `re` is **not** reliable for balanced delimiters (no recursion in
  the stdlib `re` module — only `regex` 3rd-party supports it). The
  Bash-input case `echo '{a:1}'` from spec FR-001 / Edge Cases would
  confuse any naive `r'\{.*?\}'` matcher into grabbing the wrong brace.
- Hand-written scanner is ~40 lines, **O(n)** single-pass, no backtracking,
  and trivially fast (≪1ms on 10KB text in informal benchmarks).
- Defensive string handling (track `"..."` and `'...'` boundaries so braces
  inside string literals don't perturb the balance counter) is essential
  for Bash commands with embedded JSON.

### Alternatives considered

| Option | Rejected because |
|---|---|
| Use `regex` 3rd-party for recursive matching | Adds a new dep — Constitution: "极简依赖" (no Agent framework); `regex` is borderline-OK but unnecessary. |
| `re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)` | Fails on nested `{}`; fails on multi-line JSON input that itself contains `</tool_call>` substring; greedy enough to over-match adjacent blocks. |
| Try JSON-decode of every `{...}` substring starting at `<tool_call>` | Wasteful O(n²); pre-filtering with balanced-brace scan is faster. |

## 3. What dataclass do parsed calls become?

### Decision

Parsed calls become `ToolCallDelta` instances (defined in
`agent_core/llm/types.py`). They are appended to the same `tool_calls`
list that `ChunkParseHandler` already populates. The synthetic
`tool_use_id` is `"inline_xml_{counter}"` where `counter` is the
0-based block index in the response.

### Rationale

- `ToolCallDelta` is the **exact** shape consumed by `PermissionCheckHandler`
  and `ToolExecuteHandler`. Producing any other shape would force a
  converter layer downstream.
- The synthetic ID prefix `inline_xml_` makes audit-log searches trivial
  (find all blocks the fallback produced vs. real structured blocks).
- `is_final=True` matches the convention (`ToolCallDelta.is_final = True`
  in current implementation: "完整返回,非增量").

### Alternatives considered

| Option | Rejected because |
|---|---|
| Produce a new `InlineToolCall` dataclass and convert at consumer side | Two-shape world — every downstream handler must handle both. Spec FR-002 forbids this. |
| Mutate `chunk.tool_call` from `ChunkParseHandler` directly | We don't own the chunks; they're router-owned. |

## 4. stop_reason override semantics

### Decision

When the fallback activates and produces ≥1 valid tool call, the
**fallback handler** rewrites `ctx.stage_outputs.stop_reason` to
`"tool_use"` (the canonical Anthropic stop_reason for "model decided to
call tools"). The original `stop_reason` is captured in a single INFO
log line ("original_stop=<value>") so the value is preserved for
diagnostic correlation without a new `_run_state` field.

### Rationale

- `LLMThinkingPhase.next` reads `stop_reason` only via `getattr` and only
  as the source of `final_stop_reason` in the empty-`tool_calls` branch —
  so overriding is safe even when called there.
- Adding a new `_run_state` field for "original stop_reason" is invasive
  (touches the state schema, breaks any existing session-resume
  serialization). The log line is sufficient — operators can grep.
- Using `"tool_use"` (vs `"end_turn"`) preserves the semantic meaning
  when downstream code (e.g. session persistence) gates on it.

### Alternatives considered

| Option | Rejected because |
|---|---|
| Leave `stop_reason` untouched, only set `tool_calls` | Cleaner data, but consumers that branch on `stop_reason == "end_turn"` to mean "no tools" might still misfire. Defense-in-depth favors overriding. |
| New `_run_state.original_stop_reason` field | Schema-invasive; one-off log capture is sufficient (Constitution II: data-driven, no extra metadata fields without justification). |

## 5. Configuration opt-out (FR-011)

### Decision

Add a top-level `inline_xml_fallback: InlineXmlFallbackConfig = InlineXmlFallbackConfig()`
field on the agent's main `Config` class (the same pydantic `Config` that
already holds `llm`, `memory`, `skills`, etc.). `InlineXmlFallbackConfig`
is a pydantic model with `enabled: bool = True` and
`log_provider_hash: bool = True`. Loading is via `Config.from_yaml` /
`from_env`, consistent with all other subsystems.

### Rationale

- Mirrors how `skills`, `memory`, `session` are configured — top-level
  config object, pydantic validation, defaults safe.
- Loading at `Config` level keeps the parser **stateless** — no
  agent-internal state mutation needed; the handler reads the flag from
  the agent's `Config` reference once at chain-build time.
- Users who only run Anthropic can disable without code changes (their
  LLM never emits inline-XML anyway, but the parser still costs a few μs).

### Alternatives considered

| Option | Rejected because |
|---|---|
| Per-skill toggle (in `SkillEntryConfig`) | Skill lifecycle is wrong granularity — inline-XML fallback is an LLM concern, not a skill concern. Spec mentions "top-level" explicitly (FR-011 wording). |
| Env var only (`AGENT_INLINE_XML_FALLBACK=off`) | Loses the per-instance flexibility; env vars are noisy with multiple agents in one process. |
| Hard-coded ON | Violates FR-011. |

## 6. Module layout — where does the parser live?

### Decision

New file `agent_core/react/__init__.py` + `agent_core/react/inline_xml_parser.py`,
plus a new `InlineXmlFallbackConfig` model in `agent_core/config.py`.

The handler itself (`InlineXmlFallbackHandler`) goes in
`agent_core/react/inline_xml_handler.py` (mirror of the parser / handler
separation in `agent_core/skills/env_overrides.py` + the
`SkillsPromptHandler` in `turn_chain.py`).

### Rationale

- `agent_core/skills/` already established the precedent of "module owns
  parser, handler in turn_chain": `env_overrides.py` defines `apply_*` /
  `resolve_*`, and the consuming `SkillsPromptHandler` lives in
  `turn_chain.py`. Same split here.
- New `agent_core/react/` package keeps ReAct-specific stuff (parser +
  handler + config) in one place; future ReAct improvements have an
  obvious home.
- Single-purpose pydantic config in `agent_core/config.py` (not nested
  in `react/`) mirrors how `SkillsConfig` lives in `agent_core/skills/config.py` —
  but in this case the config is **agent-wide** (one flag), not per-skill,
  so top-level `Config` is correct.

### Alternatives considered

| Option | Rejected because |
|---|---|
| All in `turn_chain.py` | Already 1700+ lines; the parser deserves its own testable unit. |
| `agent_core/llm/inline_xml.py` | Wrong layer — the parser runs **after** LLM router output is fully aggregated; it is a ReAct concern, not an LLM concern. |
| Per-handler config in `agent_core/react/config.py` | Splits agent config across multiple files; harder to audit. |

## 7. Logging format & audit guarantees

### Decision

One INFO log line per fallback activation, format:

```
🧩 react.inline_xml_fallback: provider=<8-char-sha1> blocks=<int> first_tool=<name_or '-'> original_stop=<stop>
```

- Sub-logger name: `agent_core.react.inline_xml_fallback`.
- NEVER logs input payload bytes (per FR-006 / spec SC-004).
- WARNING logs for malformed blocks: line only, no input bytes.

### Rationale

- Matches the existing `🧩` emoji convention used in 002-skill-secret-injection
  audit log (`🧩 injected: skill=%s secret=%s kind=%s`).
- Provider hash (8-char SHA1) prevents logging model names in plaintext
  (some org configs treat model as PII); matches the byte-stable scan
  pattern from `verify_skill_secrets_audit.py`.
- Spec FR-006 + SC-004 are testable as a byte-equal scan against the
  log file (mirrors SC-005 from 002-skill-secret-injection).

## 8. Performance budget (SC-005)

### Decision

- Single-block case: ≤ 5 ms (verified by a benchmark test in the test
  suite — not a manual check).
- Multi-block case: linear in text length + block count; verified by a
  second benchmark test with 5 blocks.
- No regex backtracking; no second-pass JSON validation; the hand-written
  scanner produces the dict in one pass.

### Rationale

- Hand-written scanner with single-pass JSON extraction (using `json.loads`
  only at block boundaries, not across blocks) is O(n).
- 5ms ceiling matches the existing `_LLMResult.tool_calls` assembly
  budget (which runs in the same handler-chain step).

## 9. Test fixture strategy

### Decision

Create `tests/fixtures/inline_xml/` with **8 fixture files** covering:

1. `01_single_block.jsonl` — one valid `<tool_call>`, simple Bash
2. `02_multiple_blocks.jsonl` — three sequential blocks
3. `03_malformed_inside_valid.jsonl` — one valid + one broken JSON
4. `04_empty_markers.jsonl` — `<tool_call></tool_call>` (empty body)
5. `05_nested_braces_in_input.jsonl` — Bash command with `{}` in input
6. `06_mixed_with_structured.jsonl` — structured `tool_use` block + inline-XML
7. `07_adjacent_other_xml.jsonl` — `<tool_call>{...}</tool_call><other/>`
8. `08_all_providers.jsonl` — one block per provider (Anthropic/OpenAI/GLM/Zhipu)

Each fixture has: `name`, `text`, `expected_tool_calls`, `expected_malformed`,
`expected_fallback_activated`, and a `provider` field.

### Rationale

- Mirror of `tests/fixtures/` from 002-skill-secret-injection (the secret
  audit fixture pattern).
- Each fixture drives one pytest case via parametrize → 8 cases.
- Edge-case coverage matches spec FR-001/005 + Edge Cases.

## Summary of resolved unknowns

| Unknown (from Technical Context) | Resolution |
|---|---|
| Parser location | `agent_core/react/inline_xml_parser.py` + handler in `turn_chain.py` |
| Regex strategy | Hand-written byte scanner with balanced-brace matching |
| Wire-in point | New handler immediately after `ChunkParseHandler` in llm_chain |
| Dataclass for parsed calls | `ToolCallDelta` (existing) |
| `tool_use_id` generation | `inline_xml_{block_index}` |
| `stop_reason` override | `"tool_use"` when ≥1 valid block; original logged |
| Config opt-out | Top-level `Config.inline_xml_fallback.enabled` (default True) |
| Log format | Single INFO line per activation, no input bytes |
| Performance target | ≤ 5 ms single-block, linear scaling |
| Test fixtures | `tests/fixtures/inline_xml/01..08_*.jsonl` |

No NEEDS CLARIFICATION remains.