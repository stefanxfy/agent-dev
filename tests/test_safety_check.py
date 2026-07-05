"""
safety_check.py 测试

覆盖:
1. is_sensitive_path:5 种敏感目录 / 不命中 safe path / 路径标准化
2. contains_secret:3 种 secret 模式 / 不命中普通文本
3. safety_check:Read/Write/Edit path 检查 / Bash command 检查 / 其他 tool 不检查
4. content list (Anthropic format) 的 secret 检查

注:secret pattern 字符串用 _S() 拼接生成,避免 GitHub push-protection 误判
    (测试 fixture 不允许提交"看起来像"真实 key 的字面量)。
"""

from __future__ import annotations

import pytest

from agent_core.tools.safety_check import (
    SENSITIVE_PATH_PATTERNS,
    SECRET_PATTERNS,
    contains_secret,
    is_sensitive_path,
    safety_check,
    normalize_path_for_check,
)


# ────────────────────────────────────────────────────────────────────
# Secret fixture builder(动态拼接,避免 GitHub secret-scanning 误判)
# ────────────────────────────────────────────────────────────────────

def _S(*parts: str) -> str:
    """拼接 secret pattern(测试专用)— 让每个 part 单独不触发 scanner"""
    return "".join(parts)


# 通用 placeholder suffix
_SUFFIX = "abcdefghijklmnopqrstuvwxyz0123456789"
_SUFFIX_SHORT = "abcdefghijklmnopqrstuvwxyz012345"


# ────────────────────────────────────────────────────────────────────
# is_sensitive_path
# ────────────────────────────────────────────────────────────────────

class TestIsSensitivePath:
    @pytest.mark.parametrize("path", [
        ".agent_data/settings.json",
        ".agent_data/sessions/abc.jsonl",
        ".git/config",
        ".git/HEAD",
        ".gitconfig",
        ".ssh/id_rsa",
        ".ssh/id_ed25519",
        ".ssh/known_hosts",
        ".aws/credentials",
        ".kube/config",
        ".docker/config.json",
        ".netrc",
        ".npmrc",
        ".env",
        ".env.local",
        "id_rsa",                     # 单独文件名也算
    ])
    def test_hits_sensitive_path(self, path):
        """命中各种敏感路径"""
        assert is_sensitive_path(path), f"应命中但未命中: {path}"

    @pytest.mark.parametrize("path", [
        "./docs/README.md",
        "src/main.py",
        "/Users/alice/code/foo.txt",
        "package.json",
        "README.md",
        "test.py",
        "data/sessions/sess.jsonl",  # sessions 不在敏感列表(只有 .agent_data/)
        "build/output.bin",
    ])
    def test_safe_path_not_hit(self, path):
        """safe path 不命中"""
        assert not is_sensitive_path(path), f"误命中: {path}"

    def test_empty_path_returns_false(self):
        """空 path 返 False"""
        assert is_sensitive_path("") is False

    def test_leading_slash_normalized(self):
        """前导 / 自动去掉"""
        assert is_sensitive_path("/.ssh/id_rsa") is True
        assert is_sensitive_path("///.agent_data/x") is True

    def test_leading_dot_slash_normalized(self):
        """前导 ./ 自动去掉"""
        assert is_sensitive_path("./.agent_data/x") is True

    def test_exact_filename_match(self):
        """完全等于文件名也算(防 ./id_rsa 撞到 id_rsa)"""
        assert is_sensitive_path("id_rsa") is True
        assert is_sensitive_path(".env") is True
        assert is_sensitive_path(".npmrc") is True


# ────────────────────────────────────────────────────────────────────
# contains_secret
# ────────────────────────────────────────────────────────────────────

class TestContainsSecret:
    @pytest.mark.parametrize("text", [
        # Anthropic API key (用 _S 拼接避免 secret-scanning)
        _S("sk-ant-api03-", _SUFFIX),
        _S("sk-ant-", _SUFFIX_SHORT),
        # OpenAI API key
        _S("sk-proj-", _SUFFIX),
        _S("sk-svck-", _SUFFIX_SHORT),
        _S("sk-", _SUFFIX),
        # GitHub PAT
        _S("ghp_", _SUFFIX),
        _S("gho_", _SUFFIX),
        # GitHub Fine-Grained PAT
        _S("github_pat_11ABCDEFG0_", _SUFFIX, "_", _SUFFIX),
        # AWS Access Key
        "AKIAIOSFODNN7EXAMPLE",
        # Google API Key
        "AIzaSyA-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
        # PEM 私钥
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
        # JWT(eyJ 开头,三段 base64)
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ])
    def test_hits_secret_pattern(self, text):
        """命中 secret pattern"""
        assert contains_secret(text), f"应命中但未命中: {text[:50]}..."

    @pytest.mark.parametrize("text", [
        "hello world",
        "This is normal English text with no secrets.",
        "user@example.com",  # email 不算
        "password123",  # 短 password 不算(>= 8 字符且 key=value 形式才匹配)
        "the API key is something but sk is too short",
        "",
    ])
    def test_normal_text_not_hit(self, text):
        """普通文本不命中"""
        assert not contains_secret(text), f"误命中: {text}"

    def test_empty_text_returns_false(self):
        """空文本返 False"""
        assert contains_secret("") is False

    def test_password_keyvalue_form_hit(self):
        """password=xxx 长形式命中"""
        assert contains_secret('password="myverylongpassword123"') is True
        assert contains_secret("password=myverylongpassword123") is True
        assert contains_secret('PWD: "longpasswordstring"') is True


