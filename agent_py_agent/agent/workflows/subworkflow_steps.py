"""Formal subworkflow step executor (WF-09).

Executes nested subworkflows with input/output mapping, cycle detection,
bounded depth enforcement, pinned revision verification, and observable parent/child linkage.
"""
from __future__ import annotations

import contextvars
import json
import time
import uuid
from typing import Any

from ..assets.descriptor import execution_definition_ref
from ..assets.workflow_store import (
    BUSINESS_WORKFLOW_METADATA,
    WorkflowDefinition,
    WorkflowStore,
    normalize_workflow_asset_id,
    safe_asset_id,
)
from ..conversation.business_contracts import BusinessExecutionContext
from .business_scope import workflow_state
from .models import StepExecutionRecord, WorkflowRunRecord
from .pinned_definitions import (
    PinnedDefinitionError,
    freeze_execution_definition,
    resolve_definition,
)
from .run_store import _concurrency_settled, replace_run_record

_ACTIVE_SUBWORKFLOW_ANCESTRY: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    '_ACTIVE_SUBWORKFLOW_ANCESTRY', default=()
)
_ACTIVE_SUBWORKFLOW_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    '_ACTIVE_SUBWORKFLOW_DEPTH', default=0
)
_ACTIVE_SUBWORKFLOW_RUN_CHAIN: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    '_ACTIVE_SUBWORKFLOW_RUN_CHAIN', default=()
)


def _resolve_target_child_workflow_id(spec: dict) -> str:
    config = spec.get('config') or {}
    for candidate in (
        spec.get('subworkflow_ref'),
        config.get('workflow_id'),
        config.get('subworkflow_id'),
        config.get('subworkflow_ref'),
    ):
        if isinstance(candidate, str) and candidate.strip():
            normalized = normalize_workflow_asset_id(candidate.strip())
            if normalized:
                return normalized
    return ''


def _collect_ancestry(run: WorkflowRunRecord, engine) -> tuple[tuple[str, ...], int]:
    ctx_ancestry = _ACTIVE_SUBWORKFLOW_ANCESTRY.get()
    ctx_depth = _ACTIVE_SUBWORKFLOW_DEPTH.get()

    store_ancestry: list[str] = []
    curr_parent_id = getattr(run, 'parent_workflow_run_id', '')
    visited_run_ids: set[str] = set()
    while curr_parent_id and curr_parent_id not in visited_run_ids and len(store_ancestry) < 20:
        visited_run_ids.add(curr_parent_id)
        try:
            read_raw = getattr(engine.store, '_read', None)
            if callable(read_raw):
                parent_data = read_raw(curr_parent_id, 'run.json')
                wf_id = parent_data.get('workflow_id', '') if isinstance(parent_data, dict) else ''
                next_parent_id = parent_data.get('parent_workflow_run_id', '') if isinstance(parent_data, dict) else ''
            else:
                parent_rec = engine.store.load_run(curr_parent_id)
                wf_id = getattr(parent_rec, 'workflow_id', '') if parent_rec else ''
                next_parent_id = getattr(parent_rec, 'parent_workflow_run_id', '') if parent_rec else ''
            if wf_id:
                store_ancestry.append(wf_id)
                curr_parent_id = next_parent_id
            else:
                break
        except Exception:
            break
    store_ancestry.reverse()

    combined_ancestry = tuple(dict.fromkeys((*ctx_ancestry, *store_ancestry)))
    if run.workflow_id and run.workflow_id not in combined_ancestry:
        combined_ancestry = (*combined_ancestry, run.workflow_id)

    depth = max(ctx_depth, len(store_ancestry), getattr(run, 'subworkflow_depth', 0))
    return combined_ancestry, depth


def _subworkflow_attempt(step: StepExecutionRecord) -> str | int:
    return step.escalation_result.get('attempt_id') or step.attempt_count or 1


def _child_run_id(run_id: str, step_id: str, attempt: str | int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f'subworkflow:{run_id}:{step_id}:{attempt}').hex


def _binding(run: WorkflowRunRecord, step: StepExecutionRecord, child_id: str) -> dict:
    return {'kind': 'subworkflow', 'parent_run_id': run.workflow_run_id, 'step_id': step.step_id,
            'attempt': _subworkflow_attempt(step), 'child_run_id': child_id}


