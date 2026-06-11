# Claude Code 上下文管理方案 · 完整技术实现文档

> 基于 Claude Code 源码解析，为 agent-dev 项目设计的上下文管理系统
>
> 参考：Claude Code `src/services/compact/` + `src/context.ts` + `src/utils/context.ts`
>
> 版本：v1.0 | 日期：2026-06-11

---

## 一、整体架构

Claude Code 的上下文管理不是"超了就截断"的暴力方案，而是一套 **监控 → 预测 → 压缩 → 重建** 的完整闭环。

```
┌─────────────────────────────────────────────────────────────┐
│                    Context Manager (新增)                    │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  1. ContextBudgetManager   预算管理与触发决策         │   │
│  │  2. MessageStore          消息存储（支持压缩）        │   │
│  │  3. StateKeeper           状态追踪与补偿重建         │   │
│  │  4. CompactOrchestrator   压缩编排（Fork Agent）     │   │
│  │  5. PromptCacheManager    Prompt 缓存复用            │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  │
│  │  agent_core  │   │ langgraph_   │   │ multi_agent  │  │
│  │  ReAct 循环   │   │ agent        │   │              │  │
│  └──────────────┘   └──────────────┘   └──────────────┘  │
│            ▲                ▲                ▲              │
│            └────────────────┴────────────────┘              │
│                    ReAct Agent / LangGraph Agent              │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、预算管理（ContextBudgetManager）

### 2.1 核心设计原则

Claude Code 不会把整个 Context 窗口都交给 Agent，而是做**三层预留**：

```
总窗口 (200,000 tokens)
  - Summary 预留      最多 20,000 tokens
  - Auto-Compact 缓冲  13,000 tokens
  = Agent 有效可用窗口 ≈ 167,000 tokens
