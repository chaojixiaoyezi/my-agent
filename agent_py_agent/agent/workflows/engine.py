# LLM: 正式运行沿原账本；debug默认无外部副作用，显式许可仍禁正式账本/摘要/pin；通知仅走原Owner出口。
# 模块用途: 编排正式执行及安全单节点调试，保留模式别名、审批等待和真实通知投递状态。
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from ..assets import canonical_skill_name
from ..assets.delivery_contracts import AssetDisabledError
from ..assets.descriptor import execution_definition_ref
from ..assets.script_store import ScriptStore
from ..assets.skill_adapter import get_owner_skill_descriptor
from ..assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStore,
    with_workflow_step_refs,
)
from ..assets.workflow_store import (
    execution_definition_ref as workflow_definition_ref,
)
from ..common.cancellation import ToolCancelled, raise_if_cancelled
from ..common.nofollow_fs import read_text_beneath, write_text_atomic_beneath
from ..concurrency.interrupt import is_interrupted
from .analysis_gate import apply_analysis_gate, isolation_params, query_health
from .batching import (
    batch_summary,
    dispatch_batches,
    merge_outputs,
    pack_items,
    resolve_items,
    validate_foreach,
)
from .concurrency_policy import _reconcile_workflow_claim, dispatch
from .datasource_runtime import (
    _preflight_failure,
    bind_script_execution_record,
    bind_script_workspace,
    emit_asset_im_notification,
    run_script_with_datasources,
)
from .models import (
    AnalysisContext,
    StepDebugResult,
    StepExecutionRecord,
    WorkflowRunRecord,
    output_value,
    payload,
    safe_path,
    skill_sequence,
)
from .models import redact_bearer_values as redact_sensitive_value
from .pinned_definitions import PinnedDefinitionError, historical_pin_available, resolve_definition
from .run_artifacts import bind_run_out_target
from .run_store import (
    WorkflowExecutionError,
    WorkflowRunStore,
    execution_receipt,
    replace_run_record,
)
from .script_runner import (
    ScriptRunner,
    _EntrypointMissing,
    _summary_warning_clock,
    load_script,
    parse_script_output,
)

SUCCESS = ('succeeded', 'escalated_recovered', 'skipped')


# LLM: 责任按原步骤的结构化派发事实识别，不因failed/paused标签消失，不建立第二账本。
# 函数用途: 判断这一步是否还有必须核实或取消的原任务。
def _agent_task_obligation(step):
    """任务身份/未知提交才是责任事实；failed/paused标签不能消掉已开始的派发。"""
    result = step.escalation_result
    return step.mode == 'agent_task' and (step.status == 'waiting_for_agent' or result.get('task_id')
        or 'session_task_dispatch' in result or result.get('submission_state') == 'unknown'
        or result.get('effect_started') and result.get('submission_state') != 'not_started')


# LLM: 公共准入与引擎路由共用原任务义务；只选择原生对账，不授予重提或后缀副作用许可。
# 函数用途: 判断恢复是否必须先交回原 Agent 任务完成链。
def _requires_native_agent_resume(record):
    return record.status == 'waiting_for_agent' or any(
        _agent_task_obligation(step) and not step.escalation_result.get('completion_ref') for step in record.steps)


# LLM: 只认原run终态发表失败的结构化暂停与canonical步骤证据；run级needs_repair/unknown不单独否决，不授重提或执行许可。
# 函数用途: 判断恢复是否只剩同一原运行的终态记录发表。
def _requires_terminal_publication_resume(record) -> bool:
    breakpoint = record.breakpoint if isinstance(record.breakpoint, dict) else {}
    ids = tuple(step.step_id for step in record.steps)
    return bool(record.status == 'paused_for_resume' and record.phase == 'terminal'
        and breakpoint.get('phase') == record.phase and breakpoint.get('can_resume') is True
        and breakpoint.get('error_code') in ('WORKFLOW_EXECUTION_FAILED', 'WORKFLOW_PAUSED')
        and ids and record.confirmed_step_refs == ids
        and all(step.status in SUCCESS and not step.needs_repair and not step.escalation_result.get('cleanup_error')
                for step in record.steps)
        and not _requires_native_agent_resume(record))


# LLM: 读取同一当前回合令牌和线程停止事实；不自行建控制状态，不改已持久提交的历史成功。
# 函数用途: 在步骤业务输出和运行终态发表前拒绝已接受的停止。
def _check_stop():
    raise_if_cancelled()
    if is_interrupted():
        raise InterruptedError('workflow interrupted')


# LLM: 定义恢复与refs派生只走资产层权威API，路径安全仍由引擎和Store检查。
# 函数用途: 加载并复制规范执行定义。
def _definition(home, value, workspace_root=None):
    if isinstance(value, str):
        safe_path(home, value)
        asset = WorkflowStore(home, workspace_root=workspace_root).get_workflow(value)
        value = asset.to_dict() if asset is not None else json.loads(read_text_beneath(home, ('workflows', value, 'workflow.json')) or '{}')
    value = with_workflow_step_refs(WorkflowDefinition.from_any(payload(value))).to_dict()
    descriptor = value['descriptor']
    safe_path(home, descriptor['id'])
    return value, descriptor


# LLM: 新运行和恢复后失效的后缀都创建新记录，不继承旧后缀输出。
# 函数用途: 依据定义初始化一个待执行步骤。
def _pending(spec):
    return StepExecutionRecord(spec['step_id'], spec.get('name', spec['step_id']), spec['mode'],
                               'pending', 0, 0, 0, False, script_id=spec.get('script_id', ''),
                               description=spec.get('description', ''), next_step_ids=tuple(spec.get('next_step_ids', ())))


# LLM: 新快照只比执行内容身份；旧无ref快照仍严格比较原pin，不把未知旧定义静默升级。
# 函数用途: 在恢复和逐步执行门中区分统计CAS与真实定义漂移。
def _same_pin(first, second):
    if first and second and first.get('execution_definition_ref'):
        return (first.get('script_id'), first['execution_definition_ref'], first.get('source_scope')) == (
            second.get('script_id'), second.get('execution_definition_ref'), second.get('source_scope'))
    return first == second


# LLM: 本次代码与CAS读取来自同一冻结record；统计更新不能在第二次读取中制造假的版本不一致。
# 函数用途: 从原资产快照生成数据源旧门所需读取令牌及新执行身份。
def _script_pin(script_id, row):
    return {'script_id': script_id, 'revision': row.get('revision', 0),
        'code_sha256': hashlib.sha256(row['code'].encode('utf-8')).hexdigest(),
        'source_scope': row.get('source_scope', 'owner'),
        'execution_definition_ref': execution_definition_ref(row, kind='script')}


