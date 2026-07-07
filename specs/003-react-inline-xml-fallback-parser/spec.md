# Feature Specification: ReAct Inline XML Tool-Call Fallback Parser

**Feature Branch**: `feature/react-inline-xml-fallback-parser`

**Created**: 2026-07-07

**Status**: Draft

**Input**: User observation (2026-07-07 UI manual test with `echo-skill`):
- Model GLM-5.1 emits `<tool_call>{"name":"Bash","input":{"command":"echo $SECRET_DEMO"}}</tool_call>`
  as text content inside an assistant message, instead of a structured `tool_use` block.
- API `stop_reason` is `stop` (because there are no structured tool blocks), so the
  ReAct state machine (`LLMThinkingPhase`, see `agent_core/agent_state.py:582-583`)
  sees an empty `tool_calls` list and transitions `LLM_THINKING → FINALIZING`,
  short-circuiting tool execution.
- User push-back: "当前是 GLM5.1 不是 minmax,这两个都是成熟的大模型,不应该犯这么低级的错误"
  → root cause is in **our** ReAct parser, not the model. Model's inline-XML emission
  is a real-world signal we MUST accept and convert, not a "wrong" model behavior.

> This spec defines the **WHAT / WHY**, not HOW. The parser's regex strategy, the
> integration point with `LLMResult`, and any provider-specific routing decisions
> belong in `plan.md`.

## Context — Why this exists

The agent_core ReAct state machine currently consumes only **structured** tool
calls (Anthropic `tool_use` blocks / OpenAI tool-call objects surfaced by the LLM
router). When a model emits its decision as inline text wrapped in
`<tool_call>{...}</tool_call>`, the assistant message looks like a plain text reply,
`stop_reason` becomes `stop`, and the SM happily goes to FINALIZING. Tool
execution never happens; the run terminates with an answer that is just
half-formed XML pretending to be a tool call.

This blocks **every** skill whose required first action is a tool call (e.g.
`echo-skill` whose mandatory action is `Bash echo $SECRET_DEMO`). It is also
silent — no error, no warning, just a missing tool step.

**Scope principle (Constitution V)**: this spec is **only** about the parser
fallback layer + its ReAct integration. It does **not**:
- change LLM providers' emission format
- rewrite the state machine
- add a new "model adapter" abstraction
- introduce a generic "XML preprocessor" that runs on all assistant text
- touch the secret-injection layer (002 is upstream of this; secret injection
  itself works fine once tools actually get called)

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Inline-XML tool call gets executed end-to-end (Priority: P1)

As a user running `echo-skill` (or any skill whose mandatory first action is a
tool call) with a model that emits inline-XML, I want the agent to **actually
execute the tool**, see the result, and continue the ReAct loop — instead of
silently finalizing with the raw XML still in the assistant text.

**Why this priority**: Without this, skills that rely on tool calls become
non-functional on any model that has ever been observed to emit inline-XML
(GLM-5.1 confirmed; MiniMax-M3 historically reported in `echo-skill/SKILL.md`).
P1 is the entire point of this branch.

**Independent Test**: Reproduce the user-observed scenario — inject a fake LLM
response whose assistant text contains a single
`<tool_call>{"name":"Bash","input":{"command":"echo hi"}}</tool_call>` block, run
one ReAct step, assert that:
1. `Bash` was actually invoked with `echo hi`
2. The tool result was routed back into the conversation
3. `LLM_THINKING → FINALIZING` transition did **not** happen prematurely
4. Final transcript contains the tool output (not the raw XML)

**Acceptance Scenarios**:

1. **Given** an LLM response whose `tool_calls` is empty **and** whose
   assistant text contains exactly one `<tool_call>{...}</tool_call>` block
   with valid JSON, **When** the ReAct parser runs, **Then** the parsed
   `tool_calls` list contains one tool call equivalent to the inline JSON,
   and `stop_reason` is overridden to a tool-call-presence indicator so
   the SM transitions to `EXECUTING_TOOLS`.
2. **Given** an LLM response with valid structured `tool_use` blocks already
   present, **When** the parser runs, **Then** the inline-XML fallback is
   **not** activated (zero false positives on the normal happy path).
3. **Given** an LLM response with structured `tool_use` blocks **and**
   trailing inline-XML in the text (mixed), **When** the parser runs,
   **Then** structured blocks win; inline-XML is left as visible assistant
   text (no double-execution).

---

### User Story 2 — Multiple inline-XML blocks in one response (Priority: P2)

As a model that wants to call several tools in one step, I want the parser to
extract **all** `<tool_call>` blocks, in order, so that the SM can execute them
in parallel (or sequentially, per existing SM semantics).

**Why this priority**: A model that emits inline-XML once will likely emit it
in batches when it wants parallel tools. Supporting only single-block would
leave half the runs broken. P2 because P1 already unlocks the dominant case.

