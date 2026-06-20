"""
记忆系统路径校验器（4 层防御）

M1 / Day 1 — O7 修复 + v2.1 §14 安全模型

设计要点：
1. 4 层防御（纵深防御）：
   - L1: 绝对路径检查（拒绝非相对路径，因为路径应该相对于 memory_root）
   - L2: normpath 防 .. 穿越
   - L3: 必须在 root 沙箱内（startswith + 防尾部追加 trick）
   - L4: Unicode trick 防 ‮ / 全角字符 / NFD 标准化绕过

2. 不抛通用 Exception，统一抛 PathSecurityError（agent_core.exceptions.StorageError 子类）
   —— 便于上层 try/except 分类处理

3. 设计为纯函数 + 极小实例方法（__init__ 只缓存 root 解析结果）
   —— 跨进程 / 跨线程共享安全

4. macOS / Linux / Windows 行为一致：
   - 不依赖 os.sep（用 pathlib）
   - normpath 自动处理 platform 差异
   - 区分大小写策略：Darwin 默认不区分、Linux 区分 —— 我们强制区分（更安全）
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Union

from agent_core.exceptions import StorageError


# ──────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────

class PathSecurityError(StorageError):
    """路径校验失败（v2.1 §14 安全模型）"""
    code = "PATH_SECURITY"


# ──────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────

# 合法文件后缀（防止 .py / .sh / .env 等敏感类型被写入 memory_dir）
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt", ".json", ".yaml", ".yml",
})

# 合法子目录（4 类 + meta）
_ALLOWED_TOP_DIRS: frozenset[str] = frozenset({
    "user", "feedback", "project", "reference", "meta",
})

# Unicode 危险字符（right-to-left override、零宽字符、全角点）
_FORBIDDEN_UNICODE: frozenset[int] = frozenset({
    0x202E,  # RIGHT-TO-LEFT OVERRIDE
    0x202D,  # RIGHT-TO-LEFT MARK
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE (BOM)
    0xFF0E,  # FULLWIDTH FULL STOP (．)
    0xFF0F,  # FULLWIDTH SOLIDUS (／)
    0xFF3C,  # FULLWIDTH REVERSE SOLIDUS (＼)
    0x2215,  # DIVISION SLASH (∕)
    0x2216,  # SET MINUS
})

# 文件名非法字符（Windows + POSIX 合集）
_FORBIDDEN_FILENAME_CHARS: frozenset[str] = frozenset({
    "<", ">", ":", "\"", "|", "?", "*", "\0",
})


# ──────────────────────────────────────────────────────────────────
# 校验器
# ──────────────────────────────────────────────────────────────────

class MemoryPathValidator:
    """
    记忆系统路径校验器（4 层防御）

    用法:
        validator = MemoryPathValidator(Path("~/.agent_data/memory"))
        real_path = validator.validate("user/foo.md")
        # → /Users/x/.agent_data/memory/user/foo.md

    拒绝:
        validator.validate("/etc/passwd")       # 绝对路径
        validator.validate("../../etc/passwd")  # 越界
        validator.validate("user/../user/x.md") # normpath 后仍在沙箱
        validator.validate("user\\u202ex.md")   # Unicode 绕过
        validator.validate("user/run.py")       # 禁止的扩展名
        validator.validate("admin/foo.md")      # 非法子目录
    """

    def __init__(self, memory_root: Union[str, Path]):
        # 解析 + 展开 ~ + 规范化（root 必须存在或可创建，不强制要求已存在）
        self.root = Path(memory_root).expanduser().resolve()

    def validate(self, rel_path: str, *, must_exist: bool = False) -> Path:
        """
        校验相对路径，返回解析后的绝对路径

        Args:
            rel_path: 相对路径（如 "user/foo.md"）
            must_exist: 是否要求文件必须存在（默认 False，写入场景）

        Returns:
            解析后的绝对路径（保证在 root 内）

        Raises:
            PathSecurityError: 任何一层防御失败
        """
        # L1: 拒绝绝对路径（含 Windows 盘符如 C:\、UNC 如 \\server）
        self._l1_reject_absolute(rel_path)

        # L4 (前置): Unicode trick 检查（必须在 normpath 前，否则 ‮ 被吃掉）
        self._l4_unicode_check(rel_path)

        # L2: normpath 标准化
        normalized = os.path.normpath(rel_path)
        # 拒绝 normpath 后的空（攻击者传 "..." 等）
        if normalized in ("", "."):
            raise PathSecurityError(f"路径 {rel_path!r} 解析后为空")

        # L2: 显式拒绝 normpath 后仍以 .. 开头的（防止 normpath 边界 case）
        if normalized.startswith("..") or "/.." in normalized or normalized == "..":
            raise PathSecurityError(f"路径 {rel_path!r} 含 .. 穿越")

        # L4 (中段): 文件名非法字符
        self._l4_filename_chars(normalized)

        # L3: 必须在 root 内（startswith 严格匹配 + 拒绝尾部追加 trick）
        candidate = (self.root / normalized).resolve()
        self._l3_within_sandbox(candidate)

        # 5. 子目录 + 后缀白名单
        self._check_allowed_subdir_and_ext(candidate)

        # 6. 必须存在检查
        if must_exist and not candidate.exists():
            raise PathSecurityError(f"文件不存在: {candidate}")

        return candidate

    # ── L1: 绝对路径检测 ───────────────────────────────────────

    def _l1_reject_absolute(self, path: str) -> None:
        """L1: 拒绝绝对路径（含 Windows 盘符 / UNC）"""
        if os.path.isabs(path):
            raise PathSecurityError(f"路径必须是相对路径，收到绝对路径: {path!r}")
        # Windows 盘符 (C:\, D:/ etc.)
        if re.match(r"^[A-Za-z]:[/\\]", path):
            raise PathSecurityError(f"路径禁止含 Windows 盘符: {path!r}")
        # UNC 路径 (\\server\share)
        if path.startswith("\\\\"):
            raise PathSecurityError(f"路径禁止 UNC 形式: {path!r}")

    # ── L3: 沙箱边界 ──────────────────────────────────────────

    def _l3_within_sandbox(self, candidate: Path) -> None:
        """L3: 解析后必须在 root 内"""
        root_str = str(self.root)
        candidate_str = str(candidate)
        # 强制区分大小写 + 严格前缀匹配
        # 防御 "root_evil/file" 类的尾部追加 trick
        if not candidate_str == root_str and not candidate_str.startswith(root_str + os.sep):
            raise PathSecurityError(
                f"路径越界: {candidate_str!r} 不在沙箱 {root_str!r} 内"
            )

    # ── L4: Unicode + 非法字符 ─────────────────────────────────

    def _l4_unicode_check(self, path: str) -> None:
        """L4: Unicode trick 检查（含 ‮ / 全角 / 零宽字符）"""
        for ch in path:
            if ord(ch) in _FORBIDDEN_UNICODE:
                raise PathSecurityError(
                    f"路径含禁止 Unicode 字符 U+{ord(ch):04X}: {path!r}"
                )
            # 全角字符 / 其他可疑分类
            if unicodedata.category(ch).startswith("Cf"):
                # Cf = Format category（含零宽、控制格式）
                raise PathSecurityError(
                    f"路径含 Format 类 Unicode 字符: {path!r}"
                )
        # NFD/NFC 标准化一致性检查（防止 macOS HFS+ 的 NFD bypass）
        nfc = unicodedata.normalize("NFC", path)
        nfd = unicodedata.normalize("NFD", path)
        if nfc != nfd and any(0x300 <= ord(c) <= 0x36F for c in path):
            # 含有组合用变音符号（NFD 才会拆开），可能是绕过尝试
            raise PathSecurityError(
                f"路径含组合变音符号，可能为 NFD 绕过: {path!r}"
            )

    def _l4_filename_chars(self, normalized: str) -> None:
        """L4: 文件名非法字符检查"""
        for part in PurePosixPath(normalized).parts:
            for ch in part:
                if ch in _FORBIDDEN_FILENAME_CHARS:
                    raise PathSecurityError(
                        f"路径含非法字符 {ch!r}: {normalized!r}"
                    )

    # ── 5. 白名单 ────────────────────────────────────────────

    def _check_allowed_subdir_and_ext(self, candidate: Path) -> None:
        """白名单：top-level 必须是 4 类 + meta；扩展名必须允许"""
        try:
            rel = candidate.relative_to(self.root)
        except ValueError as e:
            raise PathSecurityError(f"路径不在 root 下: {e}")
        parts = rel.parts
        if not parts:
            raise PathSecurityError(f"路径必须包含子目录: {candidate}")
        if parts[0] not in _ALLOWED_TOP_DIRS:
            raise PathSecurityError(
                f"非法子目录 {parts[0]!r}，必须为 "
                f"{'/'.join(sorted(_ALLOWED_TOP_DIRS))} 之一"
            )
        ext = candidate.suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            raise PathSecurityError(
                f"非法文件扩展名 {ext!r}，必须为 "
                f"{'/'.join(sorted(_ALLOWED_EXTENSIONS))} 之一"
            )

    # ── 工具方法 ──────────────────────────────────────────────

    def is_within_sandbox(self, abs_path: Union[str, Path]) -> bool:
        """判定 abs_path 是否在沙箱内（不抛异常，用于 audit / UI 提示）"""
        try:
            p = Path(abs_path).resolve()
            return str(p) == str(self.root) or str(p).startswith(str(self.root) + os.sep)
        except (OSError, RuntimeError):
            return False


__all__ = ["MemoryPathValidator", "PathSecurityError"]