"""原引擎每次副作用消费的审核闸；host reader 读原权威记录，JSON 不授批准权。"""
from __future__ import annotations

import hashlib
import json

from ..assets.descriptor import execution_definition_ref
from .business_scope import WorkflowPolicyRefusal
from .release_gate import WorkflowEffectAdmission, WorkflowReleaseGate


class WorkflowReconciliationRequired(WorkflowPolicyRefusal):
    """核验缺失是未执行恢复的确定性拒绝，不是本次恢复结果未知。"""
    error_code = code = 'WORKFLOW_RECONCILIATION_REQUIRED'

    def __init__(self, reason, **details):
        super().__init__(self.error_code, details={'reason': reason,
            'recommended_action': '核对原操作和目标结果，提供同运行、同步骤、同版本的可信核验。', **details})


# LLM: 继承str保持外部run_authorizer(record, phase)字符串回调100%向后兼容，同时以显式类型与preflight/resume/cancel控制阶段严格分离。
# 类用途: 标识工作流真实步骤副作用准入阶段，防止名为preflight/resume/cancel的合法步骤误走控制旁路（FLAG-5）。
class WorkflowEffectPhase(str):
    """工作流步骤副作用阶段载体；作为str子类兼容外部authorizer，同时在治理与业务授权门内与控制阶段严格区分。"""

    # LLM: 返回解包后的纯str步骤ID，供policy_provider与步骤表查询使用。
    # 函数用途: 提取不带子类身份的纯字符串步骤编号。
    @property
    def step_id(self) -> str:
        return str.__str__(self)


def workflow_candidate_hash(definition):
    # 同候选必须含冻结脚本内容，而不是资产统计修订、名称或范围标签。
    pins = {step: {key: value for key, value in pin.items() if key != 'revision'}
            for step, pin in definition.get('_frozen_scripts', {}).items()}
    candidate = {'definition': execution_definition_ref(definition, kind='workflow'), 'scripts': pins}
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def pending_review_not_started(record, step):
    """只消费原运行/步骤/尝试的未启动审核门事实，不把旧派发未知推成未执行。"""
    pending = step.escalation_result.get('pending_review')
    return (isinstance(pending, dict) and pending.get('schema') == 'workflow.pending-review.v1'
        and step.escalation_result.get('effect_started') is False
        and pending.get('owner_id') == record.owner_id
        and pending.get('workflow_run_id') == record.workflow_run_id
        and pending.get('step_id') == step.step_id and bool(pending.get('attempt_id'))
        and pending['attempt_id'] == step.escalation_result.get('attempt_id')
        and pending.get('scope_snapshot') == record.scope_snapshot
        and pending.get('business_context') == record.business_context
        and bool(pending.get('candidate_hash')) and bool(pending.get('requirement_id'))
        and bool(pending.get('input_version')))


def _pending_review_resume(record):
    step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
    if step is None or not pending_review_not_started(record, step):
        return False
    # 已确认前缀可保留，任一其它未结业务/清理/未知工具回执仍必须先对账。
    for item in record.steps:
        result = item.escalation_result
        if result.get('cleanup_error'):
            return False
        if item is step:
            continue
        if item.status == 'pending' and not result:
            continue
        receipt = result.get('tool_receipt', {})
        if (item.status not in ('succeeded', 'escalated_recovered', 'skipped') or item.needs_repair
                or not isinstance(receipt, dict) or receipt.get('effect_outcome') == 'unknown'):
            return False
    return True