**Independent Test**: Fixture with one assistant text containing three
independent `<tool_call>` blocks; assert `tool_calls` list length is 3, in
document order; assert each block's `input` JSON parses independently.

**Acceptance Scenarios**:

1. **Given** an LLM response whose text contains N≥1 valid `<tool_call>`
   blocks, **When** the parser runs, **Then** `tool_calls` length == N and
   order matches text occurrence order.

---

### User Story 3 — Malformed / partial inline-XML degrades safely (Priority: P2)

As a defensive parser, I want malformed inline-XML to be **skipped with a
warning**, not crash the run, not silently drop the whole response, and not
turn the SM into FINALIZING if at least one valid block existed.

**Why this priority**: Constitution Principle VI (defensive & pragmatic
engineering) requires this. Models occasionally emit truncated blocks or
escaped braces. Without this, one bad block breaks the whole run.

**Independent Test**: Fixture with one valid block + one malformed block
(broken JSON); assert valid block becomes a tool call, malformed block logs
a WARNING, `tool_calls` length == 1, run continues.

**Acceptance Scenarios**:

1. **Given** text with one valid block + one malformed block, **When** the
   parser runs, **Then** the valid block becomes a tool call; the malformed
   block emits a WARN log containing the line index (no values, no input
   payload); the run does not raise.
2. **Given** text with only malformed blocks, **When** the parser runs,
   **Then** `tool_calls` is empty, a WARN is logged, and the SM proceeds to
   FINALIZING as it would for any text-only response (no different from
   "model said nothing actionable").

---

### User Story 4 — Observable audit trail (Priority: P3)

As a developer debugging "why did this run take a weird path", I want every
fallback activation to leave a clear log marker, including a per-step count
and a hash of the model name (so we can correlate with provider issues
later) — but **never** the tool input values (Constitution II: no secrets
in logs, inherited from 002-skill-secret-injection SC-005).

**Why this priority**: Without observability, regressions in the parser are
silent — exactly the failure mode that hid this bug for so long. P3 because
it's not user-facing, but the failure-cost of not having it is high.

**Independent Test**: Run the parser on a synthetic inline-XML response,
capture `caplog`, assert a single INFO-level marker like
`🧩 react.inline_xml_fallback: provider=<hash> blocks=<int>` is present and
contains **no** secret value.

**Acceptance Scenarios**:

1. **Given** the parser activates on a response, **When** it logs, **Then**
   the log line contains (a) an emoji-prefixed marker, (b) provider id hash,
   (c) block count, (d) first tool name (if any), and (e) **no** tool input
   payload bytes.

---

### Edge Cases

- **Empty / whitespace-only text inside markers**: `<tool_call></tool_call>`
  → skip with WARN, not crash.
- **Nested braces in JSON**: a Bash command containing `{`/`}` (e.g.
  `echo '{a:1}'`) → parser must not be fooled by naive `{}` counting.
- **Tool name not in registered tool list**: parse succeeds, error surfaces
  at tool-execution time (existing SM behavior); parser does not pre-validate
  tool names (avoids duplicating tool-registry logic).
- **Multiple identical `<tool_call>` blocks**: extract all (de-duplication is
  the SM's job, not the parser's).
- **Marker adjacent to other XML** (e.g. `<tool_call>{a:1}</tool_call><other/>`):
  only consume the well-formed `<tool_call>...</tool_call>` pair.
- **`<tool_call>` appearing inside a code block in the text**: parser still
  extracts it (defensive: model didn't intend it as code, it intended it as
  a tool call; if model intended it as code it would backtick-escape). This
  is documented, not "fixed".

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Parser MUST detect every `<tool_call>{...}</tool_call>` block in
  an assistant message's text content, where `{...}` is a JSON object
  containing at minimum `name` (string) and `input` (object).
- **FR-002**: Parser MUST produce a `tool_calls` list whose items are
  structurally compatible with the existing data contract consumed by the
  SM (so no downstream consumer needs to change shape).
- **FR-003**: When the parser converts inline-XML blocks into tool calls,
  it MUST override the upstream `stop_reason` semantics so the SM routes
  to `EXECUTING_TOOLS` instead of `FINALIZING`. The original `stop_reason`
  value MUST be preserved in the activation INFO marker (so providers can
  still be correlated).
- **FR-004**: The fallback MUST be a **no-op** when (a) structured tool
  calls are already present in the response, or (b) no `<tool_call>` markers
  are found in the text. Both no-op cases MUST be cheap (<1 ms each).
- **FR-005**: Malformed blocks (broken JSON, missing `name`/`input`) MUST
  be skipped with a WARN log; valid blocks in the same response MUST still
  be processed.
- **FR-006**: The parser MUST log exactly one INFO-level marker per
  activation, including provider id (hashed), block count, and first tool
  name — and MUST NOT log any tool input payload bytes.
- **FR-007**: The fallback MUST run **after** the LLM response has been
  assembled into the unified `(text, structured_tool_calls)` shape, and
  **before** the SM decides its next transition. The exact wiring location
  belongs in `plan.md`; the requirement is the timing, not the file.
