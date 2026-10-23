# LLM: 首次模型菜单只消费原HTTP work/C同对象领域来源；通用观察器不签领域权，wrapper保原错误映射，所有写仍进21原perform，网络Delivery门外。
# 模块用途: 给冷/ready C的/client/models与两个真实文本入口共用原owner配置、ThreadStore和模型目录，不构造Agent、队列或第二许可。
from __future__ import annotations

from dataclasses import replace
from functools import partial
from urllib.parse import urlsplit
from uuid import uuid4

from ..settings.model_profiles import execute_model_profile_operation
from .multiprocess_global_write import GlobalWriteError

# 只列原非网络owner目录操作；OAuth/探测/decision/embedding/共享全局设置不借这个领域入口开放。
_MODEL_OPERATIONS = frozenset({
    'list', 'select', 'set_default', 'add', 'save_profile', 'save_provider',
    'save_model', 'delete_provider', 'delete_model', 'add_models',
})


# LLM: typed control/count只选择原C领域接线，不授权任何写；实际work/Root/lease仍由factory和每次perform完整核验，Count0不进入。
# 函数用途: 区分已有业务Agent的正count C与普通单进程，避免把有Agent误当可调用foreign pool。
def coordinator_model_server(server):
    from .control_plane import GatewayControlPlane
    control = getattr(server, 'control_plane', None)
    return type(control) is GatewayControlPlane and control.worker_count > 0


# LLM: 仅实际HTTP路由及原解析命令决定元数据接线；/ask仍取goal优先prompt，/control取command优先prompt，不接受未知operation或借文本授权限。
# 函数用途: 让两个真实文本入口共用原view/select/set_default元数据合同，client-only与vector仍不开放到文本路径。
def model_metadata_supported(handler, body):
    if urlsplit(handler.path).path == '/client/models':
        return body.get('operation') in _MODEL_OPERATIONS
    route = urlsplit(handler.path).path
    if route not in {'/control', '/ask'}:
        return False
    from ..conversation.control_commands import parse_conversation_control
    text = body.get('goal', body.get('prompt', '')) if route == '/ask' else body.get('command', body.get('prompt', ''))
    command = parse_conversation_control(str(text or ''), reject_unknown_slash=True)
    return command is not None and command.valid and (command.kind == 'model' and command.operation in {'view', 'select', 'set_default'}
        or command.kind == 'effort' and command.operation == 'set')


# LLM: scope只从原认证header/body投影，owner只走原配置解析器；不读请求root/owner字段、不冷建目录或加载pool。
# 函数用途: 为首次菜单及每次末核取得与普通受理相同的唯一owner。
def model_request_scope(handler, server, body):
    from .control_service import _scope_request_payload
    from .http_handlers import _gateway_control_scope, _request_channel
    from .request_worker import _resolve_request_owner_from_config

    if not model_metadata_supported(handler, body):
        raise GlobalWriteError('model_metadata_operation_unavailable', 'model_metadata')
    user_id, channel = _request_channel(handler)
    scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
    if not scope.conversation_id:
        raise ValueError('缺少会话编号。')
    control = server.control_plane
    owner = _resolve_request_owner_from_config(control.config, _scope_request_payload(scope), control.home_root)
    return scope, owner


# LLM: 原实际work、upper/lower/server/control/lease与精确资源均须同对象；新HTTP或新C不能给关闭的旧source补许可。
# 函数用途: 在21原perform最后门内复核首次模型领域身份及资源，不联网、不创建资源。
def require_model_metadata_current(source):
    from ..user_space.owner_resolver import owner_identity_from_config, resolve_owner_home
    require_model_http_work(source._model_work, source.handler, source.server)
    if source._model_scope is not source._model_scope_origin:
        raise GlobalWriteError('model_http_work_changed', 'model_metadata')
    scope, owner = model_request_scope(source.handler, source.server, source.body)
    if scope != source._model_scope or owner != source.owner:
        raise GlobalWriteError('model_http_scope_changed', 'model_metadata')
    host, home, config, store = source._resources
    if (host.home_paths is not home or host.config is not config or host.conversation_store is not store
            or owner_identity_from_config(config) != source.owner
            or (home.owner_home_dir, store.storage.root) != source._paths
            or home.owner_home_dir != resolve_owner_home(source.control.home_root, source.owner).home_dir):
        raise GlobalWriteError('model_resources_changed', 'model_metadata')


