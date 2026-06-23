"""
候选审查 actions(M10 C4.2)

提供 list / accept / reject / edit / skip 5 个动作,把 _candidate/ 下的候选
转到正式 memory/<type>/<hash>.md(或丢弃)。

设计要点:
1. 候选文件路径 = {candidate_root}/{run_id}/{type}/{ts}_{slug}.md(C3.3)
2. accept_candidate 解析 frontmatter 拿 type/body, 调 MemoryStore.write 重算 hash 落盘
3. reject/skip: reject 删文件; skip 保留(留待下次审)
4. edit 修改 body 后写回(frontmatter 不变), 不重算 hash

真实校正(与 brief 一致性修正):
- MemoryStore.write 要求非空 source_quote(L7 不变量), 候选自身没 quote。
  accept_candidate 从候选 frontmatter 的 `sources` 字段(或 title)派生一个非空
  source_quote, 满足 L7。
- MemoryStore.write 落盘时会自己加 `# {title}` 行。候选 body 以 `# {title}` 开头,
  直接透传会导致 title 重复。accept_candidate 在写正式记忆前剥离 body 开头的 H1。
- feedback/project 类要求 body 含 `**Why:**`(v2.1 §4.5 #7)。候选 body 由 distiller
  生成时已含, 透传即可。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Union

from .memory_store import MemoryStore

logger = logging.getLogger("memory.candidate_actions")

__all__ = [
    "list_candidates",
    "accept_candidate",
    "reject_candidate",
    "edit_candidate",
    "skip_candidate",
]


# ─────────────────────────────────────
# helpers
# ─────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_candidate(path: Path) -> dict:
    """解析候选 .md 文件: 返回 {type, title, body, sources, frontmatter_raw, body_raw}

    body = frontmatter 之后的全部正文(含 # title / **Why:** / ## 内容)
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # 没 frontmatter → 当 raw text, type 默认 user
        return {
            "type": "user",
            "title": path.stem,
            "body": text,
            "sources": [],
            "frontmatter_raw": "",
            "body_raw": text,
        }
    fm_raw, body_raw = m.group(1), m.group(2)
    # 简单解析 type / title / sources(避免引 yaml 依赖, 候选文件格式固定)
    type_match = re.search(r"^type:\s*(\S+)", fm_raw, re.MULTILINE)
    title_match = re.search(r"^title:\s*(.+)$", fm_raw, re.MULTILINE)
    sources_match = re.search(r"^sources:\s*(.+)$", fm_raw, re.MULTILINE)
    title = title_match.group(1).strip().strip("\"'") if title_match else path.stem
    sources: list[str] = []
    if sources_match:
        # sources: [a, b] 或 sources: a — 取括号内或整行
        sval = sources_match.group(1).strip()
        if sval.startswith("["):
            inner = sval.strip("[]")
            sources = [
                p.strip().strip("\"' ")
                for p in inner.split(",")
                if p.strip().strip("\"' ")
            ] if inner else []
        elif sval and sval != "[]":
            sources = [sval.strip("\"' ")]
    return {
        "type": type_match.group(1) if type_match else "user",
        "title": title,
        "body": body_raw.strip(),
        "sources": sources,
        "frontmatter_raw": fm_raw,
        "body_raw": body_raw,
    }


def _strip_leading_h1(body: str, title: str) -> str:
    """剥离 body 开头的 `# {title}` 行(MemoryStore.write 会自己加, 避免重复)

    只剥第一行且需匹配 title; 不匹配则原样返回(防御性, 不丢内容)。
    """
    if not body:
        return body
    lines = body.split("\n")
    if lines and lines[0].strip() == f"# {title}".strip():
        # 剥第一行 + 紧跟的空行
        rest = lines[1:]
        while rest and rest[0].strip() == "":
            rest = rest[1:]
        return "\n".join(rest)
    return body


def _serialize_candidate(parsed: dict) -> str:
    """把 {frontmatter_raw, body} 拼回 .md 文本(edit_candidate 用)"""
    if not parsed.get("frontmatter_raw"):
        return parsed.get("body", "")
    return f"---\n{parsed['frontmatter_raw']}\n---\n{parsed['body']}"


# ─────────────────────────────────────
# public API
# ─────────────────────────────────────

def list_candidates(memory_root: Union[str, Path]) -> list[Path]:
    """列所有候选文件(.md 在 _candidate/ 下递归, 含 run_id/type 嵌套)

    Returns:
        排序的 Path 列表(按 mtime 降序, 最新在前); _candidate/ 不存在 → []
    """
    cand_root = Path(memory_root) / "_candidate"
    if not cand_root.exists():
        return []
    paths = list(cand_root.rglob("*.md"))
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def accept_candidate(
    memory_root: Union[str, Path],
    cand_path: Path,
    target_type: Optional[str] = None,
) -> str:
    """接受候选: 解析 → MemoryStore.write() → 删除候选文件

    Args:
        memory_root: memory 根(_candidate/ 的父目录)
        cand_path: 候选 .md 文件路径
        target_type: 覆盖目标类型(默认用候选自己的 type)

    Returns:
        item_hash(64 字符 hex)— 落盘后的正式记忆 hash

    真实校正:
    - 从候选 sources/title 派生非空 source_quote(L7 不变量)
    - 剥离 body 开头 H1(MemoryStore.write 自己会加 title)
    """
    parsed = _parse_candidate(cand_path)
    type_ = target_type or parsed["type"]
    title = parsed["title"]

    # source_quote: L7 必填。优先用候选 sources; 都没有就用 title 兜底
    src = parsed.get("sources") or []
    source_quote = src[0] if src else f"(候选: {title})"

    # body: 剥掉开头 H1(MemoryStore.write 会自己加 `# {title}`)
    body = _strip_leading_h1(parsed["body"], title)

    store = MemoryStore(Path(memory_root))
    item_hash = store.write(
        type=type_,
        title=title,
        body=body,
        source_quote=source_quote,
    )
    # 删候选
    cand_path.unlink(missing_ok=True)
    logger.info(f"accepted candidate {cand_path.name} -> {type_}/{item_hash[:12]}.md")
    return item_hash


def reject_candidate(
    memory_root: Union[str, Path],
    cand_path: Path,
    reason: str = "",
) -> None:
    """拒绝候选: 删除文件

    meta_db.candidates.status 的写入留给 C4.4(本任务不双写)。
    """
    cand_path.unlink(missing_ok=True)
    logger.info(f"rejected candidate {cand_path.name}: {reason or '(no reason)'}")


def edit_candidate(
    memory_root: Union[str, Path],
    cand_path: Path,
    new_body: str,
) -> None:
    """修改 body(frontmatter 不变), 不重算 hash

    new_body 应为完整正文(含 # title / **Why:** / ## 内容)。
    """
    parsed = _parse_candidate(cand_path)
    parsed["body"] = new_body
    parsed["body_raw"] = new_body  # 保留字段一致
    cand_path.write_text(_serialize_candidate(parsed), encoding="utf-8")
    logger.info(f"edited candidate {cand_path.name}: body {len(new_body)} chars")


def skip_candidate(
    memory_root: Union[str, Path],
    cand_path: Path,
) -> None:
    """跳过候选(不删, 留待下次审)— 仅记日志, 不写盘"""
    logger.debug(f"skipped candidate {cand_path.name}")
