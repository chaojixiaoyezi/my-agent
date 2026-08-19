# LLM: 本包门面按符号延迟加载；解析 owner home 不得初始化索引重建、保留策略、上下文 bundle 或能力申请服务。
# 模块用途: 保留用户隔离模块的公开入口，让 TUI、飞书和 Web 只为当前请求加载必要的身份与路径代码。

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "CreateCapabilityRequest": ("capability_requests", "CreateCapabilityRequest"),
    "OwnerCapabilityRequest": ("capability_requests", "OwnerCapabilityRequest"),
    "close_capability_request": ("capability_requests", "close_capability_request"),
    "create_capability_request": ("capability_requests", "create_capability_request"),
    "expire_capability_requests": ("capability_requests", "expire_capability_requests"),
    "list_capability_requests": ("capability_requests", "list_capability_requests"),
    "MainContextBundleRequest": ("context_bundle", "MainContextBundleRequest"),
    "MainContextBundleResult": ("context_bundle", "MainContextBundleResult"),
    "build_main_context_bundle": ("context_bundle", "build_main_context_bundle"),
    "latest_main_context_bundle_path": ("context_bundle", "latest_main_context_bundle_path"),
    "HomeBackupManifest": ("home_backup", "HomeBackupManifest"),
    "create_home_backup_manifest": ("home_backup", "create_home_backup_manifest"),
    "HomeIndexRebuildResult": ("home_index_rebuild", "HomeIndexRebuildResult"),
    "rebuild_home_indexes": ("home_index_rebuild", "rebuild_home_indexes"),
    "AgentIndexRef": ("home_indexes", "AgentIndexRef"),
    "IndexRefsReport": ("home_indexes", "IndexRefsReport"),
    "RunIndexRef": ("home_indexes", "RunIndexRef"),
    "TaskIndexRef": ("home_indexes", "TaskIndexRef"),
    "dangling_index_refs": ("home_indexes", "dangling_index_refs"),
    "latest_agent_refs": ("home_indexes", "latest_agent_refs"),
    "latest_agent_refs_report": ("home_indexes", "latest_agent_refs_report"),
    "latest_owner_refs": ("home_indexes", "latest_owner_refs"),
    "latest_owner_refs_report": ("home_indexes", "latest_owner_refs_report"),
    "latest_run_refs": ("home_indexes", "latest_run_refs"),
    "latest_run_refs_report": ("home_indexes", "latest_run_refs_report"),
    "latest_task_refs": ("home_indexes", "latest_task_refs"),
    "latest_task_refs_report": ("home_indexes", "latest_task_refs_report"),
    "register_agent_ref": ("home_indexes", "register_agent_ref"),
    "register_owner_ref": ("home_indexes", "register_owner_ref"),
    "register_run_ref": ("home_indexes", "register_run_ref"),
    "register_task_ref": ("home_indexes", "register_task_ref"),
    "ensure_my_agent_home": ("home_layout", "ensure_my_agent_home"),
    "home_paths": ("home_layout", "home_paths"),
    "resolve_my_agent_home": ("home_layout", "resolve_my_agent_home"),
    "ProviderIdentityLookupReport": ("identity_store", "ProviderIdentityLookupReport"),
    "ProviderIdentityRecord": ("identity_store", "ProviderIdentityRecord"),
    "ProviderOwnerResolutionReport": ("identity_store", "ProviderOwnerResolutionReport"),
    "ensure_canonical_user_profile": ("identity_store", "ensure_canonical_user_profile"),
    "link_provider_identity": ("identity_store", "link_provider_identity"),
    "lookup_provider_identity": ("identity_store", "lookup_provider_identity"),
    "lookup_provider_identity_report": ("identity_store", "lookup_provider_identity_report"),
    "resolve_owner_from_provider_identity": (
        "identity_store",
        "resolve_owner_from_provider_identity",
    ),
    "resolve_owner_from_provider_identity_report": (
        "identity_store",
        "resolve_owner_from_provider_identity_report",
    ),
    "EffectiveOwnerPolicy": ("owner_policy", "EffectiveOwnerPolicy"),
    "OwnerDiskUsage": ("owner_policy", "OwnerDiskUsage"),
    "OwnerPolicyBundle": ("owner_policy", "OwnerPolicyBundle"),
    "OwnerPolicyBundleReport": ("owner_policy", "OwnerPolicyBundleReport"),
    "owner_disk_usage": ("owner_policy", "owner_disk_usage"),
    "read_owner_policy_bundle": ("owner_policy", "read_owner_policy_bundle"),
    "read_owner_policy_bundle_report": ("owner_policy", "read_owner_policy_bundle_report"),
    "resolve_effective_owner_policy": ("owner_policy", "resolve_effective_owner_policy"),
    "OwnerHomeResult": ("owner_resolver", "OwnerHomeResult"),
    "OwnerIdentity": ("owner_resolver", "OwnerIdentity"),
    "ensure_owner_home": ("owner_resolver", "ensure_owner_home"),
    "home_paths_with_owner": ("owner_resolver", "home_paths_with_owner"),
    "owner_identity_from_config": ("owner_resolver", "owner_identity_from_config"),
    "resolve_owner_home": ("owner_resolver", "resolve_owner_home"),
    "EnsureRunWorkspaceRequest": ("run_workspace", "EnsureRunWorkspaceRequest"),
    "RunWorkspacePaths": ("run_workspace", "RunWorkspacePaths"),
    "ensure_run_workspace": ("run_workspace", "ensure_run_workspace"),
    "CreateTemporaryGrant": ("temporary_grants", "CreateTemporaryGrant"),
    "OwnerTemporaryGrant": ("temporary_grants", "OwnerTemporaryGrant"),
    "TemporaryGrantsReport": ("temporary_grants", "TemporaryGrantsReport"),
    "create_temporary_grant": ("temporary_grants", "create_temporary_grant"),
    "expire_temporary_grants": ("temporary_grants", "expire_temporary_grants"),
    "list_temporary_grants": ("temporary_grants", "list_temporary_grants"),
    "list_temporary_grants_report": ("temporary_grants", "list_temporary_grants_report"),
    "OwnerRetentionPlan": ("home_retention", "OwnerRetentionPlan"),
    "RetentionAction": ("home_retention", "RetentionAction"),
    "apply_owner_retention": ("home_retention", "apply_owner_retention"),
    "plan_owner_retention": ("home_retention", "plan_owner_retention"),
    "build_home_doctor_report": ("home_doctor", "build_home_doctor_report"),
    "OwnerMaintenanceResult": ("owner_maintenance", "OwnerMaintenanceResult"),
    "owner_maintenance_due": ("owner_maintenance", "owner_maintenance_due"),
    "run_owner_retention_if_due": ("owner_maintenance", "run_owner_retention_if_due"),
}

__all__ = list(_EXPORTS)


# LLM: 公开符号和真实兄弟模块只在首次访问时解析并缓存；未知名称不得触发全包扫描。
# 函数用途: 按调用方需要加载用户目录、身份、索引或维护能力。
def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is not None:
        module_name, attribute_name = target
        module = importlib.import_module(f".{module_name}", __name__)
        value = getattr(module, attribute_name)
        globals()[name] = value
        return value
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name == f"{__name__}.{name}":
            raise AttributeError(name) from None
        raise
    globals()[name] = module
    return module


# LLM: introspection 只列公开目录，不触发 owner 服务初始化。
# 函数用途: 为补全和调试器列出用户隔离入口。
def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
