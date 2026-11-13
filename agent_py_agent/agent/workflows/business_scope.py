"""业务范围是原工作流资产的元数据，不是另一份正文或执行器。

actor 与 authorize/context_provider 只由可信宿主注入；JSON 不能指定它们。
W1/W2 尚未接线时，相关入口明确拒绝，不凭构造 DTO 或展示标签取得权限。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone

from ..assets.descriptor import AssetDescriptor, asset_descriptor_from_definition
from ..assets.workflow_store import (
    BUSINESS_WORKFLOW_METADATA,
    WorkflowDefinition,
    WorkflowOperationConflict,
    WorkflowStore,
)
from ..conversation.business_access import BusinessError
from ..conversation.business_contracts import BusinessExecutionContext, WorkflowScope

# 原领域入口的参数边界；原生工具只适配此入口，W13 目录仍负责完整公开 schema。
WORKFLOW_ACTION_FIELDS = {
    'workflows.create': (('definition',), ('scope',)),
    'workflows.update': (('workflow_ref', 'definition'), ()),
    'workflows.list': ((), ('cursor', 'limit', 'space_id', 'scope_mode', 'lifecycle', 'query')),
    'workflows.get': (('workflow_ref',), ()),
    'workflows.scope.get': (('workflow_ref',), ()),
    'workflows.scope.set': (('workflow_ref', 'scope'), ()),
    'workflows.enable': (('workflow_ref',), ()),
    'workflows.disable': (('workflow_ref',), ()),
    'workflows.preview': (('workflow_ref',), ('space_id', 'inputs')),
    'workflows.run': (('workflow_ref',), ('space_id', 'project_ref', 'inputs')),
    'workflow_runs.list': ((), ('workflow_ref', 'space_id', 'cursor', 'limit')),
    'workflow_runs.get': (('workflow_run_id',), ()),
    'workflow_runs.resume': (('workflow_run_id',), ('resolution_ref',)),
    'workflow_runs.cancel': (('workflow_run_id',), ()),
    'workflows.recycle': (('workflow_ref',), ()),
    'workflows.restore': (('workflow_ref',), ('scope',)),
    'workflows.protection.set': (('workflow_ref', 'protected'), ()),
    'workflows.cleanup.policy.get': ((), ()),
    'workflows.cleanup.policy.set': (('policy',), ()),
    'workflows.cleanup.preview': ((), ('cursor', 'limit')),
    'workflows.cleanup.run': ((), ()),
}
WORKFLOW_READ_ACTIONS = ('workflows.list', 'workflows.get', 'workflows.scope.get', 'workflows.preview',
    'workflow_runs.list', 'workflow_runs.get', 'workflows.cleanup.policy.get', 'workflows.cleanup.preview')
WORKFLOW_NO_CAS_ACTIONS = (*WORKFLOW_READ_ACTIONS, 'workflows.create', 'workflows.run', 'workflows.cleanup.run')


def validate_business_request(action, request, *, expected_revision=None, idempotency_key=''):
    if action not in WORKFLOW_ACTION_FIELDS or not isinstance(request, dict):
        raise ValueError('INVALID_WORKFLOW_ACTION')
    required, optional = WORKFLOW_ACTION_FIELDS[action]
    if set(required) - request.keys() or set(request) - set(required + optional):
        raise ValueError('INVALID_WORKFLOW_PARAMETERS')
    if action not in WORKFLOW_READ_ACTIONS and (not isinstance(idempotency_key, str)
            or not 0 < len(idempotency_key.encode()) <= 256):
        raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
    if action not in WORKFLOW_NO_CAS_ACTIONS and (type(expected_revision) is not int or expected_revision < 0):
        raise ValueError('EXPECTED_REVISION_REQUIRED')
    if 'scope' in request and (not isinstance(request['scope'], dict)
            or set(request['scope']) != {'scope_mode', 'space_ids'}):
        raise ValueError('INVALID_WORKFLOW_SCOPE')
    if 'scope' in request:
        WorkflowScope.from_dict({**request['scope'], 'scope_revision': 0})
    for key in ('workflow_ref', 'workflow_run_id', 'space_id', 'project_ref', 'resolution_ref'):
        if key in request and (not isinstance(request[key], str) or not 0 < len(request[key].encode()) <= 128):
            raise ValueError('INVALID_WORKFLOW_REFERENCE')


def workflow_state(definition):
    """旧无范围记录只在读取时解释为全局，不写迁移或延长孤立期限。"""
    state = json.loads(json.dumps(definition.descriptor.metadata.get(BUSINESS_WORKFLOW_METADATA, {})))
    scope = WorkflowScope.from_dict(state.get('scope', {
        'scope_mode': 'owner_global', 'space_ids': [], 'scope_revision': 0}))
    return {'lifecycle': 'active', 'protected': False, 'orphan_since': None,
            'last_content_edit_at': None, 'last_run_at': None, 'last_restore_at': None,
            **state, 'scope': scope.to_dict()}


def scope_view(scope):
    return {key: scope[key] for key in ('scope_mode', 'space_ids')}


def with_state(definition, state):
    return replace(definition, descriptor=replace(definition.descriptor,
        metadata={**definition.descriptor.metadata, BUSINESS_WORKFLOW_METADATA: state}))


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def workflow_view(definition, state=None):
    state = workflow_state(definition) if state is None else state
    return {'workflow_ref': definition.descriptor.id, 'name': definition.descriptor.name,
        'definition_revision': definition.descriptor.revision,
        'execution_definition_ref': definition.execution_definition_ref,
        'scope_revision': state['scope']['scope_revision'], 'scope': scope_view(state['scope']),
        'lifecycle': state['lifecycle'], 'enabled': definition.enabled,
        'protected': state['protected'], 'definition_ref': definition.descriptor.id}


# LLM: 只投影用途、IO 合同与步骤摘要；字段逐项有界，超出上限或无法校验的部分标 coverage=partial 而不是让详情整体失败。
# 函数用途: 生成 workflows.get 的对象详情（V07-03）。
def workflow_detail_view(definition):
    partial = len(definition.steps) > 100
    summary = str(definition.descriptor.summary or '')
    partial = partial or len(summary) > 4096
    io_contract = _io_contract_view(definition.descriptor.io_contract.to_dict())
    partial = partial or io_contract is None
    steps = []
    for step in definition.steps[:100]:
        depends = list(dict.fromkeys(value['step_id'] for value in step.inputs_map.values()
            if isinstance(value, dict) and isinstance(value.get('step_id'), str) and value['step_id'] != '$inputs'))
        partial = partial or len(depends) > 100 or len(step.name) > 256
        steps.append({'step_id': step.step_id, 'name': step.name[:256], 'mode': step.mode,
            'depends_on': depends[:100], 'conditional': bool(step.condition), 'foreach': step.foreach is not None,
            'on_error': step.on_error})
    return {'summary': summary[:4096], 'io_contract': io_contract, 'steps': steps,
            'step_count': len(definition.steps), 'coverage': 'partial' if partial else 'complete'}


def _io_contract_view(contract):
    # 只接受与公开 IOContract 一致的有界形状；旧记录不合规时返回 None（详情标 partial）。
    try:
        value = json.loads(json.dumps(contract, allow_nan=False))
    except (TypeError, ValueError):
        return None
    if (not isinstance(value, dict) or not set(value) <= {'inputs', 'outputs', 'nullable_outputs', 'output_types'}
            or any(not isinstance(value.get(key, []), list) or len(value.get(key, [])) > 1000
                   for key in ('inputs', 'outputs', 'nullable_outputs'))
            or any(not isinstance(item, str) or len(item) > 4096 for item in value.get('nullable_outputs', []))
            or not isinstance(value.get('output_types', {}), dict)
            or any(not isinstance(item, str) or len(item) > 4096 for item in value.get('output_types', {}).values())):
        return None
    return value


# LLM: 只在任何副作用之前抛出；保留 PermissionError 类型（原调用方与测试不变），业务边界据机器码给出 not_started。
# 类用途: W3 范围/生命周期策略拒绝。
class WorkflowPolicyRefusal(PermissionError):
    def __init__(self, code, *, details=None):
        super().__init__(code)
        self.error_code = self.code = code
        self.details = details or {}


# LLM: 宿主工作流上下文工厂的显式签名合同（actor, request, workflow_run_id, scope）；旧二参提供者按原调用，
#   不靠捕获任意TypeError猜签名。逻辑task_id只用准确运行ID，空间/项目解析复用原只读选择与访问、修订检查。
# 类用途: 为一次原生工作流运行解析冻结上下文；Owner级范围且未请求空间或项目时返回None，不冻结空间。
class WorkflowRunContextFactory:
    def __call__(self, actor, request, *, workflow_run_id, scope):
        if scope.get('scope_mode') != 'spaces' and not request.get('space_id') and not request.get('project_ref'):
            return None
        from ..conversation.business_context import resolve_workflow_run_context
        return resolve_workflow_run_context(actor=actor, workflow_run_id=workflow_run_id,
            requested_space_id=request.get('space_id'), project_ref=request.get('project_ref'))


class WorkflowScopeService:
    """原动作目录与 UI/plugin 共用的 Owner 领域入口；不从请求猜当前业务身份。"""

    def __init__(self, owner_home, *, owner_id, authorize, reference_reader=None, clock=None,
                 context_provider=None, agent_steps=None, governance=None, engine_factory=None):
        self.store = WorkflowStore(owner_home)
        self.owner_id = owner_id
        self.authorize = authorize
        self.reference_reader = reference_reader
        self.clock = clock or time.time
        self.context_provider = context_provider
        self.agent_steps, self.governance = agent_steps, governance
        self.engine_factory = engine_factory

    def _authorize(self, actor, action, workflow_ref=''):
        if (not callable(self.authorize) or getattr(actor, 'owner_id', None) != self.owner_id
                or self.authorize(actor, action, workflow_ref) is not True):
            raise PermissionError('BUSINESS_WORKFLOW_ACCESS_DENIED')

    def _get(self, workflow_ref):
        definition = self.store.get_workflow(workflow_ref)
        if definition is None:
            raise KeyError(workflow_ref)
        if definition.descriptor.owner_id and definition.descriptor.owner_id != self.owner_id:
            raise PermissionError('WORKFLOW_OWNER_MISMATCH')
        return definition

    def _definition(self, value):
        value = json.loads(json.dumps(value, allow_nan=False))
        descriptor = value['descriptor']
        if set(descriptor) & {'owner_id', 'revision', 'runtime_summary', 'used_by', 'scope_layer', 'source_scope', 'workspace_root'}:
            raise ValueError('WORKFLOW_HOST_FIELDS_FORBIDDEN')
        if BUSINESS_WORKFLOW_METADATA in descriptor.get('metadata', {}):
            raise ValueError('WORKFLOW_HOST_FIELDS_FORBIDDEN')
        descriptor = asset_descriptor_from_definition(descriptor, descriptor['id'], 'workflow')
        value['descriptor'] = replace(descriptor, owner_id=self.owner_id).to_dict()
        return WorkflowDefinition.from_any(value)

    def set_scope(self, workflow_ref: str, scope: WorkflowScope, *, actor: object,
                  expected_revision: int, idempotency_key: str) -> dict:
        # scope_revision 由锁内正式关系递增；调用方 DTO 的旧版本不能成为执行授权。
        if not isinstance(scope, WorkflowScope):
            raise TypeError('scope必须为WorkflowScope')
        return self.execute('workflows.scope.set', {'workflow_ref': workflow_ref,
            'scope': scope_view(scope.to_dict())}, actor=actor,
            expected_revision=expected_revision, idempotency_key=idempotency_key)

    def execute(self, action, request, *, actor, expected_revision=None, idempotency_key=''):
        validate_business_request(action, request, expected_revision=expected_revision, idempotency_key=idempotency_key)
        ref = request.get('workflow_ref', '')
        self._authorize(actor, action, ref)
        if action == 'workflows.create':
            return self._create(request, idempotency_key, actor)
        if action == 'workflows.list':
            return self._list(request, actor)
        if action.startswith('workflows.cleanup.'):
            from .orphan_maintenance import cleanup_action
            return cleanup_action(self, action, request, actor=actor, expected_revision=expected_revision,
                                  idempotency_key=idempotency_key)
        if action.startswith('workflow_runs.'):
            return self._run_action(action, request, actor=actor, expected_revision=expected_revision,
                                    idempotency_key=idempotency_key)
        try:
            definition = self._get(ref)
        except KeyError:
            # 未知引用在任何副作用之前确定：给出结构化 404/not_started，而不是让运输层断开连接。
            raise BusinessError('RESOURCE_NOT_FOUND') from None
        state = workflow_state(definition)
        if action == 'workflows.scope.get':
            return {'scope': scope_view(state['scope']), 'scope_revision': state['scope']['scope_revision'],
                    'origin': {'kind': definition.descriptor.source_scope, 'source_ref': ref,
                               'revision': state['scope']['scope_revision']}}
        if action == 'workflows.get':
            from .orphan_maintenance import cleanup_status
            return {'workflow': workflow_view(definition), 'scope': scope_view(state['scope']),
                'run_refs': [row.workflow_run_id for row in self._run_store().list_runs(workflow_id=ref, limit=100)],
                'cleanup_status': cleanup_status(self, definition), 'detail': workflow_detail_view(definition),
                'allowed_actions': [name for name in ('workflows.update', 'workflows.scope.set', 'workflows.run',
                    'workflows.recycle', 'workflows.restore') if self.authorize(actor, name, ref) is True]}
        if action == 'workflows.preview':
            self._check_scope(state['scope'], request.get('space_id'), preview=True)
            return {'plan': [{'step_id': step.step_id, 'mode': step.mode,
                'required_capabilities': ['business.execute', 'execute'],
                'dependencies': list(dict.fromkeys(value['step_id'] for value in step.inputs_map.values()
                    if isinstance(value, dict) and 'step_id' in value and value['step_id'] != '$inputs')),
                'would_execute': True} for step in definition.steps],
                'errors': [], 'required_capabilities': ['business.execute', 'execute'] if definition.steps else []}
        if action == 'workflows.run':
            self._check_scope(state['scope'], request.get('space_id'))
            return self._run(request, actor, idempotency_key)
        return self._mutate(action, request, actor, expected_revision, idempotency_key)

    def _create(self, request, key, actor):
        definition = self._definition(request['definition'])
        scope = WorkflowScope.from_dict({**request.get('scope', {'scope_mode': 'owner_global', 'space_ids': []}),
                                         'scope_revision': 1})

        def transform(previous):
            self._authorize(actor, 'workflows.create', definition.descriptor.id)
            if previous is not None:
                raise BusinessError('WORKFLOW_ALREADY_EXISTS')
            now = self.clock()
            state = {'scope': scope.to_dict(), 'lifecycle': 'active', 'protected': False,
                'orphan_since': now if scope.scope_mode == 'unassigned' else None,
                'last_content_edit_at': now, 'last_run_at': None, 'last_restore_at': None}
            return with_state(definition, state), {'workflow_ref': definition.descriptor.id,
                'definition_revision': 1, 'scope_revision': 1, 'scope': scope_view(scope.to_dict())}

        try:
            return self.store.apply_business_operation(definition.descriptor.id, action='workflows.create',
                intent=request, idempotency_key=key, expected_revision=None, revision_target=None, transform=transform)
        except WorkflowOperationConflict as error:
            raise BusinessError(error.error_code) from None

    def _check_scope(self, scope, space_id, *, preview=False):
        # 范围策略在任何副作用前裁决：结构化拒绝（not_started），不是结果未知。
        if scope['scope_mode'] == 'unassigned' and not preview:
            raise WorkflowPolicyRefusal('WORKFLOW_UNASSIGNED')
        if scope['scope_mode'] == 'spaces' and space_id not in scope['space_ids']:
            raise WorkflowPolicyRefusal('WORKFLOW_SPACE_MISMATCH')

    def _list(self, request, actor):
        rows = []
        for definition in self.store.list_workflows():
            if self.authorize(actor, 'workflows.get', definition.descriptor.id) is not True:
                continue
            state = workflow_state(definition)
            if request.get('lifecycle', 'active') not in ('all', state['lifecycle']):
                continue
            if request.get('scope_mode', 'all') not in ('all', state['scope']['scope_mode']):
                continue
            if request.get('space_id') and state['scope']['scope_mode'] != 'owner_global' and request['space_id'] not in state['scope']['space_ids']:
                continue
            if request.get('query', '') not in definition.descriptor.name:
                continue
            rows.append(workflow_view(definition))
        return self._page(rows, request)

    def _page(self, rows, request):
        limit = request.get('limit', 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit必须为1到100')
        identity = hashlib.sha256(json.dumps([rows, {key: value for key, value in request.items()
            if key not in ('cursor', 'limit')}], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        offset = 0
        if cursor := request.get('cursor'):
            try:
                saved = json.loads(base64.urlsafe_b64decode(cursor))
                if saved['snapshot'] != identity or type(saved['offset']) is not int or saved['offset'] < 0:
                    raise ValueError('LIST_SNAPSHOT_EXPIRED')
                offset = saved['offset']
            except (ValueError, TypeError, KeyError):
                raise ValueError('LIST_SNAPSHOT_EXPIRED') from None
        end = offset + limit
        cursor = base64.urlsafe_b64encode(json.dumps({'snapshot': identity, 'offset': end}).encode()).decode() if end < len(rows) else None
        return {'items': rows[offset:end], 'next_cursor': cursor, 'list_snapshot_id': identity,
                'coverage': 'complete'}

    def _mutate(self, action, request, actor, revision, key):
        ref = request['workflow_ref']
        supported = ('workflows.scope.set', 'workflows.update', 'workflows.enable', 'workflows.disable',
                     'workflows.recycle', 'workflows.restore', 'workflows.protection.set')
        if action not in supported:
            raise ValueError('UNKNOWN_WORKFLOW_ACTION')
        definition = self._get(ref)
        if definition.descriptor.source_scope != 'owner' or definition.descriptor.scope_layer != 'owner':
            raise WorkflowPolicyRefusal('WORKFLOW_READ_ONLY')
        replacement = self._definition(request['definition']) if action == 'workflows.update' else None
        if replacement is not None and replacement.descriptor.id != ref:
            raise BusinessError('WORKFLOW_REFERENCE_MISMATCH')

        def transform(previous):
            self._authorize(actor, action, ref)
            if previous is None:
                raise KeyError(ref)
            state = workflow_state(previous)
            current = previous
            now = self.clock()
            if action in ('workflows.scope.set', 'workflows.restore'):
                mode = request.get('scope', {'scope_mode': 'unassigned', 'space_ids': []})
                if set(mode) != {'scope_mode', 'space_ids'}:
                    raise ValueError('INVALID_WORKFLOW_SCOPE')
                scope = WorkflowScope.from_dict({**mode, 'scope_revision': state['scope']['scope_revision'] + 1})
                old_mode = state['scope']['scope_mode']
                state['scope'] = scope.to_dict()
                if scope.scope_mode != 'unassigned':
                    state['orphan_since'] = None
                elif old_mode != 'unassigned' or action == 'workflows.restore':
                    state['orphan_since'] = now
                if action == 'workflows.restore':
                    state.update(lifecycle='active', last_restore_at=now)
            elif action == 'workflows.update':
                if (replacement.execution_definition_ref != previous.execution_definition_ref
                        or replacement.body_markdown != previous.body_markdown
                        or replacement.descriptor.name != previous.descriptor.name):
                    state['last_content_edit_at'] = now
                    if state['scope']['scope_mode'] == 'unassigned':
                        state['orphan_since'] = now
                current = replacement
            elif action in ('workflows.enable', 'workflows.disable'):
                enabled = action == 'workflows.enable'
                if enabled and state['lifecycle'] != 'active':
                    raise WorkflowPolicyRefusal('WORKFLOW_RECYCLED')
                current = replace(previous, enabled=enabled, descriptor=replace(previous.descriptor, enabled=enabled))
            elif action == 'workflows.protection.set':
                if type(request['protected']) is not bool:
                    raise ValueError('protected必须为布尔值')
                state['protected'] = request['protected']
            elif action == 'workflows.recycle':
                from .orphan_maintenance import cleanup_status
                status = cleanup_status(self, previous)
                if status['blockers']:
                    # 引用存在或覆盖未知都在写入前拒绝；首个阻断原因让客户端区分“被引用”与“覆盖未知”。
                    raise WorkflowPolicyRefusal('WORKFLOW_REFERENCED_OR_COVERAGE_UNKNOWN', details={
                        'blocker_reason_code': status['blockers'][0]['reason_code'],
                        'blocker_count': len(status['blockers'])})
                state['lifecycle'] = 'recycled'
            current = with_state(current, state)
            current = replace(current, descriptor=replace(current.descriptor, revision=previous.descriptor.revision + 1))
            if action == 'workflows.scope.set':
                result = {'scope': scope_view(state['scope']), 'scope_revision': state['scope']['scope_revision'], 'affected_runs': []}
            elif action == 'workflows.update':
                result = {'workflow': workflow_view(current), 'definition_revision': current.descriptor.revision, 'affected_runs': []}
            elif action in ('workflows.enable', 'workflows.disable'):
                result = {'workflow': workflow_view(current), 'affected_runs': []}
            elif action == 'workflows.protection.set':
                result = {'workflow_ref': ref, 'protected': state['protected'], 'revision': current.descriptor.revision}
            elif action == 'workflows.recycle':
                result = {'workflow_ref': ref, 'lifecycle': 'recycled', 'blocked_refs': []}
            else:
                from .orphan_maintenance import cleanup_status
                result = {'workflow': workflow_view(current), 'scope': scope_view(state['scope']),
                          'cleanup_status': cleanup_status(self, current)}
            return current, result

        try:
            return self.store.apply_business_operation(ref, action=action, intent=request, idempotency_key=key,
                expected_revision=revision, revision_target='scope' if action == 'workflows.scope.set' else 'definition',
                transform=transform)
        except WorkflowOperationConflict as error:
            raise BusinessError(error.error_code) from None

    def _run_store(self):
        from .run_store import WorkflowRunStore
        return WorkflowRunStore(self.store.owner_home)

    def health_get(self, request, *, actor):
        """单工作流只读健康；授权在helper外完成，拒绝不能被聚合器吞成空成功。"""
        from .query_health import build_workflow_health_view

        if (type(request) is not dict or set(request) - {'workflow_ref', 'limit'}
                or not isinstance(request.get('workflow_ref'), str)
                or not 0 < len(request['workflow_ref'].encode()) <= 128
                or type(request.get('limit', 50)) is not int or not 1 <= request.get('limit', 50) <= 100):
            raise ValueError('INVALID_WORKFLOW_HEALTH_PARAMETERS')
        ref, limit = request['workflow_ref'], request.get('limit', 50)

        def check_workflow():
            self._authorize(actor, 'workflows.get', ref)
            try:
                self._get(ref)
            except KeyError:
                raise BusinessError('RESOURCE_NOT_FOUND') from None

        def check_run(record):
            if record.owner_id != self.owner_id or record.workflow_id != ref:
                raise PermissionError('WORKFLOW_OWNER_OR_REFERENCE_MISMATCH')
            self._authorize(actor, 'workflow_runs.get', ref)
            # 原服务的workflow ref检查之外，补核同一原actor的准确run可见性。
            if actor.authorize('workflow_runs.get', {'owner_id': self.owner_id,
                    'workflow_ref': ref, 'workflow_run_id': record.workflow_run_id}) is not True:
                raise PermissionError('BUSINESS_WORKFLOW_RUN_ACCESS_DENIED')

        check_workflow()
        sampled = self._run_store().sample_runs_readonly(workflow_id=ref, limit=limit)
        records = tuple(sampled['runs'])
        for record in records:
            check_run(record)
        window = _HealthReadWindow(records, {k: v for k, v in sampled.items() if k != 'runs'})
        result = build_workflow_health_view(window, workflow_id=ref, limit=limit, redact=True)
        # 本次窗口不是永久授权；helper完成后再次查同一原workflow和每个原run。
        check_workflow()
        for record in records:
            check_run(record)
        return {'workflow_ref': ref, 'health': result, 'health_read_coverage': window.health_read_coverage}

    # LLM: 当前业务授权先于恢复分类；原生对账不替代新副作用审核，工厂依赖仍只由宿主提供。
    # 函数用途: 为公共运行入口绑定当前 actor、生命周期和原恢复准入门。
    def _engine(self, actor, *, resolution_ref=None):
        from .engine import _requires_native_agent_resume, _requires_terminal_publication_resume
        from .governance import WorkflowEffectPhase

        # LLM: 每次运行预检、续跑、取消及步骤副作用前校验当前Actor权限、工作流活跃态、冻结空间范围与冻结业务上下文（G5a-R1）；
        #   仅非WorkflowEffectPhase的控制阶段享有cancel收回豁免与resume对账路由，同名步骤一律按真实执行副作用校验（FLAG-5）。
        # 函数用途: 校验当前业务调用者对工作流控制阶段与步骤副作用阶段的操作授权、范围复核与治理准入。
        def authorize(record, phase):
            is_control = not isinstance(phase, WorkflowEffectPhase)
            is_cancel_control = is_control and phase == 'cancel'
            is_resume_control = is_control and phase == 'resume'
            # 取消控制只收回原工作，不重新取得执行准入；名为cancel的步骤副作用及所有其它阶段仍守workflows.run执行授权。
            self._authorize(actor, 'workflow_runs.cancel' if is_cancel_control else 'workflows.run', record.workflow_id)
            current = self._get(record.workflow_id)
            if workflow_state(current)['lifecycle'] != 'active':
                raise PermissionError('WORKFLOW_RECYCLED')
            if record.business_context and record.business_context.get('owner_id') != self.owner_id:
                raise PermissionError('WORKFLOW_OWNER_MISMATCH')
            if not is_cancel_control and (record.scope_snapshot or record.business_context):
                # G5a-R1：恢复控制写入前与每次后续副作用（含名为cancel的步骤）前，按当前范围核冻结空间；凡带冻结业务上下文的运行，
                #   不论上下文端口类型，都以当前Actor经共享只读复核核当前空间/访问/绑定/项目；不重调端口、不重选默认，
                #   仅真正的取消控制豁免此门以保收回能力。
                self._check_scope(workflow_state(current)['scope'], record.business_context.get('space_id'))
                if record.business_context:
                    from ..conversation.business_context import revalidate_business_context
                    from ..conversation.business_contracts import BusinessExecutionContext
                    revalidate_business_context(actor=actor,
                        context=BusinessExecutionContext.from_dict(record.business_context))
            if self.governance is not None:
                if is_resume_control and resolution_ref is not None:
                    return self.governance.require_resolution(record, resolution_ref)
                return self.governance.require_effect(record, phase)
            if is_resume_control or resolution_ref is not None:
                step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
                receipt = step.escalation_result.get('tool_receipt', {}) if step is not None else {}
                prior_effect = receipt.get('effect_outcome', record.effect_outcome) if isinstance(receipt, dict) else record.effect_outcome
            if resolution_ref is not None:
                raise WorkflowPolicyRefusal('WORKFLOW_RESOLUTION_UNAVAILABLE', details={
                    'workflow_run_id': record.workflow_run_id,
                    'prior_effect_outcome': prior_effect,
                    'resolution_available': False,
                    'recommended_action': '当前宿主未配置可信核验入口；保留原操作并核对目标结果，禁止用新幂等键重做。',
                })
            if (is_resume_control and not _requires_native_agent_resume(record)
                    and not _requires_terminal_publication_resume(record) and (record.effect_outcome == 'unknown'
                    or any(step.escalation_result.get('effect_started') and
                           (step.needs_repair or step.status == 'running') for step in record.steps))):
                raise WorkflowPolicyRefusal('WORKFLOW_RECONCILIATION_REQUIRED', details={
                    'workflow_run_id': record.workflow_run_id,
                    'step_id': record.current_step_id,
                    'operation_id': receipt.get('operation_id', '') if isinstance(receipt, dict) else '',
                    'prior_effect_outcome': prior_effect,
                    'resolution_available': False,
                    'recommended_action': '原步骤副作用尚未核实；先核对原操作和目标结果，由可信宿主提交核验，禁止原样盲目重试。',
                })
            return None

        if not callable(self.engine_factory):
            raise PermissionError('WORKFLOW_BUSINESS_ENGINE_UNAVAILABLE')
        return self.engine_factory(self.store.owner_home, run_authorizer=authorize, agent_steps=self.agent_steps)

    @staticmethod
    def _completion_children(record, snapshot):
        """只从冻结步骤/批attempt确定子ID；不枚举目录，也不搜索运行历史。"""
        import uuid

        from .subworkflow_steps import _resolve_target_child_workflow_id

        specs = {spec['step_id']: spec for spec in snapshot.get('steps', ())}
        for step in record.steps:
            if step.mode != 'subworkflow':
                continue
            spec = specs.get(step.step_id, {})
            receipt = step.escalation_result.get('subworkflow_receipt') or {}
            if not isinstance(receipt, dict):
                raise ValueError('SUBWORKFLOW_COMPLETION_LINK_INVALID')
            # 条件假/尚未启动的合法步骤没有子调用；只对有执行证据的链接要求target。
            if not (step.subworkflow_run_id or receipt or step.escalation_result.get('effect_started')
                    or any(batch.get('attempt_id') or batch.get('status') == 'succeeded'
                           for batch in step.batches)):
                continue
            target = _resolve_target_child_workflow_id(spec)
            if not target or spec.get('mode') != 'subworkflow':
                raise ValueError('SUBWORKFLOW_COMPLETION_LINK_INVALID')
            if spec.get('foreach') is not None:
                # 原批账保存attempt而非子回执；与原执行器使用同一个确定性UUID5规则。
                for batch in step.batches:
                    attempt = batch.get('attempt_id')
                    if not attempt:
                        if batch.get('status') == 'succeeded':
                            raise ValueError('SUBWORKFLOW_COMPLETION_LINK_INVALID')
                        continue
                    if not isinstance(attempt, str):
                        raise ValueError('SUBWORKFLOW_COMPLETION_LINK_INVALID')
                    child_id = uuid.uuid5(uuid.NAMESPACE_URL,
                        f'subworkflow:{record.workflow_run_id}:{step.step_id}:{attempt}').hex
                    yield child_id, target, batch.get('status') == 'succeeded'
                continue
            attempt = step.escalation_result.get('attempt_id') or step.attempt_count
            if not (step.subworkflow_run_id or receipt or step.escalation_result.get('effect_started')):
                continue
            child_id = uuid.uuid5(uuid.NAMESPACE_URL,
                f'subworkflow:{record.workflow_run_id}:{step.step_id}:{attempt}').hex
            if ((step.subworkflow_run_id and step.subworkflow_run_id != child_id)
                    or receipt and (receipt.get('child_run_id') != child_id
                        or receipt.get('child_workflow_id') != target)):
                raise ValueError('SUBWORKFLOW_COMPLETION_LINK_INVALID')
            yield child_id, target, bool(step.subworkflow_run_id or receipt)

    def _repair_completion_clocks(self, engine, root, snapshot, actor):
        """先核全树关联和当前权限，再只补原完成计时；不改run/step或重新执行。"""
        from .run_store import WorkflowExecutionError, _decode_run, _publish_completed_run

        pending, checked, seen = [(root, snapshot)], [], set()
        while pending:
            record, frozen = pending.pop()
            if record.workflow_run_id in seen or record.owner_id != self.owner_id:
                raise PermissionError('WORKFLOW_OWNER_OR_COMPLETION_LINK_MISMATCH')
            seen.add(record.workflow_run_id)
            source = frozen.get('descriptor') if isinstance(frozen, dict) else None
            if not isinstance(source, dict) or source.get('id') != record.workflow_id:
                raise ValueError('WORKFLOW_COMPLETION_SNAPSHOT_ID_MISMATCH')
            metadata = source.get('metadata', {})
            if not isinstance(metadata, dict):
                raise ValueError('WORKFLOW_COMPLETION_SNAPSHOT_SOURCE_INVALID')
            # 来源合法性独立于业务计时资格；冻结字典仍须通过原资产codec，空Owner沿原执行合同允许。
            descriptor = AssetDescriptor.from_dict(source)
            if descriptor.owner_id and descriptor.owner_id != record.owner_id:
                raise ValueError('WORKFLOW_COMPLETION_SNAPSHOT_SOURCE_MISMATCH')
            # 无业务元数据的合法来源无需计时；业务来源损坏不能被publisher的no-op洗成成功。
            if BUSINESS_WORKFLOW_METADATA in metadata and (
                    source.get('owner_id') != record.owner_id
                    or source.get('scope_layer', 'owner') != 'owner'
                    or source.get('source_scope', 'owner') != 'owner'):
                raise ValueError('WORKFLOW_COMPLETION_SNAPSHOT_SOURCE_MISMATCH')
            self._authorize(actor, 'workflows.run', record.workflow_id)
            # 沿原控制预检核权限和范围；只补计时，不申请不存在的run步骤副作用审批。
            engine.run_authorizer(record, 'preflight')
            checked.append((record, frozen))
            for child_id, target, required in self._completion_children(record, frozen):
                try:
                    raw = engine.store._read(child_id, 'run.json')
                except FileNotFoundError:
                    if required:
                        raise
                    # 已准备attempt但尚未生成子运行，不等于存在一条完成计时。
                    continue
                child = _decode_run(raw)
                if (child.workflow_run_id != child_id or child.workflow_id != target
                        or child.parent_workflow_run_id != record.workflow_run_id
                        or child.subworkflow_depth != record.subworkflow_depth + 1):
                    raise ValueError('SUBWORKFLOW_COMPLETION_LINK_INVALID')
                pending.append((child, raw['workflow_snapshot']))
        errors = []
        for record, frozen in reversed(checked):
            # 不持任何运行锁；再次使用原授权门，计时写入仍走原资产的来源/终态校验。
            engine.run_authorizer(record, 'preflight')
            try:
                _publish_completed_run(self.store.owner_home, record, frozen)
            except WorkflowExecutionError as error:
                errors.append(error)
        if errors:
            error = WorkflowExecutionError(root, errors[0].cause, failure_stage='completion_clock')
            error.receipt['completion_clock_failures'] = [item.receipt.copy() for item in errors]
            raise error

    # LLM: 运行身份由Owner+幂等键派生，同键重放非阻塞读取原运行快照与持久准入回执，不争抢执行锁或再次准入；
    #   仍在排队的原运行按admission.json如实返回202 accepted，进入执行或终态后按原运行真实状态返回。
    # 函数用途: 以冻结上下文和范围启动一次原生工作流运行，或如实回放同键原运行回执。
    def _run(self, request, actor, key):
        if not isinstance(key, str) or not 0 < len(key.encode()) <= 256:
            raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
        definition = self._get(request['workflow_ref'])
        state = workflow_state(definition)
        if state['lifecycle'] != 'active' or not definition.enabled:
            raise WorkflowPolicyRefusal('WORKFLOW_NOT_ACTIVE')
        identity = hashlib.sha256((self.owner_id + '\0workflows.run\0' + key).encode()).hexdigest()
        intent = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        engine = self._engine(actor)
        try:
            raw_run = engine.store._read(identity, 'run.json')
        except FileNotFoundError:
            raw_run = None
        if raw_run is not None:
            if raw_run.get('workflow_run_id') != identity:
                raise ValueError('运行身份与目录不匹配')
            from .run_store import _decode_run
            saved = _decode_run(raw_run)
            if saved.owner_id != self.owner_id:
                raise PermissionError('WORKFLOW_OWNER_MISMATCH')
            if (raw_run.get('workflow_snapshot') or {}).get('_operation_intent') != intent:
                raise BusinessError('IDEMPOTENCY_CONFLICT')
            # 原终态已发表而计时写回失败时，同键命令只补发计时，不重跑已确认步骤。
            self._repair_completion_clocks(engine, saved, raw_run['workflow_snapshot'], actor)
            if saved.status == 'queued' and not callable(getattr(engine, 'concurrency_admission_observer', None)):
                try:
                    admission = engine.store._read(identity, 'admission.json')
                except FileNotFoundError:
                    admission = None
                if isinstance(admission, dict) and admission.get('status') == 'queued':
                    from ..plugin_host.contracts import DomainResult
                    return DomainResult(202, {'workflow_run_id': identity, 'operation_id': identity, 'state': 'accepted'})
            return {'workflow_run_id': identity, 'operation_id': identity, 'state': self._receipt_state(saved)}
        context, origin = None, None
        if isinstance(self.context_provider, WorkflowRunContextFactory):
            # 显式签名：工厂拿准确运行身份与冻结范围；Owner级运行未请求空间/项目时不冻结业务上下文。
            context = self.context_provider(actor, request, workflow_run_id=identity, scope=state['scope'])
            if context is not None:
                origin = 'workflow_run'
                if not isinstance(context, BusinessExecutionContext) or context.task_id != identity:
                    raise PermissionError('WORKFLOW_CONTEXT_MISMATCH')
        elif self.context_provider is not None:
            context = self.context_provider(actor, request)
            if not isinstance(context, BusinessExecutionContext):
                raise PermissionError('WORKFLOW_CONTEXT_MISMATCH')
        if context is not None and (context.owner_id != self.owner_id
                                    or request.get('space_id') and context.space_id != request['space_id']):
            raise PermissionError('WORKFLOW_CONTEXT_MISMATCH')
        if state['scope']['scope_mode'] == 'spaces' and context is None:
            raise PermissionError('BUSINESS_CONTEXT_UNAVAILABLE')
        self._check_scope(state['scope'], context.space_id if context else request.get('space_id'))
        record = engine.run_workflow(definition.to_dict(), request.get('inputs', {}), owner_id=self.owner_id,
            business_context=context, scope_snapshot=state['scope'], workflow_run_id=identity, operation_intent=intent,
            **({'business_context_origin': origin} if origin else {}))
        if isinstance(record, dict):
            # 非allow策略的准入回执不是运行记录（原生引擎不挂准入观察者）：排队=已耐久受理，给async accepted回执；
            #   跳过时引擎已把原运行落为skipped，按该记录回执。不把回执当记录读。
            if record.get('status') == 'queued':
                from ..plugin_host.contracts import DomainResult
                return DomainResult(202, {'workflow_run_id': identity, 'operation_id': identity, 'state': 'accepted'})
            record = engine.store.load_run(identity)
        return {'workflow_run_id': identity, 'operation_id': identity, 'state': self._receipt_state(record)}

    @staticmethod
    def _receipt_state(record):
        return ('completed' if record.status == 'succeeded' else 'pending'
                if record.status in ('running', 'waiting_for_agent', 'paused_for_resume') else
                'failed' if record.status == 'failed' else 'blocked')

    @staticmethod
    def _run_view(record):
        return {'workflow_run_id': record.workflow_run_id, 'workflow_ref': record.workflow_id,
            # 运行级上下文的逻辑task_id就是本运行ID，不是SessionTask，公开视图不把它当任务引用。
            'task_id': (None if record.business_context.get('task_id') == record.workflow_run_id
                        else record.business_context.get('task_id')),
            'space_id': record.business_context.get('space_id'),
            'status': record.status, 'execution_definition_ref': record.execution_definition_ref,
            'scope_revision': record.scope_revision, 'phase': record.phase,
            'effect_outcome': {'confirmed': 'committed', 'failed': 'unknown'}.get(record.effect_outcome, record.effect_outcome),
            'confirmed_step_refs': list(record.confirmed_step_refs), 'revision': record.revision}

    # LLM: 运行读写都先核原Owner与当前授权；续跑/取消前置只读快照不争抢活回调的执行锁，同键不同意图与已取消续跑
    #   在任何写入前被原账本或活准入拒绝并投影为not_started，并存异键控制返回503 WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING。
    # 函数用途: 分派workflow_runs.list/get/resume/cancel并投影原生回执。
    def _run_action(self, action, request, *, actor, expected_revision, idempotency_key):
        store = self._run_store()
        if action == 'workflow_runs.list':
            records = store.list_runs(workflow_id=request.get('workflow_ref'), limit=1000)
            rows = [self._run_view(row) for row in records if row.owner_id == self.owner_id
                    and (not request.get('space_id') or row.business_context.get('space_id') == request['space_id'])
                    and self.authorize(actor, 'workflow_runs.get', row.workflow_id) is True]
            page = self._page(rows, request)
            page.pop('list_snapshot_id')
            return page
        if action in ('workflow_runs.get', 'workflow_runs.cancel'):
            record = store.load_run(request['workflow_run_id'])
        else:
            raw_run = store._read(request['workflow_run_id'], 'run.json')
            if raw_run.get('workflow_run_id') != request['workflow_run_id']:
                raise ValueError('运行身份与目录不匹配')
            from .run_store import _decode_run
            record = _decode_run(raw_run)
        if record.owner_id != self.owner_id:
            raise PermissionError('WORKFLOW_OWNER_MISMATCH')
        self._authorize(actor, action, record.workflow_id)
        if action == 'workflow_runs.get':
            return {'run': self._run_view(record), 'steps': [{'step_id': step.step_id, 'mode': step.mode,
                'status': step.status, 'assignment_ref': step.escalation_result.get('assignment_ref'),
                'run_id': step.escalation_result.get('run_id'), 'result_ref': step.escalation_result.get('result_ref'),
                'error': {'code': step.error_code, 'message': step.error_message, 'retryable': False, 'details': []}
                         if step.error_code else None} for step in record.steps],
                'assignments': [], 'review_refs': list(dict.fromkeys(ref for step in record.steps
                    for ref in step.escalation_result.get('review_refs', ()))),
                'breakpoint': ({'step_id': record.current_step_id, 'reason_code': (record.breakpoint or {}).get('error_code', ''),
                    'can_resume': (record.breakpoint or {}).get('can_resume', False), 'required_action': 'resume'}
                    if record.breakpoint else None)}
        if not idempotency_key:
            raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
        engine = self._engine(actor, resolution_ref=request.get('resolution_ref'))
        if action == 'workflow_runs.resume':
            try:
                result = engine.resume_workflow_run(record.workflow_run_id, expected_revision=expected_revision,
                                                     operation_key=idempotency_key, control_intent=request)
            except ValueError as error:
                if type(error) is ValueError and str(error) in ('IDEMPOTENCY_CONFLICT', 'WORKFLOW_CANCELLED'):
                    raise BusinessError(str(error)) from None
                raise
            if isinstance(result, dict):
                # 同上：排队=已耐久受理（等待线程按原回调与原门执行），给accepted回执；跳过/不支持=续跑未开始、
                #   原运行未改，如实拒绝（not_started）。
                if result.get('status') == 'queued':
                    from ..plugin_host.contracts import DomainResult
                    return DomainResult(202, {'operation_id': record.workflow_run_id, 'state': 'accepted'})
                details = ({'reason_code': result['reason_code']}
                           if isinstance(result.get('reason_code'), str) and result['reason_code'] else None)
                raise WorkflowPolicyRefusal(result.get('error_code') or 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING',
                                            details=details)
            return {'run': self._run_view(result), 'operation_id': record.workflow_run_id,
                    'state': self._receipt_state(result)}
        if action == 'workflow_runs.cancel':
            try:
                outstanding = engine.cancel_workflow_run(record.workflow_run_id, expected_revision=expected_revision,
                                                           operation_key=idempotency_key)
            except ValueError as error:
                if type(error) is ValueError and str(error) == 'IDEMPOTENCY_CONFLICT':
                    raise BusinessError('IDEMPOTENCY_CONFLICT') from None
                raise
            return {'operation_id': record.workflow_run_id, 'state': 'pending' if outstanding else 'completed',
                    'outstanding_refs': outstanding}
        raise ValueError('UNKNOWN_WORKFLOW_ACTION')


class _HealthReadWindow:
    """仅本次调用的已授权原对象；不是事实仓、索引或第二权限源。"""

    def __init__(self, records, coverage):
        self._records = records
        self.health_read_coverage = coverage

    def list_runs(self, workflow_id=None, limit=50):
        return [record for record in self._records if record.workflow_id == workflow_id][:limit]