# LLM: 生命周期持有冻结定义、版本和断点合同，步骤调度由子类_drive提供，不建立另一执行器。
# 类用途: 管理工作流创建、恢复与运行级合同，供公开引擎复用。
class _WorkflowRunLifecycle:
    # LLM: 构造不执行资产；外部debug执行器/分析器只由可信宿主注入，定义JSON不授新权限。
    # 函数用途: 绑定Owner存储、分析及可选通用外部节点执行端口，默认不具备外部节点执行能力。
    def __init__(self, owner_home, *, escalation_handler=None, analysis_handler=None, skill_resolver=None,
                 skill_details_resolver=None, workspace_root=None, debug_node_handler=None,
                 agent_steps=None, run_authorizer=None, step_observer: Callable[[dict], None] | None = None):
        self.owner_home = Path(owner_home).absolute()
        self.workspace_root = workspace_root
        self.store, self.runner = WorkflowRunStore(owner_home), ScriptRunner(owner_home, workspace_root=workspace_root)
        self.escalation_handler, self.analysis_handler = escalation_handler, analysis_handler
        self.skill_resolver = skill_resolver
        self.skill_details_resolver = skill_details_resolver
        self.debug_node_handler = debug_node_handler
        self.agent_steps, self.run_authorizer = agent_steps, run_authorizer
        self.step_observer = step_observer

    # LLM: 首次执行冻结定义及依赖；准入前后重读原run，queued只读复核不再争执行权；取消终态不伪报准入，owner_id不授权。
    # 函数用途: 新建运行账本并顺序执行步骤。
    def run_workflow(self, workflow, inputs=None, trigger_kind='manual', owner_id='', *,
                     business_context=None, scope_snapshot=None, workflow_run_id=None, operation_intent=None,
                     concurrency_policy='allow', queue_limit=8, definition_version: int | None = None):
        if concurrency_policy == 'allow':
            concurrency_policy = getattr(self, 'default_concurrency_policy', 'allow')
        if concurrency_policy not in ('allow', 'skip_if_running', 'queue'):
            return {'status': 'skipped', 'error_code': 'INVALID_CONCURRENCY_POLICY'}
        version_pins = None
        if definition_version is None:
            definition, descriptor = _definition(self.owner_home, workflow, self.workspace_root)
        else:
            workflow_id = workflow if isinstance(workflow, str) else WorkflowDefinition.from_any(workflow).descriptor.id
            try:
                definition, version_pins = resolve_definition(self.owner_home, workflow_id, definition_version)
            except PinnedDefinitionError as exc:
                return {'status': 'failed', 'error_code': exc.error_code, 'effect_outcome': 'not_started'}
            descriptor = definition['descriptor']
        self._require_workflow_enabled(definition)
        if owner_id and descriptor.get('owner_id') and owner_id != descriptor['owner_id']:
            raise ValueError('工作流 Owner 不匹配')
        from ..assets.workflow_store import BUSINESS_WORKFLOW_METADATA
        from ..conversation.business_contracts import BusinessExecutionContext, WorkflowScope
        if business_context is not None and not isinstance(business_context, BusinessExecutionContext):
            raise TypeError('business_context必须由可信宿主提供DTO')
        frozen_scope = scope_snapshot if scope_snapshot is not None else descriptor.get('metadata', {}).get(
            BUSINESS_WORKFLOW_METADATA, {}).get('scope', {})
        if frozen_scope:
            frozen_scope = WorkflowScope.from_dict(frozen_scope).to_dict()
        record = WorkflowRunRecord(workflow_run_id or uuid.uuid4().hex, descriptor['id'], owner_id or descriptor.get('owner_id', ''),
                                   trigger_kind, 'running', time.time(), 0, 0,
                                   steps=tuple(_pending(spec) for spec in definition['steps']),
                                   inputs=payload({} if inputs is None else inputs),
                                   business_context=business_context.to_dict() if business_context else {},
                                   scope_snapshot=payload(frozen_scope), scope_revision=frozen_scope.get('scope_revision', 0))
        if operation_intent is not None:
            definition['_operation_intent'] = operation_intent
        record.definition_version = definition_version
        definition['_frozen_scripts'] = self._script_pins(definition['steps']) if version_pins is None else version_pins
        record = replace_run_record(record, dict(workflow_name=descriptor['name'], total_steps=len(definition['steps'])))
        execution_receipt(record, phase='preflight', effect_outcome='not_started',
                          definition_ref=workflow_definition_ref(definition, kind='workflow'))
        self._authorize_run(record, 'preflight')
        if concurrency_policy != 'allow':
            record.status = 'queued'
        self.store.create_run(record, definition)

        # LLM: 闭包在准入前捕获定义/输入/依赖与本次执行器；不按资产ID重读可变正文，取消终态不可复活。
        # 函数用途: 取得资产claim后沿本次原run锁及冻结快照执行。
        def execute_frozen():
            with self.store.execution_lock(record.workflow_run_id):
                current = self.store.load_run(record.workflow_run_id)
                if current.status in ('cancelled', 'cancelling', 'failed', 'succeeded', 'skipped'):
                    return current
                if current.status == 'queued':
                    current = replace_run_record(current, dict(status='running'))
                    self.store.save_run(current)
                return self._drive(current, definition, 0)

        if concurrency_policy != 'allow':
            with self.store.execution_lock(record.workflow_run_id):
                current = self.store.load_run(record.workflow_run_id)
                if current.status in ('cancelled', 'cancelling', 'failed', 'succeeded', 'skipped'):
                    return current
        result = dispatch(self.owner_home, 'workflow', descriptor['id'], record.workflow_run_id,
                          concurrency_policy, execute_frozen, queue_limit=queue_limit, _lifecycle=self.store)
        if isinstance(result, dict):
            result = {**result, 'workflow_run_id': record.workflow_run_id}
            # queued回执不修改运行；load_run沿原普通IO锁等待worker，而非竞争非阻塞执行锁。
            receipt_lock = self.store.execution_lock(record.workflow_run_id) if result.get('status') == 'skipped' else nullcontext()
            with receipt_lock:
                current = self.store.load_run(record.workflow_run_id)
                if current.status in ('cancelled', 'cancelling', 'failed', 'succeeded', 'skipped'):
                    return current
                if result.get('status') == 'skipped':
                    current.status, current.ended_at = 'skipped', time.time()
                    self.store.save_run(current)
                    record = current
            try:
                self.store.save_admission_receipt(record.workflow_run_id, result)
            except Exception as exc:
                # 入队已发生，回执IO失败不等于执行失败，更不能提示调用方盲目重跑。
                execution_receipt(record, effect_outcome='unknown' if result['status'] == 'queued' else 'not_started')
                raise WorkflowExecutionError(record, exc) from exc
            return self._policy_receipt(result, record)
        return result

    # LLM: 业务适配端只接本次宿主注入的观察器；原业务服务继续消费DTO，结构化准入回执由原工具返回。
    # 函数用途: 保留run/resume业务边界的既有强类型返回合同。
    def _policy_receipt(self, result, record):
        observer = getattr(self, 'concurrency_admission_observer', None)
        if isinstance(result, dict) and callable(observer):
            observer(result)
            return record
        return result

    # LLM: 参数/补丁先完成无副作用验证，再核后缀版本；合法修复可重钉当前依赖，确认/漂移断点仍强制版本门。
    # 函数用途: 在独占锁内校验修复请求、保存批准的快照变更并续跑，保留成功前缀。
    def resume_workflow_run(self, workflow_run_id, from_step_id=None, *, patched_step_output=None, updated_data_sources=None,
                            resolution_origin: Literal['user', 'agent', 'system'] | None = None, allow_script_drift: bool = False,
                            expected_revision=None, operation_key='', control_intent=None,
                            concurrency_policy='allow', queue_limit=8):
        if concurrency_policy == 'allow':
            concurrency_policy = getattr(self, 'default_concurrency_policy', 'allow')
        if concurrency_policy not in ('allow', 'skip_if_running', 'queue'):
            return {'status': 'skipped', 'error_code': 'INVALID_CONCURRENCY_POLICY'}
        options = dict(patched_step_output=payload(patched_step_output), updated_data_sources=payload(updated_data_sources),
                       resolution_origin=resolution_origin, allow_script_drift=allow_script_drift,
                       expected_revision=expected_revision, operation_key=operation_key, control_intent=payload(control_intent))
        # LLM: 参数只捕获本次原运行的显式恢复意图，实际执行仍要经过原CAS和授权门。
        # 函数用途: 将冻结的恢复请求交给取得claim的原执行器。
        def callback():
            return self._resume_workflow_run(workflow_run_id, from_step_id, **options)
        original_policy = self.store._concurrency_policy(workflow_run_id)
        if original_policy != 'allow':
            concurrency_policy = original_policy
        if concurrency_policy == 'allow':
            return callback()
        frozen = self.store._read(workflow_run_id, 'run.json')
        if options['expected_revision'] is None:
            options['expected_revision'] = frozen.get('revision', 0)
        result = dispatch(self.owner_home, 'workflow', frozen['workflow_id'], workflow_run_id,
                          concurrency_policy, callback, queue_limit=queue_limit, _lifecycle=self.store)
        if isinstance(result, dict):
            from .run_store import _decode_run
            return self._policy_receipt({**result, 'workflow_run_id': workflow_run_id}, _decode_run(frozen))
        return result

    # LLM: 公共准入后只消费原run冻结定义、CAS和控制操作账，不从可编辑资产重建运行。
    # 函数用途: 在原运行排他锁内验证和执行一次显式恢复。
    def _resume_workflow_run(self, workflow_run_id, from_step_id=None, *, patched_step_output=None, updated_data_sources=None,
                            resolution_origin: Literal['user', 'agent', 'system'] | None = None, allow_script_drift: bool = False,
                            expected_revision=None, operation_key='', control_intent=None):
        if type(allow_script_drift) is not bool:
            raise ValueError('allow_script_drift必须为布尔值')
        if resolution_origin is not None and resolution_origin not in ('user', 'agent', 'system'):
            raise ValueError('resolution_origin必须为user、agent或system')
        with self.store.execution_lock(workflow_run_id):
            record = self.store.load_run(workflow_run_id)
            definition = self.store.load_workflow_snapshot(workflow_run_id)
            if not operation_key and expected_revision is not None and expected_revision != record.revision:
                raise ValueError('WORKFLOW_RUN_REVISION_CONFLICT')
            self._require_workflow_enabled(definition)
            if operation_key:
                self._authorize_run(record, 'preflight')
                intent = control_intent if control_intent is not None else {
                    'from_step_id': from_step_id, 'patched_step_output': patched_step_output,
                    'updated_data_sources': updated_data_sources, 'resolution_origin': resolution_origin,
                    'allow_script_drift': allow_script_drift}
                replay, record = self.store.begin_control(workflow_run_id, action='resume', operation_key=operation_key,
                                                          expected_revision=expected_revision, intent=intent)
                if replay:
                    return record
            if record.status in ('cancelled', 'cancelling'):
                raise ValueError('WORKFLOW_CANCELLED')
            admission = self._authorize_run(record, 'resume')
            if _requires_terminal_publication_resume(record):
                # 不消费对账裁决或修复选项，冻结定义须与原步骤集一致；只沿原结算/发表链补发终态，不执行后缀。
                if admission is not None or any(option is not None for option in (
                        from_step_id, patched_step_output, updated_data_sources)):
                    raise ValueError('WORKFLOW_TERMINAL_PUBLICATION_ONLY')
                if [(step.step_id, step.mode) for step in record.steps] != [
                        (spec['step_id'], spec['mode']) for spec in definition['steps']]:
                    raise ValueError('WORKFLOW_TERMINAL_PUBLICATION_MISMATCH')
                result = self._drive(record, definition, len(record.steps))
                return self.store.complete_control(workflow_run_id, operation_key) if operation_key else result
            from .release_gate import WorkflowEffectAdmission
            if isinstance(admission, WorkflowEffectAdmission):
                if admission.reconciled_output is not None:
                    if patched_step_output is not None or from_step_id not in (None, admission.reconciled_step_id):
                        raise PermissionError('WORKFLOW_RECONCILIATION_CONFLICT')
                    from_step_id, patched_step_output = admission.reconciled_step_id, admission.reconciled_output
                    resolution_origin = 'system'
                if admission.effect_not_started or admission.reconciled_output is not None:
                    record.effect_outcome = 'not_started' if admission.effect_not_started else 'confirmed'
            if _requires_native_agent_resume(record):
                if from_step_id is not None or patched_step_output is not None or updated_data_sources is not None:
                    raise ValueError('WORKFLOW_AGENT_COMPLETION_HOST_ONLY')
                result = self._resume_agent(record, definition)
                return self.store.complete_control(workflow_run_id, operation_key) if operation_key else result
            ids = [s.step_id for s in record.steps]
            if from_step_id is not None and from_step_id not in ids:
                raise ValueError('恢复步骤不存在')
            start = ids.index(from_step_id) if from_step_id is not None else next(
                (i for i, step in enumerate(record.steps) if step.status not in SUCCESS), len(ids))
            if any(step.status not in SUCCESS for step in record.steps[:start]):
                raise ValueError('不能跳过未成功的前序步骤')
            repaired = None
            if patched_step_output is not None:
                if start == len(ids):
                    raise ValueError('没有可注入修复输出的目标步骤')
                repaired = self._patched_step(record.steps[start], definition['steps'][start], patched_step_output, resolution_origin)
            if updated_data_sources is not None and not isinstance(updated_data_sources, (dict, list)):
                raise ValueError('updated_data_sources必须为dict或list')
            blocked = self._resume_script_gate(record, definition, start, allow_script_drift)
            if blocked is not None:
                return blocked
            if updated_data_sources is not None:
                definition.setdefault('descriptor', {})['data_sources'] = payload(updated_data_sources)
                definition['_override_data_sources'] = True
                self._save_definition(record, definition)
            if start == len(ids) and record.status == 'succeeded':
                return self.store.complete_control(workflow_run_id, operation_key) if operation_key else record
            suffix = [_pending(spec) for spec in definition['steps'][start + bool(repaired):]]
            if not repaired and start < len(ids) and definition['steps'][start].get('foreach') is not None:
                suffix[0] = replace(record.steps[start], status='pending', needs_repair=False, error_code='', error_message='')
            steps = record.steps[:start] + ((repaired,) if repaired else ()) + tuple(suffix)
            record = replace_run_record(record, dict(steps=steps, status='running', ended_at=0, failed_step_id='',
                             needs_repair=False, current_step_id='', final_output={}, resumed_from_step_id=ids[start] if start < len(ids) else ''))
            record = replace_run_record(record, dict(breakpoint=None))
            if repaired:
                published = self.store.save_step_result(workflow_run_id, repaired, resuming=True)
                record.revision = published.revision
            self.store.save_run(record, resuming=True)
            if repaired:
                self._step_progress(record, repaired, {'seq': record.progress_seq})
            result = self._drive(record, definition, start + bool(repaired))
            return self.store.complete_control(workflow_run_id, operation_key) if operation_key else result

    resume_workflow = resume_workflow_run

    def _authorize_run(self, record, phase):
        """冻结选择不等于执行许可；新业务入口每次副作用都消费当前宿主授权。"""
        if record.scope_snapshot or record.business_context:
            if not callable(self.run_authorizer):
                raise PermissionError('WORKFLOW_BUSINESS_AUTHORIZATION_UNAVAILABLE')
            scope = record.scope_snapshot
            context = record.business_context
            if context and context.get('owner_id') != record.owner_id:
                raise PermissionError('WORKFLOW_OWNER_MISMATCH')
            if scope.get('scope_mode') == 'unassigned':
                raise PermissionError('WORKFLOW_UNASSIGNED')
            if scope.get('scope_mode') == 'spaces' and context.get('space_id') not in scope['space_ids']:
                raise PermissionError('WORKFLOW_SPACE_MISMATCH')
        if callable(self.run_authorizer):
            return self.run_authorizer(record, phase)
        return None

    # LLM: 仅显式恢复在原执行锁和授权门后发表；每次写入都携带恢复意图，普通晚到写仍受终态守卫拒绝。
    # 函数用途: 找回原任务或继续已证明未开始的原提交，不重放成功前缀或在恢复这一拍执行后缀。
    def _resume_agent(self, record, definition):
        """继续核对原步骤，不因暂停/失败重新分配attempt或执行成功前缀。"""
        if self.agent_steps is None:
            raise ValueError('WORKFLOW_AGENT_EXECUTOR_UNAVAILABLE')
        index = next((i for i, step in enumerate(record.steps)
            if _agent_task_obligation(step) and not step.escalation_result.get('completion_ref')), None)
        if index is None:
            completed = next((step for step in reversed(record.steps) if _agent_task_obligation(step)
                and step.escalation_result.get('completion_ref') and step.status in ('succeeded', 'failed')), None)
            if completed is None:
                raise ValueError('WORKFLOW_AGENT_COMPLETION_UNRESOLVED')
            # 独立步骤快照先于run.json发表；这一拍只弥合崩溃窗口，不调用完成端口或执行后缀。
            record = replace_run_record(record, dict(status='running' if completed.status == 'succeeded' else 'failed',
                needs_repair=completed.needs_repair, failed_step_id=completed.step_id if completed.needs_repair else '',
                current_step_id=completed.step_id, breakpoint=None))
            self.store.save_run(record, resuming=True)
            self._step_progress(record, completed, {'seq': record.progress_seq})
            return record
        step = record.steps[index]
        previous = payload(step.escalation_result)
        result = step.escalation_result
        # 只消费本次恢复前已经落盘的证据；本次刚证明absence不能立即启动工作。
        retry_absent = (result.get('submission_state') == 'not_started'
            and result.get('reconciliation_state') == 'absent' and not result.get('task_id'))
        has_store = getattr(getattr(self.agent_steps, 'background_service', None), 'store', None) is not None
        recovery = ('session_task_dispatch' in result and (
            result.get('submission_state') == 'unknown' or not result.get('task_id'))
            or has_store and result.get('submission_state') == 'unknown')
        if recovery:
            checked = self.agent_steps.reconcile(record, step)
            if (retry_absent and checked and not result.get('task_id')
                    and result.get('submission_state') == 'not_started' and result.get('reconciliation_state') == 'absent'):
                self._admit_effect(record, step)
                self.agent_steps.submit(record, definition['steps'][index], step)
        elif not step.escalation_result.get('task_id'):
            self._admit_effect(record, step)
            self.agent_steps.submit(record, definition['steps'][index], step)
        # 对账这一拍只恢复原引用；不能顺带运行模型、脚本或工作流后缀。
        completion = None if recovery else self.agent_steps.completion(record, step)
        if completion is None:
            changed = step.status != 'waiting_for_agent' or record.status != 'waiting_for_agent'
            step.status = 'waiting_for_agent'
            if changed or previous != step.escalation_result:
                # 已经过同一原运行的恢复授权和 CAS；不能把合法对账误判成外部晚到写入。
                record = self.store.save_step_result(record.workflow_run_id, step, resuming=True)
            if changed:
                record.status = 'waiting_for_agent'
                self.store.save_run(record, resuming=True)
            return record
        step.status = 'succeeded' if completion['status'] == 'completed' else 'failed'
        step.llm_involved = True
        step.output_snapshot = redact_sensitive_value(payload(completion.get('output', {})))
        step.escalation_result.update({name: completion.get(name) for name in ('completion_ref', 'result_ref', 'run_id')})
        step.error_code = '' if step.status == 'succeeded' else completion.get('error_code', 'WORKFLOW_AGENT_FAILED')
        step.needs_repair, step.ended_at = step.status == 'failed', time.time()
        record = replace_run_record(record, dict(status='running' if step.status == 'succeeded' else 'failed',
            needs_repair=step.needs_repair, failed_step_id=step.step_id if step.needs_repair else '', breakpoint=None))
        self.store.save_run(record, resuming=True)
        self._step_progress(record, step, {'seq': record.progress_seq})
        if step.status == 'failed':
            return record
        return self._drive(record, definition, index + 1)

    # LLM: 持原execution_lock先完整验身份并保存恢复ID，全部原责任确认前不得完成原control。
    # 函数用途: 持续核实和取消同一批任务，未知或未停净时保持取消中。
    def cancel_workflow_run(self, workflow_run_id, *, expected_revision, operation_key):
        """取消不排在自身保留占位后；运行锁退出之后才按原证明结算占位。"""
        restricted = self.store._concurrency_policy(workflow_run_id) != 'allow'
        workflow_id = self.store._read(workflow_run_id, 'run.json')['workflow_id'] if restricted else None
        try:
            return self._cancel_workflow_run(workflow_run_id, expected_revision=expected_revision,
                                             operation_key=operation_key)
        finally:
            if restricted:
                _reconcile_workflow_claim(self.owner_home, workflow_id, workflow_run_id)

    # LLM: 原执行锁和CAS内核实全部原责任；已结清终态仅完成控制回执，未知责任继续清理而不伪造完成。
    # 函数用途: 取消原运行或确认它已经结束，不改写已结清的历史结果。
    def _cancel_workflow_run(self, workflow_run_id, *, expected_revision, operation_key):
        """原执行锁内逐条清理结构化派发责任；只有全部确认才完成原控制回执。"""
        from .run_store import _concurrency_settled

        with self.store.execution_lock(workflow_run_id):
            record = self.store.load_run(workflow_run_id)
            self._authorize_run(record, 'cancel')
            replay, record = self.store.begin_control(workflow_run_id, action='cancel', operation_key=operation_key,
                                                      expected_revision=expected_revision)
            if replay:
                return []
            if record.status in ('succeeded', 'failed', 'skipped', 'cancelled') and _concurrency_settled(record):
                # 已结清终态只确认本次控制，不经过 cancelling，也不覆盖原成功或失败。
                self.store.complete_control(workflow_run_id, operation_key)
                return []
            record.status = 'cancelling'
            self.store.save_run(record)
            outstanding = []
            obligations = []
            for step in record.steps:
                if _agent_task_obligation(step):
                    previous = payload(step.escalation_result)
                    reconcile = getattr(self.agent_steps, 'reconcile', None)
                    checked = reconcile(record, step) if callable(reconcile) else self.agent_steps is not None
                    if previous != step.escalation_result:
                        record = self.store.save_step_result(workflow_run_id, step)
                    obligations.append((step, checked))
            # 所有身份先核验；恢复的canonical ID已在同一原run/step落盘，之后才能取消。
            for step, checked in obligations:
                previous = payload(step.escalation_result)
                if not checked or not self.agent_steps.cancel(record, step):
                    outstanding.append(step.escalation_result.get('task_id') or step.escalation_result.get('attempt_id') or step.step_id)
                if previous != step.escalation_result:
                    record = self.store.save_step_result(workflow_run_id, step)
            record.status = 'cancelling' if outstanding else 'cancelled'
            self.store.save_run(record)
            if not outstanding:
                self.store.complete_control(workflow_run_id, operation_key)
            return outstanding

    # LLM: 项目覆盖走原只读Store，旧meta只在正式Store缺失时读；正文摘要不授执行权。
    # 函数用途: 统一读取引擎当前作用域的脚本代码与元数据。
    def _script_metadata(self, script_id):
        return (ScriptStore(self.owner_home, workspace_root=self.workspace_root).get_script(script_id)
                or load_script(self.owner_home, script_id)[1])

    # LLM: 不可用依赖保留缺失pin，由实际步骤生成断点；代码摘要和资产版本不由输出正文推导。
    # 函数用途: 冻结指定步骤脚本的revision、hash及来源。
    def _script_pins(self, steps):
        pins = {}
        for spec in steps:
            if not spec.get('script_id'):
                continue
            try:
                row = self._script_metadata(spec['script_id'])
                pins[spec['step_id']] = _script_pin(spec['script_id'], row)
            except (OSError, ValueError, KeyError):
                pins[spec['step_id']] = {'script_id': spec['script_id'], 'revision': None, 'code_sha256': '', 'source_scope': 'missing'}
        return pins

    # LLM: 非漂移故障可修复当前依赖及后缀中完全相同原pin的同版本复用；其它后续/确认断点仍核原pin，前缀不变。
    # 函数用途: 接纳当前故障资产修复，拦截其它后续漂移，仅在核验通过后发表新pin。
    def _resume_script_gate(self, record, definition, start, allow):
        current = self._script_pins(definition['steps'][start:])
        frozen = definition.get('_frozen_scripts', {})
        historical = {key for key, value in current.items() if not _same_pin(frozen.get(key), value)
                      and historical_pin_available(self.owner_home, frozen.get(key))}
        target = record.steps[start] if start < len(record.steps) else None
        repaired = set()
        if target and target.needs_repair and target.error_code != 'SCRIPT_REVISION_DRIFT' and target.step_id in current:
            previous, replacement = frozen.get(target.step_id), current[target.step_id]
            repaired = {key for key, value in current.items() if key not in historical and (key == target.step_id
                        or previous and _same_pin(frozen.get(key), previous) and _same_pin(value, replacement))}
        changed = next((key for key, value in current.items() if key not in repaired and key not in historical
                        and not _same_pin(frozen.get(key), value)), None)
        if changed is None or allow:
            approved = {key: value for key, value in current.items() if (allow or key in repaired) and not _same_pin(frozen.get(key), value)}
            if approved:
                definition['_frozen_scripts'] = {**frozen, **approved}
                self._save_definition(record, definition)
            return None
        index = next(i for i, step in enumerate(record.steps) if step.step_id == changed)
        step = replace(record.steps[index], error_code='SCRIPT_REVISION_DRIFT',
                       error_message='依赖脚本版本已变化，需显式allow_script_drift确认', ended_at=time.time(),
                       status='paused_for_resume', needs_repair=True)
        steps = record.steps[:index] + (step,) + record.steps[index + 1:]
        blocked = replace_run_record(record, dict(steps=steps, status='paused_for_resume', needs_repair=True, current_step_id=changed,
                          failed_step_id=changed, ended_at=time.time(), breakpoint=self._breakpoint(step, index)))
        self.store.save_run(blocked)
        self._notify_breakpoint(blocked)
        return blocked

    # LLM: 一基位置对应冻结定义，错误消息统一脱敏；主动暂停也有明确恢复选项，不推断执行身份。
    # 函数用途: 构造公开断点对象。
    def _breakpoint(self, step, index):
        return redact_sensitive_value({'step_index': index + 1, 'step_id': step.step_id, 'step_name': step.name,
            'status': 'failed' if step.needs_repair or step.error_code else step.status,
            'error_code': step.error_code or 'WORKFLOW_PAUSED', 'error_message': step.error_message,
            'failed_at': datetime.fromtimestamp(step.ended_at or time.time(), timezone.utc).isoformat(),
            'can_resume': True, 'resume_options': ['rerun_from_step', 'updated_data_sources', 'patched_step_output']})

    # LLM: 通知失败不覆盖已保存运行事实；只经当前Owner通知出口，不自行解析通道或发送认证。
    # 函数用途: 把具体断点交给IM账本及可用outbox。
    def _notify_breakpoint(self, record):
        emit_asset_im_notification(self.owner_home, event_type='workflow_breakpoint', title=record.workflow_name,
            message=f'{record.workflow_name}: {record.current_step_id}',
            payload={'workflow_run_id': record.workflow_run_id, 'workflow_id': record.workflow_id,
                     'total_steps': record.total_steps, 'completed_steps': record.completed_steps, 'breakpoint': record.breakpoint})

    # LLM: 显式类型合同或完全空输出按成功步骤合并值校验，不采纳失败/跳过输出；旧非空无类型合同保持兼容。
    # 函数用途: 更新进度、构造断点并核工作流合同，保留末步真实final_output。
    def _finalize_record(self, record, definition):
        record = replace_run_record(record, dict(workflow_name=definition['descriptor']['name'], total_steps=len(record.steps),
                         completed_steps=sum(step.status in SUCCESS for step in record.steps)))
        error = ''
        if record.status == 'succeeded':
            merged_outputs = {}
            for step in record.steps:
                if step.status in ('succeeded', 'escalated_recovered') and isinstance(step.output_snapshot, dict):
                    merged_outputs.update(step.output_snapshot)
            if isinstance(record.final_output, dict):
                merged_outputs.update(record.final_output)
            contract = definition['descriptor'].get('io_contract', {})
            if isinstance(record.final_output, list) and not contract.get('outputs') and not contract.get('output_types'):
                pass
            elif contract.get('output_types') or not merged_outputs:
                _, error = parse_script_output(json.dumps(merged_outputs), contract)
        if error:
            record = replace_run_record(record, dict(status='failed', needs_repair=True,
                current_step_id=record.steps[-1].step_id if record.steps else '',
                failed_step_id=record.steps[-1].step_id if record.steps else ''))
        if record.status not in ('failed', 'paused_for_resume'):
            return replace_run_record(record, dict(breakpoint=None))
        index = next((i for i, step in enumerate(record.steps) if step.step_id == record.current_step_id), max(len(record.steps) - 1, 0))
        step = record.steps[index] if record.steps else StepExecutionRecord('', '工作流输出', '', 'failed', 0, record.ended_at, 0, False)
        if error:
            step = replace(step, needs_repair=True, error_code='WORKFLOW_OUTPUT_CONTRACT_VIOLATION', error_message=error)
        return replace_run_record(record, dict(breakpoint=self._breakpoint(step, index)))

