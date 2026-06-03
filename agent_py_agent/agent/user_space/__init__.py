
from __future__ import annotations

"""用户数据隔离模块。

按 user_id 隔离数据目录，为多用户场景打基础。
"""

from .capability_requests import (
    CreateCapabilityRequest,
    OwnerCapabilityRequest,
    close_capability_request,
    create_capability_request,
    expire_capability_requests,
    list_capability_requests,
)
from .capability_resolver import (
    CapabilityResolveOptions,
    CapabilityResolveResult,
    resolve_owner_capability,
)
from .compact_injection import render_compact_injection
from .compact_layout import CompactPackagePaths, compact_package_paths, ensure_compact_package
from .context_bundle import (
    MainContextBundleRequest,
    MainContextBundleResult,
    build_main_context_bundle,
    latest_main_context_bundle_path,
)
from .home_backup import HomeBackupManifest, create_home_backup_manifest
from .home_doctor import build_home_doctor_report
from .home_index_rebuild import HomeIndexRebuildResult, rebuild_home_indexes
from .home_indexes import (
    AgentIndexRef,
    IndexRefsReport,
    RunIndexRef,
    TaskIndexRef,
    dangling_index_refs,
    latest_agent_refs,
    latest_agent_refs_report,
    latest_owner_refs,
    latest_owner_refs_report,
    latest_run_refs,
    latest_run_refs_report,
    latest_task_refs,
    latest_task_refs_report,
    register_agent_ref,
    register_owner_ref,
    register_run_ref,
    register_task_ref,
)
from .home_layout import ensure_my_agent_home, home_paths, resolve_my_agent_home
from .home_memory_notes import (
    HomeMemoryWriteResult,
    LessonNoteRequest,
    append_hot_note,
    upsert_lesson_note,
)
from .home_migration import (
    HomeMigrationAction,
    HomeMigrationResult,
    apply_home_migration,
    plan_home_migration,
)
from .home_retention import (
    OwnerRetentionPlan,
    RetentionAction,
    apply_owner_retention,
    plan_owner_retention,
)
from .identity_store import (
    ProviderIdentityLookupReport,
    ProviderIdentityRecord,
    ProviderOwnerResolutionReport,
    ensure_canonical_user_profile,
    link_provider_identity,
    lookup_provider_identity,
    lookup_provider_identity_report,
    resolve_owner_from_provider_identity,
    resolve_owner_from_provider_identity_report,
)
from .legacy_user_paths import LegacyUserPaths, get_legacy_admin_paths, get_legacy_user_paths
from .manager import UserSpaceManager
from .migration import migrate_to_user_space
from .owner_policy import (
    EffectiveOwnerPolicy,
    OwnerDiskUsage,
    OwnerPolicyBundle,
    OwnerPolicyBundleReport,
    owner_disk_usage,
    read_owner_policy_bundle,
    read_owner_policy_bundle_report,
    resolve_effective_owner_policy,
)
from .owner_resolver import (
    OwnerHomeResult,
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)
from .provider_space import ProviderSpaceIdentity, ProviderSpacePaths, ensure_provider_space
from .run_workspace import EnsureRunWorkspaceRequest, RunWorkspacePaths, ensure_run_workspace
from .skill_candidates import (
    SkillCandidate,
    SkillCandidateAppendResult,
    append_owner_skill_candidate,
)
from .task_compact_rollup import TaskCompactRollupResult, sync_task_compact_rollup
from .temporary_grants import (
    CreateTemporaryGrant,
    OwnerTemporaryGrant,
    TemporaryGrantsReport,
    create_temporary_grant,
    expire_temporary_grants,
    list_temporary_grants,
    list_temporary_grants_report,
)

__all__ = [
    "ProviderSpaceIdentity",
    "ProviderSpacePaths",
    "CompactPackagePaths",
    "CapabilityResolveOptions",
    "CapabilityResolveResult",
    "CreateCapabilityRequest",
    "CreateTemporaryGrant",
    "HomeBackupManifest",
    "HomeMigrationAction",
    "HomeMigrationResult",
    "HomeMemoryWriteResult",
    "HomeIndexRebuildResult",
    "LessonNoteRequest",
    "OwnerHomeResult",
    "OwnerCapabilityRequest",
    "OwnerDiskUsage",
    "OwnerIdentity",
    "OwnerPolicyBundle",
    "OwnerPolicyBundleReport",
    "OwnerRetentionPlan",
    "OwnerTemporaryGrant",
    "TemporaryGrantsReport",
    "EffectiveOwnerPolicy",
    "ProviderIdentityLookupReport",
    "ProviderOwnerResolutionReport",
    "ProviderIdentityRecord",
    "LegacyUserPaths",
    "UserSpaceManager",
    "EnsureRunWorkspaceRequest",
    "MainContextBundleRequest",
    "MainContextBundleResult",
    "RunWorkspacePaths",
    "AgentIndexRef",
    "IndexRefsReport",
    "RunIndexRef",
    "RetentionAction",
    "SkillCandidate",
    "SkillCandidateAppendResult",
    "TaskIndexRef",
    "TaskCompactRollupResult",
    "append_owner_skill_candidate",
    "append_hot_note",
    "apply_owner_retention",
    "apply_home_migration",
    "build_home_doctor_report",
    "build_main_context_bundle",
    "close_capability_request",
    "compact_package_paths",
    "create_capability_request",
    "create_home_backup_manifest",
    "create_temporary_grant",
    "dangling_index_refs",
    "ensure_canonical_user_profile",
    "ensure_compact_package",
    "ensure_owner_home",
    "ensure_provider_space",
    "ensure_my_agent_home",
    "ensure_run_workspace",
    "expire_capability_requests",
    "expire_temporary_grants",
    "get_legacy_admin_paths",
    "get_legacy_user_paths",
    "home_paths",
    "home_paths_with_owner",
    "latest_main_context_bundle_path",
    "latest_owner_refs",
    "latest_agent_refs",
    "latest_agent_refs_report",
    "latest_owner_refs_report",
    "latest_run_refs",
    "latest_run_refs_report",
    "latest_task_refs",
    "latest_task_refs_report",
    "link_provider_identity",
    "list_capability_requests",
    "list_temporary_grants",
    "list_temporary_grants_report",
    "lookup_provider_identity",
    "lookup_provider_identity_report",
    "migrate_to_user_space",
    "owner_identity_from_config",
    "owner_disk_usage",
    "plan_home_migration",
    "plan_owner_retention",
    "read_owner_policy_bundle",
    "read_owner_policy_bundle_report",
    "register_owner_ref",
    "register_agent_ref",
    "register_run_ref",
    "register_task_ref",
    "rebuild_home_indexes",
    "render_compact_injection",
    "resolve_effective_owner_policy",
    "resolve_owner_capability",
    "resolve_my_agent_home",
    "resolve_owner_home",
    "resolve_owner_from_provider_identity",
    "resolve_owner_from_provider_identity_report",
    "sync_task_compact_rollup",
    "upsert_lesson_note",
]
