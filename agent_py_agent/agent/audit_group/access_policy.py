# LLM: 公共插件安装者和as_user都不能成为审计组授权依据。
# 模块用途: 定义审计组错误和每次调用的实时成员检查。
from __future__ import annotations

from pathlib import Path

from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import validate_local_user_id
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_admin_controls import _controls_from_policy
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 仅解析规范身份，不接受installation_owner/as_user字典或路径别名；无IO。
# 函数用途: 将调用者或来源账号转换为唯一owner身份。
def owner_identity(value):
    if isinstance(value, OwnerIdentity):
        identity = value
    elif type(value) is str and value == "local/main":
        identity = OwnerIdentity.local_main()
    elif type(value) is str and value.startswith("local/") and len(value.split("/")) == 2:
        identity = OwnerIdentity.local_user(validate_local_user_id(value.split("/")[1]))
    elif type(value) is str and value.startswith("providers/feishu/users/") and len(value.split("/")) == 4:
        identity = OwnerIdentity.provider_user("feishu", value.split("/")[3])
    else:
        raise AuditGroupError("AUDIT_GROUP_ACCESS_DENIED")
    if identity.provider == "local":
        if identity != OwnerIdentity.local_main():
            if identity.owner_kind != "user":
                raise AuditGroupError("AUDIT_GROUP_ACCESS_DENIED")
            validate_local_user_id(identity.owner_id)
    elif identity.provider != "feishu" or identity.owner_kind != "user":
        raise AuditGroupError("AUDIT_GROUP_ACCESS_DENIED")
    return identity

# LLM: 仅携带稳定机器码和安全诊断，不保存私有路径或源正文。
# 类用途: 向工具和Host返回可分辨的审计组拒绝原因。
class AuditGroupError(Exception):
    # LLM: message与code分开，不把任意底层异常正文复制到公开错误。
    # 函数用途: 构造包含HTTP状态和结构化详情的错误。
    def __init__(self, code, message="", *, status_code=403, details=None):
        self.code, self.message, self.status_code = code, message or code, status_code
        self.details = dict(details or {})
        super().__init__(self.message)


# LLM: 复用原管理员控制块解析，先nofollow读再严格校验；损坏不能降成空策略或允许执行。
# 函数用途: 读取一个规范owner的实时工具策略和停用控制。
def current_tool_policy(root, home):
    try:
        store = LocalUserStore(root)
        raw = store.read_json((*home.home_dir.relative_to(store.root).parts, "tool_policy.json")) or {}
        controls = _controls_from_policy(raw)
        disabled = raw.get("disabled_tools", [])
        if (raw and raw.get("schema_version") != "tool-policy.v1" or controls is None
                or type(disabled) is not list or len(disabled) > 2000 or any(type(name) is not str for name in disabled)):
            raise ValueError("invalid tool policy")
        return raw, controls
    except (HostAPIError, ValueError, TypeError, OSError):
        raise AuditGroupError("AUDIT_GROUP_POLICY_UNAVAILABLE", status_code=503) from None


# LLM: 每次执行现读本地账号，不能缓存成员资格。
# 函数用途: 核验真实有效owner是否属于审计组。
def require_audit_group_member(root_dir, caller_owner):
    try:
        identity = owner_identity(caller_owner)
        if identity == OwnerIdentity.local_main():
            return {"owner_id": "local/main", "status": "enabled", "in_audit_group": True}
        if identity.provider == "local":
            store = LocalUserStore(root_dir)
            user = store.get(identity.owner_id)
            if user is not None and user.status != "enabled":
                raise AuditGroupError("USER_DISABLED")
            if user is not None and user.in_audit_group is True:
                store.require_enabled(identity.owner_id, user.revision)
                return {"owner_id": f"local/{user.user_id}", "status": user.status,
                        "in_audit_group": True, "revision": user.revision}
    except HostAPIError as error:
        code = "USER_DISABLED" if error.code == "USER_DISABLED" else "AUDIT_GROUP_ACCESS_DENIED"
        raise AuditGroupError(code) from None
    except (ValueError, TypeError, AttributeError):
        raise AuditGroupError("AUDIT_GROUP_ACCESS_DENIED") from None
    raise AuditGroupError("AUDIT_GROUP_ACCESS_DENIED")


# LLM: 来源账号/维护状态和管理员控制任一停用均先于执行；复用规范路径和冷读，不创建home。
# 函数用途: 拒绝停用或未登记的本地来源账号。
def require_source_enabled(root_dir, source_owner):
    identity = owner_identity(source_owner)
    home = resolve_owner_home(Path(root_dir), identity)
    store = LocalUserStore(root_dir)
    try:
        if identity.provider == "local" and identity.owner_kind == "user":
            store.require_enabled(identity.owner_id)
        state = store.read_json((*home.home_dir.relative_to(store.root).parts, "owner_status.json"))
        if state is not None and state.get("status") != "active":
            raise AuditGroupError("ASSET_DISABLED_BY_OWNER", status_code=409)
        _policy, controls = current_tool_policy(root_dir, home)
        if controls.get("disabled", False):
            raise AuditGroupError("ASSET_DISABLED_BY_OWNER", status_code=409)
    except HostAPIError as error:
        code = "ASSET_DISABLED_BY_OWNER" if error.code == "USER_DISABLED" else "SOURCE_UNAVAILABLE"
        raise AuditGroupError(code, status_code=409 if code == "ASSET_DISABLED_BY_OWNER" else 503) from None
    return home
