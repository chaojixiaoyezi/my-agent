"""原引擎每次副作用消费的审核闸；host reader 读原权威记录，JSON 不授批准权。"""
from __future__ import annotations

import hashlib
import json

from ..assets.descriptor import execution_definition_ref
from .release_gate import WorkflowEffectAdmission, WorkflowReleaseGate


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

    # LLM: 原生恢复仅核对原任务；终态发表恢复无当前步骤/新副作用；普通未知副作用仍对账，每次新增副作用仍绑定当前审核及原 attempt。
    # 函数用途: 按原生/终态发表恢复分类选择完成读取、终态补发、通用对账或真实副作用审核。
    def require_effect(self, record, phase):
        from .engine import _requires_native_agent_resume, _requires_terminal_publication_resume

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
        step = next((item for item in record.steps if item.step_id == phase), None)
        policy = self.policy_provider(record, phase)
        if step is None or not isinstance(policy, dict):
            raise PermissionError('WORKFLOW_GOVERNANCE_UNAVAILABLE')
        definition = self.store.load_workflow_snapshot(record.workflow_run_id)
        refs = self.gate.require(record, step, candidate_hash=workflow_candidate_hash(definition), policy=policy)
        return WorkflowEffectAdmission(review_refs=refs)

    def _reconcile(self, record, step, resolution_ref=None):
        reader = self.resolution_reader if resolution_ref else self.effect_reader
        if step is None or not callable(reader):
            raise PermissionError('WORKFLOW_RECONCILIATION_REQUIRED')
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
            raise PermissionError('WORKFLOW_RECONCILIATION_REQUIRED')
        refs = self.gate.require(record, step, candidate_hash=expected['candidate_hash'],
                                 policy=self.policy_provider(record, step.step_id))
        if result.get('state') == 'not_started':
            return WorkflowEffectAdmission(review_refs=refs, effect_not_started=True)
        if result.get('state') == 'committed' and isinstance(result.get('output'), dict):
            return WorkflowEffectAdmission(review_refs=refs, reconciled_step_id=step.step_id, reconciled_output=result['output'])
        raise PermissionError('WORKFLOW_RECONCILIATION_REQUIRED')

    def require_resolution(self, record, reference):
        step = next((item for item in record.steps if item.step_id == record.current_step_id), None)
        return self._reconcile(record, step, reference)
