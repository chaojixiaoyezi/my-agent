# LLM: 每次授权/返回/写入前读取原安装；审计资格共用managed范围门，目录仍取effective_owner，来源不另建执行账。
# 模块用途: 绑定真实调用者与安装，核对受管选择器、普通目标、能力及工作目录边界。
from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from weakref import WeakValueDictionary

from ..plugin_host_api import verify_host_api_token
from ..plugin_manifest_host_v9 import host_capabilities
from ..user_space.owner_resolver import OwnerHomeResult, OwnerIdentity
from .action_specs import action_spec, required_capabilities
from .contracts import (
    AuthorizedCall,
    Call,
    OwnerReadContext,
    OwnerResolutionResult,
    PluginIngressSource,
    RestrictedExecutionPolicy,
    RestrictedPermission,
    VerifiedPluginBinding,
    decode_call,
)
from .errors import HostAPIError
from .schemas import canonical_json, strict_json, thaw


# LLM: 依赖均由宿主组合根注入；来源/调用按真实对象身份登记，副本不能取得写权，close立即撤销本服务来源。
# 类用途: 连接令牌、当前授权、目标resolver和本次调用的生命周期。
class IngressIssuer:
    # LLM: resolver冷读原owner；可注入原认证会话核验器，缺失时不能把会话回退成安装者。
    # 函数用途: 初始化来源登记与宿主策略依赖。
    def __init__(self, owner_resolver=None, capability_policy=None, *, session_verifier=None):
        self._resolver = owner_resolver
        self._policy = capability_policy or host_capabilities
        self._session_verifier = session_verifier
        self._sources = WeakValueDictionary()
        self._calls = WeakValueDictionary()
        self._lock = threading.RLock()
        self._closed = False

    # LLM: caller_owner仅供受信宿主内调用；HTTP只传原会话令牌，核验失败不回退安装者。
    # 函数用途: 登记固定安装与独立调用者的唯一真实来源。
    def ingress(self, token: str, *, caller_owner: OwnerIdentity | None = None,
                user_session_token: str | None = None) -> PluginIngressSource:
        fixed = self._token(token)
        if caller_owner is not None and not isinstance(caller_owner, OwnerIdentity):
            raise HostAPIError("TARGET_DENIED", 403)
        if user_session_token is not None:
            resolved = self._session_owner(fixed.activation_ref.owner().root, user_session_token)
            if caller_owner is not None and caller_owner != resolved:
                raise HostAPIError("TARGET_DENIED", 403)
            caller_owner = resolved
        with self._lock:
            if self._closed:
                raise HostAPIError("HOST_NOT_READY", 503)
            source = PluginIngressSource(self, token, caller_owner, user_session_token)
            self._sources[id(source)] = source
            return source

    # LLM: 原认证DTO只取effective_owner，保留可信旧注入owner口；不从业务正文或principal字符串推导权限。
    # 函数用途: 每次重验原会话并取得规范OwnerIdentity，密码与SSO会话走同一入站门。
    def _session_owner(self, root, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or not token.isascii() or any(
                ord(character) < 33 or ord(character) > 126 for character in token):
            raise HostAPIError("USER_SESSION_INVALID", 401)
        verifier = self._session_verifier
        if verifier is None:
            try:
                from ..user_space.user_auth import verify_user_session
                verifier = verify_user_session
            except ImportError:
                raise HostAPIError("USER_SESSION_INVALID", 401) from None
        try:
            result = verifier(root, token)
            owner = result if isinstance(result, OwnerIdentity) else (
                getattr(result, 'effective_owner', None) or getattr(result, 'owner', None))
        except (OSError, ValueError, TypeError):
            raise HostAPIError("USER_SESSION_INVALID", 401) from None
        if not isinstance(owner, OwnerIdentity):
            raise HostAPIError("USER_SESSION_INVALID", 401)
        return owner

    # LLM: 清理只结束本服务资格，不声称底层任务或外部资源已停止。
    # 函数用途: 关闭发行器并使存活来源失效。
    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._sources.clear()
            self._calls.clear()

    # LLM: 原API令牌表及安装记录是唯一权威；旧read令牌不能用于v2。
    # 函数用途: 核对令牌和显式v9固定授权。
    def _token(self, token: str):
        fixed = verify_host_api_token(token) if isinstance(token, str) else None
        if fixed is None or fixed.host_api_grant_json is None:
            raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
        return fixed

    # LLM: 身份、原会话和安装资格逐次读回；普通公共调用去掉治理/委托能力，私有严格本人。
    # 函数用途: 返回当前令牌、授权和批准能力交集。
    def _live(self, source: PluginIngressSource):
        with self._lock:
            if self._closed or self._sources.get(id(source)) is not source:
                raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
        fixed = self._token(source._lease)
        grant = strict_json(fixed.host_api_grant_json.encode())
        try:
            entry = fixed.activation_ref.require()
            caps = frozenset(entry.manifest.host_api) & frozenset(grant["approved_capabilities"]) & frozenset(self._policy())
        except (OSError, ValueError, TypeError):
            raise HostAPIError("ACTIVATION_REVOKED", 410) from None
        actor = fixed.activation_ref.owner().identity
        if source.user_session_token is not None and self._session_owner(
                fixed.activation_ref.owner().root, source.user_session_token) != source.caller_owner:
            raise HostAPIError("USER_SESSION_INVALID", 401)
        caps = _caller_capabilities(actor, entry, source.caller_owner, caps)
        return fixed, grant, caps

    # LLM: 治理先核原安装public及真实管理员，目标selector不改变数据owner；私有和公共个人均不借安装者治理。
    # 函数用途: 验证闭合调用、能力、目标和目录，发行不可伪造的调用投影。
    def authorize(self, source: PluginIngressSource, call: Call) -> AuthorizedCall:
        call = _checked_call(call)
        fixed, grant, caps = self._live(source)
        actor = fixed.activation_ref.owner().identity
        target = _call_target(source, call, grant, actor, fixed.activation_ref.require())
        _check_managed_ingress(call, fixed, source, target, grant)
        own = target.owner_kind == "user" and (target == actor or target == source.caller_owner)
        _check_capabilities(call, caps, own=own)
        digest = hashlib.sha256(canonical_json({"grant": grant, "capabilities": sorted(caps)}).encode()).hexdigest()
        binding = VerifiedPluginBinding(grant, actor, target, source, caps, policy_digest=digest)
        context = self._read(binding)
        if "workspace_dir" in call.payload:
            workspace_directory(context, call.payload["workspace_dir"])
        binding = replace(binding, user_revision=context.user_revision)
        _check_static_scope(call, grant)
        policy = _reply_policy(grant)
        if _requires_execution(call):
            policy = self._execution(binding).execution_policy
        scope = {key: grant[key] for key in ("threads", "tasks", "target_user_ids", "manageable_user_ids", "creatable_user_ids")}
        authorized = AuthorizedCall(call, binding, target, scope, policy)
        with self._lock:
            if self._closed:
                raise HostAPIError("HOST_NOT_READY", 503)
            self._calls[id(authorized)] = authorized
        return authorized

    # LLM: 返回及重放逐次重核原public管理员身份和manageable名单，禁止已发行DTO缓存旧治理权。
    # 函数用途: 在领域短提交、结果输出和流事件前核对同一调用。
    def revalidate(self, authorized: AuthorizedCall) -> None:
        with self._lock:
            if self._calls.get(id(authorized)) is not authorized:
                raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
        binding = authorized.binding
        fixed, grant, caps = self._live(binding.source)
        _check_managed_ingress(authorized.call, fixed, binding.source, binding.effective_owner, grant)
        own = binding.effective_owner.owner_kind == "user" and (
            binding.effective_owner == binding.actor or binding.effective_owner == binding.source.caller_owner)
        _check_capabilities(authorized.call, caps, own=own)
        digest = hashlib.sha256(canonical_json({"grant": grant, "capabilities": sorted(caps)}).encode()).hexdigest()
        if digest != binding.policy_digest:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        context = self._read(binding)
        if context.user_revision != binding.user_revision:
            raise HostAPIError("REVISION_CONFLICT", 409)
        if _requires_execution(authorized.call) and self._execution(binding).execution_policy != authorized.execution_policy:
            raise HostAPIError("CAPABILITY_DENIED", 403)

    # LLM: 普通目标即便等于安装者也必须走C的enabled/revision门；只有main可直接冷读自身。
    # 函数用途: 解析冷只读上下文并核对C返回的来源、身份和路径。
    def _read(self, binding: VerifiedPluginBinding) -> OwnerReadContext:
        fixed, _, _ = self._live(binding.source)
        owner = fixed.activation_ref.owner()
        if binding.effective_owner == owner.identity and owner.identity == OwnerIdentity.local_main():
            return OwnerReadContext(owner.identity, owner, binding)
        if self._resolver is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        context = self._resolver.resolve_read(binding)
        _check_owner_context(context, binding, owner.root)
        return context

    # LLM: main不作执行目标；本人只需execute，管理员代执行仍双能力，缺factory失败关闭。
    # 函数用途: 核对受限执行factory给出的普通身份和权限上限。
    def _execution(self, binding: VerifiedPluginBinding) -> OwnerResolutionResult:
        if binding.effective_owner.owner_kind != "user" or self._resolver is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        own = binding.effective_owner in {binding.actor, binding.source.caller_owner}
        if not ({"execute"} if own else {"execute", "act_as_user"}) <= binding.capabilities:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        context = self._resolver.resolve_execution(binding)
        fixed, _, _ = self._live(binding.source)
        _check_owner_context(context, binding, fixed.activation_ref.owner().root)
        if (not isinstance(context, OwnerResolutionResult) or context.permission != RestrictedPermission()
                or context.user_revision != binding.user_revision):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        _check_execution_policy(context, binding.grant["execution"])
        return context

    # LLM: 公共resolver只消费本发行器真实调用；返回前再次核目标，没有ensure副作用。
    # 函数用途: 提供领域读取用的最新owner上下文。
    def resolve_read(self, authorized: AuthorizedCall) -> OwnerReadContext:
        self.revalidate(authorized)
        return self._read(authorized.binding)

    # LLM: 仅执行动作/明确bounded才可调用factory；写入口仍须短提交重核，返回对象非永久执行票据。
    # 函数用途: 提供C已核验的当前受限执行上下文。
    def resolve_execution(self, authorized: AuthorizedCall) -> OwnerResolutionResult:
        self.revalidate(authorized)
        if not _requires_execution(authorized.call):
            raise HostAPIError("CAPABILITY_DENIED", 403)
        return self._execution(authorized.binding)


# LLM: 接收域必须核发行器登记，不接受仅有同名字段的对象。
# 函数用途: 公共授权入口。
def authorize_call(ingress: PluginIngressSource, call: Call) -> AuthorizedCall:
    if not isinstance(ingress, PluginIngressSource) or not isinstance(ingress._issuer, IngressIssuer):
        raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
    return ingress._issuer.authorize(ingress, call)


# LLM: 公共写端可直接调用本门；绕过HTTP不会免掉原对象核验。
# 函数用途: 重新验证已发行调用。
def revalidate_call(authorized: AuthorizedCall) -> None:
    if not isinstance(authorized, AuthorizedCall):
        raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
    source = authorized.binding.source
    if not isinstance(source, PluginIngressSource) or not isinstance(source._issuer, IngressIssuer):
        raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
    source._issuer.revalidate(authorized)


# LLM: 显式目录只认effective_owner=local/main；普通用户在解析路径前403，管理员沿原目录/控制面校验且不创建目录。
# 函数用途: 规范管理员项目目录，禁止普通用户用目录参数绑定家目录或任意子目录。
def workspace_directory(context: OwnerReadContext, value):
    from pathlib import Path

    from ..conversation.workspace_context import (
        WorkspaceDirectoryError,
        validate_workspace_directory,
    )

    home = context.owner_home.home_dir
    is_admin = context.identity == OwnerIdentity.local_main()
    if not is_admin:
        raise HostAPIError("WORKSPACE_DIR_FORBIDDEN_FOR_NON_ADMIN", 403)
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = home / path
        path = path.resolve(strict=False)
        return validate_workspace_directory(path, owner_home=home, is_admin=is_admin, home_root=context.owner_home.root)
    except HostAPIError:
        raise
    except (WorkspaceDirectoryError, OSError, RuntimeError, TypeError, ValueError):
        raise HostAPIError("WORKSPACE_DIR_INVALID") from None


# LLM: 直接构造Call也必须重过相同严格解码合同。
# 函数用途: 防止非HTTP调用绕开参数约束。
def _checked_call(call: Call) -> Call:
    if not isinstance(call, Call):
        raise HostAPIError("INVALID_SCHEMA")
    row = {"schema": call.schema, "action": call.action, "payload": thaw(call.payload)}
    if call.idempotency_key is not None:
        row["idempotency_key"] = call.idempotency_key
    if call.as_user_id is not None:
        row["as_user"] = {"user_id": call.as_user_id}
    return decode_call(canonical_json(row).encode())


# LLM: 本人as_user只可已核自身声明；委托仍要求act_as_user，执行始终须普通目标。
# 函数用途: 验证静态与参数相关能力。
def _check_capabilities(call: Call, capabilities: frozenset, *, own=False) -> None:
    spec = action_spec(call.action)
    if (not required_capabilities(call.action, call.payload) <= capabilities
            or (spec.any_capabilities and not spec.any_capabilities & capabilities)):
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if call.as_user_id is not None and not own and "act_as_user" not in capabilities:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if _requires_execution(call) and call.as_user_id is None and not own:
        raise HostAPIError("TARGET_DENIED", 403)


# LLM: 先验证规范UID，再核显式名单或星号；通配不免enabled与目标目录检查。
# 函数用途: 选定有效数据owner。
def _target_identity(call: Call, grant: dict, actor: OwnerIdentity) -> OwnerIdentity:
    if call.as_user_id is None:
        return actor
    from .owner_context import validate_local_user_id
    uid = validate_local_user_id(call.as_user_id)
    if uid not in grant["target_user_ids"] and "*" not in grant["target_user_ids"]:
        raise HostAPIError("TARGET_DENIED", 403)
    return OwnerIdentity("local", "user", uid)


# LLM: 所有private及公共普通调用者移除users.read/manage和委托；发现页不能宣称不存在的治理资格。
# 函数用途: 限定安装可访问性和本次有效能力。
def _caller_capabilities(actor, entry, caller, caps):
    scope = getattr(entry, "installation_scope", "private")
    if scope not in {"private", "public"}:
        raise HostAPIError("INVALID_SCHEMA")
    if scope == "private":
        caps = caps - {"users.read", "users.manage", "act_as_user"}
    if actor != OwnerIdentity.local_main():
        if actor.owner_kind != "user" or scope != "private" or caller is not None and caller != actor:
            raise HostAPIError("TARGET_DENIED", 403)
        if {"users.read", "users.manage", "act_as_user"} & caps:
            raise HostAPIError("CAPABILITY_DENIED", 403)
    if caller is not None and caller != actor and scope != "public":
        raise HostAPIError("TARGET_DENIED", 403)
    actual = caller or actor
    if actual != OwnerIdentity.local_main():
        if actual.owner_kind != "user":
            raise HostAPIError("TARGET_DENIED", 403)
        caps = caps - {"users.read", "users.manage", "act_as_user"}
    return caps


# LLM: private及旧缺省记录仅自身Owner；只有公共管理员可沿原目标名单代操作，公共个人不能换人。
# 函数用途: 固定本次数据Owner并在私有跨用户请求进入resolver前拒绝。
def _call_target(source, call, grant, actor, entry=None):
    if actor != OwnerIdentity.local_main():
        if call.as_user_id is not None:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        return actor
    if getattr(entry, "installation_scope", "private") == "private":
        if call.as_user_id is not None and call.as_user_id != actor.owner_id:
            raise HostAPIError("TARGET_DENIED", 403)
        return actor
    caller = source.caller_owner
    if caller is not None and caller != OwnerIdentity.local_main():
        if call.as_user_id is not None and call.as_user_id != caller.owner_id:
            raise HostAPIError("TARGET_DENIED", 403)
        return caller
    return _target_identity(call, grant, actor)


# LLM: 只有执行动作或明确bounded消息需要独立执行factory。
# 函数用途: 判定是否需要C的受限执行主体。
def _requires_execution(call: Call) -> bool:
    return call.action.startswith("execute.") or (call.action == "conversations.messages.send"
                                                 and call.payload.get("tool_policy") == "bounded")


# LLM: 治理及审计组资格共用managed selector门，普通local动作保留原404；IM/main即使通配仍403。
# 函数用途: 先区分受管身份范围，再核对普通账号和工具授权，避免审计组动作落错错误码。
def _check_static_scope(call: Call, grant: dict) -> None:
    if call.action.startswith(("users.managed.", "governance.reviews.")) or call.action == "users.local.audit_group.set":
        values = [managed_user_id(call.payload[key]) for key in ("user_id", "target_owner_id") if key in call.payload]
        if len(set(values)) > 1 or any(uid not in grant["manageable_user_ids"] and "*" not in grant["manageable_user_ids"] for uid in values):
            raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403)
    elif call.action.startswith("users.local.") and "user_id" in call.payload:
        from .owner_context import validate_local_user_id
        uid = validate_local_user_id(call.payload["user_id"])
        key = "creatable_user_ids" if call.action == "users.local.create" else "manageable_user_ids"
        if uid not in grant[key] and "*" not in grant[key]:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    if call.action == "execute.tool" and call.payload["tool_name"] not in grant["execution"]["tool_names"]:
        raise HostAPIError("CAPABILITY_DENIED", 403)


