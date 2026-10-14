# LLM: Owner 权限只读原策略事实；存在但坏/不可读权限不得回退成网络开启，缺失仍沿原初始化合同，不自动修复。
# 模块用途: 合并用户、父级与临时授权策略，保留读取诊断并让失效网络授权安全关闭。
from __future__ import annotations

import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..common.json_io import (
    JsonObjectReadReport,
    locked_json_path,
    read_json_object_report,
    write_private_json_file_atomic_unlocked,
)
from ..common.value_parsing import non_negative_int, positive_int
from .home_layout import MyAgentHomePaths
from .owner_quota import owner_logical_usage_bytes
from .temporary_grants import list_temporary_grants

_NETWORK_TOOL_NAMES = frozenset({"web_search", "web_fetch", "http_request"})


@dataclass(frozen=True)
class OwnerPolicyBundle:
    permissions: dict[str, Any]
    quota: dict[str, Any]
    retention: dict[str, Any]
    memory_policy: dict[str, Any]
    skill_policy: dict[str, Any]
    tool_policy: dict[str, Any]


@dataclass(frozen=True)
class OwnerPolicyBundleReport:
    bundle: OwnerPolicyBundle
    load_errors: tuple[dict[str, object], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OwnerDiskUsage:
    total_bytes: int
    by_root: dict[str, int]


@dataclass(frozen=True)
class EffectiveOwnerPolicy:
    owner_id: str
    owner_home: str
    filesystem_access_mode: str
    dangerous_paths: tuple[str, ...]
    shell_access_mode: str
    network_enabled: bool
    max_active_agents: int
    max_subagents: int
    max_depth: int
    max_disk_mb: int
    disabled_tools: tuple[str, ...]
    memory_enabled: bool
    skills_enabled: bool
    enabled_skill_sources: tuple[str, ...]
    enabled_shared_skills: tuple[str, ...]
    disabled_skills: tuple[str, ...]
    load_errors: tuple[dict[str, object], ...] = field(default_factory=tuple)
    active_grants: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "effective-owner-policy.v1",
            "owner_id": self.owner_id,
            "owner_home": self.owner_home,
            "filesystem": {
                "access_mode": self.filesystem_access_mode,
                "dangerous_paths": list(self.dangerous_paths),
            },
            "shell": {"access_mode": self.shell_access_mode},
            "network": {"enabled": self.network_enabled},
            "quota": {
                "max_active_agents": self.max_active_agents,
                "max_subagents": self.max_subagents,
                "max_depth": self.max_depth,
                "max_disk_mb": self.max_disk_mb,
            },
            "tools": {
                "disabled_tools": list(self.disabled_tools),
            },
            "memory": {
                "enabled": self.memory_enabled,
            },
            "skills": {
                "enabled": self.skills_enabled,
                "enabled_sources": list(self.enabled_skill_sources),
                "enabled_shared_skills": list(self.enabled_shared_skills),
                "disabled_skills": list(self.disabled_skills),
            },
            "active_grants": list(self.active_grants),
        }
        if self.load_errors:
            payload["load_errors"] = list(self.load_errors)
        return payload


def read_owner_policy_bundle(home: MyAgentHomePaths) -> OwnerPolicyBundle:
    return read_owner_policy_bundle_report(home).bundle


# LLM: 各原文件仍只有一份读取报告；权限文件额外验证存在性及网络 schema，坏原件保留、不写回默认值。
# 函数用途: 读取 owner 的原策略与错误，让调用方明确区分初次缺失和已存授权损坏。
def read_owner_policy_bundle_report(home: MyAgentHomePaths) -> OwnerPolicyBundleReport:
    permissions = _read_permissions_report(home.owner_permissions_json)
    quota = read_json_object_report(home.owner_quota_json, context="owner_policy.quota")
    retention = read_json_object_report(home.owner_retention_json, context="owner_policy.retention")
    memory_policy = read_json_object_report(home.owner_memory_policy_json, context="owner_policy.memory_policy")
    skill_policy = read_json_object_report(home.owner_skill_policy_json, context="owner_policy.skill_policy")
    tool_policy = read_json_object_report(home.owner_tool_policy_json, context="owner_policy.tool_policy")
    return OwnerPolicyBundleReport(
        bundle=OwnerPolicyBundle(
            permissions=permissions.payload,
            quota=quota.payload,
            retention=retention.payload,
            memory_policy=memory_policy.payload,
            skill_policy=skill_policy.payload,
            tool_policy=tool_policy.payload,
        ),
        load_errors=tuple(
            error
            for error in (
                permissions.load_error,
                quota.load_error,
                retention.load_error,
                memory_policy.load_error,
                skill_policy.load_error,
                tool_policy.load_error,
            )
            if error is not None
        ),
    )


