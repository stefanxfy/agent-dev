"""
sandbox_manager.py 测试(UseCase 层编排)。

重构后(可插拔 backend):本文件只测 SandboxManager 的编排逻辑 —— config 加载、
backend 选定(切换矩阵)、is_sandbox_enabled、wrap 委托、_build_runtime_config、cleanup 委托。
用 mock backend,不依赖平台/二进制。

具体 backend(NativeBackend/SrtBackend)的真集成测试在 test_native_backend.py /
test_srt_backend.py;共享清理(_cleanup.py)在 test_sandbox_cleanup.py;
平台/依赖检测已下沉到 NativeBackend,不再在此测。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_core.tools.sandbox_backends import NullBackend, SandboxRuntimeConfig
from agent_core.tools.sandbox_manager import (
    SandboxConfig,
    SandboxManager,
    sandbox_manager,
)


# ────────────────────────────────────────────────────────────────────
# fixture + helper
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_manager():
    """每个测试一个干净状态(production 不再是强制单例,_reset 清字段)。"""
    mgr = SandboxManager()
    mgr._reset_for_testing()
    yield mgr
    mgr._reset_for_testing()


def _mock_backend(name: str, available: bool = True, wrap_prefix: str = "[N]"):
    """造一个满足 SandboxBackend Protocol 的 mock backend。"""
    b = MagicMock()
    b.name = name
    b.is_available.return_value = available
    b.wrap.side_effect = lambda cmd, cfg, wd: f"{wrap_prefix}{cmd}"
    b.initialize = MagicMock()
    b.cleanup = MagicMock()
    return b


# ────────────────────────────────────────────────────────────────────
# SandboxConfig
# ────────────────────────────────────────────────────────────────────

class TestSandboxConfig:
    def test_defaults(self):
        cfg = SandboxConfig()
        assert cfg.enabled is False
        assert cfg.fail_if_unavailable is False
        assert cfg.auto_allow_bash_if_sandboxed is True
        assert cfg.allow_unsandboxed_commands is True
        assert cfg.excluded_commands == []
        # 新字段(决策 A / D)
        assert cfg.backend == "auto"
        assert cfg.backend_priority == ["native", "srt"]

    def test_custom_values(self):
        cfg = SandboxConfig(enabled=True, backend="srt", backend_priority=["srt"])
        assert cfg.enabled is True
        assert cfg.backend == "srt"
        assert cfg.backend_priority == ["srt"]


# ────────────────────────────────────────────────────────────────────
# load_config
# ────────────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_camel_case_keys(self, fresh_manager):
        fresh_manager.load_config({
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "backend": "native",
            "backendPriority": ["srt", "native"],
        })
        assert fresh_manager._config.enabled is True
        assert fresh_manager._config.fail_if_unavailable is True
        assert fresh_manager._config.auto_allow_bash_if_sandboxed is False
        assert fresh_manager._config.backend == "native"
        assert fresh_manager._config.backend_priority == ["srt", "native"]

    def test_none_does_nothing(self, fresh_manager):
        before = fresh_manager._config.enabled
        fresh_manager.load_config(None)
        assert fresh_manager._config.enabled is before

    def test_partial_dict(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        assert fresh_manager._config.enabled is True
        assert fresh_manager._config.backend == "auto"  # 默认

    def test_load_reselects_backend_when_already_injected(self, fresh_manager):
        """load_config 改 backend 字段后,若 backends 已注入,应重新 select。"""
        fresh_manager.load_config({"enabled": True, "backend": "native"})
        fresh_manager.configure_backends([_mock_backend("native"), _mock_backend("srt")])
        assert fresh_manager._backend.name == "native"
        # 改 backend 配置重新 load → 应切到 srt
        fresh_manager.load_config({"enabled": True, "backend": "srt"})
        assert fresh_manager._backend.name == "srt"


# ────────────────────────────────────────────────────────────────────
# configure_backends + _select_backend(切换矩阵,设计 §6)
# ────────────────────────────────────────────────────────────────────

class TestSelectBackend:
    def test_auto_selects_first_in_priority(self, fresh_manager):
        fresh_manager.load_config(
            {"enabled": True, "backend": "auto", "backendPriority": ["native", "srt"]}
        )
        native = _mock_backend("native", available=True)
        srt = _mock_backend("srt", available=True)
        fresh_manager.configure_backends([native, srt])
        assert fresh_manager._backend is native

    def test_auto_falls_through_unavailable(self, fresh_manager):
        fresh_manager.load_config(
            {"enabled": True, "backend": "auto", "backendPriority": ["native", "srt"]}
        )
        fresh_manager.configure_backends(
            [_mock_backend("native", available=False), _mock_backend("srt", available=True)]
        )
        assert fresh_manager._backend.name == "srt"

    def test_auto_all_unavailable_null_fallback(self, fresh_manager):
        fresh_manager.load_config(
            {"enabled": True, "backend": "auto", "failIfUnavailable": False}
        )
        fresh_manager.configure_backends(
            [_mock_backend("native", False), _mock_backend("srt", False)]
        )
        assert isinstance(fresh_manager._backend, NullBackend)

    def test_auto_all_unavailable_fail_raises(self, fresh_manager):
        fresh_manager.load_config(
            {"enabled": True, "backend": "auto", "failIfUnavailable": True}
        )
        with pytest.raises(SystemExit):
            fresh_manager.configure_backends(
                [_mock_backend("native", False), _mock_backend("srt", False)]
            )

    def test_explicit_selects_named(self, fresh_manager):
        fresh_manager.load_config({"enabled": True, "backend": "srt"})
        fresh_manager.configure_backends(
            [_mock_backend("native", available=True), _mock_backend("srt", available=True)]
        )
        assert fresh_manager._backend.name == "srt"

    def test_explicit_unavailable_no_cross_fallback(self, fresh_manager):
        """显式 srt 不可用 → NullBackend,绝不偷换 native(设计 §6 关键原则)。"""
        fresh_manager.load_config(
            {"enabled": True, "backend": "srt", "failIfUnavailable": False}
        )
        fresh_manager.configure_backends(
            [_mock_backend("native", available=True), _mock_backend("srt", available=False)]
        )
        assert isinstance(fresh_manager._backend, NullBackend)

    def test_explicit_unavailable_fail_raises(self, fresh_manager):
        fresh_manager.load_config(
            {"enabled": True, "backend": "srt", "failIfUnavailable": True}
        )
        with pytest.raises(SystemExit):
            fresh_manager.configure_backends([_mock_backend("srt", available=False)])

    def test_empty_backends_yields_none(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        fresh_manager.configure_backends([])
        assert fresh_manager._backend is None


# ────────────────────────────────────────────────────────────────────
# is_sandbox_enabled
# ────────────────────────────────────────────────────────────────────

class TestIsSandboxEnabled:
    def test_disabled_by_default(self, fresh_manager):
        assert fresh_manager.is_sandbox_enabled() is False

    def test_enabled_when_backend_available(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        fresh_manager.configure_backends([_mock_backend("native", True)])
        assert fresh_manager.is_sandbox_enabled() is True

    def test_null_fallback_is_available(self, fresh_manager):
        """全不可用 + fail=False → NullBackend,is_available()=True,enabled 仍 True。"""
        fresh_manager.load_config(
            {"enabled": True, "backend": "auto", "failIfUnavailable": False}
        )
        fresh_manager.configure_backends(
            [_mock_backend("native", False), _mock_backend("srt", False)]
        )
        assert fresh_manager.is_sandbox_enabled() is True  # NullBackend.is_available True

    def test_disabled_when_no_backend(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        fresh_manager.configure_backends([])
        assert fresh_manager.is_sandbox_enabled() is False


# ────────────────────────────────────────────────────────────────────
# wrap_with_sandbox(委托)
# ────────────────────────────────────────────────────────────────────

class TestWrapWithSandbox:
    def test_disabled_passthrough(self, fresh_manager):
        assert fresh_manager.wrap_with_sandbox("echo hi") == "echo hi"

    def test_no_backend_passthrough(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        fresh_manager.configure_backends([])
        assert fresh_manager.wrap_with_sandbox("echo hi") == "echo hi"

    def test_delegates_to_backend(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        b = _mock_backend("native", available=True, wrap_prefix="[N]")
        fresh_manager.configure_backends([b])
        result = fresh_manager.wrap_with_sandbox("echo hi", working_dir="/tmp")
        b.wrap.assert_called_once()
        assert result == "[N]echo hi"

    def test_passes_runtime_config_to_backend(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        b = _mock_backend("native")
        fresh_manager.configure_backends([b])
        fresh_manager.wrap_with_sandbox("echo hi", working_dir="/tmp/work")
        called_args = b.wrap.call_args[0]
        assert isinstance(called_args[1], SandboxRuntimeConfig)  # 第 2 位置参数是 cfg


# ────────────────────────────────────────────────────────────────────
# _build_runtime_config(返 dataclass)
# ────────────────────────────────────────────────────────────────────

class TestBuildRuntimeConfig:
    def test_returns_dataclass(self, fresh_manager):
        cfg = fresh_manager._build_runtime_config(".")
        assert isinstance(cfg, SandboxRuntimeConfig)

    def test_allow_write_includes_dot_and_tmp(self, fresh_manager):
        cfg = fresh_manager._build_runtime_config(".")
        assert "." in cfg.fs_allow_write

    def test_merges_config_fs_allow_write(self, fresh_manager):
        fresh_manager.load_config({"fsAllowWrite": ["/custom/path"]})
        cfg = fresh_manager._build_runtime_config(".")
        assert "/custom/path" in cfg.fs_allow_write

    def test_network_domains_mapped(self, fresh_manager):
        fresh_manager.load_config({
            "networkAllowedDomains": ["api.example.com"],
            "networkDeniedDomains": ["evil.com"],
        })
        cfg = fresh_manager._build_runtime_config(".")
        assert "api.example.com" in cfg.net_allowed
        assert "evil.com" in cfg.net_denied


# ────────────────────────────────────────────────────────────────────
# cleanup_after_command(委托)
# ────────────────────────────────────────────────────────────────────

class TestCleanupAfterCommand:
    def test_delegates_to_backend(self, fresh_manager):
        fresh_manager.load_config({"enabled": True})
        b = _mock_backend("native")
        fresh_manager.configure_backends([b])
        fresh_manager.cleanup_after_command()
        b.cleanup.assert_called_once()

    def test_no_backend_noop(self, fresh_manager):
        fresh_manager.configure_backends([])
        fresh_manager.cleanup_after_command()  # 不应抛


# ────────────────────────────────────────────────────────────────────
# 模块级默认实例
# ────────────────────────────────────────────────────────────────────

class TestModuleInstance:
    def test_module_instance_exists(self):
        assert sandbox_manager is not None
        assert isinstance(sandbox_manager, SandboxManager)

    def test_reset_for_testing_clears_state(self):
        sandbox_manager.configure_backends([_mock_backend("native")])
        sandbox_manager._reset_for_testing()
        assert sandbox_manager._backend is None
        assert sandbox_manager._backends == []