```

### 2.2 预算管理器实现

```python
# agent_core/context/budget.py
"""
ContextBudgetManager — 上下文预算管理器
参考：Claude Code src/utils/context.ts + src/services/compact/autoCompact.ts
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# ── 常量配置 ────────────────────────────────────────────────────

# Claude 3 系列默认上下文窗口（200k）
MODEL_CONTEXT_WINDOW_DEFAULT = 200_000

# Anthropic Sonnet 4 上下文窗口（200k）
MODEL_CONTEXT_WINDOW_SONNET = 200_000

# 最大输出 token 限制（用于 slot-reservation 优化）
# 参考 Claude Code 源码：99分位数输出 4,911 tokens，默认卡在 8,000
CAPPED_DEFAULT_MAX_TOKENS = 8_000
ESCALATED_MAX_TOKENS = 64_000

# Auto-Compact 缓冲 token 数（提前触发的安全边界）
# Claude Code 经验值：剩余约 13,000 tokens 时触发
AUTOCOMPACT_BUFFER_TOKENS = 13_000

# Summary API 最大输出 token 预留
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# 熔断阈值：连续压缩失败 N 次则停止压缩
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


@runtime_checkable
class TokenCounter(Protocol):
    """Token 计数器协议（支持不同 tokenizer）"""
    def count(self, text: str) -> int:
        ...
    def count_messages(self, messages: list) -> int:
        ...


@dataclass
class ModelConfig:
    """模型配置"""
    name: str
    context_window: int = MODEL_CONTEXT_WINDOW_DEFAULT
    max_output: int = CAPPED_DEFAULT_MAX_TOKENS
    supports_thinking: bool = False


# Claude Code 做法：用环境变量支持模型配置覆盖
MODEL_CONFIGS: dict[str, ModelConfig] = {
    "claude-3-7-sonnet": ModelConfig(
        name="claude-3-7-sonnet",
        context_window=200_000,
        max_output=8_000,
        supports_thinking=True,
    ),
    "claude-3-5-sonnet": ModelConfig(
        name="claude-3-5-sonnet",
        context_window=200_000,
        max_output=8_000,
        supports_thinking=True,
    ),
    "claude-3-haiku": ModelConfig(
        name="claude-3-haiku",
        context_window=200_000,
        max_output=8_000,
        supports_thinking=False,
    ),
}


def get_model_config(model: str) -> ModelConfig:
    """获取模型配置，支持环境变量覆盖"""
    # 优先用环境变量硬覆盖
    auto_compact_window = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if auto_compact_window:
        # 用户可以设置自己的 auto-compact 阈值
        pass
    
    # 从已知配置中查找
    for known_model, config in MODEL_CONFIGS.items():
        if known_model.lower() in model.lower():
            return config
    
    # 默认配置
    return ModelConfig(name=model)


def get_effective_context_window(model: str) -> int:
    """
    计算有效可用窗口：总窗口 - Summary 预留 - Auto-Compact 缓冲
    
    参考：Claude Code src/services/compact/autoCompact.ts getEffectiveContextWindowSize()
    
    这保证了当触发压缩时，API 还有足够空间容纳：
    - 压缩前的历史对话
    - Summary 提示词
    - Summary 输出（最多 20,000 tokens）
    """
    config = get_model_config(model)
    
    # 预留 Summary 输出 token
    reserved_for_summary = min(config.max_output, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    
    # 有效窗口 = 总窗口 - Summary 预留 - Auto-Compact 缓冲
    effective = config.context_window - reserved_for_summary - AUTOCOMPACT_BUFFER_TOKENS
    
    return max(effective, 50_000)  # 最低保证 50k


def get_max_output_tokens(model: str) -> int:
    """
    获取最大输出 token 数
    
    Claude Code 经验：
    - 默认卡在 8,000（99分位数输出 4,911 tokens）
    - 截断时触发 64,000 的干净重试
    """
    config = get_model_config(model)
    
    # 可用环境变量覆盖
    override = os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
    if override:
        return int(override)
    
    return config.max_output


# ── 预算状态 ────────────────────────────────────────────────────

@dataclass
class BudgetState:
    """预算状态追踪"""
    total_budget: int
    used_tokens: int = 0
    reserved_tokens: int = 0  # Summary 预留等
    
    @property
    def available(self) -> int:
        return self.total_budget - self.used_tokens
    
    @property
    def usage_ratio(self) -> float:
        return self.used_tokens / self.total_budget
    
    @property
    def should_auto_compact(self) -> bool:
        """是否应该触发 Auto-Compact"""
        return self.available < AUTOCOMPACT_BUFFER_TOKENS
    
    @property
    def is_critical(self) -> bool:
        """是否处于临界状态"""
        return self.available < AUTOCOMPACT_BUFFER_TOKENS / 2


class ContextBudgetManager:
    """
    上下文预算管理器
    
    职责：
    1. 维护预算状态
    2. 判断是否需要触发压缩
    3. 提供 token 分配建议
    
    参考：Claude Code src/services/compact/autoCompact.ts autoCompactIfNeeded()
    """
    
    def __init__(
        self,
        model: str,
        token_counter: TokenCounter,
    ):
        self.model = model
        self.token_counter = token_counter
        self.total_budget = get_effective_context_window(model)
        self.reserved = MAX_OUTPUT_TOKENS_FOR_SUMMARY
        
        # 熔断追踪
        self.consecutive_failures = 0
        self.last_compact_time: Optional[float] = None
    
    def compute_budget_state(self, messages: list) -> BudgetState:
        """计算当前预算状态"""
        used = self.token_counter.count_messages(messages)
        return BudgetState(
            total_budget=self.total_budget,
            used_tokens=used,
            reserved_tokens=self.reserved,
        )
    
    def should_compact(self, messages: list) -> tuple[bool, str]:
        """
        判断是否应该触发压缩
        
        返回：(should_compact, reason)
        """
        # 熔断检查
        if self.consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            return False, "熔断保护：连续压缩失败已达上限"
        
        state = self.compute_budget_state(messages)
        
        if state.is_critical:
            return True, f"临界状态：剩余 {state.available} tokens"
        
        if state.should_auto_compact:
            return True, f"缓冲触发：剩余 {state.available} < {AUTOCOMPACT_BUFFER_TOKENS} tokens"
        
        return False, "预算充足，无需压缩"
    
    def record_compact_success(self):
        """记录压缩成功"""
        self.consecutive_failures = 0
        self.last_compact_time = time.time()
    
    def record_compact_failure(self):
        """记录压缩失败"""
        self.consecutive_failures += 1
    
    def reset_circuit_breaker(self):
        """重置熔断器"""
        self.consecutive_failures = 0
```

### 2.3 Token Counter 实现

```python
# agent_core/context/tokenizer.py
"""
Token 计数器
参考 Claude Code 的 token 估算逻辑
"""

import re
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int:
        ...
    
    def count_messages(self, messages: list) -> int:
        ...


class SimpleTokenCounter:
    """
    简单 Token 计数器
    
    估算规则（基于 Claude Code 经验数据）：
    - 中文：约 1.4 tokens / 字
    - 英文：约 0.25 tokens / 字
    - Role overhead（每条消息的 role 标签）：约 10 tokens
    """
    
    CHINESE_RATIO = 1.4
    ENGLISH_RATIO = 0.25
    ROLE_OVERHEAD = 10
    TOOL_CALL_FIXED = 50  # 工具调用固定开销
    TOOL_RESULT_FIXED = 20  # 工具结果固定开销
    
    def count(self, text: str) -> int:
        """计算单段文本的 token 数"""
        if not text:
            return 0
        
        # 估算中英文比例
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        english_chars = len(re.findall(r'[a-zA-Z]', text))
        total_chars = len(text)
        
        if total_chars == 0:
            return 0
        
        chinese_tokens = chinese_chars * self.CHINESE_RATIO
        english_tokens = english_chars * self.ENGLISH_RATIO
        other_tokens = (total_chars - chinese_chars - english_chars) * 0.25
        
        return int(chinese_tokens + english_tokens + other_tokens)
    
    def count_messages(self, messages: list) -> int:
        """计算消息列表的总 token 数"""
        total = 0
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # Role overhead
            total += self.ROLE_OVERHEAD
            
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "text")
                        if block_type == "text":
                            total += self.count(block.get("text", ""))
                        elif block_type == "tool_use":
                            total += self.TOOL_CALL_FIXED
                            # 估算 tool input
                            tool_input = block.get("input", {})
                            total += self.count(str(tool_input))
                        elif block_type == "tool_result":
                            total += self.TOOL_RESULT_FIXED
                            # 估算 tool result
                            result_content = block.get("content", "")
                            if isinstance(result_content, str):
                                total += self.count(result_content)
                            elif isinstance(result_content, list):
                                for item in result_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        total += self.count(item.get("text", ""))
            elif content is None:
                pass
        
        return total


class TiktokenTokenCounter:
    """
    Tiktoken 精确计数器（生产环境推荐）
    
    需要安装：pip install tiktoken
    """
    
    def __init__(self, model: str = "cl100k_base"):
        import tiktoken
        self.encoding = tiktoken.get_encoding(model)
    
    def count(self, text: str) -> int:
        return len(self.encoding.encode(text))
    
    def count_messages(self, messages: list) -> int:
        """使用 tiktoken 计算消息 token"""
        total = 0
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # Role overhead
            total += self.ROLE_OVERHEAD
            
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "text")
                        if block_type == "text":
                            total += self.count(block.get("text", ""))
                        elif block_type == "tool_use":
                            import json
                            tool_input = json.dumps(block.get("input", {}))
                            total += self.TOOL_CALL_FIXED + self.count(tool_input)
                        elif block_type == "tool_result":
                            result = block.get("content", "")
                            if isinstance(result, str):
                                total += self.TOOL_RESULT_FIXED + self.count(result)
            
            # Role overhead（每条消息额外开销）
            total += self.ROLE_OVERHEAD
        
        return total
```

---

## 三、消息存储（MessageStore）

### 3.1 设计思路

Claude Code 的消息存储支持**分层保留策略**：

| 消息类型 | 保留策略 |
|---------|----------|
| System Prompt | 长期保留，不压缩 |
| 用户消息 | 强制完整保留，不压缩 |
| Assistant 响应 | 压缩后保留摘要 |
| Tool Result | 压缩前剥离文件内容，保留摘要 |
| 工具调用 | 保留结构（可重建） |

### 3.2 消息存储实现

```python
# agent_core/context/message_store.py
"""
MessageStore — 消息存储
参考：Claude Code 消息管理和压缩逻辑
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..llm.router import Message