def has_subworkflow_obligation(step: StepExecutionRecord) -> bool:
    if step.mode != 'subworkflow' or step.batches:
        return False
    facts = step.escalation_result
    receipt = facts.get('subworkflow_receipt') or {}
    if step.status == 'waiting_for_agent' or facts.get('effect_outcome') == 'unknown':
        return True
    if ((isinstance(receipt, dict) and receipt.get('status') in ('succeeded', 'failed', 'skipped', 'cancelled'))
            or facts.get('effect_outcome') == 'not_started'):
        return False
    return bool(facts.get('obligation') or step.subworkflow_run_id or receipt
                or (step.status in ('running', 'paused_for_resume') and facts.get('effect_started')
                    and facts.get('attempt_id')))


def read_obligated_child(engine, run: WorkflowRunRecord, step: StepExecutionRecord, spec: dict):
    """只查原尝试指向的子运行；旧等待步骤从原回执取得身份。"""
    facts = step.escalation_result
    receipt = facts.get('subworkflow_receipt') or {}
    existing = facts.get('obligation')
    if not isinstance(receipt, dict) or (existing is not None and not isinstance(existing, dict)):
        raise PermissionError('SUBWORKFLOW_SCOPE_DENIED')
    child_id = (existing or {}).get('child_run_id') or receipt.get('child_run_id') or step.subworkflow_run_id
    if not child_id and step.status in ('running', 'paused_for_resume') and facts.get('attempt_id'):
        # DAG 协调器先保存尝试号，worker 返回前崩溃时也可按原尝试查同一个子运行。
        child_id = _child_run_id(run.workflow_run_id, step.step_id, _subworkflow_attempt(step))
    if not child_id:
        return '', None
    binding = _binding(run, step, child_id)
    if (not isinstance(child_id, str) or child_id == run.workflow_run_id
            or spec['step_id'] != step.step_id or spec['mode'] != step.mode
            or (existing is not None and existing != binding)
            or ((existing is not None or facts.get('attempt_id'))
                and child_id != _child_run_id(run.workflow_run_id, step.step_id, binding['attempt']))
            or any(ref and ref != child_id for ref in (receipt.get('child_run_id'), step.subworkflow_run_id))):
        raise PermissionError('SUBWORKFLOW_SCOPE_DENIED')
    try:
        child = engine.store.load_run(child_id)
    except FileNotFoundError:
        return child_id, None
    if (child.parent_workflow_run_id != run.workflow_run_id or child.owner_id != run.owner_id
            or child.workflow_id != _resolve_target_child_workflow_id(spec)):
        raise PermissionError('SUBWORKFLOW_SCOPE_DENIED')
    facts['obligation'] = binding
    step.subworkflow_run_id = step.effect_id = child_id
    facts['subworkflow_receipt'] = {**receipt, 'child_workflow_id': child.workflow_id,
                                   'child_run_id': child_id, 'depth': child.subworkflow_depth}
    return child_id, child


def mark_child_missing(step: StepExecutionRecord, child_id: str) -> None:
    facts = step.escalation_result
    facts['effect_outcome'] = 'unknown'
    receipt = facts.setdefault('subworkflow_receipt', {})
    receipt.update(status='unknown', effect_outcome='unknown')
    if child_id:
        receipt['child_run_id'] = child_id
    step.status, step.needs_repair = 'failed', True
    step.error_code = 'WORKFLOW_SUBWORKFLOW_CHILD_MISSING'
    step.error_message = '原子工作流编号或运行记录缺失，执行结果未知。请核对原步骤收据并恢复原子运行记录，禁止重新派发。'
    step.ended_at = step.ended_at or time.time()


def child_is_settled(child: WorkflowRunRecord) -> bool:
    return child.status in ('succeeded', 'failed', 'skipped', 'cancelled') and _concurrency_settled(child)


