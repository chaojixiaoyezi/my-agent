"""会话管理模块。

提供会话持久化和恢复功能。
"""
from __future__ import annotations

from .models import Session, generate_session_id
from .manager import SessionManager
from .cross_channel import CrossChannelSession
from .context_sync import SessionContextSync, format_context_for_channel
from .admin_query import AdminCrossChannelQuery

__all__ = [
    "Session",
    "generate_session_id",
    "SessionManager",
    "CrossChannelSession",
    "SessionContextSync",
    "format_context_for_channel",
    "AdminCrossChannelQuery",
]
