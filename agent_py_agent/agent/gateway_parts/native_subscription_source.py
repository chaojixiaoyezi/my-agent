"""原签名推送的窄订阅来源：唯一授权仍是原token，catalog只保存业务绑定。"""
from __future__ import annotations

import hashlib
import os
import time

from ..common.nofollow_fs import open_readonly_file_beneath
from ..conversation.business_access import BusinessActor, BusinessError
from ..conversation.business_bindings import InterfaceBindingService
from ..conversation.managed_subscriptions import ManagedSubscriptionService
from .remote_push_service import push_json


def _refusal(code, reason):
    details = {'reason': reason, 'side_effect_state': 'not_started', 'retry_same_request': False,
        'next_action': '由本Owner在原界面/订阅中核对精确绑定并显式签发订阅令牌；不要借普通推送令牌或换身份重试。'}
    if code in {'SOURCE_ROUTE_UNBOUND', 'SOURCE_ROUTE_AMBIGUOUS', 'SOURCE_SCOPE_MISMATCH'}:
        details.update(resolution_state='operator_action_required',
            required_actions=['interfaces.get', 'interfaces.resolve', 'interfaces.update', 'inbox.tokens.mint'],
            next_action='暂停自动重试。由原Owner经interfaces.get/resolve核对绑定及目标空间；无绑定时用interfaces.bind，目标不明确时用interfaces.update显式选择default_space_id。再核subscriptions.get的space_id/source_ref，并由inbox.tokens.mint为正确目标重新授权。只有确认原副作用not_started，才按通道原重试与去重合同重发，不更换事件身份掩盖未知结果。')
    return BusinessError(code, details=details)


def _route(catalog, source_ref, actor):
    state = catalog._read()
    row = state['bindings'].get(source_ref)
    if row is None or row.get('state') != 'bound':
        raise _refusal('SOURCE_ROUTE_UNBOUND', '来源没有当前有效的唯一界面绑定')
    try:
        resolved = InterfaceBindingService(catalog).call('interfaces.resolve', {'binding_id': source_ref}, actor=actor)
    except BusinessError as error:
        if error.code not in {'BUSINESS_ACCESS_DENIED', 'SPACE_ARCHIVED', 'SPACE_SCOPE_MISMATCH', 'REFERENCE_NOT_FOUND'}:
            raise
        raise _refusal('SOURCE_SCOPE_MISMATCH', '当前来源目标已不可按原授权解析：' + error.code) from error
    binding = resolved['binding']
    # 当前生产入口是已鉴权HTTP推送，不能借一个字符串冒充IM或TUI连接。
    if binding['surface_ref'] != 'gateway':
        raise _refusal('SOURCE_SCOPE_MISMATCH', '签名HTTP来源只能绑定gateway界面')
    if binding['default_space_id'] not in binding['space_ids']:
        raise _refusal('SOURCE_ROUTE_AMBIGUOUS', '来源没有明确的默认空间')
    return {key: binding[key] for key in ('binding_id', 'surface_ref', 'default_space_id', 'revision', 'controller')}


def _active(row):
    return row.get('enabled') is True and (row.get('expires_at') is None or row['expires_at'] > time.time())


def _policy_reader(catalog, workflows):
    """只核正式工作流的管理引用；不执行策略、不批准步骤、不扩大发送或模型工具权限。"""
    from ..assets.workflow_store import normalize_workflow_asset_id
    from ..workflows.business_scope import workflow_state

    def check(row, intent, actor):
        ref = row.get('policy_ref')
        if not isinstance(ref, str) or not ref.startswith('workflow:') or workflows is None:
            raise _refusal('POLICY_AUTHORIZATION_REQUIRED', '需要当前Owner可读的正式workflow:引用，不猜策略类型或批准状态')
        identifier = normalize_workflow_asset_id(ref)
        try:
            workflows._authorize(actor, 'workflows.get', identifier)
            definition = workflows._get(identifier)
            if definition.descriptor.owner_id != catalog.owner_id:
                raise PermissionError('WORKFLOW_OWNER_MISMATCH')
            state = workflow_state(definition)
            if state['lifecycle'] != 'active':
                raise PermissionError('WORKFLOW_NOT_ACTIVE')
            workflows._check_scope(state['scope'], row['space_id'])
        except (KeyError, PermissionError) as error:
            raise _refusal('POLICY_AUTHORIZATION_REQUIRED',
                '原工作流不存在、非本Owner、非active、超出订阅空间或当前主体无读取授权') from error
        return True
    return check


def native_subscription_service(host, catalog, *, workflows=None):
    """默认原生CRUD只识别原界面来源；未签发令牌时诚实显示未连接。"""
    def source(ref, actor):
        route = _route(catalog, ref, actor)
        # 静态配置不能证明远端在线；只有本次成功签名入站会把读取视图提升为connected。
        return {'owner_id': catalog.owner_id, 'source_ref': ref,
                'connection_state': 'unknown', 'surface_ref': route['surface_ref']}
    return ManagedSubscriptionService(catalog, source_resolver=source, policy_check=_policy_reader(catalog, workflows))


