"""会话管理器。

负责会话的创建、加载、保存和列表操作。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .models import Session, generate_session_id


class SessionManager:
    """会话管理器。

    管理会话的持久化和检索。
    存储结构：data/sessions/{session_id}/session.json
    """

    def __init__(self, config: AgentConfig):
        """初始化会话管理器。

        Args:
            config: 智能体配置对象
        """
        self.config = config
        self._session_root = Path(config.session_workspace)
        self._session_root.mkdir(parents=True, exist_ok=True)

    def _get_session_path(self, session_id: str) -> Path:
        """获取会话文件路径。"""
        return self._session_root / session_id / "session.json"

    def create_session(
        self,
        user_id: str | None = None,
        channel: str = "chat",
        metadata: dict | None = None,
    ) -> Session:
        """创建新会话。

        Args:
            user_id: 用户 ID，默认使用配置中的 user_id
            channel: 活跃通道，默认 "chat"
            metadata: 额外的元数据

        Returns:
            新创建的 Session 对象
        """
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

    def load_session(self, session_id: str) -> Session | None:
        """加载会话。

        Args:
            session_id: 会话 ID

        Returns:
            Session 对象，如果不存在返回 None
        """
        session_file = self._get_session_path(session_id)
        if not session_file.exists():
            return None

        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            return Session.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def save_session(self, session: Session) -> None:
        """保存会话。

        Args:
            session: 要保存的 Session 对象
        """
        session_dir = self._session_root / session.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "session.json"
        session_file.write_text(json.dumps(session.to_dict()), encoding="utf-8")

    def touch_session(self, session_id: str, channel: str | None = None) -> bool:
        """更新会话的最后活跃时间。

        Args:
            session_id: 会话 ID
            channel: 活跃通道，可选

        Returns:
            是否成功更新
        """
        session = self.load_session(session_id)
        if session is None:
            return False

        session.touch(channel=channel)
        self.save_session(session)
        return True

    def list_sessions(self, user_id: str | None = None) -> list[Session]:
        """列出用户的会话。

        Args:
            user_id: 用户 ID，为空时使用配置中的 user_id

        Returns:
            Session 列表，按 updated_at 倒序
        """
        if user_id is None:
            user_id = self.config.user_id

        sessions: list[Session] = []
        if not self._session_root.exists():
            return sessions

        for session_dir in sorted(self._session_root.iterdir()):
            if not session_dir.is_dir():
                continue
            session_file = session_dir / "session.json"
            if not session_file.exists():
                continue

            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                # 过滤用户
                if data.get("user_id") == user_id:
                    sessions.append(Session.from_dict(data))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        # 按更新时间倒序
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions

    def delete_session(self, session_id: str) -> bool:
        """删除会话。

        Args:
            session_id: 会话 ID

        Returns:
            是否成功删除
        """
        session_dir = self._session_root / session_id
        if not session_dir.exists():
            return False

        import shutil
        shutil.rmtree(session_dir)
        return True

    def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在。

        Args:
            session_id: 会话 ID

        Returns:
            会话是否存在
        """
        session_file = self._get_session_path(session_id)
        return session_file.exists()


__all__ = ["SessionManager"]