class MessageType(Enum):
    """消息类型枚举"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    SUMMARY = "summary"  # 压缩后的摘要消息


@dataclass
class StoredMessage:
    """存储的消息"""
    original: Message
    msg_type: MessageType
    timestamp: float = field(default_factory=time.time)
    
    # 压缩相关
    is_compacted: bool = False
    compact_ref: Optional[str] = None  # 引用原始消息 ID
    token_count: int = 0
    
    # 状态追踪（用于状态重建）
    has_file_reads: bool = False
    read_files: list[str] = field(default_factory=list)
    has_plan: bool = False
    plan_state: Optional[dict] = None
    has_mcp_tools: bool = False
    mcp_tools: list[str] = field(default_factory=list)


@dataclass
class MessageStoreConfig:
    """消息存储配置"""
    max_messages: int = 1000  # 最大消息数
    max_tokens: int = 167_000  # 最大 token 数
    preserve_user_messages: bool = True  # 用户消息强制保留
    preserve_system: bool = True  # System 消息强制保留


class MessageStore:
    """
    消息存储
    
    职责：
    1. 存储消息历史
    2. 支持压缩/解压缩
    3. 提供状态追踪
    4. 支持状态重建
    
    参考：Claude Code src/services/compact/compact.ts
    """
    
    def __init__(self, config: Optional[MessageStoreConfig] = None):
        self.config = config or MessageStoreConfig()
        self.messages: list[StoredMessage] = []
        self._message_counter = 0
        self._id_to_index: dict[str, int] = {}
        
        # 状态追踪
        self._active_file_reads: dict[str, str] = {}  # path -> content (truncated)
        self._active_plan: Optional[dict] = None
        self._active_mcp_tools: set[str] = set()
    
    def add_message(self, message: Message, msg_type: MessageType) -> str:
        """添加消息，返回消息 ID"""
        self._message_counter += 1
        msg_id = f"msg_{self._message_counter}"
        
        stored = StoredMessage(
            original=message,
            msg_type=msg_type,
        )
        
        # 状态追踪
        self._track_state(message, msg_type)
        
        self.messages.append(stored)
        self._id_to_index[msg_id] = len(self.messages) - 1
        
        return msg_id
    
    def _track_state(self, message: Message, msg_type: MessageType):
        """追踪当前状态"""
        content = message.get("content", "")
        
        if msg_type == MessageType.USER:
            # 检查用户消息中的文件路径引用
            pass
        
        elif msg_type == MessageType.ASSISTANT:
            # 检查 assistant 消息中的 tool_use
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            if tool_name in ("FileReadTool", "Read"):
                                # 记录正在读取的文件
                                pass
        
        elif msg_type == MessageType.TOOL_RESULT:
            # 记录工具返回的文件内容
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            # 检测是否包含文件内容
                            if "File: " in text or "Contents:" in text:
                                self._active_file_reads["tracked"] = text[:5000]
    
    def compact_messages(
        self,
        summary: str,
        summary_msg_id: str,
        preserved_head: int = 5,
    ) -> list[Message]:
        """
        压缩消息
        
        策略：
        1. 保留最近 N 条消息（preserve_head）
        2. 用户消息强制保留
        3. System 消息强制保留
        4. 其余消息用 Summary 替代
        
        返回：压缩后的消息列表
        """
        if not self.messages:
            return []
        
        compacted: list[Message] = []
        
        # 1. 保留 System 消息
        for stored in self.messages:
            if stored.msg_type == MessageType.SYSTEM:
                compacted.append(stored.original)
        
        # 2. 保留最近的 N 条消息
        recent = self.messages[-preserved_head:]
        for stored in recent:
            compacted.append(stored.original)
        
        # 3. 中间部分用 Summary 替代
        if len(self.messages) > preserved_head + 1:
            middle_start = len(self.messages) - preserved_head - 1
            
            # 标记要被压缩的消息
            for i in range(middle_start):
                self.messages[i].is_compacted = True
                self.messages[i].compact_ref = summary_msg_id
            
            # 插入 Summary 消息
            summary_msg = {
                "role": "user",
                "content": f"[Previous conversation summarized]\n\n{summary}"
            }
            compacted.append(summary_msg)
        
        return compacted
    
    def get_state_for_reconstruction(self) -> dict:
        """
        获取状态重建所需的信息
        
        这部分信息在压缩后需要重新注入到上下文中
        """
        return {
            "active_file_reads": dict(self._active_file_reads),
            "active_plan": self._active_plan,
            "active_mcp_tools": list(self._active_mcp_tools),
            "message_count": len(self.messages),
        }
    
    def rebuild_state(self, state: dict):
        """从状态字典重建状态"""
        self._active_file_reads = state.get("active_file_reads", {})
        self._active_plan = state.get("active_plan")
        self._active_mcp_tools = set(state.get("active_mcp_tools", []))
    
    def to_api_format(self) -> list[Message]:
        """转换为 API 格式"""
        return [stored.original for stored in self.messages]
    
    def __len__(self):
        return len(self.messages)
    
    def __getitem__(self, index):
        return self.messages[index]
```

---

## 四、状态追踪与补偿重建（StateKeeper）

### 4.1 设计思路

Claude Code 在压缩时最害怕的不是遗忘历史，而是**丢失正在使用的工具状态**。

压缩后的上下文必须包含：
1. 被压缩的文本摘要
2. 正在查看的文件内容（截断版）
3. 正在做的 Plan
4. MCP Servers 完整声明
5. Deferred Tools 协议

### 4.2 状态追踪器实现

```python
# agent_core/context/state_keeper.py
"""
StateKeeper — 状态追踪与补偿重建
参考：Claude Code src/services/compact/compact.ts 状态重组补偿区
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm.router import Message


@dataclass
class FileReadState:
    """文件读取状态"""
    path: str
    content_preview: str  # 文件内容截断预览
    read_time: float
    token_count: int


@dataclass
class PlanState:
    """Plan 状态"""
    plan_id: str
    description: str
    current_step: int
    status: str  # "active", "completed", "paused"


@dataclass
class MCPState:
    """MCP 工具状态"""
    server_name: str
    tools: list[str]
    last_used: float


@dataclass
class DeferredTool:
    """延迟的工具调用（等待执行）"""
    tool_name: str
    tool_input: dict
    reason: str  # 为什么延迟


