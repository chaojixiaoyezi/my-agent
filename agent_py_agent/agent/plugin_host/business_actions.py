"""原Plugin授权对象到统一业务服务的窄适配，不替换AuthorizedCall。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib import import_module, util
from types import SimpleNamespace

from .authorization import revalidate_call
from .business_action_specs import (
    business_action_spec,
    business_native_action,
    business_plugin_actions,
)
from .contracts import DomainResult
from .conversations import read_context, thread_state
from .errors import HostAPIError
from .schemas import canonical_json, thaw

_STREAM_MODULE = 'agent_py_agent.agent.gateway_parts.business_event_stream'


# LLM: 业务execute能力包括control效果的resume/guidance；未实现原安装受限异步适配前，所有入口共用此闸，不改变原生owner执行。
# 函数用途: 明确标记插件暂不支持的九个可启动工作动作。
def _requires_restricted_async_adapter(action):
    spec = business_action_spec(action)
    return 'execute' in spec.capabilities or action in {'background_agents.resume', 'background_agents.restore'}


# LLM: 只消费原发行调用的规范值，revision更新不改变同一幂等操作；不接受外部另传身份。
# 函数用途: 为插件业务调用和W2来源生成一致、可重放的请求编号。
def _request_id(authorized):
    """沿原安装、动作、实参和幂等键固定请求身份；不把修订号变成另一条请求。"""
    params = thaw(authorized.call.payload)
    params.pop('expected_revision', None)
    return 'business-' + hashlib.sha256(canonical_json([authorized.binding.installation_ref,
        business_native_action(authorized.call.action), params, authorized.call.idempotency_key]).encode()).hexdigest()[:32]


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

    # LLM: 目录只报告真实接线；执行动作在受限异步适配存在前不可用，冷/热Gateway均相同。
    # 函数用途: 保留读取/控制接线，明确关闭插件异步执行。
    def ready(self, action):
        """报告原Gateway上的静态接线，不以暖Agent或某次Owner运行依赖代替接线状态。"""
        from ..gateway_parts.business_api import _COMPLETE_HOST_ACTIONS, complete_host_wired

        if self.resources.root is None or action not in business_plugin_actions():
            return False
        if action != 'business.events.stream' and _requires_restricted_async_adapter(business_native_action(action)):
            return False
        base = self._base()
        # 多进程协调器只有控制面、没有业务Agent：W2控制在静态接线上即不可用，与调用路径同为HOST_NOT_READY。
        if (base is not None and action != 'business.events.stream'
                and business_native_action(action) in _COMPLETE_HOST_ACTIONS
                and not complete_host_wired(self.resources.server)):
            return False
        # 显式关闭仍即时生效；真实调用的权限、领域ready和W14流依赖门保持原位。
        return getattr(getattr(base, 'config', None), 'business_spaces_enabled', None) is not False

    # LLM: 保留原发行对象；每次授权均重新读有效owner并解析canonical目标，不把business能力提升成owner全域。
    #   控制宿主提供者只绑原AuthorizedCall与已核身份；租用前重读live上下文，资源交集仍由同一check强制。
    # 函数用途: 为同一安装建立带原资源范围和宿主actor工厂的业务调用上下文。
    def context(self, authorized):
        from ..gateway_parts.business_api import (
            BusinessServices,
            bind_business_context,
            complete_host_wired,
            owner_complete_host,
        )
        from ..user_space.home_layout import home_paths
        from ..user_space.owner_resolver import home_paths_with_owner
        from .action_specs import required_capabilities
        current = read_context(authorized)
        base = self._base()
        if base is None:
            raise HostAPIError('HOST_NOT_READY', 503)
        scoped_home = home_paths_with_owner(home_paths(self.resources.root), current.owner_home)
        wired = complete_host_wired(self.resources.server)
        host = SimpleNamespace(home_paths=scoped_home, config=base.config,
            conversation_store=self.resources.store_for(current), business_complete_host_wired=wired)
        host.business_approval_decider = self.approve
        # 共享服务必须另行按owner绑定；绝不把main的Agent/执行器或跨owner业务实例复制进来。
        if getattr(getattr(base, 'home_paths', None), 'owner_id', None) == scoped_home.owner_id:
            for name in ('business_reference_reader', 'business_active_references', 'business_governance',
                         'business_background', 'business_capabilities', 'business_projection', 'business_subscriptions',
                         'business_context_provider', 'workflow_engine_factory', 'workflow_agent_steps',
                         'business_space_reference_resolver', 'business_workflow_reference_reader',
                         'task_business_context', '_business_host_ports_wired'):
                if hasattr(base, name):
                    setattr(host, name, getattr(base, name))
        from ..gateway_parts.business_host_ports import (
            is_business_host_ports_enabled,
            wire_business_host_ports,
        )
        if is_business_host_ports_enabled(base):
            wire_business_host_ports(host)
        key = (scoped_home.owner_id, str(scoped_home.owner_home_dir))
        services = self._services.get(key)
        if (services is None or services.host.config is not base.config
                or services.complete_host_wired != wired
                or getattr(services.host, 'business_subscriptions', None) != getattr(host, 'business_subscriptions', None)
                or services.spaces.enabled != bool(getattr(base.config, 'business_spaces_enabled', False))):
            services = BusinessServices(host)
            if len(self._services) >= 64:
                self._services.pop(next(iter(self._services)))
            self._services[key] = services

        def check(action, facts):
            current = read_context(authorized)
            spec = business_action_spec(action)
            if not required_capabilities(spec.plugin_action, {}) <= authorized.binding.capabilities:
                return False
            if (current.owner_home.owner_id != scoped_home.owner_id
                    or facts.get('owner_id', scoped_home.owner_id) != scoped_home.owner_id):
                return False
            try:
                self._require_resources(authorized, action, facts, current, services)
            except HostAPIError as error:
                if error.status == 404 and spec.metadata['implementer'] == 'W14':
                    raise HostAPIError('OBSERVATION_NOT_FOUND', 404) from None
                raise
            if _requires_restricted_async_adapter(action):
                raise HostAPIError('HOST_NOT_READY', 503)
            return True

        identity, home_dir = current.owner_home.identity, current.owner_home.home_dir

        def complete_host(stack):
            latest = read_context(authorized)
            if latest.owner_home.identity != identity or latest.owner_home.home_dir != home_dir:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            return owner_complete_host(self.resources.server, identity, stack)

        return bind_business_context(host, authorize=check,
            subject_id=authorized.binding.installation_ref, actor=authorized,
            actor_factory=lambda action: self._background_actor(authorized, action, services),
            host_provider=complete_host), services

    # LLM: 线程统一走原require_thread；SessionTask同时核两端、原业务归属和任务范围，不拿展示ID/空间当出处。
    # 函数用途: 验证一个准确任务，并返回它所引用的原线程。
    def _require_task(self, identifier, authorized, current, store):
        task = store.session_tasks.load(identifier)
        if task is None:
            services = self.resources.server.plugin_host.composition.ports.observations.services
            if not services.allows(authorized, 'task', identifier):
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            return {store.tasks.load(identifier).thread_id}
        threads = {task.sender_thread_id, task.target_thread_id}
        rows = {tid: self.conversation.require_thread(store, tid, authorized, current) for tid in threads}
        raw = task.metadata.get('business_context')
        if raw is not None:
            from ..conversation.business_contracts import BusinessExecutionContext
            context = BusinessExecutionContext.from_dict(raw)
            if context.owner_id != current.owner_home.owner_id or context.task_id != task.task_id:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        original = thread_state(rows[task.sender_thread_id]).get('installation_ref') == authorized.binding.installation_ref
        for scope in (authorized.binding.grant['tasks'], authorized.resource_scope['tasks']):
            if identifier not in scope['explicit_ids'] and not (scope['created_by_installation'] is True and original):
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return threads

    # LLM: 只读原SQLite的精确run/attempt关系，再复用原任务授权；历史attempt可读，控制仍由W2核当前attempt。
    # 函数用途: 把运行身份还原到规范线程与任务，不用请求字段猜归属。
    def _require_execution(self, run_id, attempt_id, authorized, current, store, services):
        runtime = getattr(services.observation, 'runtime', None)
        if runtime is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        try:
            if not run_id:
                with runtime._runtime_connection() as conn:
                    row = conn.execute('SELECT ar.run_id FROM agent_runs ar JOIN agent_attempts aa '
                        'ON aa.agent_run_id=ar.agent_run_id WHERE aa.attempt_id=?', (attempt_id,)).fetchone()
                if row is None:
                    raise HostAPIError('RESOURCE_NOT_FOUND', 404)
                run_id = row['run_id']
            ref = runtime.parent_execution_ref(owner_id=current.owner_home.owner_id, run_id=run_id)
            if attempt_id:
                with runtime._runtime_connection() as conn:
                    row = conn.execute('SELECT attempt_id FROM agent_attempts WHERE attempt_id=? AND agent_run_id=?',
                        (attempt_id, ref.agent_run_id)).fetchone()
                if row is None:
                    raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            task_run = runtime.get_task_run(ref.task_run_id)
            root = runtime.get_task(task_run['task_id'])
            self.conversation.require_thread(store, root['thread_id'], authorized, current)
            metadata = json.loads(task_run['metadata_json'])
            task_id = metadata.get('session_task_ref')
            if task_id:
                from ..conversation.business_contracts import BusinessExecutionContext
                context = BusinessExecutionContext.from_dict(metadata['business_context'])
                if context.owner_id != current.owner_home.owner_id or context.task_id != task_id:
                    raise HostAPIError('RESOURCE_NOT_FOUND', 404)
                self._require_task(task_id, authorized, current, store)
                if store.session_tasks.require(task_id).target_thread_id != root['thread_id']:
                    raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            else:
                self._require_task(root['task_id'], authorized, current, store)
        except (KeyError, ValueError, TypeError):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None

    # LLM: 显式过滤和隐式控制目标取交集；缺行只允许已通过原创建范围的写路径继续，既有行绝不重贴出处。
    # 函数用途: 对披露、控制、每条记录及事件使用同一原资源授权谓词。
    def _require_resources(self, authorized, action, facts, current, services):
        from ..conversation.background_agents import BackgroundAgentService

        store = self.resources.store_for(current)
        for selected in (facts, facts.get('filter', {})):
            threads = {selected[key] for key in ('thread_id', 'agent_id', 'controller_agent_id', 'handoff_agent_id')
                if selected.get(key)}
            target = selected.get('target') or {}
            runs = {selected[key] for key in ('run_id', 'parent_run_id') if selected.get(key)}
            if target.get('id'):
                (runs if target.get('kind') == 'subagent' else threads).add(target['id'])
            controller = selected.get('controller') or {}
            if controller.get('agent_id'):
                threads.add(controller['agent_id'])
            if selected.get('root_agent_id'):
                identifier = selected['root_agent_id']
                if store.threads.load(identifier) is not None:
                    threads.add(identifier)
                else:
                    runs.add(identifier)
            for identifier in threads:
                self.conversation.require_thread(store, identifier, authorized, current)
            for key in ('task_id', 'origin_task_id'):
                if selected.get(key):
                    self._require_task(selected[key], authorized, current, store)
            for identifier in runs:
                self._require_execution(identifier, selected.get('attempt_id') if identifier == selected.get('run_id') else None,
                    authorized, current, store, services)
            if selected.get('attempt_id') and not selected.get('run_id'):
                self._require_execution(None, selected['attempt_id'], authorized, current, store, services)
            if (all(key in selected for key in ('thread_id', 'task_id', 'run_id'))
                    and not threads and not runs and not selected.get('task_id')):
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        if action in {'controllers.get', 'controllers.ensure', 'tasks.submit'}:
            identifier = BackgroundAgentService(services.host)._controller_id(SimpleNamespace(owner_id=current.owner_home.owner_id))
            row, error = store.threads.load_report(identifier)
            if error:
                raise HostAPIError('HOST_NOT_READY', 503)
            if row is not None:
                self.conversation.require_thread(store, identifier, authorized, current)
            elif action != 'controllers.get':
                # 原new_thread只构造未落盘值，并同时核两层创建范围。
                self.conversation.new_thread({'title': '业务总控', 'workspace_ref': 'default'}, authorized, current)

    # LLM: 来源线程按owner/安装稳定命名；只在真实W2写调用的原锁内、两层创建范围仍许可时发表新行。
    # 函数用途: 从真实插件来源构造W2通道身份；读发现、重放与既有其它出处均不创建或重绑定线程。
    def _background_actor(self, authorized, action, services):
        from ..conversation.host_actor import HostChannelActor
        from ..conversation.models import ChannelBinding
        from ..conversation.store_threads import create_thread_atomic
        from ..gateway_parts.io import locked_file_transition

        current = read_context(authorized)
        if action != business_native_action(authorized.call.action):
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if _requires_restricted_async_adapter(action):
            raise HostAPIError('HOST_NOT_READY', 503)
        owner = current.owner_home.owner_id
        installation = authorized.binding.installation_ref
        identifier = 'thread-plugin-business-' + hashlib.sha256(canonical_json(
            [owner, installation, 'business-origin.v1']).encode()).hexdigest()[:32]
        store = self.resources.store_for(current)
        row, error = store.threads.load_report(identifier)
        if error:
            raise HostAPIError('HOST_NOT_READY', 503)
        if row is None:
            if business_action_spec(action).metadata['effect'] == 'read':
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            with locked_file_transition(store.storage.threads_dir / ('.' + identifier)):
                current = read_context(authorized)
                row, error = store.threads.load_report(identifier)
                if error:
                    raise HostAPIError('HOST_NOT_READY', 503)
                if row is None:
                    row = self.conversation.new_thread({'workspace_ref': 'default', 'title': '插件业务来源'},
                        authorized, current)
                    binding = ChannelBinding('plugin', installation, owner, owner, identifier, row.updated_at)
                    row = replace(row, thread_id=identifier, channel_bindings=(binding,))
                    current = read_context(authorized)
                    self._require_resources(authorized, action, thaw(authorized.call.payload), current, services)
                    create_thread_atomic(store.threads, row)
        current = read_context(authorized)
        row = self.conversation.require_thread(store, identifier, authorized, current)
        if (row.status != 'active' or thread_state(row).get('installation_ref') != installation
                or not any(binding.channel == 'plugin' and binding.channel_conversation_id == installation
                    and binding.channel_user_id == owner and binding.thread_id == identifier for binding in row.channel_bindings)):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return HostChannelActor(owner, identifier, _request_id(authorized), 'plugin', installation, owner)

    # LLM: 原AuthorizedCall实时验证后先拒不支持的执行；不能创建actor/thread/task/guidance/wake/workflow，也不能零工具模型兜底。
    # 函数用途: 将原插件调用投递给同一业务服务，不替换issuer对象或重放效果。
    def call(self, authorized):
        from ..gateway_parts.business_api import execute_business
        revalidate_call(authorized)
        action = business_native_action(authorized.call.action)
        read_context(authorized)
        if _requires_restricted_async_adapter(action):
            raise HostAPIError('HOST_NOT_READY', 503)
        context, services = self.context(authorized)
        params = thaw(authorized.call.payload)
        revision = params.pop('expected_revision', None)
        request = {'action': action, 'request_id': _request_id(authorized),
            'params': params}
        if authorized.call.idempotency_key is not None:
            request['idempotency_key'] = authorized.call.idempotency_key
        if revision is not None:
            request['expected_revision'] = revision
        status, result = execute_business(request, context, services)
        if result['error'] is not None:
            # Plugin错误合同只有三种结局：只读无副作用，耐久受理则已发表；未知仍保留原未知。
            effect = {'none': 'not_started', 'running': 'committed'}.get(
                result['effect_outcome'], result['effect_outcome'])
            raise HostAPIError(result['error']['code'], status, effect)
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
