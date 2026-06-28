"""
BashTool 内置实现测试

覆盖:
1. bash_handler 执行 + 输出
2. timeout / working_dir / exit code
3. dangerously_disable_sandbox 透传
4. sandbox wrap 行为
5. BASH_TOOL ToolDef 字段
6. register_builtin_tools 注册
7. jsonschema 校验
8. 输出截断 + unicode
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from agent_core.tools.base import ToolRegistry
from agent_core.tools.builtin import (
    BASH_TOOL,
    _BASH_OUTPUT_MAX_CHARS,
    bash_handler,
    register_builtin_tools,
)
from agent_core.tools.sandbox_manager import SandboxManager


@pytest.fixture(autouse=True)
def reset_sandbox():
    mgr = SandboxManager()
    mgr._reset_for_testing()
    yield
    mgr._reset_for_testing()


# ────────────────────────────────────────────────────────────────────
# bash_handler 执行
# ────────────────────────────────────────────────────────────────────

class TestBashHandlerExecution:
    def test_runs_simple_command(self):
        result = bash_handler(command="echo hello")
        assert "hello" in result

    def test_returns_stdout(self):
        result = bash_handler(command="printf 'line1\\nline2'")
        assert "line1" in result
        assert "line2" in result

    def test_returns_stderr_on_failure(self):
        # ls 不存在的目录 → stderr
        result = bash_handler(command="ls /nonexistent_dir_xyz")
        assert "No such file" in result or " nonexistent" in result.lower()

    def test_missing_command_raises_value_error(self):
        with pytest.raises(ValueError):
            bash_handler()

    def test_empty_command_raises_value_error(self):
        with pytest.raises(ValueError):
            bash_handler(command="")

    def test_whitespace_command_raises_value_error(self):
        with pytest.raises(ValueError):
            bash_handler(command="   ")

    def test_nonzero_exit_returns_output(self):
        # false 命令 exit 1,无 stdout
        result = bash_handler(command="echo done; exit 1")
        assert "done" in result


# ────────────────────────────────────────────────────────────────────
# timeout
# ────────────────────────────────────────────────────────────────────

class TestTimeout:
    def test_timeout_returns_message(self):
        result = bash_handler(command="sleep 5", timeout=0.5)
        assert "timed out" in result.lower()

    def test_custom_timeout_allows_completion(self):
        result = bash_handler(command="echo fast", timeout=10)
        assert "fast" in result

    def test_default_timeout_is_30(self):
        # 验证 schema default
        props = BASH_TOOL.parameters["properties"]["timeout"]
        assert props["default"] == 30.0


# ────────────────────────────────────────────────────────────────────
# working_dir
# ────────────────────────────────────────────────────────────────────

class TestWorkingDir:
    def test_uses_working_dir_when_specified(self, tmp_path):
        result = bash_handler(command="pwd", working_dir=str(tmp_path))
        assert str(tmp_path) in result

    def test_working_dir_none_uses_cwd(self):
        # 不传 working_dir → 用当前 cwd
        import os
        result = bash_handler(command="pwd")
        assert os.getcwd() in result


# ────────────────────────────────────────────────────────────────────
# dangerously_disable_sandbox 透传
# ────────────────────────────────────────────────────────────────────

class TestDangerouslyDisableSandbox:
    def test_disable_false_does_not_affect_when_sandbox_disabled(self):
        # sandbox 禁用 → disable 无影响,直接执行
        result = bash_handler(
            command="echo test",
            dangerously_disable_sandbox=False,
        )
        assert "test" in result

    def test_disable_true_executes_when_sandbox_disabled(self):
        result = bash_handler(
            command="echo bypass",
            dangerously_disable_sandbox=True,
        )
        assert "bypass" in result


# ────────────────────────────────────────────────────────────────────
# sandbox wrap 集成
# ────────────────────────────────────────────────────────────────────

class TestSandboxWrap:
    @pytest.fixture
    def enabled_sandbox(self):
        mgr = SandboxManager()
        mgr.load_config({"enabled": True})
        with patch.object(mgr, "_is_supported_platform", return_value=True), \
             patch.object(mgr, "_check_dependencies", return_value=True), \
             patch.object(mgr, "initialize", lambda: setattr(mgr, "_initialized", True)):
            yield mgr

    def test_wraps_command_when_sandbox_enabled(self, enabled_sandbox):
        # sandbox 启用 → wrap_with_sandbox 被调,实际执行 wrap 后命令
        # 这里不真跑 npx,而是验证 wrap 逻辑生效(命令被改写)
        wrapped_commands = []
        original_run = subprocess.run

        def spy_run(cmd, *args, **kwargs):
            wrapped_commands.append(cmd)
            # 模拟成功执行
            class FakeResult:
                stdout = "ok"
                stderr = ""
                returncode = 0
            return FakeResult()

        with patch("agent_core.tools.builtin.subprocess.run", spy_run):
            bash_handler(command="echo hello")
        # sandbox 启用 → 命令应被 wrap(含 npx 前缀)
        assert any("npx" in str(c) for c in wrapped_commands)

    def test_does_not_wrap_when_dangerously_disable(self, enabled_sandbox):
        wrapped_commands = []

        class FakeResult:
            stdout = "ok"
            stderr = ""
            returncode = 0

        def spy_run(cmd, *args, **kwargs):
            wrapped_commands.append(cmd)
            return FakeResult()

        with patch("agent_core.tools.builtin.subprocess.run", spy_run):
            bash_handler(
                command="echo hello",
                dangerously_disable_sandbox=True,
            )
        # disable + allow_unsandboxed_commands → 不 wrap
        assert not any("npx" in str(c) for c in wrapped_commands)

    def test_sandbox_judgment_failure_falls_back_to_raw(self):
        # should_use_sandbox 抛异常 → 不 wrap,直接执行原命令
        with patch(
            "agent_core.tools.sandbox_decision.should_use_sandbox",
            side_effect=RuntimeError("boom"),
        ):
            result = bash_handler(command="echo resilient")
        assert "resilient" in result


# ────────────────────────────────────────────────────────────────────
# 输出处理
# ────────────────────────────────────────────────────────────────────

class TestOutputHandling:
    def test_truncates_long_output(self):
        # 生成超长输出
        result = bash_handler(command="printf 'x%.0s' {1..6000}")
        assert len(result) <= _BASH_OUTPUT_MAX_CHARS + 100  # 留 truncation 提示余量
        assert "truncated" in result

    def test_preserves_unicode(self):
        result = bash_handler(command="echo 你好世界")
        assert "你好世界" in result

    def test_empty_output_returns_exit_code(self):
        # true 命令无 stdout → 返回 exit code 提示
        result = bash_handler(command="true")
        assert "exit code 0" in result or "succeeded" in result

    def test_stderr_included(self):
        result = bash_handler(command="echo out; echo err 1>&2")
        assert "out" in result
        assert "err" in result


# ────────────────────────────────────────────────────────────────────
# BASH_TOOL ToolDef 字段
# ────────────────────────────────────────────────────────────────────

class TestBashToolDef:
    def test_name_is_bash(self):
        assert BASH_TOOL.name == "Bash"

    def test_category_is_shell(self):
        assert BASH_TOOL.category == "shell"

    def test_check_permissions_is_none(self):
        # 由 engine Step 1c' 走专属路径,不通过 ToolDef callback
        assert BASH_TOOL.check_permissions is None

    def test_requires_user_interaction_false(self):
        assert BASH_TOOL.requires_user_interaction is False

    def test_description_mentions_sandbox(self):
        assert "sandbox" in BASH_TOOL.description.lower()

    def test_description_warns_dangerously_disable(self):
        # description 应警告不要随意 disable
        assert "dangerously_disable_sandbox" in BASH_TOOL.description
        assert "sparingly" in BASH_TOOL.description.lower() or "bypass" in BASH_TOOL.description.lower()

    def test_description_says_does_not_bypass_permission(self):
        assert "permission" in BASH_TOOL.description.lower()

    def test_parameters_has_command_required(self):
        assert "command" in BASH_TOOL.parameters["required"]

    def test_parameters_has_dangerously_disable_sandbox(self):
        props = BASH_TOOL.parameters["properties"]
        assert "dangerously_disable_sandbox" in props
        assert props["dangerously_disable_sandbox"]["type"] == "boolean"

    def test_parameters_has_timeout(self):
        props = BASH_TOOL.parameters["properties"]
        assert "timeout" in props
        assert props["timeout"]["type"] == "number"

    def test_parameters_has_working_dir(self):
        props = BASH_TOOL.parameters["properties"]
        assert "working_dir" in props


# ────────────────────────────────────────────────────────────────────
# register_builtin_tools
# ────────────────────────────────────────────────────────────────────

class TestRegisterBuiltin:
    def test_includes_bash(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        assert registry.get("Bash") is BASH_TOOL

    def test_includes_calc_and_search(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        assert registry.get("calc") is not None
        assert registry.get("search") is not None

    def test_bash_in_list_names(self):
        registry = ToolRegistry()
        register_builtin_tools(registry)
        assert "Bash" in registry.list_names()


# ────────────────────────────────────────────────────────────────────
# jsonschema 校验(通过 ToolRegistry.execute)
# ────────────────────────────────────────────────────────────────────

class TestJsonSchemaValidation:
    def test_missing_command_returns_error(self):
        registry = ToolRegistry()
        registry.register(BASH_TOOL)
        result = registry.execute("Bash", {})
        assert result["status"] == "error"
        assert "command" in result["error"].lower() or "校验" in result["error"]

    def test_valid_command_passes_schema(self):
        registry = ToolRegistry()
        registry.register(BASH_TOOL)
        result = registry.execute("Bash", {"command": "echo hi"})
        assert result["status"] == "success"

    def test_dangerously_disable_non_bool_rejected(self):
        registry = ToolRegistry()
        registry.register(BASH_TOOL)
        result = registry.execute("Bash", {
            "command": "echo hi",
            "dangerously_disable_sandbox": "yes",  # 非 bool
        })
        assert result["status"] == "error"

    def test_timeout_non_number_rejected(self):
        registry = ToolRegistry()
        registry.register(BASH_TOOL)
        result = registry.execute("Bash", {
            "command": "echo hi",
            "timeout": "fast",  # 非 number
        })
        assert result["status"] == "error"