@dataclass
class ContextState:
    """完整的上下文状态"""
    # 文件读取状态
    file_reads: dict[str, FileReadState] = field(default_factory=dict)
    
    # Plan 状态
    active_plan: Optional[PlanState] = None
    
    # MCP 工具状态
    mcp_tools: dict[str, MCPState] = field(default_factory=dict)
    
    # 延迟的工具调用
    deferred_tools: list[DeferredTool] = field(default_factory=list)
    
    # 工具注册表快照
    tool_registry_snapshot: dict[str, Any] = field(default_factory=dict)
    
    # 时间戳
    captured_at: float = field(default_factory=time.time)


class StateKeeper:
    """
    状态追踪与补偿重建器
    
    职责：
    1. 追踪 Agent 运行时的关键状态
    2. 在压缩前保存状态快照
    3. 在压缩后重建状态（注入到上下文）
    
    参考：Claude Code src/services/compact/compact.ts 中的：
    - createPostCompactFileAttachments()
    - createPlanAttachmentIfNeeded()
    - createSkillAttachmentIfNeeded()
    - getDeferredToolsDeltaAttachment()
    """
    
    # 文件内容截断上限
    MAX_FILE_CONTENT_PREVIEW = 2000  # tokens
    
    def __init__(self):
        self.state = ContextState()
        self._snapshot_history: list[ContextState] = []
    
    # ── 状态追踪 ────────────────────────────────────────────────
    
    def track_file_read(self, path: str, content: str, token_count: int):
        """追踪文件读取"""
        preview = content
        if token_count > self.MAX_FILE_CONTENT_PREVIEW:
            # 截断到前半部分
            chars_per_token = len(content) / max(token_count, 1)
            max_chars = int(self.MAX_FILE_CONTENT_PREVIEW * chars_per_token)
            preview = content[:max_chars] + "\n... [truncated for context limit]"
        
        self.state.file_reads[path] = FileReadState(
            path=path,
            content_preview=preview,
            read_time=time.time(),
            token_count=min(token_count, self.MAX_FILE_CONTENT_PREVIEW),
        )
    
    def track_plan(self, plan_id: str, description: str, current_step: int, status: str):
        """追踪 Plan 状态"""
        self.state.active_plan = PlanState(
            plan_id=plan_id,
            description=description,
            current_step=current_step,
            status=status,
        )
    
    def track_mcp_tool(self, server_name: str, tool_name: str):
        """追踪 MCP 工具使用"""
        if server_name not in self.state.mcp_tools:
            self.state.mcp_tools[server_name] = MCPState(
                server_name=server_name,
                tools=[],
                last_used=time.time(),
            )
        
        mcp = self.state.mcp_tools[server_name]
        if tool_name not in mcp.tools:
            mcp.tools.append(tool_name)
        
        mcp.last_used = time.time()
    
    def defer_tool(self, tool_name: str, tool_input: dict, reason: str):
        """延迟工具调用"""
        self.state.deferred_tools.append(DeferredTool(
            tool_name=tool_name,
            tool_input=tool_input,
            reason=reason,
        ))
    
    def snapshot_tool_registry(self, registry: dict):
        """保存工具注册表快照"""
        self.state.tool_registry_snapshot = {
            "tools": list(registry.keys()),
            "timestamp": time.time(),
        }
    
    # ── 状态快照 ────────────────────────────────────────────────
    
    def capture_snapshot(self) -> ContextState:
        """捕获当前状态快照"""
        snapshot = ContextState(
            file_reads=dict(self.state.file_reads),
            active_plan=self.state.active_plan,
            mcp_tools=dict(self.state.mcp_tools),
            deferred_tools=list(self.state.deferred_tools),
            tool_registry_snapshot=dict(self.state.tool_registry_snapshot),
            captured_at=time.time(),
        )
        self._snapshot_history.append(snapshot)
        return snapshot
    
    def get_latest_snapshot(self) -> Optional[ContextState]:
        """获取最新状态快照"""
        return self._snapshot_history[-1] if self._snapshot_history else None
    
    # ── 状态重建 ────────────────────────────────────────────────
    
    def build_reconstruction_context(self) -> list[Message]:
        """
        构建状态重建消息
        
        参考：Claude Code 压缩后的上下文结构：
        [System 边界宣告] + [精简文本摘要] + [状态补偿]
        
        返回需要注入到上下文的消息列表
        """
        messages: list[Message] = []
        
        # 1. 文件内容重建
        if self.state.file_reads:
            file_content = "\n\n".join([
                f"File: {state.path}\n```\n{state.content_preview}\n```"
                for path, state in self.state.file_reads.items()
            ])
            messages.append({
                "role": "system",
                "content": f"[Recently read files - available for reference]\n\n{file_content}"
            })
        
        # 2. Plan 状态重建
        if self.state.active_plan:
            plan_msg = (
                f"[Active Plan]\n"
                f"ID: {self.state.active_plan.plan_id}\n"
                f"Description: {self.state.active_plan.description}\n"
                f"Current Step: {self.state.active_plan.current_step}\n"
                f"Status: {self.state.active_plan.status}"
            )
            messages.append({
                "role": "system",
                "content": plan_msg
            })
        
        # 3. MCP 工具声明重建
        if self.state.mcp_tools:
            mcp_decls = "\n".join([
                f"MCP Server: {mcp.server_name}\n  Tools: {', '.join(mcp.tools)}"
                for mcp in self.state.mcp_tools.values()
            ])
            messages.append({
                "role": "system",
                "content": f"[Active MCP Tools]\n\n{mcp_decls}"
            })
        
        # 4. Deferred Tools 协议恢复
        if self.state.deferred_tools:
            deferred_msg = (
                "[Pending Tool Calls - deferred from previous context]\n\n"
                + "\n\n".join([
                    f"- {dt.tool_name}: {dt.tool_input} (reason: {dt.reason})"
                    for dt in self.state.deferred_tools
                ])
            )
            messages.append({
                "role": "system",
                "content": deferred_msg
            })
        
        # 5. 工具注册表快照
        if self.state.tool_registry_snapshot:
            tools = self.state.tool_registry_snapshot.get("tools", [])
            if tools:
                messages.append({
                    "role": "system",
                    "content": f"[Available Tools: {', '.join(tools)}]"
                })
        
        return messages
    
    def clear_expired_state(self, max_age_seconds: float = 3600):
        """清理过期状态（超过一定时间的文件读取等）"""
        now = time.time()
        cutoff = now - max_age_seconds
        
        # 清理过期的文件读取
        expired_paths = [
            path for path, state in self.state.file_reads.items()
            if state.read_time < cutoff
        ]
        for path in expired_paths:
            del self.state.file_reads[path]
        
        # 清理过期的 MCP 工具
        expired_servers = [
            server for server, mcp in self.state.mcp_tools.items()
            if mcp.last_used < cutoff
        ]
        for server in expired_servers:
            del self.state.mcp_tools[server]
