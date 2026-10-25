# LLM: 授权仅存原激活记录，本模块不创建第二套权限账。
# 模块用途: 校验v9宿主API授权、显式UID通配与普通私有安装的受限确认绑定。
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache

from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import validate_local_user_id
from ..plugin_host.schemas import (
    canonical_json,
    closed,
    integer,
    quota_schema,
    strict_json,
    text,
    validate_object,
)
from ..plugin_manifest_host_v9 import host_capabilities


# LLM: 列表既限制长度也去重，通配符从条目schema直接拒绝。
# 函数用途: 构造有界、无重复的授权列表。
def _list_schema(item: dict, minimum: int = 0) -> dict:
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": 100, "uniqueItems": True}


# LLM: 执行范围只接受固定工具/固定资产内容，网络不能开启。
# 函数用途: 生成闭合的有限执行授权结构。
def execution_schema() -> dict:
    asset = closed({"kind": {"enum": ["script", "workflow"]}, "id": text(128, 1, r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
                    "revision": integer(2147483647, 1), "sha256": text(64, 64, r"^[0-9a-f]{64}$")})
    return closed({"tool_names": _list_schema(text(96, 1, r"^[A-Za-z0-9_]{1,96}$")),
                   "asset_refs": _list_schema(asset), "network": {"type": "boolean", "enum": [False]},
                   "max_concurrent_requests": integer(8, 1), "max_tokens_per_request": integer(1000000, 1),
                   "max_duration_seconds": integer(86400, 1), "max_tool_calls": integer(1000)})


# LLM: 三组UID允许显式星号；资源和工具仍是有限精确引用，普通用户不能使用治理或代用户范围。
# 函数用途: 返回确认预览中可选择的API授权字段。
def selection_schema() -> dict:
    ids = _list_schema(text(32, 1, r"^(\*|[a-z0-9][a-z0-9_-]{1,31})$"))
    resource = closed({"created_by_installation": {"type": "boolean"},
                       "explicit_ids": _list_schema(text(128, 1, r"^[A-Za-z0-9_.:-]+$"))})
    return closed({"approved_capabilities": _list_schema({"enum": sorted(host_capabilities())}, 1),
                   "target_user_ids": ids, "manageable_user_ids": ids, "creatable_user_ids": ids,
                   "threads": resource, "tasks": resource, "quota_ceiling": quota_schema(),
                   "execution": execution_schema(), "expires_at": {"type": ["string", "null"]}})


# LLM: UTC时间不接受无时区或偏移区；过期仍可读回历史，但不能作为有效权限。
# 函数用途: 校验可选到期时间并返回UTC秒数。
def expiry_timestamp(value) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40 or not value.endswith(("Z", "+00:00")):
        raise HostAPIError("INVALID_SCHEMA")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo != timezone.utc:
            raise ValueError("不是UTC")
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise HostAPIError("INVALID_SCHEMA") from None


# LLM: 显式星号原样保留；其余UID仍核保留账号及严格语法，不以通配免掉目标身份检查。
# 函数用途: 校验管理员选择并生成独立副本。
def validate_selection(value) -> dict:
    result = validate_object(value, selection_schema())
    for key in ("target_user_ids", "manageable_user_ids", "creatable_user_ids"):
        for user_id in result[key]:
            if user_id != "*":
                validate_local_user_id(user_id)
    expiry_timestamp(result["expires_at"])
    return result


# LLM: 校验入口不授予权限，后续仍需验证安装和原激活。
# 函数用途: 验证完整宿主授权范围。
def validate_host_grant(value):
    digest = text(64, 64, r"^[0-9a-f]{64}$")
    schema = closed({**selection_schema()["properties"], "schema": {"enum": ["plugin_host_grant.v1"]},
                     "grant_revision": integer(2147483647, 1), "installation_ref": digest,
                     "package_sha256": digest, "activation_id": digest})
    result = validate_object(value, schema)
    validate_selection({key: result[key] for key in selection_schema()["properties"]})
    return result


# LLM: 原启用器的完整确认信封在此拆分，重复承运必须完全相同；旧无API授权记录不改字节。
# 函数用途: 规范化激活内的OS权限与宿主API权限，并核对固定计划。
def normalize_activation_grant(activation) -> None:
    from ..common.strict_json import load_strict_json

    permission = load_strict_json(activation.permission_json) if activation.permission_json else None
    carried = permission.pop("host_api_grant", None) if isinstance(permission, dict) else None
    encoded = activation.host_api_grant_json
    if carried is not None:
        if encoded is not None and canonical_json(strict_json(encoded.encode())) != canonical_json(carried):
            raise ValueError("重复授权不一致")
        encoded = canonical_json(carried)
        object.__setattr__(activation, "permission_json", canonical_json(permission))
    if encoded is None:
        return
    grant = validate_host_grant(strict_json(encoded.encode()))
    if (not isinstance(permission, dict) or permission.get("mode") != "restricted"
            or grant["package_sha256"] != activation.plan.package_sha256
            or grant["activation_id"] != activation.activation_id
            or any(grant[key] != permission.get(key) for key in ("installation_ref", "package_sha256", "activation_id"))):
        raise ValueError("宿主授权与固定OS权限或原计划不一致")
    object.__setattr__(activation, "host_api_grant_json", canonical_json(grant))


# LLM: 每次读安装或准备提交都从显式v9判定，不让旧包承载新授权，也不让v9借旧兼容启动。
# 函数用途: 核对授权、manifest及原安装身份，准备时额外绑定前一提交引用。
def validate_installation_grant(entry, activation=None, *, preparing: bool = False) -> dict | None:
    current = entry.activation if activation is None else activation
    encoded = getattr(current, "host_api_grant_json", None)
    if not entry.manifest.host_api_v9:
        if encoded is not None:
            raise ValueError("旧包不能携带v9授权")
        return None
    if entry.legacy_permission_json is not None:
        raise ValueError("v9不允许兼容授权")
    if current is None:
        return None
    if encoded is None:
        raise ValueError("v9缺少固定宿主授权")
    grant = validate_host_grant(strict_json(encoded.encode()))
    if (grant["package_sha256"] != entry.package_sha256
            or grant["activation_id"] != current.activation_id
            or not set(grant["approved_capabilities"]) <= set(entry.manifest.host_api)
            or (preparing and grant["installation_ref"] != entry.installation_ref)):
        raise ValueError("宿主授权与安装不一致")
    return grant


# LLM: 仅一次受信管理请求的暂态承运，不保存审批结果，不跨线程沿用，也不授予管理资格。
# 函数用途: 返回进程内上下文槽供原启用确认器读取显式范围。
@lru_cache(maxsize=1)
def _selection_slot() -> ContextVar:
    return ContextVar("plugin_host_selection", default=None)


# LLM: 选择必须在原管理员鉴权之后使用；finally清理保证下一次请求不能继承别人的选择。
# 函数用途: 在原管理命令执行期间承运一份闭合的API授权选择。
@contextmanager
def host_grant_selection(value):
    selected = validate_selection(value) if value is not None else None
    token = _selection_slot().set(canonical_json(selected) if selected is not None else None)
    try:
        yield
    finally:
        _selection_slot().reset(token)


# LLM: 默认只供完整确认预览，不隐式通过确认；跨用户名单、工具和资产都为空。
# 函数用途: 为未显式选范围的v9创建最小权限预览。
def default_selection(capabilities: tuple[str, ...]) -> dict:
    return {"approved_capabilities": list(capabilities), "target_user_ids": [], "manageable_user_ids": [],
            "creatable_user_ids": [], "threads": {"created_by_installation": True, "explicit_ids": []},
            "tasks": {"created_by_installation": True, "explicit_ids": []},
            "quota_ceiling": {"max_active_agents": 1, "max_subagents": 0, "max_depth": 0, "max_disk_mb": 64},
            "execution": {"tool_names": [], "asset_refs": [], "network": False, "max_concurrent_requests": 1,
                          "max_tokens_per_request": 4096, "max_duration_seconds": 60, "max_tool_calls": 0},
            "expires_at": None}


# LLM: 原确认流程可供普通私有安装使用，但必须restricted、无治理与代用户能力；返回值仍不是批准票据。
# 函数用途: 将v9有限范围附到原OS权限确认，让原确认码覆盖原操作及全部API范围。
def prepare_host_confirmation(context, entry, plan, os_grant: dict) -> dict:
    from ..user_space.owner_resolver import OwnerIdentity

    ordinary = context.owner.identity != OwnerIdentity.local_main()
    if os_grant["mode"] != "restricted":
        raise ValueError("v9只允许受限启用")
    if ordinary and (context.owner.identity.owner_kind != "user" or entry.installation_scope != "private"):
        raise ValueError("普通用户只能受限启用自己的私有插件")
    carried = _selection_slot().get()
    chosen = context.arguments.get("host_api_selection")
    if chosen is None:
        chosen = strict_json(carried.encode()) if carried is not None else default_selection(entry.manifest.host_api)
    selection = validate_selection(chosen)
    if ordinary and ({"users.manage", "act_as_user"} & set(selection["approved_capabilities"])
                     or any(selection[key] for key in ("target_user_ids", "manageable_user_ids", "creatable_user_ids"))):
        raise ValueError("普通用户私有插件不得申请跨用户或账号治理权限")
    if not set(selection["approved_capabilities"]) <= set(entry.manifest.host_api):
        raise ValueError("批准能力不得超过清单申请")
    grant = validate_host_grant({**selection, "schema": "plugin_host_grant.v1",
        "grant_revision": plan.installation_revision + 1, "installation_ref": entry.installation_ref,
        "package_sha256": entry.package_sha256, "activation_id": os_grant["activation_id"]})
    return {**os_grant, "host_api_grant": grant}
