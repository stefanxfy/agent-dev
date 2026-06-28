"""
sandbox_prompt.py 测试

覆盖:
1. 禁用 → 空字符串
2. 启用 → prompt 含 Filesystem/Network section
3. tmpdir 字面化(非 $TMPDIR)
4. strict mode 追加 STRICT MODE 块
5. 非 strict mode 不含 STRICT MODE
6. sandbox-caused failure 列表
7. 敏感路径警告
8. config 变量注入(fs_read / network_allowed)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_core.tools.sandbox_prompt import get_sandbox_prompt_section
from agent_core.tools.sandbox_manager import SandboxManager


@pytest.fixture
def reset_sandbox():
    mgr = SandboxManager()
    mgr._reset_for_testing()
    yield mgr
    mgr._reset_for_testing()


@pytest.fixture
def enabled_sandbox(reset_sandbox):
    """启用沙箱 + mock 平台/依赖/初始化"""
    reset_sandbox.load_config({"enabled": True})
    with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
         patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
         patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)), \
         patch.object(reset_sandbox, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
        yield reset_sandbox


# ────────────────────────────────────────────────────────────────────
# 禁用路径
# ────────────────────────────────────────────────────────────────────

class TestDisabled:
    def test_returns_empty_string_when_disabled(self, reset_sandbox):
        assert get_sandbox_prompt_section() == ""

    def test_empty_string_is_falsy(self, reset_sandbox):
        assert not get_sandbox_prompt_section()


# ────────────────────────────────────────────────────────────────────
# 启用路径 — 基础结构
# ────────────────────────────────────────────────────────────────────

class TestEnabledStructure:
    def test_returns_nonempty_string(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        assert prompt != ""
        assert isinstance(prompt, str)

    def test_has_command_sandbox_header(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        assert "## Command sandbox" in prompt

    def test_has_filesystem_section(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        assert "Filesystem:" in prompt
        assert '"read"' in prompt
        assert '"write"' in prompt

    def test_has_network_section(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        assert "Network:" in prompt
        assert "allowedHosts" in prompt
        assert "deniedHosts" in prompt


# ────────────────────────────────────────────────────────────────────
# tmpdir 字面化
# ────────────────────────────────────────────────────────────────────

class TestTmpdirLiteral:
    def test_includes_tmpdir_literal_path(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        assert "/tmp/claude-1000" in prompt

    def test_does_not_use_dollar_tmpdir_as_path(self, enabled_sandbox):
        # $TMPDIR 仅作为"不要用"的指示性文字出现,实际路径必须用字面量
        prompt = get_sandbox_prompt_section()
        # 实际临时文件指令用的是字面路径,不是 $TMPDIR 变量
        assert "use the `/tmp/claude-1000` directory" in prompt
        # $TMPDIR 只出现在 "NOT $TMPDIR" 的提示文字里(对齐 CC prompt.ts)
        # 验证它不在路径指令位置(即不作为实际路径使用)
        assert "always use the `$TMPDIR`" not in prompt

    def test_tmpdir_in_write_allowlist(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        # write allow 列表应含 tmpdir(默认 fs_allow_write 空 → ['.', tmpdir])
        # 找 write allow 段
        assert "/tmp/claude-1000" in prompt

    def test_prompt_stable_for_same_tmpdir(self, enabled_sandbox):
        # 同一 tmpdir → 同一 prompt 字符串(便于 cache)
        p1 = get_sandbox_prompt_section()
        p2 = get_sandbox_prompt_section()
        assert p1 == p2


# ────────────────────────────────────────────────────────────────────
# strict mode
# ────────────────────────────────────────────────────────────────────

class TestStrictMode:
    def test_strict_mode_adds_strict_section(self, reset_sandbox):
        reset_sandbox.load_config({"enabled": True, "allowUnsandboxedCommands": False})
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)), \
             patch.object(reset_sandbox, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            prompt = get_sandbox_prompt_section()
        assert "STRICT MODE" in prompt
        assert "disabled" in prompt.lower()

    def test_non_strict_mode_omits_strict_section(self, enabled_sandbox):
        # 默认 allow_unsandboxed_commands=True → 非 strict
        prompt = get_sandbox_prompt_section()
        assert "STRICT MODE" not in prompt


# ────────────────────────────────────────────────────────────────────
# dangerously_disable_sandbox 指引
# ────────────────────────────────────────────────────────────────────

class TestDisableGuidance:
    def test_mentions_dangerously_disable_sandbox(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        assert "dangerously_disable_sandbox" in prompt

    def test_lists_sandbox_failure_evidence(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        # sandbox-caused failure 的证据列表
        assert "Operation not permitted" in prompt
        assert "Access denied" in prompt
        assert "Network connection failures" in prompt
        assert "Unix socket connection errors" in prompt

    def test_warns_against_sensitive_paths(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        # 警告不要建议加敏感路径到 allowlist
        assert "~/.bashrc" in prompt
        assert "~/.zshrc" in prompt
        assert "~/.ssh" in prompt

    def test_defaults_to_sandbox(self, enabled_sandbox):
        prompt = get_sandbox_prompt_section()
        assert "default" in prompt.lower() or "always" in prompt.lower()


# ────────────────────────────────────────────────────────────────────
# config 变量注入
# ────────────────────────────────────────────────────────────────────

class TestConfigInjection:
    def test_custom_fs_read_appears(self, reset_sandbox):
        reset_sandbox.load_config({
            "enabled": True,
            "fsAllowRead": ["/custom/read/path"],
        })
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)), \
             patch.object(reset_sandbox, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            prompt = get_sandbox_prompt_section()
        assert "/custom/read/path" in prompt

    def test_custom_network_allowed_appears(self, reset_sandbox):
        reset_sandbox.load_config({
            "enabled": True,
            "networkAllowedDomains": ["api.example.com"],
        })
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)), \
             patch.object(reset_sandbox, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            prompt = get_sandbox_prompt_section()
        assert "api.example.com" in prompt

    def test_custom_fs_deny_write_appears(self, reset_sandbox):
        reset_sandbox.load_config({
            "enabled": True,
            "fsDenyWrite": ["/secret/dir"],
        })
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)), \
             patch.object(reset_sandbox, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            prompt = get_sandbox_prompt_section()
        assert "/secret/dir" in prompt

    def test_custom_network_denied_appears(self, reset_sandbox):
        reset_sandbox.load_config({
            "enabled": True,
            "networkDeniedDomains": ["evil.com"],
        })
        with patch.object(reset_sandbox, "_is_supported_platform", return_value=True), \
             patch.object(reset_sandbox, "_check_dependencies", return_value=True), \
             patch.object(reset_sandbox, "initialize", lambda: setattr(reset_sandbox, "_initialized", True)), \
             patch.object(reset_sandbox, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            prompt = get_sandbox_prompt_section()
        assert "evil.com" in prompt
