from __future__ import annotations

"""用户空间迁移工具。

把现有数据迁移到按 user_id 隔离的目录结构。
"""

import os
import shutil
from pathlib import Path
from typing import Any


def migrate_to_user_space(base_dir: Path | str, user_id: str = "admin") -> dict[str, Any]:
    """迁移现有数据到用户空间。

    把 base_dir 下的现有数据移到 data/users/{user_id}/ 下：
    - memory.jsonl
    - subagents/
    - gateway/
    - data/local_store/（移动到用户目录下）

    Args:
        base_dir: 项目根目录
        user_id: 目标用户 ID

    Returns:
        dict: 迁移结果，包含 moved_files, skipped_files, errors
    """
    if isinstance(base_dir, str):
        base_dir = Path(base_dir)
    base_dir = base_dir.resolve()

    user_root = base_dir / "data" / "users" / user_id

    result: dict[str, Any] = {
        "moved_files": [],
        "skipped_files": [],
        "errors": [],
    }

    migrations = [
        ("data/memory.jsonl", user_root / "memory.jsonl"),
        ("data/subagents", user_root / "subagents"),
        ("data/gateway", user_root / "gateway"),
        ("data/local_store", user_root / "local_store"),
    ]

    for source_name, dest_path in migrations:
        _migrate_single(base_dir / source_name, dest_path, result)

    create_migration_marker(base_dir, user_id)
    return result


def _migrate_single(source_path: Path, dest_path: Path, result: dict[str, Any]) -> None:
    """迁移单个文件或目录。"""

    if not source_path.exists() or dest_path.exists():
        result["skipped_files"].append(str(source_path))
        return

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(dest_path))
        result["moved_files"].append(str(source_path))
    except Exception as e:
        result["errors"].append(f"{source_path}: {e}")


def create_migration_marker(base_dir: Path, user_id: str) -> None:
    """在原位置创建迁移提示文件。

    Args:
        base_dir: 项目根目录
        user_id: 用户 ID
    """
    marker_dir = base_dir / "data"
    marker_file = marker_dir / "MIGRATED_TO_USER_SPACE.txt"

    content = (
        f"数据已迁移到用户空间\n"
        f"用户ID: {user_id}\n"
        f"迁移时间: {os.linesep}\n"
        f"请查看 data/users/{user_id}/ 目录\n"
    )

    try:
        marker_file.write_text(content, encoding="utf-8")
    except OSError:
        pass


def check_needs_migration(base_dir: Path | str) -> bool:
    """检查是否需要迁移。

    如果 data/users/ 目录不存在，说明还没做过用户空间迁移。

    Args:
        base_dir: 项目根目录

    Returns:
        bool: 是否需要迁移
    """
    if isinstance(base_dir, str):
        base_dir = Path(base_dir)
    base_dir = base_dir.resolve()

    user_data_root = base_dir / "data" / "users"
    return not user_data_root.exists()


def get_migration_status(base_dir: Path | str) -> dict[str, Any]:
    """获取迁移状态。

    Args:
        base_dir: 项目根目录

    Returns:
        dict: 迁移状态信息
    """
    if isinstance(base_dir, str):
        base_dir = Path(base_dir)
    base_dir = base_dir.resolve()

    user_data_root = base_dir / "data" / "users"
    marker_file = base_dir / "data" / "MIGRATED_TO_USER_SPACE.txt"

    status = {
        "needs_migration": not user_data_root.exists(),
        "migration_marker_exists": marker_file.exists(),
        "users": [],
    }

    status["users"] = _user_space_names(user_data_root)

    if marker_file.exists():
        try:
            status["marker_content"] = marker_file.read_text(encoding="utf-8")
        except OSError:
            pass

    return status


def _user_space_names(user_data_root: Path) -> list[str]:
    # LLM: migration status lists visible user roots without deepening status rendering.
    if not user_data_root.exists():
        return []
    return [item.name for item in user_data_root.iterdir() if item.is_dir() and not item.name.startswith(".")]
