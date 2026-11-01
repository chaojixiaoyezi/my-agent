"""统一业务边界。只适配原领域，不持有第二份任务、批准、执行或幂等账。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakSet

from ..conversation.background_agents import BackgroundAgentService
from ..conversation.business_access import BusinessActor, BusinessError
from ..conversation.business_spaces import BusinessSpaceService
from ..plugin_host.business_action_specs import (
    business_action_spec,
    business_action_specs,
    business_schema_bundle,
)
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import thaw, validate_object

_ISSUED = WeakSet()


@dataclass(frozen=True, eq=False)
class BusinessCallContext:
    host: object = field(repr=False)
    owner_id: str
    subject_id: str
    authorize: object = field(repr=False)
    actor: object = field(default=None, repr=False)
    business_context: object = field(default=None, repr=False)
    actor_factory: object = field(default=None, repr=False)

    def check(self, action, facts):
        if self not in _ISSUED or not callable(self.authorize):
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if (self.owner_id != self.host.home_paths.owner_id
                or facts.get('owner_id', self.owner_id) != self.owner_id):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        if self.authorize(action, dict(facts)) is not True:
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if action not in {'system.capabilities', 'model_profiles.list'} and not getattr(self.host.config, 'business_spaces_enabled', False):
            raise HostAPIError('HOST_NOT_READY', 503)
        return True


def bind_business_context(host, *, authorize, subject_id='host', actor=None, business_context=None, actor_factory=None):
    """仅可信宿主调用；JSON、复制DTO和展示allowed_actions都不能恢复此活跃身份。"""
    context = BusinessCallContext(host, host.home_paths.owner_id, subject_id, authorize, actor, business_context, actor_factory)
    _ISSUED.add(context)
    return context


class _BusinessBackgroundService(BackgroundAgentService):
    """只桥接W1真实空间读口；线程、CAS、任务与幂等仍归原W2。"""

    def __init__(self, services, context):
        super().__init__(services.host)
        self.spaces = services.spaces
        self.business_actor = services.actor(context)

    def _space(self, space_id, actor):
        self._actor(actor)
        space = self.spaces.get(space_id, actor=self.business_actor)['space']
        if space['space_id'] != space_id or space['state'] != 'active':
            raise BusinessError('RESOURCE_NOT_FOUND')
        return space

    def _profile(self, requested=None):
        from ..settings.thread_model_selection import default_model_profile_id

        # 仅缺省选择可继承部署引用；显式坏引用继续由原模型目录拒绝，不新建档案。
        if requested is None and not default_model_profile_id(self.host) and getattr(self.host.config, 'model_backend', None):
            return 'default'
        return super()._profile(requested)

    def _dispatch_locked(self, request, target, *, actor, idempotency_key, origin_task_id):
        from ..capability.config import CapabilityConfig
        from ..capability.runtime_config_reload import capability_config_for_agent
        from ..conversation.background_team_lifecycle import background_identity_allows_work
        from ..conversation.business_context import resolve_business_context
        from ..conversation.business_contracts import BusinessContextIntent
        from ..conversation.session_task_dispatch import (
            SessionTaskDispatchPlan,
            _task_dedupe_key,
            dispatch_session_task,
            session_task_pair_limit_error,
        )

        # 两种actor各守其门：W2核通道线程，W1解析空间；不把通道DTO伪装成BusinessActor。
        self._actor(actor)
        self._space(request['space_id'], actor)
        config = capability_config_for_agent(self.host) or CapabilityConfig()
        plan = SessionTaskDispatchPlan(actor.source_thread_id, target.thread_id, request['instruction'],
            origin_task_id, self._operation_key(actor, idempotency_key))
        previous = self.store.session_tasks.find_by_dedupe_key(_task_dedupe_key(plan))
        if previous is None and not background_identity_allows_work(target):
            raise ValueError('AGENT_NOT_AVAILABLE')
        if previous is None and (failure := session_task_pair_limit_error(
                self.host, plan.sender_thread_id, plan.target_thread_id, config)):
            raise ValueError(failure.error_code)
        intent = BusinessContextIntent(actor.owner_id, request['space_id'], request.get('project_ref'), None)

        def factory(task_id):
            return resolve_business_context(actor=self.business_actor, task_id=task_id,
                requested_space_id=intent.requested_space_id, project_ref=intent.project_ref,
                binding_id=intent.binding_id, requested_method_refs=intent.requested_method_refs,
                requested_asset_version_refs=intent.requested_asset_version_refs)

        outcome = dispatch_session_task(self.host, plan, target, config, count_pair=previous is None,
            business_context_intent=intent, business_context_factory=factory)
        if not outcome.ok:
            raise RuntimeError(outcome.error_code or 'TASK_DISPATCH_FAILED')
        return json.loads(outcome.output)


class _BackgroundPort:
    """每次调用新建领域适配值，禁止缓存上次HTTP调用者的actor或授权回调。"""

    def __init__(self, services):
        self.services = services

    def execute(self, action, params, *, actor=None, context=None, expected_revision=None, idempotency_key=''):
        if type(context) is not BusinessCallContext or context not in _ISSUED:
            raise BusinessError('ACTOR_REQUIRED')
        context.check(action, {'owner_id': self.services.owner_id, **params})
        if actor is None and callable(context.actor_factory):
            actor = context.actor_factory(action)
        service = _BusinessBackgroundService(self.services, context)
        try:
            service._actor(actor)
            return self._call(service, action, params, actor, expected_revision, idempotency_key)
        except (KeyError, FileNotFoundError) as error:
            raise BusinessError('RESOURCE_NOT_FOUND') from error
        except PermissionError as error:
            raise BusinessError('BUSINESS_ACCESS_DENIED') from error

    def _call(self, service, action, params, actor, revision, key):
        options = {'actor': actor, 'idempotency_key': key}
        if action == 'controllers.get':
            return service.get_controller(actor=actor)
        if action == 'controllers.ensure':
            return service.ensure_controller(actor=actor)
        methods = {'background_agents.create': service.create, 'tasks.submit': service.submit,
                   'tasks.assign': service.assign, 'tasks.follow_up': service.follow_up}
        if action in methods:
            return methods[action](params, **options)
        if action == 'background_agents.list':
            return service.list(params, actor=actor)
        if action == 'background_agents.get':
            return service.get(params['agent_id'], actor=actor)
        if action == 'background_agents.update':
            return service.update(params['agent_id'], {name: value for name, value in params.items() if name != 'agent_id'},
                expected_revision=revision, **options)
        if action in {'background_agents.suspend', 'background_agents.resume', 'background_agents.retire', 'background_agents.restore'}:
            return service.transition(params['agent_id'], action.rsplit('.', 1)[1], expected_revision=revision,
                disposition=params.get('disposition', 'drain'), handoff_agent_id=params.get('handoff_agent_id'), **options)
        if action == 'background_agents.model.get':
            return service.get_model(params['agent_id'], actor=actor)
        if action == 'background_agents.model.set':
            return service.set_model(params['agent_id'], params['model_profile_id'], expected_revision=revision, **options)
        if action == 'tasks.cancel':
            return service.cancel_task(params['task_id'], actor=actor, include_descendants=params['include_descendants'])
        # 这些端口依赖准确执行身份；不在本层制造子代理、run或任务快照。
        from ..conversation.background_agents import cancel_business_execution
        from ..conversation.session_messaging import (
            control_business_subagent,
            queue_owned_background_message,
        )

        if action == 'background_agents.messages.send':
            target = service._thread(params['agent_id'], actor)
            if params.get('reply_to_message_id'):
                message, errors = service.store.messages.by_id_report(target.thread_id, params['reply_to_message_id'])
                if errors or message is None or message.thread_id != target.thread_id:
                    raise BusinessError('RESOURCE_NOT_FOUND')
            return queue_owned_background_message(service.host, actor=actor, target=target, text=params['text'],
                input_kind=params['input_kind'], operation_key=service._operation_key(actor, key))
        if action in {'subagents.messages.send', 'subagents.cancel'}:
            return control_business_subagent(service.host, service, action, params, **options)
        if action == 'executions.cancel':
            return cancel_business_execution(service.host, service, params, actor=actor)
        raise HostAPIError('UNSUPPORTED_ACTION')


class BusinessServices:
    """静态选择原服务。可选端口由组合根注入，绝不从payload或任意函数名加载。"""
    def __init__(self, host, *, spaces=None, workflows=None, background=None, capabilities=None, projection=None):
        self.host = host
        self.owner_id = host.home_paths.owner_id
        self.owner_home = Path(host.home_paths.owner_home_dir)
        self.spaces = spaces or BusinessSpaceService(self.owner_home, owner_id=self.owner_id,
            enabled=bool(getattr(host.config, 'business_spaces_enabled', False)),
            reference_resolver=getattr(host, 'business_reference_reader', None),
            active_references=getattr(host, 'business_active_references', None),
            governance=getattr(host, 'business_governance', None))
        if workflows is None:
            from ..workflows.business_scope import WorkflowScopeService
            workflows = WorkflowScopeService(self.owner_home, owner_id=self.owner_id,
                authorize=lambda actor, action, ref: actor.authorize(action, {'workflow_ref': ref}),
                reference_reader=getattr(host, 'business_reference_reader', None),
                context_provider=getattr(host, 'business_context_provider', None),
                agent_steps=getattr(host, 'workflow_agent_steps', None),
                governance=getattr(host, 'business_governance', None),
                engine_factory=getattr(host, 'workflow_engine_factory', None))
        self.workflows = workflows
        self.background = background or getattr(host, 'business_background', None)
        if self.background is None and self.spaces.enabled and getattr(host, 'conversation_store', None) is not None:
            self.background = _BackgroundPort(self)
        self.capabilities = capabilities or getattr(host, 'business_capabilities', None)
        self.projection = projection or getattr(host, 'business_projection', None)
        self.approvals = getattr(host, 'business_approval_decider', None)

    def ready(self, action):
        spec = business_action_spec(action)
        if action in {'system.capabilities', 'model_profiles.list'}:
            return True
        if not bool(getattr(self.host.config, 'business_spaces_enabled', False)):
            return False
        if action == 'operations.get':
            return callable(getattr(self.projection, 'query', None))
        implementer = spec.metadata['implementer']
        if implementer == 'W1':
            if action.startswith('policies.') or action == 'approvals.decide':
                return (callable(self.approvals) and callable(getattr(self.projection, 'query', None))
                        if action == 'approvals.decide' else self.spaces.governance is not None)
            if action == 'spaces.archive':
                return callable(self.spaces.active_references)
            if action.startswith(('interfaces.', 'assets.')) or action == 'spaces.packages.mount':
                return callable(self.spaces.reference_resolver)
            return bool(self.spaces.enabled)
        if implementer == 'W3':
            if spec.metadata['effect'] == 'execute':
                return callable(self.workflows.context_provider) and callable(self.workflows.engine_factory)
            return self.workflows is not None
        if implementer == 'W2':
            return callable(getattr(self.background, 'execute', None))
        if implementer == 'W6':
            return action == 'spaces.packages.catalog' or callable(getattr(self.capabilities, 'execute', None))
        return callable(getattr(self.projection, 'query', None))

    def actor(self, context):
        return BusinessActor(self.owner_id, context.subject_id, self.spaces,
            lambda action, facts: context.check(action, facts),
            content_authorizer=getattr(context.actor, 'content_authorizer', None))

    def supported(self, context):
        result = []
        for action in business_action_specs():
            if self.ready(action):
                try:
                    context.check(action, {'owner_id': self.owner_id})
                except HostAPIError:
                    continue
                result.append(action)
        return result

    def invoke(self, action, params, context, *, expected_revision=None, idempotency_key=''):
        spec = business_action_spec(action)
        actor = self.actor(context)
        kwargs = {'expected_revision': expected_revision, 'idempotency_key': idempotency_key}
        if action == 'approvals.decide' and callable(self.approvals):
            return self.approvals(params, context, **kwargs)
        if action == 'system.capabilities':
            return {'schema_version': business_schema_bundle()['schema_version'], 'supported_actions': self.supported(context),
                'transport_modes': ['native_json', 'plugin_v2_call'],
                'limits': {'page_default': 50, 'page_max': 100, 'id_max_utf8_bytes': 128,
                    'cursor_max_chars': 2048, 'log_page_max_bytes': 262144, 'upload_max_bytes': 16777216,
                    'stream_buffer_max_events': 8}}
        if action == 'model_profiles.list':
            from ..plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers
            return ModelSettingsV2Handlers(self.host.home_paths).business_profiles(params)
        implementer = spec.metadata['implementer']
        if implementer == 'W1':
            return self.spaces.call(action, params, actor=actor, **kwargs)
        if implementer == 'W3':
            return self.workflows.execute(action, params, actor=actor, **kwargs)
        if implementer == 'W2':
            background_context = context if isinstance(self.background, _BackgroundPort) else context.business_context
            return self.background.execute(action, params, actor=context.actor, context=background_context, **kwargs)
        if implementer == 'W6':
            backend = self.capabilities
            if backend is None:
                from ..capability.business_capabilities import BusinessCapabilityService
                backend = BusinessCapabilityService(self.host)
            return backend.execute(action, params, context=context.business_context)
        return self.projection.query(action, params, actor=context)


def _response(request_id, *, data=None, error=None, accepted=False, effect='none'):
    return {'request_id': request_id, 'operation_id': data.get('operation_id') if data else None,
        'state': 'failed' if error else ('accepted' if accepted else 'completed'),
        'data': data or {}, 'error': error, 'allowed_actions': [], 'warnings': [], 'effect_outcome': effect}


def _error_code(error):
    if isinstance(error, HostAPIError):
        return error.code, error.status, error.outcome
    code = getattr(error, 'error_code', None) or getattr(error, 'code', None)
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', code):
        code = 'HOST_NOT_READY'
    status = (409 if code in {'REVISION_CONFLICT', 'IDEMPOTENCY_CONFLICT'} else
              404 if code.endswith('_NOT_FOUND') or code == 'SPACE_SCOPE_MISMATCH' else
              403 if code in {'ACTOR_REQUIRED', 'BUSINESS_ACCESS_DENIED', 'HUMAN_RESPONSE_REQUIRED'} else
              400 if code == 'INVALID_SCHEMA' else 503)
    return code, status, 'not_started'


def execute_business(request, context, services):
    """先校验原ActionSpec，再实时复验身份；不在运输层替领域补结果或重放副作用。"""
    request_id = 'invalid-request'
    entered = False
    completed_effect = None
    spec = None
    try:
        if type(request) is not dict or type(request.get('action')) is not str:
            raise HostAPIError('INVALID_SCHEMA')
        spec = business_action_spec(request['action'])
        request = validate_object(request, spec.native_request_schema)
        request_id = request['request_id']
        if type(context) is not BusinessCallContext or context not in _ISSUED:
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if (context.owner_id != services.owner_id
                or Path(context.host.home_paths.owner_home_dir) != services.owner_home):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        facts = {'owner_id': services.owner_id, **request['params']}
        context.check(spec.action, facts)
        if not services.ready(spec.action):
            raise HostAPIError('HOST_NOT_READY', 503)
        entered = True
        raw = services.invoke(spec.action, request['params'], context,
            expected_revision=request.get('expected_revision'), idempotency_key=request.get('idempotency_key', ''))
        # 原端口显式返回DomainResult(202,...)才算耐久受理；不凭state字段猜运输状态。
        from ..plugin_host.contracts import DomainResult
        accepted = isinstance(raw, DomainResult) and raw.http_status == 202
        completed_effect = 'none' if spec.metadata['effect'] == 'read' else ('running' if accepted else 'committed')
        data = spec.validate_result(thaw(raw.data) if isinstance(raw, DomainResult) else raw, accepted=accepted)
        context.check(spec.action, facts)
        return (202 if accepted else 200), _response(request_id, data=data, accepted=accepted, effect=completed_effect)
    except (HostAPIError, BusinessError) as error:
        code, status, effect = _error_code(error)
    except (OSError, ValueError, TypeError, RuntimeError, PermissionError):
        mutating = entered and spec is not None and spec.metadata['effect'] != 'read'
        code, status, effect = ('OUTCOME_UNKNOWN', 503, 'unknown') if mutating else ('HOST_NOT_READY', 503, 'not_started')
    if completed_effect is not None and effect == 'not_started':
        # 末核撤权只隐藏返回数据，不把原端口已经完成的提交谎报成未执行。
        effect = completed_effect
    error = {'code': code, 'message': code, 'retryable': status == 503, 'details': []}
    return status, _response(request_id, error=error, effect=effect)


def dispatch_business_action(request: dict, *, authenticated_context: object, services: object) -> dict:
    """冻结公共函数；HTTP状态只在运输包装消费，业务回执保持同一封闭投影。"""
    return execute_business(request, authenticated_context, services)[1]


def native_business_context(handler, server):
    """只消费原HTTP身份并只读装配owner Store；线程创建延至已验证的W2写动作。"""
    from types import SimpleNamespace

    from ..auth.middleware import _handler_peer_ip
    from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
    from .control_service import resolve_gateway_scope_owner
    from .http_handlers import _gateway_control_scope, _request_channel
    base = server.agent if server.agent is not None else getattr(server, 'control_plane', None)
    if base is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    user_id, channel = _request_channel(handler)
    scope = _gateway_control_scope(handler, {}, user_id=user_id, channel=channel)
    owner = resolve_gateway_scope_owner(base, scope)
    home = home_paths_with_owner(base.home_paths, resolve_owner_home(base.home_paths.root, owner))
    host = SimpleNamespace(home_paths=home, config=base.config)
    if getattr(base, 'conversation_store', None) is not None:
        from .owner_conversation_store import owner_conversation_store
        host.conversation_store = owner_conversation_store(base, home, initialize=False)
    if base.home_paths.owner_id == home.owner_id:
        for key in ('business_projection', 'business_reference_reader', 'business_active_references',
                    'business_governance', 'business_background', 'business_capabilities',
                    'business_context_provider', 'workflow_engine_factory', 'workflow_agent_steps', 'subagents'):
            if hasattr(base, key):
                setattr(host, key, getattr(base, key))

    def check(action, facts):
        middleware = getattr(handler, '_auth_middleware', None)
        if middleware is not None:
            if (not middleware.check_trusted(handler.headers, _handler_peer_ip(handler))
                    or middleware.extract_identity(handler.headers, _handler_peer_ip(handler)) != (user_id, channel)):
                return False
        if resolve_gateway_scope_owner(base, scope) != owner:
            return False
        if owner.owner_kind == 'user':
            from ..plugin_host.owner_context import owner_state
            from ..user_space.local_user_store import LocalUserStore
            owner_state(LocalUserStore(home.root), owner)
        # 原批准票据必须走专用人类issuer；本通用JSON上下文不能签出批准。
        return not business_action_spec(action).human_authorization_required

    def background_actor(action):
        from uuid import uuid4

        from ..conversation.host_actor import HostChannelActor

        store = getattr(host, 'conversation_store', None)
        if store is None:
            raise HostAPIError('HOST_NOT_READY', 503)
        binding = {'channel': channel, 'channel_conversation_id': scope.conversation_id,
                   'channel_user_id': user_id}
        source, error = store.threads.resolve_report(**binding)
        if error:
            raise HostAPIError('HOST_NOT_READY', 503)
        if source is None:
            if business_action_spec(action).metadata['effect'] == 'read':
                raise BusinessError('ACTOR_REQUIRED')
            source = store.threads.get_or_create({**binding, 'canonical_user_id': user_id,
                'owner_id': home.owner_id, 'owner_home': str(home.owner_home_dir)})
        if source.owner_id != home.owner_id or source.owner_home != str(home.owner_home_dir):
            raise BusinessError('RESOURCE_NOT_FOUND')
        return HostChannelActor(home.owner_id, source.thread_id, 'business-' + uuid4().hex,
            channel, scope.conversation_id, user_id)

    context = bind_business_context(host, authorize=check, subject_id=user_id, actor_factory=background_actor)
    cache = getattr(server, '_business_services', None)
    if cache is None:
        cache = server._business_services = {}
    key = (home.owner_id, str(home.owner_home_dir))
    if (key not in cache or cache[key].host.config is not base.config
            or cache[key].spaces.enabled != bool(getattr(base.config, 'business_spaces_enabled', False))
            or getattr(getattr(getattr(cache[key].host, 'conversation_store', None), 'storage', None), 'root', None)
            != getattr(getattr(getattr(host, 'conversation_store', None), 'storage', None), 'root', None)):
        if len(cache) >= 64:
            cache.pop(next(iter(cache)))
        cache[key] = BusinessServices(host)
    return context, cache[key]


def _native_source(handler):
    from urllib.parse import urlsplit

    from ..auth.middleware import require_trusted_source
    if require_trusted_source(handler):
        return False
    origin = handler.headers.get('Origin')
    if origin is not None:
        parsed = urlsplit(origin)
        if parsed.scheme not in {'http', 'https'} or parsed.netloc != handler.headers.get('Host'):
            raise HostAPIError('CAPABILITY_DENIED', 403)
    return True


def _native_json(handler):
    from ..plugin_host.schemas import REQUEST_MAX_BYTES, strict_json
    lengths = handler.headers.get_all('Content-Length', [])
    if (len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0])
            or handler.headers.get('Transfer-Encoding') is not None
            or handler.headers.get('Content-Encoding') is not None
            or handler.headers.get_content_type() != 'application/json'):
        raise HostAPIError('INVALID_SCHEMA')
    length = int(lengths[0])
    if length > REQUEST_MAX_BYTES:
        raise HostAPIError('PAYLOAD_TOO_LARGE', 413)
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise HostAPIError('INVALID_SCHEMA')
    return strict_json(raw)


def handle_client_business(handler, server):
    try:
        if not _native_source(handler):
            return
        context, services = native_business_context(handler, server)
        status, response = execute_business(_native_json(handler), context, services)
    except (HostAPIError, BusinessError) as error:
        code, status, effect = _error_code(error)
        response = _response('invalid-request', error={'code': code, 'message': code,
            'retryable': status == 503, 'details': []}, effect=effect)
    handler._send_json(status, response)


def handle_client_business_stream(handler, server):
    """W14同一typed迭代器的原生NDJSON包装；未合入时明确503，不造心跳/事件账。"""
    from importlib import import_module

    from ..plugin_host.business_actions import business_stream_available
    headers_sent = False
    try:
        if not _native_source(handler):
            return
        context, services = native_business_context(handler, server)
        request = validate_object(_native_json(handler), business_schema_bundle()['stream_request_schema'])
        context.check('events.list', request)
        if not business_stream_available():
            raise HostAPIError('HOST_NOT_READY', 503)
        iterator = import_module('agent_py_agent.agent.gateway_parts.business_event_stream').stream_business_events(
            request, authenticated_context=context, services=services)
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        headers_sent = True
        try:
            for event in iterator:
                context.check('events.list', request)
                from ..plugin_host.business_action_specs import standalone_schema
                event = validate_object(event, standalone_schema({'$ref': '#/$defs/EventView'}))
                handler.wfile.write((json.dumps(event, ensure_ascii=False) + '\n').encode())
                handler.wfile.flush()
        finally:
            close = getattr(iterator, 'close', None)
            if callable(close):
                close()
            handler.close_connection = True
    except HostAPIError as error:
        if not headers_sent:
            handler._send_json(error.status, {'error': {'code': error.code}})
        handler.close_connection = True
    except (BrokenPipeError, ConnectionError):
        handler.close_connection = True


def local_business_context(host, *, model=False):
    from ..conversation.host_actor import HostActorError, current_actor_for_child
    actor = None
    if model:
        try:
            actor = current_actor_for_child(host)
        except (HostActorError, ValueError):
            raise HostAPIError('CAPABILITY_DENIED', 403) from None

    def check(action, facts):
        spec = business_action_spec(action)
        if spec.human_authorization_required:
            return False
        if model:
            try:
                current = current_actor_for_child(host)
            except (HostActorError, ValueError):
                return False
            if current != actor or (current is not None and current.owner_id != host.home_paths.owner_id):
                return False
            if current is None and (host.home_paths.owner_provider != 'local' or host.home_paths.owner_kind != 'main'):
                return False
        return facts.get('owner_id', host.home_paths.owner_id) == host.home_paths.owner_id

    return bind_business_context(host, authorize=check, actor=actor), BusinessServices(host)


def business_command_request(namespace, text):
    """固定命令族只规范输入；完整request仍由冻结schema校验，不解析自然语言签权。"""
    import shlex
    from uuid import uuid4
    tokens = shlex.split(text)
    operation = tokens[0] if tokens else 'list'
    aliases = {'space': {'list': 'spaces.list', 'get': 'spaces.get', 'create': 'spaces.create'},
               'team': {'list': 'background_agents.list', 'get': 'background_agents.get'}}
    if operation == 'call':
        request = json.loads(text[len('call'):].strip())
    else:
        if operation not in aliases.get(namespace, {}):
            raise HostAPIError('UNSUPPORTED_ACTION')
        params = {}
        if operation in {'get', 'create'}:
            if len(tokens) != 2:
                raise HostAPIError('INVALID_SCHEMA')
            params['name' if operation == 'create' else ('space_id' if namespace == 'space' else 'agent_id')] = tokens[1]
        elif len(tokens) != 1 and tokens:
            raise HostAPIError('INVALID_SCHEMA')
        request = {'action': aliases[namespace][operation], 'request_id': 'cli-' + uuid4().hex, 'params': params}
        if business_action_spec(request['action']).requires_idempotency:
            # 便捷create只发一次；需要跨进程重试时用call显式携带稳定幂等键。
            request['idempotency_key'] = 'cli-' + uuid4().hex
    return request


def run_business_command(host, namespace, text):
    context, services = local_business_context(host)
    return dispatch_business_action(business_command_request(namespace, text), authenticated_context=context, services=services)


def _model_business_schema(schema):
    """同一ActionSpec的模型子集投影；uniqueItems仍由execute_business原schema强制验证。"""
    result = dict(schema)
    if 'uniqueItems' in result:
        result['x-business-unique-items'] = result.pop('uniqueItems')
    for key in ('properties', '$defs', 'definitions'):
        if key in result:
            result[key] = {name: _model_business_schema(child) for name, child in result[key].items()}
    for key in ('items', 'additionalProperties'):
        if isinstance(result.get(key), dict):
            result[key] = _model_business_schema(result[key])
    for key in ('allOf', 'anyOf', 'oneOf'):
        if key in result:
            result[key] = [_model_business_schema(child) for child in result[key]]
    return result


def register_business_tools(agent):
    """只向原Registry登记已接通动作；原执行、审批、归档和W9诊断链保持唯一。"""
    if not bool(getattr(agent.config, 'business_spaces_enabled', False)):
        return
    from ..tooling.models import (
        BaseTool,
        ConcurrencyPolicy,
        EffectResolverPolicy,
        IdempotencyPolicy,
        OutputPolicy,
        ResourceScopePolicy,
        SandboxPolicy,
        ToolHandlerOutcome,
        ToolModelHints,
        ToolModelSpec,
        ToolRuntimePolicy,
    )

    class BusinessTool(BaseTool):
        def __init__(self, spec):
            self.action = spec.action
            self.model_spec = ToolModelSpec('business_' + spec.action.replace('.', '_'),
                '业务动作 ' + spec.action + '；状态和权限由原宿主复核。', _model_business_schema(spec.native_request_schema),
                hints=ToolModelHints(category='business', default_deferred=True, deferred_summary='业务空间、团队与工作流：' + spec.action))
            effect = {'read': 'read_only', 'configure': 'mutating', 'execute': 'dangerous'}.get(spec.metadata['effect'], 'mutating')
            self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect=effect),
                sandbox_policy=SandboxPolicy('none'), concurrency_policy=ConcurrencyPolicy('serial'),
                idempotency_policy=IdempotencyPolicy('operation'), output_policy=OutputPolicy(trust='runtime'),
                resource_scopes=ResourceScopePolicy(mode='declared', static_scopes=('business:current_owner',)))

        def execute(self, params):
            try:
                if params.get('action') != self.action:
                    raise HostAPIError('INVALID_SCHEMA')
                context, services = local_business_context(agent, model=True)
                status, result = execute_business(params, context, services)
            except HostAPIError as error:
                status, result = error.status, {'error': {'code': error.code}, 'effect_outcome': error.outcome}
            code = (result.get('error') or {}).get('code', '')
            return ToolHandlerOutcome(self.model_spec.name, status < 400, json.dumps(result, ensure_ascii=False),
                error_code=code, reported_error_code=code,
                effect_outcome=result.get('effect_outcome', 'not_started') if status >= 400 else '')

    services = BusinessServices(agent)
    for spec in business_action_specs().values():
        if services.ready(spec.action):
            agent.tools.register(BusinessTool(spec))
