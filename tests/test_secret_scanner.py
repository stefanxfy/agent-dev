"""
M3 / Day 3 测试 —— SecretScanner (§14.4 4 pattern)

覆盖:
- 4 个核心 pattern 各一个正例
- 占位符不误杀
- assert_clean 抛异常
- scan 不抛异常
- 空文本处理
- 多行扫描
"""

from __future__ import annotations

import pytest

from agent_core.memory import (
    SecretScanner,
    ScanResult,
    SecretHit,
    SecretDetectedError,
    get_default_scanner,
    scan_text,
    assert_clean,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def scanner() -> SecretScanner:
    return SecretScanner()


# ──────────────────────────────────────────────────────────────────
# 4 个核心 pattern 正例
# ──────────────────────────────────────────────────────────────────

class TestNamedKeyPattern:
    """api_key / secret_key 命名型"""

    def test_api_key_equals(self, scanner: SecretScanner):
        r = scanner.scan("config.api_key = 'sk_live_abcd1234efgh5678'")
        assert not r.is_clean
        assert any("api_key" in h.pattern_name for h in r.hits)

    def test_secret_key_colon(self, scanner: SecretScanner):
        r = scanner.scan("env: secret_key: mySecretValue_ABCDEF123456")
        assert not r.is_clean
        assert any("secret_key" in h.pattern_name for h in r.hits)

    def test_password(self, scanner: SecretScanner):
        r = scanner.scan("password = MyVerySecretPassword_12345")
        assert not r.is_clean
        assert any("password" in h.pattern_name for h in r.hits)


class TestOpenAIPattern:
    """sk-xxx"""

    def test_openai_sk_40char(self, scanner: SecretScanner):
        text = "here is my sk-abcdefghijklmnopqrstuvwxyz1234567890abcdef"
        r = scanner.scan(text)
        assert not r.is_clean
        assert any(h.pattern_name == "openai_sk" for h in r.hits)

    def test_openai_sk_too_short_no_hit(self, scanner: SecretScanner):
        """sk- 后面不到 20 字符不命中（防误杀）"""
        r = scanner.scan("prefix: sk-short")
        assert r.is_clean


class TestAnthropicPattern:
    """sk-ant-xxx"""

    def test_anthropic_sk(self, scanner: SecretScanner):
        text = "anthropic: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
        r = scanner.scan(text)
        assert not r.is_clean
        assert any(h.pattern_name == "anthropic_sk" for h in r.hits)


class TestGitHubPattern:
    """ghp_xxx"""

    def test_github_pat(self, scanner: SecretScanner):
        text = "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        r = scanner.scan(text)
        assert not r.is_clean
        assert any(h.pattern_name == "github_token" for h in r.hits)


# ──────────────────────────────────────────────────────────────────
# 占位符白名单(不误杀)
# ──────────────────────────────────────────────────────────────────

class TestPlaceholders:

    def test_placeholder_sk_xxx(self, scanner: SecretScanner):
        r = scanner.scan("api_key = sk-xxx")
        assert r.is_clean

    def test_placeholder_your_key(self, scanner: SecretScanner):
        r = scanner.scan("secret_key = your-api-key-here")
        assert r.is_clean

    def test_placeholder_changeme(self, scanner: SecretScanner):
        r = scanner.scan("password = changeme")
        assert r.is_clean

    def test_placeholder_example(self, scanner: SecretScanner):
        r = scanner.scan("api_key = example-api-key-12345678")
        assert r.is_clean

    def test_repeated_chars(self, scanner: SecretScanner):
        r = scanner.scan("password = aaaaaa")
        assert r.is_clean


# ──────────────────────────────────────────────────────────────────
# 关键边界
# ──────────────────────────────────────────────────────────────────

class TestBoundary:

    def test_empty_text(self, scanner: SecretScanner):
        r = scanner.scan("")
        assert r.is_clean
        assert r.scanned_chars == 0

    def test_whitespace_only(self, scanner: SecretScanner):
        r = scanner.scan("   \n\t  \n")
        assert r.is_clean

    def test_normal_text_clean(self, scanner: SecretScanner):
        r = scanner.scan("用户喜欢使用 Python 写代码,特别是 numpy 库")
        assert r.is_clean

    def test_multiline_with_secret_on_l2(self, scanner: SecretScanner):
        text = """config:
  name: my-app
  secret_key = mySecretValue_ABCDEF123456
  debug: true
"""
        r = scanner.scan(text)
        assert not r.is_clean
        # 第 3 行
        assert any(h.line == 3 for h in r.hits)

    def test_scan_does_not_raise(self, scanner: SecretScanner):
        """scan() 即使命中也不抛异常"""
        r = scanner.scan("api_key = sk-abc123def456ghi789jkl012mno")
        assert r is not None

    def test_assert_clean_raises(self, scanner: SecretScanner):
        """assert_clean() 命中必须抛"""
        with pytest.raises(SecretDetectedError) as exc_info:
            scanner.assert_clean("api_key = sk-abc123def456ghi789jkl012mno")
        assert "api_key" in str(exc_info.value) or "openai_sk" in str(exc_info.value)

    def test_assert_clean_passes(self, scanner: SecretScanner):
        """assert_clean() 干净文本必须不抛"""
        scanner.assert_clean("用户的项目用 Python 实现")  # 不抛


# ──────────────────────────────────────────────────────────────────
# 模块级便捷函数
# ──────────────────────────────────────────────────────────────────

class TestModuleLevel:

    def test_scan_text(self):
        r = scan_text("api_key = mySecretValue_ABCDEF123456")
        assert not r.is_clean

    def test_assert_clean_raises(self):
        with pytest.raises(SecretDetectedError):
            assert_clean("api_key = mySecretValue_ABCDEF123456")

    def test_get_default_scanner_singleton(self):
        s1 = get_default_scanner()
        s2 = get_default_scanner()
        assert s1 is s2


# ──────────────────────────────────────────────────────────────────
# Mask 测试(不泄露原始密钥到日志)
# ──────────────────────────────────────────────────────────────────

def test_hit_match_is_masked(scanner: SecretScanner):
    """hit.match 应该是遮蔽后的,不含完整 key"""
    r = scanner.scan("api_key = mySecretValue_ABCDEF123456789012345")
    assert not r.is_clean
    h = r.hits[0]
    assert "***" in h.match
    # 完整 key 不应在 match 里
    assert "mySecretValue_ABCDEF123456789012345" not in h.match
