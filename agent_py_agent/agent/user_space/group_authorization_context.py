# LLM: 群能力门只组合原 HostChannelActor、实时 provider 事实与 tool_policy；临时持锁载体不保存第二身份或授权账。
# 模块用途: 仅为群监控复核额外授权和原锁；普通配置保持原owner/admin门，供应商失败只投影固定诊断。
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace

from ..capability.runtime_config_reload import deployment_capability_config
from ..common.json_io import locked_json_path
from ..conversation.host_actor import (
    HostChannelActor,
    _validate_scope,
    actor_from_scope,
    resolve_host_actor,
)
from .group_authorization import (
    GROUP_CAPABILITIES,
    GroupAuthoritySnapshot,
    GroupAuthorizationError,
    actor_group_chat_id,
    group_chat_id,
    member_has_group_capability,
    read_group_authorization,
)


# LLM: 仅表示当前同步调用仍持有原策略锁；退出会使同对象失效，复制异步上下文不能把它变成长期权限。
# 类用途: 让同一写事务内的嵌套服务复用检查，避免重复查成员及重复获取文件锁。
@dataclass
class _GroupMutationLease:
    owner_home: object
    capability: str
    actor: HostChannelActor
    snapshot: GroupAuthoritySnapshot | None
    execution_identity: tuple
    active: bool = True


_ACTIVE_LEASES: ContextVar[tuple[_GroupMutationLease, ...]] = ContextVar('group_mutation_leases', default=())
_LOGGER = logging.getLogger(__name__)


# LLM: 开关只读共享部署 capability，缺字段按要求开启；owner 局部配置及模型属性不能关掉这道边界。
# 函数用途: 判断群操作是否要求人工授权，非群 owner 保持原行为。
def group_authorization_required(home) -> bool:
    if getattr(home, 'owner_kind', '') != 'group':
        return False
    try:
        config = deployment_capability_config(home)
        value = getattr(config, 'im_group_authorization_required', True)
        if type(value) is not bool:
            raise ValueError('invalid deployment setting')
        return value
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_UNAVAILABLE') from exc


# LLM: 先核原registry的provider/chat；provider失败只经其safe_diagnostics输出固定reason/code/stage，不抄异常正文或响应。
# 函数用途: 获取完整官方群事实并best-effort更新原姓名展示；失败保留安全可定位原因，不缓存权限。
def current_group_authority(agent) -> GroupAuthoritySnapshot:
    from ..adapter.feishu_provider_facts import FeishuProviderFactError

    home = agent.home_paths
    try:
        adapter = agent.delivery_service.registry.adapter_for(home.owner_provider)
        raw = adapter.group_authority_snapshot(group_chat_id(home))
        snapshot = GroupAuthoritySnapshot.from_mapping(raw)
        if snapshot.provider != home.owner_provider or snapshot.chat_id != group_chat_id(home):
            raise ValueError('provider scope mismatch')
    except FeishuProviderFactError as exc:
        raise GroupAuthorizationError('GROUP_AUTHORITY_UNAVAILABLE', diagnostics=exc.safe_diagnostics()) from None
    except GroupAuthorizationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        raise GroupAuthorizationError('GROUP_AUTHORITY_UNAVAILABLE') from exc
    _remember_member_display_names(home, snapshot, raw.get('member_display_names'))
    return snapshot


# LLM: 仅当前准确provider/chat完整成员的姓名可入展示索引；索引故障仅记录异常类型，不吞授权错误、不打印名字或平台值。
# 函数用途: 将本次已有姓名交原身份模块安全保存，缺名不写，展示失败仍让授权主链继续。
def _remember_member_display_names(home, snapshot, names):
    if names is None:
        return
    from .identity_store import remember_provider_display_names

    try:
        if not isinstance(names, dict):
            raise ValueError('member display projection is invalid')
        remember_provider_display_names(home, provider=snapshot.provider,
            names={subject: name for subject, name in names.items() if subject in snapshot.member_ids})
    except (OSError, TypeError, ValueError) as exc:
        _LOGGER.warning('group_member_display_projection_failed exception_type=%s', type(exc).__name__)


