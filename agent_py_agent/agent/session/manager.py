# LLM: Session runtime module; keep conversation state and persistence contracts stable.
# 模块用途: 维护会话运行时状态、上下文和持久化边界。

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .models import Session, generate_session_id


# LLM: SessionManager 属于跨通道会话管理的类边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 类用途: 协调会话管理器的下游服务和持久化入口，对外维持稳定管理接口；关键副作用: 方法可能触发会话归属、上下文同步和用户隔离相关副作用，需保持公开契约稳定。
class SessionManager:

    # LLM: __init__ 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def __init__(self, config: AgentConfig):
        self.config = config
        self._session_root = Path(config.session_workspace)
        self._session_root.mkdir(parents=True, exist_ok=True)

    # LLM: _get_session_path 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询会话路径需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _get_session_path(self, session_id: str) -> Path:
        """获取会话文件路径。"""
        return self._session_root / session_id / "session.json"

    # LLM: create_session 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 构建会话所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def create_session(
        self,
        user_id: str | None = None,
        channel: str = "chat",
        metadata: dict | None = None,
    ) -> Session:
        if user_id is None:
            user_id = self.config.user_id

        session_id = generate_session_id()
        now_time = time.time()
        session_json = json.dumps(
            {
                "session_id": session_id,
                "user_id": user_id,
                "created_at": now_time,
                "updated_at": now_time,
                "last_active_channel": channel,
                "metadata": metadata or {},
            }
        )

        # 创建会话目录和文件
        session_dir = self._session_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "session.json"
        session_file.write_text(session_json, encoding="utf-8")

        # 返回 Session 对象
        session = Session(
            session_id=session_id,
            user_id=user_id,
            created_at=now_time,
            updated_at=now_time,
            last_active_channel=channel,
            metadata=metadata or {},
        )
        return session

    # LLM: load_session 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询会话需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def load_session(self, session_id: str) -> Session | None:
        session_file = self._get_session_path(session_id)
        if not session_file.exists():
            return None

        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            return Session.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    # LLM: save_session 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 写入会话的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动会话归属、上下文同步和用户隔离，调用方依赖写入顺序和文件格式。
    def save_session(self, session: Session) -> None:
        session_dir = self._session_root / session.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "session.json"
        session_file.write_text(json.dumps(session.to_dict()), encoding="utf-8")

    # LLM: touch_session 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 处理touch会话相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def touch_session(self, session_id: str, channel: str | None = None) -> bool:
        session = self.load_session(session_id)
        if session is None:
            return False

        session.touch(channel=channel)
        self.save_session(session)
        return True

    # LLM: list_sessions 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询sessions需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def list_sessions(self, user_id: str | None = None) -> list[Session]:
        if user_id is None:
            user_id = self.config.user_id

        sessions: list[Session] = []
        if not self._session_root.exists():
            return sessions

        for session_dir in sorted(self._session_root.iterdir()):
            session = self._load_session_for_user(session_dir, user_id)
            if session is not None:
                sessions.append(session)

        # 按更新时间倒序
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    # LLM: delete_session 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 处理delete会话相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def delete_session(self, session_id: str) -> bool:
        session_dir = self._session_root / session_id
        if not session_dir.exists():
            return False

        import shutil
        shutil.rmtree(session_dir)
        return True

    # LLM: session_exists 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 处理会话exists相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def session_exists(self, session_id: str) -> bool:
        session_file = self._get_session_path(session_id)
        return session_file.exists()

    # LLM: _load_session_for_user 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 读取或查询会话user需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _load_session_for_user(self, session_dir: Path, user_id: str) -> Session | None:
        if not session_dir.is_dir():
            return None
        session_file = session_dir / "session.json"
        if not session_file.exists():
            return None
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        # LLM: list only hydrates sessions after user filtering.
        return Session.from_dict(data) if data.get("user_id") == user_id else None


__all__ = ["SessionManager"]
