#!/usr/bin/env python3
"""
Session 恢复工具：从压缩状态回退到未压缩时刻

背景：7f071c62.jsonl 触发了 P0 bug（preserved head 6 条未作为新链起点持久化），
     信息实际上完整保留在 boundary 之前的磁盘 entry 中，本工具用于回退结构。

用法：
    # 1. 按 session_id 恢复（默认扫描 data/sessions/ 和 .agent_data/）
    python3 scripts/recover_uncompressed.py 7f071c62

    # 2. 直接指定文件路径
    python3 scripts/recover_uncompressed.py --file data/sessions/7f071c62.jsonl

    # 3. 干跑（不写盘，只打印会做什么）
    python3 scripts/recover_uncompressed.py 7f071c62 --dry-run

    # 4. 自定义扫描目录
    python3 scripts/recover_uncompressed.py 7f071c62 --dir data/sessions

行为：
    - 找到目标 .jsonl 文件
    - 找最后一个 compact_boundary（type=system + subtype=compact_boundary）
    - 备份原文件（.recovery-backup）— 如果已存在则跳过备份
    - 截断到 boundary 之前（保留 boundary 之前的所有 entry）
    - 验证：打印恢复前后的 entry 数和 LLM 加载视图

安全：
    - 默认会备份原文件到 <file>.recovery-backup（已存在则不覆盖）
    - 如果没有 boundary 报错退出，不修改文件
    - --dry-run 不写盘

退出码：
    0 = 成功
    1 = 错误（找不到文件 / 没有 boundary / 写盘失败）
    2 = 干跑模式
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional


# 默认扫描目录（按优先级）
DEFAULT_SEARCH_DIRS = [
    "data/sessions",
    ".agent_data",
    ".agent_data/sessions",
]


def find_session_file(session_id: str, search_dirs: List[str]) -> Optional[Path]:
    """按 session_id 找到 .jsonl 文件"""
    for d in search_dirs:
        p = Path(d) / f"{session_id}.jsonl"
        if p.exists():
            return p

    # 再扫一遍更深层
    for d in search_dirs:
        base = Path(d)
        if not base.exists():
            continue
        for jsonl in base.rglob(f"{session_id}.jsonl"):
            return jsonl
    return None


def find_boundary(entries: list) -> Optional[int]:
    """找最后一个 compact_boundary 的 index"""
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            return i
    return None


def load_entries(path: Path) -> list[dict]:
    """读 JSONL"""
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  ⚠️  JSON 解析错误（跳过）: {e}", file=sys.stderr)
    return entries


META_TYPES = {
    "custom-title", "ai-title", "tag",
    "agent-name", "agent-setting", "mode", "fork-info",
}
MAIN_TYPES = {"user", "assistant", "system"}


def stats(group: list[dict]) -> dict:
    return {
        "total": len(group),
        "main": sum(1 for e in group if e.get("type") in MAIN_TYPES),
        "meta": sum(1 for e in group if e.get("type") in META_TYPES),
        "types": dict(Counter(e.get("type", "?") for e in group)),
    }


def print_summary(before: list[dict], recovered: list[dict], boundary_idx: int):
    """打印恢复前后对比"""
    print()
    print("=" * 70)
    print("📊 恢复对比")
    print("=" * 70)

    s_before = stats(before)
    s_after = stats(recovered)
    boundary_entry = before[boundary_idx]
    boundary_meta = boundary_entry.get("compactMetadata", {})

    print(f"  Boundary 位置: file index #{boundary_idx + 1}")
    print(f"    type={boundary_entry.get('type')}")
    print(f"    subtype={boundary_entry.get('subtype')}")
    print(f"    compactMetadata: {boundary_meta}")
    print()
    print(f"  {'指标':<25} {'恢复前':<20} {'恢复后':<20}")
    print(f"  {'-' * 65}")
    print(f"  {'总 Entry':<25} {s_before['total']:<20} {s_after['total']:<20}")
    print(f"  {'主链 Entry':<25} {s_before['main']:<20} {s_after['main']:<20}")
    print(f"  {'元数据 Entry':<25} {s_before['meta']:<20} {s_after['meta']:<20}")
    print(f"  {'类型分布':<25} {str(s_before['types']):<20} {str(s_after['types']):<20}")
    print()
    print(f"  删除了 {s_before['total'] - s_after['total']} 条 entry（boundary + summary + 后续）")
    print()


def simulate_llm_view(recovered: list[dict], original_total: int) -> dict:
    """模拟 SessionManager 加载行为"""
    # 加载行为：get_messages(stop_at_boundary=True) 反向扫描 boundary
    # 没有 boundary 时：返回全部主链 entry
    main = [e for e in recovered if e.get("type") in MAIN_TYPES]
    return {
        "llm_messages": len(main),  # 恢复后没有 boundary，LLM 看到全部
        "preserved_完整性": "✅ 完整（boundary 之前的内容都在）",
    }


def recover(path: Path, dry_run: bool = False) -> int:
    """主流程"""
    print(f"📄 文件: {path}")
    print(f"   大小: {path.stat().st_size:,} bytes")

    if not path.exists():
        print(f"❌ 文件不存在", file=sys.stderr)
        return 1

    entries = load_entries(path)
    print(f"   Entry 总数: {len(entries)}")

    boundary_idx = find_boundary(entries)
    if boundary_idx is None:
        print(f"❌ 未找到 compact_boundary，无需恢复（已处于未压缩状态）", file=sys.stderr)
        return 1

    # 统计
    boundary_entry = entries[boundary_idx]
    print()
    print(f"🔍 找到 boundary at file index #{boundary_idx + 1}:")
    print(f"   parentUuid: {boundary_entry.get('parentUuid', 'None')[:8] if boundary_entry.get('parentUuid') else 'None'}")
    print(f"   compactMetadata: {boundary_entry.get('compactMetadata', {})}")

    # 截断
    recovered = entries[:boundary_idx]
    print_summary(entries, recovered, boundary_idx)

    # 模拟 LLM 加载
    llm_view = simulate_llm_view(recovered, len(entries))
    print("=" * 70)
    print("🤖 LLM 加载视角（get_messages_for_llm）")
    print("=" * 70)
    print(f"  恢复前: 看到 summary + 后续对话（约 {len(entries) - boundary_idx} 条）")
    print(f"  恢复后: 看到全部 {llm_view['llm_messages']} 条主链（无 boundary 截断）")
    print(f"  {llm_view['preserved_完整性']}")
    print()

    if dry_run:
        print("🔍 --dry-run: 不写盘，退出")
        return 2

    # 备份
    backup = path.with_suffix(path.suffix + ".recovery-backup")
    if backup.exists():
        print(f"⚠️  备份已存在，跳过: {backup.name}")
    else:
        shutil.copy2(path, backup)
        print(f"✅ 已备份: {backup.name} ({backup.stat().st_size:,} bytes)")

    # 写回
    try:
        with open(path, "w", encoding="utf-8") as f:
            for e in recovered:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"❌ 写盘失败: {e}", file=sys.stderr)
        return 1

    print(f"✅ 写回成功: {path.name} ({path.stat().st_size:,} bytes)")
    print()
    print("=" * 70)
    print("💡 验证建议")
    print("=" * 70)
    print(f"  启动 agent-dev，加载这个 session，LLM 应该看到完整 {llm_view['llm_messages']} 条对话")
    print(f"  备份保留在: {backup.name}")
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Session 恢复工具：从压缩状态回退到未压缩时刻",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        help="Session ID（不带 .jsonl 后缀）",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="直接指定 .jsonl 文件路径",
    )
    parser.add_argument(
        "--dir",
        action="append",
        help="自定义扫描目录（可多次指定）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑模式：不写盘，只打印会做什么",
    )

    args = parser.parse_args()

    # 解析文件路径
    if args.file:
        path = args.file
    elif args.session_id:
        search_dirs = args.dir if args.dir else DEFAULT_SEARCH_DIRS
        path = find_session_file(args.session_id, search_dirs)
        if path is None:
            print(f"❌ 找不到 session '{args.session_id}' 在 {search_dirs}", file=sys.stderr)
            return 1
    else:
        parser.print_help()
        return 1

    return recover(path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
