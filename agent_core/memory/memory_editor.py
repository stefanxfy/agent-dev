"""
Edit-only 记忆编辑器（v2.1 §4.2）

M2 / Day 2 — L7 (source_quote 必填) + L9 (输出 sanitizer) + §14.4 secret scanner

设计要点：
1. **Edit-only**：不允许从零创建文件，只能 Edit 已有记忆
   - 工具白名单：edit_memory / read_memory（不暴露 create_memory / delete_memory）
   - 理由：让 LLM 不能凭空发明记忆，只能修补 / 合并已有事实
2. **L7**: edit 的 old_string 必须来自已有内容（基于 read_memory 的结果）
3. **L9**: 输出 sanitizer（防止 LLM 在记忆里注入工具调用 / 角色切换）
   - 5 个 pattern：
     a. `<tool_use>...</tool_use>` 标签（Anthropic 内部）
     b. `[TOOL_CALL]{...}[/TOOL_CALL]`（伪工具调用）
     c. `<function_calls>...<invoke name=...>`（OpenAI 风格）
     d. `{"role": "assistant", "tool_calls": [...]}` （JSON 内嵌 tool_calls）
     e. `<|im_start|>...<|im_end|>`（ChatML 角色切换）
4. **Secret scanner**（§14.4 必装）：4 基础 pattern
   - api_key / secret_key 等
   - sk- (OpenAI) / sk-ant- (Anthropic)
5. **路径沙箱**：调用 MemoryPathValidator

接口设计：
- MemoryEditor 是 Agent 工具层
- edit_memory(rel_path, old_string, new_string) → 工具描述（LLM 看）
- _sanitize(content) 内部使用（防 LLM 注入）
- _scan_secrets(content) 内部使用（防密钥泄漏）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Union

from agent_core.exceptions import StorageError, ToolError, ToolPermissionError
from agent_core.memory.memory_store import MemoryStore
from agent_core.memory.path_validator import MemoryPathValidator, PathSecurityError


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class MemoryEditError(ToolError):
    """记忆编辑失败"""
    code = "MEMORY_EDIT"


class SecretDetectedError(MemoryEditError):
    """检测到密钥写入（§14.4 必装）"""
    code = "SECRET_DETECTED"


class InjectionDetectedError(MemoryEditError):
    """检测到 LLM 注入（工具调用 / 角色切换）"""
    code = "INJECTION_DETECTED"


class EditPreconditionError(MemoryEditError):
    """old_string 不在文件中（防止凭空编辑）"""
    code = "EDIT_PRECONDITION"


# ──────────────────────────────────────────────────────────────────
# Sanitizer（L9）
# ──────────────────────────────────────────────────────────────────

# 5 个 LLM 注入 pattern
_INJECTION_PATTERNS: tuple[tuple[str, str, re.Pattern], ...] = (
    (
        "anthropic_tool_use",
        "<tool_use> 或 </tool_use> 标签（Anthropic 内部）",
        re.compile(r"</?tool_use\b", re.IGNORECASE),
    ),
    (
        "pseudo_tool_call",
        "[TOOL_CALL]{...}[/TOOL_CALL] 伪工具调用",
        re.compile(r"\[/?TOOL_CALL\]", re.IGNORECASE),
    ),
    (
        "openai_function_calls",
        "<function_calls><invoke name=...（OpenAI 风格）",
        re.compile(r"</?function_calls\b|<invoke\s+name=", re.IGNORECASE),
    ),
    (
        "openai_tool_calls_json",
        '{"role": "assistant", "tool_calls": [...] } 内嵌',
        re.compile(r'"tool_calls"\s*:\s*\[', re.IGNORECASE),
    ),
    (
        "chatml_role_switch",
        "<|im_start|>role 或 <|im_end|> 角色切换",
        re.compile(r"<\|im_(start|end)\|>", re.IGNORECASE),
    ),
)


def sanitize(content: str) -> str:
    """
    L9 输出 sanitizer

    Returns: 清理后的内容（剥除匹配的注入标记）

    Raises:
        InjectionDetectedError: 检测到注入（拒绝写入）
    """
    for name, desc, pattern in _INJECTION_PATTERNS:
        if pattern.search(content):
            raise InjectionDetectedError(
                f"检测到 LLM 注入 ({name}): {desc}。"
                f"记忆内容不允许含工具调用 / 角色切换标记。"
            )
    return content


# ──────────────────────────────────────────────────────────────────
# Secret scanner（§14.4）
# ──────────────────────────────────────────────────────────────────

_SECRET_PATTERNS: tuple[tuple[str, str, re.Pattern], ...] = (
    ("api_key",       r"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.compile(r"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("secret_key",    r"(?i)secret[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.compile(r"(?i)secret[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("openai_sk",     r"sk-[A-Za-z0-9]{20,}", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("anthropic_sk",  r"sk-ant-[A-Za-z0-9\-_]{20,}", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}")),
)


def scan_secrets(content: str) -> list[str]:
    """
    §14.4 基础密钥扫描

    Returns: 检测到的密钥 pattern 名列表（空 = 无）
    """
    found = []
    for name, _, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            found.append(name)
    return found


# ──────────────────────────────────────────────────────────────────
# MemoryEditor
# ──────────────────────────────────────────────────────────────────

class MemoryEditor:
    """
    Edit-only 记忆编辑器（v2.1 §4.2）

    用法:
        editor = MemoryEditor(store)

        # Agent 工具调用
        result = editor.edit_memory(
            rel_path="user/abc123.md",
            old_string="用户叫小明",
            new_string="用户叫小明（在 2026-06-20 改名，原名小红）",
        )

    拒绝:
        - 路径越界（→ PathSecurityError）
        - old_string 不在文件中（→ EditPreconditionError）
        - new_string 含 LLM 注入（→ InjectionDetectedError）
        - new_string 含密钥（→ SecretDetectedError）

    工具描述（暴露给 LLM 的 edit_memory）:
        见 tool_description() 方法
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        *,
        enable_secret_scanner: bool = True,
        enable_injection_sanitizer: bool = True,
    ):
        self.store = memory_store
        self.enable_secret_scanner = enable_secret_scanner
        self.enable_injection_sanitizer = enable_injection_sanitizer

    # ── 唯一暴露的工具 ────────────────────────────────────

    def edit_memory(
        self,
        rel_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """
        Edit 已有记忆（不支持创建新文件）

        Args:
            rel_path: 相对路径（必须已存在）
            old_string: 要替换的原文（必须存在于文件中）
            new_string: 替换后的内容
            replace_all: 是否替换所有出现（默认 False = 仅第一处）

        Returns:
            {"ok": True, "path": ..., "diff_summary": ...}

        Raises:
            PathSecurityError: 路径越界
            EditPreconditionError: old_string 不在文件中
            InjectionDetectedError: new_string 含 LLM 注入
            SecretDetectedError: new_string 含密钥
        """
        # 1. 路径校验（强制文件必须存在 —— Edit-only 语义）
        abs_path = self.store.validator.validate(rel_path, must_exist=True)

        # 2. 读文件
        try:
            content = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            raise MemoryEditError(f"读文件失败: {e}", cause=e)

        # 3. L7: old_string 必须存在
        occurrences = content.count(old_string)
        if occurrences == 0:
            raise EditPreconditionError(
                f"old_string 不在文件 {rel_path} 中，无法 Edit。"
                f"记忆系统 Edit-only，禁止凭空创造内容。"
            )

        if not replace_all and occurrences > 1:
            raise EditPreconditionError(
                f"old_string 在文件 {rel_path} 中出现 {occurrences} 次，"
                f"请用 replace_all=True 或更精确的 old_string"
            )

        # 4. L9: sanitizer + 密钥扫描 new_string
        if self.enable_injection_sanitizer:
            sanitize(new_string)  # 抛 InjectionDetectedError if match

        if self.enable_secret_scanner:
            secrets = scan_secrets(new_string)
            if secrets:
                raise SecretDetectedError(
                    f"new_string 含疑似密钥 ({', '.join(secrets)})，拒绝写入。"
                    f"如确需记录密钥，请用 reference 类指向 vault / 密钥管理器。"
                )

        # 5. 实际替换
        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        # 6. 原子写
        import os, tempfile
        fd, tmp_path = tempfile.mkstemp(
            dir=abs_path.parent, prefix=".edit.", suffix=".tmp"
        )
        try:
            os.write(fd, new_content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            os.replace(tmp_path, abs_path)
        except Exception as e:
            import contextlib
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise MemoryEditError(f"写文件失败: {e}", cause=e)

        return {
            "ok": True,
            "path": rel_path,
            "diff_summary": {
                "old_length": len(old_string),
                "new_length": len(new_string),
                "replaced_count": occurrences if replace_all else 1,
            },
        }

    def read_memory(self, rel_path: str) -> dict[str, Any]:
        """只读工具（暴露给 LLM 读记忆用）"""
        return self.store.read(rel_path)

    # ── LLM 工具描述 ─────────────────────────────────────

    def tool_descriptions(self) -> list[dict[str, Any]]:
        """
        返回给 LLM 的工具定义列表（OpenAI / Anthropic 格式）

        v2.1 §4.2 设计: 只暴露 edit_memory + read_memory，
        不暴露 create / delete，防止 LLM 凭空发明或删除记忆。
        """
        return [
            {
                "name": "edit_memory",
                "description": (
                    "Edit an existing memory file. The file MUST already exist; "
                    "you cannot create new memories from scratch. "
                    "Use read_memory first to see the current content, then call "
                    "edit_memory with the exact old_string you saw and the new_string to replace it. "
                    "Do NOT include tool calls, role-switching markers, or secrets in new_string."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "rel_path": {
                            "type": "string",
                            "description": "Relative path like 'user/<hash>.md'",
                        },
                        "old_string": {
                            "type": "string",
                            "description": "Exact substring to replace (must exist in file)",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement content. Must NOT contain tool calls, role markers, or secrets.",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "default": False,
                            "description": "Replace all occurrences (default: only first)",
                        },
                    },
                    "required": ["rel_path", "old_string", "new_string"],
                },
            },
            {
                "name": "read_memory",
                "description": "Read an existing memory file's content.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "rel_path": {
                            "type": "string",
                            "description": "Relative path like 'user/<hash>.md'",
                        },
                    },
                    "required": ["rel_path"],
                },
            },
        ]


__all__ = [
    "MemoryEditor",
    "MemoryEditError",
    "SecretDetectedError",
    "InjectionDetectedError",
    "EditPreconditionError",
    "sanitize",
    "scan_secrets",
]