# LLM: 资源路径/foreign覆盖清空共21唯一叶；Store只读构造，首次thread默认回调沿原default_model_profile_id，不把空值猜成默认。
# 函数用途: 绑定原模型配置host，所有随后get/create和目录RMW都必须进原source.perform。
def bind_model_resources(source):
    from ..settings.thread_model_selection import default_model_profile_id
    from .model_control_cards import model_control_host_from_resources
    from .owner_conversation_store import owner_conversation_store_from_config
    from .private_interaction_transaction import _owner_resource_configuration

    home, config, _paths = _owner_resource_configuration(source)
    host = model_control_host_from_resources(config, home, None)
    host.conversation_store = owner_conversation_store_from_config(
        config, home, initialize=False, model_default=lambda: default_model_profile_id(host))
    source.bind_model_resources(host)
    return host


# LLM: 首次真实线程/绑定索引沿同一Store原算法并在T外层门内执行；群actor仍只由原G1 scope组装，不给监控grant。
# 函数用途: 创建或解析本次原模型设置会话，保已有默认、F4来源和原owner/channel绑定。
def scoped_model_host(source, scope):
    from ..user_space.group_authorization_context import bind_group_control_host
    host = source._resources[0]

    # LLM: 原get_or_create包含目录和索引写；显式非阻塞透传整个原锁链，不能放到门外作为纯读。
    # 函数用途: 在本次原C短门内取得唯一模型设置线程。
    def prepare():
        thread = host.conversation_store.threads.get_or_create({
            'canonical_user_id': scope.user_id, 'owner_id': host.home_paths.owner_id,
            'owner_home': str(host.home_paths.owner_home_dir), 'channel': scope.channel,
            'channel_conversation_id': scope.conversation_id, 'channel_user_id': scope.user_id,
            'title': '会话设置',
        }, blocking=False)
        return bind_group_control_host(host, host, scope, thread.thread_id), thread
    return source.perform(prepare)


# LLM: 原settings目录/选择CAS是唯一写者；整个同步非网络操作在原source门内，字段和权限仍由原validator处理。
# 函数用途: 为冷菜单与文字控制执行同一个原模型操作。
def model_operation(source, host, operation, payload, thread_id):
    return source.perform(lambda: execute_model_profile_operation(host, operation, payload, thread_id=thread_id, blocking=False))


# LLM: capability来源只用原constructor root/override，不从ownerhome猜；没有实际来源立即拒绝，不把缺省None当授权。
# 函数用途: 读取本次模型卡的原能力配置。
def model_card_capability(source):
    from ..capability.runtime_config_reload import capability_config_for_paths
    if source.control.agent_root is None:
        raise ValueError('model_capability_origin_required')
    return capability_config_for_paths(root=source.control.agent_root, user_path=source.control.capability_config_path)


# LLM: 首次key只在真实来源内创建，随后签名只读；原registry健康取canonical paths且不清PID，发送绝不持C短门。
# 函数用途: 发送第一张原模型选择卡，并在成功后沿同来源发布原active行。
def send_model_selection(source, host, thread_id, target, listing):
    from ..delivery import DeliveryContext, DeliveryService, build_default_channel_registry
    from ..user_space.interaction_refs import interaction_key
    from .channel_health import adapter_runtime_health_for_paths
    from .model_control_cards import _send_model_control_card
    from .model_control_dispatch import ModelCardDelivery

    registry = build_default_channel_registry(host.config, runtime_health_provider=partial(
        adapter_runtime_health_for_paths, source.control.paths, cleanup_stale=False))
    delivery = ModelCardDelivery(DeliveryService(registry), source.perform, source.require_current)
    source.perform(lambda: interaction_key(host.home_paths.owner_home_dir, create=True, blocking=False))
    context = DeliveryContext(channel='feishu', target=target, thread_id=thread_id, mode='reply', idempotency_key=str(uuid4()))
    return _send_model_control_card(host, host, source.owner, source.actor, context, listing, None, delivery)


# LLM: 仅实际HTTP handler调用，同source同时覆盖client和文字model；finally退原pin，原typed同对象上抛，任何失败不构造Agent或补发。
# 函数用途: 完成一个首次模型元数据请求，文字路径继续使用原control receipt状态机。
def execute_coordinator_model_metadata(handler, server, body, *, command=None):
    from .control_operation_service import execute_gateway_control_operation
    from .private_interaction_transaction import _PrivateInteractionTransaction

    source = _PrivateInteractionTransaction.for_model_metadata(handler, server, body)
    primary = None
    try:
        host = bind_model_resources(source)
        scope = replace(source._model_scope, resolved_owner=source.owner)
        if command is not None:
            return execute_gateway_control_operation(None, server.paths, command, scope,
                command_text=str(body.get('command', body.get('prompt', '')) or ''), model_transaction=source)
        host, thread = scoped_model_host(source, scope)
        result = model_operation(source, host, str(body['operation']), body, thread.thread_id)
        source.require_current()
        if body['operation'] == 'list':
            result['model_execution'] = model_execution_snapshot(source, host, scope, thread.thread_id)
        source.require_current()
        return result
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            source.close()
        except BaseException:
            if primary is None:
                raise
            primary.add_note('model_metadata_cleanup_unconfirmed')


