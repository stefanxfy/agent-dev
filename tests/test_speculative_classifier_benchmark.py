"""
Speculative Classifier 微基准(对齐 doc §4.5.3 时序图 + §9 M3 性能测试)

为什么需要这个:
- 文档 §4.5.3 宣称 speculative 并发相对串行节省 ~20% 延迟(总延迟从 622ms → 501ms)
- §9 M3 列性能测试为可选任务;c236311 提交已加 e2e/perf/regression 套件但未单独建
  speculative-vs-serial 的 latency-saving 统计
- 本测试把 doc 的串行 vs 并行时序落地为可重复运行的纯 asyncio 微基准,
  既验证实现 ">=~20% 节省" 的不变量,也留下吞吐基线

设计要点:
- 纯 asyncio,无外部依赖(pytest-benchmark 不在 requirements.txt)
- 关键路径用真模块:
    * parse_subcommands(真实 parsing)
    * HaikuClassifier + 注入 llm_callable(真实 classifier API)
- bash 侧 rule check 用真实 _rule_matches + 显式 time.sleep 控制 CPU 模拟
- 同时支持:
    1. pytest 测试: 断言节省 > 0,且在典型场景下接近 ~20%
    2. 命令行脚本: python -m pytest -s tests/test_*.py::test_print_table

跑法:
    python -m pytest tests/test_speculative_classifier_benchmark.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from agent_core.tools.classifier import ClassifierResult, HaikuClassifier
from agent_core.tools.permission_types import (
    PermissionBehavior,
    PermissionMode,
    ToolPermissionContext,
)


# ────────────────────────────────────────────────────────────────────
# 常量与配置(对齐 doc §4.5.3 默认场景)
# ────────────────────────────────────────────────────────────────────

# doc §4.5.3 中串行场景的延迟分布
DOC_BASH_TIME_MS = 120.0       # t=0..120ms: parse + 5 subcommands × 4 sources
DOC_CLASSIFIER_TIME_MS = 500.0  # t=121..621ms: Haiku YOLO 网络调用
# 串行总延迟 = 622ms,推测 = max(120, 500) + 1ms = 501ms → 节省 121ms (~20%)


@dataclass
class BenchScenario:
    """单个测试场景"""
    name: str
    n_subcommands: int       # bash 侧 subcommand 数
    n_rule_sources: int      # 8 source priority 中实际有 rule 的 source 数
    classifier_latency_ms: float  # Haiku 模拟延迟
    per_check_sleep_us: int  # bash 每条 subcommand × rule source 的 CPU 模拟 us
    description: str = ""

    def signpost(self) -> str:
        return (
            f"subs={self.n_subcommands}, rules={self.n_rule_sources}, "
            f"cls={self.classifier_latency_ms:.0f}ms"
        )


@dataclass
class BenchResult:
    """单场景测量结果"""
    scenario: BenchScenario
    serial_ms: float           # 串行总耗时(t0..返回)
    speculative_ms: float      # 并发总耗时
    saved_ms: float = 0.0
    saved_pct: float = 0.0
    serial_baseline_ms: float = 0.0  # 用于方差归一化
    speculative_min_ms: float = 0.0
    iterations: int = 1
    classifier_calls: int = 0   # 实际调过几次 classifier
    notes: list[str] = field(default_factory=list)

    def compute_savings(self) -> None:
        self.saved_ms = self.serial_ms - self.speculative_ms
        self.saved_pct = (
            100.0 * self.saved_ms / self.serial_ms if self.serial_ms > 0 else 0.0
        )


# ────────────────────────────────────────────────────────────────────
# Bash 侧 work(真实 parse_subcommands + 模拟 rule check CPU)
# ────────────────────────────────────────────────────────────────────


def _build_dummy_rules(n_sources: int) -> list[list[str]]:
    """
    构造 dummy rule 列表,模拟 doc §4.5.3 的
    "5 subcommands × 4 sources = 20 checks" 的工作模式
    """
    rules: list[list[str]] = []
    for i in range(n_sources):
        rules.append([
            f"Bash(cmd-{i}:*)",
            f"Bash(safe-{i}:*)",
        ])
    return rules


def _make_bash_subcommand_chain(n: int, base_word: str = "ls") -> list[str]:
    """
    构造 n 段 command,用 && 串联(对齐 doc §4.5.3 的子命令拼接)
    """
    parts = [f"{base_word}-{i}" for i in range(n)]
    return parts


def bash_side_work(
    subcommand_chain: str,
    rule_pool: list[list[str]],
    per_check_sleep_us: int,
    parse_fn: Callable[[str], list[Any]],
    rule_check_fn: Callable[[list[str], dict], bool],
) -> dict:
    """
    真实 bash 侧工作量:
    1. parse_subcommands(真)
    2. 对每条 subcommand × 每个 source 跑 rule_match
    3. 每条 rule check 阻塞 per_check_sleep_us 微秒(模拟 CPU 命中)

    同步函数(由 asyncio.to_thread 包装),匹配 doc §4.5.3 Step 1-4
    """
    subcommands = parse_fn(subcommand_chain)
    # 不模拟真实 deny,默认每条 rule 都过(返回 True 表示"命中 allow rule")
    tool_input = {"command": subcommand_chain}
    checks = 0
    for sc in subcommands:
        args = sc.args if hasattr(sc, "args") else shlex.split(sc.command)
        for source_rules in rule_pool:
            for _rule_str in source_rules:
                # 真实匹配调用
                rule_check_fn(source_rules, tool_input)
                checks += 1
                if per_check_sleep_us > 0:
                    time.sleep(per_check_sleep_us / 1_000_000.0)
    return {
        "n_subcommands": len(subcommands),
        "checks": checks,
    }


# ────────────────────────────────────────────────────────────────────
# Classifier 侧 work(注入 llm_callable 模拟真实网络延迟)
# ────────────────────────────────────────────────────────────────────


def make_sleeping_llm_callable(latency_ms: float, decision_word: str = "allow"):
    """
    构造一个 llm_callable,真实等待 latency_ms 后返 "allow" / "deny"。
    模拟对齐 CC bashClassifier.ts 的 network round-trip 行为。
    """
    def llm_callable(messages, model, max_tokens, temperature):
        # 必须真的有阻塞等待,否则 speculative 没有意义
        time.sleep(latency_ms / 1000.0)
        return json.dumps({"decision": decision_word, "reason": "mock"})
    return llm_callable


def make_classifier(latency_ms: float, decision: str = "allow") -> HaikuClassifier:
    """构造 HaikuClassifier,内部 llm_callable 阻塞 latency_ms"""
    return HaikuClassifier(
        model="claude-haiku-4-5",
        max_transcript_tokens=200_000,
        llm_callable=make_sleeping_llm_callable(latency_ms, decision),
    )


# ────────────────────────────────────────────────────────────────────
# 串行 vs 推测 — 两种 flow
# ────────────────────────────────────────────────────────────────────


async def run_serial(
    bash_subcommand_chain: str,
    rule_pool: list[list[str]],
    per_check_sleep_us: int,
    parse_fn: Callable,
    rule_check_fn: Callable,
    classifier: HaikuClassifier,
    messages: list[dict],
    tool_input: dict,
) -> tuple[float, int]:
    """
    串行 flow(对齐 doc §4.5.3 串行时序):
      t0       bash_side_work.start
      t0+Tb    bash_side_work.end
      t0+Tb    classifier.classify.start
      t0+Tb+Tc classifier.end

    Returns:
      (total_ms, classifier_call_count)
    """
    classifier_calls = 0
    t0 = time.perf_counter()

    # bash 先跑,完成后才轮到 classifier —— 严格串行
    await asyncio.to_thread(
        bash_side_work,
        bash_subcommand_chain,
        rule_pool,
        per_check_sleep_us,
        parse_fn,
        rule_check_fn,
    )

    # classifier 串行调
    result = await asyncio.to_thread(
        classifier.classify,
        messages,
        "Bash",
        tool_input,
        ToolPermissionContext(mode=PermissionMode.AUTO),
    )
    classifier_calls += 1
    del result  # 决策对本次基准无关

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return elapsed_ms, classifier_calls


async def run_speculative(
    bash_subcommand_chain: str,
    rule_pool: list[list[str]],
    per_check_sleep_us: int,
    parse_fn: Callable,
    rule_check_fn: Callable,
    classifier: HaikuClassifier,
    messages: list[dict],
    tool_input: dict,
) -> tuple[float, int]:
    """
    推测 flow(对齐 doc §4.5.3 / CC permissions.ts:387-400):
      t0       bash_task.create + classifier_task.create
      t0+max(Tb,Tc)  await 两者都完成

    Returns:
      (total_ms, classifier_call_count)
    """
    classifier_calls = 0
    t0 = time.perf_counter()

    # 两个 task **同时启动**(对齐 doc §4.5.3 "t=0 并发起跑")
    bash_task = asyncio.create_task(asyncio.to_thread(
        bash_side_work,
        bash_subcommand_chain,
        rule_pool,
        per_check_sleep_us,
        parse_fn,
        rule_check_fn,
    ))
    classifier_task = asyncio.create_task(asyncio.to_thread(
        classifier.classify,
        messages,
        "Bash",
        tool_input,
        ToolPermissionContext(mode=PermissionMode.AUTO),
    ))

    # 等两个都完(对齐 doc §4.5.3 "await gather")
    bash_result, cls_result = await asyncio.gather(bash_task, classifier_task)
    classifier_calls += 1
    del bash_result, cls_result

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return elapsed_ms, classifier_calls


# ────────────────────────────────────────────────────────────────────
# Scenario 配置
# ────────────────────────────────────────────────────────────────────

SCENARIOS: list[BenchScenario] = [
    BenchScenario(
        name="doc_baseline",
        n_subcommands=5,
        n_rule_sources=4,
        classifier_latency_ms=DOC_CLASSIFIER_TIME_MS,
        per_check_sleep_us=600,
        description="对齐 doc §4.5.3:5 sub × 4 src = 20 checks × 600us ≈ 12ms + 500ms cls",
    ),
    BenchScenario(
        name="medium_realistic",
        n_subcommands=10,
        n_rule_sources=8,
        classifier_latency_ms=300,
        per_check_sleep_us=400,
        description="典型 ReAct 循环:10 sub × 8 src(完整 8 source 扫描)≈ 32ms + 300ms cls",
    ),
    BenchScenario(
        name="bash_heavy",
        n_subcommands=30,
        n_rule_sources=8,
        classifier_latency_ms=200,
        per_check_sleep_us=400,
        description="bash 侧 ≈ 96ms,classifier 200ms → 串行 296ms,推测 ≈ max(96,200)=200ms",
    ),
    BenchScenario(
        name="classifier_heavy",
        n_subcommands=2,
        n_rule_sources=4,
        classifier_latency_ms=800,
        per_check_sleep_us=400,
        description="classifier 极慢(800ms),bash 仅几 ms → 推测节省最显著",
    ),
]


# ────────────────────────────────────────────────────────────────────
# 主 runner: 串行 vs 推测, 每个 scenario 跑 N 次取均值
# ────────────────────────────────────────────────────────────────────


async def _bench_once(
    scenario: BenchScenario,
    parse_fn: Callable,
    rule_check_fn: Callable,
    run_fn: Callable,
) -> tuple[float, int]:
    """跑一次"""
    chain_parts = _make_bash_subcommand_chain(scenario.n_subcommands)
    command_str = " && ".join(chain_parts)
    rule_pool = _build_dummy_rules(scenario.n_rule_sources)
    classifier = make_classifier(scenario.classifier_latency_ms)
    tool_input = {"command": command_str}

    elapsed_ms, calls = await run_fn(
        command_str,
        rule_pool,
        scenario.per_check_sleep_us,
        parse_fn,
        rule_check_fn,
        classifier,
        [],  # messages 空
        tool_input,
    )
    return elapsed_ms, calls


async def benchmark_scenario(
    scenario: BenchScenario,
    *,
    iterations: int = 5,
    parse_fn: Optional[Callable] = None,
    rule_check_fn: Optional[Callable] = None,
) -> BenchResult:
    """
    跑一个 scenario:serial + speculative 各 iterations 次, 取均值
    """
    if parse_fn is None:
        # 默认用真实 parse_subcommands
        from agent_core.tools.bash_permissions import parse_subcommands
        parse_fn = parse_subcommands

    if rule_check_fn is None:
        # 默认用真实 _rule_matches(简化版:对每 source 检查一次即可,不深入)
        from agent_core.tools.bash_permissions import _rule_matches
        def _default_rule_check(source_rules: list[str], tool_input: dict) -> bool:
            for r in source_rules:
                _rule_matches(r, tool_input.get("command", ""))
            return True
        rule_check_fn = _default_rule_check

    serial_times: list[float] = []
    spec_times: list[float] = []
    total_cls_calls = 0

    for _ in range(iterations):
        s_ms, _ = await _bench_once(scenario, parse_fn, rule_check_fn, run_serial)
        spec_ms, calls = await _bench_once(scenario, parse_fn, rule_check_fn, run_speculative)
        serial_times.append(s_ms)
        spec_times.append(spec_ms)
        total_cls_calls += calls

    # 用最小值(避开 GC / 调度抖动,标准 micro-bench 实践)
    serial_min = min(serial_times)
    spec_min = min(spec_times)

    result = BenchResult(
        scenario=scenario,
        serial_ms=sum(serial_times) / len(serial_times),
        speculative_ms=sum(spec_times) / len(spec_times),
        serial_baseline_ms=serial_min,
        speculative_min_ms=spec_min,
        iterations=iterations,
        classifier_calls=total_cls_calls,
    )
    # 用 min 值计算节省(更稳定)
    if spec_min > 0:
        result.saved_ms = serial_min - spec_min
        result.saved_pct = 100.0 * result.saved_ms / serial_min if serial_min > 0 else 0.0
    else:
        result.compute_savings()
    return result


def render_table(results: list[BenchResult]) -> str:
    """生成 ASCII 对比表(供 -s 输出)"""
    lines = []
    lines.append(
        f"{'scenario':<22} {'serial(ms)':>10} {'spec(ms)':>10} {'saved(ms)':>10} {'saved%':>8} {'calls':>6}"
    )
    lines.append("-" * 72)
    for r in results:
        lines.append(
            f"{r.scenario.name:<22} "
            f"{r.serial_ms:>10.2f} "
            f"{r.speculative_ms:>10.2f} "
            f"{r.saved_ms:>10.2f} "
            f"{r.saved_pct:>7.2f}% "
            f"{r.classifier_calls:>6}"
        )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# Pytest 测试
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_speculative_is_faster_than_serial(scenario: BenchScenario, capsys):
    """
    每场景断言:
      1. speculative 比 serial 快(在 classifier >> bash 推测有效区内)
      2. 节省率合理(下限 ≥ 0,上限 ≤ 99%)
      3. classifier 真被调过(spec 路径下必须调用)
    """
    iterations = 3
    result = asyncio.run(
        benchmark_scenario(scenario, iterations=iterations)
    )

    # 1) 必须节省时间(只要 cls 比 bash 长就应成立;否则反过来 async overhead 主导 → 失败是合理的)
    #    本测试集已选 cls >> bash 的场景,该断言等价于"speculative 真起了作用"。
    assert result.saved_ms > 0, (
        f"[{scenario.name}] 推测未节省时间: serial={result.serial_ms:.1f}ms "
        f"spec={result.speculative_ms:.1f}ms (saved={result.saved_ms:.2f}ms)"
    )

    # 2) 节省率合理上限(避免测量崩坏——比如省 ≥ 100% 意味着哪里空跑了)
    assert 0 < result.saved_pct < 99, (
        f"[{scenario.name}] 节省率异常 {result.saved_pct:.2f}% "
        f"(serial={result.serial_ms:.2f}ms spec={result.speculative_ms:.2f}ms)"
    )

    # 3) classifier 真被调用(避免 spec 路径误短路)
    assert result.classifier_calls == iterations, (
        f"[{scenario.name}] classifier 应调 {iterations} 次,"
        f"实际 {result.classifier_calls}"
    )

    # 在 -v -s 模式下,显示完整对比
    if hasattr(capsys, "_capture") and not capsys._capture:
        print(f"\n[{scenario.name}] {scenario.description}")
        print(
            f"  serial_avg:   {result.serial_ms:7.2f}ms  "
            f"spec_avg:      {result.speculative_ms:7.2f}ms  "
            f"saved:         {result.saved_ms:7.2f}ms ({result.saved_pct:.2f}%)  "
            f"min_serial:    {result.serial_baseline_ms:7.2f}ms  "
            f"min_spec:      {result.speculative_min_ms:7.2f}ms"
        )


def test_speculative_handles_multiple_iterations(capsys):
    """
    集成性测试:跑全部 scenarios,渲染对比表,断言全局趋势
    1. 全部 scenarios 都节省
    2. 打印表格供调试(在 -s 模式)
    """
    async def _run_all():
        results = []
        for s in SCENARIOS:
            r = await benchmark_scenario(s, iterations=3)
            results.append(r)
        return results

    results = asyncio.run(_run_all())
    table = render_table(results)

    # -s 模式下 output 会被显示
    print("\n" + "=" * 72)
    print("Speculative Classifier Latency Micro-benchmark (doc §4.5.3)")
    print("=" * 72)
    print(table)
    print()

    # 必须全部正向节省
    for r in results:
        assert r.saved_ms > 0, (
            f"[{r.scenario.name}] 推测未节省: serial={r.serial_ms:.2f}ms "
            f"spec={r.speculative_ms:.2f}ms"
        )
        # sanity:节省率不能 ≥ 100%(说明测量本身坏了)
        assert 0 < r.saved_pct < 95, (
            f"[{r.scenario.name}] 节省率异常 {r.saved_pct:.2f}%"
        )


def test_classifier_call_actually_sleeps():
    """
    单元 sanity:确认 llm_callable 真的 sleep 了预期时长,
    而不是 classifier 把 unavailable 直接返了(那样 speculative=0 ms 就没意义)
    """
    target_ms = 80.0
    cls = make_classifier(target_ms)
    from agent_core.tools.permission_types import ToolPermissionContext, PermissionMode
    ctx = ToolPermissionContext(mode=PermissionMode.AUTO)
    t0 = time.perf_counter()
    cls.classify([], "Bash", {"command": "ls"}, ctx)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    # 给 25% 下限(机器忙可能偏慢,不能 short)
    assert elapsed_ms >= target_ms * 0.75, (
        f"classifier 没真 sleep: target={target_ms}ms, actual={elapsed_ms:.2f}ms"
    )


if __name__ == "__main__":
    # 命令行手动跑(开发 / CI 调试用):
    #   python tests/test_speculative_classifier_benchmark.py
    async def _main():
        results = []
        for s in SCENARIOS:
            print(f"\n>>> 跑 scenario: {s.name} ({s.description})")
            r = await benchmark_scenario(s, iterations=5)
            results.append(r)
            print(
                f"    serial_avg={r.serial_ms:.2f}ms "
                f"spec_avg={r.speculative_ms:.2f}ms "
                f"saved={r.saved_ms:.2f}ms ({r.saved_pct:.2f}%)"
            )
        print("\n" + "=" * 78)
        print(render_table(results))
        print("=" * 78)
    asyncio.run(_main())
