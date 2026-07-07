---
name: echo-skill
description: "演示 secret env 注入 — 加载后**第一动作必须**是 Bash 工具调 `echo $SECRET_DEMO`,不要先输出解释文字"
metadata:
  requires:
    env: ["SECRET_DEMO"]
---

# Echo Skill

## ⚠️ MANDATORY FIRST ACTION (强制)

加载本 skill 后,本 turn 的 **第一个 tool_use block 必须是 Bash**。不允许:

- 先输出解释文字("好的,我来..."/"我来读取...")
- 描述你接下来要做什么
- 问用户澄清问题
- 用纯 text 收尾(必须是 tool_call,不是 text)

**不发出 Bash tool_use block = 本 skill 执行失败。** 用户期望的是
structured tool_call,不是 text reply。这是为了验证 SECRET_DEMO 是否真的
注入到 os.environ — 没有 Bash tool_result,无法验证 secret 是否落地。

## 执行 — Turn 1 (强制)

作为本 turn 第一动作,emit exactly 这一个 tool_use block:

```json
{
  "name": "Bash",
  "input": {"command": "echo $SECRET_DEMO"}
}
```

收到 tool_result 后,再用自然语言把结果告诉用户。

## 历史背景 (2026-07-07)

多个 model (MiniMax-M3 / GLM-5.1) 在 turn 1 + skill 加载后,**能 Read SKILL.md**
(可证 model 能 emit structured tool_use block),但**不调 Bash**(MiniMax-M3 把
tool_call 写成 inline XML;GLM-5.1 直接停 + 仅 emit 26 char text)。
根因是 LLM thinking-vs-output 错位(memory `project-llm-thinking-vs-output-inconsistency`)。
本次 SKILL.md 强化是治标(让 prompt 强到 model 不容易"偷懒"成纯 text),不是治本。

# 验证步骤(在 web/app.py chat 里问 agent):

1. 问 "用 echo-skill 演示一下:跑 `Bash -c 'echo $SECRET_DEMO'`,把输出告诉我"
2. agent 应该 emit Bash tool_use(block 形式,不是 text 里的 inline XML)
3. agent 应该能读到注入的 value(不是空,不是 $SECRET_DEMO 字面)
4. 然后再问 "再用 `Bash -c 'echo ${SECRET_DEMO:-NOT_SET}'` 跑一次,确认 secret 还在不在"
5. agent 第二次读应该拿到 `NOT_SET`(说明 run 结束后 secret 已被 revert — EnvCleanupHandler 兜底成功)

# 关键不变量(FR-013/014/015):
- system message 中 **不应** 出现 SECRET_DEMO 的字面值
- 日志中 **不应** 出现 secret value(只记 key 名)
- session.jsonl **不应** 持久化 secret value