# Contract: Inline XML Tool-Call Format

**Branch**: `feature/react-inline-xml-fallback-parser`
**Date**: 2026-07-07
**Spec**: [spec.md](../spec.md)

> Grammar / shape contract for `<tool_call>` markers emitted by LLMs in
> assistant message text content. Parser MUST accept every well-formed
> instance; MUST reject (skip + WARN) every malformed instance without
> crashing the run.

## Format (BNF-style)

```
tool_call_block  ::= "<tool_call>" ws object ws "</tool_call>"
object           ::= "{" (string ":" value ("," string ":" value)*)? "}"
value            ::= object | array | string | number | "true" | "false" | "null"
string           ::= '"' chars '"' | "'" chars "'"
chars            ::= any-char-except-quote-or-control
ws               ::= whitespace*        ; optional, ignored
```

## Required fields

Inside the JSON object, exactly two fields are required:

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `string` | yes | Tool name (e.g. `"Bash"`, `"Read"`, `"Write"`). Must match a registered tool for execution to succeed (the parser does NOT validate). |
| `input` | `object` | yes | Tool input arguments as a JSON object (never a scalar). |

Any additional fields are **forwarded** to `ToolCallDelta.tool_input` as-is
(some models may include `_meta`, `id`, etc. — we keep them, the
downstream consumer can ignore).

## Examples (canonical)

### Minimal — Bash echo

```
<tool_call>{"name":"Bash","input":{"command":"echo hi"}}</tool_call>
```

### With whitespace inside markers (allowed)

```
<tool_call> {"name":"Read","input":{"file_path":"/tmp/x"}} </tool_call>
```

### With extra fields (forwarded)

```
<tool_call>{"name":"Bash","input":{"command":"ls"},"_meta":{"trace_id":"abc"}}</tool_call>
```

### Multi-line JSON input

```
<tool_call>{
  "name": "Write",
  "input": {
    "file_path": "/tmp/out.txt",
    "content": "hello\nworld"
  }
}</tool_call>
```

## Examples (well-formed but tricky — parser MUST handle)

### Nested braces in input

```
<tool_call>{"name":"Bash","input":{"command":"echo '{a:1, b:2}'"}}</tool_call>
```

The scanner's balanced-brace counter MUST respect string boundaries
(track `"..."` and `'...'` and skip over them).

### Bash command with embedded `</tool_call>` substring (theoretical)

```
<tool_call>{"name":"Bash","input":{"command":"echo '</tool_call>' && true"}}</tool_call>
```

The scanner MUST consume **only** the first balanced `</tool_call>`
that closes the outermost `<tool_call>` it opened. (Nested markers in
input are forwarded as-is; only top-level markers count.)

### Multiple blocks in one response

```
<tool_call>{"name":"Read","input":{"file_path":"/a"}}</tool_call>
<tool_call>{"name":"Bash","input":{"command":"ls /b"}}</tool_call>
<tool_call>{"name":"Write","input":{"file_path":"/c","content":"x"}}</tool_call>
```

Parser returns 3 `InlineToolCall` in document order.

## Examples (malformed — parser MUST skip + WARN, not crash)

### Broken JSON

```
<tool_call>{"name":"Bash","input":{"command":"echo}</tool_call>
```

(unclosed string in JSON)

### Missing required field

```
<tool_call>{"name":"Bash"}</tool_call>
```

(no `input` field — schema check fails)

### Wrong type for required field

```
<tool_call>{"name":"Bash","input":"not an object"}</tool_call>
```

(input is a string, not object — schema check fails)

### Empty markers

```
<tool_call></tool_call>
<tool_call>{}</tool_call>
```

(empty body — no `name`/`input` — schema check fails)

### Non-lowercase or variant markers (out of scope)

```
<toolcall>{"name":"Bash","input":{}}</toolcall>
<tool_use>{"name":"Bash","input":{}}</tool_use>
<functioncall>{"name":"Bash","input":{}}</functioncall>
```

These are **out of scope** (spec Out-of-Scope section). Parser MUST NOT
match them; they remain visible assistant text. A future spec may add
support.

## What the parser MUST NOT do

- MUST NOT extract JSON that is not wrapped in `<tool_call>...</tool_call>`.
- MUST NOT execute the parsed calls itself (parser is read-only;
  execution is downstream `ToolExecuteHandler`'s job).
- MUST NOT log the value of `input` (only `name` is allowed in logs).
- MUST NOT modify `full_text` (the original text is passed through
  unchanged; visibility of the raw XML in the assistant message is a
  conscious decision documented in spec Edge Cases — "code block
  collision").
- MUST NOT run more than once per response (FR-008 idempotency).

## Versioning

This contract is **v1**. Any change to required field names, marker
syntax, or extraction rules is a MAJOR version bump and requires a new
spec.