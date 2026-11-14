"""把实际候选投递、显式接受与执行回执记入原使用账；不授权、不选方法。"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..conversation.business_contracts import BusinessExecutionContext
from .skill_learning_store import SkillLearningStore
from .skill_usage_ledger import record_skill_usage_events


@dataclass(frozen=True)
class UsageContext:
    task_id: str
    request_id: str
    thread_id: str
    run_id: str


_SCOPE = ContextVar('method_usage_context', default=None)


@contextmanager
def method_usage_scope(params):
    values = _context(params)
    token = _SCOPE.set(UsageContext(**values, run_id=getattr(params, 'run_id', '')))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _producer_params(params):
    return _SCOPE.get() or params


@contextmanager
def _best_effort(source):
    try:
        yield
    except Exception as error:
        logging.getLogger(__name__).warning('METHOD_USAGE_UNRECORDED source=%s exception=%s',
                                           source, type(error).__name__)


def _context(params):
    if isinstance(params, UsageContext):
        return {'task_id': params.task_id, 'request_id': params.request_id, 'thread_id': params.thread_id}
    attrs = getattr(params, 'task_attributes', None) or {}
    # attrs是可携带的任务数据，不能给使用账签任务身份；仅原宿主typed DTO或已冻结scope有此权威。
    business = getattr(params, 'business_context', None)
    task = business.task_id if isinstance(business, BusinessExecutionContext) else ''
    return {'task_id': task or getattr(params, 'task_id', ''),
            'request_id': getattr(params, 'request_id', ''),
            'thread_id': attrs.get('conversation_thread_id', '')}


def method_row(params, *, event, kind, stable_id, version_ref, source, receipt_ref,
               activation_id='', chars=0, method_source=None):
    params = _producer_params(params)
    context = _context(params)
    if not context['task_id'] or not version_ref or not receipt_ref:
        return None
    # 实际执行按原run/receipt去重，其余阶段按本任务/版本去重；读页和重复检索不放大阶段数。
    identity = [context['task_id'], kind, stable_id, version_ref, activation_id, event]
    if event == 'executed':
        identity.append(receipt_ref)
    key = hashlib.sha256('\0'.join(identity).encode()).hexdigest()
    return {'event': event, 'kind': kind, 'skill_id': stable_id, 'source': method_source or source, 'producer': source,
            'version_ref': version_ref, 'activation_id': activation_id,
            'receipt_ref': receipt_ref, 'source_key': key, 'chars': chars, **context}


def snapshot_row(params, entry, *, event, source, receipt_ref='', chars=0):
    kind = 'capability_package' if hasattr(entry, 'package_sha256') else 'skill'
    return method_row(params, event=event, kind=kind, stable_id=entry.stable_id,
        version_ref=entry.package_sha256 if kind == 'capability_package' else entry.content_sha256,
        activation_id=getattr(entry, 'activation_id', ''), source=source,
        method_source='package' if kind == 'capability_package' else entry.source,
        receipt_ref=receipt_ref or getattr(_producer_params(params), 'request_id', '')
                    or f"run:{getattr(_producer_params(params), 'run_id', '')}", chars=chars)


def record_candidates(agent, snapshot, matches, *, source):
    """只消费确实将返回的授权候选；空结果、未投递卡片和未选择均不补事件。"""
    try:
        params = getattr(agent, '_current_run_params', None)
        rows = []
        for match in matches:
            if match.get('kind') not in ('skill', 'capability_package'):
                continue
            ref = match.get('stable_id') or match.get('skill_id')
            entry = snapshot.resolve_reference(ref) if ref else None
            if entry is None:
                continue
            if _context(_producer_params(params))['task_id']:
                rows.extend(snapshot_row(params, entry, event=event, source=source)
                            for event in ('visible', 'manual_search'))
            else:
                # 旧无任务调用保持原人工检索统计，不补造任务归属或可见阶段。
                rows.append({'event': 'manual_search', 'kind': match['kind'],
                    'skill_id': entry.stable_id, 'source': 'package' if match['kind'] == 'capability_package' else entry.source})
        record_skill_usage_events(SkillLearningStore.for_home(agent.home_paths), [x for x in rows if x])
    except Exception as error:
        logging.getLogger(__name__).warning('METHOD_USAGE_CANDIDATES_UNRECORDED: %s', type(error).__name__)


def record_read(agent, entry, *, chars):
    with _best_effort('record_read'):
        params = getattr(agent, '_current_run_params', None)
        row = snapshot_row(params, entry, event='manual_get', source='skill_search.get', chars=chars)
        if row is None:
            kind = 'capability_package' if hasattr(entry, 'package_sha256') else 'skill'
            row = {'event': 'manual_get', 'kind': kind, 'skill_id': entry.stable_id,
                   'source': 'package' if kind == 'capability_package' else entry.source, 'chars': chars}
        record_skill_usage_events(SkillLearningStore.for_home(agent.home_paths), [row])


def record_template_selection(tool, definition, source_ref, *, chars=0):
    with _best_effort('record_template_selection'):
        """仅原源验证和Store登记成功后调用；纯get/preview不代表选择。"""
        params = tool.run_params_provider() if callable(tool.run_params_provider) else None
        row = method_row(params, event='selected', kind='capability_package',
            stable_id=source_ref['stable_id'], version_ref=source_ref['content_sha256'],
            activation_id=source_ref['activation_id'], source='accepted_explicit_template_selection', method_source='package',
            receipt_ref='workflow-definition:' + definition.execution_definition_ref)
        if row:
            read = method_row(params, event='manual_get', kind='capability_package',
                stable_id=source_ref['stable_id'], version_ref=source_ref['content_sha256'],
                activation_id=source_ref['activation_id'], source='workflow_template_source_read', method_source='package',
                receipt_ref=row['receipt_ref'], chars=chars)
            record_skill_usage_events(SkillLearningStore(tool.owner_home / 'data/skill_learning', tool.owner_home / 'skills'), [read, row])


def record_workflow_receipts(tool, params, outcome):
    """观察原run事实；选择、读取、执行分开，不把点选或失败前置门当执行。"""
    if params.get('action') not in ('run', 'resume', 'workflows.run', 'workflow_runs.resume'):
        return
    run_id = outcome.result_envelope.get('workflow_run_id')
    if not run_id:
        return
    try:
        current = tool.run_params_provider() if callable(tool.run_params_provider) else None
        if not _context(_producer_params(current))['task_id']:
            return
        run = tool._engine().store.load_run(run_id)
        steps = [step for step in run.steps if step.escalation_result.get('effect_started') is True
                 and not (step.batches_summary and step.batches_summary.get('total') == 0)
                 and (step.status in ('succeeded', 'escalated_recovered')
                      or step.escalation_result.get('tool_receipt', {}).get('handler_executed') is True
                      or type(step.escalation_result.get('script_execution', {}).get('exit_code')) is int)]
        if not steps:
            return
        rows = []
        frozen = tool._engine().store.load_workflow_snapshot(run_id)
        raw = frozen.get('descriptor', {}).get('metadata', {}).get('derived_from', {})
        reference = raw.get('source_ref', {}) if raw.get('verified') is True else {}
        if reference.get('kind') == 'capability_package':
            rows.append(method_row(current, event='executed', kind='capability_package',
                stable_id=reference['stable_id'], version_ref=reference['content_sha256'],
                activation_id=reference['activation_id'], source='workflow_run_receipt', method_source='package',
                receipt_ref=f'workflow_run:{run_id}'))
        snapshot = tool.skill_snapshot_provider() if callable(tool.skill_snapshot_provider) else None
        specs = {item['step_id']: item for item in frozen.get('steps', [])}
        for step in steps:
            if snapshot is None:
                break
            for ref in step.resolved_skill_ids:
                entry = snapshot.resolve_reference(ref)
                if entry is None:
                    continue
                declared = specs.get(step.step_id, {}).get('skill_ids', [])
                explicit = any((chosen := snapshot.resolve_reference(item)) is not None
                               and chosen.stable_id == entry.stable_id for item in declared)
                selection_source = 'accepted_explicit_skill_selection' if explicit else 'accepted_resolved_skill_selection'
                for event, source in (('selected', selection_source),
                                      ('manual_get', 'workflow_skill_details_read'), ('executed', 'workflow_step_receipt')):
                    rows.append(snapshot_row(current, entry, event=event, source=source,
                        receipt_ref=f'workflow_run:{run_id}'))
        record_skill_usage_events(SkillLearningStore(tool.owner_home / 'data/skill_learning', tool.owner_home / 'skills'),
                                  [row for row in rows if row])
    except Exception as error:
        logging.getLogger(__name__).warning('METHOD_USAGE_WORKFLOW_UNRECORDED: %s', type(error).__name__)
