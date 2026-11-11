"""工作流步骤重试与退避调度器 (WF-03)。

严格实现单步重试策略、退避等待与防不确定副作用重放守卫。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from ..common.cancellation import ToolCancelled, raise_if_cancelled
from ..concurrency.interrupt import is_interrupted

_TRANSIENT_ERROR_INDICATORS: tuple[str, ...] = (
    'timeout',
    'rate_limit',
    'network',
    'connection',
    'temporary',
    'busy',
    'transient',
    'io_error',
    'econnreset',
    'etimedout',
    'http_429',
    'http_500',
    'http_502',
    'http_503',
    'http_504',
    'service_unavailable',
    'bad_gateway',
    'gateway_timeout',
    'script_timeout',
    'script_transient_failure',
    'retryable',
)

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
    if not error_code or not isinstance(error_code, str):
        return False
    lower = error_code.lower()
    return any(indicator in lower for indicator in _TRANSIENT_ERROR_INDICATORS)


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
    idempotent = bool(policy.get('idempotent', False))

    step.max_attempts = max_attempts
    step.attempt_count = 1

    attempt = 1
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
            except Exception:
                pass

        # 次数已达上限
        if attempt >= max_attempts:
            return

        # 安全防线 1：错误码是否允许重试
        error_code = step.error_code or 'STEP_EXECUTION_ERROR'
        if retryable_codes:
            if error_code not in retryable_codes:
                step.escalation_result['retry_suppressed_reason'] = 'ERROR_CODE_NOT_RETRYABLE'
                return
        else:
            if not _is_transient_error(error_code):
                step.escalation_result['retry_suppressed_reason'] = 'NON_TRANSIENT_ERROR'
                return

        # 安全防线 2：有副作用模式且非幂等的已启动写入禁止重放
        if step.mode in _SIDE_EFFECTING_MODES:
            effect_started = bool(step.escalation_result.get('effect_started'))
            effect_outcome = step.escalation_result.get('effect_outcome')
            if not effect_outcome and isinstance(step.escalation_result.get('tool_receipt'), dict):
                effect_outcome = step.escalation_result['tool_receipt'].get('effect_outcome')
            if effect_started and effect_outcome != 'not_started':
                if not idempotent:
                    step.escalation_result['retry_suppressed_reason'] = 'UNCERTAIN_WRITE_NOT_IDEMPOTENT'
                    return

        # 计算退避延迟
        next_delay_seconds = min(delay_seconds * (backoff_factor ** (attempt - 1)), 60.0)

        # 记录有界 retry_history
        retry_history = step.escalation_result.setdefault('retry_history', [])
        retry_record = {
            'attempt': attempt,
            'attempt_id': step.escalation_result.get('attempt_id', ''),
            'error_code': step.error_code,
            'error_message': step.error_message,
            'next_delay_seconds': next_delay_seconds,
            'timestamp': time.time(),
        }
        retry_history.append(retry_record)
        if len(retry_history) > 20:
            del retry_history[:-20]

        # 更新步骤尝试计数
        step.attempt_count = attempt + 1
        step.max_attempts = max_attempts

        # 持久化中间步骤状态
        if engine is not None and getattr(engine, 'store', None) is not None and run is not None:
            try:
                engine.store.save_step_result(run.workflow_run_id, step)
            except Exception:
                pass

        # 检查停止
        _check_stop_cleanly(engine)

        # 退避等待
        sleeper = sleep_fn or getattr(engine, 'retry_sleep', time.sleep)
        if next_delay_seconds > 0:
            if sleeper is time.sleep and next_delay_seconds > 0.2:
                deadline = time.monotonic() + next_delay_seconds
                while time.monotonic() < deadline:
                    _check_stop_cleanly(engine)
                    time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
                _check_stop_cleanly(engine)
            else:
                sleeper(next_delay_seconds)
                _check_stop_cleanly(engine)

        # 为下一 attempt 铸造新 attempt_id
        step.escalation_result['attempt_id'] = uuid.uuid4().hex

        # 重置瞬态失败字段
        step.status = 'running'
        step.error_code = ''
        step.error_message = ''
        step.needs_repair = False

        attempt += 1
