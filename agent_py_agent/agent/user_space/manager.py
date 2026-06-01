# LLM: User-space module; keep per-user path and migration behavior stable.
# 模块用途: 管理用户隔离目录、路径推导和旧数据迁移。

from __future__ import annotations

"""用户空间管理器。

管理用户目录的创建、访问和列表。
"""

from pathlib import Path
from typing import TYPE_CHECKING

from .legacy_user_paths import LegacyUserPaths, get_legacy_user_paths

if TYPE_CHECKING:
    pass


# LLM: UserSpaceManager is a 用户空间隔离 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 用户空间管理器。 负责用户目录的创建、访问和列表。 admin 用户可以访问所有用户目录。
class UserSpaceManager:
    """用户空间管理器。

    负责用户目录的创建、访问和列表。
    admin 用户可以访问所有用户目录。"""

    ADMIN_USER = "admin"

    # LLM: UserSpaceManager.__init__ belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化用户空间管理器。。
    def __init__(self, user_data_root: Path | str):
        """初始化用户空间管理器。

        Args:
            user_data_root: 用户数据根目录（如 data/users）"""
        if isinstance(user_data_root, str):
            user_data_root = Path(user_data_root)
        self.user_data_root = user_data_root.resolve()

    # LLM: UserSpaceManager.ensure_user_space belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 确保用户目录存在，不存在则创建。；会写入或调整文件，改动时要确认路径、安全边界和失败恢复。
    def ensure_user_space(self, user_id: str) -> LegacyUserPaths:
        """确保用户目录存在，不存在则创建。

        Args:
            user_id: 用户标识符

        Returns:
            LegacyUserPaths: 旧 data/users 路径对象"""
        paths = get_legacy_user_paths(user_id, self.user_data_root)

        # 创建必要的目录
        paths.root_dir.mkdir(parents=True, exist_ok=True)
        paths.subagent_workspace.mkdir(parents=True, exist_ok=True)
        paths.gateway_workspace.mkdir(parents=True, exist_ok=True)
        paths.sessions_dir.mkdir(parents=True, exist_ok=True)
        (paths.root_dir / "local_store").mkdir(parents=True, exist_ok=True)

        return paths

    # LLM: UserSpaceManager.get_legacy_user_paths belongs to old data/users compatibility.
    # 函数用途: 获取旧 data/users 用户路径，不自动创建目录。
    def get_legacy_user_paths(self, user_id: str) -> LegacyUserPaths:
        """获取旧 data/users 用户路径，不自动创建目录。

        Args:
            user_id: 用户标识符

        Returns:
            LegacyUserPaths: 旧 data/users 路径对象"""
        return get_legacy_user_paths(user_id, self.user_data_root)

    # LLM: UserSpaceManager.list_users belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 列出所有已创建的用户。。
    def list_users(self) -> list[str]:
        """列出所有已创建的用户。

        Returns:
            list[str]: 用户 ID 列表"""
        if not self.user_data_root.exists():
            return []

        users = []
        for item in self.user_data_root.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                users.append(item.name)
        return sorted(users)

    # LLM: UserSpaceManager.user_exists belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 检查用户目录是否存在。。
    def user_exists(self, user_id: str) -> bool:
        """检查用户目录是否存在。

        Args:
            user_id: 用户标识符

        Returns:
            bool: 用户目录是否存在"""
        paths = get_legacy_user_paths(user_id, self.user_data_root)
        return paths.root_dir.exists() and paths.root_dir.is_dir()

    # LLM: UserSpaceManager.is_admin belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 检查是否为管理员用户。。
    def is_admin(self, user_id: str) -> bool:
        """检查是否为管理员用户。

        Args:
            user_id: 用户标识符

        Returns:
            bool: 是否是管理员"""
        return user_id == self.ADMIN_USER

    # LLM: UserSpaceManager.can_access_user belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 检查请求者是否有权访问目标用户的数据。 规则： - admin 可以访问所有用户 - 其他用户只能访问自己的数据。
    def can_access_user(self, requester_id: str, target_user_id: str) -> bool:
        """检查请求者是否有权访问目标用户的数据。

        规则：
        - admin 可以访问所有用户
        - 其他用户只能访问自己的数据

        Args:
            requester_id: 请求者用户 ID
            target_user_id: 目标用户 ID

        Returns:
            bool: 是否有权访问"""
        if self.is_admin(requester_id):
            return True
        return requester_id == target_user_id