class WorkflowGovernance:
    def __init__(self, store, *, policy_provider, receipt_reader, authorization_check, effect_reader=None,
                 resolution_reader=None, clock=None):
        self.store, self.policy_provider = store, policy_provider
        self.effect_reader, self.resolution_reader = effect_reader, resolution_reader
        self.authorization_check = authorization_check
        self.gate = WorkflowReleaseGate(receipt_reader=receipt_reader,
                                         authorization_check=authorization_check, clock=clock)

    # LLM: 仅非WorkflowEffectPhase的控制阶段（preflight/resume/cancel）走恢复分类或控制放行；任何WorkflowEffectPhase（含名为
    #   preflight/resume/cancel的合法步骤）一律解包纯str步骤ID送policy_provider与WorkflowReleaseGate强制核验审核回执（FLAG-5）。
    # 函数用途: 按显式阶段载体区分控制阶段与真实步骤副作用准入，防止控制同名步骤绕过发布门。
    def require_effect(self, record, phase):
        from .engine import _requires_native_agent_resume, _requires_terminal_publication_resume

        is_control = not isinstance(phase, WorkflowEffectPhase)
        if is_control:
            if phase == 'resume' and _requires_terminal_publication_resume(record):
                # 原步骤已全部确认、只差原 run 终态发表：不选择或伪造当前步骤，也不进入通用对账。
                return None
            # 创建头记录/取消本身不启动业务；等待恢复只读原完成链，尾部新副作用仍逐步审核。
            unresolved = any((item.escalation_result.get('effect_started') and
                             (item.needs_repair or item.status == 'running')) or
                             ('pending_review' in item.escalation_result and not pending_review_not_started(record, item))
                             for item in record.steps)
            if (phase == 'resume' and not _requires_native_agent_resume(record)
                    and not _pending_review_resume(record) and (record.effect_outcome == 'unknown' or unresolved)):
                step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
                if step is None:
                    step = next((item for item in record.steps if item.status not in
                                 ('succeeded', 'escalated_recovered', 'skipped')), None)
                return self._reconcile(record, step)
            if phase in ('preflight', 'resume', 'cancel'):
                return None
        step_id = phase.step_id if isinstance(phase, WorkflowEffectPhase) else (
            str.__str__(phase) if isinstance(phase, str) else str(phase)
        )
        step = next((item for item in record.steps if item.step_id == step_id), None)
        policy = self.policy_provider(record, step_id)
        if step is None or not isinstance(policy, dict):
            raise PermissionError('WORKFLOW_GOVERNANCE_UNAVAILABLE')
        definition = self.store.load_workflow_snapshot(record.workflow_run_id)
        refs = self.gate.require(record, step, candidate_hash=workflow_candidate_hash(definition), policy=policy)
        return WorkflowEffectAdmission(review_refs=refs)

    def _reconcile(self, record, step, resolution_ref=None):
        reader = self.resolution_reader if resolution_ref else self.effect_reader
        if step is None or not callable(reader):
            raise WorkflowReconciliationRequired('reader_unavailable')
        result = reader(resolution_ref, record, step) if resolution_ref else reader(record, step)
        expected = {'owner_id': record.owner_id, 'workflow_run_id': record.workflow_run_id,
                    'task_id': record.business_context.get('task_id', ''), 'step_id': step.step_id,
                    'attempt_id': step.escalation_result.get('attempt_id'),
                    'candidate_hash': workflow_candidate_hash(self.store.load_workflow_snapshot(record.workflow_run_id))}
        if resolution_ref is not None:
            expected['resolution_ref'] = resolution_ref
        if (not isinstance(result, dict) or any(result.get(key) != value for key, value in expected.items())
                or not result.get('resolution_ref')
                or self.authorization_check(result, record) is not True
                or step.escalation_result.get('cleanup_error') and result.get('cleanup_verified') is not True):
            raise WorkflowReconciliationRequired('evidence_mismatch')
        refs = self.gate.require(record, step, candidate_hash=expected['candidate_hash'],
                                 policy=self.policy_provider(record, step.step_id))
        if result.get('state') == 'not_started':
            return WorkflowEffectAdmission(review_refs=refs, effect_not_started=True)
        if result.get('state') == 'committed' and isinstance(result.get('output'), dict):
            return WorkflowEffectAdmission(review_refs=refs, reconciled_step_id=step.step_id, reconciled_output=result['output'])
        raise WorkflowReconciliationRequired('state_unconfirmed')

    def require_resolution(self, record, reference):
        step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
        return self._reconcile(record, step, reference)


def bind_workflow_tool_operation(store, run, step, call, operation, *, task_id):
    """只在原 dispatcher 中把真实操作归属固定到当前步骤，不接纳工具正文中的身份。"""
    if operation is None:
        return
    from ..local_storage.tool_operations import ToolOperationStateError
    try:
        original = store.get_tool_operation(owner_id=run.owner_id, run_id=call.run_id,
            task_id=task_id, attempt_id=call.attempt_id, operation_id=operation.operation_id,
            tool_name=call.tool_name, args_hash=operation.args_hash)
    except (ToolOperationStateError, OSError) as error:
        # 业务体可能已经完成；绑定读取失败不能改签成可重试业务失败。旧结果保留且不授恢复权。
        step.escalation_result['tool_operation_binding_error'] = type(error).__name__
        return
    if original is None:
        return
    if (original.idempotency_key != call.idempotency_key
            or original.operation_id != operation.operation_id):
        raise WorkflowReconciliationRequired('operation_binding_mismatch')
    step.escalation_result['tool_operation_binding'] = {
        'schema': 'workflow.tool-operation-binding.v1',
        'workflow_run_id': run.workflow_run_id, 'step_id': step.step_id,
        'effect_attempt_id': step.escalation_result.get('attempt_id', ''),
        'execution_definition_ref': run.execution_definition_ref,
        'owner_id': original.owner_id, 'run_id': original.run_id,
        'task_id': original.task_id, 'attempt_id': original.attempt_id,
        'operation_id': original.operation_id, 'tool_name': original.tool, 'call_id': call.call_id,
        'args_hash': original.args_hash, 'idempotency_key': original.idempotency_key,
    }


