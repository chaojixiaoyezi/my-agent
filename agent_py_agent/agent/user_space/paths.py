# LLM: User-space module; keep per-user path and migration behavior stable.
# 模块用途: 管理用户隔离目录、路径推导和旧数据迁移。

from __future__ import annotations

"""用户路径解析模块。

根据 user_id 计算用户专属的数据目录路径。
"""

from dataclasses import dataclass
from pathlib import Path


# LLM: UserPaths is a 用户空间隔离 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 用户数据路径集合。 包含某个用户的所有数据目录路径。
@dataclass
class UserPaths:
    """用户数据路径集合。

    包含某个用户的所有数据目录路径。"""

    user_id: str
    root_dir: Path
    memory_path: Path
    subagent_workspace: Path
    gateway_workspace: Path
    sessions_dir: Path
    local_store_path: Path
    local_store_files_dir: Path
    local_store_events_path: Path


# LLM: get_user_paths belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 根据 user_id 计算用户专属路径。。
def get_user_paths(user_id: str, base_dir: Path | str) -> UserPaths:
    """根据 user_id 计算用户专属路径。

    Args:
        user_id: 用户标识符
        base_dir: 用户数据根目录（通常是 data/users）

    Returns:
        UserPaths: 包含用户所有数据路径的对象"""
    if isinstance(base_dir, str):
        base_dir = Path(base_dir)
    base_dir = base_dir.resolve()

    root_dir = base_dir / user_id
    return UserPaths(
        user_id=user_id,
        root_dir=root_dir,
        memory_path=root_dir / "memory.jsonl",
        subagent_workspace=root_dir / "subagents",
        gateway_workspace=root_dir / "gateway",
        sessions_dir=root_dir / "sessions",
        local_store_path=root_dir / "local_store" / "local.db",
        local_store_files_dir=root_dir / "local_store" / "files",
        local_store_events_path=root_dir / "local_store" / "events.jsonl",
    )


# LLM: get_admin_paths belongs to 用户空间隔离; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 获取管理员路径（默认用户 admin）。。
def get_admin_paths(base_dir: Path | str) -> UserPaths:
    """获取管理员路径（默认用户 admin）。

    Args:
        base_dir: 用户数据根目录

    Returns:
        UserPaths: admin 用户的路径集合"""
    return get_user_paths("admin", base_dir)
