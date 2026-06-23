"""
密钥扫描器（v2.1 §14.4）

M3 / Day 3 — L8 修复

设计要点：
1. **4 个核心 pattern**：覆盖最常见的密钥泄露场景
   - api_key / secret_key (直接命名)
   - sk- / sk-ant- (OpenAI / Anthropic 风格)
2. **检测 + 拦截**两阶段：
   - scan() → 标记 hit 但不抛（用于审计 / 报告）
   - assert_clean() → 发现 hit 立即抛 SecretDetectedError（用于写入 / 编辑）
3. **零误杀原则**：宁可漏检也不误报（用户体验优先）
   - 不扫描注释 / 文档示例
   - 不扫描占位符 (sk-xxx / sk-YOUR_KEY)
   - 长度阈值：实际密钥 ≥ 20 字符
4. **不引入新依赖**：纯 re

触发点：
- MemoryEditor.edit_memory 写入 new_string 前
- Channel B 提取 LLM 输出后（防止 LLM 误把 key 写到 memory 里）
- Cold start 加载外部文件时
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# SecretDetectedError 实际定义在 memory_editor.py(M2 阶段)
# 避免循环依赖：直接 re-export,保证一致性
from agent_core.memory.memory_editor import SecretDetectedError


# ──────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────

@dataclass
class SecretHit:
    """单条密钥命中"""
    pattern_name: str      # 哪个 pattern 命中的
    match: str             # 命中片段（可能含 key 本体，已 mask）
    line: int              # 行号（1-based）
    span: tuple[int, int]  # (start, end) 列号 (0-based)


@dataclass
class ScanResult:
    """扫描结果"""
    hits: list[SecretHit] = field(default_factory=list)
    scanned_chars: int = 0

    @property
    def is_clean(self) -> bool:
        return len(self.hits) == 0

    def __bool__(self) -> bool:
        return self.is_clean

    def summary(self) -> str:
        if self.is_clean:
            return f"clean ({self.scanned_chars} chars scanned)"
        return f"{len(self.hits)} hit(s): " + ", ".join(
            f"{h.pattern_name}@L{h.line}" for h in self.hits
        )


# ──────────────────────────────────────────────────────────────────
# Pattern 定义
# ──────────────────────────────────────────────────────────────────

# 命名型：key=value / key: value / key "value"
# 允许空白 + 引号(单/双)
_NAMED_KEY_RE = re.compile(
    r"""
    \b(
        api[_-]?key         |
        secret[_-]?key       |
        access[_-]?key       |
        auth[_-]?token       |
        password             |
        passwd               |
        token
    )
    \s*[:=]\s*
    (['"]?)([^\s'"]{4,})\2
    """,
    re.VERBOSE | re.IGNORECASE,
)

# OpenAI 风格: sk-...  (48 字符左右)
_OPENAI_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")

# Anthropic 风格: sk-ant-...
_ANTHROPIC_SK_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")

# AWS Access Key: AKIA / ASIA 开头 20 字符
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")

# GitHub Token: ghp_ / gho_ / ghs_ / ghu_ 开头
_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")

# 占位符白名单(常见示例,绝不应该被当密钥)
_PLACEHOLDER_TOKENS = {
    "sk-xxx", "sk-your-key", "sk-placeholder",
    "sk-ant-xxx", "sk-ant-your-key", "sk-ant-placeholder",
    "your-api-key", "your_secret_key", "<your-key>",
    "your-api-key-here", "xxx", "xxxx", "example",
    "changeme", "todo", "fixme",
}

# 排除纯示例前缀的辅助函数
def _is_placeholder(s: str) -> bool:
    s_lower = s.lower().strip("\"'`<>")
    if s_lower in _PLACEHOLDER_TOKENS:
        return True
    # 全部是相同字符 (xxxxxx / aaaaaa)
    if len(set(s_lower.replace("-", ""))) <= 2:
        return True
    # 含 "example" / "sample" / "demo"
    if any(t in s_lower for t in ("example", "sample", "demo", "test-key")):
        return True
    return False


def _mask(s: str, keep: int = 4) -> str:
    """遮蔽密钥: 只保留前 keep 字符 + 省略号"""
    if len(s) <= keep * 2:
        return s[:keep] + "***"
    return s[:keep] + "***" + s[-keep:]


# ──────────────────────────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────────────────────────

class SecretScanner:
    """
    密钥扫描器

    用法：
        scanner = SecretScanner()

        # 1. 报告模式
        result = scanner.scan("my api_key = sk-abc123...")
        if not result:
            print(result.summary())

        # 2. 拦截模式（抛异常）
        scanner.assert_clean("my api_key = sk-abc123...")
    """

    # 4 个 pattern 名(对外暴露)
    PATTERNS = ("named_key", "openai_sk", "anthropic_sk", "github_token")

    def __init__(
        self,
        enable_named_key: bool = True,
        enable_openai_sk: bool = True,
        enable_anthropic_sk: bool = True,
        enable_aws_key: bool = True,
        enable_github_token: bool = True,
    ):
        self.enable_named_key = enable_named_key
        self.enable_openai_sk = enable_openai_sk
        self.enable_anthropic_sk = enable_anthropic_sk
        self.enable_aws_key = enable_aws_key
        self.enable_github_token = enable_github_token

    def scan(self, text: str) -> ScanResult:
        """
        扫描文本,返回 ScanResult(不抛异常)

        - text: 待扫描的字符串
        - 返回: ScanResult, 包含所有命中
        """
        result = ScanResult(scanned_chars=len(text))
        if not text:
            return result

        # 逐行扫描(便于报告行号)
        for lineno, line in enumerate(text.splitlines(), start=1):
            hits = self._scan_line(line, lineno)
            result.hits.extend(hits)
        return result

    def _scan_line(self, line: str, lineno: int) -> list[SecretHit]:
        """单行扫描,返回 hit 列表"""
        hits: list[SecretHit] = []
        offset = 0  # 当前行在原文中的列偏移

        # 1) 命名型
        if self.enable_named_key:
            for m in _NAMED_KEY_RE.finditer(line):
                key_name = m.group(1)
                value = m.group(3)
                if _is_placeholder(value):
                    continue
                if len(value) < 8:  # 短于 8 字符很可能是假命中
                    continue
                hits.append(SecretHit(
                    pattern_name=f"named_key({key_name})",
                    match=f"{key_name}={_mask(value)}",
                    line=lineno,
                    span=(m.start(), m.end()),
                ))

        # 2) OpenAI sk-...
        if self.enable_openai_sk:
            for m in _OPENAI_SK_RE.finditer(line):
                tok = m.group(0)
                if _is_placeholder(tok):
                    continue
                # sk-ant- 会被 anthropic_re 抢走,这里跳过
                if tok.startswith("sk-ant-"):
                    continue
                hits.append(SecretHit(
                    pattern_name="openai_sk",
                    match=_mask(tok),
                    line=lineno,
                    span=(m.start(), m.end()),
                ))

        # 3) Anthropic sk-ant-...
        if self.enable_anthropic_sk:
            for m in _ANTHROPIC_SK_RE.finditer(line):
                tok = m.group(0)
                if _is_placeholder(tok):
                    continue
                hits.append(SecretHit(
                    pattern_name="anthropic_sk",
                    match=_mask(tok),
                    line=lineno,
                    span=(m.start(), m.end()),
                ))

        # 4) AWS Access Key
        if self.enable_aws_key:
            for m in _AWS_KEY_RE.finditer(line):
                tok = m.group(0)
                if _is_placeholder(tok):
                    continue
                hits.append(SecretHit(
                    pattern_name="aws_key",
                    match=_mask(tok),
                    line=lineno,
                    span=(m.start(), m.end()),
                ))

        # 5) GitHub token
        if self.enable_github_token:
            for m in _GITHUB_TOKEN_RE.finditer(line):
                tok = m.group(0)
                if _is_placeholder(tok):
                    continue
                hits.append(SecretHit(
                    pattern_name="github_token",
                    match=_mask(tok),
                    line=lineno,
                    span=(m.start(), m.end()),
                ))

        return hits

    def assert_clean(self, text: str) -> None:
        """
        断言文本不包含密钥,发现立即抛 SecretDetectedError

        - 用于: MemoryEditor.edit_memory 写入前
        """
        result = self.scan(text)
        if not result.is_clean:
            first = result.hits[0]
            raise SecretDetectedError(
                f"检测到密钥: {first.pattern_name} "
                f"@ 第{first.line}行 '{first.match}' "
                f"(共 {len(result.hits)} 处命中)"
            )

    def redact(self, text: str) -> str:
        """
        §14.4: 扫描后把所有命中区间替换为 [REDACTED:<pattern_name>]

        Args:
            text: 原始文本
        Returns:
            redact 后的文本。多次命中区间按 start 倒序替换(避免 span 偏移)
            无命中返回原文本。
        """
        if not text:
            return text
        result = self.scan(text)
        if result.is_clean:
            return text
        # 倒序替换:从右往左,span 位置不变
        sorted_hits = sorted(result.hits, key=lambda h: h.span[0], reverse=True)
        redacted = text
        for hit in sorted_hits:
            start, end = hit.span
            replacement = f"[REDACTED:{hit.pattern_name}]"
            redacted = redacted[:start] + replacement + redacted[end:]
        return redacted


# ──────────────────────────────────────────────────────────────────
# Module-level 单例
# ──────────────────────────────────────────────────────────────────

_default_scanner: SecretScanner | None = None


def get_default_scanner() -> SecretScanner:
    """获取默认 scanner 单例"""
    global _default_scanner
    if _default_scanner is None:
        _default_scanner = SecretScanner()
    return _default_scanner


def scan_text(text: str) -> ScanResult:
    """便捷函数: 用默认 scanner 扫描"""
    return get_default_scanner().scan(text)


def assert_clean(text: str) -> None:
    """便捷函数: 用默认 scanner 拦截"""
    get_default_scanner().assert_clean(text)


def redact_text(text: str) -> str:
    """便捷函数: 用默认 scanner redact"""
    return get_default_scanner().redact(text)


__all__ = [
    "SecretScanner",
    "SecretHit",
    "ScanResult",
    "get_default_scanner",
    "scan_text",
    "assert_clean",
    "redact_text",
]