"""
Permission Hook — PreToolUse hook 注册 + 并行执行

对齐 Claude Code:
- src/hooks/HookRegistry.ts(runPreToolUseHooks + 并行 + cancel)
- src/hooks/types.ts(HookResult / HookJSONOutput)
- doc §4.4 hook 系统 + §4.4.1 并行执行 + §4.4.2 updated_input 链式 merge

核心设计:
1. **Hook 签名**:(tool_name: str, tool_input: dict, context: ToolPermissionContext) -> PreToolUseResult
2. **并行执行**:concurrent.futures.ThreadPoolExecutor 跑所有 hook
3. **第一个 DENY 终止**(类似 short-circuit):后续 hook 不跑
4. **updated_input 链式 merge**:后 hook 的 updated_input 覆盖前 hook
5. **Hook 异常不阻断 pipeline**:catch + log + 跳过该 hook
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from .permission_types import (
    PermissionBehavior,
    ToolPermissionContext,
)


logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# PreToolUseResult — hook 返回值
# ────────────────────────────────────────────────────────────────────

@dataclass
class PreToolUseResult:
    """
    Hook 执行结果(对齐 CC PreToolUseResult + HookJSONOutput)

    字段:
    - behavior: 决策(allow / deny / ask)
    - updated_input: 改写后的 input(后 hook 覆盖前 hook)
    - additional_context: 额外 context 信息(拼到 system prompt)
    - prevent_continuation: True 时立即终止后续 hook
    - reason: 决策原因(给 UI 显示)
    - hook_name: 来源 hook 名(audit 用)
    - hook_source: 来源(settings.json source)
    """
    behavior: str = PermissionBehavior.ALLOW.value
    updated_input: Optional[dict] = None
    additional_context: Optional[str] = None
    prevent_continuation: bool = False
    reason: Optional[str] = None
    hook_name: Optional[str] = None
    hook_source: Optional[str] = None

    @classmethod
    def allow(cls, **kwargs) -> "PreToolUseResult":
        """工厂:ALLOW"""
        return cls(behavior=PermissionBehavior.ALLOW.value, **kwargs)

    @classmethod
    def deny(cls, reason: str = "", **kwargs) -> "PreToolUseResult":
        """工厂:DENY"""
        return cls(
            behavior=PermissionBehavior.DENY.value,
            reason=reason,
            **kwargs,
        )

    @classmethod
    def ask(cls, reason: str = "", **kwargs) -> "PreToolUseResult":
        """工厂:ASK"""
        return cls(
            behavior=PermissionBehavior.ASK.value,
            reason=reason,
            **kwargs,
        )


# ────────────────────────────────────────────────────────────────────
# Hook 签名类型
# ────────────────────────────────────────────────────────────────────

HookCallable = Callable[
    [str, dict, ToolPermissionContext],
    PreToolUseResult,
]
"""
Hook 函数签名:
  def my_hook(tool_name, tool_input, context) -> PreToolUseResult
