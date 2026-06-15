#!/usr/bin/env python3
"""
Summary 质量验证工具

对照 compact.py 的 COMPACT_SYSTEM_PROMPT 检查 session 的 summary 是否符合要求。

要求来源：agent_core/context/compact.py:40-56
- LLM 必须输出 <analysis> 和 <summary> 标签
- <summary> 必须包含 4 段结构：用户目标/关键决策/当前状态/待办事项
- 防漂移规则：用户消息必须逐字引用（verbatim quotes）
- 必须以 [Previous conversation summarized] 开头

用法：
    # 验证单个 session
    python3 scripts/verify_summary.py 7f071c62

    # 验证所有 session
    python3 scripts/verify_summary.py --all

    # 自定义扫描目录
    python3 scripts/verify_summary.py 7f071c62 --dir data/sessions

    # 严格模式（任何 XML 标签缺失就 fail）
    python3 scripts/verify_summary.py 7f071c62 --strict

退出码：
    0 = 通过（或部分通过但有合理原因）
    1 = 完全不符合
    2 = 文件/读取错误
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


# compact.py 要求的标签和 4 段结构
REQUIRED_SEGMENTS = ["用户目标", "关键决策", "当前状态", "待办事项"]
REQUIRED_PREFIX = "[Previous conversation summarized]"
XML_TAGS = ["<analysis>", "</analysis>", "<summary>", "</summary>"]


def find_session_file(session_id: str, search_dirs: List[str]) -> Optional[Path]:
    """按 session_id 找 .jsonl"""
    for d in search_dirs:
        p = Path(d) / f"{session_id}.jsonl"
        if p.exists():
            return p
    for d in search_dirs:
        base = Path(d)
        if not base.exists():
            continue
        for jsonl in base.rglob(f"{session_id}.jsonl"):
            return jsonl
    return None


def extract_summary_from_session(path: Path) -> List[dict]:
    """从 session 中提取所有 summary entry（含旧的 type=summary 格式）"""
    summaries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 新格式
            if e.get("type") == "user" and e.get("message", {}).get("isCompactSummary"):
                summaries.append(e)
            # 旧格式兼容
            if e.get("type") == "summary":
                summaries.append(e)
    return summaries


def check_summary(entry: dict, strict: bool = False) -> dict:
    """对单个 summary entry 做完整验证

    Returns:
        dict: {
            "entry_idx": int,
            "passed": bool,
            "checks": [(name, ok, detail)],
            "content": str,
        }
    """
    msg = entry.get("message", {})
    if not msg:
        # 旧格式可能在 entry 顶层
        content = entry.get("content", "")
    else:
        content = msg.get("content", "")

    if not content:
        return {
            "entry_idx": -1,
            "passed": False,
            "checks": [("内容非空", False, "content 为空")],
            "content": "",
        }

    checks = []

    # 1. 前缀
    has_prefix = content.startswith(REQUIRED_PREFIX + "\n\n")
    checks.append((
        f"前缀 {REQUIRED_PREFIX!r}",
        has_prefix,
        f"开头: {content[:30]!r}",
    ))

    # 2. <analysis> 开标签
    has_analysis_open = "<analysis>" in content
    checks.append((
        "<analysis> 标签",
        has_analysis_open,
        "LLM 真的按 prompt 输出了 analysis 块" if has_analysis_open
        else "缺失（GLM 等模型可能直接输出 4 段结构）",
    ))

    # 3. </analysis> 闭标签
    has_analysis_close = "</analysis>" in content
    checks.append((
        "</analysis> 闭合",
        has_analysis_close,
        "" if has_analysis_close else "缺失",
    ))

    # 4. <summary> 开标签
    has_summary_open = "<summary>" in content
    checks.append((
        "<summary> 标签",
        has_summary_open,
        "LLM 真的按 prompt 输出了 summary 块" if has_summary_open
        else "缺失（GLM 可能直接以 4 段结构开头）",
    ))

    # 5. </summary> 闭标签
    has_summary_close = "</summary>" in content
    checks.append((
        "</summary> 闭合",
        has_summary_close,
        "" if has_summary_close else "缺失",
    ))

    # 6. 4 段结构（无论是否有 XML 标签）
    body = content.replace(REQUIRED_PREFIX + "\n\n", "")
    # 剥掉 <summary> 标签内容
    if "</summary>" in body:
        body = body.split("</summary>")[0]
    if body.startswith("<summary>"):
        body = body[len("<summary>"):]
    # 剥掉 <analysis> 块
    if "</analysis>" in body:
        body = body.split("</analysis>", 1)[1]

    seg_results = []
    for seg in REQUIRED_SEGMENTS:
        has = seg in body
        seg_results.append(has)
        checks.append((f"4 段结构: {seg}", has, ""))

    has_all_segments = all(seg_results)

    # 7. 长度
    body_len = len(body.strip())
    if body_len < 100:
        length_ok = False
        length_note = f"太短 ({body_len} 字符) — 压缩效果可能不到位"
    elif body_len > 2000:
        length_ok = False
        length_note = f"太长 ({body_len} 字符) — 节省 token 效果差"
    else:
        length_ok = True
        length_note = f"合理 ({body_len} 字符)"
    checks.append(("合理长度", length_ok, length_note))

    # 8. 整体通过判断
    # 核心要求（必须满足）：
    #   - 前缀
    #   - 4 段结构
    #   - 长度
    # 可选要求（缺失但不影响加载）：
    #   - XML 标签（compact.py _extract_summary 有兜底）

    core_checks = [
        has_prefix,
        has_all_segments,
        length_ok,
    ]
    xml_checks = [has_analysis_open, has_analysis_close, has_summary_open, has_summary_close]
    all_xml_present = all(xml_checks)

    # 严格模式：XML 标签必须全有
    if strict:
        passed = all(c[1] for c in checks)
    else:
        # 默认：核心 + 至少 1 个 XML 标签 OR 4 段结构完整
        passed = all(core_checks) and (all_xml_present or has_all_segments)

    return {
        "passed": passed,
        "checks": checks,
        "content": content,
        "core_ok": all(core_checks),
        "xml_ok": all_xml_present,
    }


def print_session_report(session_id: str, summaries: List[dict], results: List[dict]):
    """打印单个 session 的验证报告"""
    print("=" * 70)
    print(f"📄 {session_id}: 找到 {len(summaries)} 个 summary")
    print("=" * 70)

    if not summaries:
        print("  ℹ️  该 session 没有 summary（未触发过压缩）")
        return True

    all_passed = True
    for i, (entry, result) in enumerate(zip(summaries, results)):
        print(f"\n  📋 Summary #{i+1} (file index: #{i+1})")
        print(f"     长度: {len(result['content'])} 字符")
        print(f"     uuid: {entry.get('uuid', 'None')[:8] if entry.get('uuid') else 'None'}")
        print(f"     parent: {entry.get('parentUuid', 'None')[:8] if entry.get('parentUuid') else 'None'}")
        print(f"     核心要求: {'✅' if result['core_ok'] else '❌'}")
        print(f"     XML 标签: {'✅ 完整' if result['xml_ok'] else '⚠️  部分/缺失'}")
        print(f"     整体: {'✅ PASS' if result['passed'] else '❌ FAIL'}")
        print()
        print(f"     详细检查:")
        for name, ok, detail in result["checks"]:
            sym = "✅" if ok else "❌"
            print(f"       {sym} {name}")
            if detail:
                print(f"          {detail}")
        if not all(r["passed"] for r in results):
            all_passed = False

    return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="验证 session summary 是否符合 compact.py 要求",
    )
    parser.add_argument("session_id", nargs="?", help="Session ID")
    parser.add_argument("--all", action="store_true", help="验证所有 session")
    parser.add_argument("--dir", action="append", help="扫描目录")
    parser.add_argument("--strict", action="store_true", help="严格模式：XML 标签必须全有")

    args = parser.parse_args()

    search_dirs = args.dir if args.dir else ["data/sessions", ".agent_data", ".agent_data/sessions"]

    if args.all:
        # 扫所有 session
        all_sessions = set()
        for d in search_dirs:
            base = Path(d)
            if not base.exists():
                continue
            for jsonl in base.rglob("*.jsonl"):
                all_sessions.add(jsonl.stem)

        if not all_sessions:
            print(f"❌ 在 {search_dirs} 没找到任何 .jsonl", file=sys.stderr)
            return 1

        print(f"🔍 扫描到 {len(all_sessions)} 个 session")
        all_passed = True
        for sid in sorted(all_sessions):
            path = find_session_file(sid, search_dirs)
            if not path:
                continue
            summaries = extract_summary_from_session(path)
            if not summaries:
                continue
            results = [check_summary(s, strict=args.strict) for s in summaries]
            ok = print_session_report(sid, summaries, results)
            if not ok:
                all_passed = False
            print()

        print("=" * 70)
        print(f"📊 整体: {'✅ 全部通过' if all_passed else '⚠️  有 session 不符合'}")
        print("=" * 70)
        return 0 if all_passed else 1

    elif args.session_id:
        path = find_session_file(args.session_id, search_dirs)
        if not path:
            print(f"❌ 找不到 session '{args.session_id}'", file=sys.stderr)
            return 2

        summaries = extract_summary_from_session(path)
        results = [check_summary(s, strict=args.strict) for s in summaries]
        ok = print_session_report(args.session_id, summaries, results)
        return 0 if ok else 1
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