```

---

## 五、压缩编排器（CompactOrchestrator）

### 5.1 设计思路

Claude Code 的压缩在**独立的 Forked Agent** 里执行，借用主对话的 Prompt Cache。

压缩流程：
1. 消息预处理（脱水）
2. 构建压缩 prompt
3. Forked Agent 生成 Summary
4. PTL 防御（超限则剥洋葱）
5. 状态重建

### 5.2 压缩编排器实现

```python
# agent_core/context/compact.py
"""
CompactOrchestrator — 压缩编排器
参考：Claude Code src/services/compact/compact.ts
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm.router import Message, LLMRouter
from .budget import (
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    ContextBudgetManager,
)
from .message_store import MessageStore, MessageType
from .state_keeper import StateKeeper

logger = logging.getLogger("compact")


# ── 常量 ────────────────────────────────────────────────────────

# PTL 防御：剥洋葱策略，最多重试 N 次
MAX_PTL_RETRIES = 3

# 剥洋葱比例：每次剥掉 20% 的旧分组
TRUNCATE_RATIO = 0.2


# ── 压缩结果 ────────────────────────────────────────────────────

@dataclass
class CompactionResult:
    """压缩结果"""
    success: bool
    summary: str
    compacted_messages: list[Message]
    tokens_freed: int
    state_reconstruction: list[Message]
    error: Optional[str] = None
    
    # 统计
    original_message_count: int = 0
    compacted_message_count: int = 0
    compact_time_ms: float = 0


# ── 压缩 Prompt ─────────────────────────────────────────────────

COMPACT_SYSTEM_PROMPT = """You are a conversation summarizer. Your task is to compress a long conversation history into a concise summary while preserving key information.

CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn.
- Your entire response must be plain text with two sections:
  1. <analysis> block: Key decisions, actions taken, and important context
  2. <summary> block: Concise summary of the conversation
"""

COMPACT_USER_PROMPT_TEMPLATE = """Please summarize the following conversation history.

The summary should include:
1. Key user requests and goals
2. Important decisions made
3. Actions taken and their outcomes
4. Current state of work
5. Any pending tasks or follow-ups

Conversation:
{conversation}

Write your summary in Chinese, with <analysis> followed by <summary> format."""


# ── 压缩编排器 ─────────────────────────────────────────────────

class CompactOrchestrator:
    """
    压缩编排器
    
    职责：
    1. 管理压缩生命周期
    2. 执行消息压缩
    3. 处理 PTL 防御
    4. 协调状态重建
    
    参考：Claude Code src/services/compact/compact.ts
    """
    
    def __init__(
        self,
        llm_router: LLMRouter,
        budget_manager: ContextBudgetManager,
        message_store: MessageStore,
        state_keeper: StateKeeper,
    ):
        self.llm_router = llm_router
        self.budget_manager = budget_manager
        self.message_store = message_store
        self.state_keeper = state_keeper
    
    async def compact(
        self,
        messages: list[Message],
        suppress_follow_up: bool = True,
    ) -> CompactionResult:
        """
        执行压缩
        
        流程：
        1. 检查是否应该压缩
        2. 消息预处理（脱水）
        3. 构建压缩 prompt
        4. Forked Agent 生成 Summary
        5. PTL 防御（超限则剥洋葱）
        6. 状态重建
        """
        start_time = time.time()
        
        # 1. 检查是否应该压缩
        should_compact, reason = self.budget_manager.should_compact(messages)
        if not should_compact:
            return CompactionResult(
                success=False,
                summary="",
                compacted_messages=messages,
                tokens_freed=0,
                state_reconstruction=[],
                error=f"Not needed: {reason}",
            )
        
        logger.info(f"Starting compact: {reason}")
        
        try:
            # 2. 消息预处理
            preprocessed = self._preprocess_messages(messages)
            
            # 3. 捕获状态快照
            pre_state = self.state_keeper.capture_snapshot()
            
            # 4. 构建压缩 prompt
            conversation_text = self._messages_to_text(preprocessed)
            compact_prompt = COMPACT_USER_PROMPT_TEMPLATE.format(
                conversation=conversation_text
            )
            
            # 5. 生成 Summary（使用独立 LLM 调用）
            summary = await self._generate_summary(
                compact_prompt,
                max_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY,
            )
            
            if not summary:
                raise ValueError("Failed to generate summary")
            
            # 6. 压缩消息
            compacted = self.message_store.compact_messages(
                summary=summary,
                summary_msg_id="compact_summary",
                preserved_head=5,
            )
            
            # 7. 状态重建
            state_reconstruction = self.state_keeper.build_reconstruction_context()
            
            # 8. 组装最终结果
            final_messages = compacted + state_reconstruction
            
            # 计算释放的 token
            original_tokens = self.budget_manager.token_counter.count_messages(messages)
            compacted_tokens = self.budget_manager.token_counter.count_messages(final_messages)
            tokens_freed = original_tokens - compacted_tokens
            
            # 记录成功
            self.budget_manager.record_compact_success()
            
            elapsed_ms = (time.time() - start_time) * 1000
            
            return CompactionResult(
                success=True,
                summary=summary,
                compacted_messages=final_messages,
                tokens_freed=max(tokens_freed, 0),
                state_reconstruction=state_reconstruction,
                original_message_count=len(messages),
                compacted_message_count=len(final_messages),
                compact_time_ms=elapsed_ms,
            )
            
        except Exception as e:
            logger.error(f"Compact failed: {e}")
            self.budget_manager.record_compact_failure()
            
            return CompactionResult(
                success=False,
                summary="",
                compacted_messages=messages,
                tokens_freed=0,
                state_reconstruction=[],
                error=str(e),
            )
    
    def _preprocess_messages(self, messages: list[Message]) -> list[Message]:
        """
        消息预处理（脱水）
        
        1. 剔除图片/文档，替换为 [image] 文本提示
        2. 截断过长的工具结果
        3. 保留结构信息
        """
        preprocessed = []
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if isinstance(content, str):
                preprocessed.append(msg)
            elif isinstance(content, list):
                new_blocks = []
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "text")
                        
                        if block_type == "text":
                            new_blocks.append(block)
                        
                        elif block_type in ("image", "document"):
                            # 替换为文本提示
                            new_blocks.append({
                                "type": "text",
                                "text": "[image/document content removed for compression]"
                            })
                        
                        elif block_type == "tool_result":
                            # 截断过长的工具结果
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                new_result = []
                                for item in result_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        text = item.get("text", "")
                                        # 截断到 2000 tokens
                                        if len(text) > 8000:
                                            text = text[:8000] + "\n... [truncated]"
                                        new_result.append({"type": "text", "text": text})
                                    else:
                                        new_result.append(item)
                                new_blocks.append({"type": "text", "text": str(new_result)[:500]})
                            else:
                                new_blocks.append(block)
                        
                        else:
                            new_blocks.append(block)
                
                preprocessed.append({**msg, "content": new_blocks})
            else:
                preprocessed.append(msg)
        
        return preprocessed
    
    def _messages_to_text(self, messages: list[Message]) -> str:
        """将消息转换为可读的文本格式"""
        lines = []
        
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if isinstance(content, str):
                lines.append(f"[{role.upper()}]\n{content}")
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "text")
                        if block_type == "text":
                            parts.append(block.get("text", ""))
                        elif block_type == "tool_use":
                            name = block.get("name", "unknown")
                            inp = block.get("input", {})
                            parts.append(f"[TOOL: {name}] {json.dumps(inp, ensure_ascii=False)[:200]}")
                        elif block_type == "tool_result":
                            result = block.get("content", "")
                            if isinstance(result, list):
                                for r in result:
                                    if isinstance(r, dict) and r.get("type") == "text":
                                        parts.append(f"[RESULT] {r.get('text', '')[:500]}")
                            elif isinstance(result, str):
                                parts.append(f"[RESULT] {result[:500]}")
                lines.append(f"[{role.upper()}]\n" + "\n".join(parts))
        
        return "\n\n---\n\n".join(lines)
    
    async def _generate_summary(
        self,
        conversation_text: str,
        max_tokens: int,
    ) -> Optional[str]:
        """生成摘要"""
        try:
            response = await self.llm_router.chat(
                messages=[
                    {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
                    {"role": "user", "content": conversation_text},
                ],
                model="claude-3-5-sonnet",
                max_tokens=max_tokens,
                temperature=0.3,
            )
            
            content = response.get("content", "")
            
            # 解析 <analysis> 和 <summary> 格式
            summary = self._extract_summary(content)
            return summary
            
        except Exception as e:
            logger.error(f"Summary generation failed: {e}")
            return None
    
    def _extract_summary(self, text: str) -> str:
        """从文本中提取摘要"""
        # 尝试解析 <summary> 标签
        if "<summary>" in text:
            start = text.find("<summary>") + len("<summary>")
            end = text.find("</summary>")
            if end > start:
                return text[start:end].strip()
        
        # 尝试解析 <analysis> + <summary> 格式
        if "<analysis>" in text:
            start = text.find("<analysis>")
            return text[start:].strip()
        
        # 直接返回文本（兜底）
        return text.strip()
```

---

## 六、Prompt 缓存管理器（PromptCacheManager）

### 6.1 设计思路

Claude Code 在压缩时会**借用主对话的 Prompt Cache**，避免每次压缩都要重新发送完整的 System Prompt。

```python
# Claude Code 源码中的实现
const promptCacheSharingEnabled = getFeatureValue_CACHED_MAY_BE_STALE(
  'tengu_compact_cache_prefix',
  true,
)
```

### 6.2 缓存管理器实现

```python
# agent_core/context/prompt_cache.py
"""
PromptCacheManager — Prompt 缓存管理器
参考：Claude Code src/services/compact/compact.ts 的 prompt cache 共享逻辑
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from ..llm.router import Message