# LLM: 同步嵌套只有仍活跃且准确 owner/capability/actor 的原持锁对象可复用，结束后任何复制上下文均无效。
# 函数用途: 找到当前写事务已经核验的能力，不从模型上下文或策略缓存推断授权。
def _active_lease(home, capability=None, actor=None):
    return next((lease for lease in reversed(_ACTIVE_LEASES.get())
        if lease.active and lease.owner_home == home.owner_home_dir and (capability is None or lease.capability == capability)
        and lease.execution_identity == _execution_identity()
        and (actor is None or actor == lease.actor)), None)


# LLM: thread 与 asyncio task 都是宿主当前执行事实；ContextVar 的复制不能复制原持锁调用身份。
# 函数用途: 为同步嵌套检查取当前执行身份，无事件循环时保留明确的 None。
def _execution_identity():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


# LLM: 复制到并行执行的活租约不能等待父锁或复用父权；过期租约不构成证明，也不阻止正常重新核验。
# 函数用途: 拒绝跨 thread/task 携带的在途能力，避免事件循环自锁与身份借用。
def _reject_foreign_active_lease(home):
    if any(lease.active and lease.owner_home == home.owner_home_dir
           and lease.execution_identity != _execution_identity() for lease in _ACTIVE_LEASES.get()):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')


# LLM: actor只认宿主专有字段及原ThreadStore；topic用同源严格地址parser核基础群，保留完整绑定，缺child线程不退父级。
# 函数用途: 为工具、子孙及人工配置控制核准确原话题发起人，归群投影不扩大其作用域。
def group_operation_actor(agent, thread_id='', actor=None):
    if not thread_id:
        from ..runtime_context import current_subagent_run_id, current_task_attributes

        attrs = current_task_attributes(agent) or {}
        thread_id = attrs.get('agent_thread_id') or attrs.get('conversation_thread_id') or ''
        if not thread_id and not current_subagent_run_id(agent):
            current = getattr(agent, '_current_run_params', None)
            attrs = getattr(current, 'task_attributes', None) or {}
            thread_id = attrs.get('agent_thread_id') or attrs.get('conversation_thread_id') or ''
    if not thread_id:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    if actor is None:
        actor = resolve_host_actor(agent, thread_id)
    elif isinstance(actor, HostChannelActor):
        actor = _validate_scope(agent, thread_id, actor)
    if (not isinstance(actor, HostChannelActor) or actor.owner_id != agent.home_paths.owner_id
            or actor.channel != agent.home_paths.owner_provider or actor_group_chat_id(actor) != group_chat_id(agent.home_paths)):
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    return actor


# LLM: 仅监控查provider/grant；配置租约只核原HostActor和目标身份，不读群授权或取网，不能当作监控权。
# 函数用途: 复核监控授权或原配置调用身份，保持撤销CAS及同步锁，不让普通功能要求额外群授权。
@contextmanager
def group_capability_guard(agent, capability: str, *, thread_id: str = '', actor=None):
    home = agent.home_paths
    if not group_authorization_required(home):
        yield
        return
    if capability not in GROUP_CAPABILITIES and capability != 'assistant.configure':
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    _reject_foreign_active_lease(home)
    actual = group_operation_actor(agent, thread_id, actor)
    current = _active_lease(home)
    if current is not None and current.actor != actual:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    if _active_lease(home, capability, actual) is not None:
        yield
        return
    parent = _active_lease(home, actor=actual)
    if capability in GROUP_CAPABILITIES and parent is not None and parent.snapshot is None:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    snapshot = ((parent.snapshot if parent is not None and parent.snapshot is not None else current_group_authority(agent))
                if capability in GROUP_CAPABILITIES else None)
    with nullcontext() if parent is not None else locked_json_path(home.owner_tool_policy_json):
        if capability in GROUP_CAPABILITIES:
            _data, block = read_group_authorization(home)
            if actual.actor_id not in snapshot.member_ids:
                raise GroupAuthorizationError('GROUP_AUTHORIZATION_MEMBER_REQUIRED')
            if not member_has_group_capability(block, snapshot, actual, capability):
                raise GroupAuthorizationError('GROUP_AUTHORIZATION_REQUIRED')
        lease = _GroupMutationLease(home.owner_home_dir, capability, actual, snapshot, _execution_identity())
        token = _ACTIVE_LEASES.set((*_ACTIVE_LEASES.get(), lease))
        try:
            yield
        finally:
            lease.active = False
            _ACTIVE_LEASES.reset(token)


