"""
Distiller + DistillationScheduler —— 蒸馏 (autoDream, L5)

M5 / Day 5 —— v2.1 §7.1+§7.2+§7.3+§7.4

设计要点：
1. 锁文件二职责(§7.1 line 2434-2441)
   - mtime = "上次成功蒸馏时间"(门1用)
   - 存在性 + envelope = "当前锁状态"(门4用)
   - 成功路径保留文件;失败路径回滚 mtime
2. A1 (O_EXCL 原子) + A2 (mtime 回滚) + A11 (JSON envelope) 一次写完
3. 四重门(cheap → expensive): gate / time / throttle / sessions
4. dry_run 默认 True(§7.4):候选写到 _candidate/{type}/,不污染正式目录
5. LLM 注入式:llm_callback(prompt) -> str;不绑死 GLM/Anthropic

不在本模块范围：
- ❌ 真实 LLM 调用 (M7 集成阶段)
- ❌ 5/8 并发场景的端到端测试 (M6)
- ❌ UI diff/merge review (M7)
- ❌ _candidate/ → 正式目录原子替换 (M7)
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from agent_core.memory.config import DistillationConfig
from agent_core.memory.tracing import tracer

logger = logging.getLogger("memory.distiller")


# ──────────────────────────────────────────────────────────────────
# 异常 / 数据类
# ──────────────────────────────────────────────────────────────────

class DistillationError(Exception):
    """蒸馏失败基类"""


@dataclass
class DistillationResult:
    """蒸馏一次运行的结果

    Attributes:
        success: 是否成功(走完流程)
        skipped: 是否被门拦住(gate_disabled / too_soon / locked 等)
        skip_reason: skipped=True 时的具体原因
        candidates: 候选 dict 列表(每项含 type/title/body/source_quote/why)
        candidates_written: 写出去的 .md 文件路径(dry_run=True 时为空)
        sessions_processed: 本次处理的 session 数
        prior_mtime_ms: 锁文件 prior mtime(用于诊断)
        error: 异常 message(若有)
    """
    success: bool
    skipped: bool = False
    skip_reason: str = ""
    candidates: list[dict] = field(default_factory=list)
    candidates_written: list[Path] = field(default_factory=list)
    sessions_processed: int = 0
    prior_mtime_ms: int = 0
    error: str = ""


# ──────────────────────────────────────────────────────────────────
# Distiller —— 核心蒸馏逻辑(无调度 / 无锁)
# ──────────────────────────────────────────────────────────────────

class Distiller:
    """
    蒸馏核心(纯函数式,不持状态)

    职责:
    1. 读 session log + 现有记忆
    2. 拼蒸馏 prompt(§7.3)
    3. 调 llm_callback 拿候选列表
    4. dry_run 时返回候选,不写盘;否则写到 candidate_root/{type}/

    依赖:
    - llm_callback: 注入式 LLM 接口(prompt -> response str)
    - candidate_root: dry_run=False 时候选落盘目录(默认 _candidate/)
    """

    DEFAULT_CANDIDATE_ROOT = "_candidate"

    def __init__(
        self,
        llm_callback: Callable[[str], str],
        candidate_root: Optional[Union[str, Path]] = None,
        sm_dir: Optional[Union[str, Path]] = None,  # M10 C2.3
    ):
        self.llm = llm_callback
        self.candidate_root = Path(candidate_root) if candidate_root else Path(self.DEFAULT_CANDIDATE_ROOT)
        self.sm_dir = Path(sm_dir) if sm_dir else None  # None = 不读 SM

    def distill(
        self,
        session_log_files: list[Path],
        existing_memories: Optional[list[dict]] = None,
    ) -> list[dict]:
        """
        蒸馏:读 sessions + existing memories → LLM 整合 → 返回候选 dict 列表

        不写盘(纯函数),由调用方决定是否 write_candidates

        Returns:
            [{type, title, body, source_quote, why, tags}, ...]
        """
        sessions_text = self._read_sessions(session_log_files)
        existing_text = self._format_existing(existing_memories or [])

        # M10 C2.3: SM 跨会话作为 L4 输入
        sm_data = self._read_sm_files(self.sm_dir) if self.sm_dir else []
        sm_text = self._format_sm_files(sm_data)

        prompt = self._build_prompt(sessions_text, existing_text, sm_text)
        response = self.llm(prompt)
        candidates = self._parse_response(response)
        return candidates

    def write_candidates(
        self,
        candidates: list[dict],
        candidate_root: Optional[Union[str, Path]] = None,
        run_id: Optional[str] = None,  # M10 C3.3: 子目录隔离,便于回灌追踪
    ) -> list[Path]:
        """
        写候选到 {candidate_root}/{run_id}/{type}/{timestamp}_{slug}.md (M10 C3.3)

        或 {candidate_root}/{type}/{timestamp}_{slug}.md (run_id=None,旧行为)

        run_id sanitize:仅保留 [\w\-],其余替换为 _ 防止路径穿越
        """
        root = Path(candidate_root) if candidate_root else self.candidate_root
        if run_id:
            # M10 C3.3: 防止 path traversal(../../etc → _.._.._etc)
            safe_run_id = re.sub(r"[^\w\-]", "_", run_id)
            if not safe_run_id:
                raise ValueError(f"run_id '{run_id}' sanitize 后为空,拒绝写入")
            root = root / safe_run_id
        written: list[Path] = []
        ts = time.strftime("%Y-%m-%dT%H-%M-%S")
        for cand in candidates:
            type_ = cand.get("type", "user")
            title = cand.get("title", "untitled")
            body = cand.get("body", "")
            why = cand.get("why", "")
            sources = cand.get("sources", [])
            confidence = cand.get("confidence", 0.5)
            tags = cand.get("tags", [])

            slug = re.sub(r"[^\w一-鿿-]+", "_", title, flags=re.UNICODE)[:60] or "untitled"
            target_dir = root / type_
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f"{ts}_{slug}.md"

            frontmatter = self._render_frontmatter(
                type=type_,
                title=title,
                confidence=confidence,
                sources=sources,
                tags=tags,
            )
            full_body = self._render_body(title=title, why=why, body=body)
            target_path.write_text(frontmatter + "\n" + full_body, encoding="utf-8")
            written.append(target_path)
        return written

    # ── prompt 模板(§7.3) ────────────────────────────────

    def _build_prompt(
        self,
        sessions_text: str,
        existing_text: str,
        sm_text: str = "(无跨会话 SM 摘要)",
    ) -> str:
        """v2.1 §7.3 蒸馏 prompt(per-file 输出)"""
        return f"""从以下对话日志和现有记忆中, 蒸馏出值得保留的长期记忆.

