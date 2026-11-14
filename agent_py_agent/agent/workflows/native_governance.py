"""把工作流显式审核策略接到原 ORCH 证据库，不创建审核副本。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from ..verification.repository import VerificationContext, VerificationEvidenceRepository
from ..verification.requirement_provenance import RequirementProvenanceRecord
from .business_scope import WorkflowPolicyRefusal
from .governance import WorkflowEffectPhase, WorkflowOperationRecovery, workflow_candidate_hash
from .release_gate import WorkflowReleaseGate

_WORKFLOW_AUTHOR = ContextVar('native_workflow_review_author', default=None)


@contextmanager
def workflow_review_author_scope(host, turn, *, authorize=None, workflow_run_id=""):
    """原宿主轮只在本调用栈传递作者身份，退出即撤销，不扩大caller授权。"""
    turn.require(host, turn.params)
    if authorize is not None and (not callable(authorize) or not workflow_run_id
            or workflow_run_id != turn.params.run_id):
        raise ValueError('WORKFLOW_REVIEW_AUTHORIZER_SCOPE_INVALID')
    entry = [host, turn.params, os.getpid(), threading.get_ident(), True]
    token = _WORKFLOW_AUTHOR.set(entry) if authorize is None else None
    def invoke(record, phase):
        # 仅原栈签出的闭包运输同一轮事实；worker不复用线程绑定HostTurn句柄，
        # 不复制Context，且每次仍由原操作库核真实run/attempt是否存活。
        if (not entry[4] or entry[2] != os.getpid() or not turn.active
                or turn.params is not entry[1]):
            raise ValueError('原宿主执行轮已退出或被替换')
        if (getattr(record, 'workflow_run_id', None) != workflow_run_id
                or getattr(record, 'owner_id', None) != host.home_paths.owner_id):
            # 子流程原生预检发生在自身host scope建立前；只复用父业务权限，
            # 不运送审核作者。副作用阶段及任意其它run仍不能借用父闭包。
            from .subworkflow_steps import _ACTIVE_SUBWORKFLOW_RUN_CHAIN
            if (getattr(record, 'owner_id', None) == host.home_paths.owner_id
                    and type(phase) is str and phase == 'preflight'
                    and getattr(record, 'trigger_kind', None) == 'subworkflow'
                    and getattr(record, 'parent_workflow_run_id', None) == workflow_run_id
                    and _ACTIVE_SUBWORKFLOW_RUN_CHAIN.get()[-1:] == (workflow_run_id,)):
                return authorize(record, phase)
            raise PermissionError('WORKFLOW_REVIEW_AUTHORIZER_RUN_MISMATCH')
        local = [host, entry[1], os.getpid(), threading.get_ident(), True, entry]
        local_token = _WORKFLOW_AUTHOR.set(local)
        try:
            if not current_workflow_author(host):
                raise PermissionError('WORKFLOW_REVIEW_AUTHOR_UNAVAILABLE')
            return authorize(record, phase)
        finally:
            local[4] = False
            _WORKFLOW_AUTHOR.reset(local_token)

    try:
        yield invoke if authorize is not None else None
    finally:
        entry[4] = False
        if token is not None:
            _WORKFLOW_AUTHOR.reset(token)


class WorkflowReviewPending(WorkflowPolicyRefusal):
    def __init__(self, binding, *, reason='WORKFLOW_REVIEW_REQUIRED', reviewers_needed=0):
        self.binding = binding
        self.reviewers_needed = reviewers_needed
        super().__init__(reason, details={**binding,
            'recommended_action': 'request_approval',
            'recovery_hint': '由另一受权执行者用 manage_requirement_provenance 对该 requirement_id 和 input_version 登记独立审核，再恢复原运行。'})


def review_binding(record, step, snapshot, *, tool_precondition=None):
    facts = {'schema': 'workflow.pending-review.v1', 'owner_id': record.owner_id,
        'workflow_run_id': record.workflow_run_id, 'step_id': step.step_id,
        'attempt_id': step.escalation_result['attempt_id'],
        'candidate_hash': workflow_candidate_hash(snapshot), 'scope_snapshot': record.scope_snapshot,
        'business_context': record.business_context}
    prior = step.escalation_result.get('pending_review')
    precondition = tool_precondition if tool_precondition is not None else (
        prior.get('tool_precondition') if isinstance(prior, dict) else None)
    if precondition is not None:
        facts['tool_precondition'] = precondition
        facts['candidate_hash'] = hashlib.sha256(json.dumps({'workflow_candidate_hash': facts['candidate_hash'],
            'tool_precondition': precondition}, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {**facts, 'requirement_id': 'workflow-review-' + digest[:40], 'input_version': digest}


def validated_workflow_review_policy(metadata):
    """唯一审核策略schema：省略代表无策略，空steps有效且无需审核服务。"""
    policy = metadata.get('workflow_review_policy')
    if policy is None:
        return None
    if (not isinstance(policy, dict) or set(policy) - {'steps', 'minimum_reviews', 'ttl_seconds', 'auto_dispatch', 'reviewer_model'}
            or not isinstance(policy.get('steps'), list)
            or not all(isinstance(item, str) and item for item in policy['steps'])
            or type(policy.get('auto_dispatch', True)) is not bool
            or ('reviewer_model' in policy and (not isinstance(policy['reviewer_model'], str)
                or not 1 <= len(policy['reviewer_model'].strip()) <= 128))
            or type(policy.get('minimum_reviews', 1)) is not int or not 1 <= policy.get('minimum_reviews', 1) <= 32
            or type(policy.get('ttl_seconds', 86400)) is not int or not 1 <= policy.get('ttl_seconds', 86400) <= 86400):
        raise WorkflowPolicyRefusal('WORKFLOW_REVIEW_POLICY_INVALID')
    return policy


class NativeWorkflowGovernance(WorkflowOperationRecovery):
    """审核仅消费原独立主体记录；原业务authorizer仍在每个副作用前检查当前权限。"""

    def __init__(self, operation_store, workflow_store, *, owner_home, owner_id, author_provider):
        super().__init__(operation_store)
        self.workflow_store = workflow_store
        self.repository = VerificationEvidenceRepository(owner_home)
        self.owner_home, self.owner_id, self.author_provider = str(owner_home), owner_id, author_provider

    def require_effect(self, record, phase):
        if not isinstance(phase, WorkflowEffectPhase):
            return super().require_effect(record, phase)
        snapshot = self.workflow_store.load_workflow_snapshot(record.workflow_run_id)
        policy = validated_workflow_review_policy(snapshot.get('descriptor', {}).get('metadata', {}))
        if policy is None:
            return None
        if phase.step_id not in policy['steps']:
            return None
        step = next(item for item in record.steps if item.step_id == phase.step_id)
        current = None
        spec = next(item for item in snapshot['steps'] if item['step_id'] == phase.step_id)
        if spec.get('mode') == 'tool_call':
            reader = getattr(phase, 'review_precondition_reader', None)
            if not callable(reader):
                raise WorkflowPolicyRefusal('WORKFLOW_TOOL_PRECONDITION_UNAVAILABLE', details={
                    'recommended_action': 'report_blocker',
                    'recovery_hint': '未执行；请由原工具宿主重建当轮授权调度器后重审，不能据缺失内容钉子的旧票执行。'})
            # 原业务authorizer已核space/scope；此处才按当轮工具宿主原权读取可变资源。
            current = reader(snapshot, step)
            prior = step.escalation_result.get('pending_review')
            previous = prior.get('tool_precondition') if isinstance(prior, dict) else None
            if prior is not None and previous != current:
                reason = 'WORKFLOW_REVIEW_PRECONDITION_MISSING' if previous is None else 'WORKFLOW_REVIEW_PRECONDITION_CHANGED'
                error = WorkflowReviewPending(prior, reason=reason)
                error.details['recommended_action'] = 'repair_against_acceptance_findings'
                error.details['recovery_hint'] = ('旧待审缺少资源钉子或资源/实参已变；本次未执行，旧票未补签。'
                    '请由原任务通过显式返工修订并创建新候选后重新独立审核，不要原样恢复或覆盖旧摘要。')
                raise error
        binding = review_binding(record, step, snapshot, tool_precondition=current)
        prior = step.escalation_result.get('pending_review')
        if prior is not None and prior != binding:
            raise WorkflowReviewPending(binding, reason='WORKFLOW_REVIEW_STALE')
        if record.owner_id != self.owner_id:
            raise WorkflowPolicyRefusal('WORKFLOW_OWNER_MISMATCH')
        requirement = self.repository.get_requirement(self.owner_id, binding['requirement_id'])
        if requirement is None:
            author = self.author_provider()
            if not isinstance(author, str) or not author:
                raise WorkflowPolicyRefusal('WORKFLOW_REVIEW_AUTHOR_UNAVAILABLE')
            requirement = self.repository.bind_requirement(
                VerificationContext(self.owner_id, '', record.workflow_run_id),
                RequirementProvenanceRecord(binding['requirement_id'], self.owner_id,
                    record.workflow_run_id, self.owner_home, binding['input_version'], (), author_principal_id=author))
        if (requirement.task_id != record.workflow_run_id or requirement.input_version != binding['input_version']
                or requirement.root != self.owner_home
                or not requirement.author_principal_id):
            raise WorkflowReviewPending(binding, reason='WORKFLOW_REVIEW_STALE')
        # 原库查询有200条上限；只接受同规范版本/机器scope的有效记录，不把任意pass升级为执行批准。
        reviews, has_more = self.repository.independent_review_page(
            self.owner_id, requirement_id=binding['requirement_id'], limit=200, candidate=requirement)
        if has_more:
            error = WorkflowReviewPending(binding, reason='WORKFLOW_REVIEW_HISTORY_INCOMPLETE')
            error.details['recommended_action'] = 'repair_against_acceptance_findings'
            error.details['recovery_hint'] = ('同候选审核历史超过200条，不能据截断窗口证明所有审核者已通过；'
                '本次未执行。请由原任务修订候选并绑定新版本重新审核，不要继续给原候选追加替票或盲目恢复。')
            raise error
        receipts = {}
        now = time.time()
        seen_reviewers = set()
        changes_requested = False
        for review in reviews:
            if (review.reviewed_input_version != binding['input_version'] or review.scope != 'workflow_effect.v1'
                    or review.author_principal_id != requirement.author_principal_id
                    or review.verdict not in {'pass', 'changes_requested'}):
                continue
            try:
                expires = datetime.fromisoformat(review.created_at.replace('Z', '+00:00')).timestamp() + policy.get('ttl_seconds', 86400)
            except (ValueError, TypeError):
                continue
            if expires <= now or review.reviewer_principal_id in seen_reviewers:
                continue
            # 只在同用途、同候选且有效的意见中选该审核者最新一条；别人的pass不撤销他的返工。
            seen_reviewers.add(review.reviewer_principal_id)
            if review.verdict == 'changes_requested':
                changes_requested = True
                continue
            receipts[review.review_id] = {'receipt_ref': review.review_id, 'owner_id': self.owner_id,
                'task_id': record.business_context.get('task_id', ''), 'workflow_run_id': record.workflow_run_id,
                'step_id': step.step_id, 'attempt_id': binding['attempt_id'], 'candidate_hash': binding['candidate_hash'],
                'author_id': requirement.author_principal_id, 'reviewer_id': review.reviewer_principal_id,
                'decision': 'approved', 'expires_at': expires}
        if changes_requested:
            raise WorkflowReviewPending(binding, reason='WORKFLOW_REVIEW_CHANGES_REQUESTED')
        minimum = policy.get('minimum_reviews', 1)
        missing = minimum - len({row['reviewer_id'] for row in receipts.values()})
        if missing > 0:
            raise WorkflowReviewPending(binding, reviewers_needed=missing)
        gate = WorkflowReleaseGate(receipt_reader=lambda ref, _run, _step: receipts[ref],
            authorization_check=lambda receipt, _run: receipt.receipt_ref in receipts)
        refs = gate.require(record, step, candidate_hash=binding['candidate_hash'], policy={
            'author_id': requirement.author_principal_id, 'review_refs': list(receipts), 'minimum_reviews': minimum})
        from .release_gate import WorkflowEffectAdmission
        return WorkflowEffectAdmission(review_refs=refs)


def current_workflow_author(host):
    """作者只由当前活跃core fence签出，不把业务actor标签或模型参数当执行者。"""
    from ..runtime_db.managed_operation_store import ToolOperationAuthorityRequest
    bound = _WORKFLOW_AUTHOR.get()
    if bound is not None and (bound[0] is not host or bound[2] != os.getpid()
            or bound[3] != threading.get_ident() or not bound[4]
            or len(bound) > 5 and not bound[5][4]):
        return ''
    params = bound[1] if bound is not None else getattr(host, '_current_run_params', None)
    store = getattr(getattr(host, 'tools', None), 'operation_store', None)
    run_id = str(getattr(params, 'run_id', '') or '')
    attempt_id = str(getattr(params, 'attempt_id', '') or '')
    if not run_id or not attempt_id or not callable(getattr(store, 'require_authority', None)):
        return ''
    store.require_authority(ToolOperationAuthorityRequest(owner_id=host.home_paths.owner_id,
        run_id=run_id, attempt_id=attempt_id, task_id=str(getattr(params, 'task_id', '') or ''),
        operation_id='workflow-review-author', tool_name='workflows'))
    return run_id


def builtin_workflow_templates():
    """通用阶段草案由原模板目录投影；用户补真实工具步骤，不把研判当回归或发布成功。"""
    def definition(identifier, name, stages, *, governed):
        metadata = {'workflow_review_policy': {'steps': ['review', 'regression', 'release'],
            'minimum_reviews': 1, 'ttl_seconds': 86400},
            'workflow_rework_policy': {'replan_step_id': 'architecture', 'max_rounds': 3,
                'max_no_progress_rounds': 2}} if governed else {}
        return {'descriptor': {'id': identifier, 'kind': 'workflow', 'name': name,
            'summary': ('保留架构、独立审核、回归和授权发布阶段；请将实际执行阶段配置为原生工具/脚本，研判不代表测试或发布成功。'
                if governed else '仅做无状态分析，不强制开发审核流程。'),
            'tags': ['development' if governed else 'analysis'], 'access_targets': [], 'data_sources': [],
            'credential_refs': [], 'io_contract': {}, 'metadata': metadata},
            'body_markdown': ('本模板不授予生产凭证或发布权限。架构、实现、审核、回归和发布须逐阶段保留真实证据；'
                '审核反馈要求改动时修订候选并重新审核，不以解释替代原工具执行回执。' if governed else '根据输入给出分析结论；不执行工具或发布。'),
            'steps': [{'step_id': key, 'name': label, 'mode': 'llm_skill_analysis',
                'on_error': 'fail', 'config': {'analysis_isolation': 'stateless_skill'}} for key, label in stages]}
    return (definition('builtin-simple-analysis', '简单分析', [('analysis', '分析')], governed=False),
        definition('builtin-development-stages', '开发阶段草案', [('architecture', '架构分析'),
            ('implementation', '实现规划'), ('review', '独立审核'), ('regression', '回归证据核对'),
            ('release', '授权发布准备')], governed=True))
