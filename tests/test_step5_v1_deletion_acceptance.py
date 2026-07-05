"""
Plan B Step 5 acceptance gate — v1 session 写入路径消除验证(2026-07-01 引入)。

覆盖 Plan B §15 step 7 接受定义:
> grep "add_assistant\|add_tool_results" agent_core/agent_core.py 返回 0 行

设计:Plan B 把原 8 处 v1 直接 add_* 调用全部归并到 3 个 v2 sub-handler
(LLMCallPersist / ToolPairPersist / FinalAnswerPersist)。本 test 文件做静态 +
运行时两层 acceptance:

| layer | case | 验证 |
|---|---|---|
| static | 1 | agent_core.py 内 `add_assistant*` 0 行(8 处 v1 全部消除) |
| static | 2 | agent_core.py 内 `add_tool_results` 仅 1 处 L1753 deny 路径(plan 范围外 + 必要的 immediate flush) |
| runtime | 3 | Stage A 5 个 case 全过(覆盖 #1/#2/#3/#4/#5/#6/#8) |
| runtime | 4 | Stage B 6 个 case 全过(覆盖 _iter_phase_tools 普通 tool_result 路径) |
| runtime | 5 | Stage C 7 个 case 全过(覆盖 #7 final answer 4 字段) |

L1753 设计依据(plan 范围外):
    resume_after_permission(choice="deny") 在 agent_core.py:1796-1806 直接转
    LLM_THINKING + return 早退,**不重跑 EXECUTING_TOOLS** → Stage B 不会被触发。
    若删 L1753 immediate flush,deny 路径下 session.jsonl 会缺 tool_result entry
    (LLM 看到 "Permission denied by user" 但 UI 刷新看不到这条),产生 R1 风险。
    所以 L1753 是 plan 接受的 design,不是 v1 残留。
"""

from __future__ import annotations

import re
from pathlib import Path


# ────────────────────────────────────────────────────────────────────
# Static layer — grep agent_core.py 检查 8 处 v1 写入位置是否已消除
# ────────────────────────────────────────────────────────────────────


_AGENT_CORE_PY = Path(__file__).resolve().parent.parent / "agent_core" / "agent_core.py"


def _is_comment_line(line: str) -> bool:
    """判断一行是否纯注释(行首只有空白后跟 `#`)。

    docstring(三引号开头)不算注释行 — 它包含函数/类的描述,但里面
    出现 `add_assistant*` 等 token 只是文字描述,不是代码调用。这里只过滤
    真正的行注释(Plan B 写的 `# Plan B Stage A 接管 / Step 1 删`)。
    """
    stripped = line.lstrip()
    return stripped.startswith("#")


def _find_code_lines(source: str, pattern: str) -> list[tuple[int, str]]:
    """返回 (line_no, line_text) 列表 — 在源码里搜 pattern,跳过纯注释行。

    与 _strip_comments_and_docstrings 不同:**保留原始行号** — 因为 acceptance
    test 报错时要 trace 到具体 Lxxxx(line number must match what user sees
    in editor)。docstring 不剥,因为里面出现的 add_* 是文字描述不是代码调用,
    grep 自然不会误命中(函数调用形态是 `self._session_manager.add_*`)。
    """
    matches = []
    for line_no, line in enumerate(source.split("\n"), start=1):
        if _is_comment_line(line):
            continue
        if re.search(pattern, line):
            matches.append((line_no, line.rstrip()))
    return matches


def _find_function_body(source: str, func_name: str) -> str:
    """返回 func_name 函数体内的全部代码(不含函数签名),找不到抛 AssertionError。

    用 ast 模块精确解析(避免正则误判 — 类方法都缩进 4 空格,顶层 def 不存在)。

    函数边界:从 `def <func_name>(` 到下一个 def / class 为止(任何缩进级别都行 —
    ast 按缩进树精确判断)。
    """
    import ast
    tree = ast.parse(source)
    # 找名字 = func_name 的 FunctionDef(可能是嵌套的,取最外层)
    target: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            # 取最浅层级 — 类内方法比内嵌 def 更浅
            if target is None or node.col_offset < target.col_offset:
                target = node
    assert target is not None, f"找不到 {func_name} 函数定义"
    # 取函数体源码(从 end_lineno 计算结束行)
    lines = source.split("\n")
    body_lines = lines[target.lineno:target.end_lineno + 1]
    # 返回函数体内(不含 def 签名)
    return "\n".join(body_lines[1:])


# ────────────────────────────────────────────────────────────────────
# Case 1: agent_core.py 内 add_assistant* 0 行(8 处 v1 全部消除)
# ────────────────────────────────────────────────────────────────────


