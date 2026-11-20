"""WF-08 并行依赖 DAG 调度器。

支持显式 depends_on、隐式 inputs_map 与结构化条件上游引用依赖推导、拓扑排序与圈检测；
通过有界线程池并行执行就绪节点计算，所有账本与状态写入均由协调线程集中收口，防止锁倒置与死锁。
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from copy import copy
from dataclasses import replace
from heapq import heapify, heappop, heappush
from itertools import count
from threading import Lock
from typing import Any

from ..assets.workflow_store import (
    execution_definition_ref as workflow_definition_ref,
)
from ..common.cancellation import ToolCancelled, raise_if_cancelled
from ..concurrency.interrupt import is_interrupted
from .batching import resolved_max_parallel
from .conditions import ConditionIndeterminate, evaluate_condition, mark_condition_indeterminate
from .models import StepExecutionRecord, payload
from .run_store import execution_receipt, replace_run_record, settle_step_effect
from .step_retry import RetryWake, continuing_retry, pending_retry, start_retry, wait_for_retry

_LOG = logging.getLogger(__name__)

SUCCESS = ('succeeded', 'escalated_recovered', 'skipped')


# LLM: 待执行步骤模板创建，保持与引擎原规范一致。
# 函数用途: 初始化规范步骤记录。
def _pending_step(spec: dict[str, Any]) -> StepExecutionRecord:
    return StepExecutionRecord(
        spec['step_id'],
        spec.get('name', spec['step_id']),
        spec.get('mode', 'deterministic_script'),
        'pending',
        0,
        0,
        0,
        False,
        script_id=spec.get('script_id', ''),
        description=spec.get('description', ''),
        next_step_ids=tuple(spec.get('next_step_ids') or ()),
    )


# LLM: 取消与中断检查；步骤与协调边界统一抛出标准异常。
# 函数用途: 检查当前任务或线程是否已被取消或中断。
def _check_stop() -> None:
    raise_if_cancelled()
    if is_interrupted():
        raise InterruptedError('workflow interrupted')


# LLM: 从字典或引用对象提取上游步骤标识。
# 函数用途: 递归解析inputs_map或fallback中的步骤引用。
def _extract_step_refs_from_dict(d: Any, target_set: set[str]) -> None:
    if not isinstance(d, dict):
        return
    sid = d.get('step_id')
    if isinstance(sid, str) and sid != '$inputs' and sid.strip():
        target_set.add(sid.strip())
    fallback = d.get('fallback')
    if isinstance(fallback, dict):
        _extract_step_refs_from_dict(fallback, target_set)


# LLM: 从结构化条件表达式提取上游步骤标识。
# 函数用途: 解析条件分支中依赖的步骤标识。
def _extract_step_refs_from_condition(cond: Any, target_set: set[str]) -> None:
    if not isinstance(cond, dict):
        return
    sid = cond.get('step_id')
    if isinstance(sid, str) and sid != '$inputs' and sid.strip():
        target_set.add(sid.strip())
    ref = cond.get('ref')
    if isinstance(ref, dict):
        _extract_step_refs_from_dict(ref, target_set)
    conditions = cond.get('conditions')
    if isinstance(conditions, list):
        for sub in conditions:
            if isinstance(sub, dict):
                _extract_step_refs_from_condition(sub, target_set)


# LLM: 依赖图构建与静态检验；拒绝未知节点、自依赖与循环依赖。
# 函数用途: 从工作流定义中提取并校验全部依赖关系与拓扑顺序。
def build_dag(
    steps: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[dict[str, set[str]], dict[str, set[str]], list[str]]:
    all_step_ids: list[str] = []
    seen: set[str] = set()
    for s in steps:
        sid = s.get('step_id')
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError(f"Invalid or missing step_id in step specification: {s}")
        if sid in seen:
            raise ValueError(f"Duplicate step_id in workflow definition: '{sid}'")
        seen.add(sid)
        all_step_ids.append(sid)

    step_set = set(all_step_ids)
    deps: dict[str, set[str]] = {sid: set() for sid in all_step_ids}
    reverse_deps: dict[str, set[str]] = {sid: set() for sid in all_step_ids}

    for s in steps:
        sid = s['step_id']
        deps_spec = s.get('depends_on') or ()
        if isinstance(deps_spec, str):
            deps_spec = (deps_spec,)
        for d in deps_spec:
            if not isinstance(d, str) or not d.strip():
                raise ValueError(f"Invalid dependency in step '{sid}': {d}")
            deps[sid].add(d.strip())

        inputs_map = s.get('inputs_map') or {}
        if isinstance(inputs_map, dict):
            for ref in inputs_map.values():
                if isinstance(ref, dict):
                    _extract_step_refs_from_dict(ref, deps[sid])

        condition = s.get('condition')
        if isinstance(condition, dict):
            _extract_step_refs_from_condition(condition, deps[sid])

    for sid, dep_set in deps.items():
        for d in dep_set:
            if d not in step_set:
                raise ValueError(f"Unknown upstream dependency '{d}' for step '{sid}'")
            if d == sid:
                raise ValueError(f"Self-dependency cycle detected on step '{sid}'")
            reverse_deps[d].add(sid)

    in_degree = {sid: len(dep_set) for sid, dep_set in deps.items()}
    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    topo_order: list[str] = []

    heapify(queue)
    while queue:
        curr = heappop(queue)
        topo_order.append(curr)
        for nxt in reverse_deps[curr]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                heappush(queue, nxt)

    if len(topo_order) != len(all_step_ids):
        unresolved = [sid for sid, deg in in_degree.items() if deg > 0]
        raise ValueError(
            f"Cycle detected in workflow dependency graph involving steps: {sorted(unresolved)}"
        )

    return deps, reverse_deps, topo_order


class _ReadySteps:
    def __init__(self, deps, reverse_deps, step_status):
        self.reverse_deps = reverse_deps
        self.remaining = {sid: sum(step_status.get(dep) not in SUCCESS for dep in deps[sid])
                          for sid in deps if step_status[sid] == 'pending'}
        self.sequence = count()
        self.ready = [(next(self.sequence), sid) for sid in sorted(self.remaining) if self.remaining[sid] == 0]

    def complete(self, step_id):
        for downstream in sorted(self.reverse_deps[step_id]):
            if downstream in self.remaining:
                self.remaining[downstream] -= 1
                if self.remaining[downstream] == 0:
                    self.enqueue(downstream)

    def enqueue(self, step_id):
        heappush(self.ready, (next(self.sequence), step_id))


# LLM: 判断定义是否包含非空 depends_on 或 execution_mode == 'dag'。
# 函数用途: 决定工作流引擎是走 DAG 调度还是传统顺序推进。
def should_schedule_dag(definition: dict[str, Any]) -> bool:
    if not isinstance(definition, dict):
        return False
    metadata = (
        definition.get('descriptor', {}).get('metadata', {})
        if isinstance(definition.get('descriptor'), dict)
        else {}
    )
    if definition.get('execution_mode') == 'dag' or metadata.get('execution_mode') == 'dag':
        return True
    steps = definition.get('steps', ())
    return any(bool(s.get('depends_on')) for s in steps)


# LLM: 判定下游节点是否被上游已跳过的前置步骤阻断。
# 函数用途: 当上游步骤跳过且下游未提供有效兜底值时级联跳过。
def _is_blocked_by_skipped_upstream(
    spec: dict[str, Any],
    deps: dict[str, set[str]],
    step_status: dict[str, str],
) -> bool:
    sid = spec['step_id']
    skipped_deps = {d for d in deps.get(sid, ()) if step_status.get(d) == 'skipped'}
    if not skipped_deps:
        return False
    declared = spec.get('depends_on') or ()
    explicit = {declared} if isinstance(declared, str) else set(declared)
    inputs_map = spec.get('inputs_map') or {}
    return bool(skipped_deps & explicit) or any(
        isinstance(ref, dict) and isinstance(ref.get('step_id'), str)
        and ref['step_id'] in skipped_deps and 'fallback' not in ref for ref in inputs_map.values())


def _clear_unadmitted_worker_effect(step, spec):
    """未进入工作流效果准入即失败/取消的非 foreach 步骤，清除协调线程预标记的 effect_started。"""
    if (
        step.status in ('failed', 'cancelled')
        and getattr(step, '_worker_effect_admitted', None) is False
        and spec.get('foreach') is None  # foreach 父步骤自行维护批义务，不能清除预标记
    ):
        step.escalation_result['effect_started'] = False
        step.escalation_result.setdefault('effect_outcome', 'not_started')


def _run_effect_observed(record):
    return any(step.escalation_result.get('effect_started') or step.escalation_result.get('cleanup_error')
               for step in record.steps)


def _settle_worker_effect(engine, run_id, step, *, interrupted=False):
    try:
        marker = engine.store.step_effect_marker(run_id, step)
    except (OSError, ValueError):
        marker = None
    settle_step_effect(step, marker=marker, interrupted=interrupted)


def _worker_with_effect_admission(engine, run_id, step):
    from .governance import WorkflowEffectPhase

    worker = copy(engine)
    guard = Lock()
    admitted = False

    def authorize(run, phase, *, definition=None):
        nonlocal admitted
        result = engine._authorize_run(run, phase, definition=definition)
        if not isinstance(phase, WorkflowEffectPhase) or run.workflow_run_id != run_id or phase.step_id != step.step_id:
            return result
        # foreach 批共享父尝试的一次准入，锁只覆盖本地标记的耐久发表。
        with guard:
            _check_stop()
            if not admitted:
                engine.store.admit_step_effect(run_id, step)
                step.escalation_result.update(effect_state='admitted', effect_started=True)
                admitted = True
        return result

    worker._authorize_run = authorize
    return worker


def _run_step_worker(engine, step, spec, local_outputs, definition, local_record):
    """工作线程只发表准入标记，步骤和运行快照由协调线程提交。"""
    clock = time.monotonic()
    step._worker_effect_admitted = False
    reserved = step.escalation_result.get('effect_state') == 'reserved'
    run_id = local_record.workflow_run_id
    try:
        if reserved:
            engine = _worker_with_effect_admission(engine, run_id, step)
        _check_stop()
        engine._execute_step_with_retry_and_watermarks(
            step, spec, local_outputs, definition, local_record
        )
        _check_stop()
        step.ended_at = time.time()
        step.duration_ms = (time.monotonic() - clock) * 1000
        if reserved:
            _settle_worker_effect(engine, run_id, step)
        else:
            _clear_unadmitted_worker_effect(step, spec)
        return (step, None)
    except RetryWake as retry:
        return (step, retry)
    except (KeyboardInterrupt, SystemExit, ToolCancelled, InterruptedError) as exc:
        step.ended_at = time.time()
        step.duration_ms = (time.monotonic() - clock) * 1000
        if reserved:
            _settle_worker_effect(engine, run_id, step, interrupted=True)
        return (step, exc)
    except Exception as exc:
        step.status = 'failed'
        step.error_code = step.error_code or getattr(
            exc, 'error_code', 'STEP_EXECUTION_ERROR'
        )
        step.error_message = str(exc)
        step.needs_repair = True
        step.ended_at = time.time()
        step.duration_ms = (time.monotonic() - clock) * 1000
        if reserved:
            _settle_worker_effect(engine, run_id, step)
        else:
            _clear_unadmitted_worker_effect(step, spec)
        return (step, None)


# LLM: 核心 DAG 并行调度执行入口；所有账本写操作在协调线程原子发生。
# 函数用途: 调度并执行工作流 DAG 节点，支持并行波次、断点恢复与准入观察器。
def execute_workflow_dag(*, engine, record, definition, start, execution_state):
    state = execution_state
    steps = definition.get('steps', ())
    if not steps:
        record = replace_run_record(record, dict(status='succeeded', ended_at=time.time()))
        engine.store.save_run(record)
        state.update(record=record, committed=True)
        return record

    step_specs = {s['step_id']: s for s in steps}
    deps, reverse_deps, _ = build_dag(steps)

    timer = time.monotonic()
    progress = {'seq': record.progress_seq}

    metadata = (
        definition.get('descriptor', {}).get('metadata', {})
        if isinstance(definition.get('descriptor'), dict)
        else {}
    )
    max_parallel_val = (
        definition.get('max_parallel_steps')
        or metadata.get('max_parallel_steps')
        or getattr(engine, 'max_parallel_steps', 4)
    )
    try:
        max_parallel_int = int(max_parallel_val)
    except (TypeError, ValueError):
        max_parallel_int = 4
    max_parallel = min(max(1, max_parallel_int), resolved_max_parallel())

    outputs: dict[str, Any] = {}
    step_status: dict[str, str] = {}
    previous_steps = {s.step_id: s for s in record.steps}

    for step in record.steps:
        if step.status in SUCCESS:
            step_status[step.step_id] = step.status
            if step.status != 'skipped' and step.output_snapshot:
                outputs[step.step_id] = payload(step.output_snapshot)
        else:
            step_status[step.step_id] = 'pending'

    for spec in steps:
        sid = spec['step_id']
        if sid not in step_status:
            step_status[sid] = 'pending'

    for spec in steps:
        sid = spec['step_id']
        if step_status[sid] == 'pending':
            p_step = previous_steps.get(sid) or _pending_step(spec)
            engine._step_progress(record, p_step, progress)

    readiness = _ReadySteps(deps, reverse_deps, step_status)
    delayed = []
    admission_observer = getattr(engine, 'concurrency_admission_observer', None)

    halt_event = False
    halt_reason: dict[str, Any] = {'type': None, 'step': None, 'exc': None}

    def _mark_step_skipped(spec: dict[str, Any], reason: str = 'upstream_skipped') -> None:
        nonlocal record
        sid = spec['step_id']
        prev = previous_steps.get(sid)
        step = (
            replace(prev, status='skipped', ended_at=time.time(), duration_ms=0.0)
            if prev is not None
            else _pending_step(spec)
        )
        step.status = 'skipped'
        step.escalation_result['skip_reason'] = reason
        step.ended_at = time.time()
        step.duration_ms = 0.0
        step.next_step_ids = tuple(sorted(reverse_deps.get(sid, ())))
        record = replace_run_record(
            record,
            dict(
                steps=tuple(
                    step if item.step_id == sid else item for item in record.steps
                )
            ),
        )
        state.update(record=record, phase='step_publish')
        record = engine.store.save_step_result(record.workflow_run_id, step)
        engine._step_progress(record, step, progress)
        state.update(record=record)
        step_status[sid] = 'skipped'
        readiness.complete(sid)

    def _process_completed_step(sid: str, completed_step: StepExecutionRecord, exc: BaseException | None) -> None:
        nonlocal record, halt_event
        if isinstance(exc, RetryWake):
            current = engine.store.reserve_step_retry(
                record.workflow_run_id, exc.step, expected_attempt_id=exc.expected_attempt_id)
            if current is None:
                raise ValueError('WORKFLOW_RUN_REVISION_CONFLICT')
            record = current
            previous_steps[sid] = exc.step
            step_status[sid] = 'pending'
            heappush(delayed, (pending_retry(exc.step)['next_eligible_at'], sid))
            state.update(record=record)
            return
        record = replace_run_record(
            record,
            dict(
                steps=tuple(
                    completed_step if item.step_id == sid else item for item in record.steps
                )
            ),
        )
        if exc is not None:
            # 中断回执先沿原步骤提交；外层暂停以同一 durable step 快照聚合。
            state.update(record=record, phase='step_publish')
            record = engine.store.save_step_result(record.workflow_run_id, completed_step)
            state['record'] = record
            if halt_reason['type'] != 'cancel':
                halt_reason['type'] = 'cancel'
                halt_reason['exc'] = exc
            halt_event = True
            return

        state.update(record=record, phase='step_publish')
        if completed_step.status in ('succeeded', 'escalated_recovered'):
            engine._publish_step_watermark(completed_step, step_specs[sid], outputs, record)
        record = engine.store.save_step_result(record.workflow_run_id, completed_step)
        engine._step_progress(record, completed_step, progress)
        state.update(record=record)

        step_status[sid] = completed_step.status
        if completed_step.status == 'waiting_for_agent':
            if halt_reason['type'] != 'cancel':
                halt_reason['type'] = 'waiting_for_agent'
                halt_reason['step'] = completed_step
            halt_event = True
        elif completed_step.status in ('failed', 'paused_for_resume'):
            if not completed_step.needs_repair and completed_step.output_snapshot:
                outputs[sid] = payload(completed_step.output_snapshot)
            if halt_reason['type'] != 'cancel':
                halt_reason['type'] = completed_step.status
                halt_reason['step'] = completed_step
            halt_event = True
        elif completed_step.status in SUCCESS:
            readiness.complete(sid)
            if completed_step.status != 'skipped':
                outputs[sid] = payload(completed_step.output_snapshot)

    ready = readiness.ready
    with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix='workflow-dag') as pool:
        active_futures: dict[Any, tuple[str, dict[str, Any]]] = {}

        while (ready or delayed or active_futures) and not halt_event:
            now = time.time()
            while delayed and delayed[0][0] <= now:
                readiness.enqueue(heappop(delayed)[1])
            while ready and len(active_futures) < max_parallel and not halt_event:
                _, sid = heappop(ready)
                spec = step_specs[sid]
                prev = previous_steps.get(sid)
                retry = pending_retry(prev) if prev is not None else None
                if retry and retry['next_eligible_at'] > time.time():
                    heappush(delayed, (retry['next_eligible_at'], sid))
                    continue

                if _is_blocked_by_skipped_upstream(spec, deps, step_status):
                    _mark_step_skipped(spec, reason='upstream_skipped')
                    continue

                if spec.get('condition'):
                    try:
                        matched = evaluate_condition(spec['condition'], dict(outputs), record.inputs)
                    except ConditionIndeterminate as error:
                        step = _pending_step(spec)
                        mark_condition_indeterminate(step, error)
                        step.ended_at = time.time()
                        step.next_step_ids = tuple(sorted(reverse_deps.get(sid, ())))
                        _process_completed_step(sid, step, None)
                        break
                    if not matched:
                        _mark_step_skipped(spec, reason='condition_false')
                        continue

                # 协调线程在派发前发表 running 状态。
                from .governance import pending_review_not_started
                pending_review = prev is not None and pending_review_not_started(record, prev)
                template = (
                    StepExecutionRecord.from_dict(prev.to_dict())
                    if prev is not None and (spec.get('foreach') is not None or pending_review or continuing_retry(prev))
                    else _pending_step(spec)
                )
                step = replace(
                    template,
                    status='running',
                    started_at=time.time(),
                    input_snapshot=payload(record.inputs),
                    error_code='',
                    error_message='',
                    needs_repair=False,
                )
                if spec.get('retry_policy'):
                    pol = spec['retry_policy']
                    if isinstance(pol, dict):
                        step.max_attempts = int(pol.get('max_attempts', 1))
                if retry:
                    start_retry(step, reserve_effect=True)
                elif not pending_review:
                    step.escalation_result['attempt_id'] = uuid.uuid4().hex
                step.next_step_ids = tuple(sorted(reverse_deps.get(sid, ())))
                step.escalation_result.update(effect_state='reserved', effect_started=False)
                step.escalation_result.pop('effect_outcome', None)

                state.update(record=record, phase='step_publish')
                record = engine.store.save_step_result(record.workflow_run_id, step)
                engine._step_progress(record, step, progress)
                state.update(record=record, phase='started', started=True)

                if callable(admission_observer):
                    try:
                        admission_observer(
                            {
                                'event': 'step_admitted',
                                'step_id': sid,
                                'workflow_run_id': record.workflow_run_id,
                            }
                        )
                    except Exception:
                        pass

                local_record = replace_run_record(record, {})
                local_record._batch_worker = True
                local_record._defer_step_retry = True
                local_outputs = dict(outputs)
                ctx = copy_context()
                fut = pool.submit(
                    ctx.run,
                    _run_step_worker,
                    engine,
                    step,
                    spec,
                    local_outputs,
                    definition,
                    local_record,
                )
                active_futures[fut] = (sid, spec)

            if halt_event:
                break

            if active_futures:
                timeout = max(0.0, delayed[0][0] - time.time()) if delayed and len(active_futures) < max_parallel else None
                done, _ = wait(active_futures, timeout=timeout, return_when=FIRST_COMPLETED)
                for fut in done:
                    done_sid, _ = active_futures.pop(fut)
                    c_step, c_exc = fut.result()
                    _process_completed_step(done_sid, c_step, c_exc)
            elif delayed:
                state['started'] = _run_effect_observed(record)
                wait_for_retry(engine, delayed[0][0])

        if active_futures:
            wait(active_futures.keys())
            for fut in list(active_futures.keys()):
                done_sid, _ = active_futures.pop(fut)
                try:
                    c_step, c_exc = fut.result()
                    _process_completed_step(done_sid, c_step, c_exc)
                except Exception as exc:
                    # 排空错误可能晚于外部效果，保留现有步骤与实际已发表的同伴回执。
                    current = engine.store.load_run(record.workflow_run_id)
                    step = next(item for item in current.steps if item.step_id == done_sid)
                    code = 'WORKFLOW_EXECUTION_FAILED'
                    _LOG.error("workflow drain completion failed", extra={
                        'error_code': code, 'workflow_run_id': record.workflow_run_id, 'step_id': done_sid,
                        'phase': 'drain', 'error_type': type(exc).__name__, 'effect_outcome': 'unknown',
                    })
                    step.status, step.needs_repair, step.ended_at = 'failed', True, time.time()
                    step.error_code = code
                    step.escalation_result['drain_diagnostic'] = {
                        'code': code, 'phase': 'drain', 'error_type': type(exc).__name__,
                    }
                    settle_step_effect(step, marker=None, drain_failed=True)
                    _process_completed_step(done_sid, step, None)
            active_futures.clear()

    if halt_reason['type'] == 'cancel':
        state['started'] = _run_effect_observed(record)
        raise halt_reason['exc']

    if halt_reason['type'] == 'waiting_for_agent':
        _check_stop()
        waiting_step = halt_reason['step']
        engine._step_progress(record, waiting_step, progress)
        record = replace_run_record(
            record,
            dict(
                status='waiting_for_agent',
                needs_repair=False,
                current_step_id=waiting_step.step_id,
                ended_at=0,
                breakpoint=None,
            ),
        )
        execution_receipt(
            record,
            phase='waiting_for_agent',
            effect_outcome=(
                'unknown'
                if waiting_step.escalation_result.get('submission_state') == 'unknown'
                else 'running'
            ),
        )
        engine.store.save_run(record)
        state.update(record=record, committed=True)
        return record

    if halt_reason['type'] in ('failed', 'paused_for_resume'):
        failed_step = halt_reason['step']
        record = replace_run_record(
            record,
            dict(
                status=halt_reason['type'],
                needs_repair=failed_step.needs_repair if failed_step else True,
                current_step_id=failed_step.step_id if failed_step else '',
                failed_step_id=(
                    failed_step.step_id
                    if (failed_step and failed_step.needs_repair)
                    else ''
                ),
            ),
        )
    else:
        record = replace_run_record(
            record,
            dict(status='succeeded', needs_repair=False, current_step_id='', failed_step_id=''),
        )

    last_step_id = definition['steps'][-1]['step_id'] if definition.get('steps') else ''
    if last_step_id and last_step_id in outputs:
        final_out = outputs[last_step_id]
    elif outputs:
        final_out = next(reversed(outputs.values()), {})
    else:
        final_out = {}

    record = replace_run_record(
        record,
        dict(
            ended_at=time.time(),
            duration_ms=record.duration_ms + (time.monotonic() - timer) * 1000,
            final_output=final_out,
        ),
    )
    state.update(record=record, phase='finalize')
    record = engine._finalize_record(record, definition)
    if record.status == 'failed':
        engine._notify_unattended_run_failure(record)
    _check_stop()
    execution_receipt(
        record,
        phase='finalize' if record.breakpoint else 'terminal',
        effect_outcome=(
            'unknown' if any(item.escalation_result.get('effect_state') == 'unknown' for item in record.steps)
            else 'confirmed' if record.status == 'succeeded' else 'failed'
        ),
        definition_ref=workflow_definition_ref(definition, kind='workflow'),
    )
    state.update(record=record, phase='terminal')
    engine.store.save_run(record)
    state.update(record=record, committed=True)
    if record.breakpoint:
        engine._notify_breakpoint(record)
    engine._record_summary(record)
    return record