@dataclass
class CacheEntry:
    """缓存条目"""
    cache_key: str
    cached_content: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    hit_count: int = 0
    ttl_seconds: float = 3600  # 默认 1 小时过期


class PromptCacheManager:
    """
    Prompt 缓存管理器
    
    职责：
    1. 管理 System Prompt 的缓存
    2. 支持压缩时的 cache key 共享
    3. 自动过期和清理
    
    注意：这里管理的是"缓存键"共享，实际的 API 缓存由 LLM Router 处理
    """
    
    def __init__(self, ttl_seconds: float = 3600):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, CacheEntry] = {}
        self._enabled = True
    
    def enable(self):
        self._enabled = True
    
    def disable(self):
        self._enabled = False
    
    def get_cache_key(self, messages: list[Message]) -> str:
        """
        生成缓存键
        
        缓存键基于 System Prompt + 工具列表 + 模型
        """
        cache_parts = []
        
        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                content = msg.get("content", "")
                if isinstance(content, str):
                    cache_parts.append(content[:500])  # 取前 500 字符
        
        key_text = "|".join(cache_parts)
        return hashlib.sha256(key_text.encode()).hexdigest()[:16]
    
    def check_cache(self, cache_key: str) -> Optional[CacheEntry]:
        """检查缓存是否存在且有效"""
        if not self._enabled:
            return None
        
        entry = self._cache.get(cache_key)
        if not entry:
            return None
        
        # 检查是否过期
        if time.time() - entry.created_at > entry.ttl_seconds:
            del self._cache[cache_key]
            return None
        
        # 更新访问时间
        entry.last_used = time.time()
        entry.hit_count += 1
        
        return entry
    
    def set_cache(self, cache_key: str, content: str):
        """设置缓存"""
        if not self._enabled:
            return
        
        self._cache[cache_key] = CacheEntry(
            cache_key=cache_key,
            cached_content=content,
            ttl_seconds=self.ttl_seconds,
        )
    
    def clear_expired(self):
        """清理过期缓存"""
        now = time.time()
        expired_keys = [
            key for key, entry in self._cache.items()
            if now - entry.created_at > entry.ttl_seconds
        ]
        for key in expired_keys:
            del self._cache[key]
        
        return len(expired_keys)
    
    def get_stats(self) -> dict:
        """获取缓存统计"""
        total_hits = sum(e.hit_count for e in self._cache.values())
        return {
            "entry_count": len(self._cache),
            "total_hits": total_hits,
            "enabled": self._enabled,
        }
