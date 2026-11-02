"""原Plugin授权对象到统一业务服务的窄适配，不替换AuthorizedCall。"""
from __future__ import annotations

import hashlib
from importlib import import_module, util
from types import SimpleNamespace

from .authorization import revalidate_call
from .business_action_specs import (
    business_action_spec,
    business_native_action,
    business_plugin_actions,
)
from .contracts import DomainResult, resolve_execution_context
from .conversations import read_context
from .errors import HostAPIError
from .schemas import canonical_json, thaw

_STREAM_MODULE = 'agent_py_agent.agent.gateway_parts.business_event_stream'


def business_stream_available():
    return util.find_spec(_STREAM_MODULE) is not None and callable(
        getattr(import_module(_STREAM_MODULE), 'stream_business_events', None))


def gateway_business_port(conversation):
    from .gateway_composition import GatewayHostResources
    resources = getattr(getattr(conversation, 'store_resolver', None), '__self__', None)
    if not isinstance(resources, GatewayHostResources):
        return None
    backend = getattr(conversation, '_business_port', None)
    if backend is None:
        backend = GatewayBusinessDomain(resources, conversation)
        conversation._business_port = backend
    return backend


class GatewayBusinessDomain:
    def __init__(self, resources, conversation):
        self.resources, self.conversation = resources, conversation
        self._services = {}

    def _base(self):
        server = self.resources.server
        return server.agent if server.agent is not None else getattr(server, 'control_plane', None)

    def ready(self, action):
        """报告原Gateway上的静态接线，不以暖Agent或某次Owner运行依赖代替接线状态。"""
        if self.resources.root is None or action not in business_plugin_actions():
            return False
        base = self._base()
        # 显式关闭仍即时生效；真实调用的权限、领域ready和W14流依赖门保持原位。
        return getattr(getattr(base, 'config', None), 'business_spaces_enabled', None) is not False

    def context(self, authorized):
        from ..gateway_parts.business_api import BusinessServices, bind_business_context
        from ..user_space.home_layout import home_paths
        from ..user_space.owner_resolver import home_paths_with_owner
        from .action_specs import required_capabilities
        current = read_context(authorized)
        base = self._base()
        if base is None:
            raise HostAPIError('HOST_NOT_READY', 503)
        scoped_home = home_paths_with_owner(home_paths(self.resources.root), current.owner_home)
        host = SimpleNamespace(home_paths=scoped_home, config=base.config,
            conversation_store=self.resources.store_for(current))
        host.business_approval_decider = self.approve
        # 共享服务必须另行按owner绑定；绝不把main的Agent/执行器或跨owner业务实例复制进来。
        if getattr(getattr(base, 'home_paths', None), 'owner_id', None) == scoped_home.owner_id:
            for name in ('business_reference_reader', 'business_active_references', 'business_governance',
                         'business_background', 'business_capabilities', 'business_projection',
                         'business_context_provider', 'workflow_engine_factory', 'workflow_agent_steps'):
                if hasattr(base, name):
                    setattr(host, name, getattr(base, name))
        key = (scoped_home.owner_id, str(scoped_home.owner_home_dir))
        services = self._services.get(key)
        if (services is None or services.host.config is not base.config
                or services.spaces.enabled != bool(getattr(base.config, 'business_spaces_enabled', False))):
            services = BusinessServices(host)
            if len(self._services) >= 64:
                self._services.pop(next(iter(self._services)))
            self._services[key] = services

        def check(action, facts):
            revalidate_call(authorized)
            spec = business_action_spec(action)
            if not required_capabilities(spec.plugin_action, {}) <= authorized.binding.capabilities:
                return False
            if facts.get('owner_id', scoped_home.owner_id) != scoped_home.owner_id:
                return False
            if spec.metadata['effect'] == 'execute':
                resolve_execution_context(authorized)
            return True

        return bind_business_context(host, authorize=check,
            subject_id=authorized.binding.installation_ref, actor=authorized), services

    def call(self, authorized):
        from ..gateway_parts.business_api import execute_business
        revalidate_call(authorized)
        action = business_native_action(authorized.call.action)
        context, services = self.context(authorized)
        params = thaw(authorized.call.payload)
        revision = params.pop('expected_revision', None)
        request = {'action': action, 'request_id': 'business-' + hashlib.sha256(canonical_json([
            authorized.binding.installation_ref, action, params, authorized.call.idempotency_key]).encode()).hexdigest()[:32],
            'params': params}
        if authorized.call.idempotency_key is not None:
            request['idempotency_key'] = authorized.call.idempotency_key
        if revision is not None:
            request['expected_revision'] = revision
        status, result = execute_business(request, context, services)
        if result['error'] is not None:
            raise HostAPIError(result['error']['code'], status, result['effect_outcome'])
        return DomainResult(status, result['data'], warnings=tuple(result['warnings']))

    def approve(self, params, context, *, expected_revision, idempotency_key):
        """只桥接原交互首决；没有同Owner W14投影时在任何决定前拒绝。"""
        authorized = context.actor
        revalidate_call(authorized)
        _, services = self.context(authorized)
        projection = services.observation
        if not services._observation_ready('approvals.get'):
            raise HostAPIError('HOST_NOT_READY', 503)
        observations = self.resources.server.plugin_host.composition.ports.observations
        adapter = getattr(observations, 'routes', {}).get('interactions')
        if adapter is None:
            raise HostAPIError('HOST_NOT_READY', 503)
        actor = services.observation_actor(context)
        projection.query('approvals.get', {'approval_id': params['approval_id']}, actor=actor)
        canonical = adapter.tickets.canonical_business_response(authorized)
        adapter.call(canonical)
        try:
            context.check('approvals.decide', params)
            latest = projection.query('approvals.get', {'approval_id': params['approval_id']}, actor=actor)
            return {'approval': latest['approval'], 'state': params['decision']}
        except HostAPIError as error:
            raise HostAPIError(error.code, error.status, 'committed') from None

    def revalidate_stream(self, authorized):
        from ..conversation.business_access import BusinessError
        from ..gateway_parts.business_api import _error_code

        revalidate_call(authorized)
        context, services = self.context(authorized)
        params = thaw(authorized.call.payload)
        context.check('events.list', params)
        projection = services.observation
        if not services._observation_ready('events.list'):
            raise HostAPIError('HOST_NOT_READY', 503)
        # 同一原投影重验当前filter/绑定ACL；心跳不能仅验证激活而漏掉业务撤权。
        try:
            projection.query('events.list', {'filter': params['filter'], 'limit': 1},
                actor=services.observation_actor(context))
        except BusinessError as error:
            code, status, outcome = _error_code(error)
            raise HostAPIError(code, status, outcome) from None

    def stream(self, authorized):
        if not business_stream_available():
            raise HostAPIError('HOST_NOT_READY', 503)
        context, services = self.context(authorized)
        # 仅本请求选择8事件来源缓冲；共享服务的原生128事件配置不被改变。
        stream = import_module(_STREAM_MODULE).stream_business_events(thaw(authorized.call.payload),
            authenticated_context=context, services=SimpleNamespace(observation=services.observation,
                observation_actor=services.observation_actor, stream_transport='plugin'))
        iterator = stream.events()
        try:
            for event in iterator:
                self.revalidate_stream(authorized)
                yield event
        finally:
            close = getattr(iterator, 'close', None)
            if callable(close):
                close()