class WorkflowOperationRecovery:
    """默认宿主只读核对原工具终态；不签发审核、不授发布权、不主动重做未知操作。"""

    def __init__(self, operation_store):
        self.operation_store = operation_store

    @staticmethod
    def _required(record, reason, **details):
        step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
        receipt = step.escalation_result.get('tool_receipt', {}) if step is not None else {}
        return WorkflowReconciliationRequired(reason, workflow_run_id=record.workflow_run_id,
            step_id=record.current_step_id, resolution_available=False,
            prior_effect_outcome=receipt.get('effect_outcome', record.effect_outcome), **details)

    def require_resolution(self, record, reference):
        step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
        receipt = step.escalation_result.get('tool_receipt', {}) if step is not None else {}
        raise WorkflowPolicyRefusal('WORKFLOW_RESOLUTION_UNAVAILABLE', details={
            'resolution_available': False, 'prior_effect_outcome': receipt.get('effect_outcome', record.effect_outcome),
            'recommended_action': '本宿主只自动核验原工具操作账本；外部核验引用须由可信宿主另行接入。'})

    def require_effect(self, record, phase):
        from .engine import _requires_native_agent_resume, _requires_terminal_publication_resume
        if isinstance(phase, WorkflowEffectPhase) or phase != 'resume':
            return None
        if _requires_native_agent_resume(record) or _requires_terminal_publication_resume(record):
            return None
        unresolved = record.effect_outcome == 'unknown' or any(
            (item.escalation_result.get('effect_started') and (item.needs_repair or item.status == 'running'))
            or ('pending_review' in item.escalation_result and not pending_review_not_started(record, item))
            for item in record.steps)
        if not unresolved or _pending_review_resume(record):
            return None
        step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
        if step is None:
            raise self._required(record, 'operation_step_unavailable')
        return self._verify(record, step)

    def _verify(self, record, step):
        from ..tooling.tool_operation_coordinator import replay_completed_tool_operation
        binding = step.escalation_result.get('tool_operation_binding')
        expected = {'schema': 'workflow.tool-operation-binding.v1',
            'owner_id': record.owner_id, 'workflow_run_id': record.workflow_run_id,
            'step_id': step.step_id, 'effect_attempt_id': step.escalation_result.get('attempt_id'),
            'execution_definition_ref': record.execution_definition_ref,
            'call_id': f'workflow-step:{record.workflow_run_id}:{step.step_id}',
            'idempotency_key': f'workflow-step:{record.workflow_run_id}:{step.step_id}'}
        receipt = step.escalation_result.get('tool_receipt', {})
        if (step.mode != 'tool_call' or step.escalation_result.get('cleanup_error')
                or not isinstance(binding, dict) or any(binding.get(key) != value for key, value in expected.items())
                or not record.execution_definition_ref
                or any(not isinstance(binding.get(key), str) or not binding[key]
                       for key in ('run_id', 'task_id', 'attempt_id', 'operation_id', 'tool_name', 'args_hash', 'idempotency_key'))
                or receipt.get('operation_id') != binding['operation_id']
                or receipt.get('tool_name') != binding['tool_name']):
            raise self._required(record, 'operation_binding_unavailable_or_mismatched')
        from ..local_storage.tool_operations import ToolOperationStateError
        from ..runtime_errors import compact_error_message
        try:
            original = self.operation_store.get_tool_operation(owner_id=record.owner_id,
                **{key: binding[key] for key in ('run_id', 'task_id', 'attempt_id', 'operation_id', 'args_hash')},
                tool_name=binding['tool_name'])
        except (ToolOperationStateError, OSError) as error:
            raise self._required(record, 'original_operation_read_rejected',
                diagnostic=compact_error_message(error)) from error
        if original is None or original.idempotency_key != binding['idempotency_key']:
            raise self._required(record, 'operation_unavailable_or_mismatched')
        result = replay_completed_tool_operation(original)
        if result.ok and original.result.get('effect_outcome') in {'committed', 'none'}:
            # 仍沿原公开工具输出脱敏；账本内容是数据，不成为模型指令或批准。
            from ..tooling.output_projection import model_tool_output_body
            from ..tooling.runtime_contracts import ToolContentBlock, ToolResult
            output = model_tool_output_body(tool=original.tool, output=result.output,
                result_envelope=result.result_envelope)
            public = ToolResult(call_id=binding['call_id'], tool_name=original.tool,
                status='succeeded', content_blocks=(ToolContentBlock('text', text=output),),
                handler_executed=True, effect_outcome=original.result['effect_outcome']).to_dict()
            public.update(ok=True, operation_id=original.operation_id)
            return WorkflowEffectAdmission(reconciled_step_id=step.step_id, reconciled_output=public)
        if (not result.ok and original.result.get('effect_outcome') == 'not_started'
                and original.result.get('handler_executed') is False
                and result.error_code != 'TOOL_OPERATION_OUTCOME_UNKNOWN'):
            return WorkflowEffectAdmission(effect_not_started=True)
        raise self._required(record, 'original_operation_not_confirmed')
