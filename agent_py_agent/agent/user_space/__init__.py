# LLM: User-space module; keep per-user path and migration behavior stable.
# 模块用途: 管理用户隔离目录、路径推导和旧数据迁移。

from __future__ import annotations

"""用户数据隔离模块。

按 user_id 隔离数据目录，为多用户场景打基础。
"""

from .manager import UserSpaceManager
from .migration import migrate_to_user_space
from .paths import UserPaths, get_user_paths

__all__ = ["UserPaths", "get_user_paths", "UserSpaceManager", "migrate_to_user_space"]
