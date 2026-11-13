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

    def __init__(self, reason):
        super().__init__(self.error_code, details={'reason': reason,
            'recommended_action': '核对原操作和目标结果，提供同运行、同步骤、同版本的可信核验。'})


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
            unresolved = any(item.escalation_result.get('effect_started') and
                             (item.needs_repair or item.status == 'running') for item in record.steps)
            if phase == 'resume' and not _requires_native_agent_resume(record) and (record.effect_outcome == 'unknown' or unresolved):
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
