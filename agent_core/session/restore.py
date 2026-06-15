"""
session/restore.py - 会话恢复模块
参考 Claude Code sessionRestore.ts 实现的 Resume / Continue / Fork 语义

三种恢复语义：
- Resume   : 从最后一个 compact-boundary 处恢复，只加载摘要 + 最新消息
- Continue : 从最新消息继续，读取完整消息链（包括压缩前的旧消息）
- Fork     : 复制父会话到新 session_id，保留 parentUuid 链，独立演进

实现要点：
- 复用 SessionStorage（JSONL 单文件）
- 复用 SessionMetadata（从尾部恢复）
- 复用 ProgressTracker（从 entry 恢复）
- parentUuid 链：Fork 时映射旧 UUID → 新 UUID，保留链的可追溯性
- worktree_state entry 不复制（危险状态）
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .storage import SessionStorage
from .metadata import SessionMetadata
from .progress import ProgressTracker

logger = logging.getLogger("session.restore")


# ── 工具函数 ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat()


def _is_compact_boundary(entry: dict) -> bool:
    """判断 entry 是否为压缩边界（兼容新旧两种格式）

    新格式（3089a29 引入，对齐 Claude Code sessionStorage.ts）：
        type="system" + subtype="compact_boundary"
    旧格式（已废弃）：
        type="compact-boundary" 或 compact=True
    """
    etype = entry.get("type")
    if etype == "compact-boundary":
        return True
    if etype == "system" and entry.get("subtype") == "compact_boundary":
        return True
    if entry.get("compact") is True:
        return True
    return False


def _is_compact_summary(entry: dict) -> bool:
    """判断 entry 是否为压缩摘要（兼容新旧两种格式）

    新格式（3089a29 引入，对齐 Claude Code getCompactUserSummaryMessage）：
        type="user" + message.isCompactSummary=True
    旧格式（已废弃）：
        type="summary"
    """
    if entry.get("type") == "summary":
        return True
    if entry.get("type") == "user":
        msg = entry.get("message") or {}
        if msg.get("isCompactSummary") is True:
            return True
    return False


def _is_message_entry(entry: dict) -> bool:
    """判断 entry 是否为实际消息（而非元数据/summary/边界）"""
    # 新格式：role 在 message 字段内
    msg = entry.get("message")
    if msg:
        role = msg.get("role") or msg.get("type")
    else:
        # 兼容旧格式：role 在 entry 顶层
        role = entry.get("role") or entry.get("type")
    return role in ("user", "assistant", "system", "tool_use", "tool_result")


def _is_worktree_entry(entry: dict) -> bool:
    """判断 entry 是否为 worktree 状态（Fork 时不复制）"""
    return entry.get("type") == "worktree-state"


def _build_uuid_map(old_entries: list[dict]) -> dict[str, str]:
    """
    从旧 entries 构建 UUID 映射表

    用于 Fork：为每条旧 entry 生成新 UUID，并建立
    old_uuid → new_uuid 的映射。
    """
    return {e["uuid"]: str(uuid.uuid4()) for e in old_entries}


def _map_entry(entry: dict, uuid_map: dict[str, str], new_session_id: str) -> dict:
    """
    将旧 entry 映射为新 entry（Fork 时调用）

    - uuid: 分配新 UUID
    - parentUuid: 通过映射表指向新的父 UUID
    - sessionId: 更新为新 session_id
    - 时间戳更新
    """
    new_uuid = uuid_map.get(entry["uuid"], str(uuid.uuid4()))
    old_parent = entry.get("parentUuid")
    new_parent = uuid_map.get(old_parent) if old_parent else None

    new_entry = entry.copy()
    new_entry["uuid"] = new_uuid
    new_entry["parentUuid"] = new_parent  # None 或新 UUID
    new_entry["sessionId"] = new_session_id
    new_entry["timestamp"] = _now_iso()
    return new_entry


def _rebuild_chain(entries: list[dict]) -> list[dict]:
    """
    从 JSONL entries 重建有序消息链（按 parentUuid 拓扑排序）

    策略：
    1. 保留 boundary 在 entries 中（用于追踪），但在输出时过滤
    2. 构建 uuid → entry 映射 + child_map
    3. 找到所有根节点（boundary 作为独立链的根）
    4. 按时间顺序追踪每条链
    5. 多条链按根节点时间戳排序合并
    6. 输出时过滤掉 boundary
    """
    if not entries:
        return []

    # 构建 uuid → entry 映射（包含 boundary，用于追踪）
    uuid_to_entry = {e["uuid"]: e for e in entries}
    child_map: dict[str, str] = {}  # parentUuid → child uuid

    # 构建 child_map（boundary 不追踪自己，但其 parentUuid=None 使其成为根）
    for e in entries:
        if _is_compact_boundary(e):
            continue
        parent = e.get("parentUuid")
        if parent:
            child_map[parent] = e["uuid"]

    # 找到所有根节点（parentUuid = None）
    roots = [e for e in entries if e.get("parentUuid") is None]
    if not roots:
        return [e for e in entries if not _is_compact_boundary(e)]

    # 按时间戳排序各条链的根节点
    roots.sort(key=lambda e: e.get("timestamp", ""))

    # 追踪每条链
    chains: list[list[dict]] = []
    for root in roots:
        chain: list[dict] = []
        current = root["uuid"]
        while current:
            if current in uuid_to_entry:
                chain.append(uuid_to_entry[current])
                current = child_map.get(current)
            else:
                break
        if chain:
            chains.append(chain)

    # 按第一条消息的时间戳合并各链
    chains.sort(key=lambda c: c[0].get("timestamp", ""))

    result: list[dict] = []
    for chain in chains:
        result.extend(chain)

    # 输出时过滤掉 boundary（boundary 在 uuid_to_entry 中用于追踪，但不应出现在消息链中）
    return [e for e in result if not _is_compact_boundary(e)]


def _read_messages_up_to_boundary(
    storage: SessionStorage,
) -> tuple[list[dict], Optional[dict]]:
    """
    从断链处读取消息（用于 Resume）

    策略：
    1. 读取全部 entries
    2. 从后往前扫描，遇到 compact-boundary 停止
    3. 返回断链后的消息链 + 摘要 entry

    Returns:
        (messages_after_boundary, summary_entry_or_None)
    """
    all_entries = storage.read_entries(include_compact_boundary=True)

    if not all_entries:
        return [], None

    # 从后往前找 compact-boundary
    boundary_idx = None
    for i in range(len(all_entries) - 1, -1, -1):
        if _is_compact_boundary(all_entries[i]):
            boundary_idx = i
            break

    if boundary_idx is None:
        # 无压缩边界，返回全部消息
        messages = [e for e in all_entries if _is_message_entry(e)]
        return messages, None

    # 摘要 entry 在边界之前（或边界本身如果是 summary）
    boundary_entry = all_entries[boundary_idx]
    summary_entry = None

    # 找边界前的 summary entry（新格式：type=user + isCompactSummary=True）
    for i in range(boundary_idx - 1, -1, -1):
        if _is_compact_summary(all_entries[i]):
            summary_entry = all_entries[i]
            break

    # 边界后的消息（断链后新追加的）
    after_boundary = all_entries[boundary_idx + 1:]

    # 关键：将 boundary 也加入链条（boundary.parentUuid=None 使其成为新链的根）
    # 这样 boundary 后的消息的 parentUuid 能指向 boundary，链条才能被 _rebuild_chain 重建
    entries_for_chain = [boundary_entry] + after_boundary
    messages = _rebuild_chain(entries_for_chain)

    return messages, summary_entry


def _read_tail_metadata(storage: SessionStorage) -> SessionMetadata:
    """从尾部恢复元数据"""
    tail = storage.read_tail(kb=16)  # 只读最后 16KB
    return SessionMetadata.from_tail(tail, storage.session_id)


# ── Resume ─────────────────────────────────────────────────────────────────

def resume_session(
    session_id: str,
    data_dir: Optional[str] = None,
) -> tuple[list[dict], SessionMetadata]:
    """
    Resume 会话：从最后一个 compact-boundary 处恢复

    Resume 语义：
    - 只加载压缩边界之后的消息（摘要 + 最新消息）
    - 不加载压缩前的旧消息（已在摘要中）
    - 用于 Agent 重新启动后恢复工作

    Args:
        session_id: 会话 ID
        data_dir: 数据目录（可选）

    Returns:
        (messages, metadata): 从断链处开始的最新消息链 + 元数据
    """
    storage = SessionStorage(session_id=session_id, data_dir=data_dir, auto_flush=False)

    # 检查会话是否存在
    path = storage._ensure_path()
    if not path.exists():
        logger.warning(f"Session not found for resume: {session_id}")
        return [], SessionMetadata(session_id=session_id)

    logger.info(f"Resuming session {session_id[:8]} from compact-boundary")

    # 从断链处读取消息
    messages, summary_entry = _read_messages_up_to_boundary(storage)

    # 恢复元数据
    metadata = _read_tail_metadata(storage)

    # 如果有摘要，把摘要加到消息链开头
    if summary_entry:
        messages = [summary_entry] + messages

    logger.info(
        f"Resume: got {len(messages)} messages "
        f"(has_summary={summary_entry is not None})"
    )
    return messages, metadata


# ── Continue ────────────────────────────────────────────────────────────────

def continue_session(
    session_id: str,
    data_dir: Optional[str] = None,
) -> tuple[list[dict], SessionMetadata]:
    """
    Continue 会话：从最新消息继续（读取全部消息）

    Continue 语义：
    - 读取完整消息链（包括压缩前的旧消息）
    - 从最后一条消息继续追加
    - 用于用户明确要求"继续上次工作"

    Returns:
        (all_messages, metadata)
    """
    storage = SessionStorage(session_id=session_id, data_dir=data_dir, auto_flush=False)

    path = storage._ensure_path()
    if not path.exists():
        logger.warning(f"Session not found for continue: {session_id}")
        return [], SessionMetadata(session_id=session_id)

    logger.info(f"Continue session {session_id[:8]}: reading all entries")

    # read_entries(include_compact_boundary=True) 读取所有行，包含 boundary entry
    all_entries = storage.read_entries(include_compact_boundary=True)

    # 重建链：传入完整 entries（包含 boundary），_rebuild_chain 会用 boundary 追踪链，
    # 但在输出时过滤掉 boundary
    messages = _rebuild_chain(all_entries)

    # 恢复元数据
    metadata = _read_tail_metadata(storage)

    logger.info(f"Continue: got {len(messages)} total messages")
    return messages, metadata


# ── Fork ───────────────────────────────────────────────────────────────────

def fork_session(
    parent_session_id: str,
    data_dir: Optional[str] = None,
    new_name: Optional[str] = None,
) -> tuple[str, SessionStorage]:
    """
    Fork 会话：复制父会话到新 session_id

    Fork 语义（Git 风格）：
    - 新 session_id 生成（新会话独立存储）
    - 复制父会话所有消息 entry 到新 JSONL
    - 生成新 UUID 替换旧 UUID
    - 保留 parentUuid 链（用于历史追溯）
    - worktree_state entry 不复制（危险状态）
    - compact_boundary 复制（保留压缩历史）
    - 新会话创建 fresh metadata（标题继承或自定义）

    Args:
        parent_session_id: 父会话 ID
        data_dir: 数据目录
        new_name: 新会话名称（可选）

    Returns:
        (new_session_id, new_storage)
    """
    parent_storage = SessionStorage(
        session_id=parent_session_id,
        data_dir=data_dir,
        auto_flush=False,
    )

    parent_path = parent_storage._ensure_path()
    if not parent_path.exists():
        raise FileNotFoundError(f"Parent session not found: {parent_session_id}")

    # 生成新 session_id
    new_session_id = str(uuid.uuid4())

    # 创建新 storage
    new_storage = SessionStorage(
        session_id=new_session_id,
        data_dir=data_dir,
        auto_flush=True,
    )

    # 读取父会话全部 entry（包含元数据）
    parent_entries = parent_storage.read_entries(include_compact_boundary=True)

    if not parent_entries:
        logger.warning(f"Parent session empty: {parent_session_id}")

    # 构建 UUID 映射
    uuid_map = _build_uuid_map(parent_entries)

    # 映射每条 entry
    forked_count = 0
    skipped_worktree = 0

    for entry in parent_entries:
        # 跳过 worktree_state（危险状态，不继承）
        if _is_worktree_entry(entry):
            skipped_worktree += 1
            continue

        new_entry = _map_entry(entry, uuid_map, new_session_id)
        new_storage.append_entry(
            entry_type=new_entry["type"],
            content={k: v for k, v in new_entry.items()
                     if k not in ("uuid", "parentUuid", "sessionId", "type", "timestamp")},
            parent_uuid=new_entry["parentUuid"],
        )
        forked_count += 1

    # 追加元数据 entry（重新生成，确保新会话独立）
    fork_timestamp = _now_iso()
    new_storage.append_entry(
        entry_type="fork-info",
        content={
            "parent_session_id": parent_session_id,
            "forked_at": fork_timestamp,
            "name": new_name,
        },
    )
    new_storage.flush()

    logger.info(
        f"Forked: {parent_session_id[:8]} -> {new_session_id[:8]}, "
        f"forked={forked_count}, skipped_worktree={skipped_worktree}"
    )
    return new_session_id, new_storage


# ── 会话列表 ────────────────────────────────────────────────────────────────

def list_sessions(data_dir: Optional[str] = None) -> list[dict]:
    """列出所有会话（与 SessionStorage.list_sessions 相同）"""
    return SessionStorage.list_sessions(data_dir=data_dir)


def delete_session(session_id: str, data_dir: Optional[str] = None):
    """删除指定会话"""
    SessionStorage.delete_session(session_id, data_dir=data_dir)
    logger.info(f"Deleted session: {session_id}")


# ── 会话压缩 ────────────────────────────────────────────────────────────────

def compact_session(
    session_id: str,
    data_dir: Optional[str] = None,
) -> SessionMetadata:
    """
    手动压缩会话（在外部 LLM 生成摘要后调用）

    追加：
    1. summary entry（LLM 生成的摘要内容，由调用方提供）
    2. compact-boundary entry（parentUuid = None，断链标记）

    注意：实际摘要内容由 ContextManager 调用 LLM 生成，
    此函数只负责写入 JSONL entry。

    Args:
        session_id: 会话 ID
        data_dir: 数据目录

    Returns:
        更新后的 SessionMetadata
    """
    storage = SessionStorage(session_id=session_id, data_dir=data_dir, auto_flush=True)

    # 追加 compact-boundary（parentUuid = None，断链）
    boundary_uuid = storage.add_compact_boundary()

    # 刷新
    storage.flush()

    # 恢复元数据
    metadata = _read_tail_metadata(storage)
    logger.info(
        f"Session {session_id[:8]} compacted, boundary_uuid={boundary_uuid[:8]}"
    )
    return metadata
