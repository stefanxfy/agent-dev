# agent-dev 项目长期记忆

## 项目定性
- **定位**：从零自研生产级 Agent 系统（学习 + 生产），极简依赖，全自研核心（ReAct、工具、记忆、会话）
- **GitHub**：`git@github.com:stefanxfy/agent-dev.git`
- **活跃分支**：`feature/fork-compact`
- **Python**：3.11（uv 管理，.venv/）
- **启动入口**：`python3 -m streamlit run web/app.py`

## 技术栈
- UI：Streamlit（双入口：app.py 自研 ReAct / app_langgraph.py LangGraph）
- LLM Router：统一调用 Anthropic Claude / OpenAI GPT / 智谱 GLM / MiniMax
- Agent：手写 ReAct 循环（agent_core/agent_core.py）+ LangGraph 对比版（langgraph_agent/）
- 记忆系统：v2.3.3（M11.7），双通道写入 + 语义召回 + L3 会话压缩 + autoDream 蒸馏
- 存储：JSONL 会话日志 + SQLite meta.db + ChromaDB 向量索引 + per-file .md 记忆文件

## 记忆系统里程碑状态
- M1-M10：全部完成（2026-06-23）
- M11 系列：
  - M11（frontmatter + MEMORY.md + 召回对齐）：实施中
  - M11.5（SM 解耦 LLM callback）：✅ 已落地 2026-06-28
  - M11.6（autoDream 简化 + 接真 LLM）：✅ 已落地 2026-06-28
  - M11.7（SM 对齐 Claude Code SessionMemory）：✅ 已落地 2026-06-28

## docs 目录概览（28 个文档）
1. **记忆系统**（最核心）：memory-system-design.md（v2.3，3764 行）+ 专项设计 + superpowers/
2. **上下文管理**：context-management / token-estimation / claude-code-context 3 份
3. **会话管理**：session-features + session-impl-design
4. **工程规范**：开发规则汇总 + 架构设计坑点解析 + LangGraph 对比 + ReAct 对比
5. **工具安全沙箱**：tool/目录 3 份（permission / sandbox / security architecture）
6. **上线规划**：IMPLEMENTATION_PLAN.md（M1-M11）+ LAUNCH_v2.1.md + TODO.md