def apply_child_result(step: StepExecutionRecord, child: WorkflowRunRecord, spec: dict) -> None:
    """首次执行与恢复沿同一套输出映射和失败回执。"""
    step.escalation_result['subworkflow_receipt']['status'] = child.status
    if child.status == 'succeeded':
        step.status, step.needs_repair = 'succeeded', False
        step.error_code = step.error_message = ''
        outputs: dict[str, Any] = {}
        for child_step in child.steps:
            if child_step.status in ('succeeded', 'escalated_recovered') and isinstance(child_step.output_snapshot, dict):
                outputs.update(child_step.output_snapshot)
        if isinstance(child.final_output, dict):
            outputs.update(child.final_output)
        mapping = spec.get('output_mapping') or (spec.get('config') or {}).get('output_mapping')
        step.output_snapshot = {
            key: outputs.get(source) if isinstance(source, str) else source
            for key, source in mapping.items()
        } if isinstance(mapping, dict) else outputs
    elif child.status in ('waiting_for_agent', 'queued', 'running', 'cancelling'):
        step.status = 'waiting_for_agent'
    elif child.status in ('paused_for_resume', 'paused') and not child.needs_repair:
        step.status = 'paused_for_resume'
    else:
        step.status, step.needs_repair = 'failed', True
        failed = next((item for item in child.steps if item.status in ('failed', 'paused_for_resume')
                       or (item.needs_repair and item.error_code)), None)
        step.error_code = (failed and failed.error_code) or child.failed_step_id or 'SUBWORKFLOW_RUN_FAILED'
        step.error_message = (failed and failed.error_message) or (
            f'Child workflow run {child.workflow_run_id} failed with status {child.status}')
    if child_is_settled(child):
        # 子终态只结清等待义务；已运行子流程的失败不授予自动重放许可。
        step.escalation_result.pop('effect_outcome', None)
        step.escalation_result['subworkflow_receipt'].pop('effect_outcome', None)
    elif child.status in ('failed', 'skipped', 'cancelled'):
        step.escalation_result['effect_outcome'] = 'unknown'
        step.escalation_result['subworkflow_receipt']['effect_outcome'] = 'unknown'


