"""显式工作流策略驱动的有限无状态重新规划；计划正文不直接批准任何业务副作用。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from .models import AnalysisContext, StepExecutionRecord, payload


def validate_rework_policy(definition):
    policy = definition.get('descriptor', {}).get('metadata', {}).get('workflow_rework_policy')
    if policy is None:
        return None
    if (not isinstance(policy, dict) or set(policy) - {'replan_step_id', 'max_rounds', 'max_no_progress_rounds'}
            or not isinstance(policy.get('replan_step_id'), str) or not policy['replan_step_id']
            or any(type(policy.get(key, default)) is not int or not 1 <= policy.get(key, default) <= 100
                   for key, default in [('max_rounds', 3), ('max_no_progress_rounds', 2)])):
        raise ValueError('WORKFLOW_REWORK_POLICY_INVALID')
    spec = next((item for item in definition.get('steps', []) if item['step_id'] == policy['replan_step_id']), None)
    review_policy = definition.get('descriptor', {}).get('metadata', {}).get('workflow_review_policy', {})
    if not isinstance(review_policy, dict):
        raise ValueError('WORKFLOW_REVIEW_POLICY_INVALID')
    gated = review_policy.get('steps', [])
    if (spec is None or spec.get('mode') != 'llm_skill_analysis'
            or not isinstance(spec.get('config'), dict)
            or spec['config'].get('analysis_isolation') != 'stateless_skill'
            or spec.get('script_id') or spec.get('tool_spec') or spec.get('foreach')
            or spec['step_id'] in gated):
        raise ValueError('WORKFLOW_REPLAN_REQUIRES_UNGATED_STATELESS_ANALYSIS')
    return {**policy, 'max_rounds': policy.get('max_rounds', 3),
        'max_no_progress_rounds': policy.get('max_no_progress_rounds', 2)}


def perform_replan(engine, run, step, feedback):
    """仅已通过当前授权且未启动副作用的review反馈可进入；原失败步骤保存唯一进度。"""
    from ..common.cancellation import ToolCancelled
    from .engine import _check_stop
    definition = engine.store.load_workflow_snapshot(run.workflow_run_id)
    policy = validate_rework_policy(definition)
    if policy is None or step.escalation_result.get('effect_started'):
        return False
    _check_stop()
    signature = hashlib.sha256(json.dumps({key: feedback.get(key) for key in
        ('candidate_hash', 'scope_snapshot', 'business_context')}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    previous = step.escalation_result.get('rework_state', {})
    if (not isinstance(previous, dict) or previous and previous.get('schema') != 'workflow.rework.v1'
            or any(type(previous.get(key, 0)) is not int or not 0 <= previous.get(key, 0) <= 1000
                   for key in ('rounds', 'no_progress_rounds'))):
        raise ValueError('WORKFLOW_REWORK_STATE_INVALID')
    rounds = previous.get('rounds', 0)
    no_progress = int(previous.get('no_progress_rounds', 0)) + 1 if previous.get('progress_hash') == signature else 0
    state = {'schema': 'workflow.rework.v1', 'rounds': rounds, 'no_progress_rounds': no_progress,
        'progress_hash': signature, 'feedback_ref': feedback['requirement_id'],
        'replan_step_id': policy['replan_step_id'], 'max_rounds': policy['max_rounds'],
        'max_no_progress_rounds': policy['max_no_progress_rounds']}
    step.status, step.needs_repair = 'paused_for_resume', True
    step.escalation_result['rework_state'] = state
    if rounds >= policy['max_rounds'] or no_progress >= policy['max_no_progress_rounds']:
        step.error_code = 'WORKFLOW_REWORK_LIMIT_REACHED' if rounds >= policy['max_rounds'] else 'WORKFLOW_REWORK_NO_PROGRESS'
        step.error_message = '重新规划达到冻结策略上限；保留原运行和审核门，请修订实际候选或由用户调整后续计划，禁止原样自动重试。'
        state.update(state='limit_reached', retry_same_configuration=False,
            next_action='revise_candidate_and_request_fresh_review')
        return True
    spec = next(item for item in definition['steps'] if item['step_id'] == policy['replan_step_id'])
    engine._require_step_enabled(spec)
    attempt_id = uuid.uuid4().hex
    state.update(rounds=rounds + 1, state='planning', attempt_id=attempt_id)
    # 调用前保存计数和准确attempt；崩溃不能恢复为未消耗的新一轮。
    published = engine.store.save_step_result(run.workflow_run_id, step)
    run.revision = published.revision
    plan = StepExecutionRecord(step_id=spec['step_id'], name=spec.get('name', spec['step_id']),
        mode='llm_skill_analysis', status='running', started_at=time.time(), ended_at=0,
        duration_ms=0, llm_involved=False, escalation_result={'attempt_id': attempt_id},
        input_snapshot={'rework_feedback': {'error_code': 'WORKFLOW_REVIEW_CHANGES_REQUESTED',
            'requirement_id': feedback['requirement_id'], 'input_version': feedback['input_version'],
            'round': state['rounds']}})
    context = AnalysisContext({item.step_id: item.output_snapshot for item in run.steps
        if item.status in ('succeeded', 'escalated_recovered')}, run.inputs, plan.input_snapshot,
        data_sources=engine._sources(definition))
    # 原分析handler仍使用原core轮/global admission；不重放规划步骤原先的副作用准入或任何成功业务步骤。
    try:
        engine._analysis_step(plan, spec, context)
    except (ToolCancelled, InterruptedError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        from ..runtime_errors import compact_error_message, fallback_error_code
        plan.status, plan.error_code, plan.error_message = 'failed', fallback_error_code(error), f'{type(error).__name__}: {compact_error_message(error)}'
        plan.escalation_result['exception_type'] = type(error).__name__
    _check_stop()
    state.update(next_action='revise_candidate_and_request_fresh_review', state='ready' if plan.status in ('succeeded', 'escalated_recovered') else 'failed',
        plan=payload(plan.output_snapshot), error_code=plan.error_code,
        error_type=plan.escalation_result.get('exception_type', ''),
        analysis_attempts=payload(plan.escalation_result.get('analysis_attempts', [])))
    step.llm_involved = step.llm_involved or plan.llm_involved
    step.error_code = 'WORKFLOW_REPLAN_READY' if state['state'] == 'ready' else 'WORKFLOW_REPLAN_FAILED'
    step.error_message = ('已完成一轮无状态重新规划；计划只作修订依据，请修改真实候选并重新独立审核，原步骤未执行。'
        if state['state'] == 'ready' else plan.error_message)
    return True
