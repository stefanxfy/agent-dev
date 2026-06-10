# Agent Dev — 自研 ReAct 循环学习项目

> 手写 ReAct 循环，理解 Agent 底层原理，对比 LangGraph 框架设计

## 项目结构

```
agent-dev/
├── agent_core/          ← 自研 ReAct（当前分支）
├── langgraph_agent/     ← LangGraph 重构版（待实现）
├── web/
│   ├── app.py           ← 自研版 UI
│   └── app_langgraph.py # LangGraph 版 UI（待实现）
└── requirements.txt
```

## 快速启动

```bash
# 安装依赖
pip install -r requirements.txt

# 复制环境变量
cp .env.example .env
# 编辑 .env，填入 API Key

# 启动自研 ReAct 版
streamlit run web/app.py
```

## 学习路径

- **Stage 1**：自研 ReAct 循环 ✅（Day 1-3 完成）
- **Stage 2**：用 LangGraph 重构 📋
- **Stage 3**：构建记忆系统 📋
- **Stage 4**：非 Docker 原生沙箱 📋

## 技术栈

- LLM：Anthropic Claude / OpenAI GPT / 智谱 GLM（Router 统一调用）
- Agent：手写 ReAct 循环（支持流式 thinking + tool_use）
- 工具：Calculator / Search（ToolRegistry 管理）
- UI：Streamlit
- 依赖：`anthropic` / `openai` / `pydantic` / `streamlit` / `python-dotenv`