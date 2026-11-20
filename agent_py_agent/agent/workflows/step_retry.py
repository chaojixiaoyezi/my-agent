"""工作流步骤重试与退避调度器 (WF-03)。

严格实现单步重试策略、退避等待与防不确定副作用重放守卫。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from threading import Event
from typing import Any

from ..common.cancellation import ToolCancelled, current_cancellation_token, raise_if_cancelled
from ..concurrency.interrupt import is_interrupted
from ..contracts.error_taxonomy import error_contract

# 旧工作流回执中的确切传输错误码，按同一工具超时合同判定。
_LEGACY_TRANSIENT_CODES = {
    'TIMEOUT', 'NETWORK_TIMEOUT', 'CONNECTION_REFUSED',
    'HTTP_503_SERVICE_UNAVAILABLE', 'HTTP_504_GATEWAY_TIMEOUT',
}
_SIDE_EFFECTING_MODES: tuple[str, ...] = (
    'tool_call',
    'notification',
    'subworkflow',
    'http_request',
    'agent_task',
)

_NON_RETRYABLE_STATUSES: tuple[str, ...] = (
    'succeeded',
    'escalated_recovered',
    'skipped',
    'waiting_for_agent',
    'paused_for_resume',
)


def _is_transient_error(error_code: str) -> bool:
    if not isinstance(error_code, str) or not error_code:
        return False
    return error_contract('TOOL_TIMEOUT' if error_code in _LEGACY_TRANSIENT_CODES else error_code).retryable


def _retry_code_allowed(codes, error_code):
    return (not codes or error_code in codes) and _is_transient_error(error_code)


def _replay_proof(spec, step):
    facts = step.escalation_result
    receipt = facts.get('tool_receipt')
    outcome = facts.get('effect_outcome') or (receipt.get('effect_outcome') if isinstance(receipt, dict) else '')
    if outcome:
        return 'not_started' if outcome == 'not_started' else None
    if facts.get('effect_started') or facts.get('effect_state') in ('admitted', 'unknown'):
        return None
    if step.mode in _SIDE_EFFECTING_MODES or _get_spec_val(spec, 'script_id'):
        return None
    return 'not_started'


def retry_will_replay(spec: Any, step: Any) -> bool:
    policy = _get_spec_val(spec, 'retry_policy')
    return (isinstance(policy, dict) and bool(policy) and step.attempt_count < step.max_attempts
            and _retry_code_allowed(policy.get('retryable_codes'), step.error_code or 'STEP_EXECUTION_ERROR')
            and _replay_proof(spec, step) is not None)


class RetryWake(Exception):
    """将待提交的重试预约交还 DAG 协调线程。"""

    def __init__(self, step, expected_attempt_id):
        super().__init__(step.step_id)
        self.step = step
        self.expected_attempt_id = expected_attempt_id


def pending_retry(step):
    retry = step.escalation_result.get('retry', {})
    return retry if retry.get('state') == 'waiting' else None


def continuing_retry(step):
    facts = step.escalation_result
    return bool(facts.get('retry')) and facts.get('effect_outcome') == 'not_started'


def wait_for_retry(engine, deadline):
    signal = current_cancellation_token() or Event()
    while (remaining := deadline - time.time()) > 0:
        _check_stop_cleanly(engine)
        signal.wait(min(remaining, 0.2))
    _check_stop_cleanly(engine)


def start_retry(step, *, reserve_effect=False):
    facts = step.escalation_result
    facts['retry'] = {**facts['retry'], 'state': 'running'}
    facts.pop('effect_outcome', None)
    facts.pop('effect_state', None)
    if reserve_effect:
        facts['effect_state'] = 'reserved'


def _check_stop_cleanly(engine: Any = None) -> None:
    raise_if_cancelled()
    if is_interrupted():
        raise InterruptedError('workflow interrupted')
    if engine is not None and hasattr(engine, '_check_stop') and callable(engine._check_stop):
        engine._check_stop()


def _get_spec_val(spec: Any, key: str, default: Any = None) -> Any:
    if isinstance(spec, dict):
        return spec.get(key, default)
    return getattr(spec, key, default)


def run_step_with_retry(
    *,
    engine: Any,
    step: Any,
    spec: Any,
    outputs: dict[str, Any],
    definition: dict[str, Any] | Any,
    run: Any,
    execute_fn: Callable[[], Any],
    sleep_fn: Callable[[float], None] | None = None,
) -> None:
    """包装单步执行；支持退避重试策略与防非幂等不确定写重试守卫 (WF-03)。"""
    policy = _get_spec_val(spec, 'retry_policy')
    if not policy or not isinstance(policy, dict):
        step.attempt_count = 1
        step.max_attempts = 1
        if not step.escalation_result.get('attempt_id'):
            step.escalation_result['attempt_id'] = uuid.uuid4().hex
        execute_fn()
        return

    max_cap = int(getattr(engine, 'max_step_retry_attempts', 10))
    max_attempts = min(max(1, int(policy.get('max_attempts', 1))), max_cap)
    delay_seconds = min(max(0.0, float(policy.get('delay_seconds', 0.0))), 60.0)
    backoff_factor = max(1.0, float(policy.get('backoff_factor', 1.0)))
    raw_codes = policy.get('retryable_codes')
    retryable_codes = tuple(raw_codes) if raw_codes else ()

    step.max_attempts = max_attempts
    initial_retry = step.escalation_result.get('retry', {})
    attempt = int(initial_retry.get('consumed_budget', 0)) + 1
    store = getattr(engine, 'store', None) if run is not None and not getattr(run, '_batch_worker', False) else None
    sleeper = sleep_fn or getattr(engine, 'retry_sleep', None)
    delay = delay_seconds
    for _ in range(1, attempt):
        delay = min(delay * backoff_factor, 60.0)
    if pending_retry(step):
        wait_for_retry(engine, initial_retry['next_eligible_at'])
        start_retry(step)
        if store is not None:
            run.revision = store.save_step_result(run.workflow_run_id, step).revision
    while True:
        step.attempt_count = attempt
        if not step.escalation_result.get('attempt_id'):
            step.escalation_result['attempt_id'] = uuid.uuid4().hex

        try:
            execute_fn()
        except (KeyboardInterrupt, SystemExit, ToolCancelled, InterruptedError):
            raise
        except Exception as exc:
            step.status = 'failed'
            step.error_code = step.error_code or getattr(exc, 'error_code', 'STEP_EXECUTION_ERROR')
            step.error_message = step.error_message or str(exc)
            if getattr(exc, 'effect_outcome', None) in ('not_started', 'failed', 'partial', 'unknown'):
                step.escalation_result['effect_outcome'] = exc.effect_outcome
            step.needs_repair = True

        # 非重试终态（成功、跳过、异步代理排队、等待恢复等）
        if step.status in _NON_RETRYABLE_STATUSES:
            return

        # 若状态不是 failed，不作重试
        if step.status != 'failed':
            return

        # 尝试写后超时对账 (WF-10 timeout-after-write reconciliation)
        owner_home = getattr(engine, 'owner_home', None) or (getattr(run, 'owner_home', None) if run else None)
        workflow_id = getattr(run, 'workflow_id', '') if run else ''
        effect_id = (getattr(step, 'effect_id', '')
                     or step.escalation_result.get('effect_id', '')
                     or step.escalation_result.get('idempotency_key', ''))

        if owner_home and workflow_id and effect_id:
            try:
                from .step_watermarks import reconcile_step_watermark
                reconciled = reconcile_step_watermark(owner_home, workflow_id, step.step_id, effect_id)
                if reconciled is not None:
                    step.status = 'succeeded'
                    step.error_code = ''
                    step.error_message = ''
                    step.needs_repair = False
                    step.watermark = reconciled.get('watermark')
                    if isinstance(reconciled.get('watermark'), dict) and 'output_snapshot' in reconciled['watermark']:
                        step.output_snapshot = reconciled['watermark']['output_snapshot']
                    step.escalation_result['reconciled_from_watermark'] = True
                    step.escalation_result['reconciliation_record'] = reconciled
                    return
            except (OSError, RuntimeError, ValueError) as error:
                # 读回不明时保留原错误，不以一次失败读回授权重放。
                step.escalation_result['retry_suppressed_reason'] = 'UNCERTAIN_EFFECT_REPLAY_FORBIDDEN'
                step.escalation_result['retry_recovery_action'] = 'reconcile_original_effect'
                step.escalation_result['reconciliation_error_type'] = type(error).__name__
                return

        # 次数已达上限
        if attempt >= max_attempts:
            return

        # 安全防线 1：错误码是否允许重试
        error_code = step.error_code or 'STEP_EXECUTION_ERROR'
        allowed = _retry_code_allowed(retryable_codes, error_code)
        proof = _replay_proof(spec, step)
        if proof is None and (allowed or error_code in retryable_codes):
            step.escalation_result['retry_suppressed_reason'] = 'UNCERTAIN_EFFECT_REPLAY_FORBIDDEN'
            step.escalation_result['retry_recovery_action'] = 'reconcile_original_effect'
            step.error_message = (step.error_message or step.error_code) + (
                '；当前记录无法证明副作用未发生。用 workflows 的 inspect_run 读取原 workflow_run_id，'
                '核对原操作回执或提交经验证的步骤输出后再恢复；不要原样重试。')
            return
        if not allowed:
            step.escalation_result['retry_suppressed_reason'] = (
                'ERROR_CODE_NOT_RETRYABLE' if retryable_codes else 'NON_TRANSIENT_ERROR')
            return

        now = time.time()
        old_attempt = step.escalation_result['attempt_id']
        retry_record = {
            'attempt': attempt, 'attempt_id': old_attempt, 'error_code': step.error_code,
            'error_message': step.error_message, 'next_delay_seconds': delay, 'timestamp': now,
        }
        history = [*step.escalation_result.get('retry_history', []), retry_record][-20:]
        facts = {**step.escalation_result, 'attempt_id': uuid.uuid4().hex, 'retry_history': history,
                 'retry': {'state': 'waiting', 'consumed_budget': attempt, 'replay_proof': proof,
                           'next_eligible_at': now + delay},
                 'effect_started': False, 'effect_state': 'not_started', 'effect_outcome': 'not_started'}
        # 上一次工具回执只可作为历史，不能授权下一次失败的重放。
        for key in ('tool_receipt', 'script_execution', 'reconciliation_record',
                    'reconciled_from_watermark', 'retry_suppressed_reason', 'retry_recovery_action'):
            facts.pop(key, None)
        candidate = replace(step, status='running', attempt_count=attempt + 1,
                            escalation_result=facts, error_code='', error_message='', needs_repair=False)
        _check_stop_cleanly(engine)
        if getattr(run, '_defer_step_retry', False):
            raise RetryWake(candidate, old_attempt)
        if store is not None:
            persisted = store.reserve_step_retry(run.workflow_run_id, candidate, expected_attempt_id=old_attempt)
            if persisted is None:
                step.escalation_result['retry_suppressed_reason'] = 'WORKFLOW_RUN_REVISION_CONFLICT'
                return
            run.revision = persisted.revision
        step.status, step.error_code, step.error_message, step.needs_repair = 'running', '', '', False
        step.escalation_result, step.attempt_count = facts, attempt + 1

        if delay > 0 and sleeper is not None:
            sleeper(delay)
            _check_stop_cleanly(engine)
        else:
            wait_for_retry(engine, facts['retry']['next_eligible_at'])
        start_retry(step)
        if store is not None:
            persisted = store.save_step_result(run.workflow_run_id, step)
            run.revision = persisted.revision
        attempt += 1
        delay = min(delay * backoff_factor, 60.0)