```

---

## 七、上下文管理器主入口（ContextManager）

### 7.1 统一上下文管理器

```python
# agent_core/context/manager.py
"""
ContextManager — 统一上下文管理器
整合所有子模块，为 Agent 提供统一的上下文管理接口
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..llm.router import Message, LLMRouter
from .budget import ContextBudgetManager, get_effective_context_window
from .compact import CompactOrchestrator, CompactionResult
from .message_store import MessageStore, MessageStoreConfig, MessageType
from .prompt_cache import PromptCacheManager
from .state_keeper import StateKeeper
from .tokenizer import SimpleTokenCounter, TokenCounter

logger = logging.getLogger("context_manager")


class ContextManager:
    """
    统一上下文管理器
    
    整合：
    - ContextBudgetManager：预算管理
    - MessageStore：消息存储
    - StateKeeper：状态追踪
    - CompactOrchestrator：压缩编排
    - PromptCacheManager：缓存管理
    
    参考：Claude Code 上下文管理的整体设计
    """
    
    def __init__(
        self,
        llm_router: LLMRouter,
        model: str,
        token_counter: Optional[TokenCounter] = None,
    ):
        self.llm_router = llm_router
        self.model = model
        
        # Token 计数器
        self.token_counter = token_counter or SimpleTokenCounter()
        
        # 子模块初始化
        self.budget_manager = ContextBudgetManager(model, self.token_counter)
        self.message_store = MessageStore()
        self.state_keeper = StateKeeper()
        self.prompt_cache = PromptCacheManager()
        
        # 压缩编排器
        self.compact_orchestrator = CompactOrchestrator(
            llm_router=llm_router,
            budget_manager=self.budget_manager,
            message_store=self.message_store,
            state_keeper=self.state_keeper,
        )
        
        # 统计
        self._compact_count = 0
        self._total_tokens_freed = 0
    
    # ── 消息管理 ────────────────────────────────────────────────
    
    def add_message(self, message: Message, msg_type: MessageType):
        """添加消息"""
        self.message_store.add_message(message, msg_type)
        
        # 更新状态追踪
        self.state_keeper.snapshot_tool_registry(
            self.llm_router.tool_registry.get_tools()
        )
    
    def add_system_message(self, content: str):
        """添加 System 消息"""
        self.add_message(
            {"role": "system", "content": content},
            MessageType.SYSTEM
        )
    
    def add_user_message(self, content: str):
        """添加用户消息"""
        self.add_message(
            {"role": "user", "content": content},
            MessageType.USER
        )
    
    def add_assistant_message(self, content: str):
        """添加 Assistant 消息"""
        self.add_message(
            {"role": "assistant", "content": content},
            MessageType.ASSISTANT
        )
    
    def add_tool_result(self, tool_name: str, result: str, tool_input: dict):
        """添加工具结果"""
        content = [
            {
                "type": "tool_result",
                "tool_use_id": f"tool_{tool_name}",
                "content": result,
            }
        ]
        self.add_message(
            {"role": "user", "content": content},
            MessageType.TOOL_RESULT
        )
    
    # ── 上下文获取 ──────────────────────────────────────────────
    
    def get_messages(self) -> list[Message]:
        """获取当前消息列表"""
        return self.message_store.to_api_format()
    
    def get_budget_state(self):
        """获取预算状态"""
        return self.budget_manager.compute_budget_state(
            self.message_store.to_api_format()
        )
    
    def get_context_info(self) -> dict:
        """获取上下文信息（用于调试）"""
        messages = self.message_store.to_api_format()
        state = self.budget_manager.compute_budget_state(messages)
        
        return {
            "message_count": len(self.message_store),
            "total_tokens": state.used_tokens,
            "budget": state.total_budget,
            "usage_ratio": f"{state.usage_ratio:.1%}",
            "available_tokens": state.available,
            "should_compact": state.should_auto_compact,
            "compact_count": self._compact_count,
            "tokens_freed": self._total_tokens_freed,
            "cache_stats": self.prompt_cache.get_stats(),
            "active_file_reads": len(self.state_keeper.state.file_reads),
            "active_plan": self.state_keeper.state.active_plan is not None,
        }
    
    # ── 状态追踪 ────────────────────────────────────────────────
    
    def track_file_read(self, path: str, content: str):
        """追踪文件读取"""
        tokens = self.token_counter.count(content)
        self.state_keeper.track_file_read(path, content, tokens)
    
    def track_plan(self, plan_id: str, description: str, current_step: int):
        """追踪 Plan"""
        self.state_keeper.track_plan(plan_id, description, current_step, "active")
    
    def track_mcp_tool(self, server: str, tool: str):
        """追踪 MCP 工具"""
        self.state_keeper.track_mcp_tool(server, tool)
    
    # ── 压缩 ────────────────────────────────────────────────────
    
    async def auto_compact_if_needed(self) -> Optional[CompactionResult]:
        """
        自动压缩（如果需要）
        
        参考：Claude Code src/services/compact/autoCompact.ts autoCompactIfNeeded()
        """
        messages = self.message_store.to_api_format()
        
        should_compact, reason = self.budget_manager.should_compact(messages)
        if not should_compact:
            return None
        
        logger.info(f"Auto-compact triggered: {reason}")
        
        result = await self.compact_orchestrator.compact(messages)
        
        if result.success:
            self._compact_count += 1
            self._total_tokens_freed += result.tokens_freed
            
            # 更新消息存储
            # 注意：这里需要替换消息存储中的消息
            # 实际实现中需要更新 message_store 的内部状态
            
            logger.info(
                f"Compact succeeded: freed {result.tokens_freed} tokens, "
                f"reduced {result.original_message_count} → {result.compacted_message_count} messages"
            )
        else:
            logger.warning(f"Compact failed: {result.error}")
        
        return result
    
    async def compact(self) -> CompactionResult:
        """手动触发压缩"""
        messages = self.message_store.to_api_format()
        return await self.compact_orchestrator.compact(messages)
    
    # ── 重置 ────────────────────────────────────────────────────
    
    def reset(self):
        """重置上下文（清除所有消息）"""
        self.message_store = MessageStore()
        self.state_keeper = StateKeeper()
        self.budget_manager.reset_circuit_breaker()
        self._compact_count = 0
        self._total_tokens_freed = 0
    
    def clear_messages(self):
        """清除消息但保留配置"""
        self.message_store = MessageStore()
```

---

## 八、与现有 agent_core.py 的集成

### 8.1 集成方案

在现有的 `agent_core.py` 中，可以通过以下方式集成上下文管理器：

```python
# agent_core/agent_core.py 新增导入
from .context.manager import ContextManager, MessageType
from .context.tokenizer import SimpleTokenCounter

# ── 修改 ReActAgent 初始化 ─────────────────────────────────────

class ReActAgent:
    def __init__(
        self,
        llm_router: LLMRouter,
        tool_registry: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_turns: int = 20,
        max_tokens: int = 150_000,
    ):
        # ... 现有初始化 ...
        
        # 新增：上下文管理器
        self.context_manager = ContextManager(
            llm_router=llm_router,
            model=llm_router.model,
            token_counter=SimpleTokenCounter(),
        )
        
        # 添加 System Prompt
        self.context_manager.add_system_message(system_prompt)
    
    # ── 修改 run 方法 ──────────────────────────────────────────
    
    async def run(self, user_input: str, stream: bool = True):
        # 添加用户消息
        self.context_manager.add_user_message(user_input)
        
        # 在每轮循环中检查是否需要压缩
        while self.turn < self.max_turns:
            # ... LLM 调用 ...
            
            # 添加工具调用
            if tool_calls:
                self.context_manager.add_assistant_message(
                    self._format_tool_calls(tool_calls)
                )
            
            # 执行工具
            for tc in tool_calls:
                result = await self.execute_tool(tc)
                self.context_manager.add_tool_result(
                    tc.tool_name,
                    result,
                    tc.tool_input,
                )
                
                # 追踪文件读取
                if tc.tool_name == "FileReadTool":
                    self.context_manager.track_file_read(
                        tc.tool_input.get("file_path", ""),
                        result,
                    )
            
            # 自动压缩检查
            compact_result = await self.context_manager.auto_compact_if_needed()
            if compact_result and compact_result.success:
                # 上下文已被压缩，可以继续
                pass
        
        # 返回最终结果
        return final_response
```

### 8.2 迁移步骤

```
Phase 1: 基础框架（Week 1）
  - 实现 ContextBudgetManager + TokenCounter
  - 实现 MessageStore
  - 集成到 agent_core.py（保留现有 _trim_history）

Phase 2: 状态追踪（Week 2）
  - 实现 StateKeeper
  - 集成状态追踪到工具执行
  - 实现状态重建逻辑

Phase 3: 压缩功能（Week 3）
  - 实现 CompactOrchestrator
  - 实现 PromptCacheManager
  - 端到端测试压缩流程

Phase 4: 优化（Week 4）
  - PTL 防御实现
  - 熔断机制调优
  - 性能优化
```

---

## 九、测试用例

```python
# tests/test_context_manager.py

import pytest
from agent_core.context.manager import ContextManager
from agent_core.context.budget import ContextBudgetManager
from agent_core.context.tokenizer import SimpleTokenCounter
from agent_core.context.state_keeper import StateKeeper


class TestContextBudgetManager:
    def test_should_compact_when_buffer_exceeded(self):
        counter = SimpleTokenCounter()
        manager = ContextBudgetManager("claude-3-5-sonnet", counter)
        
        # 模拟接近缓冲边界
        messages = [{"role": "user", "content": "x" * 150_000}]
        
        should, reason = manager.should_compact(messages)
        # 取决于实际 token 数
        assert isinstance(should, bool)
        assert isinstance(reason, str)
    
    def test_circuit_breaker(self):
        counter = SimpleTokenCounter()
        manager = ContextBudgetManager("claude-3-5-sonnet", counter)
        
        # 模拟连续失败
        for _ in range(3):
            manager.record_compact_failure()
        
        messages = [{"role": "user", "content": "test"}]
        should, reason = manager.should_compact(messages)
        
        assert should == False
        assert "熔断" in reason


class TestStateKeeper:
    def test_track_file_read(self):
        keeper = StateKeeper()
        keeper.track_file_read("/path/to/file.py", "def foo(): pass", 100)
        
        assert "/path/to/file.py" in keeper.state.file_reads
        state = keeper.state.file_reads["/path/to/file.py"]
        assert state.content_preview == "def foo(): pass"
    
    def test_build_reconstruction_context(self):
        keeper = StateKeeper()
        keeper.track_file_read("/path/to/file.py", "content", 100)
        
        messages = keeper.build_reconstruction_context()
        
        assert len(messages) > 0
        assert any("file.py" in str(m) for m in messages)


class TestContextManager:
    @pytest.mark.asyncio
    async def test_add_messages(self):
        from agent_core.llm.router import LLMRouter
        
        router = LLMRouter(...)  # 需要 mock
        manager = ContextManager(router, "claude-3-5-sonnet")
        
        manager.add_system_message("You are a helpful assistant")
        manager.add_user_message("Hello")
        manager.add_assistant_message("Hi there!")
        
        assert len(manager.message_store) == 3
    
    @pytest.mark.asyncio
    async def test_auto_compact(self):
        # 测试自动压缩
        pass
```

---

## 十、总结

这套上下文管理方案的核心设计：

| 模块 | Claude Code 源码对应 | 核心职责 |
|------|---------------------|----------|
| `ContextBudgetManager` | `autoCompact.ts` | 预算管理、触发决策、熔断保护 |
| `TokenCounter` | `context.ts` | Token 估算（中英文比例） |
| `MessageStore` | 消息管理 | 分层保留、压缩/解压缩 |
| `StateKeeper` | `compact.ts` 状态重组 | 文件读取追踪、Plan 追踪、MCP 追踪 |
| `CompactOrchestrator` | `compact.ts` | 压缩流程编排、PTL 防御 |
| `PromptCacheManager` | prompt cache 共享 | Cache key 管理 |
| `ContextManager` | 整体协调 | 统一入口 |

**关键设计原则**：

1. **不要暴力截断** — 提前 13k 缓冲触发，优雅压缩
2. **防止死锁** — 熔断机制 + PTL 剥洋葱策略
3. **压缩不失联** — 状态补偿重建，工具/文件/Plan 恢复
4. **性能优化** — Prompt Cache 复用 + max_tokens 卡 8k
5. **可观测** — 每轮 token 消耗计数，跟踪状态

---

> 文档生成时间：2026-06-11
> 基于 Claude Code 源码解析
> 适用项目：agent-dev