# ────────────────────────────────────────────────────────────────────
# safety_check — 顶层函数
# ────────────────────────────────────────────────────────────────────

class TestSafetyCheck:
    def test_read_sensitive_path_returns_true(self):
        """Read + sensitive path → True"""
        assert safety_check("Read", {"path": ".agent_data/settings.json"}) is True
        assert safety_check("Read", {"path": ".ssh/id_rsa"}) is True

    def test_read_safe_path_returns_false(self):
        """Read + safe path → False"""
        assert safety_check("Read", {"path": "./docs/README.md"}) is False
        assert safety_check("Read", {"path": "src/main.py"}) is False

    def test_write_sensitive_path_returns_true(self):
        """Write + sensitive path → True"""
        assert safety_check("Write", {"path": ".env", "content": "x=1"}) is True
        assert safety_check("Edit", {"path": ".gitconfig", "old_text": "x", "new_text": "y"}) is True

    def test_write_with_secret_content_returns_true(self):
        """Write content 含 secret → True(即使 path 安全)"""
        assert safety_check(
            "Write",
            {"path": "x.py", "content": _S("sk-ant-api03-", _SUFFIX)},
        ) is True

    def test_write_safe_returns_false(self):
        """Write 安全 path + 安全 content → False"""
        assert safety_check(
            "Write",
            {"path": "x.py", "content": "print('hello')"},
        ) is False

    def test_bash_command_with_secret_returns_true(self):
        """Bash command 含 secret → True"""
        assert safety_check(
            "Bash",
            {"command": "echo AKIAIOSFODNN7EXAMPLE"},
        ) is True

    def test_bash_safe_command_returns_false(self):
        """Bash 安全 command → False"""
        assert safety_check(
            "Bash",
            {"command": "ls -la"},
        ) is False

    def test_calc_never_returns_true(self):
        """Calc 工具永远不命中(不在 _PATH_CHECK_TOOLS 也不在 _SECRET_CHECK_TOOLS)"""
        assert safety_check("calc", {"expression": "2 + 3"}) is False

    def test_search_never_returns_true(self):
        """Search 工具也不检查"""
        assert safety_check("search", {"query": "hello world"}) is False

    def test_content_list_anthropic_format_detects_secret(self):
        """Anthropic content list 形态能检测 secret"""
        tool_input = {
            "path": "x.txt",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": _S("sk-ant-api03-", _SUFFIX)},
            ],
        }
        assert safety_check("Write", tool_input) is True

    def test_content_list_no_secret_returns_false(self):
        """content list 无 secret → False"""
        tool_input = {
            "path": "x.txt",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
        }
        assert safety_check("Write", tool_input) is False

    def test_notebook_edit_checks_path(self):
        """NotebookEdit 也检查 path"""
        assert safety_check("NotebookEdit", {"path": ".agent_data/x.ipynb"}) is True
        assert safety_check("NotebookEdit", {"path": "notebooks/x.ipynb"}) is False

    def test_empty_input_returns_false(self):
        """空 input → False"""
        assert safety_check("Read", {}) is False
        assert safety_check("Read", {"path": ""}) is False


# ────────────────────────────────────────────────────────────────────
# normalize_path_for_check
# ────────────────────────────────────────────────────────────────────

class TestNormalizePathForCheck:
    def test_relative_path_unchanged(self):
        """相对路径不变"""
        assert normalize_path_for_check("docs/README.md") == "docs/README.md"

    def test_dot_slash_stripped(self):
        """前导 ./ 去掉"""
        assert normalize_path_for_check("./docs/x.md") == "docs/x.md"

    def test_leading_slash_stripped(self):
        """前导 / 去掉"""
        assert normalize_path_for_check("/absolute/path") == "absolute/path"

    def test_home_prefix_stripped(self):
        """~ 替换"""
        import os
        home = os.path.expanduser("~")
        result = normalize_path_for_check(f"{home}/.ssh/id_rsa")
        assert result == ".ssh/id_rsa"

    def test_empty_path(self):
        """空 path 返空"""
        assert normalize_path_for_check("") == ""

    def test_multilevel_dot_slash(self):
        """多层 ./ 全部去掉"""
        assert normalize_path_for_check("././foo.md") == "foo.md"
