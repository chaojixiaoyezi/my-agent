# LLM: Legacy data/users path helpers are migration-only compatibility, not the V2 owner-home model.
# 模块用途: 保留旧 data/users 路径推导，供迁移和关闭 owner-home runtime 的兼容模式使用。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# LLM: LegacyUserPaths labels old data/users paths so new runtime code does not mistake them for owner-home facts.
# 类用途: 保存旧 data/users 布局路径，只供迁移、兼容读取和关闭 owner-home runtime 后的 fallback 使用。
@dataclass
class LegacyUserPaths:
    """Path bundle for the old data/users layout.

    New runtime code should prefer owner-home paths from `owner_resolver` and
    `runtime_paths`. This type exists so migration/fallback code can name the
    legacy layout honestly.
    """

    user_id: str
    root_dir: Path
    memory_path: Path
    subagent_workspace: Path
    gateway_workspace: Path
    sessions_dir: Path
    local_store_path: Path
    local_store_files_dir: Path
    local_store_events_path: Path


# LLM: get_legacy_user_paths is only for old data/users migration/fallback.
# 函数用途: 根据 user_id 计算旧 data/users 布局路径，不用于新 owner-home 运行事实源。
def get_legacy_user_paths(user_id: str, base_dir: Path | str) -> LegacyUserPaths:
    if isinstance(base_dir, str):
        base_dir = Path(base_dir)
    base_dir = base_dir.resolve()

    root_dir = base_dir / user_id
    return LegacyUserPaths(
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


# LLM: get_legacy_admin_paths names the old admin fallback explicitly.
# 函数用途: 返回旧 data/users/admin 路径集合，仅迁移和兼容使用。
def get_legacy_admin_paths(base_dir: Path | str) -> LegacyUserPaths:
    return get_legacy_user_paths("admin", base_dir)


__all__ = ["LegacyUserPaths", "get_legacy_admin_paths", "get_legacy_user_paths"]
