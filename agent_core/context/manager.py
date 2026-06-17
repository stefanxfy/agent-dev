"""
ContextManager — 统一上下文管理器

职责边界：
- 监控 token 用量（通过 ContextBudgetManager）
- 触发压缩（通过 CompactOrchestrator）
- 不存储消息（复用 SessionStorage / Agent.messages）
- 不追踪状态（agent-dev 当前不需要）

LLMRouter.chat() 是同步生成器，本模块保持同步接口。
"""

from __future__ import annotations

import logging
from typing import Optional

from .budget import ContextBudgetManager, BudgetState
from .compact import CompactOrchestrator, CompactionResult
from .tokenizer import SimpleTokenCounter

logger = logging.getLogger("context.manager")


class ContextManager:
    """
    统一上下文管理器

    用法：
        cm = ContextManager(llm_router, model="glm-4")
        messages, result = cm.check_and_compact(messages)

    注意：ContextManager 不持有消息列表，消息由 Agent / SessionStorage 管理。
    ContextManager 只接收消息列表做"检查 → 压缩 → 返回新列表"。
    """

    def __init__(
        self,
        llm_router,
        model: str = "glm-4",
        token_counter=None,
    ):
        """
        Args:
            llm_router: LLMRouter 实例
            model: 模型名称（用于确定上下文窗口大小）
            token_counter: Token 计数器，默认用 SimpleTokenCounter
        """
        self.llm = llm_router
        self.model = model
        self.token_counter = token_counter or SimpleTokenCounter()
        self.budget = ContextBudgetManager(model, self.token_counter)
        self.compactor = CompactOrchestrator(
            llm_router=llm_router,
            budget_manager=self.budget,
            token_counter=self.token_counter,
        )

        # 统计
        self.compact_count = 0
        self.total_tokens_freed = 0

    def should_compact(self, messages: list[dict]) -> tuple[bool, str]:
        """检查是否需要压缩"""
        return self.budget.should_compact(messages)

    def set_baseline(self, input_tokens: int, message_count: int) -> None:
        """捕获 API usage 到增量估算基准"""
        self.budget.set_baseline(input_tokens, message_count)

    def invalidate_baseline(self) -> None:
        """使增量基准失效（压缩/session切换后调用）"""
        self.budget.invalidate_baseline()

    def get_usage_info(self, messages: list[dict]) -> dict:
        """获取 token 用量信息（用于 UI 显示）"""
        info = self.budget.get_usage_info(messages)
        info["compact_count"] = self.compact_count
        info["total_tokens_freed"] = self.total_tokens_freed
        return info

    def check_and_compact(
        self,
        messages: list[dict],
        parent_system: Optional[str] = None,
        parent_tools: Optional[list[dict]] = None,
        parent_messages: Optional[list[dict]] = None,
    ) -> tuple[list[dict], Optional[CompactionResult]]:
        """
        检查并在需要时执行压缩

        返回：(压缩后的消息列表, 压缩结果)
        如果不需要压缩，返回 (原消息, None)
        如果压缩失败，返回 (原消息, CompactionResult(success=False))

        Fork 模式参数透传给 CompactOrchestrator.compact()：
        - parent_system: 主 agent 的 system prompt（字节级一致）
        - parent_tools: 主 agent 的 tools schema
        """
        should, reason = self.budget.should_compact(messages)
        if not should:
            return messages, None

        logger.info(f"Auto-compact triggered: {reason}")
        result = self.compactor.compact(
            messages,
            parent_system=parent_system,
            parent_tools=parent_tools,
            parent_messages=parent_messages,
        )

        if result.success:
            self.compact_count += 1
            self.total_tokens_freed += result.tokens_freed
            self.budget.invalidate_baseline()  # 压缩后消息列表被改写，基准失效
            logger.info(result.summary_str())
            return result.compacted_messages, result
        else:
            logger.warning(f"Compact failed: {result.error}")
            return messages, result

    def force_compact(
        self,
        messages: list[dict],
        parent_system: Optional[str] = None,
        parent_tools: Optional[list[dict]] = None,
        parent_messages: Optional[list[dict]] = None,
    ) -> CompactionResult:
        """
        强制压缩（忽略预算检查）

        用于手动触发压缩的场景。
        """
        logger.info("Force compact requested")
        result = self.compactor.compact(
            messages,
            parent_system=parent_system,
            parent_tools=parent_tools,
            parent_messages=parent_messages,
        )

        if result.success:
            self.compact_count += 1
            self.total_tokens_freed += result.tokens_freed

        return result

    def get_stats(self) -> dict:
        """获取上下文管理统计"""
        return {
            "model": self.model,
            "total_budget": self.budget.total_budget,
            "compact_count": self.compact_count,
            "total_tokens_freed": self.total_tokens_freed,
            "consecutive_failures": self.budget.consecutive_failures,
        }
