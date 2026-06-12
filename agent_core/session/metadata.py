"""
SessionMetadata - 会话元数据管理
参考 Claude Code sessionStorage.ts 中的元数据管理逻辑

元数据类型：
- custom-title: 用户自定义标题
- ai-title: AI 生成的标题
- tag: 标签
- agent-name: Agent 类型名称
- agent-setting: Agent 配置
- mode: 当前模式（plan/read/write）
- worktree-state: Git worktree 状态
- last-prompt: 最后一条用户消息
- pr-link: PR 链接信息

元数据存储策略：
- 写入 JSONL 尾部（re-append 模式）
- 每次更新时重新追加到文件末尾（保证尾部是最新的）
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class SessionMetadata:
    """
    会话元数据容器

    Attributes:
        title: 用户自定义标题
        ai_title: AI 生成标题
        tags: 标签列表
        agent_name: Agent 类型
        agent_setting: Agent 配置（JSON 字符串）
        mode: 当前模式（plan / read / write）
        worktree_state: Git worktree 状态
        last_prompt: 最后一条用户消息
        project_slug: 项目标识
        session_id: 所属会话 ID
    """

    def __init__(
        self,
        session_id: str,
        title: Optional[str] = None,
        ai_title: Optional[str] = None,
        tags: Optional[list[str]] = None,
        agent_name: str = "default",
        agent_setting: Optional[str] = None,
        mode: str = "write",
        worktree_state: Optional[dict] = None,
        last_prompt: Optional[str] = None,
        project_slug: Optional[str] = None,
    ):
        self.session_id = session_id
        self.title = title
        self.ai_title = ai_title
        self.tags = tags or []
        self.agent_name = agent_name
        self.agent_setting = agent_setting
        self.mode = mode
        self.worktree_state = worktree_state or {}
        self.last_prompt = last_prompt
        self.project_slug = project_slug or "default"
        self._updated_at = time.time()

    # ── 更新方法 ─────────────────────────────────────────────────

    def update_title(self, title: str):
        """更新用户自定义标题"""
        self.title = title
        self._updated_at = time.time()

    def update_ai_title(self, ai_title: str):
        """更新 AI 生成标题"""
        self.ai_title = ai_title
        self._updated_at = time.time()

    def add_tag(self, tag: str):
        """添加标签（去重）"""
        if tag not in self.tags:
            self.tags.append(tag)
            self._updated_at = time.time()

    def remove_tag(self, tag: str):
        """移除标签"""
        if tag in self.tags:
            self.tags.remove(tag)
            self._updated_at = time.time()

    def update_mode(self, mode: str):
        """更新模式（plan / read / write）"""
        if mode in ("plan", "read", "write"):
            self.mode = mode
            self._updated_at = time.time()

    def update_last_prompt(self, prompt: str):
        """更新最后一条用户消息"""
        self.last_prompt = prompt[:1000]  # 截断到 1000 字符
        self._updated_at = time.time()

    def update_worktree_state(self, state: dict):
        """更新 Git worktree 状态"""
        self.worktree_state = state
        self._updated_at = time.time()

    def update_agent_setting(self, setting: dict):
        """更新 Agent 配置"""
        self.agent_setting = json.dumps(setting, ensure_ascii=False)
        self._updated_at = time.time()

    # ── Entry 序列化 ──────────────────────────────────────────────

    def to_entries(self) -> list[dict]:
        """
        将元数据转为 JSONL Entry 列表

        Returns:
            可写入 JSONL 的 entry 列表（无 type 字段，由调用方指定）
        """
        entries = []
        timestamp = datetime.now().isoformat()

        if self.title is not None:
            entries.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "custom-title",
                "customTitle": self.title,
                "timestamp": timestamp,
            })

        if self.ai_title is not None:
            entries.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "ai-title",
                "aiTitle": self.ai_title,
                "timestamp": timestamp,
            })

        for tag in self.tags:
            entries.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "tag",
                "tag": tag,
                "timestamp": timestamp,
            })

        entries.append({
            "uuid": str(uuid.uuid4()),
            "parentUuid": None,
            "sessionId": self.session_id,
            "type": "agent-name",
            "agentName": self.agent_name,
            "timestamp": timestamp,
        })

        if self.agent_setting:
            entries.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "agent-setting",
                "agentSetting": self.agent_setting,
                "timestamp": timestamp,
            })

        entries.append({
            "uuid": str(uuid.uuid4()),
            "parentUuid": None,
            "sessionId": self.session_id,
            "type": "mode",
            "mode": self.mode,
            "timestamp": timestamp,
        })

        if self.worktree_state:
            entries.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "worktree-state",
                "worktreeSession": self.worktree_state,
                "timestamp": timestamp,
            })

        if self.last_prompt:
            entries.append({
                "uuid": str(uuid.uuid4()),
                "parentUuid": None,
                "sessionId": self.session_id,
                "type": "last-prompt",
                "lastPrompt": self.last_prompt,
                "timestamp": timestamp,
            })

        return entries

    def to_dict(self) -> dict:
        """转为 dict（不含 session_id）"""
        return {
            "title": self.title,
            "ai_title": self.ai_title,
            "tags": self.tags,
            "agent_name": self.agent_name,
            "agent_setting": self.agent_setting,
            "mode": self.mode,
            "worktree_state": self.worktree_state,
            "last_prompt": self.last_prompt,
            "project_slug": self.project_slug,
            "updated_at": self._updated_at,
        }

    # ── 反序列化 ─────────────────────────────────────────────────

    @classmethod
    def from_tail(cls, tail_entries: list[dict], session_id: str) -> "SessionMetadata":
        """
        从尾部 Entry 列表恢复元数据

        策略：从后往前扫描，遇到某类型的第一条就停止
        （因为 re-append 模式下，同类型最新值在最后）
        """
        meta = cls(session_id=session_id)
        seen_types = set()

        # 倒序扫描
        for entry in reversed(tail_entries):
            etype = entry.get("type")

            # 每种类型只取第一个（倒序时最新的）
            if etype in seen_types:
                continue

            if etype == "custom-title":
                meta.title = entry.get("customTitle")
                seen_types.add("custom-title")

            elif etype == "ai-title":
                meta.ai_title = entry.get("aiTitle")
                seen_types.add("ai-title")

            elif etype == "tag":
                # 标签需要收集所有（不同于其他元数据的"取最新"语义）
                tag = entry.get("tag")
                if tag and tag not in meta.tags:
                    meta.tags.insert(0, tag)  # insert(0) 保证最新标签在前

            elif etype == "agent-name":
                meta.agent_name = entry.get("agentName", "default")
                seen_types.add("agent-name")

            elif etype == "agent-setting":
                meta.agent_setting = entry.get("agentSetting")
                seen_types.add("agent-setting")

            elif etype == "mode":
                meta.mode = entry.get("mode", "write")
                seen_types.add("mode")

            elif etype == "worktree-state":
                meta.worktree_state = entry.get("worktreeSession", {})
                seen_types.add("worktree-state")

            elif etype == "last-prompt":
                meta.last_prompt = entry.get("lastPrompt")
                seen_types.add("last-prompt")

        return meta

    @classmethod
    def from_dict(cls, data: dict, session_id: str) -> "SessionMetadata":
        """从 dict 构造"""
        return cls(
            session_id=session_id,
            title=data.get("title"),
            ai_title=data.get("ai_title"),
            tags=data.get("tags"),
            agent_name=data.get("agent_name", "default"),
            agent_setting=data.get("agent_setting"),
            mode=data.get("mode", "write"),
            worktree_state=data.get("worktree_state") or {},
            last_prompt=data.get("last_prompt"),
            project_slug=data.get("project_slug"),
        )

    # ── 便利方法 ─────────────────────────────────────────────────

    @property
    def display_title(self) -> str:
        """显示用的标题（优先用户自定义，其次 AI 生成）"""
        return self.title or self.ai_title or "新会话"

    def __repr__(self):
        return (
            f"SessionMetadata(session_id={self.session_id[:8]}, "
            f"title={self.display_title!r}, "
            f"mode={self.mode})"
        )
