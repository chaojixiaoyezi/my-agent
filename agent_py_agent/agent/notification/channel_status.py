# LLM: 这里决定通知路由是否直达或降级存储，状态判断要保守。
# 模块用途: 通知渠道在线状态探测、缓存和超时登记。

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


# LLM: ChannelStatusChecker 属于 通知系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 缓存并探测聊天会话和适配器通知渠道的在线状态。
class ChannelStatusChecker:

    # LLM: ChannelStatusChecker.__init__ 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 ChannelStatusChecker 的依赖、配置和运行期字段。
    def __init__(self, config: AgentConfig):
        self.config = config
        self._session_workspace = Path(config.session_workspace)
        self._adapter_workspace = Path(config.adapter_workspace)
        self._channel_timeout = getattr(config, "notification_channel_timeout_seconds", 300)
        # 内存中注册的状态，覆盖文件检测
        self._registered_status: dict[str, bool] = {}

    # LLM: ChannelStatusChecker.check 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 check 步骤，并保持调用方依赖的数据形状。
    def check(self, channel: str, user_id: str | None = None) -> bool:
        # 先检查注册状态
        if channel in self._registered_status:
            return self._registered_status[channel]

        if channel == "chat":
            return self._check_chat_online(user_id)
        elif channel in ("feishu", "qq"):
            return self._check_adapter_online(channel)
        else:
            return False

    # LLM: ChannelStatusChecker._check_chat_online 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 check_chat_online 步骤，并保持调用方依赖的数据形状。
    def _check_chat_online(self, user_id: str | None = None) -> bool:
        """检查 chat 通道是否在线."""
        if not self._session_workspace.exists():
            return False
        timeout = self._channel_timeout
        for session_dir in self._session_workspace.iterdir():
            if self._is_chat_session_online(session_dir, user_id, timeout):
                return True
        return False

    # LLM: ChannelStatusChecker._is_chat_session_online 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 判断 is_chat_session_online 是否满足安全或状态条件。
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

    # LLM: ChannelStatusChecker._check_adapter_online 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 check_adapter_online 步骤，并保持调用方依赖的数据形状。
    def _check_adapter_online(self, channel: str) -> bool:
        status_file = self._adapter_workspace / channel / "status.json"
        if not status_file.exists():
            return False

        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            return data.get("status") == "running"
        except (json.JSONDecodeError, KeyError, OSError):
            return False

    # LLM: ChannelStatusChecker.register_status 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 register_status 步骤，并保持调用方依赖的数据形状。
    def register_status(self, channel: str, status: bool) -> None:
        self._registered_status[channel] = status

    # LLM: ChannelStatusChecker.unregister_status 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 unregister_status 步骤，并保持调用方依赖的数据形状。
    def unregister_status(self, channel: str) -> None:
        self._registered_status.pop(channel, None)

    # LLM: ChannelStatusChecker.get_active_channels 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 查询并返回 get_active_channels，保持返回形状给上层调用。
    def get_active_channels(self, user_id: str | None = None) -> list[str]:
        active = []

        # 检查各通道
        for channel in ("chat", "feishu", "qq"):
            if self.check(channel, user_id):
                active.append(channel)

        return active

    # LLM: ChannelStatusChecker.set_channel_timeout 属于 通知系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 通知系统 中的 set_channel_timeout 步骤，并保持调用方依赖的数据形状。
    def set_channel_timeout(self, seconds: int) -> None:
        self._channel_timeout = seconds


__all__ = ["ChannelStatusChecker"]
