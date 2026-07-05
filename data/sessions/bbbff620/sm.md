---
session_id: bbbff620
schema_version: 1
last_compacted_msg_id: null
last_compacted_at: null
---

# Session Memory

> 此文件由 L3 SessionMemoryLayer 维护(v2.1 §4.4)。
> - extract 路径: 后台 LLM 增量更新,只 Edit 不重写
> - compact 路径: 直接读此文件 + 按 section 截断,零 LLM 调用
> - 信息永不丢失:每条消息提取后写入对应 section

## Context
用户基本信息：
- 姓名：小明
- 职业：程序员，有 Java 后端开发和运维开发经验，目前正在学习 AI 开发
## Decisions
<!-- 已做的决策(用户偏好 + 系统决策) -->

## Technical
<!-- 技术细节、依赖、API 行为 -->

## Open Questions
<!-- 待澄清的问题 -->

## User Preferences
<!-- 用户偏好(显式 + 隐式) -->
