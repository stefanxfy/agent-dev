---
name: project-llm-thinking-vs-output-inconsistency
description: "LLM extended thinking 跟实际 output 不强制一致 — thinking 说要用 tool 但 streaming 跳过 tool_use (stop_reason=stop) 会发生,SessionPersistHandler 不背锅"
metadata:
  type: project
---

LLM (Claude with extended thinking) 会出现 "thinking 说要用 tool, 但实际 content 跳过 tool_use 直接给 final answer" 的现象。

**复现证据** (logs/app/agent.log + data/sessions/):
- `data/sessions/7fe2120a.jsonl` (2026-07-01 11:30): user "计算 23*34" → assistant thinking "I'll use the calc tool" → content "23 × 34 = 782" (stop_reason=stop, 没调 tool)
- `data/sessions/d80b83d3.jsonl` (2026-07-01 11:34): user "计算 23*987" → assistant thinking "I can use the calc tool to do this" → content "23 × 987 = 22,701" (stop_reason=stop, 没调 tool)
- 对照成功案例 `data/sessions/e205860b.jsonl` (2026-07-01 11:07): turn=1 stop=tool_calls (调 calc) → turn=2 stop=stop (final)

**Why**:
- Anthropic API 的 extended thinking 是 LLM internal monologue, **不强制跟 output 一致**
- LLM 在 streaming 时 next-token 概率可能跳到 final path, 跳过 tool_use 决定
- System prompt 空 (`web/app.py:99` default = "") 没强制 "数学必须用 calc"
- SessionPersistHandler 在 FINALIZING 阶段正确处理 "无 tool_calls 直接 final" 情况 (落 final assistant entry)

**How to apply**:
- 下次 user 报 "JSONL 缺 tool_use/tool_result" 时, 先 grep logs/app/agent.log 看 LLM RESP 的 stop_reason: `stop` → LLM 行为问题, handler 不背锅; `tool_calls` → handler 触发问题, 该查 SessionPersistHandler
- 不要因为 thinking 里有 "I'll use tool" 就以为 handler 漏写 — LLM 行为不稳是上游问题
- User 已接受现状 (LLM 偶尔跳过 tool_use), 这次不修 system prompt

相关: [[project-session-persist-handler-boundary]] (SessionPersistHandler 已正确处理无 tool_calls 情况)