def execute_subworkflow_step(
    *,
    engine,
    run: WorkflowRunRecord,
    workflow: Any,
    step: StepExecutionRecord,
    spec: dict,
    context: Any = None,
    outputs: dict | None = None,
) -> StepExecutionRecord:
    """Execute a formal nested subworkflow step (WF-09)."""
    child_ref = _resolve_target_child_workflow_id(spec)
    if not child_ref:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_REF_INVALID'
        step.error_message = 'Subworkflow reference is missing or invalid'
        step.needs_repair = True
        return step

    try:
        child_workflow_id = safe_asset_id(child_ref)
    except Exception as exc:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_REF_INVALID'
        step.error_message = f'Invalid child workflow reference: {exc}'
        step.needs_repair = True
        return step

    ancestry, current_depth = _collect_ancestry(run, engine)
    max_depth = getattr(engine, 'max_subworkflow_depth', 5)

    if current_depth >= max_depth:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_MAX_DEPTH_EXCEEDED'
        step.error_message = f'Subworkflow maximum depth exceeded: {current_depth} >= {max_depth}'
        step.needs_repair = True
        return step

    if child_workflow_id in ancestry or child_workflow_id == run.workflow_id:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_CYCLE_DETECTED'
        step.error_message = (
            f'Cycle detected in subworkflow invocation: {child_workflow_id} already in ancestry {ancestry}'
        )
        step.needs_repair = True
        return step

    store = getattr(engine, 'workflow_store', None) or WorkflowStore(
        engine.owner_home, workspace_root=getattr(engine, 'workspace_root', None)
    )

    config = spec.get('config') or {}
    pinned_revision = config.get('pinned_revision') or spec.get('pinned_revision')
    pinned_definition_ref = config.get('pinned_definition_ref') or spec.get('pinned_definition_ref')
    definition_version = config.get('definition_version') or spec.get('definition_version')
    if isinstance(pinned_revision, int) and definition_version is None:
        definition_version = pinned_revision

    if definition_version is not None:
        try:
            child_def_dict, _ = resolve_definition(engine.owner_home, child_workflow_id, int(definition_version))
            child_def = WorkflowDefinition.from_any(child_def_dict)
        except PinnedDefinitionError as exc:
            step.status = 'failed'
            step.error_code = 'SUBWORKFLOW_PIN_MISMATCH'
            step.error_message = f'Failed to resolve pinned definition version {definition_version}: {exc}'
            step.needs_repair = True
            return step
        except Exception as exc:
            step.status = 'failed'
            step.error_code = 'SUBWORKFLOW_PIN_MISMATCH'
            step.error_message = f'Error resolving pinned definition: {exc}'
            step.needs_repair = True
            return step
    else:
        try:
            child_def = store.get_workflow(child_workflow_id)
        except Exception as exc:
            step.status = 'failed'
            step.error_code = 'SUBWORKFLOW_REF_INVALID'
            step.error_message = f'Error loading child workflow: {exc}'
            step.needs_repair = True
            return step

    if child_def is None:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_REF_INVALID'
        step.error_message = f'Child workflow not found: {child_workflow_id}'
        step.needs_repair = True
        return step

    # 固定版本只冻结执行内容；当前Owner资产仍是范围、生命周期与启停的权威来源。
    try:
        current_child = store.get_workflow(child_workflow_id)
    except Exception as exc:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_REF_INVALID'
        step.error_message = f'Error loading current child authority: {exc}'
        step.needs_repair = True
        return step
    if current_child is None:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_REF_INVALID'
        step.error_message = f'Current child workflow not found: {child_workflow_id}'
        step.needs_repair = True
        return step

    child_exec_ref = (
        getattr(child_def, 'execution_definition_ref', '')
        or execution_definition_ref(child_def.to_dict(), kind='workflow')
    )

    if pinned_definition_ref:
        expected_ref = str(pinned_definition_ref).strip()
        if expected_ref.startswith('sha256:'):
            expected_ref = expected_ref[len('sha256:'):].strip()
        actual_ref = child_exec_ref
        if actual_ref.startswith('sha256:'):
            actual_ref = actual_ref[len('sha256:'):].strip()
        if expected_ref != actual_ref:
            step.status = 'failed'
            step.error_code = 'SUBWORKFLOW_PIN_MISMATCH'
            step.error_message = f'Pinned definition ref mismatch: expected {pinned_definition_ref}, got {child_exec_ref}'
            step.needs_repair = True
            return step

    if pinned_revision is not None and not isinstance(pinned_revision, int):
        expected_rev = str(pinned_revision).strip()
        actual_rev = str(
            getattr(child_def.descriptor, 'revision', '')
            or child_def.descriptor.metadata.get('revision', '')
        )
        if expected_rev != actual_rev and expected_rev != child_exec_ref:
            step.status = 'failed'
            step.error_code = 'SUBWORKFLOW_PIN_MISMATCH'
            step.error_message = f'Pinned revision mismatch: expected {expected_rev}, got {actual_rev}'
            step.needs_repair = True
            return step

    if (not child_def.enabled or not child_def.descriptor.enabled
            or not current_child.enabled or not current_child.descriptor.enabled
            or workflow_state(current_child)['lifecycle'] != 'active'):
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_DISABLED'
        step.error_message = f'Child workflow {child_workflow_id} is disabled'
        step.needs_repair = True
        return step

    parent_owner = getattr(run, 'owner_id', '')
    child_owner = getattr(current_child.descriptor, 'owner_id', '')
    if parent_owner and child_owner and parent_owner != child_owner:
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_SCOPE_DENIED'
        step.error_message = f'Cross-owner subworkflow invocation denied: {parent_owner} != {child_owner}'
        step.needs_repair = True
        return step

    parent_business_context = getattr(run, 'business_context', {}) or {}
    parent_space_id = (
        parent_business_context.get('space_id')
        if isinstance(parent_business_context, dict)
        else None
    )
    strict_space = (
        isinstance(parent_business_context, dict)
        and (parent_business_context.get('strict_space_governance') is True or parent_business_context.get('space_id'))
    ) or bool(getattr(run, 'scope_snapshot', {}))

    child_state = workflow_state(current_child)
    child_scope = child_state.get('scope', {})
    child_scope_mode = child_scope.get('scope_mode', 'owner_global')
    child_space_ids = tuple(child_scope.get('space_ids', ()))

    if child_scope_mode == 'unassigned' and (parent_space_id or strict_space):
        step.status = 'failed'
        step.error_code = 'SUBWORKFLOW_SCOPE_DENIED'
        step.error_message = f'Cannot invoke unassigned workflow {child_workflow_id} under space governance'
        step.needs_repair = True
        return step

    if parent_space_id:
        if child_scope_mode == 'spaces' and parent_space_id not in child_space_ids:
            step.status = 'failed'
            step.error_code = 'SUBWORKFLOW_SCOPE_DENIED'
            step.error_message = f'Child workflow {child_workflow_id} not permitted in space {parent_space_id}'
            step.needs_repair = True
            return step

    configured_inputs = config.get('inputs') or spec.get('inputs') or {}
    if not isinstance(configured_inputs, dict):
        configured_inputs = {}
    child_inputs = {**configured_inputs, **step.input_snapshot}

    child_run_id = _child_run_id(run.workflow_run_id, step.step_id, _subworkflow_attempt(step))

    child_depth = current_depth + 1
    step.subworkflow_run_id = step.effect_id = child_run_id
    step.escalation_result.update(
        obligation=_binding(run, step, child_run_id),
        subworkflow_receipt={'child_workflow_id': child_workflow_id, 'child_run_id': child_run_id,
                             'pinned_definition_ref': child_exec_ref, 'status': 'dispatching', 'depth': child_depth},
    )
    if not run.ephemeral_debug and not getattr(run, '_batch_worker', False):
        # 先记录原子运行引用。DAG 及 foreach 的尝试号由自己的协调器持久化，worker 不能回抢父锁。
        published = engine.store.save_step_result(run.workflow_run_id, step)
        run.revision, run.effect_outcome = published.revision, published.effect_outcome
    new_ancestry = (*ancestry, child_workflow_id)
    new_run_chain = (*_ACTIVE_SUBWORKFLOW_RUN_CHAIN.get(), run.workflow_run_id)

    token_ancestry = _ACTIVE_SUBWORKFLOW_ANCESTRY.set(new_ancestry)
    token_depth = _ACTIVE_SUBWORKFLOW_DEPTH.set(child_depth)
    token_chain = _ACTIVE_SUBWORKFLOW_RUN_CHAIN.set(new_run_chain)

    try:
        child_bc = None
        if isinstance(getattr(run, 'business_context', None), BusinessExecutionContext):
            child_bc = run.business_context
        elif isinstance(getattr(run, 'business_context', None), dict) and run.business_context:
            try:
                child_bc = BusinessExecutionContext.from_dict(run.business_context)
            except Exception:
                child_bc = None

        child_run = engine.run_workflow(
            workflow=child_workflow_id,
            inputs=child_inputs,
            trigger_kind='subworkflow',
            owner_id=run.owner_id or child_def.descriptor.owner_id,
            business_context=child_bc,
            scope_snapshot=(child_scope if strict_space or current_child.descriptor.metadata.get(
                BUSINESS_WORKFLOW_METADATA) else {}),
            workflow_run_id=child_run_id,
            definition_version=definition_version,
        )
    finally:
        _ACTIVE_SUBWORKFLOW_ANCESTRY.reset(token_ancestry)
        _ACTIVE_SUBWORKFLOW_DEPTH.reset(token_depth)
        _ACTIVE_SUBWORKFLOW_RUN_CHAIN.reset(token_chain)

    if isinstance(child_run, WorkflowRunRecord):
        if (
            child_run.parent_workflow_run_id != run.workflow_run_id
            or child_run.subworkflow_depth != child_depth
        ):
            child_run = replace_run_record(
                child_run,
                dict(
                    parent_workflow_run_id=run.workflow_run_id,
                    subworkflow_depth=child_depth,
                ),
            )
            engine.store.save_run(child_run)

        apply_child_result(step, child_run, spec)
    elif isinstance(child_run, dict) and child_run.get('status') == 'queued':
        step.status = 'waiting_for_agent'
        step.escalation_result['subworkflow_receipt']['status'] = 'queued'
    else:
        result = child_run if isinstance(child_run, dict) else {}
        step.status, step.needs_repair = 'failed', True
        step.error_code = result.get('error_code') or 'SUBWORKFLOW_RUN_FAILED'
        step.error_message = f'Child workflow run failed: {child_run}'
        receipt = step.escalation_result['subworkflow_receipt']
        receipt['status'] = result.get('status', 'failed')
        if result.get('effect_outcome') == 'not_started':
            step.escalation_result['effect_outcome'] = 'not_started'
            receipt['effect_outcome'] = 'not_started'
        else:
            step.escalation_result['effect_outcome'] = 'unknown'
            receipt['effect_outcome'] = 'unknown'

    return step
