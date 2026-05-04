"""通道在线状态检测。

检测各通道是否在线：
- chat: 检查 data/sessions/ 下是否有活跃会话
- feishu/qq: 检查对应适配器状态文件
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


class ChannelStatusChecker:
    """通道状态检测器。

    检测用户各通道的在线状态：
    - chat：检查是否有活跃的 chat 会话（updated_at 在超时时间内）
    - feishu/qq：检查适配器状态文件
    """

    def __init__(self, config: AgentConfig):
        """初始化检测器。

        Args:
            config: 智能体配置对象
        """
        self.config = config
        self._session_workspace = Path(config.session_workspace)
        self._adapter_workspace = Path(config.adapter_workspace)
        self._channel_timeout = getattr(config, "notification_channel_timeout_seconds", 300)
        # 内存中注册的状态，覆盖文件检测
        self._registered_status: dict[str, bool] = {}

    def check(self, channel: str, user_id: str | None = None) -> bool:
        """检查通道是否在线。

        Args:
            channel: 通道名称（chat/feishu/qq）
            user_id: 用户 ID，可选

        Returns:
            通道是否在线
        """
        # 先检查注册状态
        if channel in self._registered_status:
            return self._registered_status[channel]

        if channel == "chat":
            return self._check_chat_online(user_id)
        elif channel in ("feishu", "qq"):
            return self._check_adapter_online(channel)
        else:
            return False

    def _check_chat_online(self, user_id: str | None = None) -> bool:
        """检查 chat 通道是否在线."""
        if not self._session_workspace.exists():
            return False
        timeout = self._channel_timeout
        for session_dir in self._session_workspace.iterdir():
            if self._is_chat_session_online(session_dir, user_id, timeout):
                return True
        return False

    def _is_chat_session_online(self, session_dir: Path, user_id: str | None, timeout: float) -> bool:
        """Return True if session dir has a chat session active within timeout."""
        if not session_dir.is_dir():
            return False
        session_file = session_dir / "session.json"
        if not session_file.exists():
            return False
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError):
            return False
        if user_id and data.get("user_id") != user_id:
            return False
        if data.get("last_active_channel") == "chat":
            updated_at = data.get("updated_at", 0)
            return (time.time() - updated_at) < timeout
        return False

    def _check_adapter_online(self, channel: str) -> bool:
        """检查适配器通道是否在线。

        检查 data/adapters/{channel}/status.json 是否存在且标记 running。
        """
        status_file = self._adapter_workspace / channel / "status.json"
        if not status_file.exists():
            return False

        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            return data.get("status") == "running"
        except (json.JSONDecodeError, KeyError, OSError):
            return False

    def register_status(self, channel: str, status: bool) -> None:
        """注册通道状态。

        适配器启动/停止时调用。
        内存状态优先级高于文件检测。

        Args:
            channel: 通道名称
            status: True=在线，False=离线
        """
        self._registered_status[channel] = status

    def unregister_status(self, channel: str) -> None:
        """取消注册通道状态。

        Args:
            channel: 通道名称
        """
        self._registered_status.pop(channel, None)

    def get_active_channels(self, user_id: str | None = None) -> list[str]:
        """获取用户所有在线通道。

        Args:
            user_id: 用户 ID，可选

        Returns:
            在线通道列表
        """
        active = []

        # 检查各通道
        for channel in ("chat", "feishu", "qq"):
            if self.check(channel, user_id):
                active.append(channel)

        return active

    def set_channel_timeout(self, seconds: int) -> None:
        """设置通道超时时间。

        Args:
            seconds: 超时秒数
        """
        self._channel_timeout = seconds


__all__ = ["ChannelStatusChecker"]
