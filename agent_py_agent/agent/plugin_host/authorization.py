# LLM: 每次授权/返回/写入前读取原安装；来源登记仅防DTO冒充，不是第二套执行账。
# 模块用途: 发行真实插件来源，计算权限交集，并通过C的受限resolver核对目标。
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
    # LLM: resolver约定resolve_read(binding)/resolve_execution(binding)，均不得创建管理员主体。
    # 函数用途: 初始化来源登记与宿主策略依赖。
    def __init__(self, owner_resolver=None, capability_policy=None):
        self._resolver = owner_resolver
        self._policy = capability_policy or host_capabilities
        self._sources = WeakValueDictionary()
        self._calls = WeakValueDictionary()
        self._lock = threading.RLock()
        self._closed = False

    # LLM: 先验证令牌，再登记唯一来源；不接受外部构造的source作为原对象。
    # 函数用途: 为一条真实入站连接生成来源。
    def ingress(self, token: str) -> PluginIngressSource:
        self._token(token)
        with self._lock:
            if self._closed:
                raise HostAPIError("HOST_NOT_READY", 503)
            source = PluginIngressSource(self, token)
            self._sources[id(source)] = source
            return source

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

    # LLM: 按对象身份核真实来源，存活资格每次读回；策略异常失败关闭。
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
        return fixed, grant, caps

    # LLM: 身份仅从令牌与C核验结果产生；资源实际归属仍由各领域在原读写锁内再核。
    # 函数用途: 验证闭合调用、能力与目标，发行不可伪造的调用投影。
    def authorize(self, source: PluginIngressSource, call: Call) -> AuthorizedCall:
        call = _checked_call(call)
        fixed, grant, caps = self._live(source)
        _check_capabilities(call, caps)
        actor = fixed.activation_ref.owner().identity
        if actor != OwnerIdentity.local_main():
            raise HostAPIError("TARGET_DENIED", 403)
        target = _target_identity(call, grant, actor)
        digest = hashlib.sha256(canonical_json({"grant": grant, "capabilities": sorted(caps)}).encode()).hexdigest()
        binding = VerifiedPluginBinding(grant, actor, target, source, caps, policy_digest=digest)
        context = self._read(binding)
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

    # LLM: 发行后的DTO不能免掉最新token/能力/用户修订/policy核验；禁止replace伪造。
    # 函数用途: 在领域短提交、结果输出和流事件前核对同一调用。
    def revalidate(self, authorized: AuthorizedCall) -> None:
        with self._lock:
            if self._calls.get(id(authorized)) is not authorized:
                raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
        binding = authorized.binding
        _, grant, caps = self._live(binding.source)
        _check_capabilities(authorized.call, caps)
        digest = hashlib.sha256(canonical_json({"grant": grant, "capabilities": sorted(caps)}).encode()).hexdigest()
        if digest != binding.policy_digest:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        context = self._read(binding)
        if context.user_revision != binding.user_revision:
            raise HostAPIError("REVISION_CONFLICT", 409)
        if _requires_execution(authorized.call) and self._execution(binding).execution_policy != authorized.execution_policy:
            raise HostAPIError("CAPABILITY_DENIED", 403)

    # LLM: 无as_user时仅纯解析原owner；跨用户必须使用C的鲜活managed/enabled读取器。
    # 函数用途: 解析冷只读上下文并核对C返回的来源、身份和路径。
    def _read(self, binding: VerifiedPluginBinding) -> OwnerReadContext:
        fixed, _, _ = self._live(binding.source)
        owner = fixed.activation_ref.owner()
        if binding.effective_owner == owner.identity:
            return OwnerReadContext(owner.identity, owner, binding)
        if self._resolver is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        context = self._resolver.resolve_read(binding)
        _check_owner_context(context, binding, owner.root)
        return context

    # LLM: main永远不能用作执行目标；缺C的factory明确未就绪，不能借只读路径构造invoker。
    # 函数用途: 核对受限执行factory给出的普通身份和权限上限。
    def _execution(self, binding: VerifiedPluginBinding) -> OwnerResolutionResult:
        if binding.effective_owner.owner_kind != "user" or self._resolver is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        if not {"execute", "act_as_user"} <= binding.capabilities:
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


# LLM: poll/stream还需领域根据原请求kind检查chat或execute，不能按此并集扩权。
# 函数用途: 验证静态与参数相关能力。
def _check_capabilities(call: Call, capabilities: frozenset) -> None:
    spec = action_spec(call.action)
    if (not required_capabilities(call.action, call.payload) <= capabilities
            or (spec.any_capabilities and not spec.any_capabilities & capabilities)):
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if call.as_user_id is not None and "act_as_user" not in capabilities:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if _requires_execution(call) and call.as_user_id is None:
        raise HostAPIError("TARGET_DENIED", 403)


# LLM: 用户目标由规范UID和批准名单决定，不能由请求传路径或管理员标记。
# 函数用途: 选定有效数据owner。
def _target_identity(call: Call, grant: dict, actor: OwnerIdentity) -> OwnerIdentity:
    if call.as_user_id is None:
        return actor
    if call.as_user_id not in grant["target_user_ids"]:
        raise HostAPIError("TARGET_DENIED", 403)
    return OwnerIdentity("local", "user", call.as_user_id)


# LLM: 只有执行动作或明确bounded消息需要独立执行factory。
# 函数用途: 判定是否需要C的受限执行主体。
def _requires_execution(call: Call) -> bool:
    return call.action.startswith("execute.") or (call.action == "conversations.messages.send"
                                                 and call.payload.get("tool_policy") == "bounded")


# LLM: 此处只筛明确allowlist，线程/task实际归属由领域核原账，未命中资源不泄漏存在性。
# 函数用途: 拒绝明确不在批准范围的用户与工具。
def _check_static_scope(call: Call, grant: dict) -> None:
    if call.action.startswith("users.local.") and "user_id" in call.payload:
        key = "creatable_user_ids" if call.action == "users.local.create" else "manageable_user_ids"
        if call.payload["user_id"] not in grant[key]:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    if call.action == "execute.tool" and call.payload["tool_name"] not in grant["execution"]["tool_names"]:
        raise HostAPIError("CAPABILITY_DENIED", 403)


# LLM: 只读也不能信任错误C适配器；已登记普通账号以C原规范home为准，不把它重路由到旧owner目录。
# 函数用途: 核对受信resolver结果的身份、来源、规范home和用户修订。
def _check_owner_context(context, binding: VerifiedPluginBinding, root) -> None:
    if (not isinstance(context, (OwnerReadContext, OwnerResolutionResult))
            or context.identity != binding.effective_owner or context.source_binding is not binding
            or not isinstance(context.owner_home, OwnerHomeResult)
            or context.owner_home.identity != context.identity or context.owner_home.root != root
            or type(context.user_revision) is not int or context.user_revision < 1):
        raise HostAPIError("TARGET_DENIED", 403)
    from ..user_space.local_user_store import LocalUserStore
    from ..user_space.owner_resolver import resolve_owner_home

    users = LocalUserStore(root)
    registered = users.get(context.identity.owner_id)
    expected = resolve_owner_home(root, context.identity)
    if registered is not None:
        user = users.require_enabled(context.identity.owner_id, context.user_revision)
        if user.owner != context.identity:
            raise HostAPIError("TARGET_DENIED", 403)
        expected = users.owner_home(user.user_id)
    if context.owner_home != expected:
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
