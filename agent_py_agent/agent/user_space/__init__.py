# LLM: User-space module; keep per-user path and migration behavior stable.
# 模块用途: 管理用户隔离目录、路径推导和旧数据迁移。

from __future__ import annotations

"""用户数据隔离模块。

按 user_id 隔离数据目录，为多用户场景打基础。
"""

from .context_bundle import (
    MainContextBundleRequest,
    MainContextBundleResult,
    build_main_context_bundle,
    latest_main_context_bundle_path,
)
from .home_layout import ensure_my_agent_home, home_paths, resolve_my_agent_home
from .manager import UserSpaceManager
from .migration import migrate_to_user_space
from .paths import UserPaths, get_user_paths
from .provider_space import ProviderSpaceIdentity, ProviderSpacePaths, ensure_provider_space
from .run_workspace import EnsureRunWorkspaceRequest, RunWorkspacePaths, ensure_run_workspace

__all__ = [
    "ProviderSpaceIdentity",
    "ProviderSpacePaths",
    "UserPaths",
    "UserSpaceManager",
    "EnsureRunWorkspaceRequest",
    "MainContextBundleRequest",
    "MainContextBundleResult",
    "RunWorkspacePaths",
    "build_main_context_bundle",
    "ensure_provider_space",
    "ensure_my_agent_home",
    "ensure_run_workspace",
    "get_user_paths",
    "home_paths",
    "latest_main_context_bundle_path",
    "migrate_to_user_space",
    "resolve_my_agent_home",
]
