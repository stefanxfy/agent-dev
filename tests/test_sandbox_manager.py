"""
sandbox_manager.py 测试

覆盖:
1. SandboxConfig 默认值 + 字段
2. SandboxManager 单例
3. is_sandbox_enabled 四段短路(config / 平台 / 依赖 / 初始化)
4. wrap_with_sandbox 命令包装(禁用 / 启用 / argv 形式)
5. initialize 依赖检查(macOS / linux / fail_if_unavailable)
6. 平台检测(macOS / linux-wsl / linux-non-wsl / windows)
7. _get_sandbox_tmp_dir 创建 0o700
8. cleanup_after_command(bare-git scrub + tmp dir 过期)
9. _build_runtime_config 结构
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.tools.sandbox_manager import (
    SandboxConfig,
    SandboxManager,
    sandbox_manager,
)


# ────────────────────────────────────────────────────────────────────
# 测试 fixture — 每个测试重置单例
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_manager():
    """每个测试一个干净单例状态"""
    mgr = SandboxManager()  # 单例
    mgr._reset_for_testing()
    yield mgr
    mgr._reset_for_testing()


# ────────────────────────────────────────────────────────────────────
# SandboxConfig
# ────────────────────────────────────────────────────────────────────

class TestSandboxConfig:
    def test_default_disabled(self):
        cfg = SandboxConfig()
        assert cfg.enabled is False

    def test_default_auto_allow_bash_if_sandboxed(self):
        cfg = SandboxConfig()
        assert cfg.auto_allow_bash_if_sandboxed is True

    def test_default_allow_unsandboxed_commands(self):
        cfg = SandboxConfig()
        assert cfg.allow_unsandboxed_commands is True

    def test_default_fail_if_unavailable_false(self):
        cfg = SandboxConfig()
        assert cfg.fail_if_unavailable is False

    def test_default_empty_lists(self):
        cfg = SandboxConfig()
        assert cfg.network_allowed_domains == []
        assert cfg.fs_allow_write == []
        assert cfg.excluded_commands == []

    def test_custom_values(self):
        cfg = SandboxConfig(
            enabled=True,
            fail_if_unavailable=True,
            fs_allow_write=["/tmp/test"],
            excluded_commands=["git commit"],
        )
        assert cfg.enabled is True
        assert cfg.fail_if_unavailable is True
        assert cfg.fs_allow_write == ["/tmp/test"]
        assert cfg.excluded_commands == ["git commit"]


# ────────────────────────────────────────────────────────────────────
# 单例
# ────────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_singleton_returns_same_instance(self):
        a = SandboxManager()
        b = SandboxManager()
        assert a is b

    def test_global_singleton_matches_class(self):
        assert sandbox_manager is SandboxManager()

    def test_load_config_updates_enabled(self, fresh_manager):
        fresh_manager.load_config({"enabled": True, "failIfUnavailable": True})
        assert fresh_manager._config.enabled is True
        assert fresh_manager._config.fail_if_unavailable is True

    def test_load_config_none_does_nothing(self, fresh_manager):
        original_enabled = fresh_manager._config.enabled
        fresh_manager.load_config(None)
        assert fresh_manager._config.enabled is original_enabled

    def test_load_config_partial_dict(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        # 只设了 enabled,其他保持默认
        assert fresh_manager._config.enabled is True
        assert fresh_manager._config.auto_allow_bash_if_sandboxed is True

    def test_load_config_camel_case_keys(self, fresh_manager):
        # settings.json 用 camelCase,内部用 snake_case
        fresh_manager.load_config({
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "networkAllowedDomains": ["api.example.com"],
        })
        assert fresh_manager._config.auto_allow_bash_if_sandboxed is False
        assert fresh_manager._config.allow_unsandboxed_commands is False
        assert fresh_manager._config.network_allowed_domains == ["api.example.com"]


# ────────────────────────────────────────────────────────────────────
# is_sandbox_enabled 四段短路
# ────────────────────────────────────────────────────────────────────

class TestIsSandboxEnabled:
    def test_disabled_by_default(self, fresh_manager):
        # config.enabled 默认 False
        assert fresh_manager.is_sandbox_enabled() is False

    def test_enabled_when_config_and_platform_and_deps_ok(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        # mock 平台支持 + 依赖 + 初始化
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", lambda: setattr(fresh_manager, "_initialized", True)):
            assert fresh_manager.is_sandbox_enabled() is True

    def test_disabled_when_platform_unsupported(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=False):
            assert fresh_manager.is_sandbox_enabled() is False

    def test_disabled_when_dependencies_missing(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=False):
            assert fresh_manager.is_sandbox_enabled() is False

    def test_triggers_initialize_when_not_initialized(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        initialize_called = {"v": False}

        def fake_init():
            initialize_called["v"] = True
            fresh_manager._initialized = True

        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", fake_init):
            fresh_manager.is_sandbox_enabled()
        assert initialize_called["v"] is True


# ────────────────────────────────────────────────────────────────────
# wrap_with_sandbox
# ────────────────────────────────────────────────────────────────────

class TestWrapWithSandbox:
    def test_disabled_returns_command_unchanged(self, fresh_manager):
        # sandbox 禁用 → 原样返回
        result = fresh_manager.wrap_with_sandbox("rm -rf /tmp/foo")
        assert result == "rm -rf /tmp/foo"

    def test_enabled_returns_npx_prefix(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", lambda: setattr(fresh_manager, "_initialized", True)):
            result = fresh_manager.wrap_with_sandbox("echo hello")
        assert "npx -y @anthropic-ai/sandbox-runtime@latest wrap" in result

    def test_includes_config_flag(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", lambda: setattr(fresh_manager, "_initialized", True)):
            result = fresh_manager.wrap_with_sandbox("ls")
        assert "--config " in result

    def test_includes_command_after_dash_dash(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", lambda: setattr(fresh_manager, "_initialized", True)):
            result = fresh_manager.wrap_with_sandbox("echo hello")
        assert "-- 'echo hello'" in result

    def test_config_json_is_valid_json(self, fresh_manager):
        import json
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", lambda: setattr(fresh_manager, "_initialized", True)):
            result = fresh_manager.wrap_with_sandbox("ls")
        # 提取 --config 后的 quoted json
        import shlex
        # 用 shlex 解析整行,找 --config 的值
        tokens = shlex.split(result)
        idx = tokens.index("--config")
        config_json = tokens[idx + 1]
        parsed = json.loads(config_json)
        assert "filesystem" in parsed
        assert "network" in parsed

    def test_command_is_shlex_quoted(self, fresh_manager):
        # 含特殊字符的 command 应被 quote(shlex.quote 处理空格/引号)
        import shlex
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", lambda: setattr(fresh_manager, "_initialized", True)):
            result = fresh_manager.wrap_with_sandbox("echo 'hello world'")
        # command 被 shlex.quote 包成一个 token,出现在 -- 之后
        quoted = shlex.quote("echo 'hello world'")
        assert f"-- {quoted}" in result
        # 原 command 字面量在 quote 后仍是单 token(含空格但被引号包住)
        assert quoted in result

    def test_wrap_with_sandbox_argv_returns_list(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "_is_supported_platform", return_value=True), \
             patch.object(fresh_manager, "_check_dependencies", return_value=True), \
             patch.object(fresh_manager, "initialize", lambda: setattr(fresh_manager, "_initialized", True)):
            argv = fresh_manager.wrap_with_sandbox_argv("echo hi")
        assert argv is not None
        assert argv[0] == "npx"
        assert "wrap" in argv
        assert "echo hi" in argv

    def test_wrap_with_sandbox_argv_disabled_returns_none(self, fresh_manager):
        argv = fresh_manager.wrap_with_sandbox_argv("echo hi")
        assert argv is None


# ────────────────────────────────────────────────────────────────────
# initialize 依赖检查
# ────────────────────────────────────────────────────────────────────

class TestInitialize:
    def test_succeeds_on_macos(self, fresh_manager):
        with patch.object(fresh_manager, "_check_dependencies_detailed",
                          return_value={"errors": [], "warnings": []}):
            fresh_manager.initialize()
        assert fresh_manager._initialized is True

    def test_fails_on_dependency_error(self, fresh_manager):
        with patch.object(fresh_manager, "_check_dependencies_detailed",
                          return_value={"errors": ["bwrap 未安装"], "warnings": []}):
            fresh_manager.initialize()
        assert fresh_manager._initialized is False

    def test_fail_if_unavailable_raises_system_exit(self, fresh_manager):
        fresh_manager.load_config({"failIfUnavailable": True})
        with patch.object(fresh_manager, "_check_dependencies_detailed",
                          return_value={"errors": ["bwrap 未安装"], "warnings": []}):
            with pytest.raises(SystemExit) as exc_info:
                fresh_manager.initialize()
        assert exc_info.value.code == 1

    def test_warnings_logged_not_fatal(self, fresh_manager):
        with patch.object(fresh_manager, "_check_dependencies_detailed",
                          return_value={"errors": [], "warnings": ["socat 未安装"]}):
            fresh_manager.initialize()
        assert fresh_manager._initialized is True

    def test_initialize_idempotent(self, fresh_manager):
        with patch.object(fresh_manager, "_check_dependencies_detailed",
                          return_value={"errors": [], "warnings": []}):
            fresh_manager.initialize()
            call_count = {"v": 0}
            orig = fresh_manager._check_dependencies_detailed

            def counting():
                call_count["v"] += 1
                return orig()

            fresh_manager._check_dependencies_detailed = counting  # type: ignore
            fresh_manager.initialize()  # 已初始化,不应再调
        assert call_count["v"] == 0


# ────────────────────────────────────────────────────────────────────
# 平台检测
# ────────────────────────────────────────────────────────────────────

class TestPlatformSupport:
    def test_macos_supported(self, fresh_manager):
        with patch("sys.platform", "darwin"):
            assert fresh_manager._is_supported_platform() is True

    def test_windows_unsupported(self, fresh_manager):
        with patch("sys.platform", "win32"):
            assert fresh_manager._is_supported_platform() is False

    def test_linux_wsl_supported(self, fresh_manager):
        # mock /proc/version 含 microsoft
        from unittest.mock import mock_open
        m = mock_open(read_data="Linux version 5.15.0-microsoft-standard-WSL2")
        with patch("sys.platform", "linux"), patch("builtins.open", m):
            assert fresh_manager._is_supported_platform() is True

    def test_linux_non_wsl_unsupported(self, fresh_manager):
        from unittest.mock import mock_open
        m = mock_open(read_data="Linux version 5.15.0-generic")
        with patch("sys.platform", "linux"), patch("builtins.open", m):
            assert fresh_manager._is_supported_platform() is False

    def test_linux_proc_version_missing_unsupported(self, fresh_manager):
        def fake_open(f, *a, **k):
            raise FileNotFoundError(f)
        with patch("sys.platform", "linux"), \
             patch("builtins.open", fake_open):
            assert fresh_manager._is_supported_platform() is False


# ────────────────────────────────────────────────────────────────────
# 依赖检测
# ────────────────────────────────────────────────────────────────────

class TestCheckDependencies:
    def test_macos_no_bwrap_ok(self, fresh_manager):
        # macOS Seatbelt 内建,不需要 bwrap
        with patch("sys.platform", "darwin"):
            assert fresh_manager._check_dependencies() is True

    def test_linux_no_bwrap_fails(self, fresh_manager):
        with patch("sys.platform", "linux"), \
             patch("agent_core.tools.sandbox_manager.shutil.which", return_value=None):
            assert fresh_manager._check_dependencies() is False

    def test_linux_with_bwrap_ok(self, fresh_manager):
        with patch("sys.platform", "linux"), \
             patch("agent_core.tools.sandbox_manager.shutil.which", return_value="/usr/bin/bwrap"):
            assert fresh_manager._check_dependencies() is True

    def test_detailed_macos_no_errors(self, fresh_manager):
        with patch("sys.platform", "darwin"):
            result = fresh_manager._check_dependencies_detailed()
        assert result["errors"] == []

    def test_detailed_linux_missing_bwrap_error(self, fresh_manager):
        with patch("sys.platform", "linux"), \
             patch("agent_core.tools.sandbox_manager.shutil.which", return_value=None):
            result = fresh_manager._check_dependencies_detailed()
        assert any("bwrap" in e for e in result["errors"])

    def test_detailed_linux_missing_socat_warning(self, fresh_manager):
        def fake_which(cmd):
            if cmd == "bwrap":
                return "/usr/bin/bwrap"
            return None  # socat missing
        with patch("sys.platform", "linux"), \
             patch("agent_core.tools.sandbox_manager.shutil.which", side_effect=fake_which):
            result = fresh_manager._check_dependencies_detailed()
        assert result["errors"] == []
        assert any("socat" in w for w in result["warnings"])


# ────────────────────────────────────────────────────────────────────
# sandbox tmp dir
# ────────────────────────────────────────────────────────────────────

class TestSandboxTmpDir:
    def test_creates_dir_with_0o700(self, fresh_manager, tmp_path, monkeypatch):
        # mock tempfile.gettempdir 指向 tmp_path
        monkeypatch.setattr(
            "agent_core.tools.sandbox_manager.Path",
            Path,  # 保持 Path 类
        )
        import tempfile
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(os, "getuid", lambda: 12345, raising=False)

        result = fresh_manager._get_sandbox_tmp_dir()
        result_path = Path(result)
        assert result_path.exists()
        assert result_path.name == "claude-12345"
        mode = result_path.stat().st_mode & 0o777
        assert mode == 0o700

    def test_returns_existing_dir(self, fresh_manager, tmp_path, monkeypatch):
        import tempfile
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(os, "getuid", lambda: 99999, raising=False)

        first = fresh_manager._get_sandbox_tmp_dir()
        second = fresh_manager._get_sandbox_tmp_dir()
        assert first == second


# ────────────────────────────────────────────────────────────────────
# _build_runtime_config
# ────────────────────────────────────────────────────────────────────

class TestBuildRuntimeConfig:
    def test_has_filesystem_and_network_keys(self, fresh_manager):
        cfg = fresh_manager._build_runtime_config(".")
        assert "filesystem" in cfg
        assert "network" in cfg

    def test_filesystem_has_allow_write_deny_write(self, fresh_manager):
        cfg = fresh_manager._build_runtime_config("/tmp/work")
        fs = cfg["filesystem"]
        assert "allowWrite" in fs
        assert "denyWrite" in fs
        assert "allowRead" in fs
        assert "denyRead" in fs

    def test_allow_write_includes_working_dir_and_tmp(self, fresh_manager):
        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            cfg = fresh_manager._build_runtime_config("/tmp/work")
        allow = cfg["filesystem"]["allowWrite"]
        assert "." in allow
        assert "/tmp/claude-1000" in allow

    def test_network_has_allowed_and_denied_hosts(self, fresh_manager):
        fresh_manager.load_config({
            "networkAllowedDomains": ["api.example.com"],
            "networkDeniedDomains": ["evil.com"],
        })
        cfg = fresh_manager._build_runtime_config(".")
        assert "api.example.com" in cfg["network"]["allowedHosts"]
        assert "evil.com" in cfg["network"]["deniedHosts"]

    def test_custom_fs_allow_write_merged(self, fresh_manager):
        fresh_manager.load_config({"fsAllowWrite": ["/custom/path"]})
        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value="/tmp/claude-1000"):
            cfg = fresh_manager._build_runtime_config(".")
        allow = cfg["filesystem"]["allowWrite"]
        assert "/custom/path" in allow


# ────────────────────────────────────────────────────────────────────
# cleanup_after_command
# ────────────────────────────────────────────────────────────────────

class TestCleanupAfterCommand:
    def test_disabled_is_noop(self, fresh_manager):
        # sandbox 禁用 → cleanup 不做任何事
        scrub_called = {"v": False}
        orig = fresh_manager._scrub_bare_git
        fresh_manager._scrub_bare_git = lambda *a, **k: scrub_called.__setitem__("v", True) or 0  # type: ignore
        fresh_manager.cleanup_after_command()
        assert scrub_called["v"] is False
        fresh_manager._scrub_bare_git = orig  # type: ignore

    def test_enabled_runs_scrub_and_cleanup(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        calls = {"scrub": 0, "tmp_cleanup": 0}

        def fake_scrub(*a, **k):
            calls["scrub"] += 1
            return 0

        def fake_tmp_cleanup(max_age_hours=24.0):
            calls["tmp_cleanup"] += 1
            return 0

        with patch.object(fresh_manager, "is_sandbox_enabled", return_value=True), \
             patch.object(fresh_manager, "_scrub_bare_git", fake_scrub), \
             patch.object(fresh_manager, "_cleanup_sandbox_tmp_dir", fake_tmp_cleanup):
            fresh_manager.cleanup_after_command()
        assert calls["scrub"] == 1
        assert calls["tmp_cleanup"] == 1

    def test_exception_does_not_propagate(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        with patch.object(fresh_manager, "is_sandbox_enabled", return_value=True), \
             patch.object(fresh_manager, "_scrub_bare_git", side_effect=RuntimeError("boom")):
            # 不应抛
            fresh_manager.cleanup_after_command()


# ────────────────────────────────────────────────────────────────────
# bare-git scrub
# ────────────────────────────────────────────────────────────────────

class TestScrubBareGit:
    def test_removes_bare_git_dir_in_tmp(self, fresh_manager, tmp_path):
        # sandbox_tmp_dir 里的 .git 目录应被删
        fake_tmp = tmp_path / "claude-1000"
        fake_tmp.mkdir()
        evil_git = fake_tmp / ".git"
        evil_git.mkdir()
        (evil_git / "config").write_text("[alias] x = !rm -rf /")

        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value=str(fake_tmp)):
            removed = fresh_manager._scrub_bare_git([str(fake_tmp)])
        assert removed == 1
        assert not evil_git.exists()

    def test_removes_xxx_git_bare_repo_in_cwd(self, fresh_manager, tmp_path):
        # cwd 下的 foo.git 形式 bare repo 应被删
        evil = tmp_path / "evil.git"
        evil.mkdir()

        # mock cwd 指向 tmp_path
        with patch("os.getcwd", return_value=str(tmp_path)), \
             patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value=str(tmp_path / "claude-1000")):
            removed = fresh_manager._scrub_bare_git([str(tmp_path)])
        assert removed == 1
        assert not evil.exists()

    def test_preserves_standard_git_dir(self, fresh_manager, tmp_path):
        # 项目根的标准 .git 目录不应被删(只删 *.git / sandbox_tmp 内的 .git)
        standard_git = tmp_path / ".git"
        standard_git.mkdir()
        (standard_git / "HEAD").write_text("ref: refs/heads/main")

        removed = fresh_manager._scrub_bare_git([str(tmp_path)])
        assert removed == 0
        assert standard_git.exists()

    def test_nonexistent_dir_returns_zero(self, fresh_manager):
        removed = fresh_manager._scrub_bare_git(["/nonexistent/path/xyz"])
        assert removed == 0

    def test_scrub_handles_oserror_gracefully(self, fresh_manager, tmp_path):
        # iterdir 抛 OSError 不应传播
        with patch.object(Path, "iterdir", side_effect=OSError("denied")):
            removed = fresh_manager._scrub_bare_git([str(tmp_path)])
        assert removed == 0


# ────────────────────────────────────────────────────────────────────
# sandbox tmp dir 过期清理
# ────────────────────────────────────────────────────────────────────

class TestCleanupSandboxTmpDir:
    def test_removes_old_dirs(self, fresh_manager, tmp_path):
        old_dir = tmp_path / "run-old"
        old_dir.mkdir()
        # 设 mtime 为 25 小时前
        old_time = time.time() - 25 * 3600
        os.utime(old_dir, (old_time, old_time))

        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value=str(tmp_path)):
            removed = fresh_manager._cleanup_sandbox_tmp_dir(max_age_hours=24.0)
        assert removed == 1
        assert not old_dir.exists()

    def test_preserves_recent_dirs(self, fresh_manager, tmp_path):
        recent_dir = tmp_path / "run-recent"
        recent_dir.mkdir()

        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value=str(tmp_path)):
            removed = fresh_manager._cleanup_sandbox_tmp_dir(max_age_hours=24.0)
        assert removed == 0
        assert recent_dir.exists()

    def test_custom_max_age(self, fresh_manager, tmp_path):
        # max_age=1h,2 小时前的目录应被删
        old_dir = tmp_path / "run-2h"
        old_dir.mkdir()
        old_time = time.time() - 2 * 3600
        os.utime(old_dir, (old_time, old_time))

        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value=str(tmp_path)):
            removed = fresh_manager._cleanup_sandbox_tmp_dir(max_age_hours=1.0)
        assert removed == 1

    def test_nonexistent_tmp_returns_zero(self, fresh_manager):
        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value="/nonexistent/xyz"):
            removed = fresh_manager._cleanup_sandbox_tmp_dir()
        assert removed == 0

    def test_skips_files_not_dirs(self, fresh_manager, tmp_path):
        # 普通文件不应被删
        a_file = tmp_path / "not-a-dir"
        a_file.write_text("keep me")

        with patch.object(fresh_manager, "_get_sandbox_tmp_dir", return_value=str(tmp_path)):
            removed = fresh_manager._cleanup_sandbox_tmp_dir(max_age_hours=0.01)
        assert removed == 0
        assert a_file.exists()


# ────────────────────────────────────────────────────────────────────
# _safe_rmtree
# ────────────────────────────────────────────────────────────────────

class TestSafeRmtree:
    def test_removes_existing_dir(self, fresh_manager, tmp_path):
        target = tmp_path / "to-remove"
        target.mkdir()
        (target / "file").write_text("x")
        fresh_manager._safe_rmtree(target)
        assert not target.exists()

    def test_swallows_oserror(self, fresh_manager, tmp_path):
        # 不存在的目录 → OSError 被吞
        target = tmp_path / "nonexistent"
        # 不应抛
        fresh_manager._safe_rmtree(target)