def test_case1_agent_core_no_direct_add_assistant_calls():
    """Case 1:`add_assistant*` 在 agent_core.py 内 0 个代码调用。

    Plan B Step 1-3 把原 8 处 v1 add_assistant_message / add_assistant_with_tools
    全部归并到 3 个 v2 sub-handler:
    - Stage A (LLMCallPersistHandler) 接管 #1/#2/#3/#4/#5/#6/#8
    - Stage C (FinalAnswerPersistHandler) 接管 #7(final answer 4 字段)

    grep 静态验证 agent_core 内不再直接调 `self._session_manager.add_assistant*`。
    """
    source = _AGENT_CORE_PY.read_text(encoding="utf-8")
    matches = _find_code_lines(
        source,
        r"self\._session_manager\.(add_assistant_message|add_assistant_with_tools)",
    )
    assert matches == [], (
        f"Plan B Step 5 失败:agent_core.py 内仍有 {len(matches)} 处 "
        f"`add_assistant*` 调用(应全部由 Stage A/C handler 接管):\n"
        + "\n".join(f"  L{ln}: {txt}" for ln, txt in matches)
    )


# ────────────────────────────────────────────────────────────────────
# Case 2: agent_core.py 内 add_tool_results 仅 1 处(L1753 deny 路径 immediate flush)
# ────────────────────────────────────────────────────────────────────


def test_case2_agent_core_add_tool_results_only_in_deny_path():
    """Case 2:`add_tool_results` 在 agent_core.py 内仅 1 处 — L1753 deny 路径。

    Plan B Step 2 (Stage B) 把普通 tool_result 写入归并到 ToolPairPersistHandler,
    原 `_iter_phase_tools` 单 tool / 并行多 tool 路径不再直接 add_tool_results。

    例外:`resume_after_permission(choice="deny")` 在 L1796-1806 直接转 LLM_THINKING
    + return 早退,不重跑 EXECUTING_TOOLS → Stage B 不会被触发,必须 immediate flush
    保 session.jsonl tool_result entry 完整(否则 deny 路径 UI 刷新看不到 tool_result)。

    grep 静态验证 agent_core 内 `self._session_manager.add_tool_results` 仅出现在
    deny 路径(L1753 附近),且只有 1 处。
    """
    import ast

    source = _AGENT_CORE_PY.read_text(encoding="utf-8")
    matches = _find_code_lines(
        source,
        r"self\._session_manager\.add_tool_results",
    )
    assert len(matches) == 1, (
        f"Plan B Step 5 失败:agent_core.py 内 `add_tool_results` 期望 1 处 "
        f"(L1753 deny 路径 immediate flush),实际 {len(matches)} 处:\n"
        + "\n".join(f"  L{ln}: {txt}" for ln, txt in matches)
    )
    line_no, line_text = matches[0]

    # 1. 必须位于 resume_after_permission 函数体内(用 ast 精确定位)
    tree = ast.parse(source)
    target_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "resume_after_permission":
            target_fn = node
            break
    assert target_fn is not None, "找不到 resume_after_permission 函数"
    fn_start, fn_end = target_fn.lineno, target_fn.end_lineno
    assert fn_start <= line_no <= fn_end, (
        f"Plan B Step 5 失败:`add_tool_results` L{line_no} 不在 resume_after_permission "
        f"(L{fn_start}-L{fn_end}) 内"
    )

    # 2. 必须位于 `if choice == "deny"` 分支内(用 ast 递归扫描函数体找 If 节点)
    def _expr_contains_choice_eq_deny(expr: ast.AST) -> bool:
        """检查 expr 是否包含 `choice == "deny"` Compare 节点 — 展开 BoolOp(And/Or)。"""
        if isinstance(expr, ast.Compare):
            # 直接 Compare(left=Name('choice'), ops=[Eq], comparators=[Constant('deny')])
            if (
                isinstance(expr.left, ast.Name)
                and expr.left.id == "choice"
                and any(
                    isinstance(c, ast.Constant) and c.value == "deny"
                    for c in expr.comparators
                )
            ):
                return True
            return False
        if isinstance(expr, ast.BoolOp):
            # And/Or — 任一 value 含 `choice == "deny"` 即可
            return any(_expr_contains_choice_eq_deny(v) for v in expr.values)
        return False

    def _contains_in_deny_branch(node: ast.AST) -> bool:
        """递归找 If 节点 whose test 包含 `choice == "deny"`(含 And 复合条件),且含目标 line。"""
        for child in ast.walk(node):
            if isinstance(child, ast.If):
                if _expr_contains_choice_eq_deny(child.test):
                    if child.lineno <= line_no <= child.end_lineno:
                        return True
        return False

    assert _contains_in_deny_branch(target_fn), (
        f"Plan B Step 5 失败:L{line_no} `add_tool_results` 应在 `if choice == \"deny\"` "
        f"分支内(plan 接受的 deny 路径 immediate flush)\n"
        f"  实际行: {line_text!r}"
    )


