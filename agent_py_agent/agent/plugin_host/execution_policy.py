# LLM: 插件执行权限只能收窄当前普通用户权限，准确工具和资产版本不能退化为通配符。
# 模块用途: 按本地或provider原空间构建插件执行白名单；不物化或复制owner策略。
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..tooling._filesystem_read import ReadFileTool
from ..tooling._filesystem_write import WriteFileTool
from ..tooling.models import ToolRuntimeSnapshot
from ..user_space.owner_quota import (
    LocalUserQuotaLimits,
    OwnerQuotaUnavailable,
    read_local_user_quota,
)
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .errors import HostAPIError


# LLM: 资产许可绑定种类、稳定ID、修订和完整摘要，名字相同不代表授权仍有效。
# 类用途: 标识一个准确获批执行的脚本或工作流版本。
@dataclass(frozen=True)
class AssetGrant:
    kind: str
    asset_id: str
    revision: int
    sha256: str

    # LLM: 资产引用只接受固定种类、正修订和完整hash，不接受路径或通配符。
    # 函数用途: 在构造授权投影时拒绝畸形资产许可。
    def __post_init__(self):
        if self.kind not in {"script", "workflow"}:
            raise HostAPIError("INVALID_SCHEMA")
        if type(self.asset_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.asset_id) is None:
            raise HostAPIError("INVALID_SCHEMA")
        if type(self.revision) is not int or self.revision < 1:
            raise HostAPIError("INVALID_SCHEMA")
        if type(self.sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", self.sha256) is None:
            raise HostAPIError("INVALID_SCHEMA")


# LLM: 空工具集合是默认值，聊天不能继承安装owner的完整工具目录。
# 类用途: 保存执行工具和资产的不可变许可集合。
@dataclass(frozen=True)
class ExecutionAllowlist:
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_assets: tuple[AssetGrant, ...] = ()

    # LLM: 固定集合不得接受可变容器、别名清洗或通配符，防止签发后权限原地扩大。
    # 函数用途: 校验有界工具和资产授权集合。
    def __post_init__(self):
        if type(self.allowed_tools) is not frozenset or len(self.allowed_tools) > 256:
            raise HostAPIError("INVALID_SCHEMA")
        if any(type(name) is not str or re.fullmatch(r"[A-Za-z0-9_]{1,96}", name) is None for name in self.allowed_tools):
            raise HostAPIError("INVALID_SCHEMA")
        if type(self.allowed_assets) is not tuple or len(self.allowed_assets) > 256:
            raise HostAPIError("INVALID_SCHEMA")
        if any(type(asset) is not AssetGrant for asset in self.allowed_assets):
            raise HostAPIError("INVALID_SCHEMA")


# LLM: 原快照不变，输出只含能在普通用户环境内执行的准确获批工具。
# 函数用途: 构造代用户执行的受限工具快照。
def build_restricted_snapshot(snapshot, policy):
    approved = {"read_file": ReadFileTool, "write_file": WriteFileTool}
    parent_allowed = snapshot.available_tool_names if snapshot.allowed_tools is None else snapshot.allowed_tools
    runtimes = tuple(runtime for runtime in snapshot.runtimes
                     if runtime.model_spec.name in policy.allowed_tools
                     and runtime.model_spec.name in parent_allowed
                     and type(runtime.handler) is approved.get(runtime.model_spec.name)
                     and runtime.runtime_policy == runtime.handler.runtime_policy
                     and runtime.model_spec.schema_hash == runtime.handler.model_spec.schema_hash
                     and runtime.availability.available and runtime.exposure.model_visible)
    names = frozenset(runtime.model_spec.name for runtime in runtimes)
    return ToolRuntimeSnapshot(snapshot.run_id, runtimes, names, (), allowed_tools=names, owner_type="subagent")


# LLM: 版本/hash必须全部命中，不能把同名新版本代替旧许可。
# 函数用途: 核验一份待执行资产的准确授权。
def require_asset(policy, asset):
    if type(asset) is not AssetGrant or asset not in policy.allowed_assets:
        raise HostAPIError("ASSET_DENIED", 403)
    return asset


# LLM: 原始allowlist与当前owner策略相交，网络和模型档案默认没有许可；不保存任何凭据。
# 类用途: 固定执行根、有限预算和可撤销策略摘要。
@dataclass(frozen=True)
class RestrictedExecutionPolicy:
    allowlist: ExecutionAllowlist
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    quota: LocalUserQuotaLimits
    quota_revision: int
    policy_digest: str
    network_destinations: tuple[str, ...] = field(default=(), init=False)
    model_profiles: tuple[str, ...] = field(default=(), init=False)

    # LLM: 工具集合只读已完成交集的冻结许可。
    # 函数用途: 暴露最终可注册工具集合。
    @property
    def allowed_tools(self):
        return self.allowlist.allowed_tools

    # LLM: 资产仍逐项绑定固定内容，不能据名称自动更新。
    # 函数用途: 暴露准确资产集合。
    @property
    def allowed_assets(self):
        return self.allowlist.allowed_assets


# LLM: 策略只读原owner路径，缺失/损坏失败关闭；provider不借local同名策略恢复权限。
# 函数用途: 冷读目标用户原策略并校验schema及列表类型。
def _read_policy_documents(store, uid):
    home = _policy_home(store, uid)
    parts = home.home_dir.relative_to(store.root).parts
    documents = {}
    for name, schema in (("permissions", "permissions.v1"), ("tool_policy", "tool-policy.v1"),
                         ("skill_policy", "skill-policy.v1")):
        value = store.read_json((*parts, name + ".json"))
        if value is None or value.get("schema_version") != schema:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        documents[name] = value
    controls = documents["tool_policy"].get("admin_controls", {})
    if type(controls) is not dict or type(controls.get("disabled", False)) is not bool:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    if controls.get("disabled"):
        raise HostAPIError("USER_DISABLED", 403)
    for name, key in (("tool_policy", "disabled_tools"), ("skill_policy", "disabled_skills")):
        value = documents[name].get(key)
        if type(value) is not list or len(value) > 1000 or any(type(item) is not str for item in value):
            raise HostAPIError("STORE_UNAVAILABLE", 503)
    permissions = documents["permissions"]
    filesystem, network = permissions.get("filesystem"), permissions.get("network")
    if type(filesystem) is not dict or filesystem.get("access_mode") not in {"workspace-write", "read-only", "full-access"}:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    if type(network) is not dict or type(network.get("enabled")) is not bool:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    return documents


# LLM: local仍要求原严格quota；provider容原v1/v2及缺省修订，所有权限只收窄且不创建目录。
# 函数用途: 构造目标账号和安装grant的严格权限交集。
def build_execution_policy(store, uid, grant):
    home = _policy_home(store, uid)
    parts = home.home_dir.relative_to(store.root).parts
    try:
        quota, revision = _policy_quota(store, home, grant)
        identities = [store.directory_identity((*parts, name)) for name in ("workspace", "tasks")]
    except (OwnerQuotaUnavailable, OSError, ValueError) as exc:
        raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
    documents = _read_policy_documents(store, uid)
    ceilings = grant.quota_ceiling.to_dict()
    limits = LocalUserQuotaLimits.parse({key: min(value, ceilings[key]) for key, value in quota.to_dict().items()})
    tools = grant.execution_policy.allowed_tools - set(documents["tool_policy"]["disabled_tools"])
    roots = (home.workspace_dir, home.tasks_dir)
    writable = documents["permissions"]["filesystem"]["access_mode"] != "read-only"
    if not writable:
        tools = tools - {"write_file"}
    encoded = json.dumps([documents, quota.to_dict(), revision, identities], sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    allowlist = ExecutionAllowlist(frozenset(tools), grant.execution_policy.allowed_assets)
    return RestrictedExecutionPolicy(allowlist, roots, roots if writable else (), limits, revision, digest)


# LLM: 兼容原UID调用方及新OwnerIdentity，不接受group；provider目录由原resolver与冷状态门共同核验。
# 函数用途: 为权限文档取得准确原owner，不猜或迁移目录。
def _policy_home(store, target):
    if isinstance(target, str):
        return store.owner_home(target)
    if not isinstance(target, OwnerIdentity):
        raise HostAPIError("TARGET_DENIED", 403)
    if target == OwnerIdentity.local_main():
        return resolve_owner_home(store.root, target)
    if target.owner_kind != "user":
        raise HostAPIError("TARGET_DENIED", 403)
    if target.provider == "local":
        return store.owner_home(target.owner_id)
    from .owner_context import owner_state
    return owner_state(store, target)[0]


# LLM: 原provider配额0磁盘代表不限，但插件仍受grant有限上限；未知版本/负数/损坏不回退默认。
# 函数用途: 从同一quota文件取得可相交的预算与修订，不种新文件。
def _policy_quota(store, home, grant):
    if home.identity.provider == "local" and home.identity.owner_kind == "user":
        return read_local_user_quota(store.root, home.home_dir)
    value = store.read_json((*home.home_dir.relative_to(store.root).parts, "quota.json"))
    if value is None or value.get("schema_version") not in {"quota.v1", "quota.v2"}:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    revision = value.get("revision", 1)
    if type(revision) is not int or revision < 1:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    limits = {key: value.get(key, ceiling) for key, ceiling in grant.quota_ceiling.to_dict().items()}
    if type(limits["max_disk_mb"]) is int and limits["max_disk_mb"] == 0:
        limits["max_disk_mb"] = grant.quota_ceiling.max_disk_mb
    return LocalUserQuotaLimits.parse(limits), revision
