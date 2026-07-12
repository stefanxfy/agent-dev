"""
_cleanup.py 共享清理测试(从旧 test_sandbox_manager.py 迁移)。

覆盖:get_sandbox_tmp_dir / scrub_bare_git / cleanup_sandbox_tmp_dir / _safe_rmtree。
这些函数原在 SandboxManager,重构后移到 sandbox_backends/_cleanup.py(模块函数)。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from agent_core.tools.sandbox.backends import cleanup as _cleanup


# ── scrub_bare_git(防 CC #29316)──

class TestScrubBareGit:
    def test_removes_dot_git_in_sandbox_tmp(self, tmp_path):
        fake_tmp = tmp_path / "claude-1000"
        fake_tmp.mkdir()
        evil = fake_tmp / ".git"
        evil.mkdir()
        (evil / "config").write_text("[alias] x = !rm -rf /")

        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value=str(fake_tmp)):
            removed = _cleanup.scrub_bare_git([str(fake_tmp)])
        assert removed == 1
        assert not evil.exists()

    def test_removes_xxx_git_bare_repo_in_cwd(self, tmp_path):
        evil = tmp_path / "evil.git"
        evil.mkdir()
        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value=str(tmp_path / "claude-1000")):
            removed = _cleanup.scrub_bare_git([str(tmp_path)])
        assert removed == 1
        assert not evil.exists()

    def test_preserves_standard_git_dir(self, tmp_path):
        """项目根的标准 .git 不应被删(只删 *.git / sandbox_tmp 内的 .git)。"""
        standard = tmp_path / ".git"
        standard.mkdir()
        (standard / "HEAD").write_text("ref: refs/heads/main")
        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value=str(tmp_path / "claude-1000")):
            removed = _cleanup.scrub_bare_git([str(tmp_path)])
        assert removed == 0
        assert standard.exists()

    def test_nonexistent_dir_returns_zero(self):
        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value="/nonexistent/xyz"):
            assert _cleanup.scrub_bare_git(["/nonexistent/path/abc"]) == 0


# ── cleanup_sandbox_tmp_dir(mtime 过期)──

class TestCleanupSandboxTmpDir:
    def test_removes_old_dirs(self, tmp_path):
        old = tmp_path / "run-old"
        old.mkdir()
        old_time = time.time() - 25 * 3600
        os.utime(old, (old_time, old_time))

        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value=str(tmp_path)):
            removed = _cleanup.cleanup_sandbox_tmp_dir(max_age_hours=24.0)
        assert removed == 1
        assert not old.exists()

    def test_preserves_recent_dirs(self, tmp_path):
        recent = tmp_path / "run-recent"
        recent.mkdir()
        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value=str(tmp_path)):
            removed = _cleanup.cleanup_sandbox_tmp_dir(max_age_hours=24.0)
        assert removed == 0
        assert recent.exists()

    def test_skips_files_not_dirs(self, tmp_path):
        a_file = tmp_path / "not-a-dir"
        a_file.write_text("keep me")
        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value=str(tmp_path)):
            removed = _cleanup.cleanup_sandbox_tmp_dir(max_age_hours=0.01)
        assert removed == 0
        assert a_file.exists()

    def test_nonexistent_returns_zero(self):
        with patch.object(_cleanup, "get_sandbox_tmp_dir", return_value="/nonexistent/xyz"):
            assert _cleanup.cleanup_sandbox_tmp_dir() == 0


# ── run_default_cleanup(组合入口)──

class TestRunDefaultCleanup:
    def test_does_not_raise_on_cleanup_error(self):
        """run_default_cleanup 内部 try/except,不抛。"""
        with patch.object(_cleanup, "scrub_bare_git", side_effect=RuntimeError("boom")):
            _cleanup.run_default_cleanup("test")  # 不应抛


# ── get_sandbox_tmp_dir ──

class TestGetSandboxTmpDir:
    def test_creates_dir_with_0o700(self, tmp_path, monkeypatch):
        import tempfile
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(os, "getuid", lambda: 12345, raising=False)

        result = _cleanup.get_sandbox_tmp_dir()
        p = Path(result)
        assert p.exists()
        assert p.name == "claude-12345"
        assert (p.stat().st_mode & 0o777) == 0o700

    def test_idempotent(self, tmp_path, monkeypatch):
        import tempfile
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(os, "getuid", lambda: 99999, raising=False)
        assert _cleanup.get_sandbox_tmp_dir() == _cleanup.get_sandbox_tmp_dir()