# LLM: 原scope来自本次HTTP，命令须与原body解析完全一致；依赖不能把model writer注入别的控制kind或改变已签输入。
# 函数用途: 把本次模型控制精确绑定到唯一原receipt入口。
def require_model_control_source(source, command, scope, command_text):
    from ..conversation.control_commands import parse_conversation_control
    from .private_interaction_transaction import _PrivateInteractionTransaction
    if type(source) is not _PrivateInteractionTransaction:
        raise GlobalWriteError('model_http_source_required', 'model_metadata')
    source.require_current()
    original_text = str(source.body.get('command', source.body.get('prompt', '')) or '')
    original = parse_conversation_control(original_text, reject_unknown_slash=True)
    if (original != command or original_text != command_text or command.kind not in {'model', 'effort'}
            or scope != replace(source._model_scope, resolved_owner=source.owner)):
        raise GlobalWriteError('model_http_scope_changed', 'model_metadata')


# LLM: 只认已有真实异常类，同码普通异常不升级；原HTTP工作/来源失败不得被控制回执catch写成普通完成或再次授权。
# 函数用途: 在模型领域控制的广义错误边界保留实际typed对象与已存部分事实。
def raise_model_transport_error(error):
    from ..backends.bounded_call import BoundedCallExitUnconfirmedError
    from ..common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from ..common.tool_write_context import ToolWriteContextUnavailable
    if isinstance(error, (GlobalWriteError, RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
                          BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable)):
        raise error


# LLM: 只读原精确binding，不get_or_create/重写索引；首次实际effect创建后才补原receipt线程投影，不从latest或外来ID猜。
# 函数用途: 为原控制回执和事件取得当前已存在的规范会话线程编号。
def model_control_thread_id(source, scope):
    from ..runtime_errors import DataCorruptionError
    source.require_current()
    thread, error = source._resources[3].threads.resolve_report(channel=scope.channel,
        channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id)
    if error is not None:
        raise DataCorruptionError('原模型会话绑定不可读')
    return thread.thread_id if thread is not None else ''


# LLM: 只复用原已加载状态投影，不能由菜单加载foreign Agent或把冷W猜成idle；写者仍完全不依赖业务Agent。
# 函数用途: 保留main的真实已加载模型状态，冷C继续返回原unknown/bound=None。
def model_execution_snapshot(source, host, scope, thread_id):
    if source.server.agent is not None:
        from .model_profile_service import _model_execution_snapshot
        return _model_execution_snapshot(source.server.agent, scope, host, thread_id)
    from ..settings.model_scope import thread_model_execution_status
    return thread_model_execution_status(host, thread_id)


# LLM: 只核原work及同upper/lower引用，不签writer/owner权；写者还须21原source与资源/actor末核，GET只可读严格receipt。
# 函数用途: 防止首次模型菜单或状态读取借替换HTTP设施、旧Root或退出工作片。
def require_model_http_work(work, handler, server):
    from .multiprocess_http_transport import HTTPHandlerWorkChanged, require_http_handler_work
    try:
        require_http_handler_work(work, handler, server)
    except HTTPHandlerWorkChanged as error:
        raise GlobalWriteError('model_http_work_changed', 'model_metadata') from error
    except ValueError as error:
        raise GlobalWriteError('model_http_work_unavailable', 'model_metadata') from error


# LLM: capture只认当前真实finish_request工作片；设施失效不能误报canonical receipt损坏，更不能构造替身或取得写权。
# 函数用途: 为只读模型状态捕获同一原HTTP work，固定设施失败码后再读账。
def capture_model_status_work(handler, server):
    from .multiprocess_http_transport import HTTPHandlerWorkChanged, capture_http_handler_work
    try:
        return capture_http_handler_work(handler, server)
    except HTTPHandlerWorkChanged as error:
        raise GlobalWriteError('model_http_work_changed', 'model_metadata') from error
    except ValueError as error:
        raise GlobalWriteError('model_http_work_unavailable', 'model_metadata') from error
