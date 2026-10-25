# LLM: 插件目标身份必须经宿主来源、账号状态与固定grant核验，不能用JSON声明权限。
# 模块用途: 按鲜活宿主授权解析普通用户；显式UID通配不免除账号状态或执行策略检查。
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

from ..auth.models import Role
from ..user_space.owner_quota import LocalUserQuotaLimits
from ..user_space.owner_resolver import OwnerIdentity
from .errors import HostAPIError
from .execution_policy import (
    AssetGrant,
    ExecutionAllowlist,
    RestrictedExecutionPolicy,
    build_execution_policy,
)


# LLM: 固定字段不能通过构造参数覆盖，不调用终端角色推断或AuthManager。
# 类用途: 为代用户访问提供不可变的普通用户权限上限。
@dataclass(frozen=True)
class RestrictedPermission:
    role: Role = field(default=Role.USER, init=False)
    is_admin: bool = field(default=False, init=False)
    can_access_all_users: bool = field(default=False, init=False)
    access_mode: str = field(default="workspace_only", init=False)


# LLM: 仅验证选择器，不读取磁盘或修改原输入。
# 函数用途: 在所有身份与文件操作前检查本地普通用户编号。
def validate_local_user_id(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", value) is None:
        raise HostAPIError("INVALID_SCHEMA")
    if value in {"main", "admin", "root", "system", "default", "owner", "local"}:
        raise HostAPIError("INVALID_SCHEMA")
    return value


# LLM: 保留显式星号，其他不支持的UID只剔除，不清洗成另一账号；实际目标仍逐次验证。
# 函数用途: 避免一项不可执行UID污染其他合法目标的授权。
def supported_user_ids(values):
    supported = set()
    for value in values:
        if value == "*":
            supported.add(value)
            continue
        try:
            supported.add(validate_local_user_id(value))
        except HostAPIError:
            continue
    return frozenset(supported)


# LLM: 无参数固定权限入口，不继承全局认证开关或父级权限。
# 函数用途: 创建插件代用户执行使用的普通用户权限。
def restricted_permission() -> RestrictedPermission:
    return RestrictedPermission()


# LLM: 旧池不能把已登记账号带回main配置克隆路径；未登记的原owner保持旧合同。
# 函数用途: 在原池命中及构造前冷读账号闸门，强制已登记账号改走本域受限工厂。
def require_unmanaged_pool_owner(config, owner):
    if owner.provider != "local" or owner.owner_kind != "user" or owner.owner_id == "main":
        return
    root = getattr(config, "my_agent_home", None)
    if not root:
        return
    try:
        uid = validate_local_user_id(owner.owner_id)
    except HostAPIError:
        return  # 旧渠道的非插件UID不自动迁入本注册表。
    from ..user_space.local_user_store import LocalUserStore

    store = LocalUserStore(root)
    if store.get(uid) is not None:
        store.require_enabled(uid)
        raise HostAPIError("HOST_NOT_READY", 503)


# LLM: A侧核验后投影的原来源，不含token；本数据本身不是授权凭证。
# 类用途: 固定安装、包、激活和grant修订。
@dataclass(frozen=True)
class PluginSourceBinding:
    installation_ref: str
    package_sha256: str
    activation_id: str
    grant_revision: int
    owner: OwnerIdentity = field(default_factory=OwnerIdentity.local_main)


# LLM: 管理、创建、代执行三组许可彼此独立；创建不会自动加入代执行范围。
# 类用途: 接收宿主权威层的冻结用户授权投影。
@dataclass(frozen=True)
class LocalUserGrant:
    source_binding: PluginSourceBinding
    capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_target_users: frozenset[str] = field(default_factory=frozenset)
    manageable_user_ids: frozenset[str] = field(default_factory=frozenset)
    creatable_user_ids: frozenset[str] = field(default_factory=frozenset)
    quota_ceiling: LocalUserQuotaLimits = field(default_factory=LocalUserQuotaLimits)
    execution_policy: ExecutionAllowlist = field(default_factory=ExecutionAllowlist)


# LLM: 本DTO只由A权威端口投影；可携带真实使用者与目标，字段正确不等于有效来源。
# 类用途: 运输已核动作、安装许可和独立的调用者/数据归属。
@dataclass(frozen=True)
class AuthorizedLocalCall:
    grant: LocalUserGrant
    action: str
    payload_json: str = "{}"
    as_user: str | None = None
    idempotency_key: str | None = None
    effective_owner: OwnerIdentity | None = None
    caller_owner: OwnerIdentity | None = None


# LLM: 该对象只有本工厂活签发记录才有效，字段投影不构成独立能力凭据。
# 类用途: 绑定普通用户身份、独立路径、固定权限与本次冻结许可。
@dataclass(frozen=True)
class OwnerResolutionResult:
    identity: OwnerIdentity
    owner_home: object
    permission: RestrictedPermission
    execution_policy: RestrictedExecutionPolicy
    user_revision: int
    source: object = field(repr=False, compare=False)
    call: AuthorizedLocalCall = field(repr=False)
    execution: bool = False

    # LLM: 原绑定只取冻结call，不允许多份可变来源相互矛盾。
    # 函数用途: 返回本执行的原安装授权来源。
    @property
    def source_binding(self):
        return self.call.grant.source_binding

    # LLM: 跨安装、激活、用户修订或策略摘要绝不共用同一个执行实例。
    # 函数用途: 给B的受限执行缓存提供完整隔离键。
    @property
    def cache_key(self):
        binding = self.source_binding
        return (self.identity, binding.installation_ref, binding.activation_id, binding.grant_revision,
                self.user_revision, self.execution_policy.policy_digest)


# LLM: 三组UID集合必须冻结，星号只作显式授权；来源摘要与修订不补默认值，实际目标另核。
# 函数用途: 验证宿主投影结构，拒绝可变/畸形权限进入执行链。
def validate_grant(grant):
    if type(grant) is not LocalUserGrant or type(grant.source_binding) is not PluginSourceBinding:
        raise HostAPIError("SOURCE_INVALID", 403)
    binding = grant.source_binding
    if type(binding.grant_revision) is not int or binding.grant_revision < 1:
        raise HostAPIError("SOURCE_INVALID", 403)
    if type(binding.owner) is not OwnerIdentity or not binding.installation_ref:
        raise HostAPIError("SOURCE_INVALID", 403)
    for value in (binding.package_sha256, binding.activation_id):
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise HostAPIError("SOURCE_INVALID", 403)
    for values in (grant.allowed_target_users, grant.manageable_user_ids, grant.creatable_user_ids):
        if type(values) is not frozenset or len(values) > 100:
            raise HostAPIError("SOURCE_INVALID", 403)
        for uid in values:
            if uid != "*":
                validate_local_user_id(uid)
    if type(grant.capabilities) is not frozenset or type(grant.quota_ceiling) is not LocalUserQuotaLimits:
        raise HostAPIError("SOURCE_INVALID", 403)
    if type(grant.execution_policy) is not ExecutionAllowlist:
        raise HostAPIError("SOURCE_INVALID", 403)


# LLM: 拒绝所有深度的重复字段，避免不同层按第一项或最后一项解读同一请求。
# 函数用途: 从严格JSON对象键值对构建唯一映射。
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


# LLM: JSON不接受NaN和Infinity，不能把非有限值传到下游schema或摘要。
# 函数用途: 拒绝非标准数值常量。
def reject_constant(value):
    raise ValueError("non-finite JSON")


# LLM: 不可变JSON字符串只承载普通数据；解析失败或非对象拒绝，不采信其中任何权限字段。
# 函数用途: 解码宿主核验过的准确动作参数。
def call_payload(call):
    try:
        if type(call.payload_json) is not str or len(call.payload_json.encode("utf-8")) > 65536:
            raise ValueError("invalid payload")
        value = json.loads(call.payload_json, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if type(value) is not dict:
            raise ValueError("invalid payload")
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise HostAPIError("INVALID_SCHEMA") from exc


# LLM: 工厂只信任宿主装配的核验端口，不接受插件dict或全局AuthManager推断身份。
# 类用途: 生成并逐次复核普通用户的冷读取与受限执行上下文。
class OwnerContextFactory:
    # LLM: 空权威端口必须保持不可执行，不初始化主owner或复制其权限。
    # 函数用途: 绑定账号存储和原授权核验入口。
    def __init__(self, store, verify_call=None, commit_scope=None):
        self.store = store
        self.verify_call = verify_call
        self.commit_scope = commit_scope
        self._issued = WeakValueDictionary()

    # LLM: 仅原宿主核验口可返回授权投影。
    # 函数用途: 核验一份原始授权调用。
    def verify(self, authorized):
        if self.verify_call is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        try:
            call = self.verify_call(authorized)
        except HostAPIError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise HostAPIError("SOURCE_INVALID", 403) from exc
        if type(call) is not AuthorizedLocalCall or type(call.action) is not str:
            raise HostAPIError("SOURCE_INVALID", 403)
        validate_grant(call.grant)
        call_payload(call)
        return call

    # LLM: 冷路径不创建owner、不初始化agent或模型。
    # 函数用途: 解析已登记且获准访问的目标用户读取上下文。
    def resolve_read_context(self, authorized):
        return self.resolve(authorized, execution=False)

    # LLM: 执行同时要求execute和act_as_user，不能退回安装owner。
    # 函数用途: 解析强制普通用户执行上下文。
    def resolve_execution_context(self, authorized):
        return self.resolve(authorized, execution=True)

    # LLM: 本人路径不借act_as_user；旧管理员委托仍核三组范围与enabled，provider不冒充local账号。
    # 函数用途: 冷解析并签发一份准确来源绑定的上下文。
    def resolve(self, authorized, *, execution):
        call = self.verify(authorized)
        if execution and call.action not in {"chat.send", "execute.tool", "execute.script", "execute.workflow", "execute.workflow_resume"}:
            raise HostAPIError("ACTION_NOT_AVAILABLE", 403)
        own = (call.effective_owner is not None and call.effective_owner.owner_kind == "user"
               and call.effective_owner == call.caller_owner)
        required = ({"execute"} if execution else set()) if own else (
            {"execute", "act_as_user"} if execution else {"act_as_user"})
        if call.grant.source_binding.owner != OwnerIdentity.local_main() and not (
                own and call.grant.source_binding.owner == call.effective_owner):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        if not required <= call.grant.capabilities:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        if not execution and not call.grant.capabilities.intersection({"read", "write", "chat", "interaction", "execute"}):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        if own:
            identity = call.effective_owner
            if call.as_user is not None and call.as_user != identity.owner_id:
                raise HostAPIError("TARGET_DENIED", 403)
        else:
            if call.as_user is None:
                raise HostAPIError("TARGET_REQUIRED", 403)
            uid = validate_local_user_id(call.as_user)
            if uid not in call.grant.allowed_target_users and "*" not in call.grant.allowed_target_users:
                raise HostAPIError("TARGET_DENIED", 403)
            identity = OwnerIdentity("local", "user", uid)
        home, revision = owner_state(self.store, identity)
        policy = build_execution_policy(self.store, identity, call.grant)
        result = OwnerResolutionResult(
            identity, home, restricted_permission(), policy,
            revision, authorized, call, execution,
        )
        self._issued[id(result)] = result
        return result

    # LLM: 原工厂对象才有效，local/provider均逐次核状态、修订与策略，拒复制投影。
    # 函数用途: 再次检查原来源及用户revision。
    def check(self, context):
        if type(context) is not OwnerResolutionResult or self._issued.get(id(context)) is not context:
            raise HostAPIError("SOURCE_INVALID", 403)
        self.check_call(context.source, context.call)
        _, revision = owner_state(self.store, context.identity)
        if revision != context.user_revision:
            raise HostAPIError("USER_REVISION_CHANGED", 409)
        current = build_execution_policy(self.store, context.identity, context.call.grant)
        if current != context.execution_policy:
            raise HostAPIError("POLICY_CHANGED", 409)
        return self.store.require_enabled(context.identity.owner_id) if context.identity.provider == "local" else None

    # LLM: 上游每次重验原活来源；同包重装、grant改版和调用实参变动都不能沿旧上下文执行。
    # 函数用途: 检查原请求仍与冻结的授权投影完全一致。
    def check_call(self, source, expected):
        current = self.verify(source)
        if current.grant.source_binding.activation_id != expected.grant.source_binding.activation_id:
            raise HostAPIError("ACTIVATION_REVOKED", 409)
        if current != expected:
            raise HostAPIError("GRANT_REVOKED", 403)
        return current

    # LLM: 必须由A提供真实生命周期短提交锁，未装配时拒绝写；顺序为生命周期→用户→quota→JSON。
    # 函数用途: 在同一原激活短提交窗口内检查并执行一个有界写动作。
    @contextmanager
    def commit_call(self, source, call):
        if self.commit_scope is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        with self.commit_scope(call.grant.source_binding):
            self.check_call(source, call)
            yield

    # LLM: local锁仍与账号停用共门；provider锁归原owner状态，锁内不执行模型或长进程。
    # 函数用途: 为一个有界工具写入提供激活和用户修订双栅栏。
    @contextmanager
    def commit(self, context):
        self.check(context)
        with self.commit_call(context.source, context.call):
            parts = (("local_users", f"{context.identity.owner_id}.json") if context.identity.provider == "local"
                     else (*context.owner_home.home_dir.relative_to(self.store.root).parts, "owner_status.json"))
            with self.store.locked(parts):
                self.check(context)
                yield


# LLM: A/C只在此转换契约；鲜活A来源仍是权威，C继续检查enabled/策略，不复制管理员执行主体。
# 类用途: 使真实IngressIssuer能使用原C普通账号冷解析器。
class IngressOwnerResolver:
    # LLM: 构造只登记原用户存储，不触发ensure或创建目录。
    # 函数用途: 绑定同一个C账号库及其受限工厂。
    def __init__(self, store):
        self.store = store
        self.factory = OwnerContextFactory(store, self._call)

    # LLM: 只投影真实A来源及同次caller/cap；保留provider身份，不把公共安装管理者变为执行者。
    # 函数用途: 将A已核验的范围转换为C受限读取/执行投影。
    def _call(self, binding):
        import hashlib

        from . import contracts
        from .authorization import IngressIssuer, revalidate_call
        from .schemas import canonical_json, thaw

        authorized = binding if isinstance(binding, contracts.AuthorizedCall) else None
        if authorized is not None:
            revalidate_call(authorized)
            binding = authorized.binding
        if not isinstance(binding, contracts.VerifiedPluginBinding) or not isinstance(binding.source._issuer, IngressIssuer):
            raise HostAPIError("SOURCE_INVALID", 403)
        issuer = binding.source._issuer
        fixed, grant, caps = issuer._live(binding.source)
        digest = hashlib.sha256(canonical_json({"grant": grant, "capabilities": sorted(caps)}).encode()).hexdigest()
        if (thaw(binding.grant) != grant or binding.capabilities != caps or binding.policy_digest != digest
                or binding.actor != fixed.activation_ref.owner().identity
                or (authorized is None and binding.effective_owner.owner_kind != "user")):
            raise HostAPIError("SOURCE_INVALID", 403)
        if self.store.root != fixed.activation_ref.owner().root:
            raise HostAPIError("TARGET_DENIED", 403)
        source = PluginSourceBinding(binding.installation_ref, binding.package_sha256, binding.activation_id,
                                     binding.grant_revision, binding.actor)
        tool_names = grant["execution"]["tool_names"] if grant["execution"].get("max_tool_calls", 1) > 0 else ()
        projected = LocalUserGrant(source, caps, supported_user_ids(grant["target_user_ids"]),
            supported_user_ids(grant["manageable_user_ids"]), supported_user_ids(grant["creatable_user_ids"]),
            LocalUserQuotaLimits.parse(grant["quota_ceiling"]), ExecutionAllowlist(
                frozenset(tool_names), tuple(AssetGrant(
                    a["kind"], a["id"], a["revision"], a["sha256"]) for a in grant["execution"].get("asset_refs", ()))))
        if authorized is None:
            # A发行前binding仅供冷解析；它不是领域动作或执行票据。
            return AuthorizedLocalCall(projected, "chat.send", as_user=binding.effective_owner.owner_id,
                effective_owner=binding.effective_owner, caller_owner=binding.source.caller_owner or binding.actor)
        call = authorized.call
        action = "chat.send" if call.action == "conversations.messages.send" else call.action
        return AuthorizedLocalCall(projected, action, canonical_json(call.payload), call.as_user_id, call.idempotency_key,
            binding.effective_owner, binding.source.caller_owner or binding.actor)


    # LLM: 每次重新冷读C账号及策略；原A继续核对身份、home和当前user_revision。
    # 函数用途: 返回A合同要求的同来源读取上下文。
    def resolve_read(self, binding):
        from .contracts import OwnerReadContext

        context = self.factory.resolve_read_context(binding)
        return OwnerReadContext(context.identity, context.owner_home, binding, context.user_revision)

    # LLM: 只投影C已经收窄的根和工具；网络仍固定关闭，A预算不得放宽，模型/工具运行不在此创建。
    # 函数用途: 返回A合同要求的普通用户受限执行上下文。
    def resolve_execution(self, binding):
        from . import contracts

        context = self.factory.resolve_execution_context(binding)
        source = context.execution_policy
        budgets = {key: value for key, value in binding.grant["execution"].items() if key.startswith("max_")}
        policy = contracts.RestrictedExecutionPolicy(source.read_roots, source.write_roots, source.allowed_tools,
                                                      False, budgets, source.policy_digest)
        return contracts.OwnerResolutionResult(context.identity, context.owner_home, contracts.RestrictedPermission(),
                                                policy, context.user_revision, binding)


# LLM: 纯冷读原账号/owner状态，nofollow绑定目录；provider不查询同名local UID、不创建或迁移空间。
# 函数用途: 返回当前普通owner规范home及鲜活修订，禁用或目录不安全时失败关闭。
def owner_state(store, identity):
    from ..user_space.owner_resolver import resolve_owner_home
    if not isinstance(identity, OwnerIdentity) or identity.owner_kind != "user":
        raise HostAPIError("TARGET_DENIED", 403)
    if identity.provider == "local":
        user = store.require_enabled(validate_local_user_id(identity.owner_id))
        return store.owner_home(user.user_id), user.revision
    home = resolve_owner_home(store.root, identity)
    expected = store.root / "owners" / "providers" / identity.provider / "users"
    if not home.home_dir.is_relative_to(expected) or home.home_dir.name != identity.owner_id:
        raise HostAPIError("TARGET_DENIED", 403)
    if not home.home_dir.is_dir():
        raise HostAPIError("USER_NOT_FOUND", 404)
    parts = home.home_dir.relative_to(store.root).parts
    try:
        store.directory_identity(parts)
    except (OSError, ValueError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None
    status = store.read_json((*parts, "owner_status.json"))
    if status is not None and status.get("status") != "active":
        raise HostAPIError("USER_DISABLED", 403)
    revision = status.get("revision", 1) if status is not None else 1
    if type(revision) is not int or revision < 1:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    return home, revision