# LLM: 权限读取诊断是安全输入；有权限文件错误时网络关闭并向子孙封顶，不能借默认 True 补出授权。
# 函数用途: 组合原 owner 与父级上界，保留错误供宿主修复，不替用户改坏策略。
def resolve_effective_owner_policy(
    home: MyAgentHomePaths,
    *,
    parent_policy: EffectiveOwnerPolicy | None = None,
) -> EffectiveOwnerPolicy:
    report = read_owner_policy_bundle_report(home)
    bundle = report.bundle
    permissions = bundle.permissions
    quota = bundle.quota
    tool_policy = bundle.tool_policy
    memory_policy = bundle.memory_policy
    skill_policy = bundle.skill_policy

    filesystem = _dict_value(permissions.get("filesystem"))
    shell = _dict_value(permissions.get("shell"))
    permissions_available = not any(error.get("context") == "owner_policy.permissions" for error in report.load_errors)
    network_enabled = permissions_available and _effective_network_enabled(permissions, parent_policy)
    disabled_tools = _effective_disabled_tools(tool_policy, network_enabled, parent_policy)
    memory_enabled = _effective_memory_enabled(memory_policy, parent_policy)
    skills_enabled = _effective_skills_enabled(skill_policy, parent_policy)

    return EffectiveOwnerPolicy(
        owner_id=str(getattr(home, "owner_id", "") or "local:main:main"),
        owner_home=str(getattr(home, "owner_home_dir", "") or home.root),
        filesystem_access_mode=_child_capped_access_mode(
            str(filesystem.get("access_mode") or "workspace-write"),
            parent_policy.filesystem_access_mode if parent_policy else "",
        ),
        dangerous_paths=_string_tuple(filesystem.get("dangerous_paths")),
        shell_access_mode=_child_capped_access_mode(
            str(shell.get("access_mode") or filesystem.get("access_mode") or "workspace-write"),
            parent_policy.shell_access_mode if parent_policy else "",
        ),
        network_enabled=network_enabled if parent_policy is None else bool(network_enabled and parent_policy.network_enabled),
        max_active_agents=_child_capped_limit(
            _positive_int(quota.get("max_active_agents"), 1000),
            parent_policy.max_active_agents if parent_policy else 0,
        ),
        max_subagents=_child_capped_limit(
            _positive_int(quota.get("max_subagents"), 50),
            parent_policy.max_subagents if parent_policy else 0,
        ),
        max_depth=_child_capped_limit(
            _positive_int(quota.get("max_depth"), 4),
            parent_policy.max_depth if parent_policy else 0,
        ),
        max_disk_mb=_child_capped_limit(
            _non_negative_int(quota.get("max_disk_mb"), 0),
            parent_policy.max_disk_mb if parent_policy else 0,
            zero_is_unlimited=True,
        ),
        disabled_tools=tuple(sorted(disabled_tools)),
        memory_enabled=memory_enabled,
        skills_enabled=skills_enabled,
        enabled_skill_sources=_string_tuple(skill_policy.get("enabled_sources")),
        enabled_shared_skills=_child_capped_allowlist(
            _string_tuple(skill_policy.get("enabled_shared_skills")),
            parent_policy.enabled_shared_skills if parent_policy else (),
        ),
        disabled_skills=_string_tuple(skill_policy.get("disabled_skills")),
        load_errors=report.load_errors,
        active_grants=tuple(_grant_payload(grant) for grant in list_temporary_grants(home, status="active")),
    )


# LLM: 调用方已核权限文件 schema/读取可用性；真缺失仍是原 bootstrap 默认，已存网络只接受严格布尔。
# 函数用途: 计算当前网络开关并按原父级上界封顶。
def _effective_network_enabled(
    permissions: dict[str, Any],
    parent_policy: EffectiveOwnerPolicy | None,
) -> bool:
    network = _dict_value(permissions.get("network"))
    enabled = bool(network.get("enabled", True))
    return enabled if parent_policy is None else bool(enabled and parent_policy.network_enabled)


