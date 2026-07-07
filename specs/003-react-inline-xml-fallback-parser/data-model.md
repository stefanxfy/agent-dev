# Data Model — ReAct Inline XML Tool-Call Fallback Parser

**Branch**: `feature/react-inline-xml-fallback-parser`
**Date**: 2026-07-07
**Spec**: [spec.md](spec.md)

## Entities

### `InlineToolCall` (parser-internal; not exposed publicly)

A parsed representation of one `<tool_call>{...}</tool_call>` block.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `str` | yes | Tool name (must match a registered tool for execution to succeed; parser does NOT validate) |
| `input` | `dict` | yes | Parsed JSON object; coerced from the raw JSON in the marker |
| `source_text_offset` | `int` | yes | 0-based byte offset of the opening `<tool_call>` in the original assistant text (for diagnostic logs only) |
| `block_index` | `int` | yes | 0-based index among all blocks in the response |

**Lifecycle**: parser produces → handler converts to `ToolCallDelta` →
downstream consumed. Internal struct, never logged or persisted in this
form.

### `ParseOutcome` (parser-internal return value)

| Field | Type | Required | Notes |
|---|---|---|---|
| `tool_calls` | `list[InlineToolCall]` | yes | Successfully parsed blocks |
| `malformed_blocks` | `list[int]` | yes | Block indices that failed JSON parse or schema validation |
| `fallback_activated` | `bool` | yes | True iff ≥1 valid block found AND structured `tool_calls` already present is False |
| `original_text` | `str` | yes | The full assistant text (echo for downstream consumers that might want it; current implementation passes through unmodified) |

### `ToolCallDelta` (existing — produced by parser as the public surface)

Reuses the existing `agent_core/llm/types.py` dataclass. The parser fills:

| Field | Type | Source |
|---|---|---|
| `tool_name` | `str` | from `InlineToolCall.name` |
| `tool_input` | `dict` | from `InlineToolCall.input` |
| `tool_use_id` | `str` | synthetic: `f"inline_xml_{block_index}"` |
| `is_final` | `bool` | always `True` (mirrors existing convention) |

The synthetic `tool_use_id` prefix `inline_xml_` distinguishes fallback-
produced calls from structured calls in audit logs.

### `InlineXmlFallbackConfig` (NEW — pydantic model)

Top-level config knob for the fallback.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `True` | Master switch. When `False`, handler short-circuits to no-op and logs DEBUG. |
| `log_provider_hash` | `bool` | `True` | When `False`, log line omits the provider hash (used in tests that assert exact log format). |

**Wired into**: `agent_core.config.Config` as `inline_xml_fallback: InlineXmlFallbackConfig`.

### `RunState.last_tool_calls` (existing — semantics change)

The fallback handler **appends** parsed `ToolCallDelta` instances to this
list in addition to whatever `ChunkParseHandler` already wrote. Order:
structured first (preserved from `ChunkParseHandler`), then parsed in
document order.

## Relationships

```text
LLM stream
    │ (router assembles)
    ▼
ChunkParseHandler
    │ writes stage_outputs.tool_calls (structured only, may be empty)
    │ writes stage_outputs.full_text (aggregated text)
    ▼
InlineXmlFallbackHandler  ◄── reads Config.inline_xml_fallback (NEW field)
    │ reads stage_outputs
    │ if structured tool_calls is empty AND inline-XML markers found:
    │     parse → produce ToolCallDelta list
    │     append to stage_outputs.tool_calls
    │     override stage_outputs.stop_reason = "tool_use"
    │     log single INFO line (no input bytes)
    │ else:
    │     no-op
    ▼
LLMThinkingPhase.next
    │ reads stage_outputs.tool_calls
    │ (now contains parsed calls if fallback activated)
    ▼
EXECUTING_TOOLS or FINALIZING
```

## Validation rules

| Rule | Where enforced |
|---|---|
| `name` MUST be non-empty string | Parser schema check |
| `input` MUST be a JSON object (not array, not scalar) | Parser schema check |
| JSON inside marker MUST parse cleanly | `json.loads` raises → block → malformed |
| Marker MUST be exactly `<tool_call>` and `</tool_call>` (lowercase, no whitespace) | Scanner is literal-match |
| `source_text_offset` MUST be a valid byte offset into the original text | Scanner emits from internal cursor |

## State transitions

The parser has no state — every call is independent. The handler has
one piece of mutable state: it mutates `ctx.stage_outputs` (which is
the standard mutation pattern across all handlers in `turn_chain.py`).

## Persistence

None. The parser is stateless; the handler mutates an in-memory
`ctx.stage_outputs`. No new fields on `RunState`, no new files, no new
session serialization keys.

## Out-of-scope entities (explicit)

- No new `AgentPhase` enum value.
- No new `RunState` field (existing `last_tool_calls` reused).
- No new event type emitted by the handler (existing `tool_call` chunk
  emission from `ChunkParseHandler` covers the case; the fallback adds
  to `stage_outputs.tool_calls` which feeds into the same downstream
  pipeline).
- No new permission model — the synthetic `inline_xml_*` `tool_use_id`
  uses the existing permission engine keyspace.
- No new memory bridge extraction — fallback-parsed tool calls are
  normal tool calls from the memory extractor's perspective.