- **FR-008**: The fallback MUST be **idempotent** — re-running it on an
  already-parsed response produces zero additional tool calls and zero
  additional log lines (defensive: protects against accidental double-wiring
  in the chain).
- **FR-009**: The parser MUST work for **all** LLM providers (Anthropic,
  OpenAI, GLM, Zhipu) — it is provider-agnostic. Provider-specific quirks
  (e.g. tool_use block types) MUST be normalized upstream so the parser
  sees a uniform `(text, structured_tool_calls)` shape.
- **FR-010**: Hot-path latency MUST stay under 5 ms per response (single-
  block case) and scale linearly (not superlinearly) with block count.
- **FR-011**: Configuration MUST allow disabling the fallback via the
  top-level `agent_core.config.Config.inline_xml_fallback` field (default
  ON), so users running only Anthropic can opt out without code changes.

### Key Entities

- **`InlineToolCall`**: a parsed representation of one `<tool_call>` block.
  Fields: `name: str`, `input: dict`, `source_text_offset: int` (for
  diagnostic logs), `block_index: int` (0-based among all blocks in the
  response).
- **`ParseOutcome`**: parser result. Fields: `tool_calls: list[InlineToolCall]`,
  `malformed_blocks: list[int]` (block indices), `fallback_activated: bool`,
  `original_stop_reason: str | None`.
- **`FallbackConfig`**: opt-out config. Fields: `enabled: bool = True`,
  `log_provider_hash: bool = True`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The original user scenario (`echo-skill` + GLM-5.1 inline-XML
  `Bash echo $SECRET_DEMO`) reaches the "Bash executed + tool result
  observed" stage in **100%** of runs, verified end-to-end by replaying the
  captured UI run log.
- **SC-002**: 100% of inline-XML patterns in a fixture suite are detected
  (fixture covers: single block, multiple blocks, empty markers, broken
  JSON, mixed-with-structured, nested braces, adjacent XML, model = each
  of Anthropic / OpenAI / GLM / Zhipu). Each case has its own pytest.
- **SC-003**: Zero regression on the 002-skill-secret-injection test suite
  (186 tests pass) and the full `agent_core` test suite (exit 0).
- **SC-004**: Fallback activation produces exactly **one** INFO log line
  per response, with `provider_id` (hashed), `block_count`, and
  `first_tool_name` present and **no** tool input bytes — verified by a
  byte-equal scan (mirrors 002-skill-secret-injection SC-005 audit pattern).
- **SC-005**: Hot-path latency measured by a benchmark test is under
  5 ms for a single-block response (1000-iter mean, on developer laptop).
- **SC-006**: Configuration opt-out works end-to-end: setting
  `enabled=False` produces zero fallback activation even when inline-XML
  is present, and logs a single DEBUG line stating "fallback disabled by
  config".

## Assumptions

- The ReAct state machine treats empty `tool_calls` combined with a
  stop-only `stop_reason` (e.g. `"stop"` / `"end_turn"`) as "nothing to
  do, go to FINALIZING" — this is the integration target the parser must
  intercept.
- LLM providers normalize structured tool blocks before surfacing to the
  parser. The parser sees a uniform
  `(text: str, structured_tool_calls: list)` shape regardless of provider.
- Tool names appearing in inline-XML are guaranteed to exist in the
  registered tool list for the skill that triggered the run (no
  cross-skill tool names). Tool-execution errors for unknown names are
  handled by the existing permission / executor layer, not by this parser.
- The fallback is **additive**: it never removes or alters structured
  tool calls; it only adds parsed calls when structured calls are absent
  (and inline-XML is present).
- Existing logging infrastructure is available; the parser uses a dedicated
  sub-logger (its name is a `plan.md` concern). Logs MUST follow the
  byte-stable pattern from 002-skill-secret-injection SC-005 (no values,
  no secret payload bytes).
- Inline-XML markers use **exactly** the form `<tool_call>` and
  `</tool_call>` (lowercase, no whitespace inside the tag). Variants
  (`<tool_call>`, `<tool-use>`, `<functioncall>`) are out of scope and
  would require a separate spec.

## Out of Scope (explicit)

- Adding new tool-call format support (e.g. `<tool_use>`, `<functioncall>`,
  Qwen-style markers).
- Touching 002-skill-secret-injection logic (this feature is a pure
  consumer of injected env vars; it does not change injection).
- Replacing the SM or rewriting `LLMThinkingPhase`.
- Adding a generic XML preprocessor or "thinking-content" extractor.
- Provider-specific routing (e.g. "use fallback only for GLM").
- Changing how structured tool calls are surfaced by `agent_core/llm/router.py`.
- Any change to `web/app.py` UI surface area (UI sees the same observable
  behavior; if a tool gets called, the UI shows it; this spec does not
  alter UI rendering).