# LLM: 最终写入只消费同owner/当前执行身份租约；配置租约不含grant或provider权，不能借其获监控能力。
# 函数用途: 保持原配置目标和监控事务的身份边界，不从路径或名称猜授权。
def require_group_mutation_lease(home, capability: str) -> None:
    if group_authorization_required(home) and _active_lease(home, capability) is None:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_REQUIRED')


# LLM: 只有同一执行上下文仍持原群策略锁时才跳过重入；其它调用必须取得原 JSON 线程/OS 锁。
# 函数用途: 让审批模式等同文件写入口在能力 guard 内复用原锁，避免非重入锁自锁。
def group_policy_write_lock(home):
    return nullcontext() if _active_lease(home) is not None else locked_json_path(home.owner_tool_policy_json)


# LLM: 只对已认证 Gateway scope 的新冷 host 装配原 HostActor；不修改共享 Agent 的运行参数，不创建授权。
# 函数用途: 给 HTTP、slash 和模型菜单配置写入提供与工具一致的真实发起人上下文。
def bind_group_control_host(base, host, scope, thread_id):
    host.delivery_service = getattr(base, 'delivery_service', None)
    if getattr(host.home_paths, 'owner_kind', '') != 'group':
        return host
    ingress = SimpleNamespace(channel=scope.channel, channel_conversation_id=scope.conversation_id,
                              channel_user_id=scope.user_id)
    actor = actor_from_scope(host, ingress, thread_id, str(scope.metadata.get('channel_message_id') or 'group-control'))
    host._current_run_params = SimpleNamespace(host_channel_actor=actor, task_id='',
        task_attributes={'conversation_thread_id': thread_id})
    return host


# LLM: 调用方已核真实SDK actor/签名/卡来源；仅从签名准确thread的原origin唯一绑定恢复topic，歧义/跨owner拒绝，不读attrs/latest。
# 函数用途: 为私聊模型表单恢复原群或话题发起人，保存仍核原身份/签名，配置不再要求监控grant。
def bind_group_interaction_host(base, host, payload, actor_id):
    host.delivery_service = getattr(base, 'delivery_service', None)
    if getattr(host.home_paths, 'owner_kind', '') != 'group':
        return host
    if not actor_id or payload.get('actor_id') != actor_id:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    from ..conversation.execution_threads import execution_origin_thread

    thread_id = payload.get('thread_id', '')
    source = execution_origin_thread(host.conversation_store.threads.require,
        host.conversation_store.threads.require(thread_id))
    conversations = {binding.channel_conversation_id for binding in source.channel_bindings
        if binding.channel == payload.get('channel')}
    if source.owner_id != host.home_paths.owner_id or len(conversations) != 1:
        raise GroupAuthorizationError('GROUP_AUTHORIZATION_INVALID')
    scope = SimpleNamespace(channel=payload.get('channel'), conversation_id=next(iter(conversations)),
        user_id=actor_id, metadata={'channel_message_id': payload.get('control_id', '')})
    return bind_group_control_host(base, host, scope, thread_id)


# LLM: 最终人格仓库按实际目标逆解析owner，消费的是原配置身份租约而非监控grant；扩大owner_home不能掩盖群目标。
# 函数用途: 保持直接人格写入的原目标身份边界，本地/个人路径不变，不读群授权或联网。
def require_group_persona_lease(target_path):
    from pathlib import Path

    from ..path_access_policy import agent_home_root_for_owner, owner_home_containing
    from .home_layout import home_paths
    from .owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home

    target_path = Path(target_path).resolve()
    root = agent_home_root_for_owner(target_path)
    owner_home = owner_home_containing(target_path, root) if root is not None else None
    if owner_home is None:
        return
    parts = owner_home.relative_to(root / 'owners').parts
    if len(parts) != 4 or parts[0] != 'providers' or parts[2] != 'groups':
        return
    identity = OwnerIdentity.provider_group(parts[1], parts[3])
    home = home_paths_with_owner(home_paths(root), resolve_owner_home(root, identity))
    require_group_mutation_lease(home, 'assistant.configure')