def subscription_token_grant(call, *, subscription_id=None, space_id=None):
    """签发只消费本次已授权调用；旧token和旧订阅不猜创建者、不迁移成业务授权。"""
    identifier = subscription_id if subscription_id is not None else call.params.get('subscription_id')
    if identifier is None:
        return {}
    service = call.services.subscriptions
    if service is None:
        raise _refusal('SOURCE_AUTHORITY_REQUIRED', '订阅来源服务未就绪')
    actor = call.services.actor(call.context)
    result = service.execute('subscriptions.get', {'subscription_id': identifier}, actor=actor)
    row = result['subscription']
    stored = service.catalog._read()['subscriptions'][identifier]
    policy_ref = stored.get('policy_ref')
    if policy_ref is not None:
        service.policy_check(stored, None, actor)
    if row['state'] != 'active' or row['space_id'] != (space_id if space_id is not None else call.params.get('space_id')):
        raise _refusal('SOURCE_SCOPE_MISMATCH', '签发空间必须明确等于当前启用订阅空间')
    route = _route(service.catalog, row['source_ref'], actor)
    if route['default_space_id'] != row['space_id']:
        raise _refusal('SOURCE_SCOPE_MISMATCH', '订阅空间与界面默认空间不一致')
    action = 'tasks.assign' if route['controller']['mode'] == 'agent' else 'tasks.submit'
    facts = {'owner_id': call.owner_id, 'space_id': row['space_id'], 'binding_id': route['binding_id'],
             **({'agent_id': route['controller']['agent_id']} if route['controller']['mode'] == 'agent' else {})}
    call.context.check('interfaces.bind', facts)
    call.context.check(action, facts)
    grant = {'owner_id': call.owner_id, 'subscription_id': identifier, 'source_ref': row['source_ref'],
             'binding_id': route['binding_id'], 'space_id': row['space_id'], 'surface_ref': route['surface_ref'],
             **({'policy_ref': policy_ref} if policy_ref is not None else {})}
    return {'business_subscription': grant, 'allowed_source_refs': [row['source_ref']],
            'allowed_subscription_ids': [identifier]}


