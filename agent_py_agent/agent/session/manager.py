
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.json_io import read_json_object_report, write_json_object
from ..common.opaque_id import validate_opaque_id
from ..runtime_errors import runtime_error_report

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .models import Session, generate_session_id


class SessionManager:

    def __init__(self, config: AgentConfig):
        self.config = config
        self._session_root = Path(config.session_workspace)
        self._session_root.mkdir(parents=True, exist_ok=True)

    def _get_session_path(self, session_id: str) -> Path:
        """获取会话文件路径（B.2：ID 拼路径前必须过拒绝式校验）。"""
        validate_opaque_id(session_id, kind="session_id")
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
        write_json_object(
            self._get_session_path(session_id),
            {
                "session_id": session_id,
                "user_id": user_id,
                "created_at": now_time,
                "updated_at": now_time,
                "last_active_channel": channel,
                "metadata": metadata or {},
            },
        )

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

        session, _load_error = self.load_session_report(session_id)
        return session

    def load_session_report(self, session_id: str) -> tuple[Session | None, dict[str, Any] | None]:
        session_file = self._get_session_path(session_id)
        if not session_file.exists():
            return None, None
        return _read_session_report(session_file, session_id)

    def save_session(self, session: Session) -> None:
        write_json_object(self._get_session_path(session.session_id), session.to_dict())

    def touch_session(self, session_id: str, channel: str | None = None) -> bool:
        session = self.load_session(session_id)
        if session is None:
            return False

        session.touch(channel=channel)
        self.save_session(session)
        return True

    def list_sessions(self, user_id: str | None = None) -> list[Session]:
        sessions, _load_errors = self.list_sessions_report(user_id)
        return sessions

    def list_sessions_report(self, user_id: str | None = None) -> tuple[list[Session], list[dict[str, Any]]]:
        if user_id is None:
            user_id = self.config.user_id

        sessions: list[Session] = []
        load_errors: list[dict[str, Any]] = []
        if not self._session_root.exists():
            return sessions, load_errors

        for session_dir in sorted(self._session_root.iterdir()):
            session, error = self._load_session_for_user_report(session_dir, user_id)
            if session is not None:
                sessions.append(session)
            if error is not None:
                load_errors.append(error)

        # 按更新时间倒序
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions, load_errors

    def delete_session(self, session_id: str) -> bool:
        session_dir = self._get_session_path(session_id).parent
        if not session_dir.exists():
            return False

        import shutil
        shutil.rmtree(session_dir)
        return True

    def session_exists(self, session_id: str) -> bool:
        session_file = self._get_session_path(session_id)
        return session_file.exists()

    def _load_session_for_user(self, session_dir: Path, user_id: str) -> Session | None:
        session, _load_error = self._load_session_for_user_report(session_dir, user_id)
        return session

    def _load_session_for_user_report(
        self,
        session_dir: Path,
        user_id: str,
    ) -> tuple[Session | None, dict[str, Any] | None]:
        if not session_dir.is_dir():
            return None, None
        session_file = session_dir / "session.json"
        if not session_file.exists():
            return None, None
        session, error = _read_session_report(session_file, session_dir.name)
        if error is not None:
            return None, error
        return (session, None) if session is not None and session.user_id == user_id else (None, None)


def _read_session_report(path: Path, session_id: str) -> tuple[Session | None, dict[str, Any] | None]:
    report = read_json_object_report(path, context="session.manager.session.read")
    if report.load_error is not None:
        return None, _session_load_error(path, session_id, report.load_error)
    try:
        return Session.from_dict(report.payload), None
    except (KeyError, TypeError, ValueError) as exc:
        return None, _session_load_error(path, session_id, runtime_error_report(exc, context="session.manager.session.read"))


def _session_load_error(path: Path, session_id: str, report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload["path"] = str(path)
    payload["session_id"] = session_id
    return payload


__all__ = ["SessionManager"]