# LLM: 三种selector形状互斥解析，不能连续剥前缀；UID沿原保留名规则，注册由领域复验。
# 函数用途: 将受管选择器变成准确本地普通用户ID，越域统一403。
def managed_user_id(value):
    from .owner_context import validate_local_user_id

    if isinstance(value, str) and value.startswith("local/users/"):
        raw = value[len("local/users/"):]
    else:
        raw = value.removeprefix("local/") if isinstance(value, str) else value
    try:
        return validate_local_user_id(raw)
    except HostAPIError:
        raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403) from None


# LLM: 原激活/调用者/effective owner共同核public管理员范围；名单复核在authorise与返回、短写/重放前共用。
# 函数用途: 防止公共插件或私有管理员安装绕过治理边界。
def _check_managed_ingress(call, fixed, source, target, grant):
    if not call.action.startswith(("users.managed.", "governance.reviews.")):
        return
    actor = fixed.activation_ref.owner().identity
    if (actor != OwnerIdentity.local_main() or (source.caller_owner or actor) != OwnerIdentity.local_main()
            or target != actor or getattr(fixed.activation_ref.require(), "installation_scope", "private") != "public"):
        raise HostAPIError("MANAGED_USER_SCOPE_DENIED", 403)
    _check_static_scope(call, grant)