# LLM: lstat 区分真正不存在与坏链接/不可读；原 JSON 报告保留原因字段并明确权限失败码，禁止空 payload 补授权或自动重试。
# 函数用途: 只读检查原 permissions 文件及 network 布尔合同，不初始化或覆盖坏文件。
def _read_permissions_report(path: Path) -> JsonObjectReadReport:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return JsonObjectReadReport({})
    except OSError:
        return JsonObjectReadReport({}, _permissions_error(path, "OWNER_PERMISSIONS_UNAVAILABLE"))
    if not stat.S_ISREG(status.st_mode):
        return JsonObjectReadReport({}, _permissions_error(path, "OWNER_PERMISSIONS_INVALID"))
    report = read_json_object_report(path, context="owner_policy.permissions")
    if report.load_error is not None:
        invalid = report.load_error.get("error_type") in {"JSONDecodeError", "UnicodeDecodeError", "ValueError"}
        code = "OWNER_PERMISSIONS_INVALID" if invalid else "OWNER_PERMISSIONS_UNAVAILABLE"
        return JsonObjectReadReport(report.payload, {**report.load_error, **_permissions_error(path, code)})
    payload = report.payload
    network = payload.get("network")
    if (payload.get("schema_version") != "permissions.v1" or not isinstance(network, dict)
            or type(network.get("enabled")) is not bool):
        return JsonObjectReadReport(payload, _permissions_error(path, "OWNER_PERMISSIONS_INVALID"))
    return report


# LLM: 诊断只含固定 code/context 与原策略地址，不包含文件正文、模型描述或凭据。
# 函数用途: 为宿主显示已存权限不可使用的原因，提醒修复而不是自动重试打开网络。
def _permissions_error(path: Path, code: str) -> dict[str, object]:
    return {"context": "owner_policy.permissions", "error_code": code, "path": str(path),
            "recoverable": False, "detail": "用户权限策略不可使用，网络权限已关闭；请修复原策略文件。"}


def effective_memory_enabled(memory_policy: dict[str, Any]) -> bool:
    """Memory 总闸判定（唯一 authority；resolve 与 owner_wake_discovery 等全部消费点共用）。

    文件缺失/坏 JSON（read_json_object_report 报 load_error 但给空 payload）视为开启（老 owner
    兼容、发现层不因解析失败误杀）——只有显式 enabled=false 才短路。"""
    return bool(_dict_value(memory_policy).get("enabled", True))


def _effective_memory_enabled(
    memory_policy: dict[str, Any],
    parent_policy: EffectiveOwnerPolicy | None,
) -> bool:
    """Memory 总闸：子代理 and 继承父开关（判定本体见 effective_memory_enabled）。"""
    enabled = effective_memory_enabled(memory_policy)
    return enabled if parent_policy is None else bool(enabled and parent_policy.memory_enabled)


def _effective_skills_enabled(
    skill_policy: dict[str, Any],
    parent_policy: EffectiveOwnerPolicy | None,
) -> bool:
    """Skill 总闸：文件缺失视为开启（老 owner 兼容），子代理 and 继承父开关。"""
    enabled = bool(_dict_value(skill_policy).get("enabled", True))
    return enabled if parent_policy is None else bool(enabled and parent_policy.skills_enabled)


def _effective_disabled_tools(
    tool_policy: dict[str, Any],
    network_enabled: bool,
    parent_policy: EffectiveOwnerPolicy | None,
) -> set[str]:
    disabled_tools = set(_string_tuple(tool_policy.get("disabled_tools")))
    if not network_enabled:
        disabled_tools.update(_NETWORK_TOOL_NAMES)
    if parent_policy:
        disabled_tools.update(parent_policy.disabled_tools)
    return disabled_tools


