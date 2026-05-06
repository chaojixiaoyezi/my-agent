from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


class ChannelInfo:
    """通道信息。"""

    def __init__(self, active: bool = False, last_active_at: float = 0):
        self.active = active
        self.last_active_at = last_active_at

    def to_dict(self) -> dict:
        return {"active": self.active, "last_active_at": self.last_active_at}

    @classmethod
    def from_dict(cls, data: dict) -> ChannelInfo:
        return cls(active=data.get("active", False), last_active_at=data.get("last_active_at", 0))


class CrossChannelSession:

    def __init__(self, config: AgentConfig):
        self.config = config
        self._session_root = Path(config.session_workspace)
        self._session_root.mkdir(parents=True, exist_ok=True)

    def _get_channels_path(self, session_id: str) -> Path:
        """获取通道配置文件路径。"""
        return self._session_root / session_id / "channels.json"

    def _load_channels(self, session_id: str) -> dict | None:
        """加载通道配置。"""
        path = self._get_channels_path(session_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_channels(self, session_id: str, data: dict) -> None:
        """保存通道配置。"""
        path = self._get_channels_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def bind_session(self, session_id: str, channel: str, user_id: str | None = None) -> bool:
        data = self._load_channels(session_id)

        if data is None:
            # 创建新通道配置
            if user_id is None:
                user_id = self.config.user_id
            data = {
                "session_id": session_id,
                "user_id": user_id,
                "channels": {},
                "primary_channel": channel,
            }

        # 更新或添加通道
        now = time.time()
        if channel not in data["channels"]:
            data["channels"][channel] = {"active": False, "last_active_at": 0}

        data["channels"][channel]["active"] = True
        data["channels"][channel]["last_active_at"] = now

        # 如果这是唯一活跃通道，设为主通道
        active_channels = [ch for ch, info in data["channels"].items() if info.get("active")]
        if len(active_channels) == 1:
            data["primary_channel"] = channel

        self._save_channels(session_id, data)
        return True

    def unbind_channel(self, session_id: str, channel: str) -> bool:
        data = self._load_channels(session_id)
        if data is None:
            return False

        if channel not in data["channels"]:
            return False

        data["channels"][channel]["active"] = False

        # 如果解绑的是主通道，选择另一个活跃通道
        if data.get("primary_channel") == channel:
            active_channels = [ch for ch, info in data["channels"].items() if info.get("active")]
            data["primary_channel"] = active_channels[0] if active_channels else ""

        self._save_channels(session_id, data)
        return True

    def get_bound_sessions(self, session_id: str) -> list[dict]:
        data = self._load_channels(session_id)
        if data is None:
            return []

        result = []
        for channel, info in data.get("channels", {}).items():
            result.append({
                "channel": channel,
                "active": info.get("active", False),
                "last_active_at": info.get("last_active_at", 0),
            })
        return result

    def get_active_session(self, user_id: str, channel: str | None = None) -> str | None:
        """获取用户在某通道的活跃会话."""
        if not self._session_root.exists():
            return None
        for session_dir in self._session_root.iterdir():
            if not session_dir.is_dir():
                continue
            session_id = self._find_session_in_dir(session_dir, user_id, channel)
            if session_id:
                return session_id
        return None

    def _find_session_in_dir(self, session_dir: Path, user_id: str, channel: str | None) -> str | None:
        """Find active session ID in a session dir, or None."""
        channels_file = session_dir / "channels.json"
        if not channels_file.exists():
            return None
        try:
            data = json.loads(channels_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if data.get("user_id") != user_id:
            return None
        return self._find_active_channel_session(data, channel)

    def _find_active_channel_session(self, data: dict, channel: str | None) -> str | None:
        """Find active session from channel data."""
        for ch, info in data.get("channels", {}).items():
            if info.get("active") and (channel is None or ch == channel):
                return data.get("session_id")
        return None

    def transfer_session(self, session_id: str, from_channel: str, to_channel: str) -> bool:
        data = self._load_channels(session_id)
        if data is None:
            return False

        # 关闭源通道
        if from_channel in data["channels"]:
            data["channels"][from_channel]["active"] = False

        # 开启目标通道
        now = time.time()
        if to_channel not in data["channels"]:
            data["channels"][to_channel] = {"active": False, "last_active_at": 0}
        data["channels"][to_channel]["active"] = True
        data["channels"][to_channel]["last_active_at"] = now

        # 更新主通道
        data["primary_channel"] = to_channel

        self._save_channels(session_id, data)
        return True

    def get_primary_channel(self, session_id: str) -> str | None:
        """获取会话的主通道。"""
        data = self._load_channels(session_id)
        if data is None:
            return None
        return data.get("primary_channel")

    def list_sessions_by_channel(self, user_id: str, channel: str) -> list[str]:
        """列出用户在指定通道的会话 ID 列表。"""
        if not self._session_root.exists():
            return []

        sessions = []
        for session_dir in self._session_root.iterdir():
            if not session_dir.is_dir():
                continue
            channels_file = session_dir / "channels.json"
            if not channels_file.exists():
                continue

            try:
                data = json.loads(channels_file.read_text(encoding="utf-8"))
                if data.get("user_id") != user_id:
                    continue
                if channel in data.get("channels", {}):
                    sessions.append(data.get("session_id"))
            except (json.JSONDecodeError, OSError):
                continue

        return sessions


__all__ = ["CrossChannelSession", "ChannelInfo"]