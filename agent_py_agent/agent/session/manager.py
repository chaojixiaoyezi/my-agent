from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .models import Session, generate_session_id


class SessionManager:

    def __init__(self, config: AgentConfig):
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
        session_file = self._get_session_path(session_id)
        if not session_file.exists():
            return None

        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            return Session.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def save_session(self, session: Session) -> None:
        session_dir = self._session_root / session.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "session.json"
        session_file.write_text(json.dumps(session.to_dict()), encoding="utf-8")

    def touch_session(self, session_id: str, channel: str | None = None) -> bool:
        session = self.load_session(session_id)
        if session is None:
            return False

        session.touch(channel=channel)
        self.save_session(session)
        return True

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

    def delete_session(self, session_id: str) -> bool:
        session_dir = self._session_root / session_id
        if not session_dir.exists():
            return False

        import shutil
        shutil.rmtree(session_dir)
        return True

    def session_exists(self, session_id: str) -> bool:
        session_file = self._get_session_path(session_id)
        return session_file.exists()

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