"""


# ────────────────────────────────────────────────────────────────────
# _HookEntry — 注册的 hook
# ────────────────────────────────────────────────────────────────────

@dataclass
class _HookEntry:
    name: str
    event: str  # "PreToolUse" | "PostToolUse" | "Stop" | etc.
    callback: HookCallable
    source: Optional[str] = None  # settings source name(audit 用)


# ────────────────────────────────────────────────────────────────────
# HookRegistry — 注册 + 调度 hook
# ────────────────────────────────────────────────────────────────────

class HookRegistry:
    """
    Hook 注册表(对齐 CC HookRegistry)

    职责:
      1. 注册 / 注销 hook
      2. 按 event 触发(目前只实现 PreToolUse)
      3. 并行执行,第一个 DENY 终止
      4. 链式 merge updated_input
      5. 异常隔离(单 hook 抛异常不影响其他 hook)
    """

    def __init__(self, max_workers: int = 8):
        """
        Args:
            max_workers: ThreadPoolExecutor 最大并发数(M1 默认 8)
        """
        self._hooks: list[_HookEntry] = []
        self._lock = threading.Lock()
        self._max_workers = max_workers

    # ── 注册 API ──────────────────────────────────────────────

    def register_hook(
        self,
        event: str,
        name: str,
        callback: HookCallable,
        source: Optional[str] = None,
    ) -> None:
        """
        注册一个 hook

        Args:
            event: 事件名("PreToolUse" / "PostToolUse" / 等)
            name: hook 名(audit + 测试用)
            callback: HookCallable
            source: 来源(settings.json source,audit 用)
        """
        with self._lock:
            self._hooks.append(_HookEntry(
                name=name,
                event=event,
                callback=callback,
                source=source,
            ))

    def unregister_hook(self, name: str) -> bool:
        """
        注销 hook(按 name)

        Returns:
            True 如果删除了,False 如果未找到
        """
        with self._lock:
            for i, h in enumerate(self._hooks):
                if h.name == name:
                    del self._hooks[i]
                    return True
            return False

    def list_hooks(self, event: Optional[str] = None) -> list[str]:
        """列出 hook 名(可按 event 过滤)"""
        with self._lock:
            if event is None:
                return [h.name for h in self._hooks]
            return [h.name for h in self._hooks if h.event == event]

    def clear(self) -> None:
        """清空所有 hook(测试用)"""
        with self._lock:
            self._hooks.clear()

    # ── 触发 PreToolUse hook ──────────────────────────────────

    def run_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict,
        context: ToolPermissionContext,
    ) -> PreToolUseResult:
        """
        并行跑所有 PreToolUse hook(对齐 CC runPreToolUseHooks)

        行为:
          1. 收集所有 event="PreToolUse" 的 hook
          2. 用 ThreadPoolExecutor 并行跑
          3. 第一个 DENY 终止后续 hook(但已 running 的仍跑完)
          4. updated_input 链式 merge
          5. 任一 hook 抛异常 → log + 跳过该 hook
          6. prevent_continuation 触发 → 后续 hook 不提交

        Args:
            tool_name: 工具名
            tool_input: 工具输入
            context: 上下文

        Returns:
            聚合后的 PreToolUseResult
        """
        hooks = self._collect_hooks("PreToolUse")
        if not hooks:
            return PreToolUseResult.allow()

        # 共享状态(线程安全)
        first_deny: dict[str, PreToolUseResult] = {}  # 第一 DENY 优先
        cancel_event = threading.Event()
        merged_input: dict = dict(tool_input)  # 链式 merge

        def _run_hook(entry: _HookEntry) -> PreToolUseResult:
            """单 hook 执行 + 异常隔离"""
            try:
                # 同步执行(M1 简化,future 仍用 thread 包装)
                result = entry.callback(tool_name, dict(merged_input), context)
                result.hook_name = entry.name
                result.hook_source = entry.source
                return result
            except Exception as e:
                logger.warning(
                    "hook %s 抛异常,跳过: %s",
                    entry.name, e, exc_info=True,
                )
                return PreToolUseResult.allow(
                    hook_name=entry.name,
                    hook_source=entry.source,
                    reason=f"hook exception: {e}",
                )

        # 并行执行
        results: list[PreToolUseResult] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {}
            for hook in hooks:
                if cancel_event.is_set():
                    break  # prevent_continuation 触发,不再提交
                future = executor.submit(_run_hook, hook)
                futures[future] = hook

            for future in concurrent.futures.as_completed(futures):
                if cancel_event.is_set():
                    # prevent_continuation 已触发,不再处理后续结果
                    continue
                try:
                    result = future.result()
                except Exception as e:
                    # _run_hook 已 catch,这里兜底
                    logger.error("hook future 异常(不应该发生): %s", e)
                    continue
                results.append(result)

                # 链式 merge updated_input
                if result.updated_input is not None:
                    merged_input.update(result.updated_input)

                # DENY 短路
                if result.behavior == PermissionBehavior.DENY.value:
                    first_deny.setdefault("result", result)
                    cancel_event.set()

                # prevent_continuation
                if result.prevent_continuation:
                    cancel_event.set()

        # 决定最终结果
        if first_deny:
            deny_result = first_deny["result"]
            if deny_result.updated_input is not None:
                merged_input.update(deny_result.updated_input)
            return replace(
                deny_result,
                updated_input=merged_input if any(r.updated_input for r in results) else None,
            )

        # 无 DENY:如果有 ASK → ASK;否则 ALLOW
        ask_results = [r for r in results if r.behavior == PermissionBehavior.ASK.value]
        if ask_results:
            # 取第一个 ASK(M1 简化:用 first)
            primary = ask_results[0]
            return replace(
                primary,
                updated_input=merged_input if any(r.updated_input for r in results) else None,
            )

        # 全 ALLOW:返回 ALLOW(带 merged_input + hook metadata)
        allow_results = [r for r in results if r.behavior == PermissionBehavior.ALLOW.value]
        if allow_results:
            primary = allow_results[0]
            return PreToolUseResult(
                behavior=PermissionBehavior.ALLOW.value,
                updated_input=merged_input if any(r.updated_input for r in results) else None,
                hook_name=primary.hook_name,
                hook_source=primary.hook_source,
                reason=primary.reason,
            )
        return PreToolUseResult.allow(
            updated_input=merged_input if any(r.updated_input for r in results) else None,
        )

    # ── 内部 helper ──────────────────────────────────────────

    def _collect_hooks(self, event: str) -> list[_HookEntry]:
        """snapshot 当前注册的所有 event hook(避免并发问题)"""
        with self._lock:
            return [h for h in self._hooks if h.event == event]

    # ── PermissionRequest hook(M3 Task 2)─────────────────────

    def run_permission_request(
        self,
        tool_name: str,
        tool_input: dict,
        context: ToolPermissionContext,
    ) -> "PermissionRequestResult":
        """
        跑 PermissionRequest hook(对齐 CC PermissionRequest hook + doc §4.4)

        场景:ASK 决策时,给外部决策来源(webhook / Slack / 钉钉)一个决策机会。
        后台 agent(should_avoid_permission_prompts=True)无法弹 UI 时尤其有用。

        与 PreToolUse 不同:
        - 串行跑(不并行 — 外部决策来源通常只有一个 webhook)
        - 第一个返 has_decision 的 hook 胜出(短路)
        - 都没决策 → 返 PermissionRequestResult()(decision=None,走默认 UI)

        Args:
            tool_name: 工具名
            tool_input: 工具输入
            context: 权限上下文

        Returns:
            PermissionRequestResult(decision=None 表示走默认 UI)
        """
        hooks = self._collect_hooks("PermissionRequest")
        if not hooks:
            return PermissionRequestResult()

        merged_input = dict(tool_input)
        for entry in hooks:
            try:
                result = entry.callback(tool_name, dict(merged_input), context)
                # 兼容 PermissionRequestResult 和 PreToolUseResult(旧签名)
                decision = getattr(result, "decision", None) or getattr(result, "behavior", None)
                if decision is not None:
                    reason = getattr(result, "reason", None)
                    updated = getattr(result, "updated_input", None)
                    if updated:
                        merged_input.update(updated)
                    return PermissionRequestResult(
                        decision=decision,
                        reason=reason,
                        updated_input=merged_input if updated else None,
                    )
            except Exception as e:
                logger.warning(
                    "PermissionRequest hook %s 异常,跳过: %s", entry.name, e,
                )
                continue
        return PermissionRequestResult()

    # ── PermissionDenied hook(M3 Task 3)───────────────────────

    def run_permission_denied(
        self,
        tool_name: str,
        tool_input: dict,
        context: ToolPermissionContext,
        decision: Any,
    ) -> "PermissionDeniedResult":
        """
        跑 PermissionDenied hook(对齐 CC PermissionDenied hook + doc §4.4)

        场景:tool 被 deny 后,给模型一个"为什么被拒 + 怎么换种方式重试"的上下文。
        CC 的 PermissionDenied hook 返 retry: true 时,模型收到提示后重试。

        串行跑所有 hook,聚合 retry_prompt / notify_message / additional_context。
        任何 hook 抛异常 → 跳过,不阻断。

        Args:
            tool_name: 工具名
            tool_input: 工具输入
            context: 权限上下文
            decision: 导致 deny 的 PermissionDecision(含 reason)

        Returns:
            PermissionDeniedResult(聚合所有 hook 输出)
        """
        hooks = self._collect_hooks("PermissionDenied")
        if not hooks:
            return PermissionDeniedResult()

        retry_parts: list[str] = []
        notify_parts: list[str] = []
        context_parts: list[str] = []
        for entry in hooks:
            try:
                # 兼容 4 参(含 decision)和 3 参(旧签名)hook
                try:
                    result = entry.callback(tool_name, tool_input, context, decision)
                except TypeError:
                    result = entry.callback(tool_name, tool_input, context)
                retry = getattr(result, "retry_prompt", None)
                notify = getattr(result, "notify_message", None) or getattr(result, "reason", None)
                addl = getattr(result, "additional_context", None)
                if retry:
                    retry_parts.append(retry)
                if notify:
                    notify_parts.append(notify)
                if addl:
                    context_parts.append(addl)
            except Exception as e:
                logger.warning(
                    "PermissionDenied hook %s 异常,跳过: %s", entry.name, e,
                )
                continue
        return PermissionDeniedResult(
            retry_prompt="\n".join(retry_parts) if retry_parts else None,
            notify_message="\n".join(notify_parts) if notify_parts else None,
            additional_context="\n".join(context_parts) if context_parts else None,
        )


# ────────────────────────────────────────────────────────────────────
# PermissionRequestResult — 后台 agent 外部决策(M3 Task 2)
# ────────────────────────────────────────────────────────────────────

@dataclass
class PermissionRequestResult:
    """
    PermissionRequest hook 返回值(对齐 CC PermissionRequest hook + doc §4.4)

    场景:后台 agent(should_avoid_permission_prompts=True)无法弹 UI 时,
    给外部决策来源(webhook / Slack / 钉钉)一个决策机会。

    字段:
    - decision: "allow" / "deny" / "ask"(None = 未决策,走默认 UI 路径)
    - reason: 决策原因(audit + UI 显示)
    - updated_input: hook 改写后的 input(可选)
    """
    decision: Optional[str] = None
    reason: Optional[str] = None
    updated_input: Optional[dict] = None

    @property
    def has_decision(self) -> bool:
        """True 表示 hook 给出了决策(调用方应直接用,不走 UI)"""
        return self.decision is not None


# ────────────────────────────────────────────────────────────────────
# Default hooks — 预置 hook
# ────────────────────────────────────────────────────────────────────

def default_secret_hook(
    tool_name: str,
    tool_input: dict,
    context: ToolPermissionContext,
) -> PreToolUseResult:
    """
    预置:secret 检测 hook(命中 secret → ASK)

    Args:
        tool_name: 工具名
        tool_input: 工具输入
        context: 上下文

    Returns:
        ASK 如果命中 secret;否则 ALLOW
    """
    from .safety_check import contains_secret, _SECRET_CHECK_TOOLS

    if tool_name not in _SECRET_CHECK_TOOLS:
        return PreToolUseResult.allow(hook_name="default_secret")

    for value in tool_input.values():
        if isinstance(value, str) and contains_secret(value):
            return PreToolUseResult.ask(
                reason=f"secret pattern detected in {tool_name} input",
                hook_name="default_secret",
            )
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for v in item.values():
                        if isinstance(v, str) and contains_secret(v):
                            return PreToolUseResult.ask(
                                reason="secret pattern detected in content list",
                                hook_name="default_secret",
                            )
                elif isinstance(item, str) and contains_secret(item):
                    return PreToolUseResult.ask(
                        reason="secret pattern detected in list item",
                        hook_name="default_secret",
                    )
        elif isinstance(value, dict):
            for v in value.values():
                if isinstance(v, str) and contains_secret(v):
                    return PreToolUseResult.ask(
                        reason="secret pattern detected in nested dict",
                        hook_name="default_secret",
                    )
    return PreToolUseResult.allow(hook_name="default_secret")


def default_path_validation_hook(
    tool_name: str,
    tool_input: dict,
    context: ToolPermissionContext,
) -> PreToolUseResult:
    """
    预置:路径验证 hook(敏感路径 → DENY)

    Args:
        tool_name: 工具名
        tool_input: 工具输入
        context: 上下文

    Returns:
        DENY 如果命中敏感路径;否则 ALLOW
    """
    from .safety_check import is_sensitive_path, _PATH_CHECK_TOOLS

    if tool_name not in _PATH_CHECK_TOOLS:
        return PreToolUseResult.allow(hook_name="default_path")

    path = tool_input.get("path") or tool_input.get("file_path") or ""
    if path and is_sensitive_path(path):
        return PreToolUseResult.deny(
            reason=f"sensitive path detected: {path}",
            hook_name="default_path",
        )
    return PreToolUseResult.allow(hook_name="default_path")


def default_hooks() -> HookRegistry:
    """
    创建带预置 hook 的 HookRegistry

    Returns:
        HookRegistry with default_secret + default_path hooks
    """
    registry = HookRegistry()
    registry.register_hook(
        event="PreToolUse",
        name="default_secret",
        callback=default_secret_hook,
        source="builtin",
    )
    registry.register_hook(
        event="PreToolUse",
        name="default_path",
        callback=default_path_validation_hook,
        source="builtin",
    )
    return registry


# ────────────────────────────────────────────────────────────────────
# PermissionDeniedResult — deny 后 retry 提示(M3 Task 3)
# ────────────────────────────────────────────────────────────────────

@dataclass
class PermissionDeniedResult:
    """
    PermissionDenied hook 返回值(对齐 CC PermissionDenied hook + doc §4.4)

    场景:tool 被 deny 后,给模型一个"为什么被拒 + 怎么换种方式重试"的上下文。
    CC 的 PermissionDenied hook 返 retry: true 时,模型收到提示后重试。

    字段:
    - retry_prompt: 追加到 tool_result error message 的重试提示(给模型看)
    - notify_message: 给用户的通知(可选,UI 显示)
    - additional_context: 额外上下文(拼到 system prompt)
    """
    retry_prompt: Optional[str] = None
    notify_message: Optional[str] = None
    additional_context: Optional[str] = None

    @property
    def has_content(self) -> bool:
        """True 表示至少有一个字段非空"""
        return any([self.retry_prompt, self.notify_message, self.additional_context])


# ────────────────────────────────────────────────────────────────────
# PermissionRequest webhook factory(M3 Task 2,示例用,不自动注册)
# ────────────────────────────────────────────────────────────────────

def make_webhook_permission_request_hook(webhook_url: str) -> HookCallable:
    """
    创建 webhook PermissionRequest hook(对齐 CC 外部决策来源 + doc §4.4)

    POST tool_use 信息到 webhook_url,期望返:
        {"decision": "allow"/"deny"/"ask", "reason": "..."}
    webhook 超时/失败 → 返 decision=None(走默认 UI,不阻断,对齐 CC fail-open)

    用户在 settings.json 配置 webhook URL 后,由 web/app.py 注册本 hook。
    默认不预置(YAGNI + 安全:不强制外发)。

    Args:
        webhook_url: 外部决策 webhook URL

    Returns:
        HookCallable(签名与 PreToolUse 一致,返 PermissionRequestResult)
    """
    def hook(
        tool_name: str,
        tool_input: dict,
        context: ToolPermissionContext,
    ) -> PermissionRequestResult:
        import requests
        try:
            resp = requests.post(
                webhook_url,
                json={"tool_name": tool_name, "tool_input": tool_input},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            decision = data.get("decision")
            if decision in ("allow", "deny", "ask"):
                return PermissionRequestResult(
                    decision=decision,
                    reason=data.get("reason", "webhook decision"),
                )
        except Exception as e:
            logger.warning("webhook permission request 失败,走默认 UI: %s", e)
        return PermissionRequestResult()
    return hook


# ────────────────────────────────────────────────────────────────────
# PermissionDenied 示例 hook factory(M3 Task 3,演示用,不自动注册)
# ────────────────────────────────────────────────────────────────────

def make_retry_hint_denied_hook() -> HookCallable:
    """
    示例 PermissionDenied hook:deny 后给通用 retry 提示

    CC 的 PermissionDenied hook 常见用法:告诉模型"换种更安全的方式重试"。
    本 hook 是示例,实际项目可注册自定义 webhook / 日志 hook。

    签名兼容 4 参(tool_name, tool_input, context, decision)和 3 参(忽略 decision)。

    Returns:
        HookCallable
    """
    def hook(tool_name: str, tool_input: dict, context: ToolPermissionContext,
             decision: Any = None) -> PermissionDeniedResult:
        return PermissionDeniedResult(
            retry_prompt=(
                f"被拒绝的工具 `{tool_name}` — 尝试换种更安全的方式,"
                f"或请用户在 /permissions 页面手动放行。"
            ),
            notify_message=f"工具 {tool_name} 被权限系统拒绝",
        )
    return hook