# LLM: 只读也必须核原local账号或provider状态；不能用同名local UID替代非local身份。
# 函数用途: 核对受信resolver结果的身份、来源、规范home和用户修订。
def _check_owner_context(context, binding: VerifiedPluginBinding, root) -> None:
    if (not isinstance(context, (OwnerReadContext, OwnerResolutionResult))
            or context.identity != binding.effective_owner or context.source_binding is not binding
            or not isinstance(context.owner_home, OwnerHomeResult)
            or context.owner_home.identity != context.identity or context.owner_home.root != root
            or type(context.user_revision) is not int or context.user_revision < 1):
        raise HostAPIError("TARGET_DENIED", 403)
    from ..user_space.local_user_store import LocalUserStore
    from .owner_context import owner_state

    users = LocalUserStore(root)
    expected, revision = owner_state(users, context.identity)
    if context.owner_home != expected or revision != context.user_revision:
        raise HostAPIError("TARGET_DENIED", 403)


# LLM: reply-only显式零工具和断网，不能沿用安装进程的OS权限。
# 函数用途: 创建非执行调用的受限上限。
def _reply_policy(grant: dict) -> RestrictedExecutionPolicy:
    budgets = {key: value for key, value in grant["execution"].items() if key.startswith("max_")}
    budgets["max_tool_calls"] = 0
    return RestrictedExecutionPolicy(budgets=budgets)


# LLM: C返回值只能继续收紧批准工具、预算和owner根；具体受保护子路径由C与原工具共同禁止。
# 函数用途: 拒绝扩大执行范围或缺预算的factory结果。
def _check_execution_policy(context: OwnerResolutionResult, grant) -> None:
    policy = context.execution_policy
    if (not isinstance(policy, RestrictedExecutionPolicy) or not policy.tool_names <= set(grant["tool_names"])
            or not policy.policy_digest):
        raise HostAPIError("CAPABILITY_DENIED", 403)
    for key, ceiling in grant.items():
        if key.startswith("max_") and (type(policy.budgets.get(key)) is not int or not 0 <= policy.budgets[key] <= ceiling):
            raise HostAPIError("CAPABILITY_DENIED", 403)
    from pathlib import Path
    for root in (*policy.read_roots, *policy.write_roots):
        if not Path(root).is_absolute() or not Path(root).resolve().is_relative_to(context.owner_home.home_dir):
            raise HostAPIError("CAPABILITY_DENIED", 403)