def owner_disk_usage(home: MyAgentHomePaths) -> OwnerDiskUsage:
    owner_root = home.owner_home_dir
    by_root: dict[str, int] = {}
    try:
        entries = tuple(owner_root.iterdir())
    except OSError:
        entries = ()
    for path in entries:
        if path.name == ".owner-quota.lock" or path.is_symlink():
            continue
        try:
            used = owner_logical_usage_bytes(path) if path.is_dir() else path.stat().st_size
        except (OSError, RuntimeError):
            used = 0
        by_root[str(path)] = max(0, int(used))
    return OwnerDiskUsage(total_bytes=sum(by_root.values()), by_root=by_root)


# LLM: 学习排除名单是 owner 级结构化数据，唯一权威位置是 memory_policy.json 的
#   learning_exclusions 列表（一个概念一个权威位置）：curator 读消息前按 channel_conversation_id
#   跳过名单会话；迁移工具只追加、不覆盖。文件缺失=没有排除；文件存在但读不动时返回读取错误，
#   由调用方决定 fail 行为（curator 侧 fail-closed：本轮不学任何会话）。
# 类用途: 保存一条学习排除会话的 channel_conversation_id 与出处。
@dataclass(frozen=True)
class LearningExclusionEntry:
    channel_conversation_id: str
    reason: str = ""
    added_at: str = ""
    added_by: str = ""

    # 函数用途: 把一条排除条目转成 JSON 可序列化对象。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: 解析只认结构化字段；任何一条非法都整体作废（宁可 fail-closed 也不静默漏掉一条排除）。
