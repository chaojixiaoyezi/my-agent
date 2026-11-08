"""统一业务边界。只适配原领域，不持有第二份任务、批准、执行或幂等账。"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from weakref import WeakSet

from ..conversation.background_agents import BackgroundAgentService
from ..conversation.business_access import BusinessActor, BusinessError
from ..conversation.business_observability import (
    OBSERVATION_ACTIONS,
    BusinessObservationService,
    observation_request,
)
from ..conversation.business_spaces import BusinessSpaceService
from ..conversation.session_task_dispatch import SessionTaskDispatchError
from ..plugin_host.business_action_specs import (
    business_action_spec,
    business_action_specs,
    business_schema_bundle,
    standalone_schema,
)
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import thaw, validate_object

_ISSUED = WeakSet()


# LLM: 冻结动作优先；扩展只解析独立注册器，不修改原冻结目录及能力发现。
# 函数用途: 给所有业务入口解析同一份规范动作与别名。
def resolve_business_action_spec(action):
    try:
        return business_action_spec(action)
    except HostAPIError as error:
        if error.code != 'UNSUPPORTED_ACTION':
            raise
    from .business_api_v13 import v13_action_spec
    return v13_action_spec(action)


# W2控制依赖准确owner的原运行库、创建门、进程/PTY清理与Gateway控制；被动组合只服务读与耐久排队。
_COMPLETE_HOST_ACTIONS = frozenset({
    'background_agents.suspend', 'background_agents.resume', 'background_agents.restore',
    'background_agents.retire', 'tasks.cancel', 'subagents.messages.send', 'subagents.cancel',
    'executions.cancel',
})


@dataclass(frozen=True, eq=False)
class BusinessCallContext:
    host: object = field(repr=False)
    owner_id: str
    subject_id: str
    authorize: object = field(repr=False)
    actor: object = field(default=None, repr=False)
    business_context: object = field(default=None, repr=False)
    actor_factory: object = field(default=None, repr=False)
    host_provider: object = field(default=None, repr=False)

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


def bind_business_context(host, *, authorize, subject_id='host', actor=None, business_context=None, actor_factory=None,
                          host_provider=None):
    """仅可信宿主调用；JSON、复制DTO和展示allowed_actions都不能恢复此活跃身份。"""
    context = BusinessCallContext(host, host.home_paths.owner_id, subject_id, authorize, actor, business_context,
                                  actor_factory, host_provider)
    _ISSUED.add(context)
    return context


# LLM: 只有多进程协调器(无业务Agent)未接完整宿主；接线只是静态事实，调用时仍逐次租用并核准确owner。
# 函数用途: 报告本Gateway组合能否为W2控制提供准确owner完整宿主。
def complete_host_wired(server):
    return getattr(server, 'agent', None) is not None


# LLM: 调用方已完成原live授权并持ExitStack；main沿原Agent，其它owner经原池原子租用，已登记账号仍由原池拒绝。
#   已登记普通账号改由独立准入的受管签发器按启用账号/规范home/当前代次原子租用，不克隆管理员实例。
#   构造/容量/归属失败都在任何账本变更前收为HOST_NOT_READY/not_started，不把未开始的控制报成未知。
# 函数用途: 为一次业务控制取得准确owner完整宿主，并把租用交给调用方栈直至控制结束。
def owner_complete_host(server, owner, stack):
    from .managed_owner_runtime import managed_local_user_id, managed_owner_pool_for_agent
    from .request_worker import _resolve_request_agent_for_owner

    base = getattr(server, 'agent', None)
    if base is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    try:
        if managed_local_user_id(base.home_paths.root, owner) is not None:
            return stack.enter_context(managed_owner_pool_for_agent(base).acquire_pinned(owner))
        return _resolve_request_agent_for_owner(base, owner, stack)
    except RuntimeError:
        raise HostAPIError('HOST_NOT_READY', 503) from None


# LLM: 仅资源不可见返回False，其它live授权或损坏异常继续中止整次披露。
# 函数用途: 为列表和事件复用原资源授权，而不把撤权当成空数据。
def _record_visible(context, action, facts):
    """逐记录过滤只隐藏范围拒绝；坏来源、停用与其它真实故障不冒充空集合。"""
    try:
        return context.check(action, facts)
    except HostAPIError as error:
        if error.status == 404:
            return False
        raise


class _BusinessBackgroundService(BackgroundAgentService):
    """只桥接W1真实空间读口；线程、CAS、任务与幂等仍归原W2。"""

    # LLM: 上下文只保留本次发行身份，不写共享服务或扩张原Actor权限；完整宿主只属本次控制，调用结束即丢弃。
    # 函数用途: 为当前业务调用装配原后台服务和同一实时授权口。
    def __init__(self, services, context, host=None):
        super().__init__(services.host if host is None else host)
        self.services = services
        self.complete_host = host
        # 控制宿主可能是受限owner配置；额度始终沿本Gateway管理员配置，不取普通owner可覆盖项。
        self.quota_config = services.host.config
        self.spaces = services.spaces
        self.call_context = context
        self.business_actor = services.actor(context)

    # LLM: 被动读取不构造Agent；运行详情只读同owner观察运行库，缺来源时沿原宿主路径失败而非伪报空闲。
    # 函数用途: 用规范只读RuntimeDB投影当前running claim的准确执行身份。
    def _execution_view(self, thread):
        runtime = getattr(self.services.observation, 'runtime', None)
        if self.complete_host is not None or runtime is None:
            return super()._execution_view(thread)
        claim, error = self.store.claims.load_report(thread.thread_id)
        if error:
            raise ValueError('CLAIM_UNREADABLE')
        authority = claim.get('runtime_authority')
        if claim.get('status') != 'running':
            return None
        self._validate_execution_claim(thread, claim)
        try:
            ref = runtime.parent_execution_ref(owner_id=thread.owner_id, run_id=authority['run_id'],
                attempt_id=authority['attempt_id'])
            context = runtime.business_context_for_execution(owner_id=thread.owner_id, run_id=ref.run_id,
                attempt_id=ref.observed_attempt_id)
        except sqlite3.Error:
            raise ValueError('EXECUTION_AUTHORITY_UNAVAILABLE') from None
        self._validate_execution_context(thread, authority, ref, context, runtime=runtime)
        return {'run_id': ref.run_id, 'attempt_id': ref.observed_attempt_id, 'task_id': context.task_id if context else None,
                'thread_id': thread.thread_id, 'space_id': context.space_id if context else None, 'status': 'running',
                'effect_outcome': 'running', 'started_at': None, 'ended_at': None, 'error': None, 'retry_refs': []}

    # LLM: R3只对新行调用；插件再次核原发行对象、两层创建范围和真实来源，原生元数据完全不变。
    # 函数用途: 为新建插件后台身份/总控附加原安装出处，不迁移、覆盖或重绑已有线程。
    def _thread_creation_metadata(self, *, actor, metadata: dict) -> dict:
        from ..plugin_host.business_action_specs import business_native_action
        from ..plugin_host.contracts import AuthorizedCall
        from ..plugin_host.conversations import read_context, thread_state

        authorized = self.call_context.actor
        if not isinstance(authorized, AuthorizedCall):
            return super()._thread_creation_metadata(actor=actor, metadata=metadata)
        current = read_context(authorized)
        self.call_context.check(business_native_action(authorized.call.action), thaw(authorized.call.payload))
        source = self._actor(actor)
        installation = authorized.binding.installation_ref
        if (current.owner_home.owner_id != actor.owner_id
                or thread_state(source).get('installation_ref') != installation
                or any(scope.get('created_by_installation') is not True for scope in
                    (authorized.binding.grant['threads'], authorized.resource_scope['threads']))):
            raise HostAPIError('CAPABILITY_DENIED', 403)
        return {**metadata, 'plugin_host_v2': {'installation_ref': installation,
            'revision': 1, 'workspace_ref': 'default'}}

    # LLM: 供继承的后台身份和派活入口复核空间；通道Actor守Owner门，业务Actor走真实空间读口，None直接保留无归属，不写空间或选择默认值。
    # 函数用途: 在Gateway适配层核实指定活跃空间，或接受明确的无归属。
    def _space(self, space_id, actor):
        self._actor(actor)
        if space_id is None:
            return None
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

    # LLM: 直接取消和继承的退休取消共用当前发行上下文；逐条走原task grant与两端线程实时门。
    # 函数用途: 把本次业务授权接入原生取消闭包，不借祖先扩大目标范围。
    def cancel_task(self, task_id, *, actor, include_descendants=False):
        from ..conversation.session_messaging import cancel_owned_session_task

        task, _ = self._owned_task(task_id, actor)
        return cancel_owned_session_task(self.host, task, include_descendants=include_descendants,
            authorize_task=lambda canonical_task: self.call_context.check('tasks.cancel',
                {'owner_id': self.call_context.owner_id, 'task_id': canonical_task.task_id}))

    # LLM: 单次原派发失败仍携原outcome/身份；不解析异常文字，也不重派可能已发表的任务。
    # 函数用途: 把准确业务上下文接入原SessionTask派发，并保留未知回执。
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

        # LLM: 由原SessionTask派发器仅在首次创建时调用；以本次业务Actor只读解析准确task_id的上下文，None显式无归属，不把通道DTO当业务授权或重算重放选择。
        # 函数用途: 为Gateway派活提供与原任务同次冻结的业务上下文。
        def factory(task_id):
            return resolve_business_context(actor=self.business_actor, task_id=task_id,
                requested_space_id=intent.requested_space_id, project_ref=intent.project_ref,
                binding_id=intent.binding_id, requested_method_refs=intent.requested_method_refs,
                requested_asset_version_refs=intent.requested_asset_version_refs,
                unassigned=intent.requested_space_id is None)

        outcome = dispatch_session_task(self.host, plan, target, config, count_pair=previous is None,
            business_context_intent=intent, business_context_factory=factory)
        if not outcome.ok:
            raise SessionTaskDispatchError(outcome)
        return json.loads(outcome.output)


class _BackgroundPort:
    """每次调用新建领域适配值，禁止缓存上次HTTP调用者的actor或授权回调。"""

    def __init__(self, services):
        self.services = services

    # LLM: 工厂只来自已发行宿主上下文；即便actor保留AuthorizedCall，也必须选真实W2身份，不放宽原_actor。
    #   控制在原live授权后经调用局部提供者取准确owner完整宿主，租用跨整个控制并在所有退出路径释放。
    # 函数用途: 使用准确通道身份调用原W2，并按同一实时资源谓词过滤列表中的记录。
    def execute(self, action, params, *, actor=None, context=None, expected_revision=None, idempotency_key=''):
        if type(context) is not BusinessCallContext or context not in _ISSUED:
            raise BusinessError('ACTOR_REQUIRED')
        facts = {'owner_id': self.services.owner_id, **params}
        context.check(action, facts)
        with ExitStack() as stack:
            host = None
            if action in _COMPLETE_HOST_ACTIONS:
                host = self._complete_host(context, stack)
                # 构造或等待租用可能跨越撤权；任何账本变更前按同一原授权再核一次。
                context.check(action, facts)
            if callable(context.actor_factory):
                actor = context.actor_factory(action)
            service = _BusinessBackgroundService(self.services, context, host=host)
            try:
                service._actor(actor)
                result = self._call(service, action, params, actor, expected_revision, idempotency_key)
                if action == 'background_agents.list':
                    result = {**result, 'items': [row for row in result['items']
                        if _record_visible(context, action, {'agent_id': row['agent_id']})]}
                elif action == 'background_agents.get':
                    result = {**result, 'pending_tasks': [row for row in result['pending_tasks']
                        if _record_visible(context, action, {'task_id': row['task_id']})]}
                    current = result.get('current_execution')
                    if current is not None and not _record_visible(context, action, current):
                        result = {**result, 'current_execution': None}
                return result
            except (KeyError, FileNotFoundError) as error:
                raise BusinessError('RESOURCE_NOT_FOUND') from error
            except PermissionError as error:
                raise BusinessError('BUSINESS_ACCESS_DENIED') from error

    # LLM: 无提供者表示可信组合根直接给出的就是owner自身宿主；提供者只能给出同owner、同home、同规范Store的实例。
    #   只有配置投影而无自有Store的组合对象没有独立运行时，沿原规范组合执行；未接线或归属漂移在变更前拒绝。
    # 函数用途: 取得并核验一次控制所用的准确owner完整宿主，返回None表示沿用原组合宿主。
    def _complete_host(self, context, stack):
        if not self.services.complete_host_wired:
            raise HostAPIError('HOST_NOT_READY', 503)
        if not callable(context.host_provider):
            return None
        host = context.host_provider(stack)
        store = getattr(host, 'conversation_store', None)
        if store is None:
            return None
        paths = getattr(host, 'home_paths', None)
        expected = getattr(getattr(self.services.host, 'conversation_store', None), 'storage', None)
        if (getattr(paths, 'owner_id', None) != self.services.owner_id
                or Path(paths.owner_home_dir).resolve() != self.services.owner_home
                or expected is None or Path(store.storage.root).resolve() != Path(expected.root).resolve()):
            raise HostAPIError('HOST_NOT_READY', 503)
        return host

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
    # LLM: 只绑定当前owner原Store与规范只读库；功能关闭不探测观察来源，显式端口不替换，不缓存actor。
    # 函数用途: 按canonical owner-home装配同一owner的业务查询和事件来源。
    def __init__(self, host, *, spaces=None, workflows=None, background=None, capabilities=None, projection=None):
        self.host = host
        self.owner_id = host.home_paths.owner_id
        self.owner_home = Path(host.home_paths.owner_home_dir).resolve()
        self._spaces = spaces
        self._default_spaces = spaces is None
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
        self.background = background if background is not None else getattr(host, 'business_background', None)
        self.capabilities = capabilities if capabilities is not None else getattr(host, 'business_capabilities', None)
        self.projection = projection if projection is not None else getattr(host, 'business_projection', None)
        self._default_projection = self.projection is None
        self.approvals = getattr(host, 'business_approval_decider', None)
        # 被动组合由组合根声明完整宿主接线；直接传入的宿主本身即owner宿主。
        self.complete_host_wired = bool(getattr(host, 'business_complete_host_wired', True))
        self._prepare_live_ports()

    # LLM: 默认领域服务保持同一实例及游标签名，启用值每次取当前宿主配置；显式注入端口不被覆盖。
    # 函数用途: 在请求使用原空间服务时同步开关，不重建业务服务或另存配置快照。
    @property
    def spaces(self):
        if self._default_spaces:
            enabled = bool(getattr(self.host.config, 'business_spaces_enabled', False))
            if self._spaces is None:
                self._spaces = BusinessSpaceService(self.owner_home, owner_id=self.owner_id, enabled=enabled,
                    reference_resolver=getattr(self.host, 'business_reference_reader', None),
                    active_references=getattr(self.host, 'business_active_references', None),
                    governance=getattr(self.host, 'business_governance', None))
            else:
                self._spaces.enabled = enabled
        return self._spaces

    @spaces.setter
    def spaces(self, value):
        self._spaces, self._default_spaces = value, False

    # LLM: 关闭时不探测来源；首次启用只装配原缺失端口，保留显式注入和已有投影身份。
    # 函数用途: 让冷启动时关闭的服务在下一次启用请求中补齐默认只读端口。
    def _prepare_live_ports(self):
        if not self.spaces.enabled or getattr(self.host, 'conversation_store', None) is None:
            return
        if self.background is None:
            self.background = _BackgroundPort(self)
        if self.projection is None:
            from ..observability.concurrency_metrics import owner_model_activity_provider
            from ..runtime_db.repository import RuntimeRepository
            from ..runtime_db.schema import runtime_db_path
            from .business_api_v13_readers import (
                DefaultBusinessObservationService,
                default_observation_readers,
            )

            runtime = RuntimeRepository(runtime_db_path(self.owner_home), read_only=True)
            resource_provider = getattr(self.host, '_business_model_activity_provider', None)
            if resource_provider is None:
                resource_provider = owner_model_activity_provider(self.owner_home, self.owner_id)
            self.projection = DefaultBusinessObservationService(self.owner_home, owner_id=self.owner_id,
                store=self.host.conversation_store, runtime=runtime,
                readers=default_observation_readers(self.owner_home, self.owner_id, self.host.conversation_store, runtime,
                    resource_provider=resource_provider))

    # LLM: 功能关闭不探测来源；显式端口不替换，刷新不建立快照或缓存授权。
    # 函数用途: 在下一请求识别首次创建或已消失的规范来源。
    def refresh_observation_readers(self):
        if not bool(getattr(self.host.config, 'business_spaces_enabled', False)):
            return
        self._prepare_live_ports()
        if self._default_projection and isinstance(self.projection, BusinessObservationService):
            from ..observability.concurrency_metrics import owner_model_activity_provider
            from .business_api_v13_readers import default_observation_readers
            projection = self.projection
            resource_provider = getattr(self.host, '_business_model_activity_provider', None)
            if resource_provider is None:
                resource_provider = owner_model_activity_provider(self.owner_home, self.owner_id)
            projection.readers = default_observation_readers(self.owner_home, self.owner_id,
                projection.store, projection.runtime, resource_provider=resource_provider)

    @property
    def observation(self):
        """查询与stream只有一个来源权威；不维护第二份可漂移的实例。"""
        return self.projection

    def _observation_ready(self, action):
        projection = self.projection
        if not callable(getattr(projection, 'query', None)):
            return False
        if not isinstance(projection, BusinessObservationService):
            return True  # 显式宿主注入的端口保留自身合同。
        if action not in OBSERVATION_ACTIONS:
            return False
        if callable(projection.readers.get(action)):
            return True
        if action in {'tasks.list', 'tasks.get', 'messages.list'}:
            return projection.store is not None
        if action in {'executions.list', 'executions.get', 'subagents.list', 'subagents.get'}:
            return projection.runtime is not None
        if action in {'events.list', 'agent_tree.get'}:
            return projection.store is not None or projection.runtime is not None
        source = {'tool_calls.list': 'tool_calls', 'artifacts.list': 'artifacts',
            'approvals.list': 'approvals'}.get(action)
        if action == 'usage.get':
            return any(callable(projection.readers.get(name)) for name in ('usage', 'resource_usage'))
        return source is not None and callable(projection.readers.get(source))

    # LLM: 关闭功能先拒绝；成本除了公共usage还必须具备内部单窗分组来源，不能虚报能力。
    # 函数用途: 在不读取观察正文的前提下核实准确业务动作接线。
    def ready(self, action):
        spec = resolve_business_action_spec(action)
        if action in {'system.capabilities', 'model_profiles.list'}:
            return True
        if not bool(getattr(self.host.config, 'business_spaces_enabled', False)):
            return False
        self._prepare_live_ports()
        if action == 'operations.get':
            return self._observation_ready(action)
        if action in {'costs.dashboard', 'costs.investigations_aggregate'}:
            from ..business_spaces.v13_w12_metrics import costs_ready
            return costs_ready(self)
        implementer = spec.metadata['implementer']
        if implementer.startswith('V13-'):
            from .business_api_v13 import v13_ready
            return v13_ready(action, self)
        if implementer == 'W1':
            if action.startswith('policies.') or action == 'approvals.decide':
                return (callable(self.approvals) and self._observation_ready('approvals.get')
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
            if action in _COMPLETE_HOST_ACTIONS and not self.complete_host_wired:
                return False
            return callable(getattr(self.background, 'execute', None))
        if implementer == 'W6':
            return action == 'spaces.packages.catalog' or callable(getattr(self.capabilities, 'execute', None))
        return self._observation_ready(action)

    def actor(self, context):
        return BusinessActor(self.owner_id, context.subject_id, self.spaces,
            lambda action, facts: context.check(action, facts),
            content_authorizer=getattr(context.actor, 'content_authorizer', None))

    # LLM: 每次新建绑定确切观察服务的actor；每一帧/查询仍复核原live上下文。
    # 函数用途: 区分空间操作身份和只读观察身份。
    def observation_actor(self, context: BusinessCallContext) -> BusinessActor:
        if (type(context) is not BusinessCallContext or context not in _ISSUED
                or context.owner_id != self.owner_id
                or Path(context.host.home_paths.owner_home_dir).resolve() != self.owner_home):
            raise BusinessError('ACTOR_REQUIRED')
        return BusinessActor(self.owner_id, context.subject_id, self.observation,
            lambda action, facts: _record_visible(context, action, facts))

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

    # LLM: 仅插件取消创建调用局部副本；原生适配器、执行宿主及全部其它依赖保持原对象。
    # 函数用途: 为工作流原任务清理绑定当前实时任务授权，禁止共享handler串用身份。
    def _workflow_cancel_service(self, context):
        from copy import copy

        from ..conversation.session_messaging import cancel_owned_session_task
        from ..workflows.agent_steps import WorkflowAgentSteps
        from ..workflows.business_scope import WorkflowScopeService

        service = self.workflows
        if not isinstance(service, WorkflowScopeService):
            raise HostAPIError('HOST_NOT_READY', 503)
        if service.agent_steps is None:
            return service  # 纯脚本取消不需要Agent执行宿主。
        if not isinstance(service.agent_steps, WorkflowAgentSteps):
            raise HostAPIError('HOST_NOT_READY', 503)
        original = service.agent_steps.background_service

        def cancel_handler(task_id, *, actor, idempotency_key):
            # 只在已知原任务确实需要清理时核宿主；未知派发和已证不存在沿原对账收口。
            if not isinstance(original, BackgroundAgentService):
                raise HostAPIError('HOST_NOT_READY', 503)
            host = original.host
            paths = getattr(host, 'home_paths', None)
            if (getattr(paths, 'owner_id', None) != self.owner_id
                    or context.owner_id != self.owner_id
                    or service.owner_id != self.owner_id
                    or Path(paths.owner_home_dir).resolve() != self.owner_home
                    or Path(context.host.home_paths.owner_home_dir).resolve() != self.owner_home
                    or Path(service.store.owner_home).resolve() != self.owner_home
                    or original.store is not getattr(host, 'conversation_store', None)):
                raise HostAPIError('HOST_NOT_READY', 503)
            task, _ = original._owned_task(task_id, actor)
            return cancel_owned_session_task(host, task, include_descendants=True,
                authorize_task=lambda canonical_task: context.check('tasks.cancel',
                    {'owner_id': context.owner_id, 'task_id': canonical_task.task_id}))

        local = copy(service)
        local.agent_steps = copy(service.agent_steps)
        local.agent_steps.cancel_handler = cancel_handler
        return local

    # LLM: 扩展和冻结动作共用实时context，扩展才消费额外请求编号。
    # 函数用途: 在同一个请求局部数据作用域内按唯一Spec分派原生领域与扩展handler。
    @observation_request()
    def invoke(self, action, params, context, *, expected_revision=None, idempotency_key='', request_id=''):
        spec = resolve_business_action_spec(action)
        if spec.metadata['implementer'].startswith('V13-'):
            from .business_api_v13 import v13_invoke
            return v13_invoke(action, params, context, self, request_id=request_id,
                idempotency_key=idempotency_key, expected_revision=expected_revision)
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
            from ..plugin_host.contracts import AuthorizedCall

            service = (self._workflow_cancel_service(context)
                if action == 'workflow_runs.cancel' and isinstance(context.actor, AuthorizedCall) else self.workflows)
            return service.execute(action, params, actor=actor, **kwargs)
        if implementer == 'W2':
            background_context = context if isinstance(self.background, _BackgroundPort) else context.business_context
            return self.background.execute(action, params, actor=context.actor, context=background_context, **kwargs)
        if implementer == 'W6':
            backend = self.capabilities
            if backend is None:
                from ..capability.business_capabilities import BusinessCapabilityService
                backend = BusinessCapabilityService(self.host)
            return backend.execute(action, params, context=context.business_context)
        if action == 'messages.list':
            from .business_api_v13 import v13_conversation_messages
            conversation = v13_conversation_messages(params, context, self)
            if conversation is not None:
                return conversation
        return self.projection.query(action, params, actor=self.observation_actor(context))


def _response(request_id, *, data=None, error=None, accepted=False, effect='none'):
    return {'request_id': request_id, 'operation_id': data.get('operation_id') if data else None,
        'state': 'failed' if error else ('accepted' if accepted else 'completed'),
        'data': data or {}, 'error': error, 'allowed_actions': [], 'warnings': [], 'effect_outcome': effect}


# LLM: 只读取结构化错误；派发原对象保留私有身份，公开响应不新增字段或虚报未执行。
# 函数用途: 将原领域错误映射为冻结业务错误码、HTTP状态和副作用结果。
def _error_code(error):
    from .business_api_v13 import v13_error_status
    if isinstance(error, HostAPIError):
        return error.code, v13_error_status(error.code) or error.status, error.outcome
    code = getattr(error, 'error_code', None) or getattr(error, 'code', None)
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', code):
        code = 'HOST_NOT_READY'
    status = (409 if code in {'REVISION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'BACKGROUND_AGENT_LIMIT_REACHED'} else
              404 if code.endswith('_NOT_FOUND') or code == 'SPACE_SCOPE_MISMATCH' else
              403 if code in {'ACTOR_REQUIRED', 'BUSINESS_ACCESS_DENIED', 'HUMAN_RESPONSE_REQUIRED'} else
              400 if code == 'INVALID_SCHEMA' else 503)
    status = v13_error_status(code) or status
    effect = 'not_started'
    if isinstance(error, SessionTaskDispatchError):
        effect = error.effect_outcome if error.effect_outcome in {'not_started', 'unknown'} else 'unknown'
    return code, status, effect


# LLM: 只投影原错误显式结构化标量，沿冻结key/value数组与脱敏；不解析异常文案。
# 函数用途: 保留覆盖范围等安全错误细节而不扩张冻结信封。
def _error_details(error):
    from ..common.log_redaction import redact_sensitive_value
    from ..plugin_host.business_action_specs import business_defs
    details = getattr(error, 'details', None)
    if not details:
        return []
    if isinstance(details, dict):
        details = [{'key': key, 'value': value} for key, value in details.items()
            if isinstance(key, str) and (value is None or type(value) in (str, int, float, bool))]
    try:
        schema = standalone_schema({'type': 'object', 'properties': {
            'details': business_defs()['Error']['properties']['details']}, 'required': ['details'],
            'additionalProperties': False})
        return validate_object({'details': redact_sensitive_value(details)}, schema)['details']
    except (HostAPIError, ValueError, TypeError):
        return []


# LLM: 服务可按宿主寿命复用，调用身份仍须本次发行且匹配真实owner与canonical home。
# 函数用途: 经冻结schema、当前授权和领域端口执行一次业务调用。
def execute_business(request, context, services):
    """先校验原ActionSpec，再实时复验身份；不在运输层替领域补结果或重放副作用。"""
    request_id = 'invalid-request'
    entered = False
    completed_effect = None
    spec = None
    details = []
    try:
        if type(request) is not dict or type(request.get('action')) is not str:
            raise HostAPIError('INVALID_SCHEMA')
        spec = resolve_business_action_spec(request['action'])
        submit_alias = False
        if (request['action'] in {'messages.list', 'events.list', 'tasks.submit'}
                and type(request.get('params')) is dict and 'thread_id' in request['params']):
            if type(context) is not BusinessCallContext or context not in _ISSUED:
                raise HostAPIError('CAPABILITY_DENIED', 403)
            from .business_api_v13 import v13_frozen_alias
            request, submit_alias = v13_frozen_alias(request, context, services)
        if not submit_alias:
            request = validate_object(request, spec.native_request_schema)
        request_id = request['request_id']
        if type(context) is not BusinessCallContext or context not in _ISSUED:
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if (context.owner_id != services.owner_id
                or Path(context.host.home_paths.owner_home_dir).resolve() != services.owner_home):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        facts = {'owner_id': services.owner_id, **request['params']}
        context.check(spec.action, facts)
        if not services.ready(spec.action):
            raise HostAPIError('HOST_NOT_READY', 503)
        entered = True
        if submit_alias:
            from .business_api_v13 import v13_submit_alias
            raw = v13_submit_alias(request, context, services)
        else:
            options = {'request_id': request_id} if spec.metadata['implementer'].startswith('V13-') else {}
            raw = services.invoke(spec.action, request['params'], context,
                expected_revision=request.get('expected_revision'), idempotency_key=request.get('idempotency_key', ''),
                **options)
        # 原端口显式返回DomainResult(202,...)才算耐久受理；不凭state字段猜运输状态。
        from ..plugin_host.contracts import DomainResult
        accepted = isinstance(raw, DomainResult) and raw.http_status == 202
        completed_effect = 'none' if spec.metadata['effect'] == 'read' else ('running' if accepted else 'committed')
        data = spec.validate_result(thaw(raw.data) if isinstance(raw, DomainResult) else raw, accepted=accepted)
        context.check(spec.action, facts)
        return (202 if accepted else 200), _response(request_id, data=data, accepted=accepted, effect=completed_effect)
    except (HostAPIError, BusinessError, SessionTaskDispatchError) as error:
        code, status, effect = _error_code(error)
        details = _error_details(error)
    except (OSError, ValueError, TypeError, RuntimeError, PermissionError):
        mutating = entered and spec is not None and spec.metadata['effect'] != 'read'
        code, status, effect = ('OUTCOME_UNKNOWN', 503, 'unknown') if mutating else ('HOST_NOT_READY', 503, 'not_started')
    if completed_effect is not None and effect == 'not_started':
        # 末核撤权只隐藏返回数据，不把原端口已经完成的提交谎报成未执行。
        effect = completed_effect
    error = {'code': code, 'message': code, 'retryable': status == 503, 'details': details}
    return status, _response(request_id, error=error, effect=effect)


def dispatch_business_action(request: dict, *, authenticated_context: object, services: object) -> dict:
    """冻结公共函数；HTTP状态只在运输包装消费，业务回执保持同一封闭投影。"""
    return execute_business(request, authenticated_context, services)[1]


# LLM: 只消费原认证owner并只读装配；关闭功能不探测审批，来源按物理home绑定，不替换显式投影。
# 函数用途: 保持原宿主身份与缓存投影，同时为当前owner绑定可信本地活动源。
def native_business_context(handler, server):
    """只消费原HTTP身份并只读装配owner Store；线程创建延至已验证的W2写动作，控制宿主只在调用内租用。"""
    from types import SimpleNamespace

    from ..auth.middleware import _handler_peer_ip
    from ..observability.concurrency_metrics import owner_model_activity_provider
    from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
    from .control_service import resolve_gateway_scope_owner
    from .http_handlers import _gateway_control_scope, _request_channel
    base = server.agent if server.agent is not None else getattr(server, 'control_plane', None)
    if base is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    user_id, channel = _request_channel(handler)
    # 客户端可用原X-Conversation-Id选择本人已有会话作为W2来源；缺省仍为default，owner/通道/用户由认证头决定。
    conversation = str(handler.headers.get('X-Conversation-Id') or '').strip()
    scope = _gateway_control_scope(handler, {'conversation_id': conversation} if conversation else {},
        user_id=user_id, channel=channel)
    owner = resolve_gateway_scope_owner(base, scope)
    home = home_paths_with_owner(base.home_paths, resolve_owner_home(base.home_paths.root, owner))
    wired = complete_host_wired(server)
    host = SimpleNamespace(home_paths=home, config=base.config, business_complete_host_wired=wired,
        _business_model_activity_provider=(owner_model_activity_provider(home.owner_home_dir, home.owner_id)
            if bool(getattr(base.config, 'business_spaces_enabled', False)) else None))
    # 路径句柄仅供可信只读适配器关联原生push；不接受JSON路径，也不因此授予其它owner权限。
    host.business_v13_gateway_paths = server.paths
    from .request_event_hub import hub_for_paths
    hub = hub_for_paths(server.paths)
    if hub is not None:
        host.business_event_signal_factory = lambda: hub.signal(("owner", home.owner_id))

    # LLM: 只读列表复用 W1 规范 helper；server 与 scope 绑定当前认证请求，不由业务参数选 owner。
    # 函数用途: 给业务会话列表绑定本次请求的原生读取入口。
    def business_session_list(*, limit, cursor, include_inflight):
        from .client_session_list import gateway_client_session_list

        return gateway_client_session_list(server, scope, limit=limit, cursor=cursor,
            include_inflight=include_inflight)

    host.business_session_list = business_session_list
    if getattr(base, 'conversation_store', None) is not None:
        from .owner_conversation_store import owner_conversation_store
        host.conversation_store = owner_conversation_store(base, home, initialize=False)
    if base.home_paths.owner_id == home.owner_id:
        for key in ('business_projection', 'business_reference_reader', 'business_active_references',
                    'business_governance', 'business_background', 'business_capabilities',
                    'business_context_provider', 'workflow_engine_factory', 'workflow_agent_steps', 'subagents',
                    'business_v13_providers', 'business_v13_clock'):
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
        return not resolve_business_action_spec(action).human_authorization_required

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
            if resolve_business_action_spec(action).metadata['effect'] == 'read':
                raise BusinessError('ACTOR_REQUIRED')
            source = store.threads.get_or_create({**binding, 'canonical_user_id': user_id,
                'owner_id': home.owner_id, 'owner_home': str(home.owner_home_dir)})
        if source.owner_id != home.owner_id or source.owner_home != str(home.owner_home_dir):
            raise BusinessError('RESOURCE_NOT_FOUND')
        return HostChannelActor(home.owner_id, source.thread_id, 'business-' + uuid4().hex,
            channel, scope.conversation_id, user_id)

    # LLM: 原生管理员判定留在请求闭包，不能被业务RBAC角色或缓存替代。
    # 函数用途: 复用原认证中间件当前管理员裁决。
    def native_admin():
        middleware = getattr(handler, '_auth_middleware', None)
        if middleware is None:
            return home.owner_provider == 'local' and home.owner_kind == 'main'
        return middleware.require_admin(handler.headers, _handler_peer_ip(handler))[0] is True

    host.business_native_admin = native_admin
    # 提供者只绑定本次已解析的owner；调用时由端口先过同一live check再租用，不缓存宿主或授权。
    context = bind_business_context(host, authorize=check, subject_id=user_id, actor_factory=background_actor,
        host_provider=lambda stack: owner_complete_host(server, owner, stack))
    cache = getattr(server, '_business_services', None)
    if cache is None:
        cache = server._business_services = {}
    key = (home.owner_id, str(home.owner_home_dir))
    if (key not in cache or cache[key].host.config is not base.config
            or cache[key].complete_host_wired != wired
            or cache[key].spaces.enabled != bool(getattr(base.config, 'business_spaces_enabled', False))
            or getattr(getattr(getattr(cache[key].host, 'conversation_store', None), 'storage', None), 'root', None)
            != getattr(getattr(getattr(host, 'conversation_store', None), 'storage', None), 'root', None)):
        if len(cache) >= 64:
            cache.pop(next(iter(cache)))
        cache[key] = BusinessServices(host)
    else:
        cache[key].host = host
        # 默认投影保持原对象和签名密钥，但所有读口须绑定本次可信宿主的同一会话库。
        if cache[key]._default_projection and isinstance(cache[key].projection, BusinessObservationService):
            cache[key].projection.store = getattr(host, 'conversation_store', None)
        cache[key].refresh_observation_readers()
    if (bool(getattr(base.config, 'business_spaces_enabled', False))
            and cache[key]._default_projection and isinstance(cache[key].projection, BusinessObservationService)):
        from .business_api_v13_readers import native_approval_readers
        cache[key].projection.readers.update(native_approval_readers(base, server.paths, home.owner_id,
            getattr(host, 'conversation_store', None)))
    if (getattr(host, 'business_reference_reader', None) is None
            and getattr(host, 'conversation_store', None) is not None
            and ('business_reference_reader' not in getattr(base, '__dict__', {})
                 or getattr(base, 'business_reference_reader', None) is not None)):
        def _default_ref_reader(kind, ref_id, _actor, _store=host.conversation_store):
            if kind != 'task':
                return None
            stask = _store.session_tasks.load(str(ref_id))
            bc = (getattr(stask, 'metadata', None) or {}).get('business_context') if stask is not None else None
            if isinstance(bc, dict):
                return {'owner_id': bc.get('owner_id'), 'space_id': bc.get('space_id'), 'task_id': stask.task_id}
            return None
        host.business_reference_reader = _default_ref_reader
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


# LLM: 首帧仍在handler鉴权并写出；detach后仅独立writer推进和关闭迭代器，失败由未移交方清理。
# 函数用途: 保留原NDJSON协议，并在启用原生流池时移交长连接以释放HTTP线程。
def handle_client_business_stream(handler, server):
    """首帧成功后才发头；可选移交只改变运输所有权，不改变实时授权和事件编码。"""
    import socket
    from importlib import import_module
    from itertools import chain

    from ..plugin_host.business_actions import business_stream_available
    from .business_stream_pool import NativeStreamStopped, stream_services_view
    headers_sent = False
    iterator = None
    ticket = sock = None
    detached_fd = None
    handed_off = False
    try:
        if not _native_source(handler):
            return
        context, services = native_business_context(handler, server)
        request = validate_object(_native_json(handler), standalone_schema(business_schema_bundle()['stream_request_schema']))
        context.check('events.list', request)
        if not business_stream_available():
            raise HostAPIError('HOST_NOT_READY', 503)
        pool = getattr(server, 'business_stream_pool', None)
        if pool is not None:
            home = services.host.home_paths
            ticket = pool.reserve((home.owner_id, str(home.owner_home_dir)))
            if ticket is None:
                # 原JSON发送器没有附加响应头接口，此处保留同样编码并补充重试提示。
                body = json.dumps({'error': {'code': 'GATEWAY_STREAM_BUSY'}}, ensure_ascii=False).encode('utf-8')
                handler.send_response(503)
                handler.send_header('Content-Type', 'application/json; charset=utf-8')
                handler.send_header('Content-Length', str(len(body)))
                handler.send_header('Retry-After', '1')
                handler.send_header('Connection', 'close')
                handler.end_headers()
                headers_sent = True
                handler.wfile.write(body)
                return
        stream = import_module('agent_py_agent.agent.gateway_parts.business_event_stream').stream_business_events(
            request, authenticated_context=context,
            services=stream_services_view(services, ticket))
        iterator = stream.events()
        event_schema = standalone_schema({'$ref': '#/$defs/EventView'})
        try:
            first = validate_object(next(iterator), event_schema)
        except StopIteration:
            raise HostAPIError('CAPABILITY_DENIED', 403) from None
        context.check('events.list', request)
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        headers_sent = True
        if ticket is not None:
            # 校验与编码闭包由原适配器持有，流池不认识业务schema。
            # LLM: 每个后续事件仍重新检查本次请求权限，不能缓存首帧授权结果。
            # 函数用途: 在writer写出前复核原业务授权。
            def check(event):
                context.check('events.list', request)

            # LLM: 沿用原事件schema及NDJSON编码，禁止流池另造帧格式。
            # 函数用途: 校验并编码一个原生业务事件。
            def encode(event):
                event = validate_object(event, event_schema)
                return (json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')

            check(first)
            handler.wfile.write(encode(first))
            handler.wfile.flush()
            handler.close_connection = True
            detached_fd = handler.connection.detach()
            sock = socket.socket(fileno=detached_fd)
            detached_fd = None
            sock.settimeout(pool.write_timeout_seconds)
            ticket.attach(sock)
            pool.submit(ticket, sock, iterator, encode, check)
            handed_off = True
        else:
            for event in chain((first,), iterator):
                context.check('events.list', request)
                event = validate_object(event, event_schema)
                handler.wfile.write((json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8'))
                handler.wfile.flush()
    except (HostAPIError, BusinessError) as error:
        if not headers_sent:
            code, status, _ = _error_code(error)
            handler._send_json(status, {'error': {'code': code}})
    except (NativeStreamStopped, BrokenPipeError, ConnectionError):
        pass
    except (OSError, ValueError, TypeError, RuntimeError):
        if not headers_sent:
            handler._send_json(503, {'error': {'code': 'HOST_NOT_READY'}})
    finally:
        try:
            if not handed_off:
                try:
                    close = getattr(iterator, 'close', None)
                    if callable(close):
                        close()
                finally:
                    try:
                        if sock is not None:
                            try:
                                sock.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            sock.close()
                        elif detached_fd is not None:
                            try:
                                socket.close(detached_fd)
                            except OSError:
                                pass
                    finally:
                        if ticket is not None:
                            ticket.release()
        finally:
            handler.close_connection = True


# LLM: 缓存仅属于这个live host，来源快照保留对象身份而非可变对象的相等比较；不保存actor或授权决定。
# 函数用途: 在owner、canonical home、配置和原服务来源不变时复用本地业务服务。
def _local_business_services(host):
    lock = vars(host).setdefault('_local_business_services_lock', RLock())
    with lock:
        store = getattr(host, 'conversation_store', None)
        storage = getattr(store, 'storage', None)
        root = getattr(storage, 'root', None)
        key = (host.home_paths.owner_id, Path(host.home_paths.owner_home_dir).resolve(),
            bool(getattr(host.config, 'business_spaces_enabled', False)),
            Path(root).resolve() if root is not None else None)
        sources = (host.config, store, storage, getattr(store, 'session_tasks', None),
            getattr(store, 'threads', None), *(getattr(host, name, None) for name in (
                'business_projection', 'business_reference_reader', 'business_active_references',
                'business_governance', 'business_background', 'business_capabilities',
                'business_context_provider', 'workflow_engine_factory', 'workflow_agent_steps',
                'business_approval_decider', 'business_v13_providers', 'business_v13_clock')))
        cached = getattr(host, '_local_business_services_cache', None)
        if (cached is None or cached[0] != key or cached[2].host is not host
                or any(previous is not current for previous, current in zip(cached[1], sources))):
            cached = (key, sources, BusinessServices(host))
            host._local_business_services_cache = cached
        return cached[2]


def _model_business_actor(host):
    """本地业务身份取当前规范线程；外部来源仍沿原冻结发起人校验。"""
    from ..conversation.channels import LOCAL_PRIVATE_CHANNELS
    from ..conversation.execution_threads import execution_origin_thread
    from ..conversation.host_actor import HostChannelActor, current_actor_for_child

    actor = current_actor_for_child(host)
    if actor is not None:
        return actor
    current = getattr(host, '_current_run_params', None)
    thread_id = str((getattr(current, 'task_attributes', None) or {}).get('conversation_thread_id') or '')
    if not thread_id:
        return None
    home = host.home_paths
    if home.owner_provider != 'local' or home.owner_kind not in {'main', 'user'}:
        raise HostAPIError('CAPABILITY_DENIED', 403)
    thread = host.conversation_store.threads.require(thread_id)
    source = execution_origin_thread(host.conversation_store.threads.require, thread)
    if any(row.owner_id != home.owner_id or Path(row.owner_home).resolve() != Path(home.owner_home_dir).resolve()
           for row in (thread, source)):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    binding = next((row for row in source.channel_bindings if row.channel in LOCAL_PRIVATE_CHANNELS), None)
    if binding is None and getattr(current, 'business_context', None) is not None:
        binding = _business_task_sender_binding(host, thread, current.business_context)
    request_id = str(getattr(current, 'request_id', '') or getattr(current, 'run_id', '') or '')
    if binding is None or not request_id:
        raise HostAPIError('CAPABILITY_DENIED', 403)
    return HostChannelActor(home.owner_id, source.thread_id, request_id, binding.channel,
        binding.channel_conversation_id, binding.channel_user_id or home.owner_id)


def _business_task_sender_binding(host, thread, context):
    """后台身份/总控执行原业务SessionTask时，沿原派活发送方的规范绑定恢复宿主身份。"""
    from ..conversation.background_team_lifecycle import background_identity
    from ..conversation.channels import LOCAL_PRIVATE_CHANNELS

    if background_identity(thread) is None and thread.metadata.get('business_controller') is not True:
        return None
    task = host.conversation_store.session_tasks.require(context.task_id)
    sender = host.conversation_store.threads.require(task.sender_thread_id)
    if (task.target_thread_id != thread.thread_id or context.owner_id != host.home_paths.owner_id
            or sender.owner_id != host.home_paths.owner_id):
        raise HostAPIError('CAPABILITY_DENIED', 403)
    return next((row for row in sender.channel_bindings if row.channel in LOCAL_PRIVATE_CHANNELS), None)


def _model_request_binding(host):
    """读取当前宿主的结构化绑定，不在列表逐项授权时重复读取来源文件。"""
    current = getattr(host, '_current_run_params', None)
    home = host.home_paths
    return (getattr(current, 'request_id', ''), getattr(current, 'run_id', ''), getattr(current, 'task_id', ''),
        getattr(current, 'host_channel_actor', None),
        (getattr(current, 'task_attributes', None) or {}).get('conversation_thread_id'),
        home.owner_id, home.owner_provider, home.owner_kind, str(home.owner_home_dir))


# LLM: 每次调用重新读取来源并发行上下文；本地列表逐项只复核当前绑定，外部身份仍沿原实时核验，不缓存跨调用授权。
# 函数用途: 给本地命令和注册模型工具绑定当前调用身份及同宿主业务服务。
def local_business_context(host, *, model=False):
    from ..conversation.channels import LOCAL_PRIVATE_CHANNELS
    from ..conversation.host_actor import HostActorError
    actor = None
    current_run = getattr(host, '_current_run_params', None)
    if model:
        try:
            actor = _model_business_actor(host)
        except (HostActorError, ValueError, KeyError, OSError):
            raise HostAPIError('CAPABILITY_DENIED', 403) from None
    local_actor = actor is not None and actor.channel in LOCAL_PRIVATE_CHANNELS
    local_binding = _model_request_binding(host) if local_actor else None

    def check(action, facts):
        spec = resolve_business_action_spec(action)
        if spec.human_authorization_required:
            return False
        if model:
            if getattr(host, '_current_run_params', None) is not current_run:
                return False
            try:
                if local_actor:
                    if _model_request_binding(host) != local_binding:
                        return False
                    current = actor
                else:
                    current = _model_business_actor(host)
            except (HostActorError, ValueError, KeyError, OSError):
                return False
            if current != actor or (current is not None and current.owner_id != host.home_paths.owner_id):
                return False
            if current is None and (host.home_paths.owner_provider != 'local' or host.home_paths.owner_kind != 'main'):
                return False
        return facts.get('owner_id', host.home_paths.owner_id) == host.home_paths.owner_id

    return bind_business_context(host, authorize=check, actor=actor,
        subject_id=actor.actor_id if actor is not None else 'host'), _local_business_services(host)


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


# LLM: 命令每次重新发行上下文，仅复用同一宿主的owner-bound服务。
# 函数用途: 经统一业务边界执行本地结构化命令。
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


# LLM: 注册与执行共用原宿主服务寿命；注册只看就绪状态，不把它缓存成调用授权。
# 函数用途: 将当前就绪业务动作注册到原模型工具目录。
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
            family = spec.action.split('.')[0]
            title, keywords = {
                'spaces': ('业务空间', ('业务空间', '空间', 'space', 'spaces', '/space')),
                'background_agents': ('后台代理', ('后台代理', '常驻代理', 'background_agents', 'team', '/team')),
                'tasks': ('后台任务', ('后台任务', '派活', '派任务', '分配任务', '布置任务', '交给后台代理', 'tasks')),
            }.get(family, ('业务动作', (spec.action,)))
            verbs = {
                'list': ('列出', '列表', '查询'), 'get': ('查看', '详情', '查询'),
                'create': ('创建', '新建'), 'update': ('修改', '更新'),
                'messages.send': ('发送消息', '发消息', 'send_message'),
                'suspend': ('暂停', '挂起'), 'resume': ('恢复', '继续'),
                'retire': ('退役', '停用'), 'restore': ('还原', '复原'),
                'assign': ('派给', '分配', '执行'), 'submit': ('交给总控', '提交'),
                'follow_up': ('追问', '补充要求', '继续'), 'cancel': ('取消', '停止'),
            }.get(spec.action.partition('.')[2], ()) if family in {'spaces', 'background_agents', 'tasks'} else ()
            purpose = (verbs[0] if verbs else '') + title + '：' + spec.action
            if family == 'tasks' and spec.metadata['effect'] == 'execute':
                purpose += '（异步执行；结束后结果自动回报本会话）'
            self.model_spec = ToolModelSpec('business_' + spec.action.replace('.', '_'),
                purpose + '；直接调用内置工具，状态和权限由当前宿主复核。',
                _model_business_schema(spec.native_request_schema),
                hints=ToolModelHints(category=family if family in {'spaces', 'background_agents', 'tasks'} else 'business',
                    keywords=(*keywords, *verbs), use_cases=(purpose,),
                    default_deferred=True, deferred_summary=purpose))
            effect = {'read': 'read_only', 'configure': 'mutating', 'execute': 'dangerous'}.get(spec.metadata['effect'], 'mutating')
            self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect=effect),
                sandbox_policy=SandboxPolicy('none'), concurrency_policy=ConcurrencyPolicy('serial'),
                idempotency_policy=IdempotencyPolicy('operation'), output_policy=OutputPolicy(trust='runtime'),
                resource_scopes=ResourceScopePolicy(mode='declared', static_scopes=('business:current_owner',)))

        # LLM: 每次执行仍取当前上下文并复核动作，不持有注册时或上一调用的actor。
        # 函数用途: 从注册工具进入同一个本地业务调用边界。
        def execute(self, params):
            try:
                if params.get('action') != self.action:
                    raise HostAPIError('INVALID_SCHEMA')
                context, services = local_business_context(agent, model=True)
                status, result = execute_business(params, context, services)
            except HostAPIError as error:
                status, result = error.status, {'error': {'code': error.code}, 'effect_outcome': error.outcome}
            code = (result.get('error') or {}).get('code', '')
            # 业务none表示只读无副作用；工具合同用not_started表达同一事实。
            # 已提交/运行的业务结果不得误报未执行；JSON原值保留，工具层保守标unknown。
            effect = result.get('effect_outcome', 'not_started')
            tool_effect = 'not_started' if effect == 'none' else effect if effect in {'not_started', 'unknown'} else 'unknown'
            return ToolHandlerOutcome(self.model_spec.name, status < 400, json.dumps(result, ensure_ascii=False),
                result_envelope=result,
                error_code=code, reported_error_code=code,
                effect_outcome=tool_effect if status >= 400 else '')

    services = _local_business_services(agent)
    for spec in business_action_specs().values():
        if services.ready(spec.action):
            agent.tools.register(BusinessTool(spec))
