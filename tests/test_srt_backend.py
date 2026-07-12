"""
SrtBackend 测试。

覆盖:
- Protocol 满足
- is_available(PATH 自动发现 / binaryPath)
- config schema 单测(实测确认:network 用 Domains 不是 Hosts)
- 真集成(@skipif not which srt)
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agent_core.tools.sandbox.backends import (
    SandboxBackend,
    SandboxRuntimeConfig,
    SrtBackend,
)

_SRT_MOD = "agent_core.tools.sandbox.backends.srt"


def test_satisfies_protocol():
    assert isinstance(SrtBackend(), SandboxBackend)


# ── is_available ──

class TestIsAvailable:
    def test_not_available_when_srt_missing(self):
        with patch(f"{_SRT_MOD}.shutil.which", return_value=None):
            assert SrtBackend().is_available() is False

    def test_available_when_binary_path_executable(self, tmp_path):
        fake = tmp_path / "fake-srt"
        fake.write_text("#!/bin/sh\necho srt-wrapper\n")
        fake.chmod(0o755)
        sb = SrtBackend(config={"binaryPath": str(fake)})
        assert sb.is_available() is True

    def test_binary_path_not_executable(self, tmp_path):
        fake = tmp_path / "not-exec"
        fake.write_text("x")
        fake.chmod(0o644)  # 不可执行
        sb = SrtBackend(config={"binaryPath": str(fake)})
        assert sb.is_available() is False

    def test_wrap_unavailable_passthrough(self):
        """srt 不存在时 wrap 返原命令(防御性)。"""
        sb = SrtBackend()
        with patch(f"{_SRT_MOD}.shutil.which", return_value=None):
            assert sb.wrap("echo hi", SandboxRuntimeConfig(), ".") == "echo hi"


# 需要 patch 在 import 之后
from unittest.mock import patch  # noqa: E402


# ── config schema(实测确认,设计 §5.3)──

class TestSchema:
    def test_network_uses_domains_not_hosts(self):
        """关键:srt 用 allowedDomains/deniedDomains(实测),不是 allowedHosts。"""
        sb = SrtBackend()
        cfg = SandboxRuntimeConfig(net_allowed=["a.com"], net_denied=["b.com"])
        schema = sb._cfg_to_srt_json(cfg, "/tmp")
        assert "allowedDomains" in schema["network"]
        assert "deniedDomains" in schema["network"]
        assert "allowedHosts" not in schema["network"]  # 旧代码的错误假设
        assert "a.com" in schema["network"]["allowedDomains"]

    def test_filesystem_keys(self):
        sb = SrtBackend()
        schema = sb._cfg_to_srt_json(SandboxRuntimeConfig(), "/tmp")
        assert {"allowWrite", "denyWrite", "allowRead", "denyRead"} <= set(schema["filesystem"])

    def test_allow_write_includes_cwd_and_tmp(self):
        sb = SrtBackend()
        cfg = SandboxRuntimeConfig(fs_allow_write=["/extra"])
        schema = sb._cfg_to_srt_json(cfg, "/my/cwd")
        aw = schema["filesystem"]["allowWrite"]
        assert "/my/cwd" in aw
        assert "/extra" in aw


# ── 真集成:需 srt 二进制(全局安装)──

@pytest.mark.skipif(shutil.which("srt") is None, reason="srt binary not installed")
class TestSrtRealIntegration:
    def test_wrap_executes_in_sandbox(self, tmp_path):
        sb = SrtBackend()
        cfg = SandboxRuntimeConfig()
        cmd = sb.wrap(f"echo hi > {tmp_path}/ok.txt", cfg, str(tmp_path))
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert (tmp_path / "ok.txt").exists()

    def test_wrap_denies_outside_write(self, tmp_path):
        sb = SrtBackend()
        cfg = SandboxRuntimeConfig()
        outside = tmp_path.parent / "srt_outside_deny.txt"
        cmd = sb.wrap(f"echo bad > {outside}", cfg, str(tmp_path))
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        assert r.returncode != 0
        assert not outside.exists()
