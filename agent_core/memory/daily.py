"""
日常日志系统 - Append-only 结构化日志
参考 QClaw 设计：日常日志 + MEMORY.md + 向量索引
"""

from __future__ import annotations
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


class DailyLogger:
    """
    日常日志系统
    
    设计原则：
    - Append-only，永不覆写
    - 结构化 Markdown 格式
    - 支持全文搜索 + 元数据过滤
    - 多线程安全（Stage3 Fork Agent 接入后多个提取 Agent 会并发写）
    """
    
    def __init__(self, log_dir: str = ".agent_data/logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # P2-1 修复：多线程写锁。Stage3 会接入 Fork Agent 异步提取，
        # 不同 agent 并发 log() 会交叉写导致 Markdown 格式错乱。
        # 使用 Lock 而非 RLock（不需要重入），开销最小。
        self._write_lock = threading.Lock()
    
    def _get_log_path(self, date: Optional[datetime] = None) -> Path:
        """获取指定日期的日志文件路径"""
        if date is None:
            date = datetime.now()
        return self.log_dir / f"{date.strftime('%Y-%m-%d')}.md"
    
    def log(
        self,
        session_id: str,
        category: str,
        key: str,
        value: str,
        metadata: Optional[dict] = None,
    ):
        """
        追加一条日志记录
        
        Args:
            session_id: 会话 ID
            category: 类别（user_preference/decision/technical/error）
            key: 记忆键
            value: 记忆值
            metadata: 额外元数据（可选）
        """
        now = datetime.now()
        log_path = self._get_log_path(now)
        
        # 构建日志条目
        entry_lines = [
            f"### {category.replace('_', ' ').title()}",
            f"- **{key}**: {value}",
        ]
        if metadata:
            entry_lines.append(f"  - 元数据: {json.dumps(metadata, ensure_ascii=False)}")
        
        entry = "\n".join(entry_lines)
        
        # 检查是否有今天的日志，没有则写 Header
        # P2-1：多线程安全。整个写块包在锁内，包括"检查 + 写 Header + Append"
        with self._write_lock:
            if not log_path.exists():
                header = f"# 日志: {now.strftime('%Y-%m-%d')}\n\n"
                log_path.write_text(header, encoding="utf-8")
            
            # Append 日志条目（原子写整个 entry，避免跨线程交叉写）
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n## [{now.strftime('%Y-%m-%d %H:%M')}] Session: {session_id}\n\n")
                f.write(entry + "\n")
    
    def search_text(self, query: str, days: int = 7) -> list[str]:
        """
        全文搜索（grep 风格）
        
        Args:
            query: 搜索关键词
            days: 搜索最近 N 天的日志
        
        Returns:
            匹配行列表（带文件名前缀）
        """
        results = []
        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            log_path = self._get_log_path(date)
            if not log_path.exists():
                continue
            
            content = log_path.read_text(encoding="utf-8")
            for line in content.split("\n"):
                if re.search(query, line, re.IGNORECASE):
                    results.append(f"{log_path.name}: {line.strip()}")
        
        return results
    
    def read_recent(self, days: int = 1) -> str:
        """
        读取最近 N 天的日志内容（用于蒸馏）
        
        Args:
            days: 读取最近 N 天
        
        Returns:
            合并后的日志文本
        """
        logs = []
        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            log_path = self._get_log_path(date)
            if log_path.exists():
                logs.append(log_path.read_text(encoding="utf-8"))
        
        return "\n\n---\n\n".join(reversed(logs))