# LLM: 复用原执行链；debug显式副作用许可与内存标志由调用方传入，不能从config/mock授予权限。
# 类用途: 执行正式工作流，或默认只预览一个节点而不改变正式运行、资产修订及外部业务。
class WorkflowEngine(_WorkflowRunLifecycle):
    # LLM: 默认False在映射/条件后阻止业务副作用；显式True不写RunStore/摘要/pin或升级，返回原mode与新旧DTO键。
    # 函数用途: 只调试目标节点，报告渲染输入、分支、预览和未持久化正式运行；脚本仍走原沙箱。
    def debug_step(self, workflow_id: str, step_id: str, *, mock_inputs: dict | None = None,
                   context_overrides: dict | None = None, timeout_seconds: float = 30.0,
                   allow_side_effects: bool = False):
        if type(allow_side_effects) is not bool:
            raise ValueError('allow_side_effects必须为布尔值')
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 3600):
            raise ValueError('调试超时必须为(0,3600]秒')
        if any(value is not None and not isinstance(value, dict) for value in (mock_inputs, context_overrides)):
            raise ValueError('调试输入和上下文必须是对象')
        definition, descriptor = _definition(self.owner_home, workflow_id, self.workspace_root)
        self._require_workflow_enabled(definition)
        selected = next((spec for spec in definition['steps'] if spec['step_id'] == step_id), None)
        if selected is None:
            raise ValueError('调试步骤不存在')
        spec = payload(selected)
        inputs, outputs = payload(mock_inputs or {}), payload(context_overrides or {})
        spec['inputs_map'] = {key: value for key, value in spec.get('inputs_map', {}).items() if key not in inputs}
        spec['timeout_seconds'] = min(float(spec.get('timeout_seconds') or timeout_seconds), timeout_seconds)
        # 失败调试不能通过escalation回调间接持久化修复；debug保留原失败，不触发兜底。
        spec['on_error'] = 'fail'
        run = WorkflowRunRecord(f'debug-{uuid.uuid4().hex[:12]}', descriptor['id'],
            descriptor.get('owner_id', ''), 'debug', 'running', time.time(), 0, 0,
            inputs=inputs, ephemeral_debug=True)
        record = replace(_pending(spec), status='running', input_snapshot=payload(inputs), started_at=time.time())
        timer = time.monotonic()
        try:
            self._perform(record, spec, outputs, definition, run, record_summary=False,
                          allow_side_effects=allow_side_effects)
        except Exception as exc:
            record.status, record.error_code = 'failed', getattr(exc, 'error_code', 'STEP_EXECUTION_ERROR')
            record.error_message = '调试步骤执行失败'
        status = ('skipped' if record.status == 'skipped' else 'succeeded'
                  if record.status in ('succeeded', 'escalated_recovered', 'paused_for_resume') else 'failed')
        return StepDebugResult(workflow_id=descriptor['id'], step_id=step_id, step_type=record.mode, status=status,
            duration_ms=(time.monotonic() - timer) * 1000, output_data=record.output_snapshot,
            rendered_input=record.input_snapshot, condition_branch=record.escalation_result.get('condition_branch'),
            logs=record.escalation_result.get('debug_logs', []),
            dry_run=record.escalation_result.get('dry_run', False),
            side_effects_blocked=record.escalation_result.get('side_effects_blocked', False),
            error_detail={'code': record.error_code, 'message': record.error_message} if status == 'failed' else None).to_dict()

    # LLM: 补丁仅替代输出；脚本合同来自当前授权作用域，故障默认agent/确认默认user，清理门保持。
    # 函数用途: 校验恢复输出并合成来源明确且输入不变的步骤。
    def _patched_step(self, target, spec, patched, resolution_origin: Literal['user', 'agent', 'system'] | None = None):
        origin = resolution_origin if resolution_origin is not None else ('agent' if target.needs_repair else 'user')
        if origin not in ('user', 'agent', 'system'):
            raise ValueError('resolution_origin必须为user、agent或system')
        if target.escalation_result.get('cleanup_error'):
            raise ValueError('进程组清理未验证，不能用输出补丁恢复')
        if not isinstance(patched, dict):
            raise ValueError('patched_step_output必须为dict')
        output = payload(patched)
        if spec.get('script_id'):
            contract = target.escalation_result.get('io_contract')
            if not contract:
                contract = self._script_metadata(spec['script_id']).get('io_contract', {})
            output, error = parse_script_output(json.dumps(output, allow_nan=False), contract)
            if error:
                raise ValueError(f'修复输出不符合脚本契约: {error}')
        return replace(target, status='escalated_recovered', output_snapshot=redact_sensitive_value(output), needs_repair=False,
                       llm_involved=bool(target.llm_involved or origin == 'agent'), error_code='', error_message='', ended_at=time.time(),
                       escalation_result=redact_sensitive_value({**payload(target.escalation_result),
                           'recovered': True, 'repaired_via': 'patched_step_output', 'resolution_origin': origin}))

    # LLM: 调用方持运行执行锁；独立快照与Store实际读取的内嵌定义保持同源，拒绝跟随链接。
    # 函数用途: 原子保存更新后的冻结数据源，供本轮及重启后续跑复用。
    def _save_definition(self, record, definition):
        safe = redact_sensitive_value(payload(definition))
        write_text_atomic_beneath(self.owner_home, ('data', 'workflow_runs', record.workflow_run_id, 'workflow_snapshot.json'),
                                  json.dumps(safe, ensure_ascii=False, allow_nan=False) + '\n')
        self.store.save_run(record, definition)

    # LLM: 项目/全局命中不写同名Owner摘要；原Store协议和失败隔离保持，不扩大只读资产写权限。
    # 函数用途: 尽力回写最近状态、耗时及错误。
    def _record_summary(self, record):
        try:
            summary = WorkflowStore(self.owner_home, workspace_root=self.workspace_root).record_run_summary
            breakpoint = record.breakpoint or {}
            error = ': '.join(str(breakpoint[key]) for key in ('error_code', 'error_message') if breakpoint.get(key))
            if not error:
                error = next((f'{s.error_code}: {s.error_message}' for s in record.steps if s.step_id == record.failed_step_id), '')
            summary(record.workflow_id, record.status, record.duration_ms,
                    datetime.fromtimestamp(record.ended_at, timezone.utc).isoformat(), last_error=error,
                    blocking=False, run_id=hashlib.sha256(json.dumps(
                        [record.workflow_run_id, record.ended_at]).encode('utf-8')).hexdigest())
        except Exception as exc:
            error_type = type(exc).__name__
            clock = _summary_warning_clock('workflow:' + error_type)
            now = time.monotonic()
            if now - clock[0] >= 60:
                clock[0] = now
                error = str(redact_sensitive_value(str(exc)))[:512]
                logging.getLogger(__name__).warning('workflow summary write failed error_type=%s error=%s',
                    error_type, error, extra={'error_type': error_type, 'error': error})

    # LLM: 失败保存若确认原run已经终结则返回耐久记录；其它异常和中断保持原回执，附属通知不改运行事实。
    # 函数用途: 调度后缀步骤，更新当前进度并结算整次运行。
    def _drive(self, record, definition, start):
        state = {'record': record, 'phase': 'preflight', 'started': bool(start), 'committed': False}
        try:
            return self._drive_steps(record, definition, start, execution_state=state)
        except (KeyboardInterrupt, SystemExit, ToolCancelled, InterruptedError) as exc:
            current = self._committed_record(state)
            if current is None:
                current = self._pause_failed_run(state, definition, exc, interrupted=True)
            exc.record = current
            exc.receipt = WorkflowExecutionError(current, exc).receipt
            raise
        except Exception as exc:
            committed = self._committed_record(state)
            if committed is not None:
                return committed
            current = self._pause_failed_run(state, definition, exc)
            if state.get('terminal_record_observed'):
                return current
            raise WorkflowExecutionError(current, exc) from exc

    # LLM: 原原子写可能已提交但ACK失败；只以同一RunStore完整回读确认，观察失败不能用暂停覆盖可能已提交的终态。
    # 函数用途: 核实原终态提交事实，区别真正未提交和提交结果未知。
    def _committed_record(self, state):
        if state['committed']:
            return state['record']
        if state['phase'] != 'terminal':
            return None
        try:
            persisted = self.store.load_run(state['record'].workflow_run_id)
            if payload(persisted) == payload(state['record']) and execution_receipt(persisted) == execution_receipt(state['record']):
                return persisted
        except Exception:
            state['commit_observation_unknown'] = True
        return None

    # LLM: 只修复原run发表；终态/CAS拒绝时回读一次，已终结则不发布未保存暂停的通知和摘要。
    # 函数用途: 统一收口执行异常和取消，保留成功前缀并尽力保存可恢复暂停。
    def _pause_failed_run(self, state, definition, error, *, interrupted=False):
        current = state['record']
        steps = tuple(replace(step, status='paused_for_resume', needs_repair=True, ended_at=time.time())
                      if step.status == 'running' else step for step in current.steps)
        breakpoint = {'error_code': 'WORKFLOW_PAUSED' if interrupted else 'WORKFLOW_EXECUTION_FAILED',
                      'error_message': str(error), 'phase': state['phase'], 'can_resume': True}
        current = replace_run_record(current, dict(steps=steps, status='paused_for_resume', needs_repair=True,
            ended_at=time.time(), completed_steps=sum(step.status in SUCCESS for step in steps), breakpoint=breakpoint))
        execution_receipt(current, phase=state['phase'],
            effect_outcome='unknown' if state['started'] else 'not_started',
            definition_ref=workflow_definition_ref(definition, kind='workflow'))
        if state.get('commit_observation_unknown'):
            current.breakpoint['can_resume'] = False
        else:
            try:
                self.store.save_run(current)
            except Exception as save_error:
                if str(save_error) in ('WORKFLOW_RUN_TERMINAL', 'WORKFLOW_RUN_REVISION_CONFLICT'):
                    try:
                        persisted = self.store.load_run(current.workflow_run_id)
                    except Exception:
                        persisted = None
                    if persisted is not None and persisted.status in ('cancelled', 'succeeded', 'failed', 'skipped'):
                        state['terminal_record_observed'] = True
                        return persisted
                current.breakpoint['can_resume'] = False
        for hook in (self._notify_breakpoint, self._record_summary):
            try:
                hook(current)
            except Exception:
                # 附属通知和统计不得遮蔽原取消或执行异常。
                pass
        return current

    # LLM: execution_state只属于本次调用栈，发表异常沿原run保存成功前缀，不在engine实例上放跨运行可变状态。
    # 函数用途: 执行原顺序步骤并在每个IO边界更新当前事实。
    def _drive_steps(self, record, definition, start, *, execution_state=None):
        state = execution_state
        progress = {'seq': record.progress_seq}
        for pending in record.steps[start:]:
            self._step_progress(record, pending, progress)
        timer = time.monotonic()
        outputs = {s.step_id: payload(s.output_snapshot) for s in record.steps[:start] if s.status in SUCCESS and s.status != 'skipped'}
        previous_steps = {step.step_id: step for step in record.steps}
        for spec in definition['steps'][start:]:
            previous = previous_steps.get(spec['step_id'])
            template = StepExecutionRecord.from_dict(previous.to_dict()) if spec.get('foreach') is not None and previous is not None else _pending(spec)
            step = replace(template, status='running', started_at=time.time(), input_snapshot=payload(record.inputs),
                           error_code='', error_message='', needs_repair=False)
            # 所有真实步骤先落准确 attempt；审核及异步完成都绑定此身份。
            step.escalation_result['attempt_id'] = uuid.uuid4().hex
            state.update(record=record, phase='step_publish')
            record = self.store.save_step_result(record.workflow_run_id, step)
            clock = time.monotonic()
            self._step_progress(record, step, progress)
            state.update(record=record)
            try:
                _check_stop()
                condition = spec.get('condition')
                if condition and output_value(condition, outputs, record.inputs) != condition['equals']:
                    step.status = 'skipped'
                else:
                    state.update(record=record, phase='started', started=True)
                    self._perform(step, spec, outputs, definition, record)
                _check_stop()
            except (KeyboardInterrupt, SystemExit, ToolCancelled, InterruptedError):
                step.ended_at, step.duration_ms = time.time(), (time.monotonic() - clock) * 1000
                state['record'] = replace_run_record(record, dict(steps=tuple(step if item.step_id == step.step_id else item for item in record.steps)))
                raise
            except Exception as exc:
                step.status, step.error_code, step.error_message = 'failed', step.error_code or getattr(exc, 'error_code', 'STEP_EXECUTION_ERROR'), str(exc)
                step.needs_repair = True
            step.ended_at, step.duration_ms = time.time(), (time.monotonic() - clock) * 1000
            record = replace_run_record(record, dict(steps=tuple(step if item.step_id == step.step_id else item for item in record.steps)))
            state.update(record=record, phase='step_publish')
            record = self.store.save_step_result(record.workflow_run_id, step)
            if step.status == 'waiting_for_agent':
                self._step_progress(record, step, progress)
                record = replace_run_record(record, dict(status='waiting_for_agent', needs_repair=False,
                    current_step_id=step.step_id, ended_at=0, breakpoint=None))
                execution_receipt(record, phase='waiting_for_agent', effect_outcome=(
                    'unknown' if step.escalation_result.get('submission_state') == 'unknown' else 'running'))
                self.store.save_run(record)
                return record
            self._step_progress(record, step, progress)
            if step.status in ('failed', 'paused_for_resume'):
                if not step.needs_repair and step.output_snapshot:
                    outputs[step.step_id] = payload(step.output_snapshot)
                record = replace_run_record(record, dict(status=step.status, needs_repair=step.needs_repair,
                                 current_step_id=step.step_id, failed_step_id=step.step_id if step.needs_repair else ''))
                break
            if step.status != 'skipped':
                outputs[step.step_id] = payload(step.output_snapshot)
        else:
            record = replace_run_record(record, dict(status='succeeded', needs_repair=False, current_step_id='', failed_step_id=''))
        record = replace_run_record(record, dict(ended_at=time.time(), duration_ms=record.duration_ms + (time.monotonic() - timer) * 1000,
                         final_output=next(reversed(outputs.values()), {})))
        state.update(record=record, phase='finalize')
        record = self._finalize_record(record, definition)
        _check_stop()
        execution_receipt(record, phase='finalize' if record.breakpoint else 'terminal',
            effect_outcome='confirmed' if record.status == 'succeeded' else 'failed',
            definition_ref=workflow_definition_ref(definition, kind='workflow'))
        state.update(record=record, phase='terminal')
        self.store.save_run(record)
        state.update(record=record, committed=True)
        if record.breakpoint:
            self._notify_breakpoint(record)
        self._record_summary(record)
        return record

    # LLM: 只投影已发表步骤；序号属于本次执行栈，观察器失败不能改业务或回落文本通道。
    # 函数用途: 向可选XC-1类型端口发送一次节点状态变化。
    def _step_progress(self, run, step, progress):
        if not callable(self.step_observer) or step.status not in ('pending', 'running', 'succeeded', 'failed', 'skipped', 'escalated_recovered'):
            return
        try:
            run.progress_seq, run.revision = self.store.reserve_progress_seq(run.workflow_run_id)
            progress['seq'] = run.progress_seq
            event = {'workflow_run_id': run.workflow_run_id, 'workflow_id': run.workflow_id,
                     'step_id': step.step_id, 'name': step.name,
                     'status': 'succeeded' if step.status == 'escalated_recovered' else step.status,
                     'duration_ms': max(0, int(step.duration_ms)) if step.status not in ('pending', 'running') else 0,
                     'description': step.description, 'next_step_ids': list(step.next_step_ids), 'seq': progress['seq']}
            self.step_observer(event)
        except Exception:
            # best-effort: 进度只是展示投影，不覆盖业务执行事实。
            pass

    # LLM: 正式链仍只原三模式；条件别名纯求值，脚本别名及审批/通知都先过debug副作用门，内置mock例外不变。
    # 函数用途: 复用旧脚本执行链并安全分派单节点，不能从mode别名/config或分析器属性取得新权限。
    def _perform(self, record, spec, outputs, definition, run, *, record_summary=True, allow_side_effects=True):
        from .debug_nodes import analysis_preview
        self._require_step_enabled(spec)
        if spec.get('foreach') is not None and not run.ephemeral_debug:
            self._foreach_step(record, spec, outputs, definition, run)
            return
        if not run.ephemeral_debug and record.mode not in ('deterministic_script', 'llm_skill_analysis', 'llm_prompt', 'hybrid', 'agent_task'):
            record.status, record.error_code = 'failed', 'NODE_EXECUTOR_UNAVAILABLE'
            record.error_message = '通用节点当前仅支持单节点调试，不支持正式DAG执行'
            record.needs_repair = True
            return
        for key, reference in spec.get('inputs_map', {}).items():
            if (record.mode in ('llm_skill_analysis', 'llm_prompt', 'hybrid') and isinstance(reference, dict)
                    and reference.get('step_id') == '$inputs' and 'key' in reference
                    and reference['key'] not in run.inputs and 'fallback' not in reference):
                raise KeyError(f"分析输入缺失: {reference['key']}")
            record.input_snapshot[key] = output_value(reference, outputs, run.inputs) if isinstance(reference, dict) and 'step_id' in reference else payload(reference)
        if run.ephemeral_debug and spec.get('condition'):
            condition = spec['condition']
            if not isinstance(condition, dict) or 'equals' not in condition:
                raise ValueError('条件必须是结构化比较')
            matched = output_value(condition, outputs, run.inputs) == condition['equals']
            record.escalation_result['condition_branch'] = matched
            if not matched:
                record.status = 'skipped'
                return
        pure = record.mode in ('condition', 'condition_branch') or record.mode == 'script_transform' and not spec.get('script_id')
        local_analysis = (record.mode in ('llm_skill_analysis', 'llm_prompt')
                          and self.analysis_handler is analysis_preview)
        if run.ephemeral_debug and not allow_side_effects and not pure:
            record.escalation_result.update(dry_run=True, side_effects_blocked=True)
            if not local_analysis:
                record.status = 'succeeded'
                record.escalation_result['debug_logs'] = ['仅预览：未执行脚本、外部分析器、网络、工具或子工作流']
                return
        self._admit_effect(run, record)
        if record.mode == 'agent_task':
            if run.ephemeral_debug or self.agent_steps is None:
                raise ValueError('WORKFLOW_AGENT_EXECUTOR_UNAVAILABLE')
            record.status = 'waiting_for_agent'
            self.agent_steps.submit(run, spec, record)
            return
        if record.mode in ('condition', 'condition_branch', 'script_transform', 'http_request', 'tool_call',
                           'subworkflow', 'approval_wait', 'notification'):
            self._generic_debug_step(record, spec, outputs, definition, run)
            return
        if record.mode in ('deterministic_script', 'script', 'hybrid'):
            self._script_step(record, spec, run, definition, record_summary=record_summary)
            if record.status not in ('succeeded', 'escalated_recovered') or record.mode in ('deterministic_script', 'script'):
                return
            outputs = {**outputs, record.step_id: payload(record.output_snapshot)}
        context = AnalysisContext(outputs, run.inputs, record.input_snapshot, data_sources=self._sources(definition))
        if apply_analysis_gate(record, spec, run.steps):
            return
        self._analysis_step(record, spec, context, run=run)

    # LLM: 批执行体不能竞争原run写锁；所有授权仍按真实run/step进行，原账本由协调线程发表。
    # 函数用途: 记录副作用准入，不允许worker独自覆盖父步骤或运行快照。
    def _admit_effect(self, run, record):
        from .release_gate import WorkflowEffectAdmission
        admission = self._authorize_run(run, record.step_id)
        if isinstance(admission, WorkflowEffectAdmission):
            record.escalation_result['review_refs'] = list(admission.review_refs)
        if not run.ephemeral_debug and not getattr(run, '_batch_worker', False):
            # 真正启动前先记未知义务；崩溃后不能把“还没发表输出”解释成未执行。
            record.escalation_result['effect_started'] = True
            published = self.store.save_step_result(run.workflow_run_id, record)
            run.revision, run.effect_outcome = published.revision, 'unknown'

    # LLM: 只在原执行锁内协调批；恢复沿原步骤保留成功批，worker不保存run或父步骤。
    # 函数用途: 在一个运行中切批、逐批发表并按序聚合输出。
    def _foreach_step(self, record, spec, outputs, definition, run):
        config = validate_foreach(spec['foreach'])
        items = resolve_items(config['from'], run.inputs, outputs)
        plan = pack_items(items, batch_size=config['batch_size'], max_batch_tokens=config['max_batch_tokens'])
        if not record.batches:
            record.batches = [{'index': index, 'status': 'pending', 'item_count': len(batch['items']),
                'input_range': [batch['start'], batch['end']], 'oversize': batch['oversize'],
                'started_at': 0, 'finished_at': 0, 'error_code': '', 'analysis_blocked': False}
                for index, batch in enumerate(plan)]
        if len(record.batches) != len(plan) or any(row['input_range'] != [batch['start'], batch['end']]
                for row, batch in zip(record.batches, plan, strict=True)):
            from .batching import ForeachInvalid
            raise ForeachInvalid('恢复批计划与冻结输入不一致')
        record.escalation_result['effect_started'] = True

        # LLM: 所有写只发生在协调线程，原Store返回的最新revision随即同步。
        # 函数用途: 发表当前逐批账本及进度摘要。
        def persist():
            record.batches_summary = batch_summary(record.batches)
            # 重算而非粘住旧阻断：恢复修好失败批后，旧健康也必须随账本更新。
            healths = [health for row in record.batches if (health := query_health(row.get('query_health'))) is not None]
            blocked = [health for health in healths if health['analysis_blocked'] is True]
            if healths:
                record.escalation_result['query_health'] = payload((blocked or healths)[0])
            else:
                record.escalation_result.pop('query_health', None)
            published = self.store.save_step_result(run.workflow_run_id, record)
            run.revision, run.effect_outcome = published.revision, 'unknown'

        # LLM: started证明先于派发写入；失败时不得把未分派批计作成功。
        # 函数用途: 为即将启动的批准备独立attempt及账本。
        def prepare(index):
            _check_stop()
            row = record.batches[index]
            row.update(status='running', started_at=time.time(), finished_at=0, error_code='', analysis_blocked=False,
                       attempt_id=uuid.uuid4().hex)
            persist()

        # LLM: 只浅复制引用路径，避免每批复制完整候选列表；分析上下文随后复制本批投影。
        # 函数用途: 将foreach源路径替换为当前分片。
        def batch_view(value, parts, batch):
            if not parts:
                return batch
            copied = dict(value)
            copied[parts[0]] = batch_view(value[parts[0]], parts[1:], batch)
            return copied

        # LLM: 每worker有独立DTO和contextvars；不修改共享engine、定义或父run。
        # 函数用途: 沿原执行链执行一个批，返回真实步骤结果。
        def execute(index):
            batch = plan[index]['items']
            child_spec = payload(spec)
            child_spec.pop('foreach')
            child_spec['_batch_index'] = index
            child_spec['inputs_map'] = {key: value for key, value in child_spec.get('inputs_map', {}).items() if key != config['as']}
            parts = config['from'].split('.')
            inputs = batch_view(run.inputs, parts[1:], batch) if parts[0] == 'inputs' else dict(run.inputs)
            child_outputs = batch_view(outputs, parts, batch) if parts[0] != 'inputs' else dict(outputs)
            inputs = {**inputs, config['as']: batch}
            child_run = replace(run, inputs=inputs)
            child_run._batch_worker = True
            child = replace(_pending(spec), status='running', started_at=record.batches[index]['started_at'], input_snapshot=payload(inputs))
            child.escalation_result['attempt_id'] = record.batches[index]['attempt_id']
            try:
                _check_stop()
                self._perform(child, child_spec, child_outputs, definition, child_run)
                _check_stop()
            except (KeyboardInterrupt, SystemExit, ToolCancelled, InterruptedError):
                raise
            except Exception as exc:
                child.status, child.needs_repair = 'failed', True
                child.error_code, child.error_message = getattr(exc, 'error_code', 'STEP_EXECUTION_ERROR'), str(exc)
            child.ended_at = time.time()
            return child

        # LLM: 结果按index放回原槽，完成乱序不会改变聚合顺序；错误批绝不使用confirmed。
        # 函数用途: 发表单批结果并告诉调度器是否停止新增派发。
        def publish(index, child):
            blocked = child.error_code == 'ANALYSIS_BLOCKED_NO_DATA'
            status = 'blocked' if blocked else 'succeeded' if child.status in ('succeeded', 'escalated_recovered') else 'skipped' if child.status == 'skipped' else 'failed'
            record.batches[index].update(status=status, finished_at=child.ended_at, error_code=child.error_code,
                analysis_blocked=blocked, output=payload(child.output_snapshot),
                query_health=payload(child.escalation_result.get('query_health')), error_message=child.error_message)
            record.llm_involved = record.llm_involved or child.llm_involved
            if child.escalation_result.get('analysis_isolation_replaced') is True:
                record.escalation_result['analysis_isolation_replaced'] = True
            persist()
            return status in ('failed', 'blocked')

        persist()
        indices = [row['index'] for row in record.batches if row['status'] != 'succeeded']
        dispatch_batches(indices, execute, publish, prepare=prepare, max_parallel=config['max_parallel'],
                         fail_fast=config['on_batch_error'] == 'fail_fast')
        values = [row['output'] for row in record.batches if row['status'] == 'succeeded']
        record.output_snapshot = merge_outputs(values) if config['mode'] == 'aggregate' else values
        failed = record.batches_summary['failed'] + record.batches_summary['blocked']
        record.status, record.needs_repair = ('failed', True) if failed and config['on_batch_error'] == 'fail_fast' else ('succeeded', False)
        record.error_code = 'WORKFLOW_BATCH_FAILED' if record.needs_repair else ''
        record.error_message = '批处理未完成，见逐批账本' if record.needs_repair else ''

    # LLM: 仅debug通过副作用门后进入；审批不自批，通知只走原Owner通知账/outbox并保留真实状态，不写正式run。
    # 函数用途: 计算条件/转换、审批等待或显式通知；其它外部节点仍需可信执行器，脚本源合同不变。
    def _generic_debug_step(self, record, spec, outputs, definition, run):
        from .debug_nodes import approval_wait_result, evaluate_expression
        config = spec.get('config', {})
        if record.mode in ('condition', 'condition_branch'):
            if 'expression' in config:
                matched = bool(evaluate_expression(config['expression'], record.input_snapshot))
            else:
                matched = record.escalation_result.get('condition_branch')
                if matched is None:
                    raise ValueError('条件节点缺少表达式或结构化比较')
            record.escalation_result['condition_branch'] = matched
            record.output_snapshot = {'matched': matched}
        elif record.mode == 'script_transform' and not spec.get('script_id'):
            if 'expression' in config:
                output = evaluate_expression(config['expression'], record.input_snapshot)
            else:
                output = {key: output_value(value, outputs, record.input_snapshot)
                          if isinstance(value, dict) and 'step_id' in value else payload(value)
                          for key, value in config.get('mapping', record.input_snapshot).items()}
            if not isinstance(output, dict):
                raise ValueError('转换输出必须是JSON对象')
            record.output_snapshot = output
        elif record.mode == 'script_transform':
            self._script_step(record, spec, run, definition, record_summary=False)
            return
        elif record.mode == 'approval_wait':
            record.output_snapshot = approval_wait_result(record.input_snapshot)
        elif record.mode == 'notification':
            title, message = config.get('title', record.name), config.get('message', record.input_snapshot.get('message', ''))
            if not isinstance(title, str) or not isinstance(message, str):
                raise ValueError('通知title/message必须是字符串')
            notice = redact_sensitive_value({'title': title, 'message': message, 'payload': record.input_snapshot})
            dispatched = emit_asset_im_notification(self.owner_home, event_type='workflow_debug_notification', **notice)
            record.output_snapshot = {'dispatch_result': dispatched}
        elif not run.ephemeral_debug or not callable(self.debug_node_handler):
            record.status, record.error_code = 'failed', 'NODE_EXECUTOR_UNAVAILABLE'
            record.error_message = '未配置可信单节点执行器'
            return
        else:
            context = AnalysisContext(outputs, run.inputs, record.input_snapshot)
            output = payload(self.debug_node_handler(payload(spec), context, timeout_seconds=spec['timeout_seconds']))
            if not isinstance(output, dict):
                raise ValueError('节点执行器必须返回JSON对象')
            record.output_snapshot = output
        record.status = 'succeeded'

    # LLM: 当前授权workspace/Owner定义决定开关，临时内联仍受同ID禁用约束，不改冻结步骤。
    # 函数用途: 在新建、恢复前拒绝已关闭工作流。
    def _require_workflow_enabled(self, definition):
        descriptor = definition['descriptor']
        current = WorkflowStore(self.owner_home, workspace_root=self.workspace_root).get_workflow(descriptor['id'])
        business = current.descriptor.metadata.get('business_workflow', {}) if current is not None else {}
        if (business.get('lifecycle', 'active') != 'active'
                or definition.get('enabled', True) is False or descriptor.get('enabled', True) is False
                or current is not None and not current.enabled):
            raise AssetDisabledError('workflow', descriptor['id'])

    # LLM: 同workspace/Owner只读核验，明确False硬拒绝；确切入口缺失交后续版本门归类，其它坏元数据不吞。
    # 函数用途: 在运行任一步前核对脚本和显式技能是否停用。
    def _require_step_enabled(self, spec):
        if spec.get('script_id'):
            with bind_script_workspace(self.owner_home, self.workspace_root):
                try:
                    script = load_script(self.owner_home, spec['script_id'])[1]
                except _EntrypointMissing:
                    script = None
            if script is not None and script.get('enabled', True) is False:
                raise AssetDisabledError('script', spec['script_id'])
        self._require_skills_enabled(spec.get('skill_ids', ()))

    # LLM: 精确来源缺失交原宿主resolver处理，已发现Owner技能的关闭不能被外部分析回调绕过。
    # 函数用途: 核对声明或动态解析出来的Skill运行闸。
    def _require_skills_enabled(self, references):
        for reference in references:
            try:
                descriptor = get_owner_skill_descriptor(self.owner_home, reference)
            except FileNotFoundError:
                continue
            if descriptor.get('enabled', True) is False:
                raise AssetDisabledError('skill', reference)

    # LLM: 源/漂移门不变；正式步骤在原调用链绑定run-out目标，debug清空目标，产物引用沿原步骤账持久化。
    # 函数用途: 沿原脚本链执行并保留成功/失败日志及调试合同，内存调试不写资产或运行账。
    def _script_step(self, record, spec, run, definition, *, record_summary=True):
        sources = definition.get('descriptor', definition).get('data_sources')
        options = {'override_data_sources': payload(sources)} if sources or definition.get('_override_data_sources') else {}
        if not record_summary:
            options['options'] = {'record_summary': False}
        frozen = definition.get('_frozen_scripts', {}).get(spec['step_id'])
        try:
            metadata = self._script_metadata(record.script_id)
        except (ValueError, FileNotFoundError, _EntrypointMissing):
            if frozen is None:
                raise
            metadata = None
        current = _script_pin(record.script_id, metadata) if metadata is not None else None
        drift = frozen is not None and (current is None or not _same_pin(frozen, current))
        if drift and not historical_pin_available(self.owner_home, frozen):
            result = _preflight_failure(record.script_id, metadata or {}, 'SCRIPT_REVISION_DRIFT', {})
        else:
            # 同一记录经内容门后冻结交原数据源门；当前CAS只是该旧门的读取一致性令牌，不再作为执行身份。
            with bind_run_out_target(self.owner_home, run.workflow_run_id, record.step_id,
                    record.escalation_result.get('attempt_id', ''), enabled=not run.ephemeral_debug), \
                    bind_script_execution_record(self.owner_home, record.script_id, self.workspace_root, metadata):
                result = run_script_with_datasources(self.owner_home, record.script_id, record.input_snapshot,
                    runner=self.runner, workspace_root=self.workspace_root, timeout_seconds=spec.get('timeout_seconds'),
                    expected_script_pin=frozen if drift else current, **options)
        if not record_summary:
            record.escalation_result['debug_logs'] = [text for text in
                (result.stdout_preview, result.stderr_preview) if text]
        record.output_snapshot, record.error_code = payload(result.output_data), result.error_code
        record.output_dir = getattr(result, 'output_dir', '')
        record.output_artifacts = tuple(payload(getattr(result, 'output_artifacts', ())))
        record.error_message = result.stderr_preview if not result.ok else ''
        record.status = 'succeeded' if result.ok else 'failed'
        record.needs_repair = not result.ok
        if isinstance(result.source_check, dict) and isinstance(result.source_check.get('query_health'), dict):
            record.escalation_result['query_health'] = payload(result.source_check['query_health'])
        if result.error_code.startswith('SCRIPT_RUN_OUT_'):
            # 文件归档损失不能被skip或模型修复输出伪装成成功。
            diagnostic = result.escalation_context.get('execution_error')
            if diagnostic:
                record.escalation_result['execution_error'] = redact_sensitive_value(payload(diagnostic))
            return
        if result.ok:
            if isinstance(record.output_snapshot, dict) and record.output_snapshot.get('_pause_for_resume') is True:
                record.status, record.needs_repair = 'paused_for_resume', False
            return
        policy = spec.get('on_error', 'escalate_to_agent')
        debug_logs = record.escalation_result.get('debug_logs', [])
        debug_metadata = {key: record.escalation_result[key] for key in
                          ('condition_branch', 'dry_run', 'side_effects_blocked') if key in record.escalation_result}
        identity = {key: value for key, value in record.escalation_result.items()
                    if key in ('attempt_id', 'review_refs', 'effect_started', 'query_health')}
        record.escalation_result = {**redact_sensitive_value(payload(result.escalation_context or {})), **identity}
        if not record_summary:
            record.escalation_result.update(debug_logs=debug_logs, **debug_metadata)
        cleanup = result.escalation_context.get('cleanup_error')
        record.escalation_result['repair_guide'] = redact_sensitive_value({
            'failed_step_id': record.step_id, 'script_id': record.script_id, 'error_code': result.error_code,
            'io_contract': payload(result.escalation_context.get('io_contract', {})),
            'resume_options': ['patched_step_output', 'updated_data_sources', 'rerun_from_step']})
        hard_stop = result.error_code in {'SCRIPT_REVISION_DRIFT', 'DATASOURCE_MISSING', 'DATASOURCE_EMPTY'}
        if hard_stop:
            record.status = 'paused_for_resume' if policy == 'escalate_to_agent' else 'failed'
            return
        if policy == 'skip' and not cleanup:
            record.status, record.needs_repair = 'skipped', False
            return
        if policy != 'escalate_to_agent':
            return
        record.status = 'paused_for_resume'
        if self.escalation_handler is None:
            return
        record.llm_involved = True
        self._admit_effect(run, record)
        recovered = payload(self._escalate(spec, result, run))
        if not isinstance(recovered, dict):
            raise ValueError('兜底回调必须返回 JSON 对象')
        record.escalation_result.update({key: value for key, value in redact_sensitive_value(recovered).items()
                                         if key not in ('repair_guide', 'cleanup_error')})
        if cleanup:
            return
        if recovered.get('recovered') is True:
            patched = recovered.get('patched_output', recovered.get('output_data'))
            output, error = parse_script_output(json.dumps(patched), result.escalation_context.get('io_contract', {}))
            if error:
                record.error_code = error
                return
            record.status, record.output_snapshot, record.needs_repair = 'succeeded', output, False

    # LLM: 升级只走三参协议，内部TypeError不能触发换签名重试。
    # 函数用途: 向兜底传入独立步骤、脚本结果和运行快照。
    def _escalate(self, spec, result, run):
        return self.escalation_handler(SimpleNamespace(**payload(spec)), replace(result, **payload(result)),
                                       WorkflowRunRecord.from_dict(run.to_dict())) or {}

    # LLM: 动态解析和宿主详情中的禁用同样硬拒绝，保留原机器码；缺技能/分析器仍不走脚本兜底。
    # 函数用途: 核对引用并读取正文，用完整上下文调用真实分析器。
    def _analysis_step(self, record, spec, context, *, run=None):
        try:
            resolved = self._resolve_skills(spec, context)
            self._require_skills_enabled(resolved)
            record.resolved_skill_ids = resolved
            details = [] if self.skill_details_resolver is None else self.skill_details_resolver(list(resolved))
            if not isinstance(details, list) or not all(isinstance(item, dict) for item in details):
                raise ValueError('技能正文读取器必须返回对象列表')
            for item in details:
                if item.get('enabled', True) is False:
                    raise AssetDisabledError('skill', str(item.get('id', '')))
            context = AnalysisContext(context.prior_outputs, context.run_inputs, context.step_inputs, details, context.data_sources,
                                      run_params=isolation_params(spec, record))
        except (LookupError, FileNotFoundError, ValueError) as exc:
            record.status, record.needs_repair = 'failed', True
            record.error_code, record.error_message = getattr(exc, 'error_code', 'SKILL_NOT_FOUND'), str(exc)
            return
        if self.analysis_handler is None:
            record.status, record.needs_repair = 'failed', True
            record.error_code, record.error_message = 'ANALYZER_UNAVAILABLE', '未提供真实技能分析回调'
            return
        record.llm_involved = record.llm_involved or getattr(self.analysis_handler, 'llm_involved', True) is not False
        if run is not None:
            self._admit_effect(run, record)
        _check_stop()
        try:
            output = self.analysis_handler(payload(spec), context, resolved)
        finally:
            attempts = getattr(context, 'analysis_attempts', None)
            if isinstance(attempts, list):
                record.escalation_result['analysis_attempts'] = redact_sensitive_value(payload(attempts))
        _check_stop()
        output = payload(output)
        if not isinstance(output, dict):
            raise ValueError('分析回调必须返回 JSON 对象')
        record.output_snapshot = output
        record.status = 'paused_for_resume' if output.get('_pause_for_resume') is True else 'succeeded'
        record.needs_repair = False

    # LLM: 注入解析器是宿主权威；无解析器只认真实Owner技能，准确来源不能被替换。
    # 函数用途: 按固定step/context协议解析引用，拒绝遗漏必需Skill。
    def _resolve_skills(self, spec, context):
        requested = skill_sequence(spec.get('skill_ids', ()))
        skill_sequence(spec.get('skill_tags', ()))
        if self.skill_resolver is None:
            for skill_id in requested:
                get_owner_skill_descriptor(self.owner_home, skill_id)
            resolved = requested
        else:
            isolated = AnalysisContext(context.prior_outputs, context.run_inputs, context.step_inputs,
                                       context.skill_details, context.data_sources)
            resolved = skill_sequence(self.skill_resolver(SimpleNamespace(**payload(spec)), isolated))
        missing = [item for item in requested if not any(item == actual or ':' not in item
                   and canonical_skill_name(actual) == item for actual in resolved)]
        if missing:
            raise FileNotFoundError('缺少必需Skill: ' + ', '.join(missing))
        return tuple(dict.fromkeys(resolved))

    # LLM: 数据源来自冻结工作流显式配置或其脚本当前权威配置；禁止将不同形状悄悄覆盖。
    # 函数用途: 给动态技能匹配提供完整的已配置数据源。
    def _sources(self, definition):
        descriptor = definition.get('descriptor', definition)
        if descriptor.get('data_sources') or definition.get('_override_data_sources'):
            return payload(descriptor['data_sources'])
        merged = None
        for script_id in dict.fromkeys(s.get('script_id') for s in definition['steps'] if s.get('script_id')):
            metadata = self._script_metadata(script_id)
            sources = metadata.get('data_sources', {})
            if merged is None:
                merged = payload(sources)
            elif isinstance(merged, dict) and isinstance(sources, dict):
                merged.update(payload(sources))
            elif isinstance(merged, list) and isinstance(sources, list):
                merged.extend(payload(sources))
            else:
                raise ValueError('数据源配置形状不一致')
        return merged if merged is not None else {}
