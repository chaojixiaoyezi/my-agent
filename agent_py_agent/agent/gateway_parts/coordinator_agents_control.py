# LLM: 仅原认证管理HTTP活栈调用；复用真实owner资源、原AGENTS仓库/nonce服务及原C短写门，不构造Agent或从回执恢复授权。
# 模块用途: 让冷C中的用户查看和确认自己的规则；业务算法和预览格式与TUI保持同源。
from functools import partial
from types import SimpleNamespace

from ..capability.agents_source_host import build_agents_source_repository
from ..capability.agents_write_service import (
    AgentsWriteService,
    _direct_request,
    parse_agents_value,
)
from ..user_space.owner_access import is_complete_local_admin_owner, resolve_owner_scope_and_access
from .agents_control_service import _agents_control_result, _conversation_key
from .owner_resources import resolve_owner_resource_configuration


# LLM: actor会话只经source固定端口先建立；target复用原显式选择算法，资源配置仍沿唯一owner叶，不能借目标P2写本人thread。
# 函数用途: 为本次规则控制选择原真实操作者和目标仓库，目录初始化仍由各自准确写门负责。
def _actor_resources(source, command, scope):
    from ..user_space.group_authorization_context import bind_group_control_host
    from .agents_control_service import _target_owner_identity
    from .owner_management_control import bind_management_resource

    host, thread = source.prepare_actor_control_thread()
    bind_group_control_host(host, host, scope, thread.thread_id)
    target, _operation, _value = parse_agents_value(command.value)
    identity = _target_owner_identity(host, target)
    selected = host
    if identity is not None:
        home, config, _paths = resolve_owner_resource_configuration(source.context, identity)
        if not home.owner_home_dir.is_dir():
            from ..capability._agents_contract_fields import AgentsContractError
            raise AgentsContractError('AGENTS_SOURCE_OWNER_UNAVAILABLE', 'owner')
        selected = SimpleNamespace(home_paths=home, config=config)
    subject = source.http_origin[7], source.http_origin[3], scope
    writer = bind_management_resource(None, host, selected.home_paths, subject, source)
    return host, selected, thread, writer, target


# LLM: 已claim活HTTP只执行原命令；nonce/精确正文/同actor沿原服务，文件与群策略锁出生都经同源末门，网络/flock等待门外。
# 函数用途: 在原AGENTS领域完成一次查看、预览或准确确认，返回原控制结果和真实会话线程编号。
def execute_coordinator_agents(source, command, scope):
    from ..user_space.group_authorization_context import group_capability_guard

    host, selected, thread, writer, target = _actor_resources(source, command, scope)
    repo = build_agents_source_repository(selected, file_commit=writer)
    _, access = resolve_owner_scope_and_access(host.home_paths, host.config)
    source._require_current()
    request = _direct_request(owner=repo.owner, actor=host.home_paths.owner_id,
        thread_id=_conversation_key(scope.user_id, scope.channel, scope.conversation_id),
        request_id=source.receipt.client_message_id, raw=command.value,
        actor_is_admin=is_complete_local_admin_owner(host.home_paths), full_access=access == 'full-access',
        admin_target_owner=target if repo.owner != host.home_paths.owner_id else '')
    with group_capability_guard(host, 'assistant.configure', thread_id=thread.thread_id,
                                birth_commit=partial(writer, owner_home=host.home_paths.owner_home_dir)):
        details = AgentsWriteService(repo).execute(request)
    source._require_current()
    return _agents_control_result(details, source.receipt.client_message_id), thread.thread_id


# LLM: 只读取本source原actor资源和原准确渠道binding；不get_or_create/latest或从body读取线程，不用于业务授权。
# 函数用途: 即使显式target被拒，也给原completed回执保留已实际解析的会话线程投影。
def coordinator_agents_thread_id(source):
    from ..runtime_errors import DataCorruptionError
    source._require_current()
    if source._actor_control_resources is None:
        return ''
    scope = source.http_origin[5]
    store = source._actor_control_resources[3]
    thread, error = store.threads.resolve_report(channel=scope.channel,
        channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id)
    if error is not None:
        raise DataCorruptionError('原规则会话绑定不可读')
    return thread.thread_id if thread is not None else ''