# 函数用途: 读取 owner 的学习排除会话名单，返回 (条目, 读取错误)。
def read_learning_exclusions(
    home: MyAgentHomePaths,
) -> tuple[tuple[LearningExclusionEntry, ...], dict[str, object] | None]:
    report = read_json_object_report(
        home.owner_memory_policy_json,
        context="owner_policy.memory_policy",
    )
    if report.load_error is not None:
        return (), report.load_error
    raw = report.payload.get("learning_exclusions")
    if raw is None:
        return (), None
    if not isinstance(raw, list):
        return (), _learning_exclusions_invalid("learning_exclusions must be a list")
    entries: list[LearningExclusionEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            return (), _learning_exclusions_invalid("entry must be an object")
        conversation_id = str(item.get("channel_conversation_id") or "").strip()
        if not conversation_id:
            return (), _learning_exclusions_invalid("channel_conversation_id is required")
        entries.append(
            LearningExclusionEntry(
                channel_conversation_id=conversation_id,
                reason=str(item.get("reason") or "").strip(),
                added_at=str(item.get("added_at") or "").strip(),
                added_by=str(item.get("added_by") or "").strip(),
            )
        )
    return tuple(entries), None


# LLM: 名单修订号与名单同文件持久化：每次追加有新增就 +1，供晋升提交前记录「判定基于哪一版
#   名单」；缺失或非法按 0 处理（旧文件没有该字段）。
# 函数用途: 读取学习排除名单的修订号。
def read_learning_exclusions_revision(home: MyAgentHomePaths) -> int:
    report = read_json_object_report(
        home.owner_memory_policy_json,
        context="owner_policy.memory_policy",
    )
    if report.load_error is not None:
        return 0
    return _normalized_revision(report.payload)


# 函数用途: 解析 memory_policy 里的名单修订号，缺失/非法按 0。
def _normalized_revision(payload: dict[str, object]) -> int:
    try:
        return max(0, int(payload.get("learning_exclusions_revision") or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 普通入口获取唯一 policy 锁；迁移已持该锁时走锁内版本，避免不可重入 flock 自死锁。
# 函数用途: 持 policy 锁幂等追加学习排除会话，返回新增条数。
def append_learning_exclusions(
    home: MyAgentHomePaths,
    entries: tuple[LearningExclusionEntry, ...],
) -> int:
    with locked_json_path(home.owner_memory_policy_json):
        return append_learning_exclusions_locked(home, entries)


# LLM: 调用方必须持 home 的 policy 锁；保留未知键与原名单，坏内容拒绝写入，新增才推进修订号。
# 函数用途: 在迁移外层 policy 临界区中原子追加排除名单，不重复取锁。
def append_learning_exclusions_locked(
    home: MyAgentHomePaths,
    entries: tuple[LearningExclusionEntry, ...],
) -> int:
    incoming = [item for item in entries if item.channel_conversation_id]
    if not incoming:
        return 0
    path = home.owner_memory_policy_json
    report = read_json_object_report(path, context="owner_policy.memory_policy")
    if report.load_error is not None:
        raise ValueError("memory policy is unreadable; refusing to append learning exclusions")
    payload = report.payload
    existing = _normalized_exclusions(payload)
    seen = {str(item.get("channel_conversation_id") or "").strip()
            for item in existing if isinstance(item, dict)}
    added = _extend_learning_exclusions(existing, incoming, seen)
    if added:
        payload["learning_exclusions"] = existing
        payload["learning_exclusions_revision"] = _normalized_revision(payload) + 1
        write_private_json_file_atomic_unlocked(path, payload)
    return added


# LLM: 名单字段只接受缺失（空表）或列表；其它类型直接拒绝，不静默覆盖已有条目。
# 函数用途: 读取并校验 memory_policy 里的 learning_exclusions 列表。
def _normalized_exclusions(payload: dict[str, object]) -> list[object]:
    raw = payload.get("learning_exclusions")
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    raise ValueError("learning_exclusions must be a list")


# LLM: 名单只追加不覆盖：按 channel_conversation_id 去重，重复条目跳过，写回由调用方在锁内完成。
# 函数用途: 把新名单条目追加到现有列表，返回实际新增条数。
def _extend_learning_exclusions(
    existing: list[object],
    incoming: list[LearningExclusionEntry],
    seen: set[str],
) -> int:
    added = 0
    for item in incoming:
        if item.channel_conversation_id in seen:
            continue
        seen.add(item.channel_conversation_id)
        existing.append(item.to_dict())
        added += 1
    return added


# LLM: 结构化错误只给稳定错误码和短说明，不夹带文件正文。
# 函数用途: 生成学习排除名单的解析错误。
def _learning_exclusions_invalid(detail: str) -> dict[str, object]:
    return {
        "context": "owner_policy.memory_policy.learning_exclusions",
        "error_code": "LEARNING_EXCLUSIONS_INVALID",
        "detail": detail,
    }


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raw = str(value or "").strip()
    return (raw,) if raw else ()


def _positive_int(value: object, default: int) -> int:
    return positive_int(value, default=default) or default


def _non_negative_int(value: object, default: int) -> int:
    return non_negative_int(value, default=default)


# LLM: Most limits use zero as an ordinary lower bound, while disk quota uses zero as unlimited;
# callers must opt into that semantic so child inheritance cannot accidentally widen a parent cap.
# 函数用途: 合并当前 owner 与父 owner 的数字上限；磁盘模式下 0 表示不限制。
def _child_capped_limit(
    value: int,
    parent: int,
    *,
    zero_is_unlimited: bool = False,
) -> int:
    if parent <= 0:
        return value
    if zero_is_unlimited and value <= 0:
        return parent
    return min(value, parent)


def _child_capped_allowlist(value: tuple[str, ...], parent: tuple[str, ...]) -> tuple[str, ...]:
    if not parent:
        return value
    if not value:
        return parent
    allowed = set(parent)
    return tuple(item for item in value if item in allowed)


def _child_capped_access_mode(value: str, parent: str) -> str:
    ranks = {"restricted": 0, "workspace-write": 1, "full-access": 2}
    normalized = value if value in ranks else "workspace-write"
    parent_normalized = parent if parent in ranks else ""
    if not parent_normalized:
        return normalized
    if ranks[normalized] > ranks[parent_normalized]:
        return parent_normalized
    if parent_normalized == "full-access" and normalized == "full-access":
        return "workspace-write"
    return normalized


def _grant_payload(grant) -> dict[str, str]:
    return {
        "grant_id": str(grant.grant_id),
        "granted_to": str(grant.granted_to),
        "capability": str(grant.capability),
        "path_prefix": str(grant.path_prefix),
        "expires_at": str(grant.expires_at),
    }


__all__ = [
    "EffectiveOwnerPolicy",
    "LearningExclusionEntry",
    "OwnerDiskUsage",
    "OwnerPolicyBundle",
    "OwnerPolicyBundleReport",
    "append_learning_exclusions",
    "owner_disk_usage",
    "read_learning_exclusions",
    "read_owner_policy_bundle",
    "read_owner_policy_bundle_report",
    "resolve_effective_owner_policy",
]