跨会话 SM 摘要(L4 输入,直接复用,不要再提取):
{sm_text}

现有记忆(不要重复提取):
{existing_text}

增量日志:
{sessions_text}

输出: 每个候选记忆一个 markdown 文件, 含 YAML frontmatter.

frontmatter 字段:
- type: 必须是 user | feedback | project | reference 之一
- created_at: YYYY-MM-DD
- confidence: 0.0-1.0
- sources: [session_id_1, session_id_2, ...]

body 格式:
# <标题>

**Why:** <这条记忆为什么重要, 用户/项目背景>

## 内容
<具体记忆内容>

蒸馏规则:
1. 合并相似记忆 (例: "先手写 ReAct" 和 "重视底层原理" → 合并)
2. 调和冲突信息 (例: 用户改主意了 → 更新原记忆, 不新增)
3. 删除过时记忆 (例: "Python 2 vs 3" 的 Python 2 相关 → 删)
4. 强制: feedback / project 类必须含 **Why:** 字段

输出格式(JSON 数组,严格遵循):
```json
[
  {{
    "type": "user",
    "title": "<标题>",
    "why": "<重要性理由>",
    "body": "<记忆正文>",
    "confidence": 0.8,
    "sources": ["session_id_1"],
    "tags": ["偏好"]
  }}
]
```
"""

    # ── helpers ─────────────────────────────────────────

    @staticmethod
    def _read_sessions(session_log_files: list[Path]) -> str:
        """读 session 日志文件,合并成文本"""
        parts: list[str] = []
        for p in session_log_files:
            try:
                content = Path(p).read_text(encoding="utf-8")
                parts.append(f"=== {p.name} ===\n{content}")
            except OSError as e:
                logger.warning(f"读 session 文件失败 {p}: {e}")
        return "\n\n".join(parts)

    @staticmethod
    def _read_sm_files(sm_dir: Optional[Path]) -> list[dict]:
        """M10 C2.3: 读所有 SM .json 文件,反序列化为 list

        Returns:
            [{"session_id": "...", "summary_message_content": "...", ...}, ...]
            文件不存在或为空 → []
        """
        if not sm_dir or not sm_dir.exists():
            return []
        out: list[dict] = []
        for p in sm_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["_path"] = str(p)  # 加 path 便于 debug
                out.append(data)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"读 SM 文件失败 {p}: {e}")
        return out

    @staticmethod
    def _format_sm_files(sm_data_list: list[dict]) -> str:
        """格式化 SM 数据为 prompt 片段"""
        if not sm_data_list:
            return "(无跨会话 SM 摘要)"
        lines = []
        for d in sm_data_list:
            sid = d.get("session_id", "?")
            content = d.get("summary_message_content", "")[:300]
            tokens = d.get("used_tokens_estimate", "?")
            lines.append(f"### Session {sid} (~{tokens} tokens)\n{content}")
        return "\n\n".join(lines)

    @staticmethod
    def _format_existing(memories: list[dict]) -> str:
        """格式化现有记忆"""
        if not memories:
            return "(无现有记忆)"
        lines = []
        for m in memories:
            lines.append(f"- [{m.get('type', '?')}] {m.get('title', '?')}: {m.get('body', '')[:200]}")
        return "\n".join(lines)

    @staticmethod
    def _parse_response(response: str) -> list[dict]:
        """解析 LLM 响应(严格 JSON 数组)"""
        # 尝试抽取 ```json ... ``` 块
        m = re.search(r"```json\s*(\[.*?\])\s*```", response, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            text = response.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"蒸馏 LLM 响应 JSON 解析失败: {e}")
            return []
        if not isinstance(data, list):
            logger.error("蒸馏响应不是 JSON 数组")
            return []
        # schema 校验(宽松): 必填字段缺失则跳过
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if "type" not in item or "title" not in item or "body" not in item:
                continue
            out.append(item)
        return out

    @staticmethod
    def _render_frontmatter(
        type: str,
        title: str,
        confidence: float,
        sources: list,
        tags: list,
    ) -> str:
        import yaml
        meta = {
            "type": type,
            "title": title,
            "created_at": time.strftime("%Y-%m-%d"),
            "confidence": float(confidence),
            "sources": sources,
            "tags": tags,
        }
        return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---"

    @staticmethod
    def _render_body(title: str, why: str, body: str) -> str:
        why_section = f"**Why:** {why}\n\n" if why else ""
        return f"# {title}\n\n{why_section}## 内容\n{body}\n"


# ──────────────────────────────────────────────────────────────────
# DistillationScheduler —— 调度 + 锁 + 端到端 run
# ──────────────────────────────────────────────────────────────────

class DistillationScheduler:
    """
    蒸馏调度器(§7.1 + §7.2)

    四重门:
    - 门0: feature gate (config.enabled)
    - 门1: 时间门(从 .consolidate-lock mtime 读, ≥ min_interval_hours)
    - 门2: 扫描节流(避免反复 listdir)
    - 门3: session 数量门(增量 ≥ min_sessions_for_distill)

    锁 v2.1(A1+A2+A11):
    - A1: _acquire_lock 用 O_CREAT|O_EXCL 原子创建
    - A11: envelope = JSON {pid, host, started_at, schema_version}
    - A2: 失败路径 utime 回 prior_mtime

    run() 流程:
    1. should_distill() → True 才往下走
    2. _acquire_lock() → prior_mtime_ms 或 0(被占)
    3. Distiller.distill() 拿 candidates
    4. dry_run=True 跳过写,False 调 write_candidates
    5. 释放锁(success=True 保留 mtime;失败回滚)
    """

    LOCK_FILENAME = ".consolidate-lock"
    ENVELOPE_SUFFIX = ".lock.json"
    MTIME_FILENAME = ".last-distill"  # 持久化 mtime = 上次成功蒸馏时间

    # 锁被占时 _acquire_lock 的返回(与 prior_mtime_ms >= 0 区分)
    LOCK_TAKEN = -1

    def __init__(
        self,
        memory_root: Union[str, Path],
        config: Optional[DistillationConfig] = None,
        llm_callback: Optional[Callable[[str], str]] = None,
    ):
        self.memory_root = Path(memory_root).expanduser()
        self.config = config or DistillationConfig()
        self.llm = llm_callback
        self._lock_path = self.memory_root / self.LOCK_FILENAME
        # envelope 用 .lock.json 后缀(不用 with_suffix 避免 dotfile 歧义)
        self._envelope_path = self.memory_root / ".consolidate-lock.lock.json"
        # 上次成功蒸馏时间(独立文件,避免 O_EXCL 与 mtime 持久化冲突)
        self._mtime_path = self.memory_root / self.MTIME_FILENAME
        # 候选目录 = memory_root/_candidate/
        self._candidate_root = self.memory_root / "_candidate"

    # ── 公开 API ─────────────────────────────────────

    def should_distill(self) -> tuple[bool, str]:
        """四重门检查"""
        # 门0: feature gate
        if not self.config.enabled:
            return False, "gate_disabled"

        # 门4: 锁状态(忙检查)
        lock_state = self._check_lock_state()
        if lock_state["busy"]:
            return False, f"locked_by_{lock_state['holder_pid']}"

        # 门1: 时间门(从 .last-distill mtime 读, 不存在 → inf = 通过)
        age_hours = lock_state["age_hours"]
        if age_hours < self.config.min_interval_hours:
            return False, f"too_soon({age_hours:.1f}h<{self.config.min_interval_hours}h)"

        # 门3: session 数量门
        sessions = self._count_recent_sessions(lock_state["prior_mtime"])
        if sessions < self.config.min_sessions_for_distill:
            return False, f"too_few_sessions({sessions}<{self.config.min_sessions_for_distill})"

        return True, "ok"

    def run(
        self,
        dry_run: bool = True,
        session_log_files: Optional[list[Path]] = None,
        existing_memories: Optional[list[dict]] = None,
    ) -> DistillationResult:
        """
        端到端运行

        Args:
            dry_run: True 只算候选不写盘;False 写到 _candidate/{type}/
            session_log_files: 增量 session 日志;None = 自动扫描 logs/
            existing_memories: 现有记忆;None = 自动从 memory_store 读
        """
        # 1. 门检查
        with tracer.start_as_current_span("memory.distill") as span:
            ok, reason = self.should_distill()
            span.set_attribute("memory.distill.dry_run", dry_run)
            span.set_attribute("memory.distill.gate_ok", ok)
            span.set_attribute("memory.distill.gate_reason", reason)

            if not ok:
                return DistillationResult(success=False, skipped=True, skip_reason=reason)

            # 2. 锁
            prior_mtime_ms = self._acquire_lock()
            span.set_attribute("memory.distill.lock_taken", prior_mtime_ms == self.LOCK_TAKEN)
            if prior_mtime_ms == self.LOCK_TAKEN:
                return DistillationResult(success=False, skipped=True, skip_reason="locked")

            # 3. 准备输入
            try:
                if session_log_files is None:
                    session_log_files = self._scan_session_logs(prior_mtime_ms / 1000)
                existing = existing_memories if existing_memories is not None else self._read_existing_memories()

                if self.llm is None:
                    self._release_lock(prior_mtime_ms, success=False)
                    return DistillationResult(
                        success=False, skipped=True, skip_reason="no_llm_callback",
                        prior_mtime_ms=prior_mtime_ms,
                    )

                # 4. 蒸馏
                distiller = Distiller(self.llm, candidate_root=self._candidate_root)
                candidates = distiller.distill(session_log_files, existing)

                # 5. 写候选(非 dry_run)
                written: list[Path] = []
                if not dry_run:
                    written = distiller.write_candidates(candidates, self._candidate_root)

                # 6. 释放锁(成功)
                self._release_lock(prior_mtime_ms, success=True)

                span.set_attribute("memory.distill.success", True)
                span.set_attribute("memory.distill.candidates", len(candidates))
                span.set_attribute("memory.distill.candidates_written", len(written))

                return DistillationResult(
                    success=True,
                    candidates=candidates,
                    candidates_written=written,
                    sessions_processed=len(session_log_files),
                    prior_mtime_ms=prior_mtime_ms,
                )
            except Exception as e:
                logger.exception("蒸馏异常,回滚 mtime")
                self._release_lock(prior_mtime_ms, success=False)
                span.set_attribute("memory.distill.success", False)
                span.set_attribute("memory.distill.error", str(e))
                return DistillationResult(
                    success=False, prior_mtime_ms=prior_mtime_ms, error=str(e),
                )

    # ── 四重门 + session 计数 ────────────────────────────

    def _check_lock_state(self) -> dict:
        """
        检查锁状态 + 时间门

        Returns:
            {busy: bool, holder_pid: int|None, age_hours: float, prior_mtime: float}

        busy 判定只看 .consolidate-lock(瞬态锁文件);
        age_hours / prior_mtime 从 .last-distill(持久 mtime 文件)读。
        """
        # 1. 锁状态(从 .consolidate-lock 读)
        busy = False
        holder_pid = None
        if self._lock_path.exists():
            envelope = self._read_envelope()
            holder_pid = envelope.get("pid") if envelope else None
            lock_mtime = self._lock_path.stat().st_mtime
            age_seconds = time.time() - lock_mtime

            # A1: PID 已死 → 可被强占
            pid_dead = holder_pid is not None and not self._pid_alive(holder_pid)
            # A2: mtime 超时 → 可被强占
            mtime_stale = age_seconds > self.config.lock_stale_mtime_seconds

            if not pid_dead and not mtime_stale:
                busy = True

        # 2. 时间门(从 .last-distill 读)
        if self._mtime_path.exists():
            prior_mtime = self._mtime_path.stat().st_mtime
            age_hours = (time.time() - prior_mtime) / 3600
        else:
            prior_mtime = 0.0
            age_hours = float("inf")  # 无上次时间 → 通过

        return {"busy": busy, "holder_pid": holder_pid, "age_hours": age_hours, "prior_mtime": prior_mtime}

    def _count_recent_sessions(self, since_mtime: float) -> int:
        """
        统计 since_mtime 之后改动的 session 数

        目录约定:.agent_data/logs/{session_id}.jsonl(DualChannelWriter._do_channel_a_write)
        """
        logs_dir = self.memory_root.parent / "logs"
        if not logs_dir.exists():
            return 0
        count = 0
        try:
            for entry in os.scandir(logs_dir):
                if entry.name.endswith(".jsonl"):
                    try:
                        if entry.stat().st_mtime > since_mtime:
                            count += 1
                    except OSError:
                        continue
        except OSError:
            return 0
        return count

    def _scan_session_logs(self, since_mtime: float) -> list[Path]:
        """扫描 since_mtime 之后的 session 日志文件"""
        logs_dir = self.memory_root.parent / "logs"
        if not logs_dir.exists():
            return []
        result = []
        for entry in os.scandir(logs_dir):
            if entry.name.endswith(".jsonl"):
                try:
                    if entry.stat().st_mtime > since_mtime:
                        result.append(Path(entry.path))
                except OSError:
                    continue
        return sorted(result)

    def _read_existing_memories(self) -> list[dict]:
        """读现有记忆(简单 glob,无 MemoryStore 依赖,降耦合)"""
        memories: list[dict] = []
        for type_dir in ("user", "feedback", "project", "reference"):
            type_path = self.memory_root / type_dir
            if not type_path.exists():
                continue
            for entry in os.scandir(type_path):
                if entry.name.endswith(".md"):
                    try:
                        text = Path(entry.path).read_text(encoding="utf-8")
                        memories.append({"type": type_dir, "path": entry.path, "text": text})
                    except OSError:
                        continue
        return memories

    # ── 锁 A1+A2+A11 ────────────────────────────────

    def _acquire_lock(self) -> int:
        """
        原子获取锁,返回 prior_mtime_ms

        Returns:
            int >= 0: 获取成功,prior_mtime_ms = .last-distill 的 mtime(无则 0)
            LOCK_TAKEN (-1): 锁被占

        A1: O_CREAT|O_EXCL 原子创建锁文件
        A11: 写 JSON envelope
        强占: 先检测 .consolidate-lock 是否陈旧(PID 死 OR mtime 超),是则删除
        """
        # 0. 强占陈旧锁(若有)
        if self._lock_path.exists():
            envelope = self._read_envelope()
            holder_pid = envelope.get("pid") if envelope else None
            age_seconds = time.time() - self._lock_path.stat().st_mtime
            pid_dead = holder_pid is not None and not self._pid_alive(holder_pid)
            mtime_stale = age_seconds > self.config.lock_stale_mtime_seconds
            if pid_dead or mtime_stale:
                # 强占:删陈旧锁 + envelope
                try:
                    self._lock_path.unlink()
                except FileNotFoundError:
                    pass
                self._clear_envelope()

        # 1. 拿 .last-distill 的 prior mtime(用于失败回滚)
        prior_mtime = self._mtime_path.stat().st_mtime if self._mtime_path.exists() else 0.0

        # 2. 原子创建锁文件
        try:
            fd = os.open(
                str(self._lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return self.LOCK_TAKEN

        try:
            # 3. 写 envelope(A11)
            envelope = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": time.time(),
                "schema_version": 1,
            }
            os.write(fd, json.dumps(envelope).encode("utf-8"))
        finally:
            os.close(fd)

        return int(prior_mtime * 1000) if prior_mtime > 0 else 0

    def _release_lock(self, prior_mtime_ms: int, success: bool) -> None:
        """
        释放锁 + 推进/回滚 .last-distill mtime

        success=True:
            - 删除 .consolidate-lock
            - touch .last-distill(mtime = now,成为新的"上次成功蒸馏时间")
        success=False:
            - 删除 .consolidate-lock
            - utime .last-distill 回 prior(失败 run 不推进 24h 门)
        """
        # 删除锁文件(无论成功失败)
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass
        self._clear_envelope()

        # 处理 .last-distill mtime
        if success:
            # 成功:touch .last-distill(mtime = now)
            self._mtime_path.touch()
        else:
            # 失败:回滚 mtime 到 prior
            if prior_mtime_ms > 0:
                ts_sec = prior_mtime_ms / 1000
                try:
                    self._mtime_path.touch()
                    os.utime(self._mtime_path, (ts_sec, ts_sec))
                except OSError as e:
                    logger.warning(f"回滚 .last-distill mtime 失败: {e}")

    def _read_envelope(self) -> dict:
        """读 JSON envelope;garbage / missing → {}"""
        if not self._envelope_path.exists():
            return {}
        try:
            text = self._envelope_path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            return {}
        except (OSError, json.JSONDecodeError):
            return {}  # garbage → 视为空(可被强占)

    def _write_envelope(self) -> None:
        """写 envelope(冗余,锁文件本身已含 envelope;保留供诊断)"""
        envelope = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": time.time(),
            "schema_version": 1,
        }
        self._envelope_path.write_text(
            json.dumps(envelope), encoding="utf-8",
        )

    def _clear_envelope(self) -> None:
        """清 envelope(锁文件本身保留)"""
        try:
            self._envelope_path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """检查 PID 是否存活(复用 ipc_lock 帮助函数)"""
        if pid is None or pid <= 0:
            return False
        # 进程内复用 ipc_lock 已实现的版本
        from agent_core.memory.ipc_lock import _is_pid_alive
        return _is_pid_alive(pid)


# ──────────────────────────────────────────────────────────────────
# 公开 API
# ──────────────────────────────────────────────────────────────────

__all__ = [
    "Distiller",
    "DistillationScheduler",
    "DistillationError",
    "DistillationResult",
]