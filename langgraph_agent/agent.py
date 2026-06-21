"""
LangGraph Agent 封装类
🔑 阶段1.5：SqliteSaver 磁盘持久化 + 会话列表管理
刷新页面后对话不丢失
"""

import json
import sqlite3
import time
from typing import Generator, Optional
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage

from .graph import build_graph
from .state import AgentState

# 默认数据库路径（项目根目录下 .agent_data/）
DEFAULT_DB_PATH = Path(__file__).parent.parent / ".agent_data" / "sessions.db"


class LangGraphAgent:
    """
    LangGraph 版 Agent。
    
    🔑 持久化设计（刷新页面不丢失）：
    - 对话历史：SqliteSaver（LangGraph 官方 checkpoint 持久化）
    - 会话元数据：自定义 SQLite 表（name, created_at, message_count）
    - 两个数据库文件：sessions.db（元数据）+ checkpoints.db（LangGraph checkpoint）
    """
    
    def __init__(
        self,
        llm_router,
        tool_registry,
        max_turns: int = 10,
        system_prompt: Optional[str] = None,
        db_path: Optional[str] = None,
        # M7 集成:记忆 + cache_namespace(默认 None = 不启用)
        memory_retriever=None,
        memory_store=None,
        cache_namespace: Optional[str] = None,
        memory_enabled: bool = False,
    ):
        self.llm_router = llm_router
        self.tool_registry = tool_registry
        self.max_turns = max_turns
        self.system_prompt = system_prompt

        # M7: 记忆模块(若 memory_enabled=True 但 retriever=None,警告但不报错)
        self.memory_retriever = memory_retriever if memory_enabled else None
        self.memory_store = memory_store if memory_enabled else None
        self.cache_namespace = cache_namespace

        # 🔑 磁盘持久化路径
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoints_path = self._db_path.parent / "checkpoints.db"
        
        # 初始化会话元数据表 + 恢复元数据
        self._init_meta_db()
        self._threads = self._load_threads()
        
        # 计算初始 thread_counter（必须在 _create_thread_in_db 之前）
        if self._threads:
            self._thread_counter = max(
                (int(tid.split("_")[1]) for tid in self._threads), default=1
            )
            # 最近更新的会话
            self._thread_id = next(iter(self._threads))
        else:
            self._thread_counter = 0
            self._thread_id = self._create_thread_in_db("新会话")
        
        # 🔑 LangGraph Checkpointer（SQLite）
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
            self._sqlite_conn = sqlite3.connect(str(self._checkpoints_path), check_same_thread=False)
            self._checkpointer = SqliteSaver(self._sqlite_conn)
            self._checkpointer.setup()
        except (ImportError, Exception) as e:
            from langgraph.checkpoint.memory import MemorySaver
            self._checkpointer = MemorySaver()
            self._sqlite_conn = None
        
        self._graph = build_graph(checkpointer=self._checkpointer)
    
    # ── 元数据持久化（SQLite）─────────────────────────────────
    
    def _init_meta_db(self):
        """初始化会话元数据表"""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS thread_meta (
                    thread_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '新会话',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()
    
    def _load_threads(self) -> dict[str, dict]:
        """从 SQLite 加载所有会话元数据，按 updated_at 倒序"""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM thread_meta ORDER BY updated_at DESC"
            ).fetchall()
            return {
                row["thread_id"]: {
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "message_count": row["message_count"],
                }
                for row in rows
            }
    
    def _save_thread_meta(self, thread_id: str, meta: dict):
        """写入/更新单条会话元数据"""
        now = time.time()
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO thread_meta 
                (thread_id, name, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, ?)
            """, (
                thread_id,
                meta.get("name", "新会话"),
                meta.get("created_at", now),
                meta.get("updated_at", now),
                meta.get("message_count", 0),
            ))
            conn.commit()
    
    def _delete_thread_meta(self, thread_id: str):
        """删除会话元数据"""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("DELETE FROM thread_meta WHERE thread_id = ?", (thread_id,))
            conn.commit()
    
    def _create_thread_in_db(self, name: str) -> str:
        """创建新会话并写入数据库，返回 thread_id"""
        self._thread_counter += 1
        new_id = f"thread_{self._thread_counter}"
        now = time.time()
        meta = {"name": name, "created_at": now, "updated_at": now, "message_count": 0}
        self._threads[new_id] = meta
        self._save_thread_meta(new_id, meta)
        return new_id
    
    # ── Agent 核心循环 ────────────────────────────────────────
    
    def run(self, user_message: str) -> Generator:
        """执行 Agent 循环，返回生成器。"""
        initial_state: AgentState = {
            "messages": [HumanMessage(content=user_message)],
            "turn": 0,
            "max_turns": self.max_turns,
            "system_prompt": self.system_prompt,
            "total_tokens": 0,
        }
        
        config = {
            "configurable": {
                "llm_router": self.llm_router,
                "tool_registry": self.tool_registry,
                "system_prompt": self.system_prompt,
                "thread_id": self._thread_id,
                # M7: 注入记忆 + cache_namespace(若启用)
                "memory_retriever": self.memory_retriever,
                "memory_store": self.memory_store,
                "cache_namespace": self.cache_namespace,
            }
        }
        
        yield ("system", "🔄 LangGraph Agent 启动")
        
        last_ai_message = None
        collected_messages = []
        
        for chunk in self._graph.stream(
            initial_state, config,
            stream_mode=["custom", "updates"]
        ):
            if not isinstance(chunk, tuple) or len(chunk) != 2:
                continue
            mode, data = chunk
            
            if mode == "custom":
                if isinstance(data, dict):
                    chunk_type = data.get("type")
                    if chunk_type == "text":
                        yield ("text", data["content"])
                    elif chunk_type == "thinking":
                        yield ("thinking", data["content"])
                    elif chunk_type == "tool_call":
                        yield ("tool_call", {
                            "name": data["name"], "input": data["input"], "id": data["id"],
                        })
                    elif chunk_type == "tool_result":
                        yield ("tool_result", {
                            "name": data["name"], "output": data["output"],
                            "success": data.get("success", True),
                        })
                    elif chunk_type == "turn":
                        yield ("system", f"🔄 Turn {data['turn']}/{self.max_turns}")
                    elif chunk_type == "error":
                        yield ("system", f"❌ {data['message']}")
            
            elif mode == "updates":
                if isinstance(data, dict):
                    for node_name, node_output in data.items():
                        messages = node_output.get("messages", [])
                        for msg in messages:
                            collected_messages.append(msg)
                            if isinstance(msg, AIMessage):
                                last_ai_message = msg
        
        # 结束标记
        if last_ai_message and not getattr(last_ai_message, "tool_calls", []):
            yield ("system", "✅ 回答完成")
        elif last_ai_message:
            yield ("system", "✅ 工具调用完成")
        else:
            yield ("system", f"⚠️ 达到最大轮次（{self.max_turns}），强制结束")
        
        # 🔑 更新元数据并持久化
        self._update_thread_meta(user_message, len(collected_messages))
    
    def _update_thread_meta(self, first_user_msg: str, new_msg_count: int):
        """更新当前会话元数据并写入 SQLite"""
        meta = self._threads.get(self._thread_id, {})
        
        # 自动命名
        if meta.get("name", "新会话") == "新会话" and first_user_msg:
            meta["name"] = first_user_msg[:20] + ("..." if len(first_user_msg) > 20 else "")
        
        meta["message_count"] = meta.get("message_count", 0) + new_msg_count
        meta["updated_at"] = time.time()
        self._threads[self._thread_id] = meta
        self._save_thread_meta(self._thread_id, meta)
    
    # ── 会话管理 API ──────────────────────────────────────────
    
    def list_threads(self) -> list[dict]:
        """获取所有会话列表（按更新时间倒序）"""
        result = []
        for tid, meta in self._threads.items():
            result.append({
                "thread_id": tid,
                "name": meta.get("name", "新会话"),
                "message_count": meta.get("message_count", 0),
                "created_at": meta.get("created_at", 0),
                "updated_at": meta.get("updated_at", meta.get("created_at", 0)),
                "is_active": tid == self._thread_id,
            })
        result.sort(key=lambda x: x["updated_at"], reverse=True)
        return result
    
    def create_thread(self, name: str = "新会话") -> str:
        """创建新会话，返回 thread_id"""
        return self._create_thread_in_db(name)
    
    def switch_thread(self, thread_id: str):
        """切换到指定会话"""
        if thread_id not in self._threads:
            raise ValueError(f"会话 {thread_id} 不存在")
        self._thread_id = thread_id
    
    def delete_thread(self, thread_id: str):
        """删除指定会话（自动切到最近的）"""
        if thread_id not in self._threads:
            raise ValueError(f"会话 {thread_id} 不存在")
        
        self._delete_thread_meta(thread_id)
        del self._threads[thread_id]
        
        if thread_id == self._thread_id:
            if self._threads:
                latest = max(self._threads.items(),
                    key=lambda x: x[1].get("updated_at", x[1].get("created_at", 0)))
                self._thread_id = latest[0]
            else:
                self._thread_id = self._create_thread_in_db("新会话")
    
    def rename_thread(self, thread_id: str, new_name: str):
        """重命名会话"""
        if thread_id not in self._threads:
            raise ValueError(f"会话 {thread_id} 不存在")
        self._threads[thread_id]["name"] = new_name
        self._save_thread_meta(thread_id, self._threads[thread_id])
    
    def get_thread_id(self) -> str:
        return self._thread_id
    
    def get_history(self) -> list[dict]:
        """从 Checkpointer 获取当前会话对话历史"""
        config = {"configurable": {"thread_id": self._thread_id}}
        state_snapshot = self._graph.get_state(config)
        
        if state_snapshot and state_snapshot.values:
            messages = state_snapshot.values.get("messages", [])
            return [
                {"role": getattr(msg, "type", "unknown"), "content": getattr(msg, "content", "")}
                for msg in messages
            ]
        return []
    
    def reset(self):
        """创建新会话并切换"""
        new_id = self._create_thread_in_db("新会话")
        self._thread_id = new_id