def prepare_native_subscription(server, authenticated_token, body, stack):
    """凭已鉴权token的显式授权选Owner；body不能签发Owner、主体、界面或权限。"""
    from ..conversation.host_actor import HostChannelActor
    from ..conversation.models import ConversationThread
    from ..conversation.store_threads import create_thread_atomic
    from .business_api import (
        BusinessServices,
        _BusinessBackgroundService,
        bind_business_context,
        owner_complete_host,
    )
    from .remote_push_service import push_owner

    grant = authenticated_token.get('business_subscription')
    keys = {'owner_id', 'subscription_id', 'source_ref', 'binding_id', 'space_id', 'surface_ref'}
    if (not isinstance(grant, dict) or set(grant) not in (keys, keys | {'policy_ref'}) or grant['source_ref'] != grant['binding_id']
            or authenticated_token.get('allowed_target_owners') != [grant['owner_id']]
            or body['target_owner'] != grant['owner_id'] or body['subscription_id'] != grant['subscription_id']
            or body['source_ref'] != grant['source_ref']):
        raise _refusal('SOURCE_AUTHORITY_REQUIRED', '原令牌未显式授权这一订阅和来源')
    host = owner_complete_host(server, push_owner(grant['owner_id']), stack)
    services = BusinessServices(host)
    catalog = services.spaces
    token_id = authenticated_token['token_id']

    token_snapshot = {}

    def current():
        # 同一请求仅缓存原文件快照，不缓存授权结果；每次no-follow重开并核inode/版本，撤销立即失效。
        descriptor = open_readonly_file_beneath(host.home_paths.root, ('remote_push', 'tokens.json'))
        with os.fdopen(descriptor, 'rb') as stream:
            stat = os.fstat(stream.fileno())
            revision = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if token_snapshot.get('revision') != revision:
                raw = stream.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise _refusal('SOURCE_AUTHORITY_REQUIRED', '原令牌文件超过读取预算')
                rows = [row for row in push_json(raw).get('tokens', []) if row.get('token_id') == token_id]
                token_snapshot.update(revision=revision, rows=rows)
        rows = token_snapshot['rows']
        if (len(rows) != 1 or not _active(rows[0]) or rows[0].get('business_subscription') != grant
                or rows[0].get('allowed_target_owners') != [grant['owner_id']]
                or rows[0].get('token_sha256') != authenticated_token.get('token_sha256')
                or rows[0].get('allowed_actions') != ['push_event']
                or rows[0].get('allowed_source_refs') != [grant['source_ref']]
                or rows[0].get('allowed_subscription_ids') != [grant['subscription_id']]):
            raise _refusal('SOURCE_AUTHORITY_REQUIRED', '原订阅令牌已撤销、到期或范围已变化')
        return True

    def authorize(action, facts):
        current()
        allowed = {'spaces.list', 'spaces.get', 'interfaces.resolve', 'subscriptions.get', 'subscriptions.update', 'tasks.submit', 'tasks.assign'}
        if action == 'workflows.get':
            from ..assets.workflow_store import normalize_workflow_asset_id

            return (current() and isinstance(grant.get('policy_ref'), str)
                and facts.get('workflow_ref') == normalize_workflow_asset_id(grant['policy_ref']))
        target_matches = ('agent_id' not in facts or
            route['controller'].get('agent_id') == facts['agent_id'])
        return (action in allowed and target_matches and facts.get('owner_id', grant['owner_id']) == grant['owner_id']
            and all(facts.get(key, grant[key]) == grant[key]
                    for key in ('space_id', 'binding_id', 'source_ref', 'subscription_id')))

    actor = BusinessActor(grant['owner_id'], 'source:' + token_id, catalog, authorize)
    source_service = native_subscription_service(host, catalog, workflows=services.workflows)
    route = _route(catalog, grant['source_ref'], actor)
    if route['default_space_id'] != grant['space_id'] or route['surface_ref'] != grant['surface_ref']:
        raise _refusal('SOURCE_SCOPE_MISMATCH', '原界面路由已改变，需由Owner重新授权')

    def source_authorizer(identity, row, current_actor):
        return (current() and current_actor is actor and identity.owner_id == grant['owner_id']
            and identity.principal_id == token_id and identity.source_ref == grant['source_ref']
            and row['subscription_id'] == grant['subscription_id'] and row['space_id'] == grant['space_id']
            and row.get('policy_ref') == grant.get('policy_ref'))

    def submit_actor(current_actor, row):
        current()
        # 服务来源无私人/IM channel_binding，也不冻结真人HostActor给模型；工具权限仍是执行身份原配置。
        digest = hashlib.sha256((grant['owner_id'] + ':' + grant['subscription_id'] + ':' + grant['source_ref']).encode()).hexdigest()
        thread_id = 'thread_source_' + digest[:32]
        store = host.conversation_store
        thread = store.threads.load(thread_id)
        provenance = {key: grant[key] for key in ('owner_id', 'subscription_id', 'source_ref')}
        if thread is None:
            create_thread_atomic(store.threads, ConversationThread(thread_id=thread_id,
                owner_id=grant['owner_id'], owner_home=str(host.home_paths.owner_home_dir),
                canonical_user_id='source:' + token_id, title='订阅事件来源',
                metadata={'host_subscription_source.v1': provenance}))
        elif thread.owner_id != grant['owner_id'] or thread.metadata.get('host_subscription_source.v1') != provenance:
            raise _refusal('SOURCE_SCOPE_MISMATCH', '原服务来源线程归属冲突')
        return HostChannelActor(grant['owner_id'], thread_id, 'source-event:' + body['event_id'],
                                'remote_push', '', 'source:' + token_id)

    class NativeSubscription(ManagedSubscriptionService):
        def prepare_event(self, subscription_id, event, *, actor, source_identity):
            intent = super().prepare_event(subscription_id, event, actor=actor, source_identity=source_identity)
            selected = _route(catalog, grant['source_ref'], actor)
            if selected != route:
                raise _refusal('SOURCE_INTENT_CONFLICT', '本次事件准备后界面路由已变化')
            return {**intent, 'route_snapshot': selected}

    class RoutedBackground:
        def submit(self, request, *, actor, idempotency_key):
            current()
            if _route(catalog, grant['source_ref'], source_actor) != route:
                raise _refusal('SOURCE_INTENT_CONFLICT', '执行前界面绑定已变化')
            context = bind_business_context(host, authorize=authorize, subject_id=source_actor.subject_id)
            background = _BusinessBackgroundService(services, context, host=host, binding_id=grant['binding_id'])
            context.check('tasks.assign' if route['controller']['mode'] == 'agent' else 'tasks.submit',
                {'owner_id': grant['owner_id'], 'space_id': grant['space_id'], 'binding_id': grant['binding_id'],
                 **({'agent_id': route['controller']['agent_id']} if route['controller']['mode'] == 'agent' else {})})
            if route['controller']['mode'] == 'agent':
                from ..conversation.background_team_lifecycle import background_identity

                target = background._thread(route['controller']['agent_id'], actor)
                project = background_identity(target).project_ref
                result = background.assign({**request, 'agent_id': target.thread_id,
                    **({'project_ref': project} if project is not None else {})},
                    actor=actor, idempotency_key=idempotency_key)
                return {'task_id': result['task_id'], 'request_id': result['task_id'],
                        'operation_id': result['task_id'], 'state': 'accepted'}
            return background.submit(request, actor=actor, idempotency_key=idempotency_key)

    source_actor = actor
    def connected_source(ref, current_actor):
        current()
        return {**source_service.source_resolver(ref, current_actor), 'connection_state': 'connected'}

    service = NativeSubscription(catalog, background_service=RoutedBackground(),
        source_resolver=connected_source, source_authorizer=source_authorizer,
        submit_actor_provider=submit_actor, policy_check=_policy_reader(catalog, services.workflows))
    return service, actor
