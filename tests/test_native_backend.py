"""
NativeBackend 测试。

覆盖:
- Protocol 满足
- 平台检测 + 依赖检测(从 sandbox_manager 下沉至此)
- macOS Seatbelt 真集成(@skipif non-darwin,不 mock,真跑 sandbox-exec)
- Linux bwrap 真集成(@skipif non-linux)
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import mock_open, patch

import pytest

from agent_core.tools.sandbox.backends import (
    NativeBackend,
    SandboxBackend,
    SandboxRuntimeConfig,
)

_NATIVE_MOD = "agent_core.tools.sandbox.backends.native"


def test_satisfies_protocol():
    assert isinstance(NativeBackend(), SandboxBackend)


# ── 平台检测(从 sandbox_manager 下沉)──

class TestPlatformSupport:
    def test_macos_supported(self):
        with patch("sys.platform", "darwin"):
            assert NativeBackend()._is_supported_platform() is True

    def test_windows_unsupported(self):
        with patch("sys.platform", "win32"):
            assert NativeBackend()._is_supported_platform() is False

    def test_linux_wsl_supported(self):
        m = mock_open(read_data="Linux version 5.15.0-microsoft-standard-WSL2")
        with patch("sys.platform", "linux"), patch("builtins.open", m):
            assert NativeBackend()._is_supported_platform() is True

    def test_linux_non_wsl_unsupported(self):
        m = mock_open(read_data="Linux version 5.15.0-generic")
        with patch("sys.platform", "linux"), patch("builtins.open", m):
            assert NativeBackend()._is_supported_platform() is False


# ── 依赖检测 ──

class TestCheckDependencies:
    def test_macos_sandbox_exec_present(self):
        with patch("sys.platform", "darwin"), \
             patch(f"{_NATIVE_MOD}.shutil.which", return_value="/usr/bin/sandbox-exec"):
            assert NativeBackend()._check_dependencies() is True

    def test_macos_sandbox_exec_missing(self):
        with patch("sys.platform", "darwin"), \
             patch(f"{_NATIVE_MOD}.shutil.which", return_value=None):
            assert NativeBackend()._check_dependencies() is False

    def test_linux_bwrap_present(self):
        with patch("sys.platform", "linux"), \
             patch(f"{_NATIVE_MOD}.shutil.which", return_value="/usr/bin/bwrap"):
            assert NativeBackend()._check_dependencies() is True

    def test_linux_bwrap_missing(self):
        with patch("sys.platform", "linux"), \
             patch(f"{_NATIVE_MOD}.shutil.which", return_value=None):
            assert NativeBackend()._check_dependencies() is False


# ── wrap 产出格式 ──

class TestWrapFormat:
    def test_unavailable_passthrough(self):
        """平台/二进制不可用时 wrap 返原命令(防御性)。"""
        nb = NativeBackend()
        with patch.object(nb, "is_available", return_value=False):
            assert nb.wrap("echo hi", SandboxRuntimeConfig(), ".") == "echo hi"

    def test_macos_wrap_contains_sandbox_exec(self, tmp_path):
        with patch("sys.platform", "darwin"), \
             patch(f"{_NATIVE_MOD}.shutil.which", return_value="/usr/bin/sandbox-exec"):
            nb = NativeBackend()
            w = nb.wrap("echo hi", SandboxRuntimeConfig(), str(tmp_path))
        assert "sandbox-exec -p" in w


# ── 真集成:macOS Seatbelt(不 mock,真跑)──

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt only")
class TestNativeMacOSRealIntegration:
    def _nb(self):
        nb = NativeBackend()
        if not nb.is_available():
            pytest.skip("sandbox-exec not found")
        return nb

    def test_allow_write_in_cwd(self, tmp_path):
        nb = self._nb()
        cfg = SandboxRuntimeConfig()
        cmd = nb.wrap(f"echo hi > {tmp_path}/ok.txt", cfg, str(tmp_path))
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert (tmp_path / "ok.txt").exists()

    def test_deny_write_outside_cwd(self, tmp_path):
        """cwd 外 + 用户显式 fs_deny_write 的路径应被 Seatbelt 拒(sandbox 拦截)。

        设计变更(2026-07-04):TMPDIR 父目录 + /tmp + /private/tmp 已默认 allow(为兼容
        mktemp 等 CLI 默认 tmp 路径),所以"cwd 外非 tmp 路径"不再可用 tmp 测。
        改测 fs_deny_write 显式 deny:用户配 deny 的路径必须被拒,与 Unix 权限无关。
        """
        nb = self._nb()
        denied_path = tmp_path.parent / "explicit_deny_dir" / "denied.txt"
        denied_dir = tmp_path.parent / "explicit_deny_dir"
        denied_dir.mkdir(exist_ok=True)
        cfg = SandboxRuntimeConfig(fs_deny_write=[str(denied_path.parent)])
        cmd = nb.wrap(f"echo bad > {denied_path}", cfg, str(tmp_path))
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        assert r.returncode != 0, f"expected Seatbelt deny, got rc={r.returncode} stderr={r.stderr[:200]}"
        assert not denied_path.exists()


# ── 真集成:Linux bwrap(不 mock,真跑)──

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux bwrap only")
class TestNativeLinuxRealIntegration:
    def test_bwrap_deny_write_outside(self, tmp_path):
        nb = NativeBackend()
        if not nb.is_available():
            pytest.skip("bwrap not found")
        cfg = SandboxRuntimeConfig()
        outside = tmp_path.parent / "native_linux_outside.txt"
        cmd = nb.wrap(f"echo bad > {outside}", cfg, str(tmp_path))
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        assert r.returncode != 0
        assert not outside.exists()