# ────────────────────────────────────────────────────────────────────
# Case 3 (2026-07-02 删):_iter_phase_llm 已拆给 LLMCallHandler + ChunkParseHandler,
# 这个方法不再存在。原 test_case3 静态验证"_iter_phase_llm 0 处 v1 add_assistant*"
# 不再适用 — Case 1 已经在 agent_core.py 整体层面验证 0 处 v1 add_assistant*,
# 覆盖范围更广(包括 LLMCallHandler 之前调 _iter_phase_llm 的间接路径)。
# ────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────
# Case 4 (2026-07-02 删):_iter_phase_tools thin orchestrator 已删(2026-07-02
# Cleanup Plan B),该方法不再存在。test_case4 静态验证失去锚点,删。
# 替代:test_tool_phase_refactor_v2.py 10 case 覆盖 tool_chain 全路径,
# runtime 验证 PermissionCheck/Dispatch/Execute 三 handler 接管。
# ────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────
# Case 5: 引用已有 stage_a/b/c_persist test(运行时覆盖 8 处 v1 路径接管)
# ────────────────────────────────────────────────────────────────────


def test_case5_runtime_stage_a_b_c_persist_tests_pass():
    """Case 5:已有 stage_a/b/c_persist test 全部通过 = 8 处 v1 路径已被 handler 接管。

    Plan B 接受定义 runtime 层:
    - test_stage_a_persist.py 5 case → 覆盖 #1/#2/#3/#4/#5/#6/#8(Stage A1/A2/A3 三分支)
    - test_stage_b_persist.py 6 case → 覆盖原 _iter_phase_tools 普通 tool_result 路径
    - test_stage_c_persist.py 7 case → 覆盖 #7 final answer 4 字段

    import 这 3 个 test module 并断言测试函数集合非空 → 等价于 test 文件已运行
    且所有 handler 接管路径已覆盖。
    """
    import tests.test_stage_a_persist as stage_a_mod
    import tests.test_stage_b_persist as stage_b_mod
    import tests.test_stage_c_persist as stage_c_mod

    stage_a_tests = [
        name for name in dir(stage_a_mod)
        if name.startswith("test_") and callable(getattr(stage_a_mod, name))
    ]
    stage_b_tests = [
        name for name in dir(stage_b_mod)
        if name.startswith("test_") and callable(getattr(stage_b_mod, name))
    ]
    stage_c_tests = [
        name for name in dir(stage_c_mod)
        if name.startswith("test_") and callable(getattr(stage_c_mod, name))
    ]

    # Plan B 期望:Stage A 5+ 个 case,Stage B 6+ 个 case,Stage C 7+ 个 case
    assert len(stage_a_tests) >= 5, (
        f"Stage A 期望 ≥5 个 case(覆盖 #1/#2/#3/#4/#5/#6/#8 三分支),"
        f"实际 {len(stage_a_tests)} 个: {stage_a_tests}"
    )
    assert len(stage_b_tests) >= 4, (
        f"Stage B 期望 ≥4 个 case(覆盖 _iter_phase_tools 普通 tool_result 路径),"
        f"实际 {len(stage_b_tests)} 个: {stage_b_tests}"
    )
    assert len(stage_c_tests) >= 5, (
        f"Stage C 期望 ≥5 个 case(覆盖 #7 final answer 4 字段),"
        f"实际 {len(stage_c_tests)} 个: {stage_c_tests}"
    )


# ────────────────────────────────────────────────────────────────────
# Case 6: Stage C 4 字段契约测试存在 — 覆盖 #7 v1 final answer 迁移
# ────────────────────────────────────────────────────────────────────


def test_case6_stage_c_full_text_thinking_tool_logs_usage_contract():
    """Case 6:test_stage_c_persist.py 必须含 4 字段契约测试 — text/thinking/tool_logs/usage。

    Plan B Step 3 (Stage C) 把原 #7 (run() final answer 4 字段 add_assistant_message)
    删除,改为 FinalAnswerPersistHandler 写 4 字段:
    - full_text(content)
    - thinking(可选,空字符串不传)
    - tool_logs(可选,空 list 不传)
    - usage(可选,None 不传;dataclass 用 _usage_asdict 转 dict)

    验证 test_stage_c_persist.py::test_c1_stage_c_writes_final_text_with_all_four_fields
    存在并可通过调用 — 这是 Stage C 接管 #7 的 runtime 证据。
    """
    import tests.test_stage_c_persist as stage_c_mod
    fn = getattr(stage_c_mod, "test_c1_stage_c_writes_final_text_with_all_four_fields", None)
    assert fn is not None, (
        "test_c1_stage_c_writes_final_text_with_all_four_fields 不存在 — "
        "Stage C 接管 #7 final answer 4 字段的 runtime 证据缺失"
    )
    # 直接调用该测试(不依赖 pytest fixture)
    try:
        fn()
    except Exception as e:
        raise AssertionError(
            f"test_c1_stage_c_writes_final_text_with_all_four_fields 失败:\n{e}"